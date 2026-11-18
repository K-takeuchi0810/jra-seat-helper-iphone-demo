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
        self.c.shutdown=threading.Event();self.c.manual=queue.Queue();self.c.owner=threading.get_ident();self.c.pumping=False
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

    def test_offscreen_fields_are_listed_and_scroll_normally_before_operation(self):
        self.browser.driver.execute_script("document.body.innerHTML='<h1>入力</h1><div style=height:1800px></div><dl><dt>連絡先</dt><dd><input id=far name=contact></dd></dl><button id=continue onclick=document.body.dataset.continued=1>次へ</button>'")
        fields=self.c._form_fields()
        field=next(f for f in fields if f['type']=='text')
        self.assertEqual(field['label'],'連絡先')
        self.c._manual({'kind':'field','token':field['token'],'value':'dummy-only'})
        self.assertEqual(self.browser.driver.find_element('id','far').get_attribute('value'),'dummy-only')
        action=next(f for f in fields if f['label']=='次へ')
        self.c._manual({'kind':'field','token':action['token'],'activate':True})
        self.assertEqual(self.browser.driver.execute_script('return document.body.dataset.continued'),'1')

    def test_login_submission_is_distinct_from_header_navigation(self):
        self.browser.driver.execute_script("document.body.innerHTML='<header><button>ログイン</button></header><main><button>ログイン</button></main><footer><a href=#help>ガイド</a></footer>'")
        fields=self.c._form_fields()
        self.assertEqual(next(f for f in fields if f['label']=='ログインを送信')['group'],'page')
        self.assertEqual(next(f for f in fields if f['label']=='ログイン画面へ')['group'],'navigation')
        self.assertEqual(next(f for f in fields if f['label']=='ガイド')['group'],'navigation')

    def test_jra_login_fields_have_readable_names_without_exporting_values(self):
        self.browser.driver.execute_script("document.body.innerHTML='<label>会員番号</label><input id=loginMail name=mail value=never-export><label>パスワード</label><input id=loginPassword type=password name=confirmation value=never-export>'")
        fields=self.c._form_fields()
        self.assertNotIn('never-export',json.dumps(fields))
        self.assertEqual({f['purpose'] for f in fields},{'jra-login-username','jra-login-password'})
        self.assertEqual(len(fields),2)

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

    def test_modal_controls_replace_background_controls_and_reject_old_tokens(self):
        self.browser.driver.execute_script("document.body.innerHTML='<button id=background>背景</button>'")
        old=self.c._form_fields()[0]['token']
        self.browser.driver.execute_script("document.body.insertAdjacentHTML('beforeend',`<div role=dialog aria-modal=true style='position:fixed;inset:0;background:white;z-index:1000'><label><input type=checkbox>確認</label><p>次へ進む</p></div>`)")
        fields=self.c._form_fields()
        self.assertNotIn('背景',[f['label'] for f in fields])
        self.assertIn('次へ進む',[f['label'] for f in fields])
        with self.assertRaises(ValueError):self.c._manual({'kind':'field','token':old,'activate':True})

    def test_embedded_auth_fields_can_be_operated_and_return_to_main_document(self):
        self.browser.driver.execute_script("const f=document.createElement('iframe');f.style='width:350px;height:300px';document.body.replaceChildren(f);f.srcdoc=arguments[0]", '<label>認証コード<input id=otp value=never-export></label><button>確認</button>')
        import time;time.sleep(.2)
        fields=self.c._form_fields()
        self.assertNotIn('never-export',json.dumps(fields))
        field=next(f for f in fields if f['type']=='text')
        self.c._manual({'kind':'field','token':field['token'],'value':'dummy-only'})
        self.assertEqual(self.browser.driver.execute_script('return window===window.top'),True)
        self.browser.driver.switch_to.frame(0)
        self.assertEqual(self.browser.driver.find_element('id','otp').get_attribute('value'),'dummy-only')
        self.browser.driver.switch_to.default_content()

    def test_cross_origin_auth_frame_uses_normal_browser_frame_switching(self):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        class Page(BaseHTTPRequestHandler):
            def do_GET(self):
                body=b'<label>OTP<input id=otp></label>'
                self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(body)
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Page)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        try:
            self.browser.driver.execute_script("const f=document.createElement('iframe');f.style='width:350px;height:300px';f.src=arguments[0];document.body.replaceChildren(f)",f'http://127.0.0.1:{server.server_port}/')
            import time;time.sleep(.4)
            fields=self.c._form_fields();field=next(f for f in fields if f['label']=='OTP')
            self.c._manual({'kind':'field','token':field['token'],'value':'local-test'})
            self.browser.driver.switch_to.frame(0)
            self.assertEqual(self.browser.driver.find_element('id','otp').get_attribute('value'),'local-test')
            self.browser.driver.switch_to.default_content()
        finally:server.shutdown();server.server_close()

    def test_manual_window_switch_keeps_the_original_browser_session(self):
        driver=self.browser.driver;first=driver.current_window_handle;url=driver.current_url
        driver.switch_to.new_window('tab');driver.get(url)
        windows=self.c._windows()
        self.assertEqual(len(windows),2)
        token=next(k for k,v in self.c.window_tokens.items() if v==first)
        self.c._manual({'kind':'window','token':token})
        self.assertEqual(driver.current_window_handle,first)
        self.assertEqual(len(driver.window_handles),2)

    def test_inspection_does_not_reload_or_submit_the_page(self):
        self.browser.driver.execute_script("document.body.dataset.marker='keep-current-payment'")
        self.c._manual({'kind':'inspect'})
        self.assertEqual(self.browser.driver.execute_script('return document.body.dataset.marker'),'keep-current-payment')

    def test_closed_popup_returns_to_surviving_window_without_new_navigation(self):
        driver=self.browser.driver;original=driver.current_window_handle;url=driver.current_url
        driver.switch_to.new_window('tab');driver.get(url);driver.close()
        self.c._windows()
        self.assertEqual(driver.current_window_handle,original)
        self.assertEqual(driver.current_url,url)

    def test_stopped_state_allows_user_input_on_https_authentication_host(self):
        from unittest.mock import patch,PropertyMock
        self.c.demo=False;self.c.state['phase']='stopped'
        driver=self.browser.driver
        driver.execute_script("document.body.innerHTML='<label>認証入力<input id=otp></label>'")
        with patch.object(type(driver),'current_url',new_callable=PropertyMock,return_value='https://issuer.example.test/auth'):
            field=next(f for f in self.c._form_fields() if f['type']=='text')
            self.c._manual({'kind':'field','token':field['token'],'value':'dummy-only'})
            self.assertEqual(driver.find_element('id','otp').get_attribute('value'),'dummy-only')
        self.c.demo=True

    def test_frame_diagnostics_do_not_export_query_or_entered_values(self):
        self.browser.driver.execute_script("const f=document.createElement('iframe');f.srcdoc=arguments[0];f.style='height:300px;width:350px';document.body.replaceChildren(f)",'<input value=private-otp>')
        import time;time.sleep(.2)
        self.c._form_fields()
        data=json.dumps(self.c.frame_diagnostics)
        self.assertNotIn('private-otp',data)
        self.assertEqual(self.c.frame_diagnostics[0]['readyState'],'complete')

    def test_hidden_auth_frame_is_diagnosed_without_exposing_controls(self):
        self.browser.driver.execute_script("const f=document.createElement('iframe');f.style.display='none';f.srcdoc=arguments[0];document.body.replaceChildren(f)",'<input value=private-hidden><button>secret-action</button>')
        import time;time.sleep(.2)
        fields=self.c._form_fields()
        self.assertEqual(fields,[])
        self.assertEqual(len(self.c.frame_diagnostics),1)
        self.assertFalse(self.c.frame_diagnostics[0]['visible'])
        self.assertNotIn('private-hidden',json.dumps(self.c.frame_diagnostics))

    def test_network_diagnostics_strip_urls_headers_and_payloads(self):
        from unittest.mock import Mock
        entries=[{'method':'Network.requestWillBeSent','params':{'requestId':'1','type':'Document','request':{'url':'https://issuer.example.test/auth?token=secret','headers':{'Authorization':'secret'},'postData':'private-otp'}}},
                 {'method':'Network.loadingFailed','params':{'requestId':'1','errorText':'net::ERR_CONNECTION_TIMED_OUT'}}]
        driver=self.browser.driver
        self.browser.driver=Mock()
        try:
            self.browser.driver.get_log.return_value=[{'message':json.dumps({'message':e})} for e in entries]
            result=self.browser.network_diagnostics()
            self.assertEqual(result,[{'host':'issuer.example.test','type':'Document','error':'net::ERR_CONNECTION_TIMED_OUT'}])
            self.assertNotIn('secret',json.dumps(result));self.assertNotIn('private-otp',json.dumps(result))
        finally:self.browser.driver=driver

    def test_loaded_hidden_json_error_is_diagnosed_without_exposing_private_text(self):
        body=json.dumps({'errorCode':'E400','message':'private-otp','transactionId':'secret-token','card':'4111111111111111'})
        self.browser.driver.execute_script("const f=document.createElement('iframe');f.style.display='none';f.srcdoc=arguments[0];document.body.replaceChildren(f)",'<pre>'+body+'</pre>')
        import time;time.sleep(.2)
        self.assertEqual(self.c._form_fields(),[])
        result=self.c.frame_diagnostics
        self.assertEqual(result[0]['serviceCodes'],['E400'])
        serialized=json.dumps(result)
        for secret in ['private-otp','secret-token','4111111111111111']:
            self.assertNotIn(secret,serialized)
        self.assertTrue(self.browser.driver.execute_script('return window===window.top'))

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

    def test_login_submit_fills_and_clicks_once_without_using_header_link(self):
        self.login_fixture();d=self.browser.driver
        d.execute_script("""document.body.insertAdjacentHTML('afterbegin','<header><a href="#" onclick="document.body.dataset.wrong=1">ログイン</a></header>');document.querySelector('form').onsubmit=()=>{document.body.dataset.count=Number(document.body.dataset.count||0)+1;document.body.dataset.submitted='yes';return false;}""")
        self.c._manual({'kind':'login_submit','username':'qa-user','password':'qa-password'})
        self.assertEqual(d.execute_script('return document.body.dataset.count'),'1')
        self.assertIsNone(d.execute_script('return document.body.dataset.wrong||null'))
        self.assertEqual(d.find_element('id','loginMail').get_attribute('value'),'qa-user')

    def test_login_submit_stops_when_buttons_are_ambiguous(self):
        self.login_fixture();d=self.browser.driver
        d.execute_script("document.querySelector('form').insertAdjacentHTML('beforeend','<button type=submit>ログイン</button>')")
        with self.assertRaises(ValueError):self.c._manual({'kind':'login_submit','username':'qa-user','password':'qa-password'})
        self.assertIsNone(d.execute_script('return document.body.dataset.submitted||null'))

    def test_login_submit_waits_for_enabled_button_and_works_after_stop(self):
        self.login_fixture();d=self.browser.driver;self.browser.stop.set()
        d.execute_script("document.querySelector('button').disabled=true;setTimeout(()=>document.querySelector('button').disabled=false,200)")
        self.c._manual({'kind':'login_submit','username':'qa-user','password':'qa-password'})
        self.assertEqual(d.execute_script('return document.body.dataset.submitted'),'yes')

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
