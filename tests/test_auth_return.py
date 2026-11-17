"""Synthetic OTP/redirect only. Never contacts an issuer or creates a purchase."""
import threading,time,unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from controller import Controller
from engine import Browser
from selenium.webdriver.support.ui import WebDriverWait

class AuthenticationReturnTests(unittest.TestCase):
    def test_manual_dummy_otp_returns_from_frame_to_mock_booking_confirmation(self):
        class Merchant(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.end_headers()
                self.wfile.write('<h1>購入完了（架空）</h1><p>架空の席を取得しました。課金はありません。</p>'.encode())
            def log_message(self,*args):pass
        merchant=ThreadingHTTPServer(('127.0.0.1',0),Merchant)
        class Issuer(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.end_headers()
                self.wfile.write('<form method=post><label>確認コード（架空）<input name=otp></label><button>認証（架空）</button></form>'.encode())
            def do_POST(self):
                body=self.rfile.read(int(self.headers.get('Content-Length','0')))
                if body!=b'otp=000000':self.send_error(400);return
                self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.end_headers()
                self.wfile.write(('<script>top.location="http://localhost:'+str(merchant.server_port)+'/complete"</script>').encode())
            def log_message(self,*args):pass
        issuer=ThreadingHTTPServer(('127.0.0.1',0),Issuer)
        for server in [merchant,issuer]:threading.Thread(target=server.serve_forever,daemon=True).start()
        browser=Browser(threading.Event(),lambda *a:None,demo=True)
        try:
            browser.open();driver=browser.driver
            driver.execute_script("document.body.innerHTML='<h2>クレジットカード情報入力</h2>';const f=document.createElement('iframe');f.style='width:350px;height:300px';f.src=arguments[0];document.body.append(f)",f'http://127.0.0.1:{issuer.server_port}/')
            self.assertEqual(browser.outcome(),'handoff')
            c=Controller.__new__(Controller);c.browser=browser;c.state={'phase':'payment'};c.demo=True;c.form_targets={};c.last_frame=0
            def find_fields(_):
                fields=c._form_fields()
                return fields if any(f['label']=='認証（架空）' for f in fields) else False
            fields=WebDriverWait(driver,10).until(find_fields)
            field=next(f for f in fields if f['type']=='text')
            button=next(f for f in fields if f['label']=='認証（架空）')
            c._manual({'kind':'field','token':field['token'],'value':'000000'})
            c._manual({'kind':'field','token':button['token'],'activate':True})
            WebDriverWait(driver,10).until(lambda d:d.current_url==f'http://localhost:{merchant.server_port}/complete')
            self.assertIn('購入完了（架空）',driver.find_element('tag name','body').text)
        finally:
            if browser.driver:browser.driver.quit()
            for server in [merchant,issuer]:server.shutdown();server.server_close()

if __name__=='__main__':unittest.main()
