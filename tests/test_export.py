import os, sys, json, threading, hashlib, tempfile, subprocess, re, http.server, socketserver
TOKEN='t'*64; state={}
class H(http.server.BaseHTTPRequestHandler):
    def log_message(s,*a): pass
    def out(s,code,body=b'',hdr=None):
        s.send_response(code)
        for k,v in (hdr or {}).items(): s.send_header(k,v)
        s.send_header('Content-Length',str(len(body))); s.end_headers(); s.wfile.write(body)
    def do_POST(s):
        b=json.loads(s.rfile.read(int(s.headers['Content-Length'])))
        port=s.server.server_address[1]; ev=b.get('event'); state.setdefault('events',[]).append(ev)
        if b.get('token')!=TOKEN: r={'ok':False,'error':'unauthorized'}
        elif ev=='upload_init':
            host='http://evil.example' if state.get('evil') else f'http://127.0.0.1:{port}'
            state['total']=b['size']; state['buf']=bytearray(); state['fails']=state.get('fails',2); r={'ok':True,'session':host+'/session/x'}
        elif ev=='upload_done': r={'ok':b['fileId']=='FILEID123456' and bytes(state['buf'])!=b'' and len(state['buf'])==b['size'],'url':'https://drive.example/f'}
        else: r={'ok':True}
        state['reply']=json.dumps(r).encode(); s.out(302,hdr={'Location':f'http://127.0.0.1:{port}/result'})   # like real Apps Script: POST -> 302 -> GET
    def do_GET(s): s.out(200,state['reply'],{'Content-Type':'application/json'})
    def do_PUT(s):
        n=int(s.headers.get('Content-Length',0)); data=s.rfile.read(n); cr=s.headers['Content-Range']; buf=state['buf']; total=state['total']
        if cr.startswith('bytes */'):
            if len(buf)>=total: return s.out(200,json.dumps({'id':'FILEID123456'}).encode())
            return s.out(308,hdr=({'Range':f'bytes=0-{len(buf)-1}'} if buf else {}))
        a,e=map(int,re.match(r'bytes (\d+)-(\d+)/',cr).groups())
        if state['fails']>0 and a>0: state['fails']-=1; return s.out(500)               # transient server error
        if state.get('short') and a>0: state['short']=False; buf+=data[:len(data)//2]; return s.out(308,hdr={'Range':f'bytes=0-{len(buf)-1}'})  # partial accept
        if a!=len(buf): return s.out(308,hdr=({'Range':f'bytes=0-{len(buf)-1}'} if buf else {}))
        buf+=data
        if len(buf)>=total: return s.out(200,json.dumps({'id':'FILEID123456'}).encode())
        s.out(308,hdr={'Range':f'bytes=0-{len(buf)-1}'})
class S(socketserver.ThreadingMixIn,http.server.HTTPServer): daemon_threads=True
srv=S(('127.0.0.1',0),H); port=srv.server_address[1]; threading.Thread(target=srv.serve_forever,daemon=True).start()
tmp=tempfile.mkdtemp(); vid=tmp+'/Test_Highlights.mp4'; open(vid,'wb').write(os.urandom(256*1024*5+12345))
want=hashlib.sha256(open(vid,'rb').read()).hexdigest(); res=[]
def chk(n,c): res.append(c); print(('PASS ' if c else 'FAIL ')+n)
def run(**o):
    env=dict(os.environ,APPS_SCRIPT_URL=f'http://127.0.0.1:{port}/exec',WEBHOOK_TOKEN=o.pop('token',TOKEN),EXPORT_CHUNK=str(256*1024),EXPORT_RETRY_SLEEP='0',EXPORT_ALLOW_HOST='127.0.0.1')
    state.clear(); state.update(o); p=subprocess.run([sys.executable,os.path.join(os.path.dirname(__file__),'..','autonomy','drive_export.py'),vid],env=env,capture_output=True,text=True); return p
p=run(fails=0); chk('clean upload exits 0 and bytes identical',p.returncode==0 and hashlib.sha256(bytes(state['buf'])).hexdigest()==want)
chk('success email event sent and url saved','success' in state['events'] and os.path.exists(tmp+'/export_url.txt'))
p=run(fails=2); chk('survives 2 transient 500s, bytes identical',p.returncode==0 and hashlib.sha256(bytes(state['buf'])).hexdigest()==want)
p=run(fails=0,short=True); chk('resumes after partial chunk accept, bytes identical',p.returncode==0 and hashlib.sha256(bytes(state['buf'])).hexdigest()==want)
p=run(fails=0,token='wrong'); chk('wrong token refused (exit 1)',p.returncode==1 and 'refused' in p.stdout)
p=run(fails=0,evil=True); chk('session on foreign host refused, no upload',p.returncode==1 and 'unexpected host' in p.stdout and len(state.get('buf',b''))==0)
p=run(fails=99); chk('persistent failure gives up with exit 1',p.returncode==1 and 'giving up' in p.stdout)
print(f'\n{sum(res)}/{len(res)} passed'); sys.exit(0 if all(res) else 1)
