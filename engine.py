"""Independent reservation helper. No code from the distributed executable is used."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import queue
import re
import sys
import threading
import time
import unicodedata
from urllib.parse import urlsplit

os.environ["SE_AVOID_STATS"] = "true"

from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.webdriver import WebDriver as ChromeDriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select

JST = dt.timezone(dt.timedelta(hours=9))
BASE = Path(__file__).parent / "static"
DATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "JRASeatHelper"
PREFIX = "Main_SeatListItemRepeater_SeatListItem_"


class Halt(Exception):
    pass


class Unavailable(Halt):
    """An explicit server rejection, not inferred from the availability icon."""


def message_result(text):
    text = normalized(text)
    if any(t in text for t in ("お席をご用意できませんでした", "お席をご用意することができませんでした")):
        return "unavailable"
    return "unknown"


def normalized(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))


def parse_start(value, now=None):
    now = now or dt.datetime.now(JST)
    target = dt.datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
    if target <= now:
        raise ValueError("開始日時は現在より後の日本時間を指定してください。")
    return target


def choose_seat(seats, wishes):
    for wish in wishes:
        matches = [s for s in seats if normalized(s["name"]) == normalized(wish)
                   and s["available"]]
        if len(matches) > 1:
            raise Halt("同名の席が複数あります。ブラウザで確認してください。")
        if matches:
            return matches[0]
    return None


SEATS_JS = r"""
return [...document.querySelectorAll('[id*="_spnSeatStockSymbolClass_"]')].map(e => {
 const m=e.id.match(/^(.*)_spnSeatStockSymbolClass_(\d+)$/);
 const row=e.closest('li');
 const area=document.getElementById(m[1]+'_AreaDropDownList_'+m[2]);
 const control=document.getElementById(m[1]+'_PnlAutoSelectButton_'+m[2]);
 return {base:m[1], index:m[2], name:(row ? row.innerText : '').split('\n')[0].trim(),
 available:!!control && e.getClientRects().length>0 &&
 (e.classList.contains('seat_vacant') || e.classList.contains('seat_few')),
 stock:e.classList.contains('seat_few')?'few':e.classList.contains('seat_vacant')?'vacant':'none',
 areas:area?[...area.options].map(o=>o.text.trim()):[]};
});
"""


class Browser:
    def __init__(self, stop, log, demo=False, on_event=None):
        self.driver = None
        self.stop = stop
        self.log = log
        self.demo = demo
        self.target = None
        self.on_event = on_event

    def event(self, name):
        if self.on_event:
            try:
                self.on_event(name)
            except Exception:
                self.log("通知の連携に失敗しました。処理状況は画面で確認してください。")

    def check(self):
        if self.stop.is_set():
            raise Halt("中止しました。送信済みの申し込みは取り消されません。")

    def open(self):
        if self.driver:
            raise Halt("ブラウザはすでに開いています。")
        options = ChromeOptions()
        options.page_load_strategy = "eager"
        options.add_argument("--incognito")
        options.add_argument("--headless=new")
        options.add_argument("--window-size=430,900")
        options.add_argument("--disable-dev-shm-usage")
        if os.environ.get("CHROME_NO_SANDBOX") == "1":
            options.add_argument("--no-sandbox")
        if os.environ.get("CHROME_BINARY"):
            options.binary_location = os.environ["CHROME_BINARY"]
        service = ChromeService(executable_path=os.environ["CHROME_DRIVER"]) if os.environ.get("CHROME_DRIVER") else ChromeService()
        self.driver = ChromeDriver(options=options, service=service)
        self.driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 760, "deviceScaleFactor": 1, "mobile": False})
        self.driver.set_page_load_timeout(30)
        self.driver.get((BASE / "demo.html").as_uri() if self.demo else "https://jra-tickets.jp/")
        self.log("ブラウザを開きました。「ブラウザ操作」でログインし、対象開催日の席種一覧まで進めてください。")

    def guard(self):
        self.check()
        if not self.driver:
            raise Halt("先にブラウザを開いてください。")
        url = self.driver.current_url
        parsed = urlsplit(url)
        if self.demo:
            if url.split("#")[0] != (BASE / "demo.html").as_uri():
                raise Halt("デモ以外のページを検出したため停止しました。")
        elif parsed.scheme != "https" or parsed.hostname != "jra-tickets.jp" or parsed.port not in (None, 443):
            raise Halt("対象外のページです。JRAの席種一覧を開いてください。")
        return url

    def text(self):
        return self.driver.find_element(By.TAG_NAME, "body").text

    def blockers(self):
        self.guard()
        body = self.text()
        # An existing confirmation/message must be handled by the person first.
        for ident in ("messageDialog", "confirmDialog"):
            if self.visible(f"#{ident}"):
                raise Halt("ブラウザに確認・エラーが出ています。内容を手動で確認してください。")
        if any(t in body for t in ("順番待ち", "待合室", "アクセスが集中", "ロボットではない", "認証してください")):
            raise Halt("待機・混雑・認証画面です。自動操作を止めました。ブラウザで対応してください。")
        if self.visible("#loginPassword"):
            raise Halt("ログイン画面です。ブラウザでログインしてください。")

    def visible(self, selector):
        return [e for e in self.driver.find_elements(By.CSS_SELECTOR, selector) if e.is_displayed()]

    def seats(self):
        self.blockers()
        seats = self.driver.execute_script(SEATS_JS)
        if not seats or any(not s["name"] or not s["base"].startswith(PREFIX) for s in seats):
            raise Halt("対応する席種一覧が見つかりません。ページ構成が変わった可能性があります。")
        return seats

    def capture(self):
        seats = self.seats()
        self.target = self.driver.current_url
        return seats

    def outcome(self):
        """Inspect visible results only; never dismiss or accept a dialog."""
        # A visible explicit rejection wins over a stale stock icon or payment heading.
        # The site may show it outside the legacy messageDialog container.
        if message_result(self.text()) == "unavailable":
            return "unavailable"
        if self.visible("#confirmDialog"):
            return "confirm"
        dialogs = self.visible("#messageDialog")
        if dialogs:
            return message_result(dialogs[0].text)
        # Seat pages can contain payment instructions. They are not a handoff.
        if self.visible("[id*='_spnSeatStockSymbolClass_']"):
            return "pending"
        headings = self.visible("h1, h2, h3, legend, [role='heading']")
        payment_titles = ("クレジットカード情報入力", "クレジットカード情報の入力",
                          "クレジットカード番号入力", "カード情報入力")
        if any(any(t in normalized(h.text) for t in payment_titles) for h in headings):
            return "handoff"
        if self.visible("input[autocomplete='cc-number']"):
            return "handoff"
        if any(t in self.text() for t in ("お支払い方法", "支払方法を選択", "購入内容の確認")):
            return "handoff"
        return "pending"

    def click(self, element):
        self.guard()
        if not element.is_displayed() or not element.is_enabled():
            raise Halt("操作ボタンが有効ではありません。ブラウザを確認してください。")
        # Regular WebDriver click: do not force hidden or blocked controls.
        element.click()

    def reserve(self, seat, count, area):
        self.blockers()
        fresh = [s for s in self.seats() if s["base"] == seat["base"] and s["index"] == seat["index"]
                 and normalized(s["name"]) == normalized(seat["name"])]
        if len(fresh) != 1 or not fresh[0]["available"]:
            raise Halt("申し込み直前に空席候補が変わりました。申し込みを送信せず停止しました。")
        base, index = seat["base"], seat["index"]
        controls = self.visible(f"#{base}_PnlAutoSelectButton_{index} label.btn_01")
        if len(controls) != 1:
            raise Halt("申し込みボタンを一意に識別できないため停止しました。")
        self.click(controls[0])
        popup = f"#{base}_ConfirmPopup_{index}_PopupPanel_{index}"
        # The user reads and accepts site notices; no blanket acceptance.
        deadline = time.monotonic() + 120
        informed = False
        while self.visible(popup):
            self.guard()
            if not informed:
                self.log("ブラウザの注意事項を読み、同意する場合は手動で先へ進めてください（2分待機）。")
                self.event("attention_required")
                informed = True
            if time.monotonic() > deadline:
                raise Halt("注意事項の確認待ちで停止しました。")
            self.stop.wait(0.4)
        self.blockers()
        dropdown = self.visible(f"#{base}_AreaDropDownList_{index}")
        if dropdown:
            select = Select(dropdown[0])
            if area:
                opts = [o for o in select.options if normalized(o.text) == normalized(area)]
                if len(opts) != 1:
                    raise Halt("希望エリアが見つかりません。別エリアへの変更は行いません。")
                self.check()
                opts[0].click()
            else:
                self.check()
                select.select_by_index(0)
        elif area:
            raise Halt("エリア選択欄が見つかりません。")
        links = self.visible(f"[id^='{base}_SeatAmountRepeater_'][id*='_LinkButton_']")
        matches = [e for e in links if normalized(e.text) == f"{count}席"]
        if len(matches) != 1:
            raise Halt("希望枚数のボタンがありません。枚数を変えずに停止しました。")
        self.log(f"{seat['name']}・{count}席で取り置きを申し込みます。空席表示は候補の目安で、確保の保証ではありません。")
        self.click(matches[0])
        # A timeout is an unknown result, never a reason to submit again.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            self.guard()
            result = self.outcome()
            if result == "confirm":
                self.event("attention_required")
                raise Halt("追加確認が出ています。ブラウザで内容を確認してください。自動再送信は行いません。")
            if result == "unavailable":
                raise Unavailable("サイトが、この条件ではお席をご用意できないと回答しました。この申し込みでは確保できていません。確認のため停止しました。")
            if result == "unknown":
                raise Halt("未対応のサイトメッセージです。成否は未判定のため停止しました。ブラウザで内容を確認してください。自動再送信は行いません。")
            if result == "handoff":
                self.log("支払い・購入内容の確認画面を検出しました。取り置きの成立と期限はブラウザで確認し、購入確定は手動で行ってください。")
                self.event("payment_ready")
                return
            self.stop.wait(0.5)
        raise Halt("申し込み後の結果を判定できません。ブラウザを確認してください。自動再送信は行いません。")

    def run(self, wishes, count, area, start, watch, interval, minutes):
        if not self.target:
            raise Halt("先に「席種を読み込む」で対象ページを確認してください。")
        if not wishes:
            raise Halt("希望席を1つ以上選んでください。")
        if interval < 60 or not 1 <= minutes <= 120 or not 1 <= count <= 4:
            raise Halt("確認間隔は60秒以上、監視時間は1〜120分、枚数は1〜4席です。")
        while dt.datetime.now(JST) < start:
            self.check()
            self.stop.wait(min(0.5, (start - dt.datetime.now(JST)).total_seconds()))
        end = time.monotonic() + minutes * 60
        while True:
            self.guard()
            if self.driver.current_url != self.target:
                raise Halt("対象ページが変わったため停止しました。席種を読み込み直してください。")
            self.blockers()
            self.check()
            self.driver.refresh()
            seats = self.seats()
            seat = choose_seat(seats, wishes)
            if seat:
                self.reserve(seat, count, area)
                return
            if not watch or time.monotonic() + interval >= end:
                raise Halt("希望席の空きを確認できず終了しました。")
            self.log(f"希望席は空きなし。{interval}秒後に再確認します。")
            self.stop.wait(interval)

