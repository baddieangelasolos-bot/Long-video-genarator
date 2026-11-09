import os,sys,json,threading,subprocess,http.server,socketserver
TOKEN='t'*64
class H(http.server.BaseHTTPRequestHandler):
    def log_message(s,*a): pass
    def do_POST(s):
        n=int(s.headers.get('Content-Length',0)); b=json.loads(s.rfile.read(n) or b'{}')
        if s.path=='/echo405':
            body=b'<html>Error 405 (Method Not Allowed)</html>'; s.send_response(405)
        else:
            ev=b.get('event'); r={'pong':True,'ok':True} if ev=='ping' else {'ok':True,'session':'https://www.googleapis.com/upload/x'} if ev=='upload_init' else {'ok':True}
            body=json.dumps(r).encode(); s.send_response(200)
        s.send_header('Content-Length',str(len(body))); s.end_headers(); s.wfile.write(body)
srv=http.server.HTTPServer(('127.0.0.1',0),H); port=srv.server_address[1]; threading.Thread(target=srv.serve_forever,daemon=True).start()
res=[]
def chk(n,c): res.append(c); print(('PASS ' if c else 'FAIL ')+n)
def run(url,*args,allow=True):
    env=dict(os.environ,APPS_SCRIPT_URL=url,WEBHOOK_TOKEN=TOKEN,EXPORT_RETRY_SLEEP='0')
    if allow: env['EXPORT_ALLOW_HOST']='127.0.0.1'
    return subprocess.run([sys.executable,'autonomy/drive_export.py','--ping',*args],env=env,capture_output=True,text=True)
p=run('https://script.googleusercontent.com/macros/echo?user_content_key=SECRETKEY123&lib=abc',allow=False)
chk('echo URL rejected with clear 405 explanation, key not printed',p.returncode==1 and 'HTTP 405' in p.stdout+p.stderr and 'SECRETKEY123' not in p.stdout+p.stderr)
p=run('https://script.google.com/macros/s/AKfycbXYZ/dev',allow=False); chk('/dev URL rejected',p.returncode==1 and 'does not look like' in p.stdout+p.stderr)
p=run('https://example.com/exec',allow=False); chk('foreign host rejected',p.returncode==1 and 'does not look like' in p.stdout+p.stderr)
p=run(f'http://127.0.0.1:{port}/echo405'); chk('server answering 405 HTML gives clear message',p.returncode==1 and 'HTTP 405' in p.stdout+p.stderr and 'use the /exec URL' in p.stdout+p.stderr)
p=run(f'http://127.0.0.1:{port}/exec'); chk('preflight passes against healthy mock',p.returncode==0 and 'PREFLIGHT OK' in p.stdout)
import importlib.util
sp=importlib.util.spec_from_file_location('de','autonomy/drive_export.py'); de=importlib.util.module_from_spec(sp); sp.loader.exec_module(de)
chk('exec URL forms accepted (personal + workspace + trailing slash)',all(de.GOOD_URL.match(u) for u in ['https://script.google.com/macros/s/AKfycbw_x-1/exec','https://script.google.com/a/macros/example.com/s/AKfy123/exec','https://script.google.com/macros/s/AKfy123/exec/']))
print(f'{sum(res)}/{len(res)} passed'); sys.exit(0 if all(res) else 1)
