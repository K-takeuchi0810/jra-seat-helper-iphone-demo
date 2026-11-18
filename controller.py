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
import traceback
from urllib.parse import urlsplit

from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import StaleElementReferenceException, NoSuchWindowException
from engine import Browser, Halt, Unavailable, JST, JRA_HOSTS, FRONT_JS, parse_start, safe_service_codes, safe_service_details, booking_target, RACECOURSES


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
        self.seat_catalog={}
        try:
            seeded=json.loads((Path(__file__).parent/'static'/'seat-catalog.json').read_text(encoding='utf-8')).get('courses',{})
            for course,entry in seeded.items():
                names=entry.get('names',[])
                if course in RACECOURSES and isinstance(names,list) and names and len(names)<=100 and all(isinstance(n,str) and 0<len(n)<=120 for n in names):
                    self.seat_catalog[course]={"names":list(dict.fromkeys(names)),"observedAt":entry.get('observedAt',''),"eventDate":entry.get('eventDate','')}
        except (OSError,ValueError,TypeError,AttributeError):pass
        self.state = {"phase": "idle", "busy": False, "seats": [], "targetReady": False, "booking": None, "catalog": [], "catalogAt": 0, "seatCatalog": self.seat_catalog,
                      "demo": demo, "job": "", "activeJob": None, "frame": 0, "frameAt": 0,
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
            if name == "reserving":
                self.state["phase"] = "running"
                return
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
            if operation not in {"open", "capture", "catalog", "prepare", "run", "close"}:
                raise ValueError("未対応の操作です。")
            if operation == "run":
                data = self.validate_job(data)
                if data.get("booking"):
                    if not self.browser.driver:
                        raise ValueError("先にブラウザを開いてJRAにログインしてください。条件は入力したまま保持できます。")
                else:
                    if not self.state["targetReady"]:
                        raise ValueError("先に席種を読み込んでください。")
                    names = {s["name"] for s in self.state["seats"]}
                    if any(w not in names for w in data["wishes"]):
                        raise ValueError("読み込んだ一覧から希望席を選び直してください。")
                self.state["job"] = uuid.uuid4().hex
                self.state["activeJob"]={"booking":data.get("booking"),"wishes":list(data["wishes"]),"count":data["count"],"area":data["area"],"startMode":data.get("startMode","scheduled"),"start":data["start"].strftime("%Y-%m-%d %H:%M:%S"),"watch":data["watch"],"interval":data["interval"],"minutes":data["minutes"]}
            if operation == "prepare":
                data = {"booking": booking_target(data.get("booking"))}
            if operation in ("capture", "prepare", "catalog"):
                self.state["targetReady"] = False
            self._put(self.commands, (operation, data))
            self.state["busy"] = True
            self.state["detail"]={"open":"ブラウザを起動しています。","catalog":"サイトの開催一覧を取得しています。","prepare":"指定開催日の席種を取得しています。","capture":"表示中の席種を取得しています。","run":"指定条件を確認しています。","close":"ブラウザを終了しています。"}[operation]
            self.state["phase"] = "waiting" if operation == "run" and data.get("startMode") != "now" else "loading"
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
        if "booking" in data:
            result["booking"] = booking_target(data["booking"])
        area = data.get("area", "")
        if not isinstance(area, str) or len(area)>120:
            raise ValueError("エリア名を確認してください。")
        result["area"] = area.strip()
        start_mode=data.get("startMode", "scheduled")
        if start_mode not in ("now", "scheduled"):
            raise ValueError("開始方法を選択してください。")
        if start_mode == "now":
            start=dt.datetime.now(JST)
        else:
            try:
                start = parse_start(data.get("start", ""))
            except (ValueError, TypeError):
                raise ValueError("開始日時は未来の日本時間で指定してください。") from None
        result["startMode"]=start_mode
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
                        detail = {'login_fill':'JRA画面に入力しました。ログインは送信していません。', 'inspect':'画面の状態だけ更新しました。認証や決済は送信していません。', 'window':'表示する画面を切り替えました。'}.get(data.get('kind'),'入力欄の操作を反映しました。')
                except Exception as e:
                    self.log("手動操作を実行できませんでした（" + type(e).__name__ + "）。画面を確認してください。",status=False)
                finally:
                    if request_id:
                        with self.lock:self.state['manualResult']={'id':request_id,'ok':ok,'detail':detail}
                    self.manual.task_done()
            if self.browser.driver and self.state["phase"] != "running" and time.monotonic()-self.last_frame>1.2:
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
                        self.state['browserDiagnostics'] = {'readyState':driver.execute_script('return document.readyState'),'windows':len(windows),'embeddedFrames':len(driver.find_elements('css selector','iframe,frame')),'displayMode':getattr(self.browser,'display_mode','unknown')}
                        self.state['formFields'] = self._form_fields() if self.demo or (urlsplit(meta['url']).scheme=='https' and self.state['phase'] in self.MANUAL_PHASES) else []
                        self.state['browserDiagnostics']['frames'] = getattr(self,'frame_diagnostics',[])
                        self.state['browserDiagnostics']['networkErrors'] = self.browser.network_diagnostics()
                        self.state['browserDiagnostics']['scriptErrors'] = self.browser.script_diagnostics()
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
        self.frame_diagnostics=[]
        def visit(path, expose=True):
            fields,found=self._document_fields(previous,path,window,top_url)
            if expose:result.extend(fields);targets.update(found)
            if len(path)>=3:return
            frames=driver.find_elements('css selector','iframe,frame')
            for frame in frames[:12]:
                visible=frame.is_displayed()
                interactive=expose and visible and driver.execute_script(FRONT_JS+'return front.contains(arguments[0]) && uncovered(arguments[0]);',frame)
                entered=False
                source_host=driver.execute_script("try{return new URL(arguments[0].src,location.href).hostname}catch{return ''}",frame)
                diagnostic={'visible':visible,'sourceHost':source_host,'depth':len(path)+1,'readyState':'unknown','actualHost':''}
                self.frame_diagnostics.append(diagnostic)
                try:
                    driver.switch_to.frame(frame);entered=True
                    # Browser-controlled frame switching; no cross-origin policy override.
                    meta=driver.execute_script('return {protocol:location.protocol,host:location.hostname,readyState:document.readyState}')
                    scheme=meta['protocol'];diagnostic.update(actualHost=meta['host'],readyState=meta['readyState'])
                    # Existing error documents can outlive their network log entry.
                    # Read only a bounded JSON document and export whitelisted codes.
                    if meta['host'] in ('as-pgx.ps.paysec-3dssvgw.com','pgx-auth-api.ps.paysec-3dssvgw.com') or self.demo:
                        error_document=driver.execute_script("const t=(document.body?.innerText||'').trim();return t.length<=65536 && /^[{[]/.test(t)?t:''")
                        diagnostic['serviceCodes']=safe_service_codes(error_document)
                        if error_document:diagnostic['responseDetails']=safe_service_details(error_document)
                    if scheme in ('https:','about:') or self.demo:visit(path+[frame],interactive)
                except Exception:diagnostic['error']=True
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
          if(!visual)return false;const r=visual.getBoundingClientRect();const inViewport=r.bottom>0&&r.top<innerHeight&&r.right>0&&r.left<innerWidth;if(inViewport&&!uncovered(visual))return false;
          if(e.tagName==='INPUT')return ['text','password','email','tel','number','search','url','date','datetime-local','checkbox','radio','submit','button'].includes(e.type);
          if(e.tagName==='LABEL' && (e.control || e.querySelector('input,select,textarea') || !e.matches('[onclick],[role=button],.btn_01,.btn_02')))return false;
          if(!['SELECT','TEXTAREA'].includes(e.tagName) && e.querySelector(selector))return false;
          return true;
        }).slice(0,250).map(e=>{
          const type=e.tagName==='SELECT'?'select':e.type==='password'?'password':['checkbox','radio'].includes(e.type)?e.type:
            e.tagName==='TEXTAREA'||(e.tagName==='INPUT'&&!['submit','button'].includes(e.type))?'text':'action';
          const term=e.closest('dd')?.previousElementSibling;const caption=term?.tagName==='DT'?term.innerText.split('\n')[0]:'';const purpose=e.id==='loginMail'?'jra-login-username':e.id==='loginPassword'?'jra-login-password':'';
          const group=e.closest('header,footer,[role=banner],[role=contentinfo]')?'navigation':'page';const rawAction=(e.innerText||'').replace(/[\ue000-\uf8ff\s]/g,'');const actionLabel=rawAction==='ログイン'?(e.closest('main')?'ログインを送信':'ログイン画面へ'):'';
          return {purpose,group,element:!e.getClientRects().length&&['checkbox','radio'].includes(type)?[...e.labels].find(l=>l.getClientRects().length):e,label:([...(e.labels||[])].map(l=>l.innerText).join(' ') || e.getAttribute('aria-label') || purpose==='jra-login-username'&&'JRAの会員番号・メールアドレス' || purpose==='jra-login-password'&&'JRAのパスワード' || type!=='action'&&caption || type==='action'&&actionLabel || type==='action'&&e.innerText || e.getAttribute('value')&&type==='action'&&e.getAttribute('value') || e.placeholder || e.name || e.querySelector('img')?.alt || '操作項目').slice(0,160),type,
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
            if not self.demo and (parsed.scheme != 'https' or self.state.get('phase') not in self.MANUAL_PHASES):
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
            if element.is_displayed() and element.is_enabled():
                driver.execute_script("arguments[0].scrollIntoView({block:'center',inline:'nearest'})",element)
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
                elif operation == "catalog":
                    catalog=self.browser.booking_catalog()
                    with self.lock:
                        self.state.update(catalog=catalog, catalogAt=time.time(), booking=None, targetReady=False, seats=[], phase="ready")
                    self.log("サイトで現在選択できる競馬場・開催日を取得しました。開催を選ぶと席種を取得できます。")
                elif operation in ("capture", "prepare"):
                    if operation == "prepare":
                        seats = self.browser.prepare_booking(data["booking"])
                    else:
                        self.browser.booking = None
                        seats = self.browser.capture()
                    with self.lock:
                        self.state.update(seats=seats, targetReady=True, phase="ready", booking=getattr(self.browser,"booking",None))
                        target=self.state["booking"]
                        if target:
                            self.seat_catalog={**self.seat_catalog,target['course']:{"names":list(dict.fromkeys(s['name'] for s in seats)),"observedAt":dt.datetime.now(JST).isoformat(),"eventDate":target['date']}}
                            self.state["seatCatalog"]=self.seat_catalog
                    target = self.state["booking"]
                    label = f"{target['date']}・{target['course']}競馬場" if target else "対象ページ"
                    self.log(f"{label}の席種{len(seats)}種類を読み込みました。まだ申し込みは送信していません。")
                elif operation == "close":
                    if self.browser.driver:
                        self.browser.driver.quit()
                        self.browser.driver = None
                    with self.lock:
                        self.state.update(phase="idle", targetReady=False, booking=None, seats=[], frame=0, frameAt=0, host="", formFields=[], loginReady=False, manualResult=None)
                        self.frame = b""
                    self.log("ブラウザを終了しました。再開する場合はホームから開いてください。")
                elif operation == "run":
                    self.log("指定条件で今すぐ開始します。" if data.get("startMode")=="now" else f"{data['start']:%Y-%m-%d %H:%M:%S}（日本時間）まで待機します。処理はサーバーで継続します。")
                    args=(data["wishes"], data["count"], data["area"], data["start"], data["watch"], data["interval"], data["minutes"])
                    if data.get("booking"):
                        with self.lock:self.state["booking"]=data["booking"]
                        self.log(f"{data['booking']['date']}・{data['booking']['course']}競馬場／{' → '.join(data['wishes'])}／{data['count']}席で準備します。")
                        self.browser.run(*args, booking=data["booking"])
                    else:
                        self.browser.run(*args)
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
                frames=[{'file':Path(f.filename).name,'function':f.name,'line':f.lineno} for f in traceback.extract_tb(e.__traceback__) if Path(f.filename).name in ('engine.py','controller.py')]
                stage=getattr(self.browser,'stage','画面操作')
                print(json.dumps({'event':'browser_failure','type':type(e).__name__,'stage':stage,'frames':frames},ensure_ascii=False),flush=True)
                self.log(f"処理を停止しました（{type(e).__name__}／{stage}）。ブラウザ画面を確認してください。自動再送信は行いません。")
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
