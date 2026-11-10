#!/usr/bin/env python3
"""Downloads the project media from YOUR Drive folders (anonymous link download) into WORK/src, byte-exact (size + SHA-256 verified).
Every run asks Apps Script (event list_sources) what is in 'resources/source video', 'resources/audio file' and 'resources/reference videos',
so new uploads are used automatically. Files are saved as s01.mp4, s02.mp4 ... / music.m4a / ref.mp4 and names.json keeps the real names.
Source selection: all videos of the source folder (newest FETCH_MAX_SOURCES if more), newest audio file, newest reference video (optional).
env: APPS_SCRIPT_URL, WEBHOOK_TOKEN | RELAY_FILES_JSON=[[name,driveId,bytes],...] (explicit override) | USE_BUILTIN_FILES=1 (old fixed project files)
Exit 0 ok, 1 failed."""
import json, os, re, sys, time, hashlib
HERE=os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0,HERE)
DEST=os.environ.get('SRC_DIR','/home/claude/work/src'); MAXSRC=int(os.environ.get('FETCH_MAX_SOURCES','12')); SLEEP=float(os.environ.get('RELAY_RETRY_SLEEP','20'))
VID_EXT=('.mp4','.mov','.m4v','.mkv','.webm','.avi','.mts','.mpg','.mpeg'); AUD_EXT=('.m4a','.mp3','.wav','.aac','.flac','.ogg','.opus','.wma')
BUILTIN=json.loads(os.environ.get('BUILTIN_FILES_JSON') or '[]')   # legacy fixed files: supplied only through the BUILTIN_FILES_JSON secret, never stored in the repo
def log(*a): print('[fetch]',*a,flush=True)
def sha(p):
    h=hashlib.sha256()
    with open(p,'rb') as f:
        for b in iter(lambda:f.read(1<<20),b''): h.update(b)
    return h.hexdigest()
def is_video(f): return str(f.get('mime','')).startswith('video/') or f.get('name','').lower().endswith(VID_EXT)
def is_audio(f): return str(f.get('mime','')).startswith('audio/') or f.get('name','').lower().endswith(AUD_EXT)
def plan_from_drive():
    import drive_export as DE
    r=DE.call('list_sources')
    if r.get('error')=='unauthorized':
        raise SystemExit('[fetch] Apps Script answered "unauthorized": the WEBHOOK_TOKEN GitHub secret does not match the WEBHOOK_TOKEN Script Property of the deployment behind APPS_SCRIPT_URL. Fix the secret (value only, no spaces or label) or run showToken in Apps Script. Code was NOT the problem.')
    if not r.get('ok') or 'video' not in r:
        raise SystemExit('[fetch] Apps Script did not return list_sources ('+str(r.get('error','unknown event'))+'). Paste the NEW docs/Code_v2_upload.gs into Apps Script and Deploy > Manage deployments > Edit > New version.')
    vids=[f for f in r['video'] if is_video(f) and f.get('size',0)>0]; auds=[f for f in r['audio'] if is_audio(f) and f.get('size',0)>0]; refs=[f for f in r['reference'] if is_video(f) and f.get('size',0)>0]
    log(f"Drive folders: {len(vids)} source video(s), {len(auds)} audio file(s), {len(refs)} reference video(s)")
    if not vids: raise SystemExit("[fetch] 'resources/source video' has no video files: nothing to edit. Upload footage there.")
    if not auds: raise SystemExit("[fetch] 'resources/audio file' has no audio file: the edit needs music for the beat grid.")
    if len(vids)>MAXSRC:
        log(f'warning: {len(vids)} source videos, using the newest {MAXSRC} (FETCH_MAX_SOURCES)'); vids=sorted(vids,key=lambda f:-f.get('modified',0))[:MAXSRC]
    vids=sorted(vids,key=lambda f:f['name'].lower()); names={}; files=[]
    for i,f in enumerate(vids,1): n=f's{i:02d}'; files.append([n+'.mp4',f['id'],int(f['size'])]); names[n]=f['name']
    a=max(auds,key=lambda f:f.get('modified',0))
    if len(auds)>1: log('several audio files found, using the newest: '+a['name'])
    files.append(['music.m4a',a['id'],int(a['size'])]); names['music']=a['name']
    if refs:
        rf=max(refs,key=lambda f:f.get('modified',0))
        if len(refs)>1: log('several reference videos found, using the newest: '+rf['name'])
        files.append(['ref.mp4',rf['id'],int(rf['size'])]); names['ref']=rf['name']
    else: log('no reference video: the default editing style and rules will be used')
    return files,names
def main():
    if os.environ.get('RELAY_FILES_JSON'): files=json.loads(os.environ['RELAY_FILES_JSON']); names={n.rsplit('.',1)[0]:n for n,_,_ in files}; log('using the explicit file list (RELAY_FILES_JSON)')
    elif os.environ.get('USE_BUILTIN_FILES')=='1': files=BUILTIN; names={n.rsplit('.',1)[0]:n for n,_,_ in files}; log('USE_BUILTIN_FILES=1: using the old fixed project files')
    elif os.environ.get('APPS_SCRIPT_URL') and os.environ.get('WEBHOOK_TOKEN'): files,names=plan_from_drive()
    else: print('[fetch] APPS_SCRIPT_URL / WEBHOOK_TOKEN are not set, so your Drive folders cannot be read. (Set USE_BUILTIN_FILES=1 only to reuse the old fixed files.)'); return 1
    import gdown
    os.makedirs(DEST,exist_ok=True); keep={n for n,_,_ in files}
    for f in os.listdir(DEST):      # never leave stale media from an earlier run next to the new set
        if re.fullmatch(r's\d\d\.mp4|gal\.mp4|dor\.mp4|rma\.mp4|music\.m4a|ref\.mp4|full_.*\.npy',f) and f not in keep and not f.startswith('full_'):
            try: os.remove(os.path.join(DEST,f))
            except OSError: pass
    man={'files':{}}; bad=[]; total=sum(int(s) for _,_,s in files); log(f'{len(files)} file(s), {total/1e6:.0f} MB to download')
    for name,fid,size in files:
        size=int(size)
        if not re.fullmatch(r'[A-Za-z0-9._-]{1,80}',name) or size<=0: print('bad entry',name); return 1
        dst=f'{DEST}/{name}'; ok=os.path.exists(dst) and os.path.getsize(dst)==size
        for a in range(1,6):
            if ok: break
            if os.path.exists(dst): os.remove(dst)
            try: gdown.download(id=fid,output=dst,quiet=True,resume=False)
            except Exception as e: log(name,'attempt',a,type(e).__name__,str(e)[:160])
            got=os.path.getsize(dst) if os.path.exists(dst) else 0
            if got==size: ok=True; break
            log(name,'size',got,'expected',size,'attempt',a,'(Drive quota or permission page? Run shareResourceFolders once in Apps Script so the folders are viewable by link)'); time.sleep(SLEEP*a)
        if ok: man['files'][name]={'size':size,'sha256':sha(dst),'original':names.get(name.rsplit('.',1)[0],name)}; log('OK',name,size,'<-',names.get(name.rsplit('.',1)[0],name))
        else: bad.append(name)
    json.dump(man,open(DEST+'/manifest.json','w'),indent=1); json.dump(names,open(DEST+'/names.json','w'),indent=1)
    if bad: print('FAILED:',bad); return 1
    return 0
if __name__=='__main__': sys.exit(main())
