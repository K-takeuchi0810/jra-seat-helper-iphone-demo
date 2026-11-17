"""A single owner thread serializes browser automation and human input."""
from __future__ import annotations

from collections import deque
import datetime as dt
import json
from pathlib import Path
import queue
import re
import base64
import threading
import time
import uuid
from urllib.parse import urlsplit

from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import StaleElementReferenceException, NoSuchWindowException
from engine import Browser, Halt, Unavailable, JST, JRA_HOSTS, FRONT_JS, parse_start


class CooperativeStop:
    def __init__(self, pump):
        self.flag = threading.Event()
        self.pump = pump
    def is_set(self):
        self.pump()
        return self.flag.is_set()
    def set(self):
        self.flag.set()
    def clear(self):
        self.flag.clear()
    def wait(self, seconds):
        end = time.monotonic() + max(0, seconds)
        while time.monotonic() < end:
            self.pump()
            if self.flag.wait(min(.15, max(0, end-time.monotonic()))):
                return True
        return self.flag.is_set()


class Controller:
    MANUAL_PHASES = {"ready", "attention", "payment", "stopped", "unavailable", "error"}
    def __init__(self, demo=True, browser_factory=Browser, notify=None):
        self.demo = demo
        self.notify = notify or (lambda job, event: None)
        self.lock = threading.RLock()
        self.commands = queue.Queue(maxsize=20)
        self.manual = queue.Queue(maxsize=20)
        self.shutdown = threading.Event()
        self.pumping = False
        self.owner = None
        self.stop = CooperativeStop(self.pump)
        self.browser = browser_factory(self.stop, self.log, demo, self.event)
        self.frame = b""
        self.last_frame = 0
        self.selected_element = None
        self.select_token = ""
        self.form_targets = {}
        self.logs = deque(maxlen=100)
        self.state = {"phase": "idle", "busy": False, "seats": [], "targetReady": False,
                      "demo": demo, "job": "", "frame": 0, "frameAt": 0,
                      "width": 390, "height": 760, "host": "", "notification": "未設定", "detail": "", "browserDiagnostics": None, "browserWindows": [], "formFields": [], "activeSelect": None, "loginReady": False, "manualResult": None}
        self.thread = threading.Thread(target=self._loop, daemon=True, name="browser-owner")
        self.thread.start()

    def log(self, text, status=True):
        with self.lock:
            self.logs.append({"time": dt.datetime.now(JST).strftime("%H:%M:%S"), "text": text})
            if status:
                self.state["detail"] = text
            if self.state["busy"] and "取り置きを申し込みます" in text:
                self.state["phase"] = "running"

    def event(self, name):
        with self.lock:
            if name == "retry_wait":
                self.state["phase"] = "retrying"
                return
            if name == "attention_required":
                self.state["phase"] = "attention"
            elif name == "payment_ready":
                self.state["phase"] = "payment"
                self.state["targetReady"] = False
            job = self.state["job"]
        self._notify(job, name)

    def _notify(self, job, event):
        try:
            self.notify(job, event)
        except Exception:
            self.log("通知の受付を確認できませんでした。画面で処理状況を確認してください。", status=False)

    def snapshot(self):
        with self.lock:
            return {**self.state, "logs": list(self.logs)}

    def submit(self, operation, data=None):
        data = data or {}
        with self.lock:
            if operation == "stop":
                self.stop.set()
                if self.state["busy"]:
                    self.state["phase"] = "stopping"
                return
            if operation == "manual":
                if self.state["phase"] not in self.MANUAL_PHASES:
                    raise ValueError("自動操作中は手動操作できません。中止または操作待ちを確認してください。")
                self._put(self.manual, data)
                return
            if self.state["busy"]:
                raise ValueError("別の処理が動作中です。")
            if operation not in {"open", "capture", "run", "close"}:
                raise ValueError("未対応の操作です。")
            if operation == "run":
                data = self.validate_job(data)
                if not self.state["targetReady"]:
                    raise ValueError("先に席種を読み込んでください。")
                names = {s["name"] for s in self.state["seats"]}
                if any(w not in names for w in data["wishes"]):
                    raise ValueError("読み込んだ一覧から希望席を選び直してください。")
                self.state["job"] = uuid.uuid4().hex
            if operation == "capture":
                self.state["targetReady"] = False
            self._put(self.commands, (operation, data))
            self.state["busy"] = True
            self.state["phase"] = "waiting" if operation == "run" else "loading"
            self.stop.clear()

    @staticmethod
    def _put(destination, value):
        try:
            destination.put_nowait(value)
        except queue.Full:
            raise ValueError("操作が混み合っています。少し待ってから操作してください。") from None

    @staticmethod
    def validate_job(data):
        wishes = data.get("wishes")
        if not isinstance(wishes, list) or not 1 <= len(wishes) <= 3 or any(not isinstance(w, str) or not w or len(w)>120 for w in wishes):
            raise ValueError("希望席を1〜3つ選んでください。")
        result = {"wishes": list(dict.fromkeys(wishes))}
        for key, low, high in (("count", 1, 4), ("interval", 10, 86400), ("minutes", 1, 120)):
            value = data.get(key)
            if type(value) is not int or not low <= value <= high:
                raise ValueError("枚数・確認間隔・監視時間を確認してください。")
            result[key] = value
        if type(data.get("watch")) is not bool or data.get("confirmed") is not True:
            raise ValueError("開催日・競馬場と申し込み条件を確認してください。")
        result["watch"] = data["watch"]
        area = data.get("area", "")
        if not isinstance(area, str) or len(area)>120:
            raise ValueError("エリア名を確認してください。")
        result["area"] = area.strip()
        try:
            start = parse_start(data.get("start", ""))
        except (ValueError, TypeError):
            raise ValueError("開始日時は未来の日本時間で指定してください。") from None
        if start - dt.datetime.now(JST) > dt.timedelta(days=1):
            raise ValueError("開始は24時間以内で指定してください。ログイン状態はブラウザで確認してください。")
        result["start"] = start
        return result

    def pump(self):
        if threading.get_ident() != self.owner or self.pumping:
            return
        self.pumping = True
        try:
            for _ in range(4):
                try:
                    data = self.manual.get_nowait()
                except queue.Empty:
                    break
                request_id = data.get('requestId','')
                request_id = request_id if isinstance(request_id,str) and re.fullmatch(r'[A-Za-z0-9-]{1,64}',request_id) else ''
                ok = False
                detail = "入力欄を選び直し、表示中の画面と入力内容を確認してください。"
                try:
                    with self.lock:
                        allowed = self.state["phase"] in self.MANUAL_PHASES
                    if allowed:
                        self._manual(data)
                        ok = True
                        detail = "JRA画面に入力しました。ログインは送信していません。" if data.get('kind')=='login_fill' else "入力欄の操作を反映しました。"
                except Exception as e:
                    self.log("手動操作を実行できませんでした（" + type(e).__name__ + "）。画面を確認してください。",status=False)
                finally:
                    if request_id:
                        with self.lock:self.state['manualResult']={'id':request_id,'ok':ok,'detail':detail}
                    self.manual.task_done()
            if self.browser.driver and time.monotonic()-self.last_frame>1.2:
                try:
                    driver = self.browser.driver
                    windows = self._windows()
                    driver.switch_to.default_content()
                    active_select = None
                    if self.selected_element is not None:
                        active_select = driver.execute_script("""const e=arguments[0];return e.isConnected && !e.disabled?{options:[...e.options].map(o=>o.text),disabled:[...e.options].map(o=>o.disabled || !!o.closest('optgroup:disabled')),selected:e.selectedIndex}:null;""", self.selected_element)
                        if active_select:
                            active_select['token'] = self.select_token
                        else:
                            self.selected_element = None
                    meta = driver.execute_script("return {width:innerWidth,height:innerHeight,url:location.href,loginReady:['loginMail','loginPassword'].every(id=>{const e=document.getElementById(id);return e && e.getClientRects().length>0 && !e.disabled && !e.readOnly})}")
                    image = base64.b64decode(driver.execute_cdp_cmd('Page.captureScreenshot', {'format':'jpeg','quality':65})['data'])
                    host = urlsplit(meta['url']).hostname or ("デモ" if self.demo else "")
                    with self.lock:
                        changed = image != self.frame
                        self.frame = image
                        self.state.update(width=meta['width'], height=meta['height'], host=host, frameAt=time.time(), frame=self.state['frame']+int(changed))
                        self.state['browserWindows'] = windows
                        self.state['browserDiagnostics'] = {'readyState':driver.execute_script('return document.readyState'),'windows':len(windows),'embeddedFrames':len(driver.find_elements('css selector','iframe,frame'))}
                        self.state['formFields'] = self._form_fields() if host in JRA_HOSTS or self.demo or self.state['phase']=='payment' else []
                        self.state['activeSelect'] = active_select
                        self.state['loginReady'] = bool(meta['loginReady'] and (self.demo or host in ('jra-tickets.jp','my.jra-tickets.jp')))
                except Exception:
                    self.selected_element = None
                    with self.lock:self.state['browserDiagnostics']={'error':True}
                self.last_frame = time.monotonic()
        finally:
            self.pumping = False

    def _windows(self):
        driver = self.browser.driver
        handles = driver.window_handles
        old = getattr(self,'window_tokens',{})
        self.window_tokens = {next((k for k,v in old.items() if v==h),uuid.uuid4().hex):h for h in handles}
        try:current = driver.current_window_handle
        except NoSuchWindowException:
            if not handles:return []
            driver.switch_to.window(handles[0]);current=handles[0]
            self.selected_element=None;self.form_targets={}
        return [{'token':token,'label':f'画面 {i+1}','selected':handle==current}
                for i,(token,handle) in enumerate(self.window_tokens.items())]

    def _form_fields(self):
        driver=self.browser.driver
        previous=getattr(self,'form_targets',{})
        result=[];targets={};window=driver.current_window_handle;top_url=driver.current_url
        def visit(path):
            fields,found=self._document_fields(previous,path,window,top_url)
            result.extend(fields);targets.update(found)
            if len(path)>=3:return
            frames=driver.find_elements('css selector','iframe,frame')
            for frame in frames[:12]:
                if not frame.is_displayed():continue
                if not driver.execute_script(FRONT_JS+'return front.contains(arguments[0]) && uncovered(arguments[0]);',frame):continue
                entered=False
                try:
                    driver.switch_to.frame(frame);entered=True
                    # Browser-controlled frame switching; no cross-origin policy override.
                    scheme=driver.execute_script('return location.protocol')
                    if scheme in ('https:','about:') or self.demo:visit(path+[frame])
                except Exception:pass
                finally:
                    if entered:driver.switch_to.parent_frame()
        driver.switch_to.default_content()
        try:visit([])
        finally:driver.switch_to.default_content()
        self.form_targets=targets
        return result

    def _document_fields(self, previous, frame_path, window, top_url):
        driver = self.browser.driver
        fields = driver.execute_script(FRONT_JS + r"""
        const selector='input,textarea,select,button,a[href],label,[role="button"],[onclick],.btn_01,.btn_02';
        const visible=e=>!!e.getClientRects().length || (['checkbox','radio'].includes(e.type) && [...(e.labels||[])].some(l=>l.getClientRects().length));
        const disabled=e=>!!e.closest('[disabled],[aria-disabled="true"],.disabled,.aspNetDisabled') || getComputedStyle(e).pointerEvents==='none';
        return [...new Set([...front.querySelectorAll(selector),...front.querySelectorAll('p,span,div')])].filter(e=>{
          if(!e.matches(selector) && (e.innerText||'').replace(/\s/g,'')!=='次へ進む')return false;
          if(!visible(e)||disabled(e)||e.readOnly||e.multiple)return false;
          const visual=e.getClientRects().length?e:[...(e.labels||[])].find(l=>l.getClientRects().length);
          if(!visual||!uncovered(visual))return false;
          if(e.tagName==='INPUT')return ['text','password','email','tel','number','search','url','date','datetime-local','checkbox','radio','submit','button'].includes(e.type);
          if(e.tagName==='LABEL' && (e.control || e.querySelector('input,select,textarea')))return false;
          if(!['SELECT','TEXTAREA'].includes(e.tagName) && e.querySelector(selector))return false;
          return true;
        }).slice(0,250).map(e=>{
          const type=e.tagName==='SELECT'?'select':e.type==='password'?'password':['checkbox','radio'].includes(e.type)?e.type:
            e.tagName==='TEXTAREA'||(e.tagName==='INPUT'&&!['submit','button'].includes(e.type))?'text':'action';
          return {element:!e.getClientRects().length&&['checkbox','radio'].includes(type)?[...e.labels].find(l=>l.getClientRects().length):e,label:([...(e.labels||[])].map(l=>l.innerText).join(' ') || e.getAttribute('aria-label') || type==='action'&&e.innerText || e.getAttribute('value')&&type==='action'&&e.getAttribute('value') || e.placeholder || e.name || e.querySelector('img')?.alt || '操作項目').slice(0,160),type,
            checked:['checkbox','radio'].includes(type)?e.checked:null,
            options:type==='select'?[...e.options].map(o=>({text:o.text,disabled:o.disabled||!!o.closest('optgroup:disabled')})):null,
            selected:type==='select'?e.selectedIndex:null};
        });""")
        old = previous
        targets = {}; result = []
        for item in fields:
            element = item.pop('element')
            token = next((k for k,v in old.items() if v[0] == element and v[1] == top_url and v[3] == window), uuid.uuid4().hex)
            targets[token] = (element, top_url, frame_path, window)
            item['sourceHost']=driver.execute_script('return location.hostname') if frame_path else ''
            result.append(dict(item, token=token))
        return result, targets

    def _manual(self, data):
        try:return self._manual_impl(data)
        finally:
            if self.browser.driver:
                try:self.browser.driver.switch_to.default_content()
                except NoSuchWindowException:self._windows()

    def _manual_impl(self, data):
        driver = self.browser.driver
        if not driver:
            raise ValueError("ブラウザが開いていません。")
        url = driver.current_url
        kind = data.get("kind")
        if not (kind in ("window","inspect") and url=="about:blank" or urlsplit(url).scheme == "https" or self.demo and url.split('#')[0] == (Path(__file__).parent/"static"/"demo.html").as_uri()):
            raise ValueError("安全なブラウザ画面ではないため操作できません。")
        if kind == 'inspect':
            # Refresh our view only: never reload or resubmit the payment page.
            self._windows()
        elif kind == "window":
            self._windows()
            handle=self.window_tokens.get(data.get('token'))
            if handle is None:raise ValueError("画面が閉じられています。")
            driver.switch_to.window(handle)
            driver.switch_to.default_content()
            self.selected_element=None;self.form_targets={}
        elif kind == "field":
            parsed = urlsplit(url)
            if not self.demo and (parsed.scheme != 'https' or (parsed.hostname not in JRA_HOSTS and self.state.get('phase')!='payment')):
                raise ValueError("JRAの画面だけに入力できます。")
            target = getattr(self, 'form_targets', {}).get(data.get('token'))
            try:
                if target and target[3] == driver.current_window_handle:
                    for frame in target[2]:driver.switch_to.frame(frame)
                else:target=None
                connected = bool(target and target[1] == url and driver.execute_script('return arguments[0].isConnected', target[0]))
            except StaleElementReferenceException:
                connected = False
            if not connected:
                raise ValueError("画面が変わりました。入力欄を読み込み直してください。")
            element = target[0]
            if not element.is_displayed() or not element.is_enabled() or not driver.execute_script(FRONT_JS + 'return front.contains(arguments[0]) && uncovered(arguments[0]);',element):
                raise ValueError("この欄は操作できません。")
            if element.tag_name == 'select':
                from selenium.webdriver.support.ui import Select
                select = Select(element); index = data.get('index')
                if type(index) is not int or not 0 <= index < len(select.options) or not select.options[index].is_enabled():
                    raise ValueError("この項目は選択できません。")
                select.select_by_index(index)
            elif data.get('activate') is True:
                if element.tag_name in ('input','textarea') and element.get_attribute('type') not in ('checkbox','radio','submit','button'):
                    raise ValueError("入力欄は文字を入力して反映してください。")
                if not driver.execute_script("return !arguments[0].closest('[disabled],[aria-disabled=\"true\"],.disabled,.aspNetDisabled')", element):
                    raise ValueError("この項目は無効です。")
                element.click()
            else:
                value = data.get('value')
                if not isinstance(value,str) or len(value)>1000 or any(ord(c)<32 or 0xE000<=ord(c)<=0xF8FF for c in value):
                    raise ValueError("入力内容を確認してください。")
                self._replace_input(element,value)
        elif kind == "tap":
            x, y = data.get("x"), data.get("y")
            if type(x) not in (int,float) or type(y) not in (int,float) or not (0 <= x < self.state["width"] and 0 <= y < self.state["height"]):
                raise ValueError("タップ位置が画面外です。")
            self.selected_element = driver.execute_script("const e=document.elementFromPoint(arguments[0],arguments[1]);return e && e.closest('select:not(:disabled)')", x, y)
            self.select_token = uuid.uuid4().hex
            if self.selected_element is not None:
                driver.execute_script("arguments[0].focus()", self.selected_element)
                self.last_frame = 0
                return
            for event in ("mousePressed", "mouseReleased"):
                driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type":event,"x":x,"y":y,"button":"left","clickCount":1})
        elif kind in ("login_fill", "login_clear"):
            parsed = urlsplit(url)
            if not self.demo and (parsed.scheme!='https' or parsed.hostname not in JRA_HOSTS or parsed.port not in (None,443)):
                raise ValueError("JRAのログイン画面だけに入力できます。")
            if self.browser.congested():
                raise ValueError("混雑画面を確認してください。")
            fields = [self.browser.visible('#loginMail'),self.browser.visible('#loginPassword')]
            if any(len(items)!=1 for items in fields) or fields[1][0].get_attribute('type')!='password':
                raise ValueError("JRAのログイン欄を開いてください。")
            values = [data.get('username'),data.get('password')] if kind=='login_fill' else ['', '']
            if any(not isinstance(v,str) or len(v)>limit or any(ord(c)<32 or 0xE000<=ord(c)<=0xF8FF for c in v) for v,limit in zip(values,[254,32])):
                raise ValueError("ID・パスワードに改行を含めず入力してください。")
            if kind=='login_fill' and any(not v for v in values):
                raise ValueError("IDとパスワードの両方を入力してください。")
            for items,value in zip(fields,values):
                self._replace_input(items[0],value)
            self.selected_element = None
        elif kind == "clear":
            self._replace_input(driver.switch_to.active_element,'')
        elif kind == "type":
            value = data.get("value", "")
            if not isinstance(value,str) or len(value)>1000:
                raise ValueError("入力文字が長すぎます。")
            self._replace_input(driver.switch_to.active_element,value)
        elif kind == "key":
            keys = {"Enter":Keys.ENTER,"Tab":Keys.TAB,"Backspace":Keys.BACKSPACE,"Escape":Keys.ESCAPE}
            if data.get("key") not in keys:
                raise ValueError("未対応のキーです。")
            driver.switch_to.active_element.send_keys(keys[data["key"]])
        elif kind == "select":
            from selenium.webdriver.support.ui import Select
            index = data.get("index")
            element = self.selected_element
            if element is None or data.get('token') != self.select_token or not driver.execute_script('return arguments[0].isConnected && !arguments[0].disabled', element) or type(index) is not int or index < 0:
                raise ValueError("先に選択欄をタップしてください。")
            options = Select(element)
            if index >= len(options.options):
                raise ValueError("選択肢が変わりました。画面を確認してください。")
            if not options.options[index].is_enabled():
                raise ValueError("この項目は選択できません。")
            options.select_by_index(index)
            self.selected_element = None
        elif kind == "scroll":
            delta = data.get("delta")
            if type(delta) is not int or abs(delta)>1000:
                raise ValueError("スクロール量が大きすぎます。")
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type":"mouseWheel","x":190,"y":400,"deltaY":delta,"deltaX":0})
        else:
            raise ValueError("未対応の手動操作です。")
        self.last_frame = 0

    @staticmethod
    def _replace_input(element,value):
        if element.tag_name not in ('input','textarea') or (element.tag_name=='input' and element.get_attribute('type') not in ('text','password','email','search','tel','url','number')):
            raise ValueError("先に入力欄を選択してください。")
        if not element.is_displayed() or not element.is_enabled() or element.get_property('readOnly'):
            raise ValueError("この入力欄は編集できません。")
        element.clear()
        element.send_keys(Keys.CONTROL,'a',Keys.NULL,Keys.BACKSPACE)
        if value:element.send_keys(value)

    def _loop(self):
        self.owner = threading.get_ident()
        while not self.shutdown.is_set():
            self.pump()
            try:
                operation, data = self.commands.get(timeout=.15)
            except queue.Empty:
                continue
            try:
                if operation == "open":
                    self.browser.open()
                    with self.lock:
                        self.state["phase"] = "ready"
                elif operation == "capture":
                    seats = self.browser.capture()
                    with self.lock:
                        self.state.update(seats=seats, targetReady=True, phase="ready")
                    self.log(f"{len(seats)}種類の席種を読み込みました。開催日と競馬場はブラウザ画面で確認してください。")
                elif operation == "close":
                    if self.browser.driver:
                        self.browser.driver.quit()
                        self.browser.driver = None
                    with self.lock:
                        self.state.update(phase="idle", targetReady=False, seats=[], frame=0, frameAt=0, host="", formFields=[], loginReady=False, manualResult=None)
                        self.frame = b""
                elif operation == "run":
                    self.log(f"{data['start']:%Y-%m-%d %H:%M:%S}（日本時間）まで待機します。処理はサーバーで継続します。")
                    self.browser.run(data["wishes"], data["count"], data["area"], data["start"], data["watch"], data["interval"], data["minutes"])
            except Unavailable as e:
                self.log(str(e))
                with self.lock:
                    self.state["phase"] = "unavailable"
                    self.state["targetReady"] = False
            except Halt as e:
                self.log(str(e))
                with self.lock:
                    if self.state["phase"] != "attention":
                        self.state["phase"] = "stopped"
            except Exception as e:
                self.log(f"処理を停止しました（{type(e).__name__}）。ブラウザ画面を確認してください。自動再送信は行いません。")
                with self.lock:
                    self.state["phase"] = "error"
            finally:
                with self.lock:
                    self.state["busy"] = False
                    phase, job = self.state["phase"], self.state["job"]
                if operation == "run" and phase not in ("payment", "attention"):
                    self._notify(job, "unavailable" if phase == "unavailable" else "stopped")
                self.commands.task_done()
        if self.browser.driver:
            try:
                self.browser.driver.quit()
            except Exception:
                pass

    def close(self):
        self.stop.set()
        self.shutdown.set()
        self.thread.join(timeout=35)
