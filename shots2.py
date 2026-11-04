import sys, json, subprocess, os, shutil
from PIL import Image, ImageDraw, ImageFont
S='/home/claude/work/src'; O='/home/claude/work/shots'; tw,th,lw,per=256,144,170,8
try: font=ImageFont.truetype('/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf',15)
except Exception: font=ImageFont.load_default()
for name in sys.argv[1:]:
    fd=f'/tmp/fr_{name}'; shutil.rmtree(fd,ignore_errors=True); os.makedirs(fd)
    subprocess.run(['ffmpeg','-v','error','-i',f'{S}/{name}.mp4','-an','-vf',f'fps=2,scale={tw}:{th}','-q:v','5',f'{fd}/%05d.jpg'],check=True); print(name,'frames',len(os.listdir(fd)),flush=True)
    rows=json.load(open(f'{O}/{name}_rows.json'))
    for si in range(0,len(rows),per):
        chunk=rows[si:si+per]; im=Image.new('RGB',(lw+4*tw,len(chunk)*th)); dr=ImageDraw.Draw(im)
        for ri,r in enumerate(chunk):
            y=ri*th; dr.text((4,y+6),r['id'],fill=(255,255,0),font=font); dr.text((4,y+30),f"{r['s']:.1f}-{r['e']:.1f}",fill=(255,255,255),font=font)
            dr.text((4,y+54),f"{r['e']-r['s']:.1f}s mo{r['motion']}",fill=(180,180,255),font=font); dr.text((4,y+78),f"shot {r['shot_start']:.0f}-{r['shot_end']:.0f}",fill=(150,150,150),font=font)
            for j in range(4):
                tt=r['s']+(r['e']-r['s'])*(0.08+0.28*j); k=int(round(tt*2))+1; fp=f'{fd}/{k:05d}.jpg'
                if os.path.exists(fp): im.paste(Image.open(fp),(lw+j*tw,y))
        im.save(f'{O}/{name}_sheet_{si//per:02d}.jpg',quality=82)
    print(name,'DONE sheets',(len(rows)+per-1)//per,flush=True)
