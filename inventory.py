"""python3 inventory.py DIR [name=KEYWORD ...]  -> ffprobe table; creates ABSOLUTE symlinks in work/src (e.g. gal=galatasaray)."""
import sys, os, json, subprocess, glob
D=sys.argv[1] if len(sys.argv)>1 else '/mnt/user-data/uploads'; S='/home/claude/work/src'; os.makedirs(S,exist_ok=True)
def probe(p):
    r=subprocess.run(['ffprobe','-v','error','-show_entries','stream=codec_type,width,height,r_frame_rate,sample_rate:format=duration','-of','json',p],capture_output=True,text=True,timeout=120)
    try: j=json.loads(r.stdout)
    except Exception: return None
    if 'format' not in j or 'duration' not in j.get('format',{}): return None
    v=[s for s in j.get('streams',[]) if s.get('codec_type')=='video']; a=[s for s in j.get('streams',[]) if s.get('codec_type')=='audio']
    fps=None
    if v:
        n,d=v[0]['r_frame_rate'].split('/'); fps=round(float(n)/float(d or 1),2)
    return dict(dur=round(float(j['format']['duration']),2),res=f"{v[0]['width']}x{v[0]['height']}" if v else None,fps=fps,asr=a[0].get('sample_rate') if a else None)
files=sorted(f for f in glob.glob(D+'/*') if os.path.isfile(f))
for f in files:
    pr=probe(f)
    if pr is not None: print(os.path.basename(f)[:60].ljust(62),pr)
for kv in sys.argv[2:]:
    name,kw=kv.split('=',1); m=[f for f in files if kw.lower() in os.path.basename(f).lower()]
    if len(m)!=1: print('LINK FAIL',name,'matches:',len(m)); continue
    ext=os.path.splitext(m[0])[1]; dst=f'{S}/{name}{ext if name in("music","ref") else ".mp4"}'
    if os.path.lexists(dst): os.remove(dst)
    os.symlink(os.path.abspath(m[0]),dst); print('linked',dst,'->',m[0]); assert os.path.exists(dst)
