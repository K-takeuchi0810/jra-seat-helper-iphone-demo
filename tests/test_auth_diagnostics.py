import json
import threading
import unittest
from unittest.mock import Mock
from engine import Browser,safe_service_codes

class AuthDiagnosticTests(unittest.TestCase):
    def test_only_explicit_error_codes_leave_response_parser(self):
        value={'errorCode':'E400','message':'private-otp','transactionId':'secret-token','card':'4111111111111111','nested':{'error_type':'invalid_request'}}
        self.assertEqual(safe_service_codes(json.dumps(value)),['E400','invalid_request'])
        self.assertEqual(safe_service_codes('plain text secret-token'),[])
        self.assertEqual(safe_service_codes(json.dumps({'errorCode':'private-token-with-id-123456789'})),[])
        self.assertEqual(safe_service_codes('x'*65537),[])

    def test_http400_response_is_inspected_without_request_replay(self):
        browser=Browser(threading.Event(),lambda *a:None,demo=True)
        browser.driver=Mock()
        url='https://as-pgx.ps.paysec-3dssvgw.com/auth?transaction=secret-token'
        events=[
            {'method':'Network.requestWillBeSent','params':{'requestId':'local-only','type':'Document','request':{'url':url,'method':'POST','postData':'private-otp'}}},
            {'method':'Network.responseReceived','params':{'requestId':'local-only','type':'Document','response':{'url':url,'status':400}}},
            {'method':'Network.loadingFinished','params':{'requestId':'local-only'}}]
        browser.driver.get_log.return_value=[{'message':json.dumps({'message':e})} for e in events]
        browser.driver.execute_cdp_cmd.return_value={'base64Encoded':False,'body':json.dumps({'errorCode':'E400','message':'private-otp','transactionId':'secret-token'})}
        result=browser.network_diagnostics()
        self.assertEqual(result,[{'host':'as-pgx.ps.paysec-3dssvgw.com','method':'POST','type':'Document','status':400,'serviceCodes':['E400']}])
        browser.driver.execute_cdp_cmd.assert_called_once_with('Network.getResponseBody',{'requestId':'local-only'})
        self.assertNotIn('secret-token',json.dumps(result));self.assertNotIn('private-otp',json.dumps(result))
        browser.driver.get.assert_not_called();browser.driver.refresh.assert_not_called()

    def test_script_errors_export_category_not_text_or_tokens(self):
        browser=Browser(threading.Event(),lambda *a:None,demo=True);browser.driver=Mock()
        browser.driver.get_log.return_value=[{'message':'https://issuer.test/?secret Uncaught TypeError: private-otp is invalid'}, {'message':'Refused due to Content Security Policy: secret-token'}]
        result=browser.script_diagnostics()
        self.assertEqual(result,[{'category':'TypeError'},{'category':'CSP'}])
        self.assertNotIn('secret',json.dumps(result));self.assertNotIn('private-otp',json.dumps(result))

if __name__=='__main__':unittest.main()
