"""Restart-safe device certificates; authentication also requires the device's private key.

No login secret or private device key is stored in this certificate. A copied cookie
alone cannot restore a session. The browser keeps its non-exportable signing key.
"""
import base64
import hashlib
import hmac
from http import cookies
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time

COOKIE='jra_phone_device'
LIFETIME=30*24*3600

def crypto(payload):
    startup=None
    if os.name=='nt':
        startup=subprocess.STARTUPINFO()
        startup.dwFlags|=subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow=0
    result=subprocess.run(['node',str(Path(__file__).with_name('auth_device.mjs'))],
        input=json.dumps(payload),text=True,encoding='utf-8',stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,timeout=8,startupinfo=startup)
    return result.returncode==0 and json.loads(result.stdout).get('valid') is True

class DeviceAuth:
    def __init__(self,secret):
        self.key=hmac.new(secret.encode(),b'seat-device-certificate-v1',hashlib.sha256).digest()
        self.lock=threading.Lock()
        self.challenges={}
        self.revoked={}

    @staticmethod
    def public_key(value):
        if not isinstance(value,dict) or set(value)-{'kty','crv','x','y','ext','key_ops'}:
            raise ValueError('端末の登録情報を確認できませんでした。')
        if value.get('kty')!='EC' or value.get('crv')!='P-256' or any(
            not isinstance(value.get(k),str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',value[k]) for k in ('x','y')):
            raise ValueError('端末の登録情報を確認できませんでした。')
        return {k:value[k] for k in ('kty','crv','x','y')}

    def issue(self,public_key):
        key=self.public_key(public_key)
        if not crypto({'operation':'validate','key':key}):
            raise ValueError('この端末のログイン保持を設定できませんでした。')
        value={'v':1,'key':key,'exp':int(time.time())+LIFETIME,'id':secrets.token_hex(16)}
        encoded=base64.urlsafe_b64encode(json.dumps(value,separators=(',',':')).encode()).decode().rstrip('=')
        signature=hmac.new(self.key,encoded.encode(),hashlib.sha256).hexdigest()
        return encoded+'.'+signature,value['exp']

    def certificate(self,header):
        try:
            jar=cookies.SimpleCookie(header)
            raw=jar[COOKIE].value
            if len(raw)>1500:return None
            encoded,signature=raw.split('.')
            expected=hmac.new(self.key,encoded.encode(),hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected,signature):return None
            value=json.loads(base64.urlsafe_b64decode(encoded+'='*(-len(encoded)%4)))
            if value.get('v')!=1 or type(value.get('exp')) is not int or value['exp']<=time.time():return None
            self.public_key(value['key'])
            with self.lock:
                self.revoked={k:v for k,v in self.revoked.items() if v>time.time()}
                if value['id'] in self.revoked:return None
            return value
        except (KeyError,ValueError,TypeError,cookies.CookieError):
            return None

    def challenge(self,header,origin):
        cert=self.certificate(header)
        if not cert:raise ValueError('この端末のログイン保持は期限切れか未設定です。')
        nonce=secrets.token_urlsafe(32)
        message='SeatHelper device login v1\n'+origin+'\n'+nonce
        with self.lock:
            self.challenges={k:v for k,v in self.challenges.items() if v['exp']>time.time()}
            if len(self.challenges)>=128:raise ValueError('少し待ってからログインしてください。')
            self.challenges[nonce]={'exp':time.time()+90,'id':cert['id'],'message':message}
        return {'challenge':nonce,'message':message}

    def authenticate(self,header,data):
        cert=self.certificate(header)
        nonce,signature=data.get('challenge'),data.get('signature')
        if not cert or not isinstance(nonce,str) or not isinstance(signature,str) or not re.fullmatch(r'[A-Za-z0-9_-]{86}',signature):return False
        with self.lock:
            challenge=self.challenges.pop(nonce,None)
        if not challenge or challenge['id']!=cert['id'] or challenge['exp']<=time.time():return False
        return crypto({'operation':'verify','key':cert['key'],'message':challenge['message'],'signature':signature})

    def revoke(self,header):
        cert=self.certificate(header)
        if cert:
            with self.lock:self.revoked[cert['id']]=cert['exp']
