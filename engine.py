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
JRA_HOSTS = frozenset({"jra-tickets.jp", "my.jra-tickets.jp"})
JRA_LOGIN_URL = "https://my.jra-tickets.jp/login/"
BASE = Path(__file__).parent / "static"
DATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "JRASeatHelper"
PREFIX = "Main_SeatListItemRepeater_SeatListItem_"


class Halt(Exception):
    pass


class Unavailable(Halt):
    """An explicit server rejection, not inferred from the availability icon."""


class SeatLimit(Halt):
    """The selected seat type does not permit the requested quantity."""


def picker_capacity(text):
    text = normalized(text)
    if "おまかせ席選択" not in text:
        return None
    values = set(re.findall(r"ご予約できる最大席数は([0-9]+)席です", text))
    return int(next(iter(values))) if len(values) == 1 else None


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
 const button=control?.querySelector('label.btn_01,a,button,input[type=button],input[type=submit]');
 const enabled=!!button && !!button.getClientRects().length && !button.closest('[disabled],[aria-disabled="true"],.disabled,.aspNetDisabled') && getComputedStyle(button).pointerEvents!=='none';
 return {base:m[1], index:m[2], name:(row ? row.innerText : '').split('\n')[0].trim(),
 available:enabled && e.getClientRects().length>0 &&
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
        options.add_argument("--lang=ja-JP")
        options.add_experimental_option("prefs", {"intl.accept_languages": "ja-JP,ja"})
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
        self.driver.get((BASE / "demo.html").as_uri() if self.demo else JRA_LOGIN_URL)
        if not self.demo:
            self.prepare_login()
        else:
            self.log("ブラウザを開きました。「ブラウザ操作」でログインし、対象開催日の席種一覧まで進めてください。")

    def prepare_login(self):
        # Only prepare the form. Never enter credentials or submit a login.
        deadline = time.monotonic() + 10
        while True:
            self.check()
            if self.congested():
                self.log("JRAでアクセス集中・待機画面を検出しました。画面を維持しますので、サイトの案内に従ってください。自動更新・ログイン送信は行いません。")
                return
            fields = self.visible("#loginMail")
            passwords = self.visible("#loginPassword")
            lang = self.driver.execute_script("return document.documentElement.lang") or ''
            if fields and passwords and lang.lower().startswith('ja') and fields[0].is_enabled():
                self.driver.execute_script("arguments[0].focus();arguments[0].scrollIntoView({block:'center'})", fields[0])
                self.log("日本語のJRAログイン画面を開き、会員番号・メールアドレス欄を選択しました。ブラウザ操作の入力欄から入力してください。")
                return
            if time.monotonic() >= deadline:
                self.log("ログイン入力欄の準備を確認できませんでした。表示中のJRA画面を確認してください。自動ログインや再読み込みは行いません。")
                return
            self.stop.wait(.4)

    def congested(self):
        body = self.text()
        return any(t in body for t in ("順番待ち", "待合室", "アクセスが集中", "アクセス集中", "ただいま混み合", "現在混み合", "ただいま大変混雑"))

    def guard(self):
        self.check()
        if not self.driver:
            raise Halt("先にブラウザを開いてください。")
        url = self.driver.current_url
        parsed = urlsplit(url)
        if self.demo:
            if url.split("#")[0] != (BASE / "demo.html").as_uri():
                raise Halt("デモ以外のページを検出したため停止しました。")
        elif parsed.scheme != "https" or parsed.hostname not in JRA_HOSTS or parsed.port not in (None, 443):
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
        if self.congested():
            raise Halt("JRAのアクセス集中・待機画面です。空席なしとは判定せず自動操作を停止しました。画面を維持し、サイトの案内に従ってください。")
        if any(t in body for t in ("ロボットではない", "認証してください")):
            raise Halt("本人による認証が必要です。自動操作を止めました。ブラウザで対応してください。")
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
        if self.congested():
            raise Halt("JRAのアクセス集中・待機画面を検出しました。申し込みの再送はせず停止しました。サイトの案内に従ってください。")
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
        opening_deadline = time.monotonic() + 8
        while not self.visible(popup) and not self.visible(f"[id^='{base}_SeatAmountRepeater_'][id*='_LinkButton_']"):
            self.blockers()
            if time.monotonic() >= opening_deadline:
                raise Halt("注意事項または枚数選択の表示を確認できず停止しました。")
            self.stop.wait(.2)
        # Only the seat-specific notice opened by this attempt is eligible.
        deadline = time.monotonic() + 120
        advanced = False
        while self.visible(popup):
            self.guard()
            if not advanced:
                panel = self.visible(popup)[0]
                checks = []
                for field in panel.find_elements(By.CSS_SELECTOR, "input[type='checkbox']"):
                    # Styled controls often hide the native input and expose its label.
                    labels = self.driver.execute_script("return Array.from(arguments[0].labels || [])", field)
                    labels = [e for e in labels if e.is_displayed() and normalized(e.text) == "注意事項を確認しました"]
                    if not labels and field.is_displayed():
                        # Some WebForms templates render text beside the input without
                        # an HTML label association. Accept only its exact local caption.
                        parent = field.find_element(By.XPATH, "..")
                        if (parent.is_displayed() and normalized(parent.text) == "注意事項を確認しました"
                                and len(parent.find_elements(By.CSS_SELECTOR, "input[type='checkbox']")) == 1):
                            labels = [parent]
                    if field.is_enabled() and len(labels) == 1:
                        checks.append((field, field if field.is_displayed() else labels[0]))
                if len(checks) != 1:
                    raise Halt("席種の注意事項チェック欄を一意に確認できません。自動で進めず停止しました。")
                field, target = checks[0]
                if not field.is_selected():
                    self.click(target)
                if not field.is_selected():
                    raise Halt("注意事項のチェック反映を確認できません。次へ進まず停止しました。")
                next_deadline = time.monotonic() + 8
                while True:
                    self.guard()
                    buttons = self.notice_next_buttons(panel)
                    if len(buttons) == 1 and buttons[0].is_enabled() and buttons[0].get_attribute('aria-disabled') != 'true':
                        break
                    if time.monotonic() >= next_deadline:
                        raise Halt(f"注意事項の「次へ進む」を一意に確認できません（候補{len(buttons)}件）。停止しました。")
                    self.stop.wait(.2)
                self.log("席種の注意事項をチェックし、次へ進みます。まだ席は確保できていません。")
                self.click(buttons[0])
                advanced = True
            if time.monotonic() > deadline:
                raise Halt("注意事項から次の画面への遷移を確認できず停止しました。再送は行いません。")
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
        picker_deadline = time.monotonic() + 8
        while True:
            self.guard()
            self.blockers()
            capacity = picker_capacity(self.text())
            if capacity is not None and capacity < count:
                raise SeatLimit(f"この席種の申し込み上限は{capacity}席ですが、希望は{count}席です。枚数または席種を見直してください。自動で枚数を減らしたり再申し込みしたりせず停止しました。")
            links = self.visible(f"[id^='{base}_SeatAmountRepeater_'][id*='_LinkButton_']")
            matches = [e for e in links if normalized(e.text) == f"{count}席"]
            if len(matches) == 1:
                break
            if len(matches) > 1 or time.monotonic() >= picker_deadline:
                raise Halt("希望枚数のボタンを一意に確認できません。枚数を変えずに停止しました。")
            self.stop.wait(.4)
        limit = getattr(self, '_attempt_deadline', None)
        if limit is not None and time.monotonic() >= limit:
            raise Halt("監視時間に達したため、次の申し込みを送信せず停止しました。")
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
                raise Unavailable("サイトが、この条件ではお席をご用意できないと回答しました。この申し込みでは確保できていません。自動再確認の設定に従って処理します。")
            if result == "unknown":
                raise Halt("未対応のサイトメッセージです。成否は未判定のため停止しました。ブラウザで内容を確認してください。自動再送信は行いません。")
            if result == "handoff":
                self.log("支払い・購入内容の確認画面を検出しました。取り置きの成立と期限はブラウザで確認し、購入確定は手動で行ってください。")
                self.event("payment_ready")
                return
            self.stop.wait(0.5)
        raise Halt("申し込み後の結果を判定できません。ブラウザを確認してください。自動再送信は行いません。")

    def notice_next_buttons(self, panel):
        """Read visible controls in this notice; nested captions are one control."""
        selector = "button,a,input[type='submit'],input[type='button'],input[type='image'],label,[role='button'],[onclick]"
        candidates = []
        for element in panel.find_elements(By.CSS_SELECTOR, selector):
            if not element.is_displayed():
                continue
            caption = element.text or element.get_attribute('value') or element.get_attribute('alt') or ''
            if normalized(caption).strip('›»→❯〉＞>') == '次へ進む':
                candidates.append(element)
        # A label/wrapper and its link can both expose the same caption.
        # Prefer an actual button/link; never guess between separate controls.
        primary = [e for e in candidates if e.tag_name.lower() in ('a', 'button', 'input')]
        if primary:
            return [e for e in candidates if not any(e != p and self.driver.execute_script(
                'return arguments[0].contains(arguments[1]) || arguments[1].contains(arguments[0])', e, p)
                for p in primary)]
        return [e for e in candidates if not any(e != other and self.driver.execute_script(
            'return arguments[0].contains(arguments[1])', e, other) for other in candidates)]

    def run(self, wishes, count, area, start, watch, interval, minutes):
        if not self.target:
            raise Halt("先に「席種を読み込む」で対象ページを確認してください。")
        if not wishes:
            raise Halt("希望席を1つ以上選んでください。")
        if interval < 10 or not 1 <= minutes <= 120 or not 1 <= count <= 4:
            raise Halt("確認間隔は10秒以上、監視時間は1〜120分、枚数は1〜4席です。")
        while dt.datetime.now(JST) < start:
            self.check()
            self.stop.wait(min(0.5, (start - dt.datetime.now(JST)).total_seconds()))
        end = time.monotonic() + minutes * 60
        last_unavailable = None
        first_check = True
        while True:
            cycle_started = time.monotonic()
            attempt_failed = False
            self.guard()
            if not first_check and time.monotonic() >= end:
                if last_unavailable:
                    raise Unavailable("監視時間内に指定枚数を確保できず終了しました。")
                raise Halt("監視時間内に希望席の空きを確認できず終了しました。")
            if last_unavailable:
                # Never refresh a failed POST. Navigate with GET to the exact seat-list
                # URL captured by the person, only after the explicit rejection wait.
                self.driver.get(self.target)
            else:
                if self.driver.current_url != self.target:
                    raise Halt("対象ページが変わったため停止しました。席種を読み込み直してください。")
                self.blockers()
                self.check()
                self.driver.refresh()
            if self.driver.current_url != self.target:
                raise Halt("対象の席種一覧に戻れませんでした。開催日をブラウザで確認してください。再申し込みは行いません。")
            first_check = False
            seats = self.seats()
            seat = choose_seat(seats, wishes)
            if time.monotonic() >= end:
                raise Halt("監視時間に達したため、次の申し込みを送信せず停止しました。")
            if seat:
                self._attempt_deadline = end
                try:
                    self.reserve(seat, count, area)
                    return
                except Unavailable:
                    # An unknown response, timeout, payment/extra confirmation, login,
                    # queue, or CAPTCHA is never permission to repeat a submission.
                    if not watch:
                        raise Unavailable("サイトが指定枚数を確保できないと回答しました。定期確認が無効のため停止しました。")
                    self.guard()
                    if self.outcome() != "unavailable" or self.visible("#confirmDialog") or self.has_payment_evidence():
                        raise Halt("確保不可の回答以外の画面も検出しました。結果を確認するため停止しました。再申し込みは行いません。")
                    last_unavailable = True
                    attempt_failed = True
                finally:
                    self._attempt_deadline = None
            # Inventory checks aim for start-to-start spacing. A definitive
            # rejected reservation waits the full interval AFTER its response.
            delay = interval if attempt_failed else max(0, interval-(time.monotonic()-cycle_started))
            if not watch or time.monotonic() + delay >= end:
                if last_unavailable:
                    raise Unavailable("指定枚数を確保できませんでした。次の確認が監視時間を超えるため終了しました。")
                raise Halt("希望席の空きを確認できず終了しました。")
            if attempt_failed:
                self.log(f"指定した{count}席を確保できませんでした。{interval}秒後に同じ条件で再確認します。")
            elif delay:
                self.log(f"希望席は空きなし。{delay:.1f}秒後に再確認します（確認間隔{interval}秒）。")
            else:
                self.log(f"希望席は空きなし。確認処理が{interval}秒以上かかったため、次の確認へ進みます。処理は重ねません。")
            self.event("retry_wait")
            self.stop.wait(delay)

    def has_payment_evidence(self):
        """A rejection alongside payment evidence is ambiguous: never auto retry."""
        if self.visible("input[autocomplete='cc-number']"):
            return True
        titles = ("クレジットカード情報", "クレジットカード番号", "カード情報入力", "購入内容の確認", "お支払い方法", "支払方法を選択")
        return any(any(t in normalized(h.text) for t in titles)
                   for h in self.visible("h1, h2, h3, legend, [role='heading']"))
