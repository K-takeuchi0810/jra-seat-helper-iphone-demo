// Only a non-exportable device signing key lives here, never the app login key.
const DB='seat-helper-device-v1';
let restoreAttempted=false,restorePromise=null,lastRestoreAt=0;
async function storage(mode,operation){
  const db=await new Promise((resolve,reject)=>{const r=indexedDB.open(DB,1);r.onupgradeneeded=()=>r.result.createObjectStore('keys');r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error);});
  try{return await new Promise((resolve,reject)=>{const tx=db.transaction('keys',mode);const request=operation(tx.objectStore('keys'));tx.oncomplete=()=>resolve(request?.result);tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error);});}finally{db.close();}
}
async function request(path,body){
  const response=await fetch(path,{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json','X-Seat-Request':'1'},body:JSON.stringify(body)});
  const value=await response.json();if(!response.ok)throw new Error(value.error||'ログイン保持を確認できませんでした。');return value;
}
export async function rememberDevice(secret){
  if(!isSecureContext||!crypto.subtle||!globalThis.indexedDB)throw new Error('この環境ではログイン保持に対応していません。');
  const pair=await crypto.subtle.generateKey({name:'ECDSA',namedCurve:'P-256'},false,['sign','verify']);
  const publicKey=await crypto.subtle.exportKey('jwk',pair.publicKey);
  await storage('readwrite',store=>store.put(pair.privateKey,'private'));
  try{await request('/api/device/register',{publicKey,secret});restoreAttempted=false;}catch(e){await forgetLocalDevice();throw e;}
}
export async function forgetLocalDevice(){
  restoreAttempted=true;
  try{await storage('readwrite',store=>store.delete('private'));}catch{}
}
export async function forgetDevice(){
  await forgetLocalDevice();await request('/api/device/forget',{});
}
export async function restoreDevice(){
  if(restorePromise)return restorePromise;
  if(restoreAttempted||Date.now()-lastRestoreAt<30000)return false;
  restoreAttempted=true;
  restorePromise=(async()=>{try{
    const key=await storage('readonly',store=>store.get('private'));if(!key)return false;
    const challenge=await request('/api/device/challenge',{});
    const signature=new Uint8Array(await crypto.subtle.sign({name:'ECDSA',hash:'SHA-256'},key,new TextEncoder().encode(challenge.message)));
    const encoded=btoa(String.fromCharCode(...signature)).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');
    await request('/api/device/login',{challenge:challenge.challenge,signature:encoded});lastRestoreAt=Date.now();restoreAttempted=false;return true;
  }catch{return false;}finally{restorePromise=null;}})();
  return restorePromise;
}
