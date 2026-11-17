import base64
import json
import subprocess
import unittest
from unittest.mock import patch
from device_auth import DeviceAuth
import test_backend as base_tests
SECRET=base_tests.SECRET

class DeviceAuthTests(unittest.TestCase):
    setUp=base_tests.APITests.setUp
    tearDown=base_tests.APITests.tearDown
    request=base_tests.APITests.request
    login=base_tests.APITests.login
    @classmethod
    def setUpClass(cls):
        cls.keys=json.loads(subprocess.check_output(['node','-e',"const c=require('node:crypto');const p=c.generateKeyPairSync('ec',{namedCurve:'prime256v1'});process.stdout.write(JSON.stringify({public:p.publicKey.export({format:'jwk'}),private:p.privateKey.export({format:'jwk'})}))"],text=True))
    def sign(self,message):
        script="let s='';process.stdin.on('data',x=>s+=x);process.stdin.on('end',()=>{const v=JSON.parse(s),c=require('node:crypto');const key=c.createPrivateKey({key:v.key,format:'jwk'});process.stdout.write(c.sign('sha256',Buffer.from(v.message),{key,dsaEncoding:'ieee-p1363'}).toString('base64url'))})"
        return subprocess.check_output(['node','-e',script],input=json.dumps({'key':self.keys['private'],'message':message}),text=True)
    def registered(self):
        session=self.login()
        status,_,headers=self.request('/api/device/register',{'publicKey':self.keys['public'],'secret':SECRET},cookie=session)
        self.assertEqual(status,200)
        cookie=headers['Set-Cookie'].split(';')[0]
        self.assertNotIn(SECRET,cookie)
        self.assertIn('HttpOnly',headers['Set-Cookie']);self.assertIn('SameSite=Strict',headers['Set-Cookie'])
        return session,cookie
    def challenge(self,cookie):
        status,body,_=self.request('/api/device/challenge',{},cookie=cookie)
        self.assertEqual(status,200)
        return json.loads(body)
    def proof(self,challenge):return {'challenge':challenge['challenge'],'signature':self.sign(challenge['message'])}
    def test_cookie_alone_cannot_restore_but_device_proof_survives_auth_restart(self):
        session,device=self.registered()
        self.app.sessions.clear();self.app.devices=DeviceAuth(SECRET)
        self.assertEqual(self.request('/api/state',cookie=session)[0],401)
        self.assertEqual(self.request('/api/state',cookie=device)[0],401)
        challenge=self.challenge(device)
        status,_,headers=self.request('/api/device/login',self.proof(challenge),cookie=device)
        self.assertEqual(status,200)
        self.assertEqual(self.request('/api/state',cookie=headers['Set-Cookie'].split(';')[0])[0],200)
    def test_challenges_are_single_use_and_bad_signatures_cannot_log_in(self):
        _,device=self.registered();challenge=self.challenge(device);proof=self.proof(challenge)
        self.assertEqual(self.request('/api/device/login',proof,cookie=device)[0],200)
        self.assertEqual(self.request('/api/device/login',proof,cookie=device)[0],401)
        challenge=self.challenge(device)
        self.assertEqual(self.request('/api/device/login',{'challenge':challenge['challenge'],'signature':'A'*86},cookie=device)[0],401)
    def test_expired_challenge_is_rejected(self):
        _,device=self.registered();challenge=self.challenge(device)
        self.app.devices.challenges[challenge['challenge']]['exp']=0
        self.assertEqual(self.request('/api/device/login',self.proof(challenge),cookie=device)[0],401)
    def test_logout_revokes_session_and_remembered_certificate(self):
        session,device=self.registered()
        self.assertEqual(self.request('/api/logout',{},cookie=session+'; '+device)[0],200)
        self.assertEqual(self.request('/api/state',cookie=session)[0],401)
        self.assertEqual(self.request('/api/device/challenge',{},cookie=device)[0],401)
    def test_registration_needs_login_and_rejects_private_key_material(self):
        self.assertEqual(self.request('/api/device/register',{'publicKey':self.keys['public'],'secret':SECRET})[0],401)
        session=self.login()
        self.assertEqual(self.request('/api/device/register',{'publicKey':self.keys['private'],'secret':SECRET},cookie=session)[0],400)
    def test_a_session_cookie_without_the_login_secret_cannot_enroll_a_device(self):
        session=self.login()
        self.assertEqual(self.request('/api/device/register',{'publicKey':self.keys['public']},cookie=session)[0],401)

    def test_device_registration_and_restoration_reject_cross_origin(self):
        session,device=self.registered()
        self.assertEqual(self.request('/api/device/register',{'publicKey':self.keys['public'],'secret':SECRET},cookie=session,origin='https://other.example')[0],403)
        self.assertEqual(self.request('/api/device/challenge',{},cookie=device,origin='https://other.example')[0],403)
    def test_tampered_or_expired_certificate_is_rejected(self):
        _,device=self.registered()
        self.assertIsNone(self.app.devices.certificate(device[:-1]+('1' if device[-1]!='1' else '2')))
        with patch('device_auth.time.time',return_value=1):
            token,_=self.app.devices.issue(self.keys['public'])
        self.assertIsNone(self.app.devices.certificate('jra_phone_device='+token))

if __name__=='__main__':unittest.main()
