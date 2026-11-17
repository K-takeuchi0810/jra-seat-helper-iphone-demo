import {rememberDevice,restoreDevice,forgetDevice,forgetLocalDevice} from './device-auth.js';
const $=id=>document.getElementById(id);
let state=null,tab='home',lastFrame=-1,lastSeats='',pendingJob=null,polling=false,toastTimer=null,manualInputBusy=false;
const phases={idle:'準備前',loading:'画面を読み込み中',ready:'条件を設定できます',waiting:'開始日時まで待機中',retrying:'次の再確認まで待機中',running:'申し込み処理中',attention:'本人による操作待ち',payment:'購入手続きの確認待ち',stopping:'中止を要求しました',stopped:'処理を停止しました',unavailable:'この条件では確保できませんでした',error:'画面の確認が必要です'};
function toast(text){$('toast').textContent=text;$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,6500);}
async function api(path,data){const options={credentials:'same-origin',cache:'no-store'};if(data!==undefined){options.method='POST';options.headers={'Content-Type':'application/json','X-Seat-Request':'1'};options.body=JSON.stringify(data);}
 let response=await fetch(path,options);if(response.status===401&&path!=='/api/login'&&path!=='/api/logout'&&await restoreDevice()){pushSyncStarted=false;lastFrame=-1;response=await fetch(path,options);}let result;try{result=await response.json();}catch{throw new Error('サーバーに接続できません。少し待ってから画面を確認してください。');}
 if(!response.ok){if(response.status===401){$('loginView').hidden=false;$('appView').hidden=true;$('bottomNav').hidden=true;state=null;}throw new Error(result.error||'操作を完了できませんでした。');}return result;}
function changeTab(value){tab=value;document.querySelectorAll('[data-view]').forEach(e=>e.hidden=e.dataset.view!==tab);document.querySelectorAll('nav button').forEach(e=>{e.classList.toggle('active',e.dataset.tab===tab);e.setAttribute('aria-current',e.dataset.tab===tab?'page':'false');});window.scrollTo({top:0});if(tab==='browser')updateFrame();}
function option(value,label=value){const e=document.createElement('option');e.value=value;e.textContent=label;return e;}
function jstInput(date){return new Date(date.getTime()+9*3600000).toISOString().slice(0,19);}
let pushSyncStarted=false;
async function syncExistingPush(){if(pushSyncStarted)return;pushSyncStarted=true;try{if('Notification'in window&&Notification.permission==='granted'&&'serviceWorker'in navigator){const registration=await navigator.serviceWorker.ready;const subscription=await registration.pushManager.getSubscription();if(subscription)await api('/api/push/subscribe',{subscription:subscription.toJSON()});}}catch{}}
function render(s){state=s;syncExistingPush();$('loginView').hidden=true;$('appView').hidden=false;$('bottomNav').hidden=false;$('modeBadge').textContent=s.demo?'デモモード':'実サイト';
 $('statusLabel').textContent=phases[s.phase]||'状況を確認してください';$('updatedAt').textContent=new Intl.DateTimeFormat('ja-JP',{hour:'2-digit',minute:'2-digit',timeZone:'Asia/Tokyo'}).format(new Date());
 $('statusDetail').textContent=s.detail||s.logs.at(-1)?.text||'ブラウザを開き、対象の競馬場と開催日を確認してください。';$('stopButton').hidden=!s.busy;
 $('openBrowser').disabled=s.busy||s.frame>0;$('captureSeats').disabled=s.busy||s.frame===0;$('reviewJob').disabled=s.busy||!s.targetReady;$('closeBrowser').disabled=s.busy||s.frame===0;
 const note=s.profile==='render-free'?'無料Renderでは、アクセスが15分ないとサーバーが停止する場合があります。iPhoneを閉じたままの長時間待機は保証できません。':s.profile==='local'?'作成中のローカルデモです。iPhoneの実機通知とクラウドの長時間継続はまだ確認していません。':'';
 $('hostingNotice').textContent=note;$('hostingNotice').hidden=!note;
 $('remoteHost').textContent=s.host||'未接続';$('frameAge').textContent=s.frameAt?`${Math.max(0,Math.floor(Date.now()/1000-s.frameAt))}秒前の画面`:'—';
 const manual=['ready','attention','payment','stopped','unavailable','error'].includes(s.phase);document.querySelectorAll('.remote-controls button,#sendText,#clearRemoteText').forEach(e=>e.disabled=!manual||manualInputBusy);
 $('remoteFrame').classList.toggle('input-pending',manualInputBusy||!manual);$('jraLoginPanel').hidden=!s.loginReady;document.querySelectorAll('#jraLoginForm input,#jraLoginForm button').forEach(e=>e.disabled=!manual||manualInputBusy);
 const diag=s.browserDiagnostics;$('browserDiagnostics').textContent=!diag?'ブラウザを開くと状態を表示します。':diag.error?'画面情報を取得できませんでした。決済の成否は未判定です。':`ページ状態：${diag.readyState==='complete'?'読み込み済み':diag.readyState==='interactive'?'表示準備中':'読み込み中'} ／ 開いている画面：${diag.windows} ／ 埋め込み画面：${diag.embeddedFrames}。本人認証や購入の完了を示すものではありません。`;
 if(diag?.displayMode)$('browserDiagnostics').textContent+=' ブラウザ：'+(diag.displayMode==='headed'?'通常表示':diag.displayMode==='headless'?'ヘッドレス':'不明')+'。';
 if(diag?.frames?.length)$('browserDiagnostics').textContent+=' '+diag.frames.map((f,i)=>`埋込${i+1}：${f.actualHost||f.sourceHost||'接続先未確定'}（${f.visible===false?'非表示・':''}${f.error?'取得エラー':f.readyState}）${f.serviceCodes?.length?' ['+f.serviceCodes.join(', ')+']':''}`).join(' ／ ');
 if(diag?.networkErrors?.length)$('browserDiagnostics').textContent+=' 通信エラー：'+diag.networkErrors.map(e=>`${e.host||'接続先不明'} ${e.method||''} ${e.type||''} ${e.status||e.error}${e.serviceCodes?.length?' ['+e.serviceCodes.join(', ')+']':''}${e.responseDetails?' '+e.responseDetails.format+' '+(e.responseDetails.categories||[]).join(',')+' '+(e.responseDetails.mentionedFields||[]).join(','):''}`).join(' ／ ');
 if(diag?.scriptErrors?.length)$('browserDiagnostics').textContent+=' スクリプト診断：'+diag.scriptErrors.map(e=>[e.sourceHost,e.category,e.symbol,e.syntaxKind].filter(Boolean).join(' ')).join(' ／ ');
 $('inspectBrowser').disabled=!manual||manualInputBusy||!s.frame;
 const windows=s.browserWindows||[];$('browserWindowRow').hidden=windows.length<2;
 const wk=JSON.stringify(windows);if($('browserWindow').dataset.key!==wk){$('browserWindow').replaceChildren(...windows.map(w=>{const e=option(w.token,w.label);e.selected=w.selected;return e;}));$('browserWindow').dataset.key=wk;}$('browserWindow').disabled=!manual||manualInputBusy;
 const select=s.activeSelect;$('remoteSelectRow').hidden=!select;if(select){const key=JSON.stringify([select.token,select.options,select.disabled]);if($('remoteSelect').dataset.key!==key){$('remoteSelect').replaceChildren(...select.options.map((text,index)=>{const e=option(String(index),text);e.disabled=!!select.disabled?.[index];return e;}));$('remoteSelect').dataset.key=key;}$('remoteSelect').value=String(select.selected);$('remoteSelect').disabled=!manual;}
 const offered=s.seats.filter(v=>v.available);
 renderPageFields(s.formFields||[],manual);
 const seatsKey=JSON.stringify(offered);if(seatsKey!==lastSeats){lastSeats=seatsKey;['wish1','wish2','wish3'].forEach((id,i)=>{const current=$(id).value;$(id).replaceChildren(option('',i===0?'希望席を選択':'指定しない'),...offered.map(v=>option(v.name)));if(offered.some(v=>v.name===current))$(id).value=current;});if(offered.length&&!$('wish1').value)$('wish1').value=offered[0].name;updateAreas();}
 $('seatsReadInfo').textContent=s.seats.length?`${offered.length}種類が受付可能（読み込み${s.seats.length}種類）。競馬場・開催日をブラウザで確認してください。`:'席種はまだ読み込んでいません。';
 $('pushDetail').textContent=s.push.detail;$('confirmPush').disabled=!s.push.testId||s.push.received;$('pushSummary').textContent=s.push.received?'通知の到着を確認済み':s.subscribers?'通知登録済み・到着テストをしてください':'通知を準備しましょう';
 const logs=s.logs.slice().reverse();$('activityLog').replaceChildren(...logs.map(item=>{const li=document.createElement('li'),time=document.createElement('time'),p=document.createElement('p');time.textContent=item.time;p.textContent=item.text;li.append(time,p);return li;}));
 $('pushWarning').hidden=s.push.received;updateFrame();}
function updateAreas(){const selected=state?.seats.find(s=>s.name===$('wish1').value);$('areas').replaceChildren(...(selected?.areas||[]).map(v=>option(v)));}
function updateFrame(){if(!state)return;if(!state.frame){lastFrame=-1;$('remoteFrame').hidden=true;$('frameEmpty').hidden=false;return;}if(tab==='browser'&&lastFrame!==state.frame){lastFrame=state.frame;$('remoteFrame').src=`/api/frame?v=${state.frame}`;$('remoteFrame').hidden=false;$('frameEmpty').hidden=true;}}
async function poll(){if(polling||document.hidden)return;polling=true;try{render(await api('/api/state'));$('offlineNotice').hidden=true;}catch(e){if(state)$('offlineNotice').hidden=false;}finally{polling=false;}}
async function action(path,data={}){try{await api(path,data);await poll();return true;}catch(e){toast(e.message);return false;}}
$('loginForm').addEventListener('submit',async e=>{e.preventDefault();const remember=$('rememberLogin').checked,secret=$('loginSecret').value;if(await action('/api/login',{secret})){$('loginSecret').value='';try{if(remember){await rememberDevice(secret);toast('この端末で30日間、ログインを復元できるよう設定しました。');}else await forgetDevice();}catch{toast('ログインはできましたが、この端末での保持を設定できませんでした。保存済みパスワードをお使いください。');}await poll();}});
document.querySelectorAll('[data-tab]').forEach(b=>b.addEventListener('click',()=>changeTab(b.dataset.tab)));
$('openBrowser').addEventListener('click',async()=>{if(await action('/api/browser/open'))changeTab('browser');});
$('captureSeats').addEventListener('click',()=>action('/api/browser/capture'));
$('watch').addEventListener('change',()=>{$('watchOptions').hidden=!$('watch').checked;});$('wish1').addEventListener('change',updateAreas);
$('jobForm').addEventListener('submit',e=>{e.preventDefault();if(!state?.targetReady)return;pendingJob={wishes:[1,2,3].map(i=>$('wish'+i).value).filter(Boolean),count:Number($('count').value),area:$('area').value.trim(),start:$('startTime').value.replace('T',' '),watch:$('watch').checked,interval:Number($('interval').value),minutes:Number($('minutes').value)};
 if(pendingJob.start.length===16)pendingJob.start+=':00';$('jobSummary').textContent=`希望順：${pendingJob.wishes.join(' → ')}\n枚数：${pendingJob.count}席\nエリア：${pendingJob.area||'指定なし'}\n開始：${pendingJob.start} 日本時間\n確認：${pendingJob.watch?`${pendingJob.interval}秒ごと／最大${pendingJob.minutes}分`:'1回のみ'}`;$('venueConfirmed').checked=false;$('confirmStart').disabled=true;$('confirmDialog').showModal();});
$('venueConfirmed').addEventListener('change',()=>{$('confirmStart').disabled=!$('venueConfirmed').checked;});$('cancelReview').addEventListener('click',()=>$('confirmDialog').close());
$('confirmStart').addEventListener('click',async()=>{if(!pendingJob||!$('venueConfirmed').checked)return;$('confirmStart').disabled=true;if(await action('/api/run',{...pendingJob,confirmed:true})){$('confirmDialog').close();toast('開始日時まで待機します。処理状況はホームで確認できます。');}else $('confirmStart').disabled=false;});
$('stopButton').addEventListener('click',async()=>{if(await action('/api/stop'))toast('中止を要求しました。送信済みの申し込みは取り消されません。');});
$('remoteFrame').addEventListener('click',e=>{if(!state)return;const rect=e.currentTarget.getBoundingClientRect();action('/api/browser/manual',{kind:'tap',x:(e.clientX-rect.left)*state.width/rect.width,y:(e.clientY-rect.top)*state.height/rect.height});});
for(const [id,data] of [['scrollUp',{kind:'scroll',delta:-450}],['scrollDown',{kind:'scroll',delta:450}],['remoteEnter',{kind:'key',key:'Enter'}],['remoteTab',{kind:'key',key:'Tab'}]])$(id).addEventListener('click',()=>action('/api/browser/manual',data));
async function inputCommand(data){
 if(manualInputBusy)return false;manualInputBusy=true;if(state)render(state);
 try{const requestId=crypto.randomUUID();await api('/api/browser/manual',{...data,requestId});const until=Date.now()+20000;
  while(Date.now()<until){const s=await api('/api/state');render(s);if(s.manualResult?.id===requestId){if(!s.manualResult.ok)throw new Error(s.manualResult.detail);toast(s.manualResult.detail);return true;}await new Promise(resolve=>setTimeout(resolve,400));}
  throw new Error('操作の反映を確認できませんでした。ブラウザを確認してください。自動再送はしません。');
 }catch(e){toast(e.message);return false;}finally{manualInputBusy=false;if(state)render(state);}
}
$('showRemoteText').addEventListener('change',()=>{$('remoteText').type=$('showRemoteText').checked?'text':'password';});
$('sendText').addEventListener('click',async()=>{const value=$('remoteText').value;if(!value)return;if(await inputCommand({kind:'type',value}))$('remoteText').value='';});
$('clearRemoteText').addEventListener('click',async()=>{if(await inputCommand({kind:'clear'}))$('remoteText').value='';});
$('jraLoginForm').addEventListener('submit',async e=>{e.preventDefault();if($('jraUsername').value==='seat-helper'){toast('JRAの保存項目を選んでください。アプリ用のログイン鍵はここでは使いません。');return;}if(await inputCommand({kind:'login_fill',username:$('jraUsername').value,password:$('jraPassword').value})){$('jraUsername').value='';$('jraPassword').value='';}});
$('clearJraLogin').addEventListener('click',async()=>{if(await inputCommand({kind:'login_clear'})){$('jraUsername').value='';$('jraPassword').value='';}});
$('remoteSelect').addEventListener('change',()=>action('/api/browser/manual',{kind:'select',index:Number($('remoteSelect').value),token:state?.activeSelect?.token}));
$('closeBrowser').addEventListener('click',()=>{if(confirm('ブラウザを終了しますか？ ログイン状態と表示中の画面を失います。'))action('/api/browser/close');});
$('logout').addEventListener('click',async()=>{await forgetLocalDevice();if(await action('/api/logout')){state=null;$('appView').hidden=true;$('bottomNav').hidden=true;$('loginView').hidden=false;}});
function base64url(value){const raw=atob(value.replace(/-/g,'+').replace(/_/g,'/'));return Uint8Array.from(raw,c=>c.charCodeAt(0));}
const isiOS=/iPad|iPhone|iPod/.test(navigator.userAgent),standalone=window.matchMedia('(display-mode: standalone)').matches||navigator.standalone===true;
$('standaloneState').textContent=standalone?'ホーム画面から起動しています':isiOS?'Safariの共有メニューから追加してください':'iPhoneではホーム画面に追加してから通知を設定します';
$('enablePush').addEventListener('click',async()=>{if(!state)return;if(isiOS&&!standalone){toast('先にホーム画面に追加し、追加したアイコンから開き直してください。');return;}if(!window.isSecureContext||!('PushManager'in window)||!('Notification'in window)){toast('この環境では通知に対応していません。HTTPSとiOS 16.4以降のホーム画面アプリで開いてください。');return;}
 try{const permission=await Notification.requestPermission();if(permission!=='granted')throw new Error('通知が許可されませんでした。端末の設定を確認してください。');const registration=await navigator.serviceWorker.ready;let subscription=await registration.pushManager.getSubscription();if(!subscription)subscription=await registration.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:base64url(state.pushKey)});await api('/api/push/subscribe',{subscription:subscription.toJSON()});toast('通知を登録しました。次にテスト通知を送ってください。');await poll();}catch(e){toast(e.message);}});
$('testPush').addEventListener('click',()=>action('/api/push/test'));
$('confirmPush').addEventListener('click',()=>action('/api/push/received',{testId:state?.push.testId}));
if('serviceWorker'in navigator)navigator.serviceWorker.register('/sw.js').catch(()=>{});
$('startTime').value=jstInput(new Date(Date.now()+3*60000));
document.addEventListener('visibilitychange',()=>{if(!document.hidden)poll();});window.addEventListener('online',poll);poll();setTimeout(async function tick(){await poll();setTimeout(tick,tab==='browser'?700:2000);},700);

function renderPageFields(fields,manual){
 $('pageFieldsPanel').hidden=!fields.length;
 const key=JSON.stringify(fields.map(({selected,checked,...rest})=>rest));
 if($('pageFields').dataset.key!==key){
  $('pageFields').dataset.key=key;$('pageFields').replaceChildren();
  for(const f of fields){
   const row=document.createElement('div'),label=document.createElement('label'),control=document.createElement(f.type==='select'?'select':'input'),apply=document.createElement('button');
   row.dataset.caption=f.label;row.className='input-card';control.id='page-field-'+f.token;control.dataset.token=f.token;label.htmlFor=control.id;label.textContent=(f.sourceHost?f.sourceHost+'：':'')+f.label;
   if(['action','checkbox','radio'].includes(f.type)){
    apply.type='button';apply.className='secondary full';apply.dataset.checkToken=f.token;apply.textContent=(f.type==='checkbox'?(f.checked?'☑ ':'☐ '):f.type==='radio'?(f.checked?'● ':'○ '):'')+f.label;
    apply.addEventListener('click',()=>inputCommand({kind:'field',token:f.token,activate:true}));row.append(apply);$('pageFields').append(row);continue;
   }
   if(f.type==='select'){control.append(...f.options.map((o,i)=>{const e=option(String(i),o.text);e.disabled=o.disabled;return e;}));control.value=String(f.selected);}
   else {control.type=f.type;control.autocomplete='off';control.maxLength=1000;control.setAttribute('autocapitalize','none');control.spellcheck=false;}
   apply.type='button';apply.className='secondary';apply.textContent=f.type==='select'?'選択を反映':'入力を反映';
   apply.addEventListener('click',async()=>{const data=f.type==='select'?{index:Number(control.value)}:{value:control.value};if(await inputCommand({kind:'field',token:f.token,...data})){if(f.type!=='select')control.value='';}});
   row.append(label,control,apply);$('pageFields').append(row);
  }
 }
 fields.filter(f=>['checkbox','radio'].includes(f.type)).forEach(f=>{const e=$('pageFields').querySelector(`[data-check-token="${f.token}"]`);if(e)e.textContent=(f.type==='checkbox'?(f.checked?'☑ ':'☐ '):(f.checked?'● ':'○ '))+f.label;});
 $('pageFields').querySelectorAll('input,select,button').forEach(e=>e.disabled=!manual||manualInputBusy);
}

$('pageControlSearch').addEventListener('input',()=>{const query=$('pageControlSearch').value.trim();$('pageFields').querySelectorAll('[data-caption]').forEach(e=>e.hidden=!e.dataset.caption.includes(query));});

$('browserWindow').addEventListener('change',()=>inputCommand({kind:'window',token:$('browserWindow').value}));

$('inspectBrowser').addEventListener('click',()=>inputCommand({kind:'inspect'}));
