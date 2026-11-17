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
        self.c.browser=self.browser;self.c.state={'width':390,'height':760,'phase':'ready','frame':0};self.c.demo=True
        self.c.selected_element=None;self.c.select_token='';self.c.last_frame=0
        self.c.manual=queue.Queue();self.c.owner=threading.get_ident();self.c.pumping=False
        self.c.lock=threading.RLock();self.c.frame=b''
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

if __name__=='__main__':unittest.main()
