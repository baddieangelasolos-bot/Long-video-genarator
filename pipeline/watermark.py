import subprocess, numpy as np, sys, cv2
name,dur=sys.argv[1],float(sys.argv[2]); N=48; frames=[]
for i in range(N):
    t=20+i*(dur-40)/N
    out=subprocess.run(['ffmpeg','-v','error','-ss',f'{t:.2f}','-i',f'/home/claude/work/src/{name}.mp4','-frames:v','1','-vf','scale=960:540,format=gray','-f','rawvideo','-'],capture_output=True).stdout
    if len(out)==960*540: frames.append(np.frombuffer(out,np.uint8).reshape(540,960))
F=np.stack(frames).astype(np.float32); med=np.median(F,0)
score=np.abs(med-cv2.GaussianBlur(med,(0,0),3))/(F.std(0)+3)
s=np.zeros_like(score); s[380:]=score[380:]
ys,xs=np.where(s>np.percentile(s[380:],99.7))
x0,x1,y0,y1=xs.min()*2-6,xs.max()*2+6,ys.min()*2-6,ys.max()*2+6
print(f'delogo {name}: {x0}:{y0}:{x1-x0}:{y1-y0}   (VERIFY on a zoomed crop)')
