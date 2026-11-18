import threading
import unittest

from engine import Browser,Halt,Unavailable,EventUnavailable,SeatLimit


class CloudBrowserTests(unittest.TestCase):
    def setUp(self):
        self.logs=[];self.events=[]
        self.browser=Browser(threading.Event(),self.logs.append,demo=True,on_event=self.events.append)
        self.browser.open()
    def tearDown(self):
        if self.browser.driver:self.browser.driver.quit()
    def consent(self):
        pass  # The application now advances the known seat notice itself.

    def test_invisible_rejection_text_does_not_trigger_a_retry(self):
        self.browser.driver.execute_script("document.body.insertAdjacentHTML('beforeend','<div style=opacity:0><p>ご希望の条件にて、お席をご用意することができませんでした。</p></div>')")
        self.assertEqual(self.browser.outcome(),'pending')
        self.assertNotIn('お席をご用意',self.browser.text())

    def event_error(self,extra=''):
        self.browser.driver.execute_script("""const dialog=document.createElement('section');dialog.id='messageDialog';dialog.innerHTML=`<h2>ご確認ください</h2><p>指定された公演は購入することができません。</p><p class=extra></p><button>閉じる</button>`;dialog.querySelector('.extra').textContent=arguments[0];dialog.querySelector('button').onclick=()=>dialog.remove();document.body.append(dialog);""",extra)

    def test_known_event_rejection_is_distinct_and_can_be_closed_before_search(self):
        self.event_error()
        self.assertEqual(self.browser.outcome(),'event_unavailable')
        self.assertTrue(self.browser.dismiss_event_rejection())
        self.assertEqual(self.browser.outcome(),'pending')
        self.assertNotIn('payment_ready',self.events)

    def test_event_rejection_with_an_additional_warning_is_not_closed(self):
        self.event_error('別の追加確認です')
        self.assertEqual(self.browser.outcome(),'unknown')
        self.assertFalse(self.browser.dismiss_event_rejection())
        self.assertTrue(self.browser.driver.find_element('id','messageDialog').is_displayed())

    def test_event_rejection_with_payment_evidence_is_never_dismissed(self):
        self.event_error()
        self.browser.driver.execute_script("document.body.insertAdjacentHTML('beforeend','<h2>購入内容の確認</h2>')")
        with self.assertRaises(Halt):self.browser.dismiss_event_rejection()
        self.assertTrue(self.browser.driver.find_element('id','messageDialog').is_displayed())

    def test_quantity_submission_event_rejection_never_reports_payment_ready(self):
        seats=self.browser.capture()
        self.browser.driver.execute_script("""window.done=()=>{document.getElementById('amounts').style.display='none';document.body.insertAdjacentHTML('beforeend','<section id=messageDialog><h2>ご確認ください</h2><p>指定された公演は購入することができません。</p><button>閉じる</button></section>');};""")
        with self.assertRaises(EventUnavailable):self.browser.reserve(seats[0],2,'')
        self.assertNotIn('payment_ready',self.events)
        self.assertIsNone(self.browser.driver.execute_script('return document.body.dataset.purchased||null'))

    def test_existing_purchase_or_unknown_status_prevents_event_retry(self):
        self.browser.driver.execute_script("document.body.innerHTML='<h1>発売スケジュール</h1><p>申込ステータス 購入済</p>'")
        with self.assertRaises(Halt):self.browser.verify_event_eligibility(required=True)
        self.browser.driver.execute_script("document.body.innerHTML='<h1>発売スケジュール</h1><p>申込ステータス 未申込</p>'")
        self.assertTrue(self.browser.verify_event_eligibility(required=True))
        self.browser.driver.execute_script("document.body.innerHTML='<h1>発売スケジュール</h1>'")
        with self.assertRaises(Halt):self.browser.verify_event_eligibility(required=True)

    def test_mobile_sized_cloud_browser_reaches_payment_and_emits_one_notification_event(self):
        seats=self.browser.capture();self.consent()
        self.browser.reserve(seats[0],2,'前列')
        self.assertEqual(self.events.count('payment_ready'),1)
        self.assertEqual(self.browser.driver.execute_script('return window.innerWidth'),390)
        self.assertEqual(self.browser.driver.execute_script('return document.body.dataset.reserved'),'yes')
        self.assertIsNone(self.browser.driver.execute_script('return document.body.dataset.purchased || null'))
    def test_visible_rejection_outside_message_dialog_wins_over_payment_and_stock(self):
        self.browser.driver.execute_script("document.body.insertAdjacentHTML('beforeend','<section><h2>購入内容の確認</h2><p>ご希望の条件にて、 お席をご用意することができませんでした</p></section>')")
        self.assertEqual(self.browser.outcome(), 'unavailable')
        self.assertNotIn('payment_ready',self.events)

    def test_hidden_rejection_is_not_treated_as_a_server_response(self):
        self.browser.driver.execute_script("document.body.insertAdjacentHTML('beforeend','<p hidden>ご希望の条件にて、お席をご用意することができませんでした。</p>')")
        self.assertEqual(self.browser.outcome(), 'pending')

    def test_maximum_one_seat_does_not_click_one_seat_for_a_two_seat_request(self):
        seats=self.browser.capture();self.consent()
        self.browser.driver.execute_script("document.getElementById('amounts').insertAdjacentHTML('afterbegin','<h2>おまかせ席選択</h2><p>ただいまご予約できる最大席数は1席です</p>');document.querySelector('[id*=_SeatAmountRepeater_1_]').remove();window.done=()=>document.body.dataset.submitted='yes'")
        with self.assertRaises(SeatLimit):self.browser.reserve(seats[0],2,'')
        self.assertIsNone(self.browser.driver.execute_script('return document.body.dataset.submitted || null'))
        self.assertNotIn('payment_ready',self.events)

    def test_seat_type_limit_four_allows_exact_requested_two_seats(self):
        seats=self.browser.capture();self.consent()
        self.browser.driver.execute_script("document.getElementById('amounts').insertAdjacentHTML('afterbegin','<h2>おまかせ席選択</h2><p>ただいまご予約できる最大席数は4席です</p>')")
        self.browser.reserve(seats[0],2,'')
        self.assertEqual(self.events.count('payment_ready'),1)
        self.assertIsNone(self.browser.driver.execute_script('return document.body.dataset.purchased || null'))

    def test_notice_and_exact_quantity_one_through_four_are_automatic(self):
        for count in range(1,5):
            with self.subTest(count=count):
                self.browser.driver.refresh()
                seats=self.browser.capture()
                self.browser.driver.execute_script("document.querySelectorAll('[id*=SeatAmountRepeater]').forEach(b=>b.addEventListener('click',()=>document.body.dataset.chosen=b.textContent))")
                self.browser.reserve(seats[0],count,'')
                self.assertEqual(self.browser.driver.execute_script('return document.body.dataset.chosen'),f'{count}席')
                self.assertTrue(self.browser.driver.find_element('id','noticeCheck').is_selected())
                self.assertIsNone(self.browser.driver.execute_script('return document.body.dataset.purchased || null'))
        self.assertNotIn('attention_required',self.events)
        self.assertEqual(self.events.count('payment_ready'),4)

    def test_visible_checkbox_with_unassociated_sibling_caption(self):
        seats=self.browser.capture()
        self.browser.driver.execute_script("const l=document.querySelector('label:has(#noticeCheck)');const s=document.createElement('span');s.innerHTML=l.innerHTML;l.replaceWith(s)")
        self.browser.reserve(seats[0],2,'')
        self.assertTrue(self.browser.driver.find_element('id','noticeCheck').is_selected())
        self.assertEqual(self.events.count('payment_ready'),1)

    def test_styled_hidden_checkbox_is_checked_by_visible_label(self):
        seats=self.browser.capture()
        self.browser.driver.execute_script("document.getElementById('noticeCheck').style.display='none'")
        self.browser.reserve(seats[0],2,'')
        self.assertTrue(self.browser.driver.find_element('id','noticeCheck').is_selected())
        self.assertEqual(self.events.count('payment_ready'),1)

    def test_plain_paragraph_next_is_found_in_front_notice(self):
        seats=self.browser.capture()
        self.browser.driver.execute_script("const b=document.querySelector('.notice button');const p=document.createElement('p');p.textContent='次へ進む';p.addEventListener('click',b.onclick);b.replaceWith(p)")
        self.browser.reserve(seats[0],2,'')
        self.assertEqual(self.events.count('payment_ready'),1)

    def test_delegated_div_button_with_css_class(self):
        seats=self.browser.capture()
        self.browser.driver.execute_script("const b=document.querySelector('.notice button');const d=document.createElement('div');d.className='btn_01';d.textContent='次へ進む';d.addEventListener('click',b.onclick);b.replaceWith(d)")
        self.browser.reserve(seats[0],2,'')
        self.assertEqual(self.events.count('payment_ready'),1)

    def test_notice_footer_sibling_is_found_without_searching_seat_page(self):
        seats=self.browser.capture()
        self.browser.driver.execute_script("const p=document.querySelector('.notice');const w=document.createElement('section');p.replaceWith(w);w.append(p);const b=p.querySelector('button');w.append(b);b.onclick=()=>{p.style.display='none';b.style.display='none';document.getElementById('amounts').style.display='block'}")
        self.browser.reserve(seats[0],2,'')
        self.assertEqual(self.events.count('payment_ready'),1)

    def test_private_use_icon_does_not_change_next_caption(self):
        seats=self.browser.capture()
        self.browser.driver.execute_script("document.querySelector('.notice button').textContent='次へ進む \ue001'")
        self.browser.reserve(seats[0],2,'')
        self.assertEqual(self.events.count('payment_ready'),1)

    def test_nested_label_and_link_are_one_next_control(self):
        seats=self.browser.capture()
        self.browser.driver.execute_script("const b=document.querySelector('.notice button');const w=document.createElement('label');b.replaceWith(w);w.append(b);b.onclick=()=>{document.querySelector('.notice').style.display='none';document.getElementById('amounts').style.display='block'};b.innerHTML='<span>次へ進む</span><span>›</span>'")
        self.browser.reserve(seats[0],2,'')
        self.assertEqual(self.events.count('payment_ready'),1)

    def test_next_button_waits_for_checkbox_activation(self):
        seats=self.browser.capture()
        self.browser.driver.execute_script("const b=document.querySelector('.notice button');b.disabled=true;document.getElementById('noticeCheck').addEventListener('change',()=>setTimeout(()=>b.disabled=false,700))")
        self.browser.reserve(seats[0],2,'')
        self.assertEqual(self.events.count('payment_ready'),1)

    def test_separate_next_controls_remain_ambiguous(self):
        self.browser.driver.execute_script("const p=document.querySelector('.notice');p.style.display='block';p.append(p.querySelector('button').cloneNode(true))")
        panel=self.browser.visible('.notice')[0]
        self.assertEqual(len(self.browser.notice_next_buttons(panel)),2)

    def test_plain_next_label_without_button_class_is_supported(self):
        seats=self.browser.capture()
        self.browser.driver.execute_script("const b=document.querySelector('.notice button');const l=document.createElement('label');l.textContent=b.textContent;l.onclick=b.onclick;b.replaceWith(l)")
        self.browser.reserve(seats[0],1,'')
        self.assertEqual(self.events.count('payment_ready'),1)

    def test_unrecognized_notice_checkbox_never_advances(self):
        seats=self.browser.capture()
        self.browser.driver.execute_script("document.querySelector('label:has(#noticeCheck)').lastChild.textContent='別の同意事項'")
        with self.assertRaises(Halt):self.browser.reserve(seats[0],1,'')
        self.assertNotIn('payment_ready',self.events)
        self.assertIsNone(self.browser.driver.execute_script('return document.body.dataset.reserved || null'))

    def test_seat_label_linked_to_disabled_input_is_unavailable(self):
        self.browser.driver.execute_script("const l=document.querySelector('[id*=PnlAutoSelectButton] label');l.htmlFor='blocked';l.insertAdjacentHTML('afterend','<input id=blocked type=checkbox disabled hidden>')")
        self.assertFalse(self.browser.capture()[0]['available'])

    def test_disabled_seat_button_is_not_an_available_candidate(self):
        self.assertTrue(self.browser.capture()[0]['available'])
        self.browser.driver.execute_script("document.querySelector('[id*=PnlAutoSelectButton] label').setAttribute('aria-disabled','true')")
        self.assertFalse(self.browser.capture()[0]['available'])

    def test_payment_browser_uses_fresh_normal_profile_without_security_bypasses(self):
        arguments=self.browser.driver.execute_cdp_cmd('Browser.getBrowserCommandLine',{})['arguments']
        self.assertNotIn('--incognito',arguments)
        self.assertNotIn('--disable-web-security',arguments)
        self.assertNotIn('--ignore-certificate-errors',arguments)
        self.assertTrue(self.browser.driver.capabilities['chrome']['userDataDir'])

    def test_browser_prefers_japanese(self):
        self.assertTrue(self.browser.driver.execute_script('return navigator.language').startswith('ja'))

    def test_japanese_login_preparation_focuses_empty_account_field_without_submitting(self):
        self.browser.driver.execute_script("document.documentElement.lang='ja';document.body.innerHTML='<h1>ログイン</h1><input id=loginMail><input type=password id=loginPassword><button onclick=\"document.body.dataset.submitted=1\">ログイン</button>'")
        self.browser.prepare_login()
        self.assertEqual(self.browser.driver.execute_script('return document.activeElement.id'),'loginMail')
        self.assertEqual(self.browser.driver.find_element('id','loginMail').get_attribute('value'),'')
        self.assertIsNone(self.browser.driver.execute_script('return document.body.dataset.submitted || null'))

    def test_congestion_screen_is_preserved_and_never_a_payment_outcome(self):
        self.browser.driver.execute_script("document.body.innerHTML='<h1>現在アクセスが集中しています</h1><p>順番待ちです。この画面でお待ちください。</p>'")
        self.browser.prepare_login()
        self.assertIn('画面を維持',self.logs[-1])
        with self.assertRaises(Halt):self.browser.outcome()
        self.assertNotIn('payment_ready',self.events)
        self.assertIn('順番待ち',self.browser.text())

    def test_user_reported_rejection_does_not_emit_success_notification(self):
        seats=self.browser.capture();self.consent()
        self.browser.driver.execute_script("""window.done=()=>document.body.insertAdjacentHTML('beforeend','<div id="messageDialog">ご希望の条件にて、お席をご用意することができませんでした。</div>')""")
        with self.assertRaises(Unavailable):self.browser.reserve(seats[0],1,'')
        self.assertNotIn('payment_ready',self.events)


if __name__=='__main__':unittest.main(verbosity=2)
