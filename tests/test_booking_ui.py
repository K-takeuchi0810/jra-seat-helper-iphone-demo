"""Mobile settings and cancel-wait selection against a local fake controller."""
import datetime as dt
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from selenium.webdriver.support.ui import Select, WebDriverWait
from engine import Browser, JST
from server import PhoneApp, Handler

SECRET='local-ui-test-only-012345678901234567890123456789'
DAY=(dt.datetime.now(JST).date()+dt.timedelta(days=5)).isoformat()
class UIController:
    def __init__(self,**kwargs):
        self.lock=threading.RLock();self.frame=b'';self.calls=[]
        self.state={'phase':'ready','busy':False,'seats':[{'name':'A指定席','available':True,'stock':'vacant','areas':[]},{'name':'B指定席','available':False,'stock':'none','areas':[]}], 'booking':{'course':'東京','date':DAY}, 'catalog':[{'course':'東京','dates':[{'date':DAY,'label':'対象日'}]}],'targetReady':True,'demo':True,'frame':1,'frameAt':0,'logs':[]}
    def snapshot(self):return dict(self.state)
    def submit(self,op,data=None):self.calls.append((op,data))
    def log(self,*a,**kw):pass
    def close(self):pass
class UIPush:
    def __init__(self,*a):self.subscriptions=[];self.public_key='dummy'
    def enqueue(self,*a):pass
    def close(self):pass

class BookingUITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.app=PhoneApp(SECRET,self.tmp.name,secure=False,controller_factory=UIController,push_factory=UIPush)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.application=self.app
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.browser=Browser(threading.Event(),lambda *a:None,True);self.browser.open();self.driver=self.browser.driver
        self.driver.get('http://127.0.0.1:'+str(self.server.server_port))
        self.driver.find_element('id','loginSecret').send_keys(SECRET);self.driver.find_element('css selector','#loginForm button').click()
        WebDriverWait(self.driver,5).until(lambda d:d.find_element('id','appView').is_displayed())
        self.driver.execute_script("document.documentElement.style.scrollBehavior='auto'")
    def tearDown(self):
        self.driver.get('about:blank');self.driver.quit();self.server.shutdown();self.server.server_close();self.app.close();self.tmp.cleanup()
    def click(self,ident):
        element=self.driver.find_element("id",ident)
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'})",element)
        element.click()
    def select_target(self):
        Select(self.driver.find_element('id','racecourse')).select_by_value('東京')
        Select(self.driver.find_element('id','eventDate')).select_by_value(DAY)
        WebDriverWait(self.driver,5).until(lambda d:len(Select(d.find_element('id','wish1')).options)==3)
    def test_full_seat_stays_selectable_for_cancel_wait_and_submission_has_target(self):
        self.select_target()
        choice=Select(self.driver.find_element('id','wish1'));self.assertIn('現在満席',choice.options[2].text)
        choice.select_by_value('B指定席')
        self.click('watch')
        self.click('reviewJob')
        self.assertIn('東京競馬場',self.driver.find_element('id','jobSummary').text)
        self.click('venueConfirmed');self.click('confirmStart')
        WebDriverWait(self.driver,5).until(lambda d:any(op=='run' for op,_ in self.app.controller.calls))
        job=next(v for op,v in self.app.controller.calls if op=='run')
        self.assertEqual(job['startMode'],'now');self.assertEqual(job['start'],'')
        self.assertEqual(job['booking'],{'course':'東京','date':DAY});self.assertEqual(job['wishes'],['B指定席']);self.assertTrue(job['watch'])
    def test_existing_browser_action_does_not_open_another_session(self):
        self.click('openBrowser')
        self.assertTrue(self.driver.find_element('css selector','[data-view=browser]').is_displayed())
        self.assertFalse(any(op=='open' for op,_ in self.app.controller.calls))
        self.assertFalse(self.driver.find_element('id','diagnosticDetails').get_attribute('open'))

    def test_external_select_tracks_page_changes_and_search_empty_state(self):
        self.app.controller.state['formFields']=[{'token':'mock-select','label':'選択項目','type':'select','options':[{'text':'最初','disabled':False},{'text':'別の項目','disabled':False}],'selected':0}]
        self.driver.find_element('css selector','nav [data-tab=browser]').click()
        WebDriverWait(self.driver,5).until(lambda d:len(d.find_elements('id','page-field-mock-select'))==1)
        self.app.controller.state['formFields'][0]['selected']=1
        WebDriverWait(self.driver,5).until(lambda d:d.find_element('id','page-field-mock-select').get_attribute('value')=='1')
        self.driver.find_element('id','pageControlSearch').send_keys('存在しない項目')
        self.assertTrue(self.driver.find_element('id','pageFieldsEmpty').is_displayed())

    def test_browser_close_requires_an_in_page_confirmation_and_can_be_cancelled(self):
        self.click('openBrowser');self.click('closeBrowser')
        self.assertTrue(self.driver.find_element('id','closeBrowserDialog').is_displayed())
        self.click('cancelCloseBrowser')
        self.assertFalse(any(op=='close' for op,_ in self.app.controller.calls))
        self.click('closeBrowser');self.click('confirmCloseBrowser')
        WebDriverWait(self.driver,5).until(lambda d:any(op=='close' for op,_ in self.app.controller.calls))
        WebDriverWait(self.driver,5).until(lambda d:not d.find_element('id','closeBrowserDialog').is_displayed())

    def test_navigation_links_are_collapsed_but_remain_searchable(self):
        self.app.controller.state['formFields']=[{'token':'main','label':'ログインを送信','type':'action','group':'page'},{'token':'footer','label':'JRAホームページ','type':'action','group':'navigation'}]
        self.driver.find_element('css selector','nav [data-tab=browser]').click()
        WebDriverWait(self.driver,5).until(lambda d:len(d.find_elements('id','extraPageFields'))==1)
        self.assertFalse(self.driver.find_element('id','extraPageFields').get_attribute('open'))
        self.driver.find_element('id','pageControlSearch').send_keys('JRAホーム')
        self.assertTrue(self.driver.find_element('id','extraPageFields').get_attribute('open'))
        self.assertFalse(self.driver.find_element('id','pageFieldsEmpty').is_displayed())

    def test_mobile_layout_and_scheduled_start_remain_usable(self):
        self.click('startScheduled')
        self.assertTrue(self.driver.find_element('id','startTime').is_enabled())
        self.assertTrue(self.driver.find_element('id','startTimeRow').is_displayed())
        self.click('startNow')
        self.assertFalse(self.driver.find_element('id','startTime').is_enabled())
        for width in (320,390):
            self.driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride',{'width':width,'height':844,'deviceScaleFactor':1,'mobile':False})
            self.assertLessEqual(self.driver.execute_script('return document.documentElement.scrollWidth'),width)
        self.assertNotIn('希望の席を',self.driver.find_element('tag name','body').text)

    def test_cached_seat_can_start_cancel_wait_without_a_live_seat_list(self):
        self.app.controller.state.update(targetReady=False,seats=[],booking=None,seatCatalog={'東京':{'names':['A指定席','B指定席'],'observedAt':'2026-11-18','eventDate':DAY}})
        self.select_target()
        WebDriverWait(self.driver,5).until(lambda d:d.execute_script("return document.querySelector('#wish1 option:nth-child(2)')?.textContent.includes('未確認')"))
        Select(self.driver.find_element('id','wish1')).select_by_value('A指定席')
        self.click('watch');Select(self.driver.find_element('id','count')).select_by_value('2')
        self.click('reviewJob');self.click('venueConfirmed');self.click('confirmStart')
        WebDriverWait(self.driver,5).until(lambda d:any(op=='run' for op,_ in self.app.controller.calls))
        job=next(v for op,v in self.app.controller.calls if op=='run')
        self.assertEqual(job['wishes'],['A指定席']);self.assertEqual(job['count'],2);self.assertTrue(job['watch'])

    def test_preparation_and_secondary_full_seat_gate_start(self):
        self.select_target()
        Select(self.driver.find_element('id','wish1')).select_by_value('A指定席')
        self.assertTrue(self.driver.find_element('id','reviewJob').is_enabled())
        Select(self.driver.find_element('id','wish2')).select_by_value('B指定席')
        self.assertFalse(self.driver.find_element('id','reviewJob').is_enabled())
        self.click('watch')
        self.assertTrue(self.driver.find_element('id','reviewJob').is_enabled())
        Select(self.driver.find_element('id','racecourse')).select_by_value('')
        self.assertFalse(self.driver.find_element('id','saveConditions').is_enabled())
        self.assertFalse(self.driver.find_element('id','eventDate').is_enabled())
        self.assertEqual(self.driver.find_element('id','wish1').get_attribute('value'),'')
        self.assertFalse(self.driver.find_element('id','area').is_enabled())

    def test_payment_screen_disables_navigation_and_new_reservation(self):
        self.select_target()
        Select(self.driver.find_element('id','wish1')).select_by_value('A指定席')
        self.app.controller.state['phase']='payment'
        WebDriverWait(self.driver,5).until(lambda d:not d.find_element('id','captureSeats').is_enabled())
        self.assertFalse(self.driver.find_element('id','loadCatalog').is_enabled())
        self.assertFalse(self.driver.find_element('id','reviewJob').is_enabled())
        self.assertTrue(self.driver.find_element('id','openBrowser').is_enabled())

    def test_saved_conditions_restore_without_automatically_starting(self):
        self.select_target();Select(self.driver.find_element('id','wish1')).select_by_value('A指定席')
        Select(self.driver.find_element('id','count')).select_by_value('2')
        self.click('saveConditions');self.driver.refresh()
        WebDriverWait(self.driver,5).until(lambda d:d.find_element('id','count').get_attribute('value')=='2')
        self.assertEqual(self.driver.find_element('id','racecourse').get_attribute('value'),'東京')
        self.assertEqual(self.driver.find_element('id','wish1').get_attribute('value'),'A指定席')
        self.assertFalse(any(op=='run' for op,_ in self.app.controller.calls))
        self.assertLessEqual(self.driver.execute_script('return document.documentElement.scrollWidth'),390)

if __name__=='__main__':unittest.main(verbosity=2)
