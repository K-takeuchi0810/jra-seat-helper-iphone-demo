import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../monitor/Code.gs',import.meta.url),'utf8');
function fixture({url='https://seat-helper-test.onrender.com/health',status=200,body='{"ok":true}'}={}){
 const properties={SEAT_HELPER_HEALTH_URL:url};const calls=[];const triggers=[];let creations=0;
 const props={getProperty:key=>properties[key],setProperties:value=>Object.assign(properties,value)};
 const context=vm.createContext({
   PropertiesService:{getScriptProperties:()=>props},
   LockService:{getScriptLock:()=>({tryLock:()=>true,releaseLock:()=>{}})},
   UrlFetchApp:{fetch:(address,options)=>{calls.push({address,options});return {getResponseCode:()=>status,getContentText:()=>body};}},
   ScriptApp:{getProjectTriggers:()=>triggers.slice(),deleteTrigger:t=>triggers.splice(triggers.indexOf(t),1),newTrigger:handler=>{
     let minutes;const builder={timeBased:()=>builder,everyMinutes:value=>{minutes=value;return builder;},create:()=>{
       const t={getHandlerFunction:()=>handler,minutes};triggers.push(t);creations++;return t;
     }};return builder;
   }},console:{log:()=>{}}
 });
 vm.runInContext(source,context);
 return {context,calls,triggers,properties,get creations(){return creations;}};
}
test('health monitor contacts only the configured public Render health endpoint with TLS and no redirects',()=>{
 const f=fixture();assert.equal(f.context.checkSeatHelperHealth().ok,true);assert.equal(f.calls.length,1);
 assert.equal(f.calls[0].address,'https://seat-helper-test.onrender.com/health');
 assert.equal(f.calls[0].options.validateHttpsCertificates,true);assert.equal(f.calls[0].options.followRedirects,false);
 assert.equal(f.properties.LAST_CHECK_RESULT,'ok');assert.ok(f.properties.LAST_SUCCESS_AT);
});
test('monitor does not accept a JRA endpoint, an insecure URL, userinfo, queries or an arbitrary host',()=>{
 for(const url of ['https://jra-tickets.jp/health','http://seat.onrender.com/health','https://user:pass@seat.onrender.com/health',
   'https://seat.onrender.com/health?key=secret','https://seat.onrender.com.attacker.example/health','https://127.0.0.1/health','']){
   const f=fixture({url});assert.throws(()=>f.context.checkSeatHelperHealth());assert.equal(f.calls.length,0);
 }
});
test('an HTML loading page or unsuccessful health result is not counted as successful monitoring',()=>{
 for(const value of [{status:503,body:'down'},{status:200,body:'<html>Starting</html>'},{status:200,body:'{"ok":false}'}]){
   const f=fixture(value);assert.throws(()=>f.context.checkSeatHelperHealth());assert.match(f.properties.LAST_CHECK_RESULT,/failed/);
   assert.equal(f.properties.LAST_SUCCESS_AT,undefined);
 }
});
test('installation is idempotent and creates only one five-minute trigger',()=>{
 const f=fixture();f.context.installSeatHelperMonitor();f.context.installSeatHelperMonitor();
 assert.equal(f.creations,1);assert.equal(f.triggers.length,1);assert.equal(f.triggers[0].minutes,5);
});
test('stopping this monitor leaves unrelated triggers untouched',()=>{
 const f=fixture();const other={getHandlerFunction:()=> 'anotherFunction'};f.triggers.push(other);
 f.context.installSeatHelperMonitor();f.context.stopSeatHelperMonitor();assert.deepEqual(f.triggers,[other]);
});
