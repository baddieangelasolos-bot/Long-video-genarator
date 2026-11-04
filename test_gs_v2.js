const vm=require('vm'),fs=require('fs'); let pass=0,tot=0; const chk=(n,c)=>{tot++; if(c)pass++; console.log((c?'PASS ':'FAIL ')+n)};
const TOKEN='a'.repeat(64); const props={WEBHOOK_TOKEN:TOKEN,OUTPUTS_ID:'OUT123456789'}; const mails=[]; let fetchArgs=null, fetchResp;
let files={GOODFILEID1:{parents:['OUT123456789'],size:1000},ELSEFILE123:{parents:['OTHER'],size:1000}};
const ctx={Logger:{log(){}},PropertiesService:{getScriptProperties:()=>({getProperty:k=>props[k]||null,setProperty:(k,v)=>props[k]=v})},
 MailApp:{sendEmail:(to,s,b)=>mails.push([to,s,b])},Session:{getEffectiveUser:()=>({getEmail:()=>'me@x.com'})},CacheService:{getScriptCache:()=>({get:()=>null,put(){}})},
 ContentService:{createTextOutput:t=>({t,setMimeType(){return this}}),MimeType:{JSON:1}},ScriptApp:{getOAuthToken:()=>'OAUTH'},Utilities:{getUuid:()=>'1234-5678'},
 UrlFetchApp:{fetch:(u,o)=>{fetchArgs=[u,o];return fetchResp}},
 DriveApp:{getFileById:id=>{const f=files[id]; if(!f)throw new Error('nf'); return {getParents:()=>{let i=0;return{hasNext:()=>i<f.parents.length,next:()=>({getId:()=>f.parents[i++]})}},getSize:()=>f.size,getUrl:()=>'https://drive/'+id,getName:()=>'v.mp4'}}}};
vm.createContext(ctx); vm.runInContext(fs.readFileSync(__dirname+'/../docs/Code_v2_upload.gs','utf8'),ctx);
const post=o=>JSON.parse(ctx.doPost({postData:{contents:JSON.stringify(o)}}).t);
const resp=(code,loc)=>({getResponseCode:()=>code,getAllHeaders:()=>loc?{Location:loc}:{},getContentText:()=>'err'});
fetchResp=resp(200,'https://www.googleapis.com/upload/drive/v3/files?upload_id=abc');
let r=post({token:TOKEN,event:'upload_init',name:'A_Highlights.mp4',size:5000});
chk('upload_init returns session',r.ok&&r.session.includes('upload_id=abc'));
chk('session request is Drive resumable, authorised, parent = outputs, size header',fetchArgs[0].includes('uploadType=resumable')&&fetchArgs[1].headers.Authorization==='Bearer OAUTH'&&JSON.parse(fetchArgs[1].payload).parents[0]==='OUT123456789'&&fetchArgs[1].headers['X-Upload-Content-Length']==='5000');
chk('bad token rejected',post({token:'x',event:'upload_init',name:'a.mp4',size:5}).error==='unauthorized');
for(const [n,o] of [['path in name',{name:'../a.mp4',size:5}],['wrong extension',{name:'a.exe',size:5}],['zero size',{name:'a.mp4',size:0}],['over cap',{name:'a.mp4',size:3e9}],['fractional size',{name:'a.mp4',size:5.5}]]){ fetchArgs=null; r=post(Object.assign({token:TOKEN,event:'upload_init'},o)); chk('rejects '+n,!r.ok&&fetchArgs===null);}
fetchResp=resp(403,null); r=post({token:TOKEN,event:'upload_init',name:'a.mp4',size:5}); chk('Drive refusal reported, no session',!r.ok&&!r.session);
delete props.OUTPUTS_ID; r=post({token:TOKEN,event:'upload_init',name:'a.mp4',size:5}); chk('missing OUTPUTS_ID reported',!r.ok&&/OUTPUTS_ID/.test(r.error)); props.OUTPUTS_ID='OUT123456789';
r=post({token:TOKEN,event:'upload_done',fileId:'GOODFILEID1',size:1000}); chk('upload_done ok in outputs folder',r.ok&&r.url.includes('GOODFILEID1'));
chk('upload_done size mismatch',!post({token:TOKEN,event:'upload_done',fileId:'GOODFILEID1',size:999}).ok);
chk('upload_done file outside outputs folder rejected',post({token:TOKEN,event:'upload_done',fileId:'ELSEFILE123'}).error==='file is not in the outputs folder');
chk('upload_done unknown file / bad id',!post({token:TOKEN,event:'upload_done',fileId:'NOPEFILE123'}).ok&&!post({token:TOKEN,event:'upload_done',fileId:'a b'}).ok);
r=post({token:TOKEN,event:'notice',step:'Upgrade suggestions',message:'m'}); chk('notice event sends its own email',r.ok&&mails.length===1&&/Upgrade suggestions/.test(mails[0][1]));
chk('old events still work (ping, unknown)',post({token:TOKEN,event:'ping'}).pong===true&&post({token:TOKEN,event:'zzz'}).error==='unknown event');
console.log(`\n${pass}/${tot} passed`); process.exit(pass===tot?0:1);
