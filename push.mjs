import crypto from 'node:crypto';
import webpush from 'web-push';
import {pathToFileURL} from 'node:url';

export function keys(seed) {
  if (typeof seed !== 'string' || seed.length < 32) throw new Error('secret required');
  const curve = crypto.createECDH('prime256v1');
  let raw = crypto.createHash('sha256').update('JRA phone push v1\0'+seed).digest();
  for (;;) {
    try {curve.setPrivateKey(raw); break;} catch {raw=crypto.createHash('sha256').update(raw).digest();}
  }
  return {privateKey: curve.getPrivateKey().toString('base64url'), publicKey: curve.getPublicKey().toString('base64url')};
}

export function validSubscription(subscription) {
  const url = new URL(subscription.endpoint);
  const hosts = ['fcm.googleapis.com', 'updates.push.services.mozilla.com'];
  const apple = url.hostname === 'web.push.apple.com' || url.hostname.endsWith('.push.apple.com');
  if (url.protocol !== 'https:' || url.username || url.password || (url.port && url.port !== '443') ||
      !(apple || hosts.includes(url.hostname)) || url.href.length > 2048) throw new Error('invalid push endpoint');
  if (!/^[A-Za-z0-9_-]+={0,2}$/.test(subscription.keys?.p256dh ?? '') ||
      !/^[A-Za-z0-9_-]+={0,2}$/.test(subscription.keys?.auth ?? '') ||
      Buffer.from(subscription.keys.p256dh,'base64url').length !== 65 ||
      Buffer.from(subscription.keys.auth,'base64url').length !== 16) throw new Error('invalid push keys');
  return true;
}

async function main() {
  let text='';
  for await (const chunk of process.stdin) {text+=chunk;if(text.length>16000)throw new Error('too large');}
  const input=JSON.parse(text);
  const vapid=keys(input.seed);
  if(input.operation==='publicKey') return {publicKey:vapid.publicKey};
  validSubscription(input.subscription);
  if(input.operation==='validate') return {valid:true};
  if(input.operation!=='send') throw new Error('unsupported operation');
  try {
    const result=await webpush.sendNotification(input.subscription,JSON.stringify(input.message),{
      TTL:300,urgency:'high',timeout:12000,
      vapidDetails:{subject:input.subject,...vapid}
    });
    return {accepted:result.statusCode>=200 && result.statusCode<300,status:result.statusCode};
  } catch(error) {return {accepted:false,status:Number(error.statusCode)||0};}
}

if(process.argv[1] && import.meta.url===pathToFileURL(process.argv[1]).href) {
  main().then(result=>process.stdout.write(JSON.stringify(result)))
    .catch(()=>{process.stdout.write(JSON.stringify({accepted:false,status:0}));process.exitCode=1;});
}
