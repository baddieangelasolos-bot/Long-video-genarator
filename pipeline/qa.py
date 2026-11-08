"""python3 qa.py FINAL.mp4 [sheetdir]. Frame count, boundary spikes (relative), internal spikes, audio start/peak/clipping, labelled contact sheets (1/s)."""
import sys, json, subprocess, numpy as np, re, os, tempfile, shutil
from PIL import Image, ImageDraw, ImageFont
HERE=os.path.dirname(os.path.abspath(__file__)); v=sys.argv[1]; sd=sys.argv[2] if len(sys.argv)>2 else '/home/claude/work/qa'; os.makedirs(sd,exist_ok=True)
EDL=json.load(open(HERE+'/edl.json'))
try: FPS=int(json.load(open(HERE+'/config.json')).get('fps',30))
except Exception: FPS=30
exp=sum(c['frames'] for c in EDL); problems=[]
j=json.loads(subprocess.run(['ffprobe','-v','error','-count_frames','-show_entries','stream=codec_type,nb_read_frames,width,height,r_frame_rate,start_time,sample_rate','-of','json',v],capture_output=True,text=True).stdout)
V=[s for s in j['streams'] if s['codec_type']=='video'][0]; A=[s for s in j['streams'] if s['codec_type']=='audio']
print('video',V['width'],V['height'],V['r_frame_rate'],'frames',V['nb_read_frames'],'expected',exp)
if int(V['nb_read_frames'])!=exp: problems.append('frame count mismatch')
if (V['width'],V['height'])!=(1920,1080): problems.append('not 1920x1080')
if not A: problems.append('no audio')
else:
    print('audio start_time',A[0]['start_time'],'sr',A[0]['sample_rate'])
    if abs(float(A[0]['start_time']))>0.05: problems.append('audio start_time != 0')
W,H=96,54; p=subprocess.Popen(['ffmpeg','-v','error','-i',v,'-an','-vf',f'scale={W}:{H},format=gray','-f','rawvideo','-'],stdout=subprocess.PIPE)
prev=None; d=[]
while True:
    b=p.stdout.read(W*H)
    if len(b)<W*H: break
    f=np.frombuffer(b,np.uint8).astype(np.float32); d.append(0.0 if prev is None else float(np.abs(f-prev).mean())); prev=f
d=np.array(d); bounds=[int(x) for x in np.cumsum([c['frames'] for c in EDL])[:-1]]
def weak(b):
    loc=np.median(np.concatenate([d[max(b-12,0):max(b-2,0)],d[b+8:b+18]])); return d[max(b-1,0):b+2].max()<max(6,3*loc+4)
wk=[(c['id'],b) for c,b in zip(EDL[1:],bounds) if c['sec']!='intro' and weak(b)]; print('boundaries with NO cut spike:',wk)
if wk: problems.append('boundaries without cut')
st=set(); [st.update(range(b,b+7)) for b in bounds]
sp=[(i,round(float(d[i]),1)) for i in range(7,len(d)) if d[i]>30 and i not in st]; print('internal spikes >30:',sp)
if sp: problems.append('possible hidden cuts (inspect sheets at these frames)')
if A:
    vd=subprocess.run(['ffmpeg','-hide_banner','-i',v,'-vn','-af','volumedetect','-f','null','-'],capture_output=True,text=True).stderr
    mx=re.search(r'max_volume: (-?[\d.]+) dB',vd); h0=re.search(r'histogram_0db: (\d+)',vd); print('max_volume',mx and mx.group(1),'dB | samples at 0dB:',h0 and h0.group(1))
    if mx and float(mx.group(1))>=-0.1 or (h0 and int(h0.group(1))>0): problems.append('audio at/near full scale (clipping risk)')
# labelled contact sheets, 1 tile/second via accurate fast-seek
ids=[]; 
for c in EDL: ids+= [c['id']]*c['frames']
tw,th,cols,per=240,135,8,40; n=int(exp/FPS); tmp=tempfile.mkdtemp()
try: font=ImageFont.truetype('/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf',13)
except Exception: font=ImageFont.load_default()
sheets=[]
for s0 in range(0,n,per):
    ts=list(range(s0,min(s0+per,n))); im=Image.new('RGB',(cols*tw,((len(ts)+cols-1)//cols)*th)); dr=ImageDraw.Draw(im)
    for k,t in enumerate(ts):
        fp=f'{tmp}/{t}.jpg'; subprocess.run(['ffmpeg','-v','error','-y','-ss',str(t+0.5),'-i',v,'-frames:v','1','-vf',f'scale={tw}:{th}','-q:v','4',fp],timeout=60)
        x,y=(k%cols)*tw,(k//cols)*th
        if os.path.exists(fp): im.paste(Image.open(fp),(x,y))
        dr.rectangle([x,y,x+92,y+16],fill=(0,0,0)); dr.text((x+3,y),f'{t}s {ids[min(int((t+0.5)*FPS),len(ids)-1)]}',fill=(255,255,0),font=font)
    pth=f'{sd}/qa_sheet_{s0//per+1}.jpg'; im.save(pth,quality=80); sheets.append(pth)
shutil.rmtree(tmp,ignore_errors=True); print('sheets:',sheets)
print('QA RESULT:','PASS (automated checks only; visual review of sheets still required)' if not problems else 'ATTENTION: '+'; '.join(problems))
