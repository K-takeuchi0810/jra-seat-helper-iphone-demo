"""Retry only definitive no-seat results; fake time never contacts the live site."""
import datetime as dt
import unittest
from unittest.mock import patch
from engine import Browser,Halt,Unavailable,SeatLimit,ListingPending,ListingConnectionLost,picker_capacity,JST
from selenium.common.exceptions import StaleElementReferenceException,WebDriverException

class Clock:
    def __init__(self):self.now=0;self.cancelled=False;self.waits=[]
    def monotonic(self):return self.now
    def wait(self,seconds):self.waits.append(seconds);self.now+=seconds;return self.cancelled
    def is_set(self):return self.cancelled

class Driver:
    current_url='https://jra-tickets.jp/fixture-seats'
    def __init__(self):self.navigation=[]
    def refresh(self):self.navigation.append('refresh')
    def get(self,url):self.navigation.append(('GET',url));self.current_url=url

class Harness(Browser):
    def __init__(self,clock,results):
        self.stop=clock;self.clock=clock;self.driver=Driver();self.target=self.driver.current_url
        self.results=list(results);self.calls=[];self.logs=[];self.events=[];self.payment=False;self.confirm=False
        self.log=self.logs.append
    def check(self):
        if self.stop.is_set():raise Halt('中止')
    def guard(self):self.check()
    def blockers(self):pass
    def seats(self):return [{'name':'A指定席','available':True}]
    def reserve(self,seat,count,area):
        self.calls.append((seat['name'],count,area))
        result=self.results.pop(0)
        if result=='capacity':raise SeatLimit('この席種の上限は1席')
        if result=='no':raise Unavailable('指定枚数を確保不可')
        if result=='unknown':raise Halt('結果不明')
        self.event('payment_ready')
    def text(self):return 'おまかせ席選択 ただいまご予約できる最大席数は1席です'
    def outcome(self):return 'unavailable'
    def visible(self,selector):return [object()] if self.confirm and selector=='#confirmDialog' else []
    def has_payment_evidence(self):return self.payment
    def event(self,event):self.events.append(event)
    def run_fixture(self,watch=True,minutes=5,interval=60):
        with patch('engine.time',self.clock):
            self.run(['A指定席'],2,'前列',dt.datetime.now(JST)-dt.timedelta(seconds=1),watch,interval,minutes)

class RetryTests(unittest.TestCase):
    def preflight_browser(self,failures):
        c=Clock();b=Harness(c,['ok']);attempts=[];recoveries=[]
        def prepare(target):
            attempts.append(target)
            if failures:raise failures.pop(0)
        b.prepare_booking=prepare;b.load_event_search=lambda:recoveries.append(True)
        b.advance_event_notice=lambda:False;b.verify_booking=lambda target:None;b.full_entry_visible=lambda:False
        return c,b,attempts,recoveries

    def run_preflight(self,c,b,watch=True,minutes=5):
        with patch('engine.time',c):b.run(['A指定席'],2,'前列',dt.datetime.now(JST)-dt.timedelta(seconds=1),watch,10,minutes,booking={'course':'東京','date':'2026-11-23'})

    def test_read_only_preflight_recovers_transport_and_stale_without_changing_conditions(self):
        c,b,attempts,recoveries=self.preflight_browser([ListingConnectionLost('切断'),StaleElementReferenceException('更新')])
        self.run_preflight(c,b)
        self.assertEqual(c.waits,[30,60]);self.assertEqual(len(attempts),3);self.assertEqual(len(recoveries),2)
        self.assertEqual(b.calls,[('A指定席',2,'前列')])

    def test_read_only_recovery_is_bounded(self):
        c,b,attempts,recoveries=self.preflight_browser([ListingConnectionLost('切断')]*4)
        with self.assertRaises(Halt):self.run_preflight(c,b)
        self.assertEqual(c.waits,[30,60]);self.assertEqual(len(attempts),3);self.assertEqual(b.calls,[])

    def test_read_only_recovery_respects_deadline_and_single_check(self):
        for watch,minutes in [(False,5),(True,1)]:
            c,b,attempts,recoveries=self.preflight_browser([ListingConnectionLost('切断')]*3)
            with self.assertRaises(Halt):self.run_preflight(c,b,watch,minutes)
            self.assertEqual(b.calls,[])
            self.assertLessEqual(len(recoveries),1 if watch else 0)

    def test_unknown_driver_error_in_preflight_is_not_retried(self):
        c,b,attempts,recoveries=self.preflight_browser([WebDriverException('unknown browser failure')])
        with self.assertRaises(WebDriverException):self.run_preflight(c,b)
        self.assertEqual(c.waits,[]);self.assertEqual(recoveries,[]);self.assertEqual(b.calls,[])

    def test_stale_during_reservation_is_never_retried(self):
        c,b,attempts,recoveries=self.preflight_browser([])
        def reserve(*args):raise StaleElementReferenceException('after action')
        b.reserve=reserve
        with self.assertRaises(StaleElementReferenceException):self.run_preflight(c,b)
        self.assertEqual(c.waits,[]);self.assertEqual(recoveries,[])

    def test_only_known_transport_error_in_fixed_get_is_classified_for_recovery(self):
        c,b,_,_=self.preflight_browser([])
        for message,kind in [('net::ERR_CONNECTION_CLOSED',ListingConnectionLost),('unknown browser failure',WebDriverException)]:
            with patch.object(b.driver,'get',side_effect=WebDriverException(message)) as get:
                with self.assertRaises(kind):Browser.load_event_search(b)
                get.assert_called_once_with('https://jra-tickets.jp/EventSearch.aspx')

    def test_cached_wish_waits_for_disabled_full_entry_then_reserves_exact_count(self):
        c=Clock();b=Harness(c,['ok']);calls=[]
        def prepare(target):
            calls.append(target)
            if len(calls)<3:raise ListingPending('満席の入口')
        b.prepare_booking=prepare;b.advance_event_notice=lambda:False;b.verify_booking=lambda target:None;b.full_entry_visible=lambda:False
        target={'course':'東京','date':'2026-11-23'}
        with patch('engine.time',c):b.run(['A指定席'],2,'前列',dt.datetime.now(JST)-dt.timedelta(seconds=1),True,10,1,booking=target)
        self.assertEqual(c.waits,[10,10]);self.assertEqual(b.calls,[('A指定席',2,'前列')]);self.assertEqual(b.events.count('payment_ready'),1)
        self.assertEqual(calls,[target]*3)

    def test_disabled_full_entry_without_watch_never_reserves(self):
        c=Clock();b=Harness(c,['ok'])
        def prepare(target):raise ListingPending('満席の入口')
        b.prepare_booking=prepare
        with patch('engine.time',c):
            with self.assertRaises(Unavailable):b.run(['A指定席'],2,'',dt.datetime.now(JST)-dt.timedelta(seconds=1),False,10,1,booking={'course':'東京','date':'2026-11-23'})
        self.assertEqual(b.calls,[]);self.assertEqual(c.waits,[])

    def test_known_rejection_waits_before_get_and_keeps_count_area_then_stops_on_payment(self):
        c=Clock();b=Harness(c,['no','ok','ok']);b.run_fixture()
        self.assertEqual(c.waits,[60]);self.assertEqual(b.calls,[('A指定席',2,'前列')]*2)
        self.assertEqual(b.driver.navigation,['refresh',('GET',b.target)])
        self.assertEqual(b.events,['retry_wait','payment_ready']);self.assertEqual(len(b.results),1)
    def test_seat_type_quantity_limit_stops_without_retries_or_reducing_count(self):
        c=Clock();b=Harness(c,['capacity','ok'])
        with self.assertRaises(SeatLimit):b.run_fixture()
        self.assertEqual(b.calls,[('A指定席',2,'前列')])
        self.assertEqual(c.waits,[]);self.assertNotIn('payment_ready',b.events)

    def test_capacity_parser_requires_visible_picker_and_unambiguous_maximum(self):
        self.assertEqual(picker_capacity('おまかせ席選択 ただいまご予約できる最大席数は１席です'),1)
        self.assertIsNone(picker_capacity('ご予約できる最大席数は1席です'))
        self.assertIsNone(picker_capacity('おまかせ席選択 ご予約できる最大席数は1席です ご予約できる最大席数は2席です'))

    def test_ten_second_inventory_spacing_includes_read_time(self):
        c=Clock();b=Harness(c,['ok']);starts=[];queries=[]
        def refresh():starts.append(c.now);c.now+=7
        def seats():
            queries.append(True)
            return [{'name':'A指定席','available':len(queries)>1}]
        b.driver.refresh=refresh;b.seats=seats
        b.run_fixture(interval=10)
        self.assertEqual(starts,[0,10]);self.assertEqual(c.waits,[3])
        self.assertEqual(len(b.calls),1)

    def test_slow_checks_do_not_overlap_or_queue_catch_up_requests(self):
        c=Clock();b=Harness(c,['ok']);starts=[]
        def refresh():starts.append(c.now);c.now+=13
        def seats():return [{'name':'A指定席','available':len(starts)>1}]
        b.driver.refresh=refresh;b.seats=seats
        b.run_fixture(interval=10)
        self.assertEqual(starts,[0,13]);self.assertEqual(c.waits,[0])

    def test_rejected_submission_waits_ten_seconds_after_response(self):
        c=Clock();b=Harness(c,['no','ok']);original=b.reserve
        def reserve(*args):c.now+=7;return original(*args)
        b.reserve=reserve;b.run_fixture(interval=10)
        self.assertEqual(c.waits,[10]);self.assertEqual(len(b.calls),2)

    def test_slow_return_to_list_cannot_submit_after_deadline(self):
        c=Clock();b=Harness(c,['no','ok'])
        get=b.driver.get
        def slow_get(url):get(url);c.now+=200
        b.driver.get=slow_get
        with self.assertRaises(Halt):b.run_fixture(minutes=2)
        self.assertEqual(len(b.calls),1)

    def test_watch_disabled_never_repeats(self):
        c=Clock();b=Harness(c,['no','ok'])
        with self.assertRaises(Unavailable):b.run_fixture(watch=False)
        self.assertEqual(len(b.calls),1);self.assertEqual(c.waits,[])
    def test_unknown_result_never_repeats(self):
        c=Clock();b=Harness(c,['unknown','ok'])
        with self.assertRaises(Halt):b.run_fixture()
        self.assertEqual(len(b.calls),1);self.assertEqual(c.waits,[])
    def test_budget_exhaustion_stops_without_next_attempt(self):
        c=Clock();b=Harness(c,['no']*5)
        with self.assertRaises(Unavailable):b.run_fixture(minutes=2)
        self.assertEqual(c.waits,[60]);self.assertEqual(len(b.calls),2)
    def test_payment_or_confirmation_with_rejection_is_ambiguous(self):
        for attr in ['payment','confirm']:
            c=Clock();b=Harness(c,['no','ok']);setattr(b,attr,True)
            with self.assertRaises(Halt):b.run_fixture()
            self.assertEqual(c.waits,[]);self.assertEqual(len(b.calls),1)
    def test_cancel_during_wait_prevents_another_submission(self):
        c=Clock();b=Harness(c,['no','ok'])
        original=c.wait
        def cancel(seconds):original(seconds);c.cancelled=True
        c.wait=cancel
        with self.assertRaises(Halt):b.run_fixture()
        self.assertEqual(len(b.calls),1)

if __name__=='__main__':unittest.main()
