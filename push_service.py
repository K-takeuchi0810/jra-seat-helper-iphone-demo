"""Web Push support. Push endpoints receive only fixed, non-sensitive notices."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import threading
from urllib.parse import urlsplit

HERE = Path(__file__).parent
MESSAGES = {
    "payment_ready": ("席の申し込みが進みました", "支払い画面を検出しました。アプリを開いて取り置きの成立・期限を確認してください。購入は完了していません。"),
    "attention_required": ("操作の確認待ちです", "注意事項または追加確認が表示されました。席取りサポートのブラウザ画面を開いてください。"),
    "stopped": ("席取り処理が停止しました", "アプリを開いて結果を確認してください。この通知は席の確保や購入完了を意味しません。"),
    "unavailable": ("この条件では席を確保できませんでした", "サイトから席をご用意できないとの回答があり、処理を停止しました。一覧の空席表示とは結果が異なります。"),
    "test": ("席取りサポート・通知テスト", "これは接続テストです。席の申し込みや購入は行っていません。"),
}


class PushService:
    def __init__(self, secret, directory, on_result, invoke=None):
        self.secret = secret
        self.directory = Path(directory)
        self.on_result = on_result
        self.invoke = invoke or self._invoke
        self.lock = threading.Lock()
        self.seen = set()
        self.pending = queue.Queue()
        self.subscriptions = []
        self.public_key = self.invoke({"operation": "publicKey"})["publicKey"]
        try:
            values = json.loads((self.directory / "subscriptions.json").read_text(encoding="utf-8"))
            for value in values:
                if self.invoke({"operation": "validate", "subscription": value["subscription"]}).get("valid"):
                    self.subscriptions.append(value)
        except (OSError, ValueError, KeyError, TypeError):
            self.subscriptions = []
        self.thread = threading.Thread(target=self._work, daemon=True, name="web-push")
        self.thread.start()

    def _invoke(self, payload):
        startup = None
        if os.name == "nt":
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0
        result = subprocess.run(["node", str(HERE / "push.mjs")], input=json.dumps({**payload, "seed": self.secret}),
                                text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=20, startupinfo=startup)
        if result.returncode:
            raise ValueError("通知サービスの初期化または送信に失敗しました。")
        return json.loads(result.stdout)

    def _save(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.directory / "subscriptions.tmp"
        temporary.write_text(json.dumps(self.subscriptions), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.directory / "subscriptions.json")

    def subscribe(self, subscription, origin):
        parsed = urlsplit(origin)
        if parsed.scheme != "https" or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("通知の登録にはHTTPSの公開URLが必要です。")
        if not self.invoke({"operation": "validate", "subscription": subscription}).get("valid"):
            raise ValueError("通知先の形式が正しくありません。")
        with self.lock:
            self.subscriptions = [x for x in self.subscriptions if x["subscription"]["endpoint"] != subscription["endpoint"]]
            self.subscriptions.append({"subscription": subscription, "origin": origin})
            self.subscriptions = self.subscriptions[-3:]
            self._save()

    def enqueue(self, job, event):
        if event not in MESSAGES:
            raise ValueError("未対応の通知イベントです。")
        with self.lock:
            if (job, event) in self.seen:
                return False
            self.seen.add((job, event))
            if len(self.seen) > 1000:
                self.seen = {(job, event)}
            subscriptions = list(self.subscriptions)
        self.pending.put((job, event, subscriptions))
        return True

    def _work(self):
        while True:
            item = self.pending.get()
            try:
                if item is None:
                    return
                job, event, subscriptions = item
                if not subscriptions:
                    self.on_result(job, event, False, "通知先が登録されていません。")
                    continue
                accepted = 0
                for value in subscriptions:
                    title, body = MESSAGES[event]
                    try:
                        result = self.invoke({"operation": "send", "subscription": value["subscription"],
                            "subject": value["origin"], "message": {"title": title, "body": body,
                            "tag": f"{job}-{event}", "url": "/"}})
                    except Exception:
                        result = {"accepted": False, "status": 0}
                    accepted += bool(result.get("accepted"))
                    if result.get("status") in (404, 410):
                        with self.lock:
                            self.subscriptions = [x for x in self.subscriptions if x != value]
                            self._save()
                detail = "通知サービスが受け付けました。iPhoneへの到着は端末で確認してください。" if accepted else "通知の受付を確認できませんでした。アプリで状況を確認してください。"
                self.on_result(job, event, bool(accepted), detail)
            finally:
                self.pending.task_done()

    def close(self):
        self.pending.put(None)
