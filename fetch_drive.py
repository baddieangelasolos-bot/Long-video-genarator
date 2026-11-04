#!/usr/bin/env python3
"""Downloads the project files from Drive (anonymous links) byte-exact into WORK/src with size + SHA-256 verification. Exit 0 ok, 1 failed.
env RELAY_FILES_JSON = [[name, driveId, bytes], ...] (empty = built-in five).  Names: gal.mp4 dor.mp4 rma.mp4 music.m4a ref.mp4"""
import json, os, re, sys, time, hashlib
import gdown
DEFAULT=[["gal.mp4","1h5Q1aHNXMWDBo_qR-Kmp5OpTZqtHBrwF",125795124],["dor.mp4","11WovlOWIZbhjTZBwbIPFd3U5SWG3Uvid",273754662],["rma.mp4","1zl87D_E0h5ogGCUINr_29ivNmZxr7Pp2",344812952],["music.m4a","1XaLWyDqyGj-wTlgB04Sxv7j9oKTrF5hc",2612210],["ref.mp4","13D83SMPio2H9LWn4vWeI0ZBYJaXvWkbZ",35303250]]
DEST=os.environ.get('SRC_DIR','/home/claude/work/src'); FILES=json.loads(os.environ['RELAY_FILES_JSON']) if os.environ.get('RELAY_FILES_JSON') else DEFAULT
def sha(p):
    h=hashlib.sha256()
    with open(p,'rb') as f:
        for b in iter(lambda:f.read(1<<20),b''): h.update(b)
    return h.hexdigest()
os.makedirs(DEST,exist_ok=True); man={'files':{}}; bad=[]
for name,fid,size in FILES:
    if not re.fullmatch(r'[A-Za-z0-9._-]{1,80}',name) or int(size)<=0: print('bad entry',name); sys.exit(1)
    dst=f'{DEST}/{name}'; ok=False
    if os.path.exists(dst) and os.path.getsize(dst)==size: ok=True
    for a in range(1,6):
        if ok: break
        if os.path.exists(dst): os.remove(dst)
        try: gdown.download(id=fid,output=dst,quiet=True,resume=False)
        except Exception as e: print(name,'attempt',a,type(e).__name__,str(e)[:160],flush=True)
        got=os.path.getsize(dst) if os.path.exists(dst) else 0
        if got==size: ok=True; break
        print(name,'size',got,'expected',size,'attempt',a,'(Drive quota/permission page? files must be shared Anyone with the link)',flush=True); time.sleep(float(os.environ.get('RELAY_RETRY_SLEEP','20'))*a)
    if ok: man['files'][name]={'size':size,'sha256':sha(dst)}; print('OK',name,size,flush=True)
    else: bad.append(name)
json.dump(man,open(DEST+'/manifest.json','w'),indent=1)
if bad: print('FAILED:',bad); sys.exit(1)
