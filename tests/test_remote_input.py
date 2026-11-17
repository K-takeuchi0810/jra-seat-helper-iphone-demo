import json
from collections import deque
import queue
import threading
import unittest
from controller import Controller
from engine import Browser

class RemoteInputTests(unittest.TestCase):
    def setUp(self):
        self.browser=Browser(threading.Event(),lambda *a:None,demo=True)
        self.browser.open()
        self.browser.driver.execute_script("document.body.innerHTML=`<select id='fixture' style='position:fixed;left:10px;top:10px;width:200px;height:50px' onchange='document.body.dataset.changed=this.value'><option value='a'>最初</option><option value='b'>別の選択</option><option disabled>選択不可</option></select><button id='other'>別の要素</button>`")
        self.c=Controller.__new__(Controller)
        self.c.browser=self.browser;self.c.state={'width':390,'height':760,'phase':'ready','frame':0,'busy':False};self.c.demo=True
        self.c.selected_element=None;self.c.select_token='';self.c.last_frame=0
        self.c.manual=queue.Queue();self.c.owner=threading.get_ident();self.c.pumping=False
        self.c.lock=threading.RLock();self.c.frame=b'';self.c.logs=deque(maxlen=100)
    def test_form_fields_target_without_tapping_and_never_export_text_values(self):
        self.browser.driver.execute_script("document.body.insertAdjacentHTML('beforeend','<label>メール<input id=mail value=private@example.test></label><input hidden value=secret>')")
        fields=self.c._form_fields()
        self.assertNotIn('private@example.test',json.dumps(fields))
        self.assertGreaterEqual(len(fields),2)
        mail=next(f for f in fields if f['type']=='text')
        self.c._manual({'kind':'field','token':mail['token'],'value':'new@example.test'})
        self.assertEqual(self.browser.driver.find_element('id','mail').get_attribute('value'),'new@example.test')
        pick=next(f for f in fields if f['type']=='select')
        self.c._manual({'kind':'field','token':pick['token'],'index':1})
        self.assertEqual(self.browser.driver.execute_script('return document.body.dataset.changed'),'b')
        with self.assertRaises(ValueError):self.c._manual({'kind':'field','token':pick['token'],'index':2})
        self.browser.driver.execute_script("document.getElementById('mail').remove()")
        with self.assertRaises(ValueError):self.c._manual({'kind':'field','token':mail['token'],'value':'bad'})

    def test_external_actions_links_and_checks_work_without_image_taps(self):
        self.browser.driver.execute_script("""document.body.innerHTML=`<button onclick="document.body.dataset.action='yes'">検索</button><a href='#next'>次の画面</a><label><input type=checkbox style='display:none'>注意事項を確認しました</label>`""")
        fields=self.c._form_fields()
        button=next(f for f in fields if f['label']=='検索')
        self.c._manual({'kind':'field','token':button['token'],'activate':True})
        self.assertEqual(self.browser.driver.execute_script('return document.body.dataset.action'),'yes')
        check=next(f for f in fields if f['type']=='checkbox')
        self.c._manual({'kind':'field','token':check['token'],'activate':True})
        self.assertTrue(self.browser.driver.execute_script('return document.querySelector("input").checked'))
        link=next(f for f in fields if f['label']=='次の画面')
        self.c._manual({'kind':'field','token':link['token'],'activate':True})
        self.assertTrue(self.browser.driver.current_url.endswith('#next'))

    def tearDown(self):self.browser.driver.quit()
    def test_select_keeps_its_target_after_focus_moves_and_emits_change(self):
        self.c._manual({'kind':'tap','x':50,'y':30});token=self.c.select_token
        self.browser.driver.execute_script("document.getElementById('other').focus()")
        self.c._manual({'kind':'select','index':1,'token':token})
        self.assertEqual(self.browser.driver.execute_script('return document.body.dataset.changed'),'b')
    def test_stale_token_and_removed_select_are_rejected(self):
        self.c._manual({'kind':'tap','x':50,'y':30});token=self.c.select_token
        with self.assertRaises(ValueError):self.c._manual({'kind':'select','index':1,'token':'old'})
        self.browser.driver.execute_script("document.getElementById('fixture').remove()")
        with self.assertRaises(Exception):self.c._manual({'kind':'select','index':1,'token':token})
    def test_jpeg_frame_has_select_options_and_disabled_state(self):
        self.c._manual({'kind':'tap','x':50,'y':30});self.c.pump()
        self.assertTrue(self.c.frame.startswith(b'\xff\xd8'))
        self.assertEqual(self.c.state['activeSelect']['disabled'],[False,False,True])
        self.assertEqual(self.c.state['width'],390)
        self.assertEqual(self.c.state['activeSelect']['token'],self.c.select_token)

    def login_fixture(self):
        self.browser.driver.execute_script("document.body.innerHTML=`<form onsubmit=\"document.body.dataset.submitted='yes';return false\"><label>ID<input id=loginMail type=text value=old-user></label><label>PW<input id=loginPassword type=password value=old-password></label><button type=submit>ログイン</button></form>`")

    def test_selected_input_can_be_replaced_and_cleared(self):
        self.login_fixture();d=self.browser.driver
        d.execute_script("document.getElementById('loginMail').focus()")
        self.c._manual({'kind':'type','value':'new-user'})
        self.assertEqual(d.find_element('id','loginMail').get_attribute('value'),'new-user')
        self.c._manual({'kind':'clear'})
        self.assertEqual(d.find_element('id','loginMail').get_attribute('value'),'')
        self.assertIsNone(d.execute_script('return document.body.dataset.submitted || null'))

    def test_login_pair_is_overwritten_then_both_fields_can_be_cleared_without_submit(self):
        self.login_fixture();d=self.browser.driver
        self.c._manual({'kind':'login_fill','username':'qa-user','password':'qa-password'})
        self.assertEqual(d.find_element('id','loginMail').get_attribute('value'),'qa-user')
        self.assertEqual(d.find_element('id','loginPassword').get_attribute('value'),'qa-password')
        self.c._manual({'kind':'login_clear'})
        self.assertEqual(d.find_element('id','loginMail').get_attribute('value'),'')
        self.assertEqual(d.find_element('id','loginPassword').get_attribute('value'),'')
        self.assertIsNone(d.execute_script('return document.body.dataset.submitted || null'))

    def test_login_control_characters_are_rejected_before_any_fields_are_changed(self):
        self.login_fixture();d=self.browser.driver
        for password in ['bad\nvalue','bad\ue007value']:
            with self.assertRaises(ValueError):self.c._manual({'kind':'login_fill','username':'qa-user','password':password})
        self.assertEqual(d.find_element('id','loginMail').get_attribute('value'),'old-user')
        self.assertIsNone(d.execute_script('return document.body.dataset.submitted || null'))

    def test_login_fill_requires_the_expected_visible_login_form(self):
        with self.assertRaises(ValueError):self.c._manual({'kind':'login_fill','username':'qa-user','password':'qa-password'})

    def test_acknowledgement_and_state_never_contain_entered_credentials(self):
        self.login_fixture()
        self.c.manual.put({'kind':'login_fill','username':'fixture-account','password':'fixture-secret','requestId':'qa-input-1'})
        self.c.pump()
        self.assertTrue(self.c.state['manualResult']['ok'])
        self.assertEqual(self.c.state['manualResult']['id'],'qa-input-1')
        self.assertTrue(self.c.state['loginReady'])
        state=json.dumps(self.c.snapshot())
        self.assertNotIn('fixture-account',state);self.assertNotIn('fixture-secret',state)

    def test_input_can_be_corrected_after_cancellation_without_resetting_stop_flag(self):
        self.login_fixture();self.browser.stop.set()
        self.c._manual({'kind':'login_fill','username':'qa-user','password':'qa-password'})
        self.assertEqual(self.browser.driver.find_element('id','loginMail').get_attribute('value'),'qa-user')
        self.assertTrue(self.browser.stop.is_set())

    def test_an_overlong_app_key_is_not_forwarded_as_a_jra_password(self):
        self.login_fixture()
        with self.assertRaises(ValueError):self.c._manual({'kind':'login_fill','username':'qa-user','password':'a'*44})
        self.assertEqual(self.browser.driver.find_element('id','loginMail').get_attribute('value'),'old-user')

    def test_readonly_input_cannot_be_cleared(self):
        self.login_fixture();d=self.browser.driver
        d.execute_script("const e=document.getElementById('loginMail');e.readOnly=true;e.focus()")
        with self.assertRaises(ValueError):self.c._manual({'kind':'clear'})
        self.assertEqual(d.find_element('id','loginMail').get_attribute('value'),'old-user')

if __name__=='__main__':unittest.main()
