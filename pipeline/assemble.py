"""python3 assemble.py MUSIC L|auto OUT.mp4 . Refuses unless render_status.json matches current edl.json with 0 fails and every clip verified."""
import json, os, sys, subprocess, hashlib
HERE=os.path.dirname(os.path.abspath(__file__)); B='/home/claude/work/build'
music,Larg,out=sys.argv[1],sys.argv[2],sys.argv[3]
try: FPS=int(json.load(open(HERE+'/config.json')).get('fps',30))
except Exception: FPS=30
EDLTXT=open(HERE+'/edl.json').read(); EDL=json.loads(EDLTXT)
try: st=json.load(open(B+'/render_status.json'))
except Exception: sys.exit('ABORT: no render_status.json (render not finished)')
if st['edl_sha1']!=hashlib.sha1(EDLTXT.encode()).hexdigest(): sys.exit('ABORT: render was for a different EDL')
if st['fails']: sys.exit(f"ABORT: failed clips {st['fails']}")
def nbf(p):
    r=subprocess.run(['ffprobe','-v','error','-select_streams','v:0','-show_entries','stream=nb_frames','-of','csv=p=0',p],capture_output=True,text=True); return int(r.stdout.strip().split(',')[0])
lines=[]; tot=0
for c in EDL:
    p=f"{B}/clips/{c['id']}.mp4"
    if not(os.path.exists(p) and os.path.exists(p+'.ok')): sys.exit('ABORT: missing '+p)
    if nbf(p)!=c['frames']: sys.exit(f"ABORT: {c['id']} has {nbf(p)} frames, expected {c['frames']}")
    lines.append(f"file '{p}'"); tot+=c['frames']
open(B+'/list.txt','w').write('\n'.join(lines)+'\n')
r=subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','csv=p=0',music],capture_output=True,text=True); mlen=float(r.stdout.strip())
L=json.load(open(HERE+'/beat.json'))['end'] if Larg=='auto' else float(Larg)
if L>mlen+0.05: sys.exit(f'ABORT: L={L} longer than music {mlen}')
if abs(tot/FPS-L)>0.04: sys.exit(f'ABORT: video {tot/FPS:.3f}s vs L={L:.3f}s (fix EDL final_len)')
tmp=out+'.part.mp4'; af=f'volume=-2dB,afade=t=out:st={L-0.45}:d=0.45,atrim=0:{L}'
base=['ffmpeg','-v','error','-y','-f','concat','-safe','0','-i',B+'/list.txt','-i',music,'-map','0:v:0','-map','1:a:0']
tail=['-af',af,'-c:a','aac','-b:a','256k','-ar','44100','-t',str(L),'-movflags','+faststart',tmp]
r=subprocess.run(base+['-c:v','copy']+tail,capture_output=True,text=True)
if r.returncode!=0:
    print('stream-copy concat failed, FALLBACK re-encode:',r.stderr[-200:])
    r=subprocess.run(base+['-c:v','libx264','-preset','slow','-crf','15','-pix_fmt','yuv420p','-r',str(FPS),'-g',str(FPS),'-x264-params',f'keyint={FPS}:min-keyint={FPS}:scenecut=0','-colorspace','bt709','-color_primaries','bt709','-color_trc','bt709']+tail,capture_output=True,text=True)
    if r.returncode!=0: sys.exit('ABORT: both assembly paths failed: '+r.stderr[-300:])
d=float(subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','csv=p=0',tmp],capture_output=True,text=True).stdout.strip())
if abs(d-L)>0.1: sys.exit(f'ABORT: output {d:.2f}s != {L:.2f}s')
os.replace(tmp,out); print(f'assembled {out} {d:.2f}s')
