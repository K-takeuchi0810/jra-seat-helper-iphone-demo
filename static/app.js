import {rememberDevice,restoreDevice,forgetDevice,forgetLocalDevice} from './device-auth.js';
const $=id=>document.getElementById(id);
let cachedSeatCatalog=Object.create(null),lastLearnedCatalog='';try{const saved=JSON.parse(localStorage.getItem('seat-helper-seat-catalog-v1')||'{}');if(saved&&typeof saved==='object')for(const [course,entry] of Object.entries(saved)){if(entry&&Array.isArray(entry.names)&&entry.names.length<=100&&entry.names.every(n=>typeof n==='string'&&n.length>0&&n.length<=120))cachedSeatCatalog[course]=entry;}}catch{}
let state=null,tab='home',lastFrame=-1,lastSeats='',pendingJob=null,polling=false,toastTimer=null,manualInputBusy=false;
const phases={idle:'準備前',loading:'画面を読み込み中',ready:'条件を設定できます',waiting:'開始日時まで待機中',retrying:'次の再確認まで待機中',running:'申し込み処理中',attention:'本人による操作待ち',payment:'購入手続きの確認待ち',stopping:'中止を要求しました',stopped:'処理を停止しました',unavailable:'この条件では確保できませんでした',error:'画面の確認が必要です'};
function setText(id,text){if($(id).textContent!==text)$(id).textContent=text;}
function toast(text){$('toast').textContent=text;$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,6500);}
async function api(path,data){const options={credentials:'same-origin',cache:'no-store'};if(data!==undefined){options.method='POST';options.headers={'Content-Type':'application/json','X-Seat-Request':'1'};options.body=JSON.stringify(data);}
 let response=await fetch(path,options);if(response.status===401&&path!=='/api/login'&&path!=='/api/logout'&&await restoreDevice()){pushSyncStarted=false;lastFrame=-1;response=await fetch(path,options);}let result;try{result=await response.json();}catch{throw new Error('サーバーに接続できません。少し待ってから画面を確認してください。');}
 if(!response.ok){if(response.status===401){$('loginView').hidden=false;$('appView').hidden=true;$('bottomNav').hidden=true;$('modeBadge').textContent='未ログイン';state=null;}throw new Error(result.error||'操作を完了できませんでした。');}return result;}
function changeTab(value){tab=value;document.querySelectorAll('[data-view]').forEach(e=>e.hidden=e.dataset.view!==tab);document.querySelectorAll('nav button').forEach(e=>{e.classList.toggle('active',e.dataset.tab===tab);e.setAttribute('aria-current',e.dataset.tab===tab?'page':'false');});window.scrollTo({top:0});if(tab==='browser')updateFrame();}
function option(value,label=value){const e=document.createElement('option');e.value=value;e.textContent=label;return e;}
function jstInput(date){return new Date(date.getTime()+9*3600000).toISOString().slice(0,19);}
let pushSyncStarted=false;
async function syncExistingPush(){if(pushSyncStarted)return;pushSyncStarted=true;try{if('Notification'in window&&Notification.permission==='granted'&&'serviceWorker'in navigator){const registration=await navigator.serviceWorker.ready;const subscription=await registration.pushManager.getSubscription();if(subscription)await api('/api/push/subscribe',{subscription:subscription.toJSON()});}}catch{}}
function render(s){state=s;syncExistingPush();$('loginView').hidden=true;$('appView').hidden=false;$('bottomNav').hidden=false;$('modeBadge').textContent=s.demo?'デモモード':'実サイト';
 setText('statusLabel',phases[s.phase]||'状況を確認してください');$('statusCard').dataset.phase=s.phase;$('updatedAt').textContent='取得 '+new Intl.DateTimeFormat('ja-JP',{hour:'2-digit',minute:'2-digit',timeZone:'Asia/Tokyo'}).format(new Date());
 setText('statusDetail',s.detail||s.logs.at(-1)?.text||'ブラウザを開き、開催一覧を取得してください。');$('statusAction').hidden=!s.frame||!['stopped','error','attention','payment'].includes(s.phase);$('stopButton').hidden=!s.busy;const active=s.activeJob;$('activeJobSummary').hidden=!active;if(active)setText('activeJobText',`${active.booking?active.booking.date+'・'+active.booking.course+'競馬場\n':''}${active.wishes.join(' → ')}／${active.count}席／エリア：${active.area||'指定なし'}\n${active.startMode==='now'?'今すぐ開始':active.start+' 日本時間に開始'}${active.watch?'／'+active.interval+'秒ごと・最大'+active.minutes+'分':''}`);
 $('openBrowser').disabled=s.busy&&!s.frame;setText('openBrowser',s.frame?'ブラウザへ移動':'ブラウザを開く');$('loadCatalog').disabled=s.busy||s.frame===0||s.phase==='payment';$('closeBrowser').disabled=s.busy||s.frame===0;
 const note=s.profile==='render-free'?'無料Renderでは、アクセスが15分ないとサーバーが停止する場合があります。iPhoneを閉じたままの長時間待機は保証できません。':s.profile==='local'?'作成中のローカルデモです。iPhoneの実機通知とクラウドの長時間継続はまだ確認していません。':'';
 $('hostingNotice').textContent=note;$('hostingNotice').hidden=!note;$('hostingPanel').hidden=!note;
 setText('frameEmpty',s.busy?'ブラウザを準備しています。完了までお待ちください。':'ホームからブラウザを開いてください。');
 $('remoteHost').textContent=s.host||'未接続';$('frameAge').textContent=s.frameAt?`${Math.max(0,Math.floor(Date.now()/1000-s.frameAt))}秒前の画面`:'—';
 const manual=!!s.frame&&['ready','attention','payment','stopped','unavailable','error'].includes(s.phase);document.querySelectorAll('.remote-controls button,#sendText,#clearRemoteText').forEach(e=>e.disabled=!manual||manualInputBusy);
 $('remoteFrame').classList.toggle('input-pending',manualInputBusy||!manual);$('jraLoginPanel').hidden=!s.loginReady;document.querySelectorAll('#jraLoginForm input,#jraLoginForm button').forEach(e=>e.disabled=!manual||manualInputBusy);
 setText('browserTaskState',!s.frame?(s.busy?'ブラウザを準備しています。完了までお待ちください。':'ブラウザは開いていません。ホームから開いてください。'):s.busy?'自動処理中です。手動操作は停止または操作待ちの後に行えます。':s.loginReady?'JRAの会員番号・パスワードを入力し、「JRAにログイン」を押してください。':s.phase==='payment'?'決済画面を検出しています。内容と取り置き期限を確認し、決済・本人認証はご自身で行ってください。':'接続先を確認し、画面の操作一覧またはブラウザ画面から操作してください。');
 const diag=s.browserDiagnostics;$('browserDiagnostics').textContent=!diag?'ブラウザを開くと状態を表示します。':diag.error?'画面情報を取得できませんでした。決済の成否は未判定です。':`ページ状態：${diag.readyState==='complete'?'読み込み済み':diag.readyState==='interactive'?'表示準備中':'読み込み中'} ／ 開いている画面：${diag.windows} ／ 埋め込み画面：${diag.embeddedFrames}。本人認証や購入の完了を示すものではありません。`;
 if(diag?.displayMode)$('browserDiagnostics').textContent+=' ブラウザ：'+(diag.displayMode==='headed'?'通常表示':diag.displayMode==='headless'?'ヘッドレス':'不明')+'。';
 if(diag?.frames?.length)$('browserDiagnostics').textContent+=' '+diag.frames.map((f,i)=>`埋込${i+1}：${f.actualHost||f.sourceHost||'接続先未確定'}（${f.visible===false?'非表示・':''}${f.error?'取得エラー':f.readyState}）${f.serviceCodes?.length?' ['+f.serviceCodes.join(', ')+']':''}`).join(' ／ ');
 if(diag?.networkErrors?.length)$('browserDiagnostics').textContent+=' 通信エラー：'+diag.networkErrors.map(e=>`${e.host||'接続先不明'} ${e.method||''} ${e.type||''} ${e.status||e.error}${e.serviceCodes?.length?' ['+e.serviceCodes.join(', ')+']':''}${e.responseDetails?' '+e.responseDetails.format+' '+(e.responseDetails.categories||[]).join(',')+' '+(e.responseDetails.mentionedFields||[]).join(','):''}`).join(' ／ ');
 if(diag?.scriptErrors?.length)$('browserDiagnostics').textContent+=' スクリプト診断：'+diag.scriptErrors.map(e=>[e.sourceHost,e.category,e.symbol,e.syntaxKind].filter(Boolean).join(' ')).join(' ／ ');
 $('inspectBrowser').disabled=!manual||manualInputBusy||!s.frame;
 const windows=s.browserWindows||[];$('browserWindowRow').hidden=windows.length<2;
 const wk=JSON.stringify(windows);if($('browserWindow').dataset.key!==wk){$('browserWindow').replaceChildren(...windows.map(w=>{const e=option(w.token,w.label);e.selected=w.selected;return e;}));$('browserWindow').dataset.key=wk;}$('browserWindow').disabled=!manual||manualInputBusy;
 const select=s.activeSelect;$('remoteSelectRow').hidden=!select;if(select){const key=JSON.stringify([select.token,select.options,select.disabled]);if($('remoteSelect').dataset.key!==key){$('remoteSelect').replaceChildren(...select.options.map((text,index)=>{const e=option(String(index),text);e.disabled=!!select.disabled?.[index];return e;}));$('remoteSelect').dataset.key=key;}$('remoteSelect').value=String(select.selected);$('remoteSelect').disabled=!manual;}
 renderCatalog(s);$('captureSeats').disabled=s.busy||s.frame===0||s.phase==='payment'||!$('racecourse').value||!$('eventDate').value;$('racecourse').disabled=s.busy||!(s.catalog||[]).length;$('eventDate').disabled=s.busy||!$('racecourse').value||!($('eventDate').options.length>1);
 const sameTarget=s.booking?.course===$('racecourse').value&&s.booking?.date===$('eventDate').value;
 mergeSeatCatalog(s.seatCatalog||{});
 if(sameTarget&&s.targetReady&&s.seats.length){const learned=JSON.stringify([s.booking,s.seats.map(v=>v.name)]);if(learned!==lastLearnedCatalog){mergeSeatCatalog({[s.booking.course]:{names:s.seats.map(v=>v.name),observedAt:new Date().toISOString(),eventDate:s.booking.date}});lastLearnedCatalog=learned;}}
 const liveSeats=sameTarget&&s.targetReady?s.seats:[];
 const offered=liveSeats.filter(v=>v.available);
 renderPageFields((s.formFields||[]).filter(f=>!s.loginReady||!f.purpose?.startsWith('jra-login')),manual);
 const savedNames=cachedSeatCatalog[$('racecourse').value]?.names||[];
 const allSeats=liveSeats.length?liveSeats:savedNames.map(name=>({name,available:false,stock:'unknown',areas:[]}));
 const seatsKey=JSON.stringify([s.booking,allSeats]);if(seatsKey!==lastSeats){lastSeats=seatsKey;[1,2,3].forEach(i=>{const current=$('wish'+i).value;const choices=allSeats.map(v=>option(v.name,v.name+(v.available?'（受付可能）':v.stock==='none'?'（現在満席・キャンセル待ち）':v.stock==='unknown'?'（保存済み・受付状況未確認）':'（現在受付不可・再確認待ち）')));$('wish'+i).replaceChildren(option('',i===1?'希望席を選択':'指定しない'),...choices);if(current){if(!allSeats.some(v=>v.name===current))$('wish'+i).append(option(current,current+'（保存済み・要再取得）'));$('wish'+i).value=current;}});updateAreas();}
 $('seatsReadInfo').textContent=liveSeats.length?`${s.booking.date}・${s.booking.course}競馬場：${offered.length}種類が取得時点で受付可能（一覧${liveSeats.length}種類）。`:allSeats.length?`${$('racecourse').value}競馬場の保存済み席種${allSeats.length}種類を選択できます。現在の受付状況は未確認です。`:'競馬場と開催日を指定して席種を取得できます。';
 const canSet=!!allSeats.length&&!s.busy;
 document.querySelectorAll('#jobForm select,#jobForm input,#jobForm button').forEach(e=>e.disabled=s.busy);
 [1,2,3].forEach(i=>$('wish'+i).disabled=!canSet);
 $('area').disabled=s.busy||!$('wish1').value;$('saveConditions').disabled=s.busy||!$('racecourse').value||!$('eventDate').value||!$('wish1').value;
 $('startTime').disabled=s.busy||$('startNow').checked;$('startTime').required=$('startScheduled').checked;
 const needsWatch=!liveSeats.length||[1,2,3].some(i=>allSeats.some(v=>v.name===$('wish'+i).value&&!v.available));
 $('reviewJob').disabled=!canSet||!$('wish1').value||[1,2,3].some(i=>$('wish'+i).value&&!allSeats.some(v=>v.name===$('wish'+i).value))||!$('racecourse').value||!$('eventDate').value||!s.frame||['payment','attention'].includes(s.phase)||(needsWatch&&!$('watch').checked);
 setText('jobReadiness',s.busy?'処理中の条件は変更できません。':!$('racecourse').value?'競馬場を選択してください。一覧がない場合はブラウザを開いて開催一覧を取得してください。':!$('eventDate').value?'開催日を選択してください。':!s.frame?'条件を保存できます。開始する前にブラウザを開いてJRAにログインしてください。':['payment','attention'].includes(s.phase)?'表示中の手続きをブラウザで確認してください。':!allSeats.length?'この競馬場の席種がまだ取得されていません。取得可能になったら席種を保存します。':!$('wish1').value?'第1希望の席種を選択してください。':needsWatch&&!$('watch').checked?'満席・受付状況未確認の席種は、定期確認をオンにするとキャンセル待ちを開始できます。':'条件を確認して開始してください。');
 $('pushDetail').textContent=s.subscribers&&(!s.push.detail||s.push.detail==='通知は未設定です。')?'通知先は登録済みです。テスト通知の到着を確認してください。':s.push.detail;$('testPush').disabled=!s.subscribers;$('confirmPush').disabled=!s.push.testId||s.push.received;$('pushSummary').textContent=s.push.received?'通知の到着を確認済み':s.subscribers?'通知登録済み・到着テストをしてください':'通知を準備しましょう';
 const logs=s.logs.slice().reverse();$('logsEmpty').hidden=!!logs.length;$('activityLog').replaceChildren(...logs.map(item=>{const li=document.createElement('li'),time=document.createElement('time'),p=document.createElement('p');time.textContent=item.time;p.textContent=item.text;li.append(time,p);return li;}));
 $('pushWarning').hidden=s.push.received;updateFrame();}
function updateAreas(){const matches=state?.booking?.course===$('racecourse').value&&state?.booking?.date===$('eventDate').value;const selected=matches?state?.seats.find(s=>s.name===$('wish1').value):null;$('areas').replaceChildren(...(selected?.areas||[]).map(v=>option(v)));}
function updateFrame(){if(!state)return;if(!state.frame){lastFrame=-1;$('remoteFrame').hidden=true;$('frameEmpty').hidden=false;return;}if(tab==='browser'&&lastFrame!==state.frame){lastFrame=state.frame;$('remoteFrame').src=`/api/frame?v=${state.frame}`;$('remoteFrame').hidden=false;$('frameEmpty').hidden=true;}}
async function poll(){if(polling||document.hidden)return;polling=true;try{render(await api('/api/state'));$('offlineNotice').hidden=true;}catch(e){if(state)$('offlineNotice').hidden=false;}finally{polling=false;}}
async function action(path,data={}){try{await api(path,data);await poll();return true;}catch(e){toast(e.message);return false;}}
$('loginForm').addEventListener('submit',async e=>{e.preventDefault();const remember=$('rememberLogin').checked,secret=$('loginSecret').value;if(await action('/api/login',{secret})){$('loginSecret').value='';try{if(remember){await rememberDevice(secret);toast('この端末で30日間、ログインを復元できるよう設定しました。');}else await forgetDevice();}catch{toast('ログインはできましたが、この端末での保持を設定できませんでした。保存済みパスワードをお使いください。');}await poll();}});
document.querySelectorAll('[data-tab]').forEach(b=>b.addEventListener('click',()=>changeTab(b.dataset.tab)));
$('openBrowser').addEventListener('click',async()=>{if(state?.frame){changeTab('browser');return;}if(await action('/api/browser/open'))changeTab('browser');});
$('loadCatalog').addEventListener('click',()=>action('/api/browser/catalog'));
$('captureSeats').addEventListener('click',()=>{const booking=readBooking();if(booking)action('/api/browser/prepare',{booking});});
$('watch').addEventListener('change',()=>{$('watchOptions').hidden=!$('watch').checked;if(state)render(state);});[1,2,3].forEach(i=>$('wish'+i).addEventListener('change',()=>{updateAreas();if(state)render(state);}));
$('jobForm').addEventListener('submit',e=>{e.preventDefault();const booking=readBooking();if(!booking)return;if(!state?.frame||state.busy||['payment','attention'].includes(state.phase)){toast('ブラウザの状態を確認してから開始してください。');return;}const selected=[1,2,3].map(i=>$('wish'+i).value).filter(Boolean);const live=state.targetReady&&state.booking?.course===booking.course&&state.booking?.date===booking.date?state.seats:[];const known=live.length?live.map(v=>v.name):cachedSeatCatalog[booking.course]?.names||[];if(selected.some(name=>!known.includes(name))){toast('希望席を取得した一覧から選び直してください。');return;}if(!$('watch').checked&&(!live.length||selected.some(name=>live.some(v=>v.name===name&&!v.available)))){toast('現在満席・受付不可の席種は、定期確認をオンにするとキャンセル待ちできます。');return;}pendingJob={booking,wishes:[1,2,3].map(i=>$('wish'+i).value.trim()).filter(Boolean),count:Number($('count').value),area:$('area').value.trim(),startMode:$('startNow').checked?'now':'scheduled',start:$('startNow').checked?'':$('startTime').value.replace('T',' '),watch:$('watch').checked,interval:Number($('interval').value),minutes:Number($('minutes').value)};
 if(pendingJob.start.length===16)pendingJob.start+=':00';$('jobSummary').textContent=`開催：${pendingJob.booking.date}・${pendingJob.booking.course}競馬場\n希望順：${pendingJob.wishes.join(' → ')}\n枚数：${pendingJob.count}席\nエリア：${pendingJob.area||'指定なし'}\n開始：${pendingJob.startMode==='now'?'今すぐ':pendingJob.start+' 日本時間'}\n確認：${pendingJob.watch?`${pendingJob.interval}秒ごと／最大${pendingJob.minutes}分`:'1回のみ'}`;saveConditions(false);$('venueConfirmed').checked=false;$('confirmStart').disabled=true;$('confirmDialog').showModal();});
$('venueConfirmed').addEventListener('change',()=>{$('confirmStart').disabled=!$('venueConfirmed').checked;});$('cancelReview').addEventListener('click',()=>$('confirmDialog').close());
$('confirmStart').addEventListener('click',async()=>{if(!pendingJob||!$('venueConfirmed').checked)return;$('confirmStart').disabled=true;if(await action('/api/run',{...pendingJob,confirmed:true})){$('confirmDialog').close();toast('指定条件で準備し、開始日時から申し込みます。決済画面への到達を通知します。');}else $('confirmStart').disabled=false;});
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
$('jraLoginForm').addEventListener('submit',async e=>{e.preventDefault();if($('jraUsername').value==='seat-helper'){toast('JRAの保存項目を選んでください。アプリ用のログイン鍵はここでは使いません。');return;}if(await inputCommand({kind:'login_submit',username:$('jraUsername').value,password:$('jraPassword').value})){$('jraUsername').value='';$('jraPassword').value='';}});
$('clearJraLogin').addEventListener('click',async()=>{if(await inputCommand({kind:'login_clear'})){$('jraUsername').value='';$('jraPassword').value='';}});
$('remoteSelect').addEventListener('change',()=>action('/api/browser/manual',{kind:'select',index:Number($('remoteSelect').value),token:state?.activeSelect?.token}));
$('closeBrowser').addEventListener('click',()=>{$('closeBrowserDialog').showModal();$('cancelCloseBrowser').focus();});
$('cancelCloseBrowser').addEventListener('click',()=>$('closeBrowserDialog').close());
$('confirmCloseBrowser').addEventListener('click',async()=>{if(state?.busy)return;$('confirmCloseBrowser').disabled=true;try{if(await action('/api/browser/close'))$('closeBrowserDialog').close();}finally{$('confirmCloseBrowser').disabled=false;}});
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
  const extra=document.createElement('details'),extraSummary=document.createElement('summary');extra.id='extraPageFields';extraSummary.textContent='その他のリンク・操作';extra.append(extraSummary);
  for(const f of fields){
   const row=document.createElement('div'),label=document.createElement('label'),control=document.createElement(f.type==='select'?'select':'input'),apply=document.createElement('button');
   row.dataset.caption=f.label;row.className='input-card';control.id='page-field-'+f.token;control.dataset.token=f.token;label.htmlFor=control.id;label.textContent=(f.sourceHost?f.sourceHost+'：':'')+f.label;
   if(['action','checkbox','radio'].includes(f.type)){
    apply.type='button';apply.className='secondary full';apply.dataset.checkToken=f.token;apply.textContent=(f.type==='checkbox'?(f.checked?'☑ ':'☐ '):f.type==='radio'?(f.checked?'● ':'○ '):'')+f.label;
    apply.addEventListener('click',()=>inputCommand({kind:'field',token:f.token,activate:true}));row.append(apply);(f.group==='navigation'?extra:$('pageFields')).append(row);continue;
   }
   if(f.type==='select'&&!f.options.some(o=>!o.disabled&&o.text.trim()&&!['選択してください','選択して下さい'].includes(o.text.trim()))){control.dataset.unavailable='true';apply.dataset.unavailable='true';const hint=document.createElement('p');hint.className='hint';hint.textContent='選択できる候補がありません。';row.append(hint);}
   if(f.type==='select'){control.append(...f.options.map((o,i)=>{const e=option(String(i),o.text);e.disabled=o.disabled;return e;}));control.value=String(f.selected);}
   else {control.type=f.type;control.autocomplete='off';control.maxLength=1000;control.setAttribute('autocapitalize','none');control.spellcheck=false;}
   apply.type='button';apply.className='secondary';apply.textContent=f.type==='select'?'選択を反映':'入力を反映';
   apply.addEventListener('click',async()=>{const data=f.type==='select'?{index:Number(control.value)}:{value:control.value};if(await inputCommand({kind:'field',token:f.token,...data})){if(f.type!=='select')control.value='';}});
   const actions=document.createElement('div');actions.className='remote-field-actions';actions.append(apply);if(f.type!=='select'){const clear=document.createElement('button');clear.type='button';clear.className='secondary';clear.textContent='欄を空にする';clear.addEventListener('click',async()=>{if(await inputCommand({kind:'field',token:f.token,value:''}))control.value='';});actions.append(clear);}row.append(label,control,actions);(f.group==='navigation'?extra:$('pageFields')).append(row);
  }
  if(extra.children.length>1)$('pageFields').append(extra);
 }
 fields.filter(f=>f.type==='select').forEach(f=>{const e=$('page-field-'+f.token);if(e&&document.activeElement!==e)e.value=String(f.selected);});
 fields.filter(f=>['checkbox','radio'].includes(f.type)).forEach(f=>{const e=$('pageFields').querySelector(`[data-check-token="${f.token}"]`);if(e)e.textContent=(f.type==='checkbox'?(f.checked?'☑ ':'☐ '):(f.checked?'● ':'○ '))+f.label;});
 $('pageFields').querySelectorAll('input,select,button').forEach(e=>e.disabled=!manual||manualInputBusy||e.dataset.unavailable==='true');filterPageFields();
}

function filterPageFields(){const query=$('pageControlSearch').value.trim();const rows=[...$('pageFields').querySelectorAll('[data-caption]')];rows.forEach(e=>e.hidden=!e.dataset.caption.includes(query));$('pageFieldsEmpty').hidden=!rows.length||rows.some(e=>!e.hidden);const extra=$('extraPageFields');if(extra){const matches=[...extra.querySelectorAll('[data-caption]')].some(e=>!e.hidden);extra.hidden=!matches;if(query&&matches)extra.open=true;}}
$('pageControlSearch').addEventListener('input',filterPageFields);

$('browserWindow').addEventListener('change',()=>inputCommand({kind:'window',token:$('browserWindow').value}));

$('inspectBrowser').addEventListener('click',()=>inputCommand({kind:'inspect'}));


const conditionsKey='seat-helper-booking-v1';
function readBooking(){
 for(const id of ['racecourse','eventDate'])if(!$(id).reportValidity())return null;
 return {course:$('racecourse').value,date:$('eventDate').value};
}
function saveConditions(show=true){
 const booking=readBooking();if(!booking)return;
 const wishes=[1,2,3].map(i=>$('wish'+i).value.trim());
 try{localStorage.setItem(conditionsKey,JSON.stringify({booking,wishes,count:$('count').value,area:$('area').value.trim(),watch:$('watch').checked,interval:$('interval').value,minutes:$('minutes').value}));$('savedConditionsInfo').textContent='条件をこの端末に保存しました。開始日時は毎回確認してください。保存だけでは開始しません。';if(show)toast('競馬場・開催日・希望席・枚数を保存しました。');}
 catch{$('savedConditionsInfo').textContent='この端末には条件を保存できません。画面を閉じる前にご確認ください。';}
}
$('saveConditions').addEventListener('click',()=>saveConditions());
$('clearConditions').addEventListener('click',()=>{try{localStorage.removeItem(conditionsKey);$('savedConditionsInfo').textContent='保存済み条件を解除しました。現在の入力と動作中の処理は変更しません。';}catch{toast('保存済み条件を解除できませんでした。');}});
try{
 const draft=JSON.parse(localStorage.getItem(conditionsKey)||'null');
 if(draft&&typeof draft.booking?.course==='string'&&typeof draft.booking?.date==='string'){
  $('racecourse').append(option(draft.booking.course,draft.booking.course+'競馬場（保存済み・要取得）'));$('racecourse').value=draft.booking.course;$('eventDate').append(option(draft.booking.date,draft.booking.date+'（保存済み・要取得）'));$('eventDate').value=draft.booking.date;
  [1,2,3].forEach((i)=>{if(typeof draft.wishes?.[i-1]==='string'&&draft.wishes[i-1]){const v=draft.wishes[i-1].slice(0,120);$('wish'+i).append(option(v,v+'（保存済み・要取得）'));$('wish'+i).value=v;}});
  for(const id of ['count','area','interval','minutes'])if(typeof draft[id]==='string')$(id).value=draft[id].slice(0,120);
  $('watch').checked=draft.watch===true;$('watchOptions').hidden=!$('watch').checked;
  $('savedConditionsInfo').textContent='保存済みの条件を復元しました。開催日と開始日時を確認してから開始してください。';
 }
}catch{}

function renderCatalog(s){
 const catalog=s.catalog||[],key=JSON.stringify(catalog);
 if($('racecourse').dataset.key!==key){const current=$('racecourse').value||s.booking?.course||'';$('racecourse').replaceChildren(option('','競馬場を選択'),...catalog.map(v=>option(v.course,v.course+'競馬場')));if(current){if(!catalog.some(v=>v.course===current))$('racecourse').append(option(current,current+'競馬場（保存済み・要取得）'));$('racecourse').value=current;}$('racecourse').dataset.key=key;}
 const dates=catalog.find(v=>v.course===$('racecourse').value)?.dates||[],dk=JSON.stringify([$('racecourse').value,dates]);
 if($('eventDate').dataset.key!==dk){const current=$('eventDate').value||(s.booking?.course===$('racecourse').value?s.booking.date:'');$('eventDate').replaceChildren(option('','開催日を選択'),...dates.map(v=>option(v.date,v.date.replaceAll('-','/')+(v.label.match(/[（(]([^）)]+)[）)]/)?.[1]?'（'+v.label.match(/[（(]([^）)]+)[）)]/)[1]+'）':''))));if(current){if(!dates.some(v=>v.date===current))$('eventDate').append(option(current,current+'（保存済み・要取得）'));$('eventDate').value=current;}$('eventDate').dataset.key=dk;}
}
$('racecourse').addEventListener('change',()=>{$('eventDate').value='';[1,2,3].forEach(i=>$('wish'+i).value='');$('area').value='';if(state)render(state);});
$('eventDate').addEventListener('change',()=>{if(state)render(state);if(state?.frame&&!state.busy&&!['payment','attention'].includes(state.phase)&&$('racecourse').value&&$('eventDate').value)action('/api/browser/prepare',{booking:{course:$('racecourse').value,date:$('eventDate').value}});});

function updateStartMode(){$('startTimeRow').hidden=$('startNow').checked;$('startTime').disabled=$('startNow').checked;$('startTime').required=$('startScheduled').checked;}
for(const id of ['startNow','startScheduled'])$(id).addEventListener('change',updateStartMode);updateStartMode();

function mergeSeatCatalog(incoming){
 let changed=false;
 for(const [course,entry] of Object.entries(incoming)){
  if(!entry||!Array.isArray(entry.names)||!entry.names.length||entry.names.length>100||!entry.names.every(n=>typeof n==='string'&&n.length>0&&n.length<=120))continue;
  const old=cachedSeatCatalog[course];
  if(old?.observedAt&&entry.observedAt&&String(old.observedAt)>String(entry.observedAt))continue;
  const next={names:[...new Set(entry.names)],observedAt:entry.observedAt||'',eventDate:entry.eventDate||''};
  if(JSON.stringify(old)!==JSON.stringify(next)){cachedSeatCatalog[course]=next;changed=true;}
 }
 if(changed)try{localStorage.setItem('seat-helper-seat-catalog-v1',JSON.stringify(cachedSeatCatalog));}catch{}
}
