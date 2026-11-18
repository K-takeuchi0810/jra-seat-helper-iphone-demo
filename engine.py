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
from selenium.common.exceptions import StaleElementReferenceException, WebDriverException
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


class ListingPending(Halt):
    """Read-only event discovery encountered an explicitly full entry."""


class ListingConnectionLost(Halt):
    """A known transport failure during the fixed, read-only event search GET."""


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


def safe_service_codes(body):
    """Return restricted service error codes, never error text or transaction IDs."""
    if not isinstance(body,str) or len(body)>65536:return []
    try:document=json.loads(body)
    except (ValueError,TypeError):return []
    codes=[]
    def walk(value,depth=0):
        if depth>4:return
        if isinstance(value,dict):
            for key,item in value.items():
                if key in ('errorCode','error_code','errCode','errorType','error_type') and isinstance(item,(str,int)):
                    code=str(item)
                    if re.fullmatch(r'(?:[A-Z][A-Z_]{0,16}[0-9]{0,6}|[0-9]{3,6}|[a-z]+(?:_[a-z]+){1,3})',code):codes.append(code)
                elif isinstance(item,(dict,list)):walk(item,depth+1)
        elif isinstance(value,list):
            for item in value[:12]:walk(item,depth+1)
    walk(document)
    return sorted(set(codes))[:8]


def safe_service_details(body):
    """Classify an error document without returning its text or identifiers."""
    if not isinstance(body,str) or len(body)>65536:return {'format':'unavailable','codes':[]}
    details={'codes':safe_service_codes(body)}
    try:
        parsed=json.loads(body);details['format']='json'
    except (ValueError,TypeError):
        parsed=None;details['format']='html' if body.lstrip().startswith('<') else 'text'
    allowed={'threeDSMethodData','threeDSServerTransID','threeDSMethodNotificationURL','creq','browserInfo','browserJavaEnabled','browserJavascriptEnabled','browserLanguage','browserColorDepth','browserScreenHeight','browserScreenWidth','browserTZ','notificationURL'}
    fields=set()
    def walk(value,depth=0):
        if depth>4:return
        if isinstance(value,dict):
            for key,item in value.items():
                if key in allowed:fields.add(key)
                if isinstance(item,(dict,list)):walk(item,depth+1)
        elif isinstance(value,list):
            for item in value[:12]:walk(item,depth+1)
    if parsed is not None:walk(parsed)
    if fields:details['mentionedFields']=sorted(fields)
    lowered=body.lower()
    categories=[name for name,phrases in {
        'bad-request':('bad request','bad_request'),
        'missing-parameter':('missing parameter','required parameter','field is required'),
        'invalid-parameter':('invalid parameter','invalid request'),
        'expired':('session expired','token expired','expired token'),
        'forbidden':('access denied','forbidden'),
    }.items() if any(phrase in lowered for phrase in phrases)]
    if categories:details['categories']=categories
    return details


def parse_start(value, now=None):
    now = now or dt.datetime.now(JST)
    target = dt.datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
    if target <= now:
        raise ValueError("開始日時は現在より後の日本時間を指定してください。")
    return target


RACECOURSES = ("札幌", "函館", "福島", "新潟", "東京", "中山", "中京", "京都", "阪神", "小倉")


def booking_target(data, today=None):
    if not isinstance(data, dict):
        raise ValueError("競馬場と開催日を指定してください。")
    course = data.get("course", "")
    date = data.get("date", "")
    if not isinstance(course, str) or course not in RACECOURSES or not isinstance(date, str):
        raise ValueError("競馬場と開催日を指定してください。")
    try:
        day = dt.date.fromisoformat(date)
    except ValueError:
        raise ValueError("開催日は年月日で指定してください。") from None
    today = today or dt.datetime.now(JST).date()
    if not today <= day <= today + dt.timedelta(days=90):
        raise ValueError("開催日は本日から90日以内で指定してください。")
    return {"course": course, "date": day.isoformat()}


def matches_booking_header(text, target):
    """Only a visible seat-page header is evidence, not options behind a modal."""
    text = unicodedata.normalize("NFKC", text)
    day = dt.date.fromisoformat(target["date"])
    courses = set(re.findall(r"(札幌|函館|福島|新潟|東京|中山|中京|京都|阪神|小倉)競馬場", text))
    dates = set()
    for match in re.finditer(r"(?:(20[0-9]{2})[年/.-])?([0-9]{1,2})[月/]([0-9]{1,2})(?:日|(?![0-9]))", text):
        year, month, date = match.groups()
        dates.add((int(year) if year else day.year, int(month), int(date)))
    return courses == {target["course"]} and dates == {(day.year, day.month, day.day)}


def choose_seat(seats, wishes):
    for wish in wishes:
        matches = [s for s in seats if normalized(s["name"]) == normalized(wish)
                   and s["available"]]
        if len(matches) > 1:
            raise Halt("同名の席が複数あります。ブラウザで確認してください。")
        if matches:
            return matches[0]
    return None


FRONT_JS = r"""
const shown=e=>!!e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
const modalCandidates=[...document.querySelectorAll('dialog[open],[aria-modal="true"],[role="dialog"],[id*="_PopupPanel_"],#messageDialog,#confirmDialog')].filter(shown);
let front=modalCandidates.filter(e=>!modalCandidates.some(other=>other!==e&&e.contains(other)))
  .sort((a,b)=>(parseInt(getComputedStyle(a).zIndex)||0)-(parseInt(getComputedStyle(b).zIndex)||0)).at(-1)||document.body;
if(front!==document.body && !(front.innerText||'').includes('次へ進む')){
 for(let i=0;i<3;i++){const p=front.parentElement;if(!p||['BODY','HTML','FORM'].includes(p.tagName)||p.querySelector('[id*="_spnSeatStockSymbolClass_"]'))break;
 if((p.innerText||'').includes('次へ進む')){front=p;break;}}
}
const uncovered=e=>{
 const r=e.getBoundingClientRect();const x=(Math.max(0,r.left)+Math.min(innerWidth,r.right))/2,y=(Math.max(0,r.top)+Math.min(innerHeight,r.bottom))/2;
 if(r.bottom<=0||r.top>=innerHeight||r.right<=0||r.left>=innerWidth)return front!==document.body&&front.contains(e);
 const top=document.elementFromPoint(x,y);return !!top&&(e.contains(top)||top.contains(e));
};
"""

SEATS_JS = r"""
return [...document.querySelectorAll('[id*="_spnSeatStockSymbolClass_"]')].map(e => {
 const m=e.id.match(/^(.*)_spnSeatStockSymbolClass_(\d+)$/);
 const row=e.closest('li');
 const area=document.getElementById(m[1]+'_AreaDropDownList_'+m[2]);
 const control=document.getElementById(m[1]+'_PnlAutoSelectButton_'+m[2]);
 const button=control?.querySelector('label.btn_01,a,button,input[type=button],input[type=submit]');
 const linked=button?.htmlFor?document.getElementById(button.htmlFor):null;
 const blocked=el=>!!el && [...(function*(x){while(x&&x!==control?.parentElement){yield x;x=x.parentElement}})(el)].some(x=>/disabled|disable|soldout|unavailable/i.test(x.className||'')||x.matches('[disabled],[aria-disabled="true"]'));
 const enabled=!blocked(button) && !blocked(linked) && !!button && !!button.getClientRects().length && !button.closest('[disabled],[aria-disabled="true"],.disabled,.aspNetDisabled') && getComputedStyle(button).pointerEvents!=='none';
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
        self.booking = None
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
        options.set_capability("goog:loggingPrefs", {"performance":"ALL","browser":"ALL"})
        # A fresh ChromeDriver profile isolates each session. Incognito blocks
        # third-party cookies by default and can break embedded authentication.
        if os.environ.get("CHROME_INCOGNITO") == "1":
            options.add_argument("--incognito")
        options.add_argument("--lang=ja-JP")
        options.add_experimental_option("prefs", {"intl.accept_languages": "ja-JP,ja", "credentials_enable_service":False, "profile.password_manager_enabled":False, "autofill.credit_card_enabled":False, "autofill.profile_enabled":False})
        self.display_mode="headless" if os.environ.get("CHROME_HEADLESS","1") != "0" else "headed"
        if self.display_mode=="headless":
            options.add_argument("--headless=new")
        options.add_argument("--window-size=430,900")
        options.add_argument("--disable-dev-shm-usage")
        if os.environ.get("CHROME_NO_SANDBOX") == "1":
            options.add_argument("--no-sandbox")
        if os.environ.get("CHROME_BINARY"):
            options.binary_location = os.environ["CHROME_BINARY"]
        service = ChromeService(executable_path=os.environ["CHROME_DRIVER"]) if os.environ.get("CHROME_DRIVER") else ChromeService()
        self._network_hosts={};self._network_pending={};self._network_errors=[];self._script_errors=[]
        self.driver = ChromeDriver(options=options, service=service)
        self.driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 760, "deviceScaleFactor": 1, "mobile": False})
        self.driver.set_page_load_timeout(30)
        self.driver.get((BASE / "demo.html").as_uri() if self.demo else JRA_LOGIN_URL)
        if not self.demo:
            self.prepare_login()
        else:
            self.log("ブラウザを開きました。「ブラウザ操作」でログインし、対象開催日の席種一覧まで進めてください。")

    def network_diagnostics(self):
        """Keep only host/status/error codes. Never retain URLs, headers or bodies."""
        hosts=getattr(self,'_network_hosts',{})
        errors=getattr(self,'_network_errors',[])
        pending=getattr(self,'_network_pending',{})
        try:entries=self.driver.get_log('performance')
        except Exception:return errors[-12:]
        for entry in entries:
            try:
                message=json.loads(entry['message'])['message'];params=message['params'];method=message['method']
                if method=='Network.requestWillBeSent':
                    hosts[params['requestId']]={'host':urlsplit(params['request']['url']).hostname or '', 'type':params.get('type','')}
                    if params['request'].get('method') in ('GET','POST','OPTIONS','HEAD'):hosts[params['requestId']]['method']=params['request']['method']
                    if len(hosts)>256:hosts.pop(next(iter(hosts)))
                elif method=='Network.loadingFailed':
                    code=re.search(r'net::ERR_[A-Z0-9_]+',params.get('errorText',''))
                    errors.append(dict(hosts.get(params['requestId'],{'host':'','type':''}),error=code.group(0) if code else 'loading-failed'))
                    hosts.pop(params['requestId'],None)
                elif method=='Network.responseReceived':
                    response=params['response']
                    if response['status']>=400:
                        failure={'host':urlsplit(response['url']).hostname or '', 'type':params.get('type',''), 'status':int(response['status'])}
                        if hosts.get(params.get('requestId'),{}).get('method'):failure['method']=hosts[params['requestId']]['method']
                        errors.append(failure)
                        if failure['host'] in ('as-pgx.ps.paysec-3dssvgw.com','pgx-auth-api.ps.paysec-3dssvgw.com'):
                            pending[params.get('requestId')]=failure
                            if len(pending)>24:pending.pop(next(iter(pending)))
                elif method=='Network.loadingFinished':
                    hosts.pop(params['requestId'],None)
                    failure=pending.pop(params['requestId'],None)
                    if failure is not None:
                        try:
                            payload=self.driver.execute_cdp_cmd('Network.getResponseBody',{'requestId':params['requestId']})
                            body=payload.get('body','')
                            if payload.get('base64Encoded'):
                                import base64
                                body=base64.b64decode(body).decode('utf-8','replace') if len(body)<90000 else ''
                            failure['serviceCodes']=safe_service_codes(body)
                            failure['responseDetails']=safe_service_details(body)
                        except Exception:failure['serviceCodes']=[]
            except (ValueError,KeyError,TypeError):continue
        self._network_hosts=hosts;self._network_pending=pending;self._network_errors=errors[-12:]
        return self._network_errors

    def script_diagnostics(self):
        errors=getattr(self,'_script_errors',[])
        try:entries=self.driver.get_log('browser')
        except Exception:return errors[-8:]
        for entry in entries:
            message=entry.get('message','')
            match=re.search(r'Uncaught (TypeError|ReferenceError|SyntaxError|RangeError|SecurityError|Error)\b',message)
            error=None
            if match:
                error={'category':match.group(1)}
                if match.group(1)=='ReferenceError':
                    symbol=re.search(r'ReferenceError: ([A-Za-z_$][A-Za-z0-9_$]{0,63}) is not defined\b',message)
                    if symbol:error['symbol']=symbol.group(1)
                elif match.group(1)=='SyntaxError':
                    for kind in ('Unexpected token','Unexpected end of JSON input','Invalid or unexpected token','Unexpected identifier','Invalid regular expression'):
                        if kind in message:error['syntaxKind']=kind;break
            elif 'Content Security Policy' in message:error={'category':'CSP'}
            elif 'CORS policy' in message:error={'category':'CORS'}
            if error:
                source=re.search(r'https?://[^\s\"<>]+',message)
                if source:
                    try:error['sourceHost']=urlsplit(source.group(0)).hostname or ''
                    except ValueError:pass
                if error not in errors:errors.append(error)
        self._script_errors=errors[-8:]
        return self._script_errors

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

    def load_event_search(self):
        """Only this known GET is eligible for transport recovery; never replay a form."""
        try:
            self.driver.get("https://jra-tickets.jp/EventSearch.aspx")
        except WebDriverException as exc:
            codes=('net::ERR_CONNECTION_CLOSED','net::ERR_CONNECTION_RESET','net::ERR_NETWORK_CHANGED','net::ERR_INTERNET_DISCONNECTED','net::ERR_TIMED_OUT')
            if any(code in str(exc) for code in codes):
                raise ListingConnectionLost("開催一覧の取得中にJRAとの通信が切れました。席の申し込みは送信していません。") from None
            raise

    def booking_catalog(self):
        self.guard()
        if self.congested():
            raise Halt("アクセス集中の画面を維持しました。サイトの案内に従ってください。")
        if self.has_payment_evidence():
            raise Halt("支払い中の画面を維持しました。開催一覧の取得は手続き終了後に行ってください。")
        if not self.demo:
            self.load_event_search()
        self.blockers()
        tabs=self.visible('#linkButtonRaceCource')
        if len(tabs)!=1:
            raise Halt("開催競馬場の検索画面を確認できません。ログイン状態をご確認ください。")
        if 'on' not in (tabs[0].get_attribute('class') or '').split():self.click(tabs[0])
        fields=self.visible('#dropDownListPlace')
        if len(fields)!=1:raise Halt("競馬場の一覧を取得できません。")
        places=[(o.get_attribute('value'),normalized(o.text)) for o in Select(fields[0]).options if o.is_enabled() and normalized(o.text).removesuffix('競馬場') in RACECOURSES]
        catalog=[]
        for value,label in places:
            self.blockers()
            Select(self.visible('#dropDownListPlace')[0]).select_by_value(value)
            fields=self.visible('#dropDownListEventDate')
            if len(fields)!=1:raise Halt("開催日の一覧を取得できません。")
            dates=[]
            for option in Select(fields[0]).options:
                value=(option.get_attribute('value') or '').split('T')[0]
                if not option.is_enabled():continue
                try:target=booking_target({'course':label.removesuffix('競馬場'),'date':value})
                except ValueError:continue
                dates.append({'date':target['date'],'label':option.text.strip()})
            if dates:catalog.append({'course':label.removesuffix('競馬場'),'dates':dates})
        if not catalog:raise Halt("現在選択できる開催がありません。別の日付は生成しません。")
        self.booking=None
        self.target=None
        return catalog

    def booking_header(self):
        # Stop at the first seat row; do not read hidden options, notices, footers,
        # or unrelated calendar entries as proof of the selected event.
        return self.driver.execute_script("""
        const root=document.querySelector('main')||document.body;
        const first=root.querySelector('[id*="_spnSeatStockSymbolClass_"]');
        if(!first)return '';
        const boundary=first.closest('li')||first;
        const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);
        let text='',node;
        while(node=walker.nextNode()){
          if(boundary.contains(node)||(boundary.compareDocumentPosition(node)&Node.DOCUMENT_POSITION_FOLLOWING))break;
          const p=node.parentElement;
          if(p&&!p.closest('script,style,select,dialog,[role="dialog"],[aria-modal="true"]')&&p.getClientRects().length&&getComputedStyle(p).visibility!=='hidden')text+=' '+node.textContent;
          if(text.length>4000)break;
        }return text;
        """)

    def verify_booking(self, target):
        self.blockers()
        if not matches_booking_header(self.booking_header(), target):
            raise Halt("表示中の競馬場・開催日が指定条件と一致することを確認できません。申し込みを送信せず停止しました。")

    def full_entry_visible(self, reserved_only=False):
        return bool(self.driver.execute_script(r"""
        const norm=s=>(s||'').normalize('NFKC').replace(/\s/g,'');
        return [...document.querySelectorAll('button,a,input,span,label')].some(e=>{
          if(!e.getClientRects().length||getComputedStyle(e).visibility==='hidden'||norm(e.innerText||e.value)!=='満席')return false;
          if(arguments[0]){let scoped=false;for(let p=e.parentElement,n=0;p&&n<8;p=p.parentElement,n++){if(['BODY','HTML','FORM'].includes(p.tagName)||norm(p.innerText).includes('入場券'))break;if([...p.querySelectorAll('*')].some(h=>norm(h.innerText)==='指定席')){scoped=true;break;}}if(!scoped)return false;}
          return !!e.disabled||!!e.closest('[disabled],[aria-disabled="true"],.disabled,.aspNetDisabled,.btn_off,.soldout')||
            (!['A','BUTTON','INPUT'].includes(e.tagName)&&!e.hasAttribute('onclick')&&e.getAttribute('role')!=='button');
        });
        """,reserved_only))

    def event_notice_panel(self):
        """Recognize only the common JRA event-entry notice, never an arbitrary agreement."""
        self.guard()
        if not self.demo and urlsplit(self.driver.current_url).path.lower() != '/eventdetail.aspx':
            return None
        panels=self.driver.execute_script(r"""
        const norm=s=>(s||'').normalize('NFKC').replace(/\s/g,'');
        const shown=e=>!!e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
        const candidates=[];
        for(const field of document.querySelectorAll('input[type=checkbox]')){
          const captions=[...(field.labels||[])].map(e=>norm(e.innerText));
          if(!captions.includes('注意事項を確認しました')&&norm(field.parentElement?.innerText)!=='注意事項を確認しました')continue;
          for(let p=field.parentElement,n=0;p&&n<10;p=p.parentElement,n++){
            if(['BODY','HTML','FORM'].includes(p.tagName))break;
            const text=norm(p.innerText);
            if(shown(p)&&p.querySelectorAll('input[type=checkbox]').length===1&&!p.querySelector('[id*="_spnSeatStockSymbolClass_"]')&&
               text.includes('お知らせ')&&text.includes('指定席の転売・譲渡・不正購入は禁止されています')&&
               text.includes('同行者様')&&text.includes('注意事項を確認しました')&&text.includes('次へ進む'))candidates.push(p);
          }
        }
        const unique=[...new Set(candidates)];
        return unique.filter(p=>!unique.some(other=>other!==p&&p.contains(other)));
        """)
        if len(panels)>1:
            raise Halt("共通のお知らせが複数あります。内容を確認するため停止しました。")
        return panels[0] if panels else None

    def notice_checks(self, panel):
        checks=[]
        for field in panel.find_elements(By.CSS_SELECTOR, "input[type='checkbox']"):
            labels=self.driver.execute_script("return Array.from(arguments[0].labels || [])",field)
            labels=[e for e in labels if e.is_displayed() and normalized(e.text)=="注意事項を確認しました"]
            if not labels and field.is_displayed():
                parent=field.find_element(By.XPATH,"..")
                if parent.is_displayed() and normalized(parent.text)=="注意事項を確認しました" and len(parent.find_elements(By.CSS_SELECTOR,"input[type='checkbox']"))==1:
                    labels=[parent]
            if field.is_enabled() and len(labels)==1:
                checks.append((field,field if field.is_displayed() else labels[0]))
        return checks

    def advance_event_notice(self):
        panel=self.event_notice_panel()
        if panel is None:return False
        if self.congested() or self.has_payment_evidence():
            raise Halt("混雑または支払い画面を検出したため、共通のお知らせを自動操作せず停止しました。")
        checks=self.notice_checks(panel)
        if len(checks)!=1:raise Halt("共通のお知らせの確認欄を一意に識別できません。")
        field,control=checks[0]
        if not field.is_selected():self.click(control)
        if not field.is_selected():raise Halt("共通のお知らせのチェック反映を確認できません。")
        deadline=time.monotonic()+8
        while True:
            self.check()
            buttons=self.notice_next_buttons(panel)
            if len(buttons)==1 and buttons[0].is_enabled() and buttons[0].get_attribute('aria-disabled')!='true':break
            if time.monotonic()>=deadline:raise Halt("共通のお知らせの「次へ進む」を一意に確認できません。")
            self.stop.wait(.15)
        self.log("開催日の共通のお知らせを確認し、席種一覧へ進みます。まだ席の申し込みは送信していません。")
        self.click(buttons[0])
        deadline=time.monotonic()+15
        while True:
            self.check()
            try:
                if not panel.is_displayed():return True
            except StaleElementReferenceException:return True
            if time.monotonic()>=deadline:raise Halt("共通のお知らせからの遷移を確認できません。次へ進む操作は再送しません。")
            self.stop.wait(.15)

    def event_seat_entry(self):
        """Read the reserved-seat entry on the exact event detail, never admission tickets."""
        if not self.demo and urlsplit(self.driver.current_url).path.lower()!='/eventdetail.aspx':return None
        candidates=self.driver.execute_script(r"""
        const norm=s=>(s||'').normalize('NFKC').replace(/\s/g,'');
        const shown=e=>!!e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
        const entries=[...document.querySelectorAll('a,button,input[type=button],input[type=submit],label[onclick],label.btn_01')].filter(e=>shown(e)&&norm(e.innerText||e.value)==='受付中'&&!e.disabled&&!e.closest('[disabled],[aria-disabled="true"],.disabled,.aspNetDisabled,.btn_off'));
        const scoped=entries.filter(e=>{
          for(let p=e.parentElement,n=0;p&&n<8;p=p.parentElement,n++){
            if(['BODY','HTML','FORM'].includes(p.tagName))break;
            const text=norm(p.innerText);
            if(text.includes('入場券'))break;
            if([...p.querySelectorAll('*')].some(h=>norm(h.innerText)==='指定席'))return true;
          }
          return false;
        });
        return scoped.filter(e=>!scoped.some(other=>other!==e&&e.contains(other)));
        """)
        if len(candidates)>1:raise Halt("指定席の受付入口が複数あります。別の発売を選ばず停止しました。")
        return candidates[0] if candidates else None

    def prepare_booking(self, target):
        target = booking_target(target)
        pending_notice=self.event_notice_panel()
        if pending_notice is None:self.blockers()
        if pending_notice is None and self.visible('[id*="_spnSeatStockSymbolClass_"]') and matches_booking_header(self.booking_header(), target):
            self.booking = target
            return self.capture()
        if self.has_payment_evidence():
            raise Halt("支払い中の画面を維持しました。対象日の検索へ移動する前に手続きを終了してください。")
        if not self.demo:
            self.load_event_search()
        self.blockers()
        tabs = self.visible('#linkButtonRaceCource')
        if len(tabs) != 1:
            raise Halt("開催競馬場の検索画面を確認できません。ログインと画面をご確認ください。")
        if 'on' not in (tabs[0].get_attribute('class') or '').split():
            self.click(tabs[0])
        places = self.visible('#dropDownListPlace')
        if len(places) != 1:
            raise Halt("競馬場の選択欄を確認できません。")
        choices = [o for o in Select(places[0]).options if normalized(o.text) == target['course']+'競馬場' and o.is_enabled()]
        if len(choices) != 1:
            raise Halt("指定競馬場は現在の開催選択肢にありません。別の競馬場は選択しません。")
        Select(places[0]).select_by_value(choices[0].get_attribute('value'))
        end = time.monotonic()+8
        while True:
            self.blockers()
            fields = self.visible('#dropDownListEventDate')
            choices = [o for o in Select(fields[0]).options if (o.get_attribute('value') or '').split('T')[0] == target['date'] and o.is_enabled()] if len(fields)==1 else []
            if len(choices)==1:break
            if time.monotonic()>=end:
                raise Halt("指定開催日は現在の発売対象にありません。日付を変更せず停止しました。")
            self.stop.wait(.15)
        Select(fields[0]).select_by_value(choices[0].get_attribute('value'))
        search = self.visible('#Main_LinkButtonSearch')
        if len(search)!=1 or any(x in (search[0].get_attribute('class') or '').split() for x in ('btn_off','disabled')):
            raise Halt("指定条件の検索ボタンが有効ではありません。")
        self.click(search[0])
        end = time.monotonic()+15
        while True:
            self.blockers()
            links=[e for e in self.visible('a[href*="EventDetail.aspx?event="]') if normalized(e.text) in ('申し込みへ','受付中') and e.is_enabled() and e.get_attribute('aria-disabled')!='true' and not self.driver.execute_script("return !!arguments[0].closest('.btn_off,.disabled,.aspNetDisabled,[disabled],[aria-disabled=\"true\"]')",e)]
            if len(links)==1:break
            if not links and self.full_entry_visible():
                raise ListingPending("指定開催日の入口が満席で受付停止中です。席種の希望を保持して受付再開を確認します。")
            if len(links)>1 or time.monotonic()>=end:
                raise Halt("指定開催日の申し込み先を一意に確認できません。")
            self.stop.wait(.15)
        # Search is scoped to the exact selected full ISO date and racecourse.
        self.click(links[0])
        end=time.monotonic()+15
        entry_clicked=False
        while True:
            if self.advance_event_notice():end=time.monotonic()+15
            self.blockers()
            if self.visible('[id*="_spnSeatStockSymbolClass_"]'):break
            if not entry_clicked:
                entry=self.event_seat_entry()
                if entry is not None:
                    self.log("対象開催日の指定席受付から席種一覧へ進みます。まだ席の申し込みは送信していません。")
                    self.click(entry);entry_clicked=True;end=time.monotonic()+15
                    continue
            if self.full_entry_visible(reserved_only=True):
                raise ListingPending("指定開催日は満席のため席種一覧を開けません。希望条件を保持して受付再開を確認します。")
            if time.monotonic()>=end:
                self.event('attention_required')
                raise Halt("開催日の画面を開きましたが、席種一覧への追加操作が必要です。画面の操作一覧で内容を確認してください。申し込みは送信していません。")
            self.stop.wait(.15)
        self.verify_booking(target)
        self.booking=target
        return self.capture()

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
        self.event("reserving")
        self.blockers()
        if getattr(self, "booking", None):
            self.verify_booking(self.booking)
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
                checks = self.notice_checks(panel)
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
                        self.log("次へ進むの識別情報: " + json.dumps(self.notice_diagnostics(panel), ensure_ascii=False),)
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
        selector = "*"
        candidates = []
        scope = panel
        # Some templates place the footer beside the content panel. Stay inside
        # a small modal ancestor, never broaden the search to the seat page/body.
        for _ in range(3):
            if any('次へ進む' in normalized(e.text or e.get_attribute('value') or e.get_attribute('alt') or '')
                   for e in scope.find_elements(By.CSS_SELECTOR, selector) if e.is_displayed()):
                break
            parent = scope.find_element(By.XPATH, '..')
            if parent.tag_name.lower() in ('body','html','form') or len(parent.find_elements(By.CSS_SELECTOR,"[id*='_spnSeatStockSymbolClass_']")):
                break
            scope = parent
        for element in scope.find_elements(By.CSS_SELECTOR, selector):
            if not element.is_displayed():
                continue
            caption = element.text or element.get_attribute('value') or element.get_attribute('alt') or ''
            caption = re.sub(r'[\ue000-\uf8ff]', '', normalized(caption))
            if caption.strip('›»→❯〉＞>') == '次へ進む':
                candidates.append(element)
        # A label/wrapper and its link can both expose the same caption.
        # Prefer an actual button/link; never guess between separate controls.
        primary = [e for e in candidates if e.tag_name.lower() in ('a', 'button', 'input')]
        if primary:
            result=[e for e in candidates if not any(e != p and self.driver.execute_script(
                'return arguments[0].contains(arguments[1]) || arguments[1].contains(arguments[0])', e, p)
                for p in primary)]
        else:
            result=[e for e in candidates if not any(e != other and self.driver.execute_script(
                'return arguments[0].contains(arguments[1])', e, other) for other in candidates)]
        if len(result)==1:
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center',inline:'nearest'})",result[0])
            if not self.driver.execute_script(FRONT_JS+'return front.contains(arguments[0]) && uncovered(arguments[0]);',result[0]):return []
        return result

    def notice_diagnostics(self, panel):
        # Only structural information for next controls, never field values or
        # notice contents, account identifiers, URLs, or payment information.
        return self.driver.execute_script("""const p=arguments[0];return [...document.querySelectorAll('button,a,input,label,span,div')]
          .filter(e=>e.getClientRects().length && (e.innerText||e.value||e.alt||'').replace(/\\s/g,'').includes('次へ進む'))
          .filter(e=>(e.innerText||e.value||e.alt||'').length<80).slice(0,12)
          .map(e=>({tag:e.tagName,type:e.getAttribute('type'),inside:p.contains(e),
             inParent:!!p.parentElement?.contains(e),disabled:!!e.disabled,
             captionLength:(e.innerText||e.value||e.alt||'').trim().length}));""", panel)

    def run(self, wishes, count, area, start, watch, interval, minutes, booking=None):
        self.booking = booking
        if not wishes:raise Halt("希望席を1つ以上選んでください。")
        if interval < 10 or not 1 <= minutes <= 120 or not 1 <= count <= 4:
            raise Halt("確認間隔は10秒以上、監視時間は1〜120分、枚数は1〜4席です。")
        while dt.datetime.now(JST) < start:
            self.check()
            self.stop.wait(min(0.5,(start-dt.datetime.now(JST)).total_seconds()))
        end=time.monotonic()+minutes*60
        if booking:
            transient_failures=0
            recover_search=False
            while True:
                self.check()
                try:
                    if recover_search:
                        self.load_event_search()
                        recover_search=False
                    self.prepare_booking(booking)
                    break
                except (ListingConnectionLost,StaleElementReferenceException) as exc:
                    transient_failures+=1
                    reason='通信切断' if isinstance(exc,ListingConnectionLost) else '画面更新'
                    if not watch or transient_failures>=3:
                        raise Halt(f"申し込み前の開催確認が{reason}で停止しました。ブラウザを確認して再開してください。席の申し込みは送信していません。") from None
                    delay=max(interval,30)*transient_failures
                    if time.monotonic()+delay>=end:
                        raise Halt("開催確認の復旧待ちが監視期限を超えるため終了しました。席の申し込みは送信していません。") from None
                    self.log(f"申し込み前の開催確認で{reason}を検出しました。{delay}秒後に開催一覧の取得から再確認します（復旧{transient_failures}/2回）。")
                    self.event('retry_wait')
                    self.stop.wait(delay)
                    recover_search=True
                except ListingPending as exc:
                    transient_failures=0
                    if not watch:raise Unavailable(str(exc)+" 定期確認が無効のため終了しました。") from None
                    if time.monotonic()+interval>=end:raise Unavailable("監視時間内に受付が再開せず終了しました。") from None
                    self.log(f"開催日の入口が満席です。希望席・枚数を保持し、{interval}秒後に受付再開を確認します。")
                    self.event('retry_wait')
                    self.stop.wait(interval)
            names={normalized(s['name']) for s in self.seats()}
            if any(normalized(w) not in names for w in wishes):
                raise Halt("指定した席種が対象開催の一覧にありません。席種名を確認してください。")
        if not self.target:raise Halt("対象の席種一覧が未取得です。競馬場と開催日を指定してください。")
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
            if booking:self.advance_event_notice()
            if booking and not self.visible('[id*="_spnSeatStockSymbolClass_"]') and self.full_entry_visible():
                seats=[]
            else:
                if booking:self.verify_booking(booking)
                seats=self.seats()
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
