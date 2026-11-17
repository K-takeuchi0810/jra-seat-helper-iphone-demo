import {test} from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import webpush from 'web-push';
import {keys,validSubscription} from '../push.mjs';

test('VAPID keys survive process restarts with the same dedicated secret',()=>{
 const first=keys('a'.repeat(48)),second=keys('a'.repeat(48));
 assert.deepEqual(first,second);assert.notDeepEqual(first,keys('b'.repeat(48)));
 assert.equal(Buffer.from(first.publicKey,'base64url').length,65);
 assert.equal(Buffer.from(first.privateKey,'base64url').length,32);
});

function subscription(host='web.push.apple.com'){
 const receiver=crypto.createECDH('prime256v1');receiver.generateKeys();
 return {endpoint:`https://${host}/test-not-a-real-subscription`,keys:{p256dh:receiver.getPublicKey().toString('base64url'),auth:crypto.randomBytes(16).toString('base64url')}};
}

test('push destinations exclude local networks, credentials, alternate ports and lookalike hosts',()=>{
 for(const host of ['127.0.0.1','169.254.169.254','web.push.apple.com.attacker.example','localhost'])assert.throws(()=>validSubscription(subscription(host)));
 const value=subscription();value.endpoint='https://user:pass@web.push.apple.com/x';assert.throws(()=>validSubscription(value));
 value.endpoint='https://web.push.apple.com:8443/x';assert.throws(()=>validSubscription(value));
});

test('real Web Push library encrypts the notification payload without a network request',()=>{
 const value=subscription();assert.equal(validSubscription(value),true);
 const pair=keys('c'.repeat(48));
 const result=webpush.generateRequestDetails(value,JSON.stringify({title:'確認待ち',body:'購入は完了していません。'}),{
   vapidDetails:{subject:'https://seat-helper.example',...pair},TTL:300
 });
 assert.equal(result.endpoint,value.endpoint);assert.equal(result.headers['Content-Encoding'],'aes128gcm');
 assert.ok(result.headers.Authorization);assert.ok(Buffer.isBuffer(result.body));
 assert.ok(!result.body.includes(Buffer.from('確認待ち')));
});

test('malformed subscription keys are rejected before sending',()=>{
 const value=subscription();value.keys.auth='bad';assert.throws(()=>validSubscription(value));
});
