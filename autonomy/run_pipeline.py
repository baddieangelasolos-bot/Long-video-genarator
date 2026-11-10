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
def _guard(self,where=''):
    """Protected-goal check (goals.json hash, exact length, minimum clip, wide-shot cap) using the run's own fps and style instead of fixed 30 fps / 90 frames."""
    import hashlib
    if hashlib.sha256(open(M.GOALS_PATH,'rb').read()).hexdigest()!=self.goals_sha: raise M.Halt('goals.json changed during the run: protected goals must never be edited by automation')
    def j(p,d):
        try: return json.load(open(p))
        except Exception: return d
    p=PIPE+'/edl.json'
    if os.path.exists(p):
        e=json.load(open(p)); fps=int(j(PIPE+'/config.json',{}).get('fps',30)); st=j(PIPE+'/style.json',{}); minf=int(round(float(st.get('min_clip_s',3.0))*fps)); frames=sum(c['frames'] for c in e)
        fl=j(PIPE+'/edl_input.json',{}).get('final_len')
        if fl and abs(frames-round(fl*fps))>1: raise M.Halt(f'{where}: edit list is {frames} frames but the planned length is {fl}s = {round(fl*fps)} frames')
        if any(c['frames']<minf and not c.get('pad') for c in e): raise M.Halt(f'{where}: a clip is shorter than the {minf/fps:.1f} s minimum')
        cap=float(st.get('wide_share_max',0.25)); ws=j(os.path.join(WORK,'analysis','plan_notes.json'),{}).get('stats',{}).get('wide_share',0)
        if ws>cap+1e-9: raise M.Halt(f'{where}: wide-shot share {ws} exceeds the {cap:.0%} cap')
M.Mechanic.guard=_guard
def main():
    m=M.Mechanic(gemini=G); S={'player':'HIGHLIGHTS','club':'','identity':'','known':False,'final_len':None,'out':None,'srcs':[]}
    py=lambda script,*a,**k: m.run_cmd([sys.executable,script,*map(str,a)],**k)
    def prepare():
        for d in ('src','build/clips','fine','analysis','qa','shots','cards'): os.makedirs(f'{WORK}/{d}',exist_ok=True)
        m.run_cmd('ffmpeg -version | head -1 && ffprobe -version | head -1',cwd=WORK)
        if not os.path.exists('/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf'): m.notices.append('Poppins font missing: cards use a fallback font')
        if os.environ.get('APPS_SCRIPT_URL'):
            try:
                r=subprocess.run([sys.executable,HERE+'/drive_export.py','--ping'],capture_output=True,text=True,timeout=300); txt=(r.stdout+r.stderr).strip()[-600:]
                m.log('export preflight: '+txt.replace('\n',' | '))
                if r.returncode!=0:
                    m.notices.append('Drive export preflight FAILED (render continues): '+txt); m.notify('notice','[Video pipeline] Drive export will fail - fix now',txt)
            except Exception as e: m.log(f'export preflight skipped: {type(e).__name__}')
        if G:
            rep=G.report(); m.notices+= [x for x in rep.splitlines() if 'NO AI' in x or 'Primary' in x or 'Requested' in x]
    def fetch():
        if os.environ.get('SKIP_FETCH')=='1': return
        py(HERE+'/fetch_drive.py',cwd=WORK)
    def pick_fps(info):
        """Output frame rate follows the footage (24/25/30/50/60, capped by MAX_FPS) so motion stays smooth instead of being forced to 30."""
        if os.environ.get('FPS_OVERRIDE') in ('24','25','30','50','60'): return int(os.environ['FPS_OVERRIDE'])
        cap=int(os.environ.get('MAX_FPS','60')); w={}
        for i in info.values(): near=min((24,25,30,50,60),key=lambda x:abs(x-i['fps'])); w[near]=w.get(near,0)+i['dur']
        return min(cap,max(w,key=w.get)) if w else 30
    def inventory():
        info={}
        names=sorted(f[:-4] for f in os.listdir(SRC) if re.fullmatch(r's\d\d\.mp4',f)) or [s for s in ('gal','dor','rma') if os.path.exists(f'{SRC}/{s}.mp4')]
        for s in names:
            p=f'{SRC}/{s}.mp4'; j=ff(p); n,d=j['streams'][0]['r_frame_rate'].split('/'); dur=float(j['format']['duration'])
            if dur<4: m.notices.append(f'{s} skipped: shorter than 4 s'); continue
            info[s]=dict(fps=float(n)/float(d),dur=dur,w=j['streams'][0]['width'],h=j['streams'][0]['height'])
        if not info: raise M.StageFail('no usable source videos found in the source folder')
        if not os.path.exists(f'{SRC}/music.m4a'): raise M.StageFail('missing music.m4a')
        json.dump(info,open(WORK+'/analysis/src_info.json','w')); S['srcs']=sorted(info)
        cfg=json.load(open(PIPE+'/config.json')); cfg.update(fps=pick_fps(info),delogo={},grade={}); json.dump(cfg,open(PIPE+'/config.json','w'),indent=1)
        m.log(f"{len(info)} source video(s), output {cfg['fps']} fps: "+', '.join(f"{s} {info[s]['w']}x{info[s]['h']}@{info[s]['fps']:.0f} {info[s]['dur']:.0f}s" for s in S['srcs']))
    def identify():
        try: py(HERE+'/ai_director.py','identify',cwd=PIPE,env={'PIPE_DIR':PIPE,'WORK_DIR':WORK,'SRC_DIR':SRC},timeout=1200)
        except M.StageFail as e: m.notices.append('player identification failed, neutral title used: '+str(e)[:100])
        try: r=json.load(open(WORK+'/analysis/identity.json')); S['player'],S['club'],S['identity']=r['player'],r['club'],r['how']
        except Exception: pass
        m.log(f"title: {S['player']} / {S['club'] or '-'} ({S['identity']})")
    def style():
        if not os.path.exists(SRC+'/ref.mp4'): m.log('no reference video: default editing style and rules'); return
        py(PIPE+'/analyze_reference.py',SRC+'/ref.mp4',cwd=PIPE,timeout=900)
    def style_ai():
        try: py(HERE+'/ai_director.py','style',cwd=PIPE,env={'PIPE_DIR':PIPE,'WORK_DIR':WORK,'SRC_DIR':SRC},timeout=1800)
        except M.StageFail as e:
            m.notices.append('reference style analysis failed, default style used: '+str(e)[:100]); sys.path.insert(0,HERE); import ai_director as D; json.dump(D.DEFAULT_STYLE,open(PIPE+'/style.json','w'),indent=1)
        try: S['style']=json.load(open(PIPE+'/style.json'))
        except Exception: S['style']={}
        ue=(S.get('style') or {}).get('unrecognized_effects') or []
        if ue:
            m.notices.append('effects seen in the reference that this pipeline cannot reproduce: '+'; '.join(ue))
            S.setdefault('sug',[]).append('Reference effects not reproducible yet (upload example clips or descriptions to the effects folder and ask Claude to add them): '+'; '.join(ue))
            m.notify('notice','[Video pipeline] Reference effects I could not reproduce','These effects or transitions appear in the reference but are not in the pipeline vocabulary (hard cut, white/black flash, RGB split, zoom punch, fade, invert flash, blur pulse):\n - '+'\n - '.join(ue)+'\nUpload examples to the effects folder in Drive, then ask Claude to add them as new render tools.')
        elif S.get('style',{}).get('source')=='reference' and not (S['style'].get('gemini')):
            S.setdefault('sug',[]).append('Gemini could not watch the reference this run, so effects were measured only (cuts, flashes, colour). Re-run when Gemini is available.')
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
        n=json.load(open(WORK+'/analysis/plan_notes.json')); m.notices+=[x for x in n['notes'] if 'shortfall' in x.lower() or 'DEGRADED' in x or 'SHORTER VIDEO' in x]
        S.setdefault('sug',[]).extend(n.get('suggestions',[]))
        try: S['final_len']=json.load(open(PIPE+'/edl_input.json'))['final_len']      # (planner may shorten)
        except Exception: pass
        tgt=n.get('target') or 0
        if os.environ.get('ALLOW_SHORT')!='1' and tgt and S.get('final_len') and S['final_len']<0.85*tgt:
            sp=n.get('supply_seconds',{})
            raise M.Halt(f"NEEDS MORE SOURCE VIDEO. Usable footage only covers {S['final_len']}s of the {tgt}s music. Usable seconds found: {sp}. Needed: roughly {n.get('need_nonwide_s')}s of non-wide footage (close-ups, dribbling, skills) plus pre-match and celebration clips. "+' | '.join(n.get('suggestions',[]))+" WHAT TO DO: upload more clips to the source video folder in Drive, then open this run in GitHub Actions and tap Re-run failed jobs (or run the workflow again). Files are re-read from Drive on every run.")
        try: pass      # the planner may shorten the video when footage is short
        except Exception: pass
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
        base=re.sub(r'[^A-Za-z0-9_]+','_',S['player'].title()).strip('_') or 'Video'; S['out']=f"{BUILD}/{base}.mp4" if base=='Highlights' else f"{BUILD}/{base}_Highlights.mp4"; py(PIPE+'/assemble.py',SRC+'/music.m4a',S['final_len'],S['out'],cwd=PIPE)
    def qa():
        out=py(PIPE+'/qa.py',S['out'],WORK+'/qa',cwd=PIPE,timeout=1800); line=[l for l in out.splitlines() if l.startswith('QA RESULT')]
        hard=[w for w in ('frame count mismatch','not 1920x1080','no audio','audio start_time') if line and w in line[0]]
        if hard: raise M.StageFail('QA hard failure: '+', '.join(hard),out[-1200:])
        if line and 'ATTENTION' in line[0]: m.notices.append('QA notices (advisory, inspect the sheets): '+line[0][:220])
    def verify_output():
        FPSOUT=int(json.load(open(PIPE+'/config.json')).get('fps',30)); j=ff(S['out']); st=j['streams'][0]; n,d=st['r_frame_rate'].split('/'); dur=float(j['format']['duration'])
        if (st['width'],st['height'])!=(1920,1080) or abs(float(n)/float(d)-FPSOUT)>0.01 or abs(dur-S['final_len'])>0.1: raise M.Halt(f'output spec violated protected goals: {st["width"]}x{st["height"]} {float(n)/float(d):.2f}fps {dur:.2f}s vs 1920x1080 30fps {S["final_len"]}s')
    def export():
        e=HERE+'/drive_export.py'
        if not (os.path.exists(e) and os.environ.get('APPS_SCRIPT_URL')): m.notices.append('Drive export not configured: video kept as workflow artifact'); open(BUILD+'/export_failed','w').write('not configured'); return
        ef=BUILD+'/export_failed'
        try: py(e,S['out'],cwd=PIPE,timeout=3600)
        except BaseException: open(ef,'w').write('export failed'); raise
        if os.path.exists(ef): os.remove(ef)
    try:
        for name,fn,kw in (('prepare',prepare,{}),('fetch',fetch,{'retries':3}),('inventory',inventory,{}),('identify',identify,{'optional':True,'retries':0}),('style',style,{'optional':True,'retries':1}),('style_ai',style_ai,{'optional':True,'retries':0}),('beat',beat,{'retries':1}),('scan',scan,{}),
                           ('plan',plan,{}),('edl',edl,{'retries':4}),('watermark',watermark,{'optional':True,'retries':1}),('cards',cards,{}),('grade',grade,{'optional':True,'retries':1}),
                           ('render',render,{'retries':3}),('assemble',assemble,{}),('qa',qa,{'retries':1}),('verify_output',verify_output,{'retries':0}),('export',export,{'optional':True,'retries':0})):
            m.stage(name,fn,**kw)
        sty=S.get('style') or {}; extra=f"Video: {S['out']}\nSources: {len(S['srcs'])} | title: {S['player']} / {S['club'] or '-'} ({S['identity']}) | style: {sty.get('source','?')} - {str(sty.get('describe',''))[:160]}\nPlayer: {S['player']} / {S['club']} | plan: {'known-good (reproduced)' if S['known'] else 'AI/heuristic planner'} | final length {S['final_len']} s"
        status='SUCCESS' if not m.unsolved else 'SUCCESS WITH WARNINGS'; m.notify('success',f'[Video pipeline] {status}',m.summary_text(status,extra))
        sg=m.suggest(extra=S.get('sug',[]))
        try:
            rf=json.load(open(PIPE+'/style_profile.json')).get('fps'); mx=max(i['fps'] for i in json.load(open(WORK+'/analysis/src_info.json')).values())
            if rf and mx and rf>mx+5: sg.append(f'Reference is {rf:.0f} fps but your footage is {mx:.0f} fps: the output follows the footage; true {rf:.0f} fps smoothness needs {rf:.0f} fps source clips.')
        except Exception: pass
        m.notify('notice','[Video pipeline] Suggestions after this run (nothing applied)','\n'.join(' - '+x for x in sg) if sg else 'No suggestions this run.')
        return 0
    except M.Halt as e:
        m.notify('failure','[Video pipeline] HALTED: needs you',m.summary_text('HALTED',f'Reason: {e}')); return 1
    except Exception as e:
        m.unsolved.append(f'unexpected crash: {type(e).__name__}: {e}'); m.notify('failure','[Video pipeline] CRASHED',m.summary_text('CRASHED',traceback.format_exc()[-1500:] if False else str(e))); return 1
import traceback
if __name__=='__main__': sys.exit(main())
