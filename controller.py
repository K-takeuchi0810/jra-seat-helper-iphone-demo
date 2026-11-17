"""A single owner thread serializes browser automation and human input."""
from __future__ import annotations

from collections import deque
import datetime as dt
import json
from pathlib import Path
import queue
import base64
import threading
import time
import uuid
from urllib.parse import urlsplit

from selenium.webdriver.common.keys import Keys
from engine import Browser, Halt, Unavailable, JST, parse_start


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
        self.logs = deque(maxlen=100)
        self.state = {"phase": "idle", "busy": False, "seats": [], "targetReady": False,
                      "demo": demo, "job": "", "frame": 0, "frameAt": 0,
                      "width": 390, "height": 760, "host": "", "notification": "未設定", "detail": "", "activeSelect": None}
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
        for key, low, high in (("count", 1, 4), ("interval", 60, 86400), ("minutes", 1, 120)):
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
                try:
                    with self.lock:
                        allowed = self.state["phase"] in self.MANUAL_PHASES
                    if allowed:
                        self._manual(data)
                except Exception as e:
                    self.log("手動操作を実行できませんでした（" + type(e).__name__ + "）。画面を確認してください。")
                finally:
                    self.manual.task_done()
            if self.browser.driver and time.monotonic()-self.last_frame>1.2:
                try:
                    driver = self.browser.driver
                    active_select = None
                    if self.selected_element is not None:
                        active_select = driver.execute_script("""const e=arguments[0];return e.isConnected && !e.disabled?{options:[...e.options].map(o=>o.text),disabled:[...e.options].map(o=>o.disabled || !!o.closest('optgroup:disabled')),selected:e.selectedIndex}:null;""", self.selected_element)
                        if active_select:
                            active_select['token'] = self.select_token
                        else:
                            self.selected_element = None
                    meta = driver.execute_script("return {width:innerWidth,height:innerHeight,url:location.href}")
                    image = base64.b64decode(driver.execute_cdp_cmd('Page.captureScreenshot', {'format':'jpeg','quality':65})['data'])
                    host = urlsplit(meta['url']).hostname or ("デモ" if self.demo else "")
                    with self.lock:
                        changed = image != self.frame
                        self.frame = image
                        self.state.update(width=meta['width'], height=meta['height'], host=host, frameAt=time.time(), frame=self.state['frame']+int(changed))
                        self.state['activeSelect'] = active_select
                except Exception:
                    self.selected_element = None
                self.last_frame = time.monotonic()
        finally:
            self.pumping = False

    def _manual(self, data):
        driver = self.browser.driver
        if not driver:
            raise ValueError("ブラウザが開いていません。")
        url = driver.current_url
        if not (urlsplit(url).scheme == "https" or self.demo and url.split('#')[0] == (Path(__file__).parent/"static"/"demo.html").as_uri()):
            raise ValueError("安全なブラウザ画面ではないため操作できません。")
        kind = data.get("kind")
        if kind == "tap":
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
        elif kind == "type":
            value = data.get("value", "")
            if not isinstance(value,str) or len(value)>1000:
                raise ValueError("入力文字が長すぎます。")
            element = driver.switch_to.active_element
            if element.tag_name not in ("input", "textarea"):
                raise ValueError("先に入力欄をタップしてください。")
            element.clear()
            element.send_keys(value)
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
                        self.state.update(phase="idle", targetReady=False, seats=[], frame=0, frameAt=0, host="")
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
