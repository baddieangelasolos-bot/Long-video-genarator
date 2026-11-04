#!/usr/bin/env python3
"""Uploads the final video to the Drive outputs folder via an Apps Script-created resumable session.
usage: drive_export.py VIDEO.mp4        env: APPS_SCRIPT_URL, WEBHOOK_TOKEN   exit 0 ok, 1 failed
Chunks are multiples of 256 KiB; interrupted uploads resume from what Drive reports it has."""
import os, sys, re, json, time, hashlib
from urllib.parse import urlparse
import requests
CHUNK=int(os.environ.get('EXPORT_CHUNK',8*1024*1024)); assert CHUNK%(256*1024)==0
ALLOW=('googleapis.com',)+tuple(h for h in os.environ.get('EXPORT_ALLOW_HOST','').split(',') if h)
SLEEP=float(os.environ.get('EXPORT_RETRY_SLEEP','5')); TRIES=int(os.environ.get('EXPORT_TRIES','8'))
def log(*a): print('[export]',*a,flush=True)
def host_ok(u):
    p=urlparse(u); h=p.hostname or ''
    return (p.scheme=='https' or h in ALLOW) and any(h==a or h.endswith('.'+a) for a in ALLOW)
def call(event,**kw):
    url=os.environ['APPS_SCRIPT_URL']; tok=os.environ['WEBHOOK_TOKEN']; last=None
    for a in range(1,5):
        try:
            r=requests.post(url,data=json.dumps(dict(token=tok,event=event,**kw)),headers={'Content-Type':'text/plain'},timeout=90)
            j=r.json()
            if j.get('ok') is not None: return j
            last=f'unexpected reply {r.text[:120]}'
        except Exception as e: last=f'{type(e).__name__}: {str(e)[:120]}'
        time.sleep(SLEEP*a)
    raise RuntimeError(f'Apps Script call {event} failed: {last}')
def status(session,total):
    r=requests.put(session,headers={'Content-Range':f'bytes */{total}','Content-Length':'0'},timeout=60)
    if r.status_code in (200,201): return total,r
    if r.status_code==308:
        m=re.match(r'bytes=0-(\d+)',r.headers.get('Range',''))
        return (int(m.group(1))+1 if m else 0),r
    raise RuntimeError(f'status query failed: HTTP {r.status_code}')
def main():
    if len(sys.argv)<2 or not os.path.isfile(sys.argv[1]): log('video file missing'); return 1
    path=sys.argv[1]; total=os.path.getsize(path); name=re.sub(r'[^A-Za-z0-9._-]','_',os.path.basename(path))[:100]
    if not name.lower().endswith('.mp4'): name+='.mp4'
    if not (os.environ.get('APPS_SCRIPT_URL') and os.environ.get('WEBHOOK_TOKEN')): log('APPS_SCRIPT_URL / WEBHOOK_TOKEN not set'); return 1
    init=call('upload_init',name=name,size=total)
    if not init.get('ok'): log('session refused:',init.get('error')); return 1
    session=init['session']
    if not host_ok(session): log('refusing to upload to unexpected host'); return 1
    h=hashlib.sha256(); log(f'uploading {name} ({total} bytes) in {CHUNK//1024} KiB chunks')
    sent=0; fails=0; final=None
    with open(path,'rb') as f:
        while final is None:
            f.seek(sent); data=f.read(min(CHUNK,total-sent)); end=sent+len(data)-1
            try:
                r=requests.put(session,data=data,headers={'Content-Range':f'bytes {sent}-{end}/{total}'},timeout=300)
                if r.status_code in (200,201): final=r; sent=total
                elif r.status_code==308:
                    m=re.match(r'bytes=0-(\d+)',r.headers.get('Range','')); sent=int(m.group(1))+1 if m else 0; fails=0
                else: raise RuntimeError(f'HTTP {r.status_code}')
            except Exception as e:
                fails+=1; log(f'chunk at {sent} failed ({type(e).__name__}: {str(e)[:100]}), attempt {fails}/{TRIES}')
                if fails>=TRIES: log('giving up'); return 1
                time.sleep(SLEEP*fails)
                try:
                    sent,r2=status(session,total)
                    if sent>=total: final=r2
                except Exception as e2: log('status query failed:',str(e2)[:100])
            if sent and sent%(64*CHUNK)<CHUNK: log(f'{100*sent//total}%')
    try: fid=final.json()['id']
    except Exception: log('upload finished but no file id returned'); return 1
    done=call('upload_done',fileId=fid,size=total)
    if not done.get('ok'): log('Drive verification failed:',done); return 1
    log('DONE',done.get('url')); open(os.path.join(os.path.dirname(path),'export_url.txt'),'w').write(done.get('url',''))
    try: call('success',step='Video exported to Drive',message=f'{name} ({total} bytes) is in the outputs folder.',url=done.get('url',''))
    except Exception as e: log('success email failed (video is uploaded):',e)
    return 0
if __name__=='__main__': sys.exit(main())
