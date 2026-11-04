"""python3 measure_grade.py FILE [t0 t1]  -> letterbox bars over time + grade stats on the ACTIVE picture area (bars excluded)."""
import sys, subprocess, json, numpy as np
f=sys.argv[1]; t0=float(sys.argv[2]) if len(sys.argv)>2 else 0; t1=float(sys.argv[3]) if len(sys.argv)>3 else 1e9
pr=json.loads(subprocess.run(['ffprobe','-v','error','-select_streams','v:0','-show_entries','stream=width,height:format=duration','-of','json',f],capture_output=True,text=True).stdout); dur=float(pr['format']['duration'])
W,H=192,108; t1=min(t1,dur)
p=subprocess.Popen(['ffmpeg','-v','error','-ss',str(t0),'-t',str(t1-t0),'-i',f,'-an','-vf',f'fps=5,scale={W}:{H}','-pix_fmt','rgb24','-f','rawvideo','-'],stdout=subprocess.PIPE)
fr=[]
while True:
    b=p.stdout.read(W*H*3)
    if len(b)<W*H*3: break
    fr.append(np.frombuffer(b,np.uint8).reshape(H,W,3))
F=np.stack(fr).astype(np.float32); G=F.mean(3); n=len(F)
def bars(g):
    rows=g.mean(1); top=0
    while top<H//3 and rows[top]<9: top+=1
    bot=0
    while bot<H//3 and rows[H-1-bot]<9: bot+=1
    return top,bot
B=np.array([bars(g) for g in G]); 
print('letterbox (rows of 108, per 1s median): ',end='')
for s in range(0,int(t1-t0),8):
    seg=B[s*5:(s+8)*5]; 
    if len(seg): print(f"{t0+s:.0f}s:{int(np.median(seg[:,0]))}/{int(np.median(seg[:,1]))}",end='  ')
print()
tb=int(np.median(B[:,0])); bb=int(np.median(B[:,1])); act=slice(tb,H-bb)
print(f'median bars top/bot = {tb}/{bb} rows of {H} -> picture aspect ~ {16*H/(H-tb-bb)/9*9/16*16:.2f}:{9}' if False else f'median bars = {tb}+{bb} of {H} rows -> picture area aspect {(W/(H-tb-bb)):.2f}:1')
A=F[:,act,:]; mx=A.max(3); mn=A.min(3); S=((mx-mn)/(mx+1e-3)); Ga=A.mean(3)
h=Ga.shape[1]; cor=np.concatenate([Ga[:,:12,:16].reshape(n,-1),Ga[:,:12,-16:].reshape(n,-1),Ga[:,-12:,:16].reshape(n,-1),Ga[:,-12:,-16:].reshape(n,-1)],1).mean(1); cen=Ga[:,h//2-10:h//2+10,W//2-20:W//2+20].reshape(n,-1).mean(1)
print(json.dumps(dict(saturation=round(float(S.mean()),3),luma_mean=round(float(Ga.mean()),1),luma_std=round(float(Ga.std((1,2)).mean()),1),vignette_corner_over_center=round(float((cor/np.maximum(cen,1)).mean()),3),p5_luma=round(float(np.percentile(Ga,5)),1),p95_luma=round(float(np.percentile(Ga,95)),1))))
