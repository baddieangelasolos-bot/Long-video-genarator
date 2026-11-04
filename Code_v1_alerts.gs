// VIDEO PIPELINE - Drive setup + authenticated alert webhook (Google Apps Script)
// FIRST USE: pick initialSetup in the toolbar dropdown, click Run, copy the token from the Execution log.
// Then: Deploy > New deployment > Web app (Execute as: Me, Who has access: Anyone).
// Every POST must contain the right "token" in its JSON body, otherwise it is rejected.

var ROOT_NAME = 'yt video generate';
var TREE = ['resources/source video', 'resources/audio file', 'resources/effects', 'resources/reference videos', 'outputs'];
var MAX_BODY = 20000;
var MAX_MSG = 4000;
var MAX_EMAILS_PER_10MIN = 20;

function props_() { return PropertiesService.getScriptProperties(); }

// ---------- one-time setup (run from the editor) ----------
function initialSetup() {
  var root = ensureTree_();
  var token = props_().getProperty('WEBHOOK_TOKEN');
  var fresh = !token;
  if (fresh) { token = newToken_(); props_().setProperty('WEBHOOK_TOKEN', token); }
  props_().setProperty('ROOT_ID', root.getId());
  Logger.log('==============================================');
  Logger.log((fresh ? 'NEW ' : 'EXISTING ') + 'WEBHOOK_TOKEN (copy it): ' + token);
  Logger.log('Drive folder: ' + root.getUrl());
  Logger.log('Alerts go to: ' + alertTo_());
  Logger.log('NEXT: Deploy > New deployment > Web app (Execute as: Me, Who has access: Anyone)');
  Logger.log('==============================================');
  sendMail_('[Video pipeline] Setup complete', 'Folder tree verified/created.\nToken saved in Script Properties (not emailed).\n\nDrive: ' + root.getUrl());
}
function showToken() { Logger.log('WEBHOOK_TOKEN: ' + (props_().getProperty('WEBHOOK_TOKEN') || '(none - run initialSetup)')); }
function rotateToken() {
  var t = newToken_();
  props_().setProperty('WEBHOOK_TOKEN', t);
  Logger.log('NEW WEBHOOK_TOKEN: ' + t);
}
function testEmail() { sendMail_('[Video pipeline] Test', 'Email path works.'); Logger.log('sent to ' + alertTo_()); }

// ---------- helpers ----------
function newToken_() { return Utilities.getUuid().replace(/-/g, '') + Utilities.getUuid().replace(/-/g, ''); }
function alertTo_() { return props_().getProperty('ALERT_EMAIL') || Session.getEffectiveUser().getEmail(); }
function clean_(s, n) { return String(s == null ? '' : s).replace(/[\r\n]+/g, ' ').substring(0, n); }
function ensureFolder_(parent, name) {
  var it = parent.getFoldersByName(name);
  return it.hasNext() ? it.next() : parent.createFolder(name);
}
function ensureTree_() {
  var root = ensureFolder_(DriveApp.getRootFolder(), ROOT_NAME);
  for (var i = 0; i < TREE.length; i++) {
    var parts = TREE[i].split('/'), f = root;
    for (var j = 0; j < parts.length; j++) { f = ensureFolder_(f, parts[j]); }
  }
  return root;
}
function safeEqual_(a, b) {
  var diff = a.length ^ b.length;
  var n = Math.max(a.length, b.length);
  for (var i = 0; i < n; i++) { diff |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0); }
  return diff === 0;
}
function rateOk_() {
  try {
    var c = CacheService.getScriptCache();
    var n = Number(c.get('mails') || 0);
    if (n >= MAX_EMAILS_PER_10MIN) { return false; }
    c.put('mails', String(n + 1), 600);
    return true;
  } catch (e) { return true; }
}
function sendMail_(subject, body) { MailApp.sendEmail(alertTo_(), clean_(subject, 200), body); }
function json_(o) { return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON); }

// ---------- alerts ----------
function sendFailureEmail(stepName, errorMessage) {
  sendMail_('[Video pipeline] FAILED at ' + clean_(stepName, 80), 'Step: ' + clean_(stepName, 200) + '\n\nError / stack trace:\n' + String(errorMessage || '').substring(0, MAX_MSG));
}
function sendSuccessEmail(title, message, fileUrl) {
  sendMail_('[Video pipeline] ' + clean_(title, 100), String(message || '').substring(0, MAX_MSG) + '\n\nOutput: ' + clean_(fileUrl, 500));
}
function verifyFolders() {
  var it = DriveApp.getRootFolder().getFoldersByName(ROOT_NAME);
  if (!it.hasNext()) { return { ok: false, missing: [ROOT_NAME] }; }
  var root = it.next(), missing = [], files = {};
  for (var i = 0; i < TREE.length; i++) {
    var parts = TREE[i].split('/'), f = root, found = true;
    for (var j = 0; j < parts.length && found; j++) {
      var sub = f.getFoldersByName(parts[j]);
      if (sub.hasNext()) { f = sub.next(); } else { found = false; missing.push(TREE[i]); }
    }
    if (found) {
      var fl = f.getFiles(), c = 0;
      while (fl.hasNext()) { fl.next(); c++; }
      files[TREE[i]] = c;
    }
  }
  return { ok: missing.length === 0, missing: missing, fileCounts: files };
}

// ---------- web app ----------
function doGet() { return json_({ ok: true, service: 'video-pipeline', note: 'POST JSON with token' }); }

// POST JSON: {token, event: ping | verify | success | failure, step, message, url}
function doPost(e) {
  try {
    var raw = e && e.postData && e.postData.contents;
    if (!raw) { return json_({ ok: false, error: 'empty body' }); }
    if (raw.length > MAX_BODY) { return json_({ ok: false, error: 'body too large' }); }
    var b;
    try { b = JSON.parse(raw); } catch (x) { return json_({ ok: false, error: 'bad json' }); }
    var expected = props_().getProperty('WEBHOOK_TOKEN');
    if (!expected) { return json_({ ok: false, error: 'not initialised: run initialSetup' }); }
    if (typeof b.token !== 'string' || !safeEqual_(b.token, expected)) {
      Logger.log('rejected request: bad token');
      return json_({ ok: false, error: 'unauthorized' });
    }
    switch (b.event) {
      case 'ping':
        return json_({ ok: true, pong: true, time: new Date().toISOString() });
      case 'verify':
        return json_(verifyFolders());
      case 'failure':
      case 'success':
        if (!rateOk_()) { return json_({ ok: false, error: 'rate limited' }); }
        if (b.event === 'failure') { sendFailureEmail(b.step, b.message); }
        else { sendSuccessEmail(b.step || 'Render complete', b.message, b.url); }
        return json_({ ok: true });
      default:
        return json_({ ok: false, error: 'unknown event' });
    }
  } catch (err) {
    Logger.log('server error: ' + String(err));
    return json_({ ok: false, error: 'server error' });
  }
}
