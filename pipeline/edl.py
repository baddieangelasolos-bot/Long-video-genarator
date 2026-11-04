"""python3 edl.py [edl_input.json]. Input: {"P":..,"OFF":..} (or omit -> beat.json), "final_len", "clips":[[id,sec,src,start,speed,beats,zb,ax,ay],...]
Hard rules: every clip >= 90 frames (3.0s), unique ids, valid windows, no cut inside window (when scan data exists)."""
import json, sys, os, subprocess
HERE=os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0,HERE)
from ai_router import local_window_ok
d=json.load(open(sys.argv[1] if len(sys.argv)>1 else HERE+'/edl_input.json'))
if 'P' not in d: b=json.load(open(HERE+'/beat.json')); d['P'],d['OFF']=b['P'],b['OFF']
P,OFF,FPS=d['P'],d['OFF'],30; fr=lambda k:int(round((OFF+P*k)*FPS)); out=[]; k=0; errs=[]; seen=set(); info={}
def srcinfo(s):
    if s not in info:
        r=subprocess.run(['ffprobe','-v','error','-select_streams','v:0','-show_entries','stream=r_frame_rate:format=duration','-of','json',f'/home/claude/work/src/{s}.mp4'],capture_output=True,text=True)
        try: j=json.loads(r.stdout); n,dd=j['streams'][0]['r_frame_rate'].split('/'); info[s]=(float(n)/float(dd),float(j['format']['duration']))
        except Exception: info[s]=None
    return info[s]
for row in d['clips']:
    try: (i,sec,src,st,sp,b,zb,ax,ay)=row; st,sp,b,zb=float(st),float(sp),int(b),float(zb)
    except Exception as e: errs.append(f'bad row {row}: {e}'); continue
    if i in seen: errs.append(f'duplicate id {i}')
    seen.add(i)
    if sec not in('intro','body','finale'): errs.append(f'{i}: bad section {sec}')
    if not 0.3<=sp<=1.5: errs.append(f'{i}: speed {sp} out of range')
    f0,f1=fr(k),fr(k+b); fl=f1-f0; sd=fl/FPS*sp
    if fl<90: errs.append(f'{i}: only {fl} frames (<90 = 3.0s rule)')
    si=srcinfo(src)
    if si is None: errs.append(f'{i}: source {src}.mp4 missing/unreadable')
    else:
        fps,dur=si
        if st<0 or st+sd+0.4>dur: errs.append(f'{i}: window {st}+{sd:.2f}s exceeds source ({dur:.1f}s)')
        ok=local_window_ok(src,st,st+sd,fps)
        if ok is False: errs.append(f'{i}: cut/re-frame detected inside window {st}-{st+sd:.2f} ({src})')
        elif ok is None: print(f'warn {i}: no scan data for {src}, cut check skipped')
    out.append(dict(id=i,sec=sec,src=src,start=st,speed=sp,beats=b,zb=zb,ax=ax,ay=ay,k0=k,frames=fl,srcdur=sd)); k+=b
if d.get('final_len') and out:
    extra=int(round(d['final_len']*FPS))-sum(c['frames'] for c in out)
    if abs(extra)>30: errs.append(f'final_len mismatch by {extra} frames; fix beat plan instead of padding')
    else: out[-1]['frames']+=extra; out[-1]['pad']=extra; out[-1]['last_fade']=True
if errs: print('EDL INVALID:\n  '+'\n  '.join(errs)); sys.exit(2)
json.dump(out,open(HERE+'/edl.json','w'),indent=1); print('EDL ok:',len(out),'clips',sum(c['frames'] for c in out),'frames =',sum(c['frames'] for c in out)/FPS,'s')
