import threading
import unittest

from engine import Browser,Halt,Unavailable,SeatLimit


class CloudBrowserTests(unittest.TestCase):
    def setUp(self):
        self.logs=[];self.events=[]
        self.browser=Browser(threading.Event(),self.logs.append,demo=True,on_event=self.events.append)
        self.browser.open()
    def tearDown(self):
        if self.browser.driver:self.browser.driver.quit()
    def consent(self):
        self.browser.driver.execute_script("""const t=setInterval(()=>{const p=document.querySelector('.notice');if(p && p.style.display==='block'){p.querySelector('button').click();clearInterval(t)}},100)""")
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
