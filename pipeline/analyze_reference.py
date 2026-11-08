#!/usr/bin/env python3
"""python3 analyze_reference.py REF.mp4  -> measures the reference edit and writes style_profile.json (+ per-shot sheets for visual labelling).
Measured: cut times & shot-length distribution (intro/body/outro), flash/transition frames, slow-motion share (duplicate frames), grade (saturation, contrast, vignette),
and where its cuts sit relative to ITS OWN beat grid."""
import sys, json, subprocess, os, re, numpy as np
ref=sys.argv[1]; W,H=96,54; HERE=os.path.dirname(os.path.abspath(__file__)); OUT='/home/claude/work/analysis'; os.makedirs(OUT,exist_ok=True)
pr=json.loads(subprocess.run(['ffprobe','-v','error','-select_streams','v:0','-show_entries','stream=width,height,r_frame_rate:format=duration','-of','json',ref],capture_output=True,text=True).stdout)
n,dd=pr['streams'][0]['r_frame_rate'].split('/'); fps=float(n)/float(dd); dur=float(pr['format']['duration'])
p=subprocess.Popen(['ffmpeg','-v','error','-i',ref,'-an','-vf',f'scale={W}:{H}','-pix_fmt','rgb24','-f','rawvideo','-'],stdout=subprocess.PIPE)
F=[]
while True:
    b=p.stdout.read(W*H*3)
    if len(b)<W*H*3: break
    F.append(np.frombuffer(b,np.uint8).reshape(H,W,3))
F=np.stack(F).astype(np.float32); N=len(F); G=F.mean(3)
d=np.concatenate([[0],np.abs(np.diff(G,axis=0)).mean((1,2))]); L=G.mean((1,2))
mx=F.max(3); mn=F.min(3); S=((mx-mn)/(mx+1e-3)).mean((1,2))
r,g,b=F[...,0],F[...,1],F[...,2]; green=((g>r+8)&(g>b+8)&(g>60)).mean((1,2))
cut_i=[]
for i in range(9,N-9):
    loc=np.median(np.concatenate([d[i-8:i],d[i+1:i+9]]))
    if d[i]>max(14,3*loc+6): cut_i.append(i)
m=[]
for i in cut_i:
    if m and (i-m[-1])/fps<0.1:
        if d[i]>d[m[-1]]: m[-1]=i
    else: m.append(i)
cuts=[i/fps for i in m]
# flashes: luminance jump vs neighbours
fl=[]
for i in range(3,N-3):
    ref_l=np.median(np.concatenate([L[i-3:i-1],L[i+2:i+4]]))
    if L[i]-ref_l>35 and L[i]>150: fl.append(i/fps)
flash=[]; 
for t in fl:
    if not flash or t-flash[-1]>0.3: flash.append(t)
# match-footage start: first sustained green>0.45 after 5 s
ms=next((i/fps for i in range(int(5*fps),N-30) if (green[i:i+30]>0.45).all()),None)
t=[0.0]+cuts+[dur]; shots=[(a,b) for a,b in zip(t[:-1],t[1:])]
sl=np.array([b-a for a,b in shots])
def stats(sel): 
    x=np.array([b-a for a,b in sel]); 
    return dict(n=len(x),median=round(float(np.median(x)),2),mean=round(float(x.mean()),2),p10=round(float(np.percentile(x,10)),2),p90=round(float(np.percentile(x,90)),2),min=round(float(x.min()),2),max=round(float(x.max()),2)) if len(x) else {}
intro=[s for s in shots if s[1]<=(ms or 19)]; body=[s for s in shots if s[0]>=(ms or 19) and s[1]<=dur-20]; outro=[s for s in shots if s[0]>=dur-20]
# slow-motion: near-duplicate / low-diff frames inside body shots
dup=[]; 
for a,b in body:
    ia,ib=int(a*fps)+3,int(b*fps)-3; seg=d[ia:ib]; dup.append(float((seg<0.35).mean()) if len(seg)>3 else 0)
body_frames=[(i) for a,b in body for i in range(int(a*fps)+3,int(b*fps)-3)]
bf=np.array(body_frames) if body_frames else np.arange(N); 
# letterbox bars (rows darker than 9 at the top/bottom) -> grade is measured on the ACTIVE picture only
def _bars(g):
    rows=g.mean(1); t=0
    while t<H//3 and rows[t]<9: t+=1
    b=0
    while b<H//3 and rows[H-1-b]<9: b+=1
    return t,b
BB=np.array([_bars(G[i]) for i in bf]); tb=int(np.median(BB[:,0])); bb=int(np.median(BB[:,1])); letterbox_pct=round(100.0*(tb+bb)/2.0/H,2)
def _cropdetect():
    """Full-resolution bar measurement (the 54-row analysis frames are too coarse): median of ffmpeg cropdetect over 7 spots."""
    ys=[]; vh=int(pr['streams'][0]['height'])
    for q in (0.2,0.3,0.4,0.5,0.6,0.7,0.8):
        r=subprocess.run(['ffmpeg','-hide_banner','-ss',f'{dur*q:.1f}','-t','2','-i',ref,'-an','-vf','cropdetect=limit=24:round=2:reset=0','-f','null','-'],capture_output=True,text=True,timeout=120)
        m=re.findall(r'crop=(\d+):(\d+):(\d+):(\d+)',r.stderr)
        if m: w_,h_,x_,y_=map(int,m[-1]); ys.append((vh-h_)/2.0)
    return round(100.0*float(np.median(ys))/vh,2) if ys else None
try:
    _c=_cropdetect()
    if _c is not None: letterbox_pct=min(12.0,max(0.0,_c))
except Exception: pass
tb=bb=int(round(letterbox_pct/100.0*H)); act=slice(tb,H-bb); ha=H-tb-bb
Ga=G[bf][:,act]; Sa=S_full=((mx-mn)/(mx+1e-3))[bf][:,act]
corner=np.concatenate([Ga[:,:8,:12].reshape(len(bf),-1),Ga[:,-8:,-12:].reshape(len(bf),-1)],1).mean(1); center=Ga[:,ha//2-8:ha//2+8,W//2-12:W//2+12].reshape(len(bf),-1).mean(1)
prof=dict(file=os.path.basename(ref),duration=round(dur,2),fps=fps,resolution=f"{pr['streams'][0]['width']}x{pr['streams'][0]['height']}",
 match_footage_starts=round(ms,2) if ms else None,cut_count=len(cuts),
 shot_len_s=dict(intro=stats(intro),body=stats(body),outro=stats(outro)),
 flash_events=dict(count=len(flash),times=[round(x,2) for x in flash[:60]],per_minute=round(len(flash)/(dur/60),1),share_of_cuts_with_flash=round(sum(any(abs(f-c)<0.35 for f in flash) for c in cuts)/max(len(cuts),1),2)),
 slowmo_dup_frame_share_body=dict(mean=round(float(np.mean(dup)),3),shots_over_25pct=int(sum(x>0.25 for x in dup)),of=len(dup)),
 letterbox_pct=letterbox_pct,grade_extra=dict(p5=round(float(np.percentile(Ga,5)),1),p95=round(float(np.percentile(Ga,95)),1)),
 grade=dict(mean_saturation=round(float(Sa.mean()),3),luma_mean=round(float(Ga.mean()),1),luma_std=round(float(Ga.std((1,2)).mean()),1),vignette_corner_over_center=round(float((corner/np.maximum(center,1)).mean()),3),green_fraction=round(float(green[bf].mean()),3)),
 cuts=[round(c,2) for c in cuts])
# own beat grid + alignment of cuts
env=dict(os.environ,BEAT_OUT=OUT+'/ref_beat.json',BEAT_WAV='/home/claude/work/ref_audio.wav')
r=subprocess.run(['python3',HERE+'/beatgrid.py',ref,'0.38','0.70'],capture_output=True,text=True,env=env,timeout=250)
try:
    bj=json.load(open(OUT+'/ref_beat.json')); P,O=bj['P'],bj['OFF']
    ph=[((c-O+P/2)%P)-P/2 for c in cuts]; prof['ref_audio_beat']=dict(P=round(P,4),bpm=round(60/P,2),offset=round(O,3),constant=bj['constant'],cuts_within_40ms_of_beat=round(float(np.mean([abs(x)<0.04 for x in ph])),2),median_offset_ms=round(float(np.median(ph))*1000),
        beats_per_shot_body=round(float(np.median([b-a for a,b in body]))/P,2))
except Exception as e: prof['ref_audio_beat']={'error':str(e)[:100],'log':r.stdout[-300:]}
json.dump(prof,open(HERE+'/style_profile.json','w'),indent=1); json.dump([[round(a,2),round(b,2)] for a,b in shots],open(OUT+'/ref_shots.json','w'))
print(json.dumps({k:v for k,v in prof.items() if k!='cuts'},indent=1))
