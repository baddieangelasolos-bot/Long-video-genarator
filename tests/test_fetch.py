import os,sys,json,threading,subprocess,http.server,hashlib,tempfile,shutil
TOKEN='t'*64; store='/tmp/fk/store'; shutil.rmtree(store,ignore_errors=True); os.makedirs(store)
def put(i,n): open(f'{store}/{i}','wb').write(os.urandom(n)); return n
listing={'ok':True,'video':[{'id':'V2','name':'zeta clip.mov','size':put('V2',3000),'mime':'video/quicktime','modified':2},{'id':'V1','name':'Alpha goal.mp4','size':put('V1',2000),'mime':'video/mp4','modified':1}],
 'audio':[{'id':'A1','name':'old.mp3','size':put('A1',500),'mime':'audio/mpeg','modified':1},{'id':'A2','name':'new.m4a','size':put('A2',600),'mime':'audio/mp4','modified':5}],
 'reference':[{'id':'R1','name':'ref edit.mp4','size':put('R1',800),'mime':'video/mp4','modified':3}]}
cur={'l':listing}
class H(http.server.BaseHTTPRequestHandler):
    def log_message(s,*a): pass
    def do_POST(s):
        b=json.loads(s.rfile.read(int(s.headers['Content-Length']))); r=cur['l'] if b.get('event')=='list_sources' and b.get('token')==TOKEN else {'ok':False,'error':'unknown event'}
        body=json.dumps(r).encode(); s.send_response(200); s.send_header('Content-Length',str(len(body))); s.end_headers(); s.wfile.write(body)
srv=http.server.HTTPServer(('127.0.0.1',0),H); port=srv.server_address[1]; threading.Thread(target=srv.serve_forever,daemon=True).start()
res=[]
def chk(n,c): res.append(c); print(('PASS ' if c else 'FAIL ')+n)
def run(extra=None,listing_override=None):
    if listing_override is not None: cur['l']=listing_override
    d=tempfile.mkdtemp(); env=dict(os.environ,PYTHONPATH='/tmp/fk',SRC_DIR=d,APPS_SCRIPT_URL=f'http://127.0.0.1:{port}/exec',WEBHOOK_TOKEN=TOKEN,EXPORT_ALLOW_HOST='127.0.0.1',RELAY_RETRY_SLEEP='0',EXPORT_RETRY_SLEEP='0'); env.update(extra or {})
    env.pop('RELAY_FILES_JSON',None) if not extra or 'RELAY_FILES_JSON' not in extra else None
    p=subprocess.run([sys.executable,'autonomy/fetch_drive.py'],env=env,capture_output=True,text=True); return p,d
p,d=run()
chk('exit 0 and source videos saved as s01/s02 in name order',p.returncode==0 and os.path.exists(d+'/s01.mp4') and os.path.exists(d+'/s02.mp4'))
n=json.load(open(d+'/names.json')); chk('names.json maps Alpha->s01, zeta->s02, newest audio, reference',n['s01']=='Alpha goal.mp4' and n['s02']=='zeta clip.mov' and n['music']=='new.m4a' and n['ref']=='ref edit.mp4')
chk('downloaded bytes identical to Drive (new audio used, not old)',open(d+'/music.m4a','rb').read()==open(store+'/A2','rb').read() and open(d+'/s01.mp4','rb').read()==open(store+'/V1','rb').read())
chk('does not use the old built-in Yamal files',not os.path.exists(d+'/gal.mp4'))
l2=dict(listing,reference=[]); p,d=run(listing_override=l2); chk('no reference -> runs, no ref.mp4, says default style',p.returncode==0 and not os.path.exists(d+'/ref.mp4') and 'default editing style' in p.stdout)
p,d=run(listing_override=dict(listing,video=[])); chk('empty source folder -> clear failure',p.returncode!=0 and 'no video files' in p.stdout+p.stderr)
p,d=run(listing_override=dict(listing,audio=[])); chk('no audio -> clear failure',p.returncode!=0 and 'audio' in p.stdout+p.stderr)
cur['l']={'ok':False,'error':'unknown event'}; p,d=run(); chk('old Apps Script (no list_sources) -> tells you to redeploy',p.returncode!=0 and 'New version' in p.stdout+p.stderr)
cur['l']=listing; os.remove(store+'/V2'); p,d=run(); chk('missing/unshared file -> exit 1 with the shareResourceFolders hint',p.returncode==1 and 'shareResourceFolders' in p.stdout)
put('V2',3000)
p,d=run(extra={'APPS_SCRIPT_URL':'','WEBHOOK_TOKEN':''}); chk('no Apps Script config -> refuses instead of silently reusing old files',p.returncode==1 and 'cannot be read' in p.stdout)
p,d=run(extra={'USE_BUILTIN_FILES':'1','APPS_SCRIPT_URL':'','WEBHOOK_TOKEN':''}); chk('USE_BUILTIN_FILES=1 still possible on purpose',('using the old fixed' in p.stdout))
print(f'{sum(res)}/{len(res)} passed'); sys.exit(0 if all(res) else 1)
