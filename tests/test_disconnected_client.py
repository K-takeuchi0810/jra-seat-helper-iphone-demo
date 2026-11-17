"""Real HTTP + worker + Chrome test; only the local fictitious seat page is used."""
import datetime as dt
from http import client
from http.server import ThreadingHTTPServer
import json
import tempfile
import threading
import time
import unittest

from controller import Controller
from engine import Browser,JST
from server import Handler,PhoneApp
from test_backend import FakePush,SECRET


class DisconnectedClientTests(unittest.TestCase):
    def test_real_job_and_notification_event_continue_without_a_connected_phone(self):
        with tempfile.TemporaryDirectory() as directory:
            factory=lambda **kwargs:Controller(browser_factory=Browser,**kwargs)
            app=PhoneApp(SECRET,directory,secure=False,controller_factory=factory,push_factory=FakePush)
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
            server.daemon_threads=True;server.application=app
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            port=server.server_address[1];cookie=''

            def call(path,data=None):
                headers={'Cookie':cookie}
                if data is not None:headers.update({'Content-Type':'application/json','X-Seat-Request':'1'})
                conn=client.HTTPConnection('127.0.0.1',port,timeout=5)
                conn.request('POST' if data is not None else 'GET',path,json.dumps(data) if data is not None else None,headers)
                response=conn.getresponse();body=json.loads(response.read());status=response.status;response_headers=dict(response.getheaders())
                conn.close()
                self.assertIn(status,(200,202),body)
                return body,response_headers

            def wait_idle(timeout=20):
                deadline=time.monotonic()+timeout
                while time.monotonic()<deadline:
                    state,_=call('/api/state')
                    if not state['busy']:return state
                    time.sleep(.1)
                self.fail('Browser worker did not finish in time')

            try:
                _,headers=call('/api/login',{'secret':SECRET});cookie=headers['Set-Cookie'].split(';')[0]
                call('/api/browser/open',{});self.assertEqual(wait_idle()['phase'],'ready')
                call('/api/browser/capture',{});self.assertTrue(wait_idle()['targetReady'])
                start=(dt.datetime.now(JST)+dt.timedelta(seconds=2)).strftime('%Y-%m-%d %H:%M:%S')
                call('/api/run',{'wishes':['A指定席'],'count':2,'area':'前列','start':start,
                     'watch':False,'interval':60,'minutes':1,'confirmed':True})

                # There are no open client connections or UI polling during this interval.
                time.sleep(5)
                state=wait_idle()
                self.assertEqual(state['phase'],'payment',state['logs'])
                self.assertFalse(state['targetReady'])
                payment_events=[item for item in app.push.calls if item[1]=='payment_ready']
                self.assertEqual(len(payment_events),1)
                self.assertEqual(payment_events[0][0],state['job'])
                self.assertNotIn('stopped',[event for _,event in app.push.calls])
                self.assertIn('支払い・購入内容',state['detail'])
            finally:
                server.shutdown();server.server_close();app.close()


if __name__=='__main__':unittest.main(verbosity=2)
