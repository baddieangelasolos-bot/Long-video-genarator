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
CFG=json.load(open(HERE+'/config.json')); FPS=int(CFG.get('fps',30))
try: STYLE=json.load(open(HERE+'/style.json'))
except Exception: STYLE={}
PRESET=os.environ.get('RENDER_PRESET','slow'); CRF=os.environ.get('RENDER_CRF','15')
TRANS=STYLE.get('transitions') or {'flash_white':1/3,'rgb_split':1/3,'hard_cut':1/3}; DEFAULT_STYLE=STYLE.get('source','default')=='default'
def pick_transition(bi):
    """Deterministic weighted choice per body clip (the reference's transition mix). The default style keeps the original flash / rgb-split / plain cycle."""
    if DEFAULT_STYLE: return ('flash_white','rgb_split','hard_cut')[bi%3]
    cnt={k:0 for k in TRANS}
    for i in range(bi+1):
        k=max(TRANS,key=lambda t:TRANS[t]*(i+1)-cnt[t]); cnt[k]+=1
    return k
EDLTXT=open(HERE+'/edl.json').read(); EDL=json.loads(EDLTXT); EDLSHA=hashlib.sha1(EDLTXT.encode()).hexdigest()
STATUS=B+'/render_status.json'
if os.path.exists(STATUS): os.remove(STATUS)
for p in glob.glob(OUT+'/*.part.mp4'): os.remove(p)
def probe(path):
    r=subprocess.run(['ffprobe','-v','error','-select_streams','v:0','-show_entries','stream=width,height,r_frame_rate,color_transfer','-of','json',path],capture_output=True,text=True,timeout=60)
    s=json.loads(r.stdout)['streams'][0]; n,d=(s.get('r_frame_rate') or '30/1').split('/')
    return s['width'],s['height'],(float(n)/float(d or 1) if float(d or 1) else 30.0),s.get('color_transfer','') in ('smpte2084','arib-std-b67')
def count_frames(p):
    r=subprocess.run(['ffprobe','-v','error','-count_frames','-select_streams','v:0','-show_entries','stream=nb_read_frames','-of','csv=p=0',p],capture_output=True,text=True,timeout=180)
    return int(r.stdout.strip().split(',')[0])
def delogo_box(src,W,H):
    v=CFG.get('delogo',{}).get(src)
    if not v: return None
    x,y,w,h=[int(float(n)) for n in v.split(':')]; x=max(1,x); y=max(1,y); w=min(w,W-x-2); h=min(h,H-y-2)
    return None if w<4 or h<4 else (x,y,w,h)
TINT={'warm':'colorbalance=rs=.05:bs=-.05:rm=.04:bm=-.04','cool':'colorbalance=rs=-.05:bs=.06:rm=-.03:bm=.04','teal_orange':'colorbalance=rs=-.05:bs=.07:rh=.08:bh=-.06','black_white':'hue=s=0'}
def vf_for(c,bi,box,blend=True,effect=True,sfps=30.0,hdr=False,tm=True):
    sp=c['speed']; dur=c['frames']/FPS; f=[]; intro=c['sec']=='intro'; tr=pick_transition(bi) if (c['sec']=='body' and effect) else 'hard_cut'
    if hdr and tm: f.append("zscale=t=linear:npl=100,format=gbrpf32le,tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p")
    if abs(sp-1)>1e-6:
        f.append(f"setpts=PTS/{sp}")
        # slow motion: use the source's real frames when it has enough (crisp); only blend when the source runs out of frames
        f.append(f"framerate=fps={FPS}:interp_start=15:interp_end=240:scene=8" if (blend and sfps*sp<FPS-0.5) else f"fps={FPS}")
    else: f.append(f"fps={FPS}")
    if box: f.append("delogo=x=%d:y=%d:w=%d:h=%d"%box)       # BEFORE zoom/crop/grade
    zb=c['zb']; zr=0.04 if intro else float(STYLE.get('zoom_push',0.05)); punch=0.0
    if not intro and (STYLE.get('punch_all',True) or tr=='zoom_punch'): punch=float(STYLE.get('punch',0.09))
    # supersampled zoom: 2x canvas + sub-pixel crop window, then one high-quality Lanczos downscale (no per-frame integer rounding = no shimmer / judder)
    z=f"{zb}*(1+{zr}*on/{c['frames']})+{punch}*max(0,1-on/{0.28*FPS:.2f})"
    f.append("scale=3840:2160:force_original_aspect_ratio=increase:flags=lanczos,crop=3840:2160")
    f.append(f"zoompan=z='{z}':x='(iw-iw/zoom)*{c['ax']}':y='(ih-ih/zoom)*{c['ay']}':d=1:s=3840x2160:fps={FPS}")
    f.append("scale=1920:1080:flags=lanczos+accurate_rnd+full_chroma_int,setsar=1")
    g=CFG.get('grade',{}).get(c['src'])
    if g: f+=[g['eq']]+([g['vignette']] if g.get('vignette') else [])
    else: f+=["eq=contrast=1.07:saturation=1.2:gamma=0.98","vignette=PI/6"]
    if STYLE.get('tint') in TINT: f.append(TINT[STYLE['tint']])
    sh=float(STYLE.get('sharpen',0.4))
    if sh>0: f.append(f"unsharp=5:5:{sh}:5:5:0")        # luma only: sharper picture without colour noise
    bp=int(round(1080*float(STYLE.get('bars_pct',5.09))/100))
    if bp>=2 and not c.get('fullframe') and c['id']!='f6': f+=[f"crop=1920:{1080-2*bp}:0:{bp}",f"pad=1920:1080:0:{bp}:black"]     # letterbox like the reference until the end card
    if intro: f+=[f"fade=t=in:st=0:d={0.8 if c['id']=='i1' else 0.25}",f"fade=t=out:st={dur-0.2:.3f}:d=0.2"]
    elif c['sec']=='body' and effect:
        if tr=='flash_white': f.append("fade=t=in:st=0:d=0.14:color=white")
        elif tr in ('flash_black','fade_black'): f.append("fade=t=in:st=0:d=0.2:color=black")
        elif tr=='rgb_split': f.append("rgbashift=rh=-6:bh=6:rv=2:bv=-2:enable='lt(t,0.2)'")
        elif tr=='invert_flash': f.append("negate=enable='lt(t,0.07)'")
        elif tr=='blur_pulse': f.append("gblur=sigma=14:steps=2:enable='lt(t,0.15)'")
    return f
ENC=['-r',str(FPS),'-c:v','libx264','-preset',PRESET,'-crf',CRF,'-profile:v','high','-bf','3','-pix_fmt','yuv420p','-g',str(FPS),'-x264-params',f'keyint={FPS}:min-keyint={FPS}:scenecut=0','-colorspace','bt709','-color_primaries','bt709','-color_trc','bt709','-video_track_timescale','90000']
def build_cmd(c,bi,part,box,blend,effect,sfps=30.0,hdr=False,tm=True):
    dur=c['frames']/FPS; chain=",".join(vf_for(c,bi,box,blend,effect,sfps,hdr,tm)); tail=f",fade=t=out:st={dur-1.3:.3f}:d=1.3" if c.get('last_fade') else ""
    cmd=['ffmpeg','-v','error','-y','-ss',str(c['start']),'-t',f"{dur*c['speed']+0.4:.3f}",'-i',f"{SRC}/{c['src']}.mp4"]
    if c.get('card') and os.path.exists(c['card']):
        s,d=c.get('card_fade',[0,0.5]); cmd+=['-loop','1','-framerate',str(FPS),'-t',f"{dur:.3f}",'-i',c['card']]
        fc=f"[0:v]{chain}[v];[1:v]format=rgba,fade=t=in:st={s}:d={d}:alpha=1[t];[v][t]overlay=0:0:format=auto{tail},format=yuv420p[o]"; cmd+=['-an','-filter_complex',fc,'-map','[o]']
    else: cmd+=['-an','-vf',chain+tail+',format=yuv420p']
    return cmd+['-frames:v',str(c['frames'])]+ENC+[part]
fails=[]; degraded=[]; bi=0
for c in EDL:
    idx=bi if c['sec']=='body' else 0
    if c['sec']=='body': bi+=1
    out=f"{OUT}/{c['id']}.mp4"; part=f"{OUT}/{c['id']}.part.mp4"
    sig=hashlib.sha1((json.dumps(c,sort_keys=True)+json.dumps(CFG.get('delogo',{}).get(c['src']))+str(idx)+json.dumps(STYLE,sort_keys=True,default=str)+str(FPS)+PRESET+CRF+json.dumps(CFG.get('grade',{}).get(c['src']),sort_keys=True)).encode()).hexdigest()
    try:
        if os.path.exists(out) and open(out+'.ok').read().split()[0]==sig: continue
    except Exception: pass
    t0=time.time(); done=False; used=''
    try: W,H,sfps,hdr=probe(f"{SRC}/{c['src']}.mp4"); box=delogo_box(c['src'],W,H)
    except Exception as e: fails.append(c['id']); print(f"FAIL {c['id']}: cannot probe source ({e})",flush=True); continue
    if c['src'] in CFG.get('delogo',{}) and box is None: print(f"warn {c['id']}: delogo box invalid for {W}x{H}, none applied",flush=True)
    ladder=[('full',True,True,True),('no-blend',False,True,True),('no-blend,no-effect',False,False,True)]+([('no-blend,no-effect,no-tonemap',False,False,False)] if hdr else [])
    for name,blend,effect,tm in ladder:
        if abs(c['speed']-1)<1e-6 and name=='no-blend': continue
        try:
            r=subprocess.run(build_cmd(c,idx,part,box,blend,effect,sfps,hdr,tm),capture_output=True,text=True,timeout=3600)
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
