import json
import threading
import unittest
from unittest.mock import Mock
from engine import Browser,safe_service_codes,safe_service_details,safe_cookie_blocks

class AuthDiagnosticTests(unittest.TestCase):
    def test_cookie_diagnostics_discard_values_and_unknown_reason_text(self):
        params={'associatedCookies':[{'cookie':{'name':'private-name','value':'private-otp'},'blockedReasons':['ThirdPartyPhaseout','private-token']}],'blockedCookies':[{'cookieLine':'secret=private-otp','blockedReasons':['SameSiteNoneInsecure']}]}
        self.assertEqual(safe_cookie_blocks(params),['SameSiteNoneInsecure','ThirdPartyPhaseout'])
        self.assertNotIn('private',json.dumps(safe_cookie_blocks(params)))

    def test_cookie_extra_info_before_request_is_correlated_without_headers_or_replay(self):
        b=Browser(threading.Event(),lambda *a:None,demo=True);b.driver=Mock()
        events=[{'method':'Network.requestWillBeSentExtraInfo','params':{'requestId':'local','associatedCookies':[{'cookie':{'name':'secret','value':'private-otp'},'blockedReasons':['ThirdPartyPhaseout']}],'headers':{'Cookie':'secret=private-otp'}}},
                {'method':'Network.requestWillBeSent','params':{'requestId':'local','type':'Document','wallTime':1700000000,'request':{'url':'https://as-pgx.ps.paysec-3dssvgw.com/path?token=private-token','method':'GET'}}}]
        b.driver.get_log.return_value=[{'message':json.dumps({'message':e})} for e in events]
        result=b.network_diagnostics()
        self.assertEqual(result[0]['blockedCookieReasons'],['ThirdPartyPhaseout'])
        self.assertEqual(result[0]['at'],'2023-11-14T22:13:20+00:00')
        self.assertNotIn('private',json.dumps(result));self.assertNotIn('headers',json.dumps(result))
        b.driver.get.assert_not_called();b.driver.refresh.assert_not_called()

    def test_error_categories_expose_fixed_labels_only(self):
        self.assertEqual(safe_service_details('<h1>Request Header Or Cookie Too Large</h1><p>secret-token</p>')['categories'],['cookie-too-large'])
        self.assertEqual(safe_service_details('Unsupported browser; private-otp')['categories'],['unsupported-browser'])

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
        self.assertEqual(result,[{'host':'as-pgx.ps.paysec-3dssvgw.com','method':'POST','type':'Document','status':400,'serviceCodes':['E400'],'responseDetails':{'codes':['E400'],'format':'json'}}])
        browser.driver.execute_cdp_cmd.assert_called_once_with('Network.getResponseBody',{'requestId':'local-only'})
        self.assertNotIn('secret-token',json.dumps(result));self.assertNotIn('private-otp',json.dumps(result))
        browser.driver.get.assert_not_called();browser.driver.refresh.assert_not_called()

    def test_script_errors_export_category_not_text_or_tokens(self):
        browser=Browser(threading.Event(),lambda *a:None,demo=True);browser.driver=Mock()
        browser.driver.get_log.return_value=[{'message':'https://issuer.test/?secret Uncaught TypeError: private-otp is invalid'}, {'message':'Refused due to Content Security Policy: secret-token'}]
        result=browser.script_diagnostics()
        self.assertEqual(result,[{'category':'TypeError','sourceHost':'issuer.test'},{'category':'CSP'}])
        self.assertNotIn('secret',json.dumps(result));self.assertNotIn('private-otp',json.dumps(result))

    def test_reference_error_exports_symbol_and_origin_only(self):
        browser=Browser(threading.Event(),lambda *a:None,demo=True);browser.driver=Mock()
        browser.driver.get_log.return_value=[{'message':'https://as-pgx.ps.paysec-3dssvgw.com/js?token=secret 1:5 Uncaught ReferenceError: authenticationInit is not defined; private-otp'}]
        self.assertEqual(browser.script_diagnostics(),[{'category':'ReferenceError','symbol':'authenticationInit','sourceHost':'as-pgx.ps.paysec-3dssvgw.com'}])

    def test_html_and_validation_details_do_not_export_response_text(self):
        self.assertEqual(safe_service_details('<h1>Bad Request</h1><p>private-otp secret-token</p>'),{'codes':[],'format':'html','categories':['bad-request']})
        details=safe_service_details(json.dumps({'errors':{'threeDSMethodData':['Field is required; secret-token'],'cardNumber':['4111111111111111']}}))
        self.assertEqual(details['mentionedFields'],['threeDSMethodData'])
        for secret in ('secret-token','4111111111111111','cardNumber'):
            self.assertNotIn(secret,json.dumps(details))

if __name__=='__main__':unittest.main()
