#!/usr/bin/env python3
"""python3 grade_fit.py  -> fits per-source eq + vignette so the graded ACTIVE picture matches the reference's measured stats. Writes config.json['grade']."""
import json, os, subprocess, shutil, numpy as np
HERE=os.path.dirname(os.path.abspath(__file__)); EDL=json.load(open(HERE+'/edl.json')); CFG=json.load(open(HERE+'/config.json'))
T=dict(sat=0.512,luma=81.4,std=42.5,p5=13.0,p95=196.7,vig=0.908)     # measured on reference body (measure_grade.py, bars excluded)
TW,TH=256,144; ACT=int(TH*0.05)
def frames(src):
    d=f'/tmp/gf_{src}'; shutil.rmtree(d,ignore_errors=True); os.makedirs(d); k=0
    for c in EDL:
        if c['src']!=src: continue
        for q in (0.25,0.75):
            k+=1; subprocess.run(['ffmpeg','-v','error','-y','-ss',f"{c['start']+q*c['srcdur']:.2f}",'-i',f'/home/claude/work/src/{src}.mp4','-frames:v','1','-vf',f'scale={TW}:{TH}',f'{d}/{k:03d}.png'],timeout=60)
    return d,k
def stats(d,eq,vig):
    vf=','.join(x for x in (eq,vig,f'crop={TW}:{TH-2*ACT}:0:{ACT}','format=rgb24') if x)
    r=subprocess.run(['ffmpeg','-v','error','-framerate','1','-i',f'{d}/%03d.png','-vf',vf,'-f','rawvideo','-'],capture_output=True)
    h=TH-2*ACT; A=np.frombuffer(r.stdout,np.uint8).reshape(-1,h,TW,3).astype(np.float32); n=len(A); G=A.mean(3)
    S=((A.max(3)-A.min(3))/(A.max(3)+1e-3)).mean(); ch,cw=int(.12*h),int(.083*TW)
    cor=np.concatenate([G[:,:ch,:cw].reshape(n,-1),G[:,:ch,-cw:].reshape(n,-1),G[:,-ch:,:cw].reshape(n,-1),G[:,-ch:,-cw:].reshape(n,-1)],1).mean(1)
    cen=G[:,h//2-int(.093*h):h//2+int(.093*h),TW//2-int(.104*TW):TW//2+int(.104*TW)].reshape(n,-1).mean(1)
    return dict(sat=S,luma=G.mean(),std=G.std((1,2)).mean(),p5=np.percentile(G,5),p95=np.percentile(G,95),vig=(cor/np.maximum(cen,1)).mean())
def loss(s): return ((s['luma']-T['luma'])/8)**2+((s['std']-T['std'])/4)**2+((s['sat']-T['sat'])/.04)**2+((s['p95']-T['p95'])/12)**2+((s['p5']-T['p5'])/4)**2+((s['vig']-T['vig'])/.05)**2
GRID=dict(contrast=[1.0,1.05,1.1,1.15,1.2,1.3,1.4],brightness=[-0.04,0,0.03,0.06,0.09,0.12],saturation=[0.5,0.6,0.7,0.8,0.9,1.0,1.1],gamma=[0.9,1.0,1.1,1.2,1.3,1.4])
VIG=['','vignette=PI/24','vignette=PI/16','vignette=PI/10','vignette=PI/6']
out={}
for src in sorted({c['src'] for c in EDL}):
    d,k=frames(src); p=dict(contrast=1.1,brightness=0.03,saturation=0.8,gamma=1.1); vig=''
    eqs=lambda p:'eq='+':'.join(f'{a}={b}' for a,b in p.items())
    base=stats(d,'eq=contrast=1.07:saturation=1.2:gamma=0.98','vignette=PI/6'); best=loss(stats(d,eqs(p),vig))
    for rnd in range(3):
        for key,vals in GRID.items():
            for v in vals:
                q=dict(p); q[key]=v; s=loss(stats(d,eqs(q),vig))
                if s<best: best,p=s,q
        for vg in VIG:
            s=loss(stats(d,eqs(p),vg))
            if s<best: best,vig=s,vg
    fin=stats(d,eqs(p),vig); out[src]=dict(eq=eqs(p),vignette=vig)
    print(src,k,'frames | old grade:',{a:round(float(b),2) for a,b in base.items()},'\n   fitted:',eqs(p),vig or '(no vignette)','->',{a:round(float(b),2) for a,b in fin.items()})
CFG['grade']=out; json.dump(CFG,open(HERE+'/config.json','w'),indent=1); print('target',T)
