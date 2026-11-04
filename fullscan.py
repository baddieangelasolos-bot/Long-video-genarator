import subprocess, numpy as np, sys
W,H=96,54; S='/home/claude/work/src'
def scan(name):
    p=subprocess.Popen(['ffmpeg','-v','error','-i',f'{S}/{name}.mp4','-an','-vf',f'scale={W}:{H},format=gray','-f','rawvideo','-'],stdout=subprocess.PIPE)
    prev=None; d=[]
    while True:
        b=p.stdout.read(W*H)
        if len(b)<W*H: break
        f=np.frombuffer(b,np.uint8).astype(np.float32); d.append(0.0 if prev is None else float(np.abs(f-prev).mean())); prev=f
    np.save(f'{S}/full_{name}.npy',np.array(d,dtype=np.float32)); print(name,len(d),flush=True)
for n in sys.argv[1:]: scan(n)
