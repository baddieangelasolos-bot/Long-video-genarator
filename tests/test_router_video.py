import os,sys,json,threading,http.server,tempfile
sys.path.insert(0,'/home/claude/w/repo/autonomy')
log=[]; state={'polls':0,'deleted':False,'gen_bodies':[]}
class H(http.server.BaseHTTPRequestHandler):
    def log_message(s,*a): pass
    def send(s,code,obj=None,hdr=None):
        b=json.dumps(obj or {}).encode(); s.send_response(code)
        for k,v in (hdr or {}).items(): s.send_header(k,v)
        s.send_header('Content-Length',str(len(b))); s.end_headers(); s.wfile.write(b)
    def do_GET(s):
        if s.path.startswith('/v1beta/models'): return s.send(200,{'models':[{'name':'models/gemini-2.5-flash','supportedGenerationMethods':['generateContent']}]})
        if s.path.startswith('/v1beta/files/abc'):
            state['polls']+=1; return s.send(200,{'name':'files/abc','state':'PROCESSING' if state['polls']<2 else 'ACTIVE','uri':'https://x/files/abc','mimeType':'video/mp4'})
        s.send(404)
    def do_DELETE(s): state['deleted']=True; s.send(200)
    def do_POST(s):
        n=int(s.headers.get('Content-Length',0)); body=s.rfile.read(n)
        if s.path.startswith('/upload/v1beta/files') and 'start' in s.headers.get('X-Goog-Upload-Command',''): return s.send(200,{},{'X-Goog-Upload-URL':f'http://127.0.0.1:{port}/upload/v1beta/put'})
        if s.path=='/upload/v1beta/put': state['uploaded']=len(body); return s.send(200,{'file':{'name':'files/abc','uri':'https://x/files/abc'}})
        if ':generateContent' in s.path:
            j=json.loads(body); state['gen_bodies'].append(j); return s.send(200,{'candidates':[{'content':{'parts':[{'text':'{"look":{"color_grade":"warm"}}'}]}}]})
        s.send(404)
srv=http.server.HTTPServer(('127.0.0.1',0),H); port=srv.server_address[1]; threading.Thread(target=srv.serve_forever,daemon=True).start()
os.environ.update(GEMINI_API_KEY='k'*20,GEMINI_API_BASE=f'http://127.0.0.1:{port}/v1beta',GEMINI_UPLOAD_BASE=f'http://127.0.0.1:{port}/upload/v1beta',GEMINI_FILE_POLL_SLEEP='0')
import gemini_router as R
v=tempfile.mktemp(suffix='.mp4'); open(v,'wb').write(os.urandom(5000))
txt,m=R.ask_video('vision','describe',v)
res=[]
def chk(n,c): res.append(c); print(('PASS ' if c else 'FAIL ')+n)
chk('video bytes uploaded to the Files API',state.get('uploaded')==5000)
chk('waited for ACTIVE processing (polled more than once)',state['polls']>=2)
chk('generateContent got the video as file_data, plus the prompt',any('file_data' in p for p in state['gen_bodies'][0]['contents'][0]['parts']) and state['gen_bodies'][0]['contents'][0]['parts'][0]['text']=='describe')
chk('JSON answer returned from a discovered real model',json.loads(txt)['look']['color_grade']=='warm' and m=='gemini-2.5-flash')
chk('uploaded copy deleted afterwards',state['deleted'])
os.environ['GEMINI_API_KEY']=''; R._cache['ids']=None; chk('no API key -> (None,None), never raises',R.ask_video('vision','x',v)==(None,None))
print(f'{sum(res)}/{len(res)} passed'); sys.exit(0 if all(res) else 1)
