#!/usr/bin/env python3
"""Entry point on the runner: python3 autonomy/run_pipeline.py   (everything happens inside, supervised by the Mechanic)"""
import os, sys, json, re, time, glob, math, shutil, subprocess
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import mechanic as M
try: import gemini_router as G
except Exception: G=None
HERE,WORK,PIPE,BUILD=M.HERE,M.WORK,M.PIPE,M.BUILD; SRC=os.environ.get('SRC_DIR',WORK+'/src')
def ff(path,entries='format=duration:stream=r_frame_rate,width,height'):
    r=subprocess.run(['ffprobe','-v','error','-select_streams','v:0','-show_entries',entries,'-of','json',path],capture_output=True,text=True); return json.loads(r.stdout)
def main():
    m=M.Mechanic(gemini=G); S={'player':os.environ.get('PLAYER_NAME','LAMINE YAMAL'),'club':os.environ.get('CLUB_NAME','FC BARCELONA'),'known':False,'final_len':None,'out':None,'srcs':[]}
    py=lambda script,*a,**k: m.run_cmd([sys.executable,script,*map(str,a)],**k)
    def prepare():
        for d in ('src','build/clips','fine','analysis','qa','shots','cards'): os.makedirs(f'{WORK}/{d}',exist_ok=True)
        m.run_cmd('ffmpeg -version | head -1 && ffprobe -version | head -1',cwd=WORK)
        if not os.path.exists('/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf'): m.notices.append('Poppins font missing: cards use a fallback font')
        if G:
            rep=G.report(); m.notices+= [x for x in rep.splitlines() if 'NO AI' in x or 'Primary' in x or 'Requested' in x]
    def fetch():
        if os.environ.get('SKIP_FETCH')=='1': return
        py(HERE+'/fetch_drive.py',cwd=WORK)
    def inventory():
        info={}
        for s in ('gal','dor','rma'):
            p=f'{SRC}/{s}.mp4'
            if os.path.exists(p):
                j=ff(p); n,d=j['streams'][0]['r_frame_rate'].split('/'); info[s]=dict(fps=float(n)/float(d),dur=float(j['format']['duration']),w=j['streams'][0]['width'],h=j['streams'][0]['height'])
        if not info: raise M.StageFail('no source videos found (gal/dor/rma.mp4)')
        for f in ('music.m4a','ref.mp4'):
            if not os.path.exists(f'{SRC}/{f}'): raise M.StageFail(f'missing {f}')
        json.dump(info,open(WORK+'/analysis/src_info.json','w')); S['srcs']=sorted(info)
    def style():
        py(PIPE+'/analyze_reference.py',SRC+'/ref.mp4',cwd=PIPE,timeout=900)
    def beat():
        res=[]
        def one(lo,hi):
            out=py(PIPE+'/beatgrid.py',SRC+'/music.m4a',lo,hi,cwd=PIPE,timeout=900); b=json.load(open(PIPE+'/beat.json')); b['bpm']=60/b['P']; res.append((b,out)); return out
        out=one(0.38,0.70); cands=[float(x) for x in re.findall(r'\d+\.\d+|\d+',(re.search(r'autocorr candidates BPM: \[(.*?)\]',out) or [0,''])[1])][:3]
        periods=[]
        for bpm in cands:
            for f in (1,0.5,1/3,2):
                p=60/bpm*f
                if 0.33<=p<=0.75 and not any(abs(p-q)/q<0.03 for q in periods): periods.append(p)
        for p in periods[:4]:
            if any(b['constant'] for b,_ in res): break
            one(round(p*0.96,4),round(p*1.04,4))
        good=[b for b,_ in res if b['constant']]
        if not good: raise M.Halt('tempo could not be verified as constant (period and phase must agree): refusing to lock a beat grid. Human review of the music needed.')
        best=sorted(good,key=lambda b:(not(80<=b['bpm']<=170),b['phase_spread']))[0]; best['final_len']=math.floor(best['end']*2)/2
        json.dump(best,open(PIPE+'/beat.json','w'),indent=1); S['final_len']=best['final_len']; m.log(f"beat grid: {best['bpm']:.2f} BPM, P={best['P']:.5f}, offset={best['OFF']:.3f}, phase spread {best['phase_spread']*1000:.0f} ms, final length {best['final_len']} s")
        if len(res)>1: m.fixes.append(f"beat: first broad fit was {res[0][0]['bpm']:.1f} BPM (constant={res[0][0]['constant']}); narrowed to the autocorrelation families and chose {best['bpm']:.2f} BPM")
    def scan():
        info=json.load(open(WORK+'/analysis/src_info.json')); todo=[s for s in S['srcs'] if not os.path.exists(f'{SRC}/full_{s}.npy') or os.path.getmtime(f'{SRC}/full_{s}.npy')<os.path.getmtime(f'{SRC}/{s}.mp4')]
        procs={s:subprocess.Popen([sys.executable,PIPE+'/fullscan.py',s],cwd=PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True) for s in todo}
        bad=[]
        for s,p in procs.items():
            out=p.communicate()[0]; m.logf.write(out[-2000:]+'\n')
            if p.returncode!=0 or not os.path.exists(f'{SRC}/full_{s}.npy'): bad.append((s,out[-400:]))
        if bad: raise M.StageFail('cut scan failed for '+', '.join(s for s,_ in bad),'\n'.join(t for _,t in bad))
    def plan():
        r=subprocess.run([sys.executable,HERE+'/apply_known_good.py'],capture_output=True,text=True,cwd=PIPE,env=dict(m.env,PIPE_DIR=PIPE)); m.log(r.stdout.strip()+(' | stderr: '+r.stderr.strip()[-300:] if r.returncode not in (0,3) or not r.stdout.strip() else ''))
        if r.returncode==0:
            S['known']=True; mm=re.search(r'player (.+?) club (.+)$',r.stdout.strip()); 
            if mm: S['player'],S['club']=mm.group(1),mm.group(2)
            return
        info=json.load(open(WORK+'/analysis/src_info.json')); env={'ROWS_ONLY':'1'}
        for s in S['srcs']: py(PIPE+'/shots.py',s,info[s]['fps'],info[s]['dur'],2.6,cwd=PIPE,env=env)
        py(PIPE+'/shots2.py',*S['srcs'],cwd=PIPE,timeout=3000)
        py(HERE+'/ai_planner.py',S['player'],cwd=PIPE,env={'PIPE_DIR':PIPE},timeout=3000)
        n=json.load(open(WORK+'/analysis/plan_notes.json')); m.notices+=[x for x in n['notes'] if 'shortfall' in x.lower() or 'DEGRADED' in x]
    def edl(): py(PIPE+'/edl.py',cwd=PIPE)
    def watermark():
        cfg=json.load(open(PIPE+'/config.json')); info=json.load(open(WORK+'/analysis/src_info.json'))
        for s in S['srcs']:
            if s in cfg.get('delogo',{}): continue
            out=py(PIPE+'/watermark.py',s,int(info[s]['dur']),cwd=PIPE,timeout=900); mm=re.search(r'delogo \w+: (\d+:\d+:\d+:\d+)',out)
            if mm: cfg.setdefault('delogo',{})[s]=mm.group(1)
        json.dump(cfg,open(PIPE+'/config.json','w'),indent=1)
    def cards():
        py(HERE+'/make_cards.py',S['player'],S['club'],WORK+'/cards',cwd=PIPE); py(HERE+'/finalize.py',WORK+'/cards',cwd=PIPE,env={'PIPE_DIR':PIPE})
    def grade():
        cfg=json.load(open(PIPE+'/config.json'))
        if all(s in cfg.get('grade',{}) for s in S['srcs']): return
        py(PIPE+'/grade_fit.py',cwd=PIPE,timeout=1800)
    def render():
        if os.path.exists(BUILD+'/render_status.json'): os.remove(BUILD+'/render_status.json')
        out=py(PIPE+'/render.py',cwd=PIPE); st=json.load(open(BUILD+'/render_status.json'))
        if st['fails']: raise M.StageFail('render failed clips: '+str(st['fails']),out[-1500:])
        if st['degraded']: m.notices.append('render degraded clips (retry ladder used): '+str(st['degraded']))
    def assemble():
        S['out']=f"{BUILD}/{S['player'].title().replace(' ','_')}_Highlights.mp4"; py(PIPE+'/assemble.py',SRC+'/music.m4a',S['final_len'],S['out'],cwd=PIPE)
    def qa():
        out=py(PIPE+'/qa.py',S['out'],WORK+'/qa',cwd=PIPE,timeout=1800); line=[l for l in out.splitlines() if l.startswith('QA RESULT')]
        hard=[w for w in ('frame count mismatch','not 1920x1080','no audio','audio start_time') if line and w in line[0]]
        if hard: raise M.StageFail('QA hard failure: '+', '.join(hard),out[-1200:])
        if line and 'ATTENTION' in line[0]: m.notices.append('QA notices (advisory, inspect the sheets): '+line[0][:220])
    def verify_output():
        j=ff(S['out']); st=j['streams'][0]; n,d=st['r_frame_rate'].split('/'); dur=float(j['format']['duration'])
        if (st['width'],st['height'])!=(1920,1080) or abs(float(n)/float(d)-30)>0.01 or abs(dur-S['final_len'])>0.1: raise M.Halt(f'output spec violated protected goals: {st["width"]}x{st["height"]} {float(n)/float(d):.2f}fps {dur:.2f}s vs 1920x1080 30fps {S["final_len"]}s')
    def export():
        e=HERE+'/drive_export.py'
        if not (os.path.exists(e) and os.environ.get('APPS_SCRIPT_URL')): m.notices.append('Drive export not configured: video kept as workflow artifact'); open(BUILD+'/export_failed','w').write('not configured'); return
        ef=BUILD+'/export_failed'
        try: py(e,S['out'],cwd=PIPE,timeout=3600)
        except BaseException: open(ef,'w').write('export failed'); raise
        if os.path.exists(ef): os.remove(ef)
    try:
        for name,fn,kw in (('prepare',prepare,{}),('fetch',fetch,{'retries':3}),('inventory',inventory,{}),('style',style,{'optional':True,'retries':1}),('beat',beat,{'retries':1}),('scan',scan,{}),
                           ('plan',plan,{}),('edl',edl,{'retries':4}),('watermark',watermark,{'optional':True,'retries':1}),('cards',cards,{}),('grade',grade,{'optional':True,'retries':1}),
                           ('render',render,{'retries':3}),('assemble',assemble,{}),('qa',qa,{'retries':1}),('verify_output',verify_output,{'retries':0}),('export',export,{'optional':True,'retries':2})):
            m.stage(name,fn,**kw)
        extra=f"Video: {S['out']}\nPlayer: {S['player']} / {S['club']} | plan: {'known-good (reproduced)' if S['known'] else 'AI/heuristic planner'} | final length {S['final_len']} s"
        status='SUCCESS' if not m.unsolved else 'SUCCESS WITH WARNINGS'; m.notify('success',f'[Video pipeline] {status}',m.summary_text(status,extra))
        sg=m.suggest()
        if sg: m.notify('notice','[Video pipeline] Upgrade suggestions (need your approval, nothing applied)','\n'.join(' - '+x for x in sg))
        return 0
    except M.Halt as e:
        m.notify('failure','[Video pipeline] HALTED: needs you',m.summary_text('HALTED',f'Reason: {e}')); return 1
    except Exception as e:
        m.unsolved.append(f'unexpected crash: {type(e).__name__}: {e}'); m.notify('failure','[Video pipeline] CRASHED',m.summary_text('CRASHED',traceback.format_exc()[-1500:] if False else str(e))); return 1
import traceback
if __name__=='__main__': sys.exit(main())
    
