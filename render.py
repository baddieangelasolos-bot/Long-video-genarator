#!/usr/bin/env python3
"""Resumable, atomic clip renderer. Start: (setsid nohup python3 render.py >> /home/claude/work/build/render.log 2>&1 < /dev/null &)
Single-instance lock; each clip validated by frame count before it counts as done; writes render_status.json at the end."""
import json, os, subprocess, time, sys, fcntl, hashlib, glob
HERE=os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0,HERE)
from notify import notify
SRC='/home/claude/work/src'; B='/home/claude/work/build'; OUT=B+'/clips'; os.makedirs(OUT,exist_ok=True)
lk=open(B+'/render.lock','w')
try: fcntl.flock(lk,fcntl.LOCK_EX|fcntl.LOCK_NB)
except BlockingIOError: print('ABORT: another render.py is running'); sys.exit(3)
CFG=json.load(open(HERE+'/config.json')); EDLTXT=open(HERE+'/edl.json').read(); EDL=json.loads(EDLTXT); EDLSHA=hashlib.sha1(EDLTXT.encode()).hexdigest()
STATUS=B+'/render_status.json'
if os.path.exists(STATUS): os.remove(STATUS)
for p in glob.glob(OUT+'/*.part.mp4'): os.remove(p)
def probe(path):
    r=subprocess.run(['ffprobe','-v','error','-select_streams','v:0','-show_entries','stream=width,height','-of','json',path],capture_output=True,text=True,timeout=60)
    s=json.loads(r.stdout)['streams'][0]; return s['width'],s['height']
def count_frames(p):
    r=subprocess.run(['ffprobe','-v','error','-count_frames','-select_streams','v:0','-show_entries','stream=nb_read_frames','-of','csv=p=0',p],capture_output=True,text=True,timeout=180)
    return int(r.stdout.strip().split(',')[0])
def delogo_box(src,W,H):
    v=CFG.get('delogo',{}).get(src)
    if not v: return None
    x,y,w,h=[int(float(n)) for n in v.split(':')]; x=max(1,x); y=max(1,y); w=min(w,W-x-2); h=min(h,H-y-2)
    return None if w<4 or h<4 else (x,y,w,h)
def vf_for(c,bi,box,blend=True,effect=True):
    sp=c['speed']; dur=c['frames']/30; f=[]
    if abs(sp-1)>1e-6:
        f.append(f"setpts=PTS/{sp}"); f.append("framerate=fps=30:interp_start=1:interp_end=254:scene=100" if blend else "fps=30")
    else: f.append("fps=30")
    if box: f.append("delogo=x=%d:y=%d:w=%d:h=%d"%box)       # BEFORE zoom/crop/grade
    zb=c['zb']; intro=c['sec']=='intro'
    Z=f"({zb}*(1+0.04*t/{dur:.3f}))" if intro else f"({zb}*(1+0.05*t/{dur:.3f})+0.09*max(0,1-t/0.28))"
    f.append(f"scale=w='2*trunc(1920*{Z}/2)':h='2*trunc(1080*{Z}/2)':eval=frame:flags=bicubic")
    f.append(f"crop=1920:1080:x='(iw-1920)*{c['ax']}':y='(ih-1080)*{c['ay']}'")
    g=CFG.get('grade',{}).get(c['src'])
    if g: f+=[g['eq']]+([g['vignette']] if g.get('vignette') else [])+["unsharp=5:5:0.3"]
    else: f+=["eq=contrast=1.07:saturation=1.2:gamma=0.98","unsharp=5:5:0.4","vignette=PI/6"]
    if not c.get('fullframe') and c['id']!='f6': f+=["crop=1920:970:0:55","pad=1920:1080:0:55:black"]     # reference keeps ~5% bars top+bottom until the end card
    if intro: f+=[f"fade=t=in:st=0:d={0.8 if c['id']=='i1' else 0.25}",f"fade=t=out:st={dur-0.2:.3f}:d=0.2"]
    elif c['sec']=='body' and effect:
        m=bi%3
        if m==0: f.append("fade=t=in:st=0:d=0.14:color=white")
        elif m==1: f.append("rgbashift=rh=-6:bh=6:rv=2:bv=-2:enable='lt(t,0.2)'")
    return f
ENC=['-r','30','-c:v','libx264','-preset','veryfast','-crf','17','-pix_fmt','yuv420p','-g','30','-x264-params','keyint=30:min-keyint=30:scenecut=0','-colorspace','bt709','-color_primaries','bt709','-color_trc','bt709','-video_track_timescale','15360']
def build_cmd(c,bi,part,box,blend,effect):
    dur=c['frames']/30; chain=",".join(vf_for(c,bi,box,blend,effect)); tail=f",fade=t=out:st={dur-1.3:.3f}:d=1.3" if c.get('last_fade') else ""
    cmd=['ffmpeg','-v','error','-y','-ss',str(c['start']),'-t',f"{dur*c['speed']+0.4:.3f}",'-i',f"{SRC}/{c['src']}.mp4"]
    if c.get('card') and os.path.exists(c['card']):
        s,d=c.get('card_fade',[0,0.5]); cmd+=['-loop','1','-framerate','30','-t',f"{dur:.3f}",'-i',c['card']]
        fc=f"[0:v]{chain}[v];[1:v]format=rgba,fade=t=in:st={s}:d={d}:alpha=1[t];[v][t]overlay=0:0:format=auto{tail},format=yuv420p[o]"; cmd+=['-an','-filter_complex',fc,'-map','[o]']
    else: cmd+=['-an','-vf',chain+tail+',format=yuv420p']
    return cmd+['-frames:v',str(c['frames'])]+ENC+[part]
fails=[]; degraded=[]; bi=0
for c in EDL:
    idx=bi if c['sec']=='body' else 0
    if c['sec']=='body': bi+=1
    out=f"{OUT}/{c['id']}.mp4"; part=f"{OUT}/{c['id']}.part.mp4"
    sig=hashlib.sha1((json.dumps(c,sort_keys=True)+json.dumps(CFG.get('delogo',{}).get(c['src']))+str(idx)).encode()).hexdigest()
    try:
        if os.path.exists(out) and open(out+'.ok').read().split()[0]==sig: continue
    except Exception: pass
    t0=time.time(); done=False; used=''
    try: W,H=probe(f"{SRC}/{c['src']}.mp4"); box=delogo_box(c['src'],W,H)
    except Exception as e: fails.append(c['id']); print(f"FAIL {c['id']}: cannot probe source ({e})",flush=True); continue
    if c['src'] in CFG.get('delogo',{}) and box is None: print(f"warn {c['id']}: delogo box invalid for {W}x{H}, none applied",flush=True)
    ladder=[('full',True,True),('no-blend',False,True),('no-blend,no-effect',False,False)]
    for name,blend,effect in ladder:
        if abs(c['speed']-1)<1e-6 and name=='no-blend': continue
        try:
            r=subprocess.run(build_cmd(c,idx,part,box,blend,effect),capture_output=True,text=True,timeout=900)
            if r.returncode==0 and os.path.exists(part) and count_frames(part)==c['frames']:
                os.replace(part,out); open(out+'.ok','w').write(sig+' '+name); done=True; used=name; break
            err=(r.stderr or '')[-200:]+f' frames={count_frames(part) if os.path.exists(part) else "n/a"}'
        except Exception as e: err=f'{type(e).__name__}: {e}'
        print(f"retry {c['id']} after '{name}' failed: {err}",flush=True)
        if os.path.exists(part): os.remove(part)
    if done:
        if used!='full': degraded.append((c['id'],used))
        print(f"ok {c['id']} {c['frames']}f [{used}] in {time.time()-t0:.0f}s",flush=True)
    else: fails.append(c['id']); print(f"FAIL {c['id']} (all attempts)",flush=True)
st=dict(edl_sha1=EDLSHA,fails=fails,degraded=degraded,t=time.time()); json.dump(st,open(STATUS+'.tmp','w')); os.replace(STATUS+'.tmp',STATUS)
print(f"FAILS={len(fails)} DEGRADED={degraded}"); print('DONE',flush=True)
notify('failure' if fails else 'success','render',f'fails={fails} degraded={degraded}')
