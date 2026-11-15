import subprocess, numpy as np, sys, json
from PIL import Image
W,H=96,54; S='/home/claude/work/src'
def src_fps(name):
    try:
        r=subprocess.run(['ffprobe','-v','error','-select_streams','v:0','-show_entries','stream=r_frame_rate','-of','json',f'{S}/{name}.mp4'],capture_output=True,text=True,timeout=60)
        n,d=json.loads(r.stdout)['streams'][0]['r_frame_rate'].split('/'); return float(n)/float(d or 1)
    except Exception: return 30.0
def dhash(f):
    im=Image.fromarray(f.reshape(H,W).astype(np.uint8)).resize((17,16),Image.BILINEAR); a=np.asarray(im,dtype=np.int16)
    return np.packbits((a[:,1:]>a[:,:-1]).flatten())      # 256 bits, used by scripts/dup_scan.py (same footage twice)
def scan(name):
    fps=src_fps(name); stride=max(1,int(round(fps/2.0)))
    p=subprocess.Popen(['ffmpeg','-v','error','-i',f'{S}/{name}.mp4','-an','-vf',f'scale={W}:{H},format=gray','-f','rawvideo','-'],stdout=subprocess.PIPE)
    prev=None; d=[]; hs=[]; sd=[]; ts=[]
    while True:
        b=p.stdout.read(W*H)
        if len(b)<W*H: break
        f=np.frombuffer(b,np.uint8).astype(np.float32); d.append(0.0 if prev is None else float(np.abs(f-prev).mean())); prev=f
        if (len(d)-1)%stride==0:
            try: hs.append(dhash(f)); sd.append(float(f.std())); ts.append((len(d)-1)/fps)
            except Exception: pass
    np.save(f'{S}/full_{name}.npy',np.array(d,dtype=np.float32)); print(name,len(d),flush=True)
    if hs: np.savez(f'{S}/hash_{name}.npz',h=np.array(hs,dtype=np.uint8),std=np.array(sd,dtype=np.float32),t=np.array(ts,dtype=np.float32))
for n in sys.argv[1:]: scan(n)
