"""Preselected event flow; synthetic pages only, no real reservations."""
import datetime as dt
import threading
import unittest
from unittest.mock import patch
from controller import Controller
from engine import Browser, Halt, ListingPending, JST, booking_target, matches_booking_header

class TargetValidationTests(unittest.TestCase):
    def test_target_dates_are_exact_and_bounded(self):
        today=dt.date(2026,11,18)
        self.assertEqual(booking_target({'course':'東京','date':'2026-11-29'},today)['date'],'2026-11-29')
        for target in [{'course':'東京','date':'2026-11-17'},{'course':'東京','date':'2027-11-29'},{'course':'架空','date':'2026-11-29'}]:
            with self.assertRaises(ValueError):booking_target(target,today)
    def test_header_must_have_only_requested_course_and_date(self):
        target={'course':'東京','date':'2026-11-29'}
        for text in ['東京競馬場 2026年11月29日(日)','東京競馬場 11/29 日']:
            self.assertTrue(matches_booking_header(text,target),text)
        for text in ['東京競馬場 11/2','京都競馬場 11/29','東京競馬場 2025年11月29日','東京競馬場 11/29 11/30','東京競馬場 京都競馬場 11/29']:
            self.assertFalse(matches_booking_header(text,target),text)

class BookingFlowTests(unittest.TestCase):
    def setUp(self):
        self.events=[]
        self.browser=Browser(threading.Event(),lambda *a:None,True,self.events.append)
        self.browser.open()
        self.day=dt.datetime.now(JST).date()+dt.timedelta(days=5)
        self.target={'course':'東京','date':self.day.isoformat()}
        self.header=f'東京競馬場 {self.day.year}年{self.day.month}月{self.day.day}日'
        self.html=self.browser.driver.execute_script('return document.body.innerHTML')
    def tearDown(self):self.browser.driver.quit()
    def search_fixture(self):
        self.browser.driver.execute_script("""
          const html=arguments[0],header=arguments[1],day=arguments[2];
          document.body.innerHTML='<a id="linkButtonRaceCource" class="on">開催競馬場</a><select id="dropDownListPlace"><option value="">選択してください</option><option value="TKIJ">東京競馬場</option></select><select id="dropDownListEventDate"><option value="">選択してください</option><option value="'+day+'T00:00:00">開催日</option></select><a id="Main_LinkButtonSearch">検索</a>';
          document.getElementById('Main_LinkButtonSearch').onclick=()=>{
            document.body.dataset.selected=[document.getElementById('dropDownListPlace').value,document.getElementById('dropDownListEventDate').value].join('|');
            const a=document.createElement('a');a.href='EventDetail.aspx?event=fixture';a.textContent='申し込みへ';a.onclick=e=>{e.preventDefault();document.body.innerHTML='<main><h1>'+header+'</h1>'+html+'</main>';};document.body.append(a);
          };
        """,self.html,self.header,self.day.isoformat())
    def test_search_dropdown_reacquires_replaced_field_and_waits_for_options(self):
        self.browser.driver.execute_script("""
          document.body.innerHTML='<select id="dropDownListEventDate"><option value="">選択</option><option value="target">対象日</option></select>';
          document.querySelector('select').onchange=()=>{
            document.querySelector('select').outerHTML='<select id="dropDownListEventDate"><option value="">更新中</option></select>';
            setTimeout(()=>document.querySelector('select').insertAdjacentHTML('beforeend','<option value="target">対象日</option>'),350);
          };
        """)
        self.browser.select_search_value('#dropDownListEventDate','target')
        self.assertEqual(self.browser.driver.find_element('id','dropDownListEventDate').get_attribute('value'),'target')
        self.assertNotIn('payment_ready',self.events)

    def test_search_dropdown_helper_refuses_seat_and_payment_fields(self):
        with self.assertRaises(ValueError):self.browser.select_search_value('#cardType','target')
        with self.assertRaises(ValueError):self.browser.select_search_value('#seatCount','2')

    def test_body_read_waits_for_document_instead_of_using_a_missing_body_element(self):
        with patch.object(self.browser.driver,'execute_script',side_effect=[None,None,'検索結果']) as read:
            self.assertEqual(self.browser.text(),'検索結果')
            self.assertEqual(read.call_count,3)
        self.assertNotIn('payment_ready',self.events)

    def test_catalog_contains_only_site_options_and_does_not_reserve(self):
        self.search_fixture()
        catalog=self.browser.booking_catalog()
        self.assertEqual(catalog,[{'course':'東京','dates':[{'date':self.day.isoformat(),'label':'開催日'}]}])
        self.assertNotIn('payment_ready',self.events)
        self.assertIsNone(self.browser.target)

    def test_course_and_date_search_then_exact_two_seats_notifies_on_payment(self):
        self.search_fixture()
        seats=self.browser.prepare_booking(self.target)
        self.assertEqual(self.browser.driver.execute_script('return document.body.dataset.selected'),'TKIJ|'+self.day.isoformat()+'T00:00:00')
        self.assertEqual(self.browser.booking,self.target)
        self.browser.reserve(seats[0],2,'')
        self.assertEqual(self.events.count('payment_ready'),1)
        self.assertIsNone(self.browser.driver.execute_script('return document.body.dataset.purchased||null'))
    def test_full_run_refresh_keeps_the_selected_demo_event_and_reaches_payment(self):
        self.browser.booking_catalog()
        self.browser.prepare_booking(self.target)
        self.browser.run(['A指定席'],2,'',dt.datetime.now(JST)-dt.timedelta(seconds=1),False,10,1,booking=self.target)
        self.assertEqual(self.events.count('payment_ready'),1)
        self.assertIsNone(self.browser.driver.execute_script('return document.body.dataset.purchased||null'))

    def add_common_notice(self,unknown=False):
        self.browser.driver.execute_script(r"""
        const policy=arguments[0]?'別の契約です。':'指定席の転売・譲渡・不正購入は禁止されています。同行者様の情報をご確認ください。';
        document.body.insertAdjacentHTML('beforeend','<section id="confirmDialog" role="dialog" aria-modal="true" style="position:fixed;inset:0;background:white;z-index:900;overflow:auto"><h2>お知らせ</h2><p>'+policy+'</p><label><input id="commonCheck" type="checkbox" onchange="document.getElementById(\'commonNext\').disabled=!this.checked">注意事項を確認しました</label><button id="commonNext" disabled onclick="document.body.dataset.commonAdvanced=1;this.closest(\'section\').remove()">次へ進む</button></section>');
        """,unknown)

    def test_known_common_notice_advances_without_reserving(self):
        self.add_common_notice()
        self.assertTrue(self.browser.advance_event_notice())
        self.assertEqual(self.browser.driver.execute_script('return document.body.dataset.commonAdvanced'),'1')
        self.assertNotIn('payment_ready',self.events)
        self.assertIsNone(self.browser.driver.execute_script('return document.body.dataset.reserved||null'))

    def test_unknown_notice_is_not_checked_or_accepted(self):
        self.add_common_notice(unknown=True)
        self.assertFalse(self.browser.advance_event_notice())
        self.assertFalse(self.browser.driver.find_element('id','commonCheck').is_selected())

    def test_common_notice_in_event_preparation_then_exact_two_seats(self):
        self.search_fixture()
        original=self.browser.click
        def click(element):
            caption=element.text
            original(element)
            if caption=='申し込みへ':self.add_common_notice()
        self.browser.click=click
        seats=self.browser.prepare_booking(self.target)
        self.assertEqual(self.browser.driver.execute_script('return document.body.dataset.commonAdvanced'),'1')
        self.browser.reserve(seats[0],2,'')
        self.assertEqual(self.events.count('payment_ready'),1)

    def test_common_notice_then_reserved_seat_entry_skips_admission(self):
        self.search_fixture()
        original=self.browser.click
        def click(element):
            caption=element.text
            original(element)
            if caption=='申し込みへ':
                self.browser.driver.execute_script("""
                const html=arguments[0],header=arguments[1];
                document.body.innerHTML='<main><h1>'+header+'</h1><section><h2>指定席</h2><button id="reservedEntry"><label class="btn_01">受付中</label></button></section><section><h2>入場券</h2><button onclick="document.body.dataset.wrongEntry=1">受付中</button></section></main>';
                document.getElementById('reservedEntry').onclick=()=>{document.body.innerHTML='<main><h1>'+header+'</h1>'+html+'</main>';document.body.dataset.seatEntry=1;};
                """,self.html,self.header)
                self.add_common_notice()
        self.browser.click=click
        seats=self.browser.prepare_booking(self.target)
        self.assertTrue(seats)
        self.assertEqual(self.browser.driver.execute_script('return document.body.dataset.seatEntry'),'1')
        self.assertIsNone(self.browser.driver.execute_script('return document.body.dataset.wrongEntry||null'))
        self.assertIsNone(self.browser.driver.execute_script('return document.body.dataset.reserved||null'))

    def test_full_entry_is_scoped_to_reserved_seats(self):
        self.browser.driver.execute_script("document.body.innerHTML='<section><h2>指定席</h2><button>受付中</button></section><section><h2>入場券</h2><button disabled>満席</button></section>'")
        self.assertFalse(self.browser.full_entry_visible(reserved_only=True))
        self.assertIsNotNone(self.browser.event_seat_entry())
        self.browser.driver.execute_script("document.body.innerHTML='<section><h2>指定席</h2><button disabled>満席</button></section><section><h2>入場券</h2><button>受付中</button></section>'")
        self.assertTrue(self.browser.full_entry_visible(reserved_only=True))
        self.assertIsNone(self.browser.event_seat_entry())

    def test_disabled_full_entry_does_not_click_or_submit(self):
        self.search_fixture()
        self.browser.driver.execute_script("document.getElementById('Main_LinkButtonSearch').onclick=()=>{document.body.innerHTML='<form><div><h1>検索結果</h1><a class=aspNetDisabled aria-disabled=true><span>満席</span></a></div></form>'}")
        with self.assertRaises(ListingPending):self.browser.prepare_booking(self.target)
        self.assertNotIn('payment_ready',self.events)
        self.assertIsNone(self.browser.driver.execute_script('return document.body.dataset.reserved||null'))

    def test_wrong_result_day_never_submits(self):
        self.header='東京競馬場 11/1'
        self.search_fixture()
        with self.assertRaises(Halt):self.browser.prepare_booking(self.target)
        self.assertNotIn('payment_ready',self.events)
        self.assertIsNone(self.browser.driver.execute_script('return document.body.dataset.reserved||null'))
    def test_same_target_does_not_repeat_search_and_hidden_text_is_not_evidence(self):
        self.browser.driver.execute_script("document.body.insertAdjacentHTML('afterbegin','<h1>'+arguments[0]+'</h1>')",self.header)
        self.browser.prepare_booking(self.target)
        self.browser.driver.execute_script("document.querySelector('h1').hidden=true")
        with self.assertRaises(Halt):self.browser.verify_booking(self.target)
    def test_changed_header_before_reserve_prevents_seat_submission(self):
        self.search_fixture();seats=self.browser.prepare_booking(self.target)
        self.browser.driver.execute_script("document.querySelector('main h1').textContent='京都競馬場 11/1'")
        with self.assertRaises(Halt):self.browser.reserve(seats[0],2,'')
        self.assertNotIn('payment_ready',self.events)
    def test_payment_page_is_preserved_during_prepare(self):
        self.browser.driver.execute_script("document.body.innerHTML='<h1>クレジットカード情報入力</h1>'")
        with self.assertRaises(Halt):self.browser.prepare_booking(self.target)
        self.assertIn('クレジットカード',self.browser.text())

if __name__=='__main__':unittest.main(verbosity=2)
