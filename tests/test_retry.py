"""Retry only definitive no-seat results; fake time never contacts the live site."""
import datetime as dt
import unittest
from unittest.mock import patch
from engine import Browser,Halt,Unavailable,CountUnavailable,picker_capacity,JST

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
        if result=='capacity':raise CountUnavailable('最大席数は1席')
        if result=='no':raise Unavailable('指定枚数を確保不可')
        if result=='unknown':raise Halt('結果不明')
        self.event('payment_ready')
    def text(self):return 'おまかせ席選択 ただいまご予約できる最大席数は1席です'
    def outcome(self):return 'unavailable'
    def visible(self,selector):return [object()] if self.confirm and selector=='#confirmDialog' else []
    def has_payment_evidence(self):return self.payment
    def event(self,event):self.events.append(event)
    def run_fixture(self,watch=True,minutes=5):
        with patch('engine.time',self.clock):
            self.run(['A指定席'],2,'前列',dt.datetime.now(JST)-dt.timedelta(seconds=1),watch,60,minutes)

class RetryTests(unittest.TestCase):
    def test_known_rejection_waits_before_get_and_keeps_count_area_then_stops_on_payment(self):
        c=Clock();b=Harness(c,['no','ok','ok']);b.run_fixture()
        self.assertEqual(c.waits,[60]);self.assertEqual(b.calls,[('A指定席',2,'前列')]*2)
        self.assertEqual(b.driver.navigation,['refresh',('GET',b.target)])
        self.assertEqual(b.events,['retry_wait','payment_ready']);self.assertEqual(len(b.results),1)
    def test_explicit_capacity_shortage_rechecks_without_reducing_requested_count(self):
        c=Clock();b=Harness(c,['capacity','ok']);b.run_fixture()
        self.assertEqual(b.calls,[('A指定席',2,'前列')]*2)
        self.assertEqual(c.waits,[60]);self.assertEqual(b.events[-1],'payment_ready')

    def test_capacity_parser_requires_visible_picker_and_unambiguous_maximum(self):
        self.assertEqual(picker_capacity('おまかせ席選択 ただいまご予約できる最大席数は１席です'),1)
        self.assertIsNone(picker_capacity('ご予約できる最大席数は1席です'))
        self.assertIsNone(picker_capacity('おまかせ席選択 ご予約できる最大席数は1席です ご予約できる最大席数は2席です'))

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
