const vm=require('vm'),fs=require('fs'); let pass=0,tot=0; const chk=(n,c)=>{tot++; if(c)pass++; console.log((c?'PASS ':'FAIL ')+n)};
const TOKEN='a'.repeat(64); const props={WEBHOOK_TOKEN:TOKEN,OUTPUTS_ID:'OUT1'}; const shared=[];
const mk=(name,kids,files)=>{const o={name,kids:{},files:files||[],getFoldersByName:n=>{const k=o.kids[n];let d=!!k;return{hasNext:()=>d,next:()=>{d=false;return k}}},createFolder:n=>{o.kids[n]=mk(n);return o.kids[n]},getId:()=>'id_'+name,getUrl:()=>'u',
 getFiles:()=>{let i=0;return{hasNext:()=>i<o.files.length,next:()=>o.files[i++]}},setSharing:(a,p)=>shared.push([name,a,p])};return o};
const F=(name,size,mime)=>({getId:()=>'F_'+name,getName:()=>name,getSize:()=>size,getMimeType:()=>mime,getLastUpdated:()=>({getTime:()=>1700000000000}),getSharingAccess:()=>'ANYONE_WITH_LINK'});
const root=mk('root'); const my={};
const ctx={Logger:{log(){}},PropertiesService:{getScriptProperties:()=>({getProperty:k=>props[k]||null,setProperty:(k,v)=>props[k]=v})},MailApp:{sendEmail(){}},Session:{getEffectiveUser:()=>({getEmail:()=>'me@x.com'})},
 CacheService:{getScriptCache:()=>({get:()=>null,put(){}})},ContentService:{createTextOutput:t=>({t,setMimeType(){return this}}),MimeType:{JSON:1}},ScriptApp:{getOAuthToken:()=>'T'},Utilities:{getUuid:()=>'1234-5678'},UrlFetchApp:{},
 DriveApp:{getRootFolder:()=>root,Access:{ANYONE_WITH_LINK:'AWL'},Permission:{VIEW:'VIEW'}}};
vm.createContext(ctx); vm.runInContext(fs.readFileSync('docs/Code_v2_upload.gs','utf8'),ctx);
const post=o=>JSON.parse(ctx.doPost({postData:{contents:JSON.stringify(o)}}).t);
// build the tree through the script's own helper, then add files
ctx.folderAt_('resources/source video').files.push(F('mbappe goal.mp4',123456,'video/mp4'),F('notes',5,'application/vnd.google-apps.document'),F('clip2.mov',999,'video/quicktime'));
ctx.folderAt_('resources/audio file').files.push(F('song.mp3',5000,'audio/mpeg'));
let r=post({token:TOKEN,event:'list_sources'});
chk('list_sources ok with three lists',r.ok&&Array.isArray(r.video)&&Array.isArray(r.audio)&&Array.isArray(r.reference));
chk('lists new video files with id/name/size/mime',r.video.length===2&&r.video[0].id==='F_mbappe goal.mp4'&&r.video[0].size===123456&&r.video[1].mime==='video/quicktime');
chk('google docs are skipped',!r.video.some(f=>f.name==='notes'));
chk('audio listed, empty reference folder listed empty',r.audio[0].name==='song.mp3'&&r.reference.length===0);
chk('bad token rejected',post({token:'x',event:'list_sources'}).error==='unauthorized');
ctx.shareResourceFolders(); chk('shareResourceFolders shares exactly the 3 resource folders view-only, not outputs',shared.length===3&&shared.every(x=>x[1]==='AWL'&&x[2]==='VIEW')&&!shared.some(x=>x[0]==='outputs'));
console.log(`\n${pass}/${tot} passed`); process.exit(pass===tot?0:1);
