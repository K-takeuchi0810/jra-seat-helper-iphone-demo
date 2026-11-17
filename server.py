"""Single-user phone app. Public deployment must be behind HTTPS."""
from __future__ import annotations

import base64
from collections import defaultdict, deque
import hashlib
import hmac
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlsplit

from controller import Controller
from push_service import PushService
from device_auth import DeviceAuth, COOKIE as DEVICE_COOKIE, LIFETIME as DEVICE_LIFETIME

HERE = Path(__file__).parent
STATIC = HERE / "static"
STATIC_FILES = {"/":"index.html", "/app.js":"app.js", "/styles.css":"styles.css",
                "/device-auth.js":"device-auth.js", "/sw.js":"sw.js", "/icon.svg":"icon.svg", "/icon-192.png":"icon-192.png", "/icon-512.png":"icon-512.png", "/manifest.webmanifest":"manifest.webmanifest"}


class PhoneApp:
    def __init__(self, secret, directory, demo=True, secure=True, profile="local", controller_factory=Controller, push_factory=PushService):
        if len(secret)<32:
            raise ValueError("32文字以上の専用ログイン鍵を設定してください。JRAのパスワードは使用しないでください。")
        self.secret = secret
        self.secure = secure
        self.profile = profile
        self.lock = threading.RLock()
        self.attempts = defaultdict(deque)
        self.sessions = {}
        self.devices = DeviceAuth(secret)
        self.push_status = {"accepted":False,"received":False,"testId":"","detail":"通知は未設定です。"}
        self.push = push_factory(secret, directory, self.on_push_result)
        self.controller = controller_factory(demo=demo, notify=self.push.enqueue)

    def on_push_result(self, job, event, accepted, detail):
        with self.lock:
            self.push_status.update(accepted=accepted, detail=detail)
        self.controller.log(detail,status=False)

    def cookie(self):
        expiry, nonce = int(time.time())+12*3600, secrets.token_urlsafe(16)
        value = f"{expiry}.{nonce}"
        signature = hmac.new(self.secret.encode(), value.encode(), hashlib.sha256).hexdigest()
        with self.lock:
            self.sessions = {k:v for k,v in self.sessions.items() if v>time.time()}
            self.sessions[nonce] = expiry
        return value+"."+signature

    def authenticated(self, header):
        try:
            jar = cookies.SimpleCookie(header)
            value = jar["jra_phone_session"].value
            expiry, nonce, signature = value.split(".")
            expected = hmac.new(self.secret.encode(), f"{expiry}.{nonce}".encode(), hashlib.sha256).hexdigest()
            with self.lock:
                active = self.sessions.get(nonce, 0)>time.time()
            return active and int(expiry)>time.time() and hmac.compare_digest(expected, signature)
        except (KeyError, ValueError, cookies.CookieError):
            return False

    def revoke(self, header):
        try:
            jar = cookies.SimpleCookie(header)
            nonce = jar["jra_phone_session"].value.split(".")[1]
            with self.lock:
                self.sessions.pop(nonce, None)
        except (KeyError, IndexError, cookies.CookieError):
            pass

    def login_allowed(self, peer):
        with self.lock:
            now = time.monotonic()
            recent = self.attempts[peer]
            while recent and recent[0]<now-60:
                recent.popleft()
            if len(recent)>=6:
                return False
            recent.append(now)
            if len(self.attempts)>1024:
                self.attempts = defaultdict(deque, {peer:recent})
            return True

    def close(self):
        self.controller.close()
        self.push.close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SeatPhone"

    def log_message(self, fmt, *args):
        # No request URLs, cookies, form inputs, or screenshots in service logs.
        pass

    @property
    def application(self):
        return self.server.application

    def reply(self, status, value, mime="application/json; charset=utf-8", headers=None):
        body = value if isinstance(value, bytes) else json.dumps(value,ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for key, val in (headers or {}).items():
            for item in (val if isinstance(val,list) else [val]):
                self.send_header(key, item)
        self.end_headers()
        self.wfile.write(body)

    def session_header(self):
        suffix = "; Secure" if self.application.secure else ""
        return "jra_phone_session="+self.application.cookie()+"; Path=/; HttpOnly; SameSite=Strict; Max-Age=43200"+suffix

    def device_header(self,value="",age=0):
        suffix = "; Secure" if self.application.secure else ""
        return DEVICE_COOKIE+"="+value+"; Path=/; HttpOnly; SameSite=Strict; Max-Age="+str(age)+suffix

    def require_auth(self):
        if not self.application.authenticated(self.headers.get("Cookie", "")):
            self.reply(401,{"error":"ログインしてください。"})
            return False
        return True

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/health":
            self.reply(200,{"ok":True})
        elif path in STATIC_FILES:
            f = STATIC/STATIC_FILES[path]
            mime = "application/manifest+json" if path.endswith(".webmanifest") else mimetypes.guess_type(str(f))[0] or "application/octet-stream"
            self.reply(200,f.read_bytes(),mime)
        elif path == "/api/state" and self.require_auth():
            state = self.application.controller.snapshot()
            with self.application.lock:
                push = dict(self.application.push_status)
            state.update(push=push,pushKey=self.application.push.public_key,profile=self.application.profile,
                         subscribers=len(self.application.push.subscriptions))
            self.reply(200,state)
        elif path == "/api/frame" and self.require_auth():
            with self.application.controller.lock:
                image = self.application.controller.frame
            self.reply(200,image,"image/jpeg") if image else self.reply(404,{"error":"画面を読み込み中です。"})
        elif path not in ("/api/state", "/api/frame"):
            self.reply(404,{"error":"ページが見つかりません。"})

    def do_POST(self):
        path = urlsplit(self.path).path
        if self.headers.get("X-Seat-Request") != "1" or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            self.reply(403,{"error":"この操作はアプリから行ってください。"})
            self.close_connection = True
            return
        expected = ("https" if self.application.secure else "http")+"://"+self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if origin and origin.rstrip("/") != expected:
            self.reply(403,{"error":"別のサイトからの操作は受け付けません。"})
            self.close_connection = True
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0<length<=16000:
                raise ValueError()
            data = json.loads(self.rfile.read(length))
            if not isinstance(data,dict):
                raise ValueError()
        except (ValueError,UnicodeError):
            self.reply(400,{"error":"入力データを確認してください。"})
            self.close_connection = True
            return
        app = self.application
        if path == "/api/login":
            if not app.login_allowed(self.client_address[0]):
                self.reply(429,{"error":"ログインを少し待ってからやり直してください。"})
            elif isinstance(data.get("secret"),str) and hmac.compare_digest(data["secret"].encode(),app.secret.encode()):
                self.reply(200,{"ok":True},headers={"Set-Cookie":self.session_header()})
            else:
                self.reply(401,{"error":"ログイン鍵が一致しません。"})
            return
        if path in ("/api/device/challenge", "/api/device/login"):
            try:
                if path.endswith('/challenge'):
                    if not app.login_allowed(self.client_address[0]):
                        self.reply(429,{"error":"少し待ってからログインしてください。"})
                    else:
                        self.reply(200,app.devices.challenge(self.headers.get('Cookie',''),expected))
                elif app.devices.authenticate(self.headers.get('Cookie',''),data):
                    self.reply(200,{"ok":True},headers={"Set-Cookie":self.session_header()})
                else:
                    self.reply(401,{"error":"ログイン鍵で接続し直してください。"})
            except Exception:
                self.reply(401,{"error":"ログイン鍵で接続し直してください。"})
            return
        if not self.require_auth():
            return
        try:
            if path == "/api/device/register":
                if not isinstance(data.get('secret'),str) or not hmac.compare_digest(data['secret'].encode(),app.secret.encode()):
                    self.reply(401,{"error":"端末の登録にはログイン鍵の確認が必要です。"})
                    return
                value,expiry=app.devices.issue(data.get('publicKey'))
                self.reply(200,{"ok":True,"expiresAt":expiry},headers={"Set-Cookie":self.device_header(value,DEVICE_LIFETIME)})
            elif path == "/api/device/forget":
                app.devices.revoke(self.headers.get('Cookie',''))
                self.reply(200,{"ok":True},headers={"Set-Cookie":self.device_header()})
            elif path == "/api/logout":
                app.revoke(self.headers.get("Cookie", ""))
                app.devices.revoke(self.headers.get("Cookie", ""))
                self.reply(200,{"ok":True},headers={"Set-Cookie":["jra_phone_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0",self.device_header()]})
            elif path.startswith("/api/browser/"):
                operation = path.removeprefix("/api/browser/")
                app.controller.submit(operation,data)
                self.reply(202,{"accepted":True})
            elif path == "/api/run":
                app.controller.submit("run",data)
                self.reply(202,{"accepted":True})
            elif path == "/api/stop":
                app.controller.submit("stop")
                self.reply(202,{"accepted":True})
            elif path == "/api/push/subscribe":
                app.push.subscribe(data.get("subscription",{}),expected)
                self.reply(200,{"ok":True})
            elif path == "/api/push/test":
                test_id = "test-"+secrets.token_hex(12)
                with app.lock:
                    app.push_status.update(testId=test_id,received=False,accepted=False,detail="通知テストを送信中です。")
                app.push.enqueue(test_id,"test")
                self.reply(202,{"testId":test_id})
            elif path == "/api/push/received":
                with app.lock:
                    if not data.get("testId") or data["testId"] != app.push_status["testId"]:
                        raise ValueError("通知テストを先に行ってください。")
                    app.push_status["received"] = True
                self.reply(200,{"ok":True})
            else:
                self.reply(404,{"error":"操作が見つかりません。"})
        except ValueError as e:
            self.reply(400,{"error":str(e)})
        except Exception:
            self.reply(500,{"error":"操作を完了できませんでした。状況を確認し、自動再送信はしないでください。"})


def serve(secret, directory, host="127.0.0.1", port=8080, demo=True, secure=True, profile="local"):
    application = PhoneApp(secret,directory,demo,secure,profile)
    server = ThreadingHTTPServer((host,port),Handler)
    server.daemon_threads = True
    server.application = application
    try:
        server.serve_forever()
    finally:
        server.server_close()
        application.close()


if __name__ == "__main__":
    serve(os.environ.get("APP_LOGIN_SECRET", ""),os.environ.get("APP_DATA_DIR",str(HERE/"data")),
          os.environ.get("HOST","127.0.0.1"),int(os.environ.get("PORT","8080")),
          demo=os.environ.get("DEMO_ONLY","1")!="0",secure=os.environ.get("SECURE_COOKIES","1")!="0",
          profile=os.environ.get("HOSTING_PROFILE","local"))
