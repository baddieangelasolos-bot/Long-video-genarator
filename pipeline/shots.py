import sys, json, subprocess, numpy as np, os, tempfile, shutil
from PIL import Image, ImageDraw, ImageFont
from cutq import cuts
name,fps,dur,minlen=sys.argv[1],float(sys.argv[2]),float(sys.argv[3]),float(sys.argv[4]); S='/home/claude/work/src'; O='/home/claude/work/shots'
c=cuts(name,0,dur,fps); t=[0.0]+[x[0] for x in c]+[dur]; d=np.load(f'{S}/full_{name}.npy')
rows=[]
for a,b in zip(t[:-1],t[1:]):
    if b-a<minlen: continue
    ua,ub=a+0.1,b-0.1; k=int(np.ceil((ub-ua)/10.0)) or 1
    for i in range(k):
        s0=ua+i*(ub-ua)/k; e0=ua+(i+1)*(ub-ua)/k; mot=float(d[int(s0*fps):max(int(e0*fps),int(s0*fps)+1)].mean())
        rows.append(dict(src=name,shot_start=round(a,2),shot_end=round(b,2),s=round(s0,2),e=round(e0,2),motion=round(mot,1)))
for i,r in enumerate(rows): r['id']=f'{name}-{i:02d}'
json.dump(rows,open(f'{O}/{name}_rows.json','w'),indent=0); print(name,len(rows),'rows',flush=True)
if os.environ.get('ROWS_ONLY'): sys.exit(0)
tw,th,lw=256,144,170; per=8; tmp=tempfile.mkdtemp()
try: font=ImageFont.truetype('/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf',15)
except Exception: font=ImageFont.load_default()
for si in range(0,len(rows),per):
    chunk=rows[si:si+per]; im=Image.new('RGB',(lw+4*tw,len(chunk)*th)); dr=ImageDraw.Draw(im)
    for ri,r in enumerate(chunk):
        y=ri*th; dr.text((4,y+6),r['id'],fill=(255,255,0),font=font); dr.text((4,y+30),f"{r['s']:.1f}-{r['e']:.1f}",fill=(255,255,255),font=font)
        dr.text((4,y+54),f"{r['e']-r['s']:.1f}s mo{r['motion']}",fill=(180,180,255),font=font); dr.text((4,y+78),f"shot {r['shot_start']:.0f}-{r['shot_end']:.0f}",fill=(150,150,150),font=font)
        for j in range(4):
            tt=r['s']+(r['e']-r['s'])*(0.08+0.28*j); fp=f'{tmp}/{ri}_{j}.jpg'
            subprocess.run(['ffmpeg','-v','error','-y','-ss',f'{tt:.2f}','-i',f'{S}/{name}.mp4','-frames:v','1','-vf',f'scale={tw}:{th}','-q:v','5',fp],timeout=60)
            if os.path.exists(fp): im.paste(Image.open(fp),(lw+j*tw,y))
    im.save(f'{O}/{name}_sheet_{si//per:02d}.jpg',quality=82); print(name,'sheet',si//per,flush=True)
shutil.rmtree(tmp,ignore_errors=True); print(name,'DONE',flush=True)
