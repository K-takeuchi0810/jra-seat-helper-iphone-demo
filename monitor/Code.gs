/** Public health checks only. This script never contacts JRA or reads account data. */
const SEAT_MONITOR_HANDLER = 'checkSeatHelperHealth';
const SEAT_MONITOR_URL_KEY = 'SEAT_HELPER_HEALTH_URL';

function seatMonitorUrl_() {
  const value = PropertiesService.getScriptProperties().getProperty(SEAT_MONITOR_URL_KEY) || '';
  if (!/^https:\/\/[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.onrender\.com\/health$/i.test(value)) {
    throw new Error('スクリプトのプロパティに、自分のRenderアプリのHTTPS /health URLを設定してください。');
  }
  return value;
}

function checkSeatHelperHealth() {
  const url = seatMonitorUrl_();
  const props = PropertiesService.getScriptProperties();
  const checkedAt = new Date().toISOString();
  let status = 0;
  try {
    const response = UrlFetchApp.fetch(url, {
      method: 'get', muteHttpExceptions: true, followRedirects: false,
      validateHttpsCertificates: true,
    });
    status = response.getResponseCode();
    if (status !== 200) throw new Error('unhealthy response');
    let value;
    try { value = JSON.parse(response.getContentText()); } catch (e) { throw new Error('invalid health body'); }
    if (!value || value.ok !== true) throw new Error('health not confirmed');
    props.setProperties({LAST_CHECK_AT: checkedAt, LAST_SUCCESS_AT: checkedAt, LAST_CHECK_RESULT: 'ok'});
    console.log(JSON.stringify({checkedAt: checkedAt, ok: true, status: status}));
    return {ok: true, checkedAt: checkedAt};
  } catch (e) {
    props.setProperties({LAST_CHECK_AT: checkedAt, LAST_CHECK_RESULT: 'failed:' + status});
    throw new Error('アプリの稼働を確認できませんでした。Render側の稼働状況を確認してください。HTTP ' + status);
  }
}

function installSeatHelperMonitor() {
  seatMonitorUrl_();
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) throw new Error('ほかの設定処理が動作中です。少し待ってやり直してください。');
  try {
    const existing = ScriptApp.getProjectTriggers().filter(function(t) {
      return t.getHandlerFunction() === SEAT_MONITOR_HANDLER;
    });
    if (existing.length) return {installed: true, alreadyInstalled: true};
    checkSeatHelperHealth();
    ScriptApp.newTrigger(SEAT_MONITOR_HANDLER).timeBased().everyMinutes(5).create();
    return {installed: true, everyMinutes: 5};
  } finally { lock.releaseLock(); }
}

function stopSeatHelperMonitor() {
  let removed = 0;
  ScriptApp.getProjectTriggers().forEach(function(t) {
    if (t.getHandlerFunction() === SEAT_MONITOR_HANDLER) {
      ScriptApp.deleteTrigger(t); removed++;
    }
  });
  return {removed: removed};
}
