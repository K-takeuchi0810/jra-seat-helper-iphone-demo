import datetime as dt
from http import client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

from controller import Controller
from engine import Halt,JST
from push_service import PushService
from server import Handler,PhoneApp

SECRET='test-only-not-for-deployment-01234567890123456789'


class FakeController:
    def __init__(self,**kwargs):
        self.lock=threading.RLock();self.frame=b'test';self.calls=[]
    def submit(self,*args):self.calls.append(args)
    def log(self,*args,**kwargs):pass
    def close(self):pass
    def snapshot(self):return {'phase':'idle','logs':[]}


class FakePush:
    def __init__(self,secret,directory,on_result):
        self.subscriptions=[];self.public_key='public';self.calls=[]
    def enqueue(self,*args):self.calls.append(args)
    def close(self):pass
    def subscribe(self,*args):self.subscriptions.append(args)


class APITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.app=PhoneApp(SECRET,self.tmp.name,secure=False,controller_factory=FakeController,push_factory=FakePush)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.daemon_threads=True;self.server.application=self.app
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.port=self.server.server_address[1]
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.app.close();self.tmp.cleanup()
    def request(self,path,data=None,cookie=None,origin=None,guard=True):
        headers={}
        if cookie:headers['Cookie']=cookie
        if origin:headers['Origin']=origin
        body=None
        if data is not None:
            body=json.dumps(data);headers['Content-Type']='application/json'
            if guard:headers['X-Seat-Request']='1'
        conn=client.HTTPConnection('127.0.0.1',self.port,timeout=3)
        conn.request('POST' if data is not None else 'GET',path,body,headers)
        response=conn.getresponse();value=response.read();status=response.status;resultheaders=dict(response.getheaders());conn.close()
        return status,value,resultheaders
    def login(self):
        status,_,headers=self.request('/api/login',{'secret':SECRET});self.assertEqual(status,200)
        return headers['Set-Cookie'].split(';')[0]
    def test_private_state_and_frames_require_login(self):
        self.assertEqual(self.request('/api/state')[0],401)
        self.assertEqual(self.request('/api/frame')[0],401)
    def test_login_cookie_is_httponly_and_logout_revokes_copied_cookie(self):
        cookie=self.login();self.assertEqual(self.request('/api/state',cookie=cookie)[0],200)
        self.assertEqual(self.request('/api/logout',{},cookie=cookie)[0],200)
        self.assertEqual(self.request('/api/state',cookie=cookie)[0],401)
    def test_csrf_guard_prevents_cross_origin_or_plain_form_writes(self):
        cookie=self.login()
        self.assertEqual(self.request('/api/browser/open',{},cookie,origin='https://another.example')[0],403)
        self.assertEqual(self.request('/api/browser/open',{},cookie,guard=False)[0],403)
        self.assertEqual(self.app.controller.calls,[])
    def test_logout_does_not_cancel_or_close_browser(self):
        cookie=self.login();self.request('/api/logout',{},cookie)
        self.assertEqual(self.app.controller.calls,[])
    def test_multiple_wrong_logins_are_rate_limited(self):
        for _ in range(6):self.assertEqual(self.request('/api/login',{'secret':'wrong'})[0],401)
        self.assertEqual(self.request('/api/login',{'secret':SECRET})[0],429)
    def test_receipt_requires_latest_test_identifier_and_human_action(self):
        cookie=self.login()
        self.assertEqual(self.request('/api/push/received',{'testId':'fake'},cookie)[0],400)
        _,body,_=self.request('/api/push/test',{},cookie);test=json.loads(body)['testId']
        self.assertFalse(self.app.push_status['received'])
        self.request('/api/push/received',{'testId':test},cookie)
        self.assertTrue(self.app.push_status['received'])
    def test_static_shell_has_no_private_browser_data(self):
        status,body,headers=self.request('/')
        self.assertEqual(status,200);self.assertNotIn(SECRET.encode(),body)
        self.assertEqual(headers['Cache-Control'],'no-store')


class SlowBrowser:
    def __init__(self,stop,log,demo,on_event):self.stop=stop;self.log=log;self.driver=None
    def run(self,*args):
        while not self.stop.wait(.05):pass
        raise Halt('中止しました')


class ControllerTests(unittest.TestCase):
    def job(self):
        return {'wishes':['A指定席'],'count':1,'interval':60,'minutes':10,'watch':True,
                'start':(dt.datetime.now(JST)+dt.timedelta(seconds=60)).strftime('%Y-%m-%d %H:%M:%S'),'confirmed':True}
    def test_requires_verified_target_and_rejects_boolean_counts(self):
        job=self.job();job['count']=True
        with self.assertRaises(ValueError):Controller.validate_job(job)
        job=self.job();job['confirmed']=False
        with self.assertRaises(ValueError):Controller.validate_job(job)
    def test_running_job_survives_absence_of_a_phone_client_and_can_be_stopped(self):
        c=Controller(browser_factory=SlowBrowser)
        try:
            c.state.update(targetReady=True,seats=[{'name':'A指定席'}])
            c.submit('run',self.job())
            time.sleep(.2)
            self.assertTrue(c.snapshot()['busy'])
            with self.assertRaises(ValueError):c.submit('run',self.job())
            c.submit('stop')
            deadline=time.monotonic()+2
            while c.snapshot()['busy'] and time.monotonic()<deadline:time.sleep(.02)
            self.assertFalse(c.snapshot()['busy']);self.assertEqual(c.snapshot()['phase'],'stopped')
        finally:c.close()
    def test_notification_failure_does_not_kill_browser_owner_thread(self):
        c=Controller(browser_factory=SlowBrowser,notify=Mock(side_effect=OSError('no network')))
        try:
            c.state.update(targetReady=True,seats=[{'name':'A指定席'}]);c.submit('run',self.job());time.sleep(.1);c.submit('stop')
            c.commands.join();self.assertTrue(c.thread.is_alive())
        finally:c.close()
    def test_notification_status_does_not_replace_payment_result(self):
        c=Controller(browser_factory=SlowBrowser)
        try:
            c.log('支払い画面を検出しました。')
            c.event('payment_ready')
            c.log('通知先が登録されていません。',status=False)
            self.assertEqual(c.snapshot()['detail'],'支払い画面を検出しました。')
            self.assertEqual(c.snapshot()['phase'],'payment')
            self.assertFalse(c.snapshot()['targetReady'])
        finally:c.close()


class PushTests(unittest.TestCase):
    def test_fixed_messages_deduplicate_and_do_not_leak_credentials(self):
        sent=[];results=[]
        def invoke(payload):
            if payload['operation']=='publicKey':return {'publicKey':'public'}
            if payload['operation']=='validate':return {'valid':True}
            sent.append(payload);return {'accepted':True,'status':201}
        with tempfile.TemporaryDirectory() as directory:
            p=PushService(SECRET,directory,lambda *args:results.append(args),invoke)
            try:
                p.subscribe({'endpoint':'https://web.push.apple.com/test','keys':{}},'https://seat-helper.example')
                self.assertTrue(p.enqueue('job1','payment_ready'));self.assertFalse(p.enqueue('job1','payment_ready'))
                p.pending.join();self.assertEqual(len(sent),1)
                self.assertNotIn(SECRET,json.dumps(sent[0]));self.assertIn('購入は完了していません',sent[0]['message']['body'])
                self.assertTrue(results[0][2])
            finally:p.close();p.pending.join()
    def test_dead_subscriptions_are_removed_and_not_claimed_delivered(self):
        def invoke(payload):
            return {'publicKey':'public'} if payload['operation']=='publicKey' else {'valid':True} if payload['operation']=='validate' else {'accepted':False,'status':410}
        with tempfile.TemporaryDirectory() as directory:
            results=[];p=PushService(SECRET,directory,lambda *args:results.append(args),invoke)
            try:
                p.subscribe({'endpoint':'https://web.push.apple.com/test'},'https://seat-helper.example');p.enqueue('j','test');p.pending.join()
                self.assertEqual(p.subscriptions,[]);self.assertFalse(results[0][2])
            finally:p.close();p.pending.join()


if __name__=='__main__':unittest.main(verbosity=2)
