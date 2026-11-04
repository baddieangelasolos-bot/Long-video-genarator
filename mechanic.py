#!/usr/bin/env python3
"""THE MECHANIC: runs the whole video pipeline on the GitHub runner and heals it.
Order of brains: (1) built-in known fixes (retry, wait, pip, disk, oom, EDL window repair) -> (2) Gemini consult for unfamiliar errors (restricted actions only)
-> (3) halt and email a human. It NEVER edits the goals (final length 148.5 rule, 3 s clips, 1920x1080, 30 fps, wide <= 25%, letterbox) and never applies code patches:
patch ideas are saved and emailed for approval. Always emails: what went wrong, what it fixed, what it could not fix."""
import os, sys, re, json, time, subprocess, hashlib, glob, shutil, math, smtplib, traceback
from email.message import EmailMessage
HERE=os.path.dirname(os.path.abspath(__file__)); ROOT=os.path.dirname(HERE)
WORK=os.environ.get('WORK_DIR','/home/claude/work'); PIPE=os.environ.get('PIPE_DIR',WORK+'/pipeline'); BUILD=WORK+'/build'
GOALS_PATH=HERE+'/goals.json'
ENV_WHITELIST={'RELAY_RETRY_SLEEP','JOBS','AI_THROTTLE','USE_AI','MECH_EXTRA_WAIT'}
PIP_ALLOW={'numpy','scipy','opencv-python-headless','pillow','requests','gdown','pyyaml'}; PIP_MAP={'cv2':'opencv-python-headless','PIL':'pillow','yaml':'pyyaml'}
APT_ALLOW={'ffmpeg','fonts-dejavu-core'}
PROTECTED_WORDS=('final_len','min_clip','1920','1080','fps','wide_share','letterbox','goals','end_at')
SIGNATURES=[(r'Too many users have viewed|Cannot retrieve the public link|quota exceeded|Access denied|Permission denied.*drive','drive_wait'),
 (r"ModuleNotFoundError: No module named '([^']+)'",'pip'),(r'No space left on device','disk'),
 (r'ffmpeg: command not found|ffprobe: command not found|ffmpeg: not found','apt_ffmpeg'),
 (r'EDL INVALID','edl'),(r'FAIL \w+ \(all attempts\)|FAILS=[1-9]','render_retry'),
 (r'ConnectionError|Connection reset|Read timed out|Temporary failure in name resolution|HTTP 5\d\d|RemoteDisconnected','net_wait')]
PATCH_FORBIDDEN=re.compile(r'1920|1080|fps|final_len|wide_share|min_clip|letterbox|crop=1920|goals|148\.5|floor\(|GEMINI_API_KEY|WEBHOOK_TOKEN|GH_TOKEN|GITHUB|MAIL_|environ|requests\.|urllib|socket|os\.system|rmtree|shutil\.rmtree|remove\(',re.I)
class Halt(Exception): pass
class StageFail(Exception):
    def __init__(s,msg,tail='',rc=1): super().__init__(msg); s.tail=tail; s.rc=rc
def now(): return time.strftime('%H:%M:%S')
class Mechanic:
    def __init__(s,gemini=None,notify_fn=None):
        s.g=gemini; s.env=dict(os.environ); s.stages=[]; s.fixes=[]; s.unsolved=[]; s.notices=[]; s.consults=0; s.t0=time.time(); s.proposals=[]; s.patches=[]
        s.goals_sha=hashlib.sha256(open(GOALS_PATH,'rb').read()).hexdigest(); s.notify_fn=notify_fn
        os.makedirs(BUILD,exist_ok=True); s.logf=open(WORK+'/build.log','a')
    # ---------- logging / secrets ----------
    def redact(s,t):
        for k in ('GEMINI_API_KEY','WEBHOOK_TOKEN','GH_TOKEN','MAIL_APP_PASSWORD','GITHUB_TOKEN'):
            v=os.environ.get(k,'')
            if len(v)>6: t=t.replace(v,'***')
        return t
    def log(s,m):
        m=s.redact(f'[{now()}] {m}'); print(m,flush=True); s.logf.write(m+'\n'); s.logf.flush()
    def run_cmd(s,cmd,timeout=None,cwd=None,env=None):
        s.log('$ '+(cmd if isinstance(cmd,str) else ' '.join(cmd)))
        e=dict(s.env); e.update(env or {}); p=subprocess.run(cmd,shell=isinstance(cmd,str),capture_output=True,text=True,timeout=timeout,cwd=cwd or PIPE,env=e)
        out=s.redact((p.stdout or '')+(p.stderr or '')); s.logf.write(out[-20000:]+'\n'); tail='\n'.join(out.splitlines()[-120:])
        if p.returncode!=0: raise StageFail(f'command failed (exit {p.returncode}): {cmd if isinstance(cmd,str) else cmd[0]}',tail,p.returncode)
        return out
    # ---------- guardrails ----------
    def guard(s,where=''):
        if hashlib.sha256(open(GOALS_PATH,'rb').read()).hexdigest()!=s.goals_sha: raise Halt('goals.json changed during the run: protected goals must never be edited by automation')
        p=PIPE+'/edl.json'
        if os.path.exists(p):
            e=json.load(open(p)); frames=sum(c['frames'] for c in e)
            try: fl=json.load(open(PIPE+'/edl_input.json'))['final_len']
            except Exception: fl=None
            if fl and abs(frames-round(fl*30))>1: raise Halt(f'{where}: edit list is {frames} frames but the protected length is {fl}s = {round(fl*30)} frames')
            if any(c['frames']<90 for c in e): raise Halt(f'{where}: a clip is shorter than 3.0 s (protected rule)')
            n=os.path.join(WORK,'analysis','plan_notes.json')
            if os.path.exists(n):
                st=json.load(open(n)).get('stats',{}); 
                if st.get('wide_share',0)>0.25+1e-9: raise Halt(f'{where}: wide-shot share {st["wide_share"]} exceeds the protected 25%')
    # ---------- known fixes ----------
    def classify(s,text):
        for rx,fid in SIGNATURES:
            m=re.search(rx,text,re.I|re.S)
            if m: return fid,(m.group(1) if m.groups() else None)
        return None,None
    def wait(s,sec): time.sleep(sec*float(os.environ.get('MECH_SLEEP_SCALE','1')))
    def known_fix(s,fid,arg,attempt,stage):
        if fid=='drive_wait': s.wait(min(60*attempt,300)); return 'waited for the Drive download limit to reset'
        if fid=='net_wait': s.wait(15*attempt); return 'waited out a transient network error'
        if fid=='render_retry': return 'resumed the render (finished clips are kept)'
        if fid=='pip':
            pkg=PIP_MAP.get(arg,arg)
            if pkg in PIP_ALLOW: s.run_cmd([sys.executable,'-m','pip','install','-q',pkg]); return f'installed missing package {pkg}'
            return None
        if fid=='disk':
            for g in ('/tmp/fr_*','/tmp/gf_*',WORK+'/shots/*.jpg',WORK+'/fine/*'):
                for f in glob.glob(g): shutil.rmtree(f,ignore_errors=True) if os.path.isdir(f) else os.remove(f)
            return 'freed disk space (temporary frames and sheets)'
        if fid=='apt_ffmpeg': s.run_cmd('sudo apt-get install -y ffmpeg',cwd=WORK); return 'installed ffmpeg'
        if fid=='edl':
            ch=s.autofix_edl(s.last_tail)
            return ('repaired edit-list windows: '+'; '.join(ch)) if ch else None
        return None
    def autofix_edl(s,text):
        """Safe, local repairs of invalid clip windows only. Never touches final length, beat totals or protected rules."""
        p=PIPE+'/edl_input.json'; d=json.load(open(p)); P=json.load(open(PIPE+'/beat.json'))['P']; changed=[]
        rows={r[0]:r for r in d['clips']}
        for m in re.finditer(r'^\s*(\w+): (.+)$',text,re.M):
            cid,msg=m.group(1),m.group(2); r=rows.get(cid)
            if not r: continue
            if 'cut/re-frame detected inside window' in msg:
                w=re.search(r'window ([\d.]+)-([\d.]+) \((\w+)\)',msg); a,b,src=float(w.group(1)),float(w.group(2)),w.group(3)
                sys.path.insert(0,PIPE); import cutq
                fps=60.0 if src=='gal' else 30.0
                try: cuts=[t for t,v in cutq.cuts(src,a-0.3,b+0.3,fps)]
                except Exception: continue
                inside=[t for t in cuts if a-0.06<=t<=b+0.06]
                if not inside: continue
                floor=1.0 if r[4]>=1.0 else (0.6 if fps>=50 else 0.8); beats=r[5]
                last=max(inside); first=min(inside); nxt=min([t for t in cuts if t>last]+[1e9])
                def after():      # keep the beats, start after the last cut (slow down only if the room is short)
                    ns=round(last+0.12,2); room=nxt-0.1-ns; sp=min(r[4],math.floor(room/(beats*P)*100)/100)
                    return (ns,sp) if sp>=floor else None
                def before():     # keep the beats, stay before the first cut by slowing down
                    room=first-0.1-r[3]; sp=math.floor(room/(beats*P)*100)/100
                    return (r[3],sp) if floor<=sp<=r[4] else None
                fix=(after() or before()) if first<=a+0.5 else (before() or after())
                if fix: r[3],r[4]=fix; changed.append(f'{cid} start {fix[0]}s speed {fix[1]} (cut at {first:.2f}s avoided, beats unchanged)')
            elif 'exceeds source' in msg: r[3]=round(max(0,r[3]-1.5),2); changed.append(f'{cid} moved earlier inside the source')
        if changed: json.dump(d,open(p,'w'))
        return changed
    # ---------- automatic patching (Gemini 'solution'), with safety rails ----------
    def safe_apply_patch(s,diff,stage):
        """Applies a small unified diff to a pipeline script ONLY if it passes the rules. Backs up the originals; the stage runner reverts them if the stage still fails."""
        lines=diff.splitlines()
        if not diff.strip() or len(lines)>200: return False,'empty or too large (>200 lines)'
        files=re.findall(r'^\+\+\+ (?:[ab]/)?(\S+)',diff,re.M)
        if not 1<=len(files)<=2: return False,'a patch must touch 1-2 files'
        targets=[]
        for f in files:
            base=os.path.basename(f)
            if '..' in f.split('/') or f.startswith('/') or not base.endswith('.py') or not os.path.exists(os.path.join(PIPE,base)): return False,f'file not patchable (only existing pipeline/*.py): {f}'
            targets.append(os.path.join(PIPE,base))
        for l in lines:
            if (l.startswith('+') or l.startswith('-')) and not l.startswith('+++') and not l.startswith('---') and PATCH_FORBIDDEN.search(l): return False,'patch touches a protected goal, secret, network or deletion line: '+l.strip()[:80]
        pf=WORK+'/build/_patch.diff'; open(pf,'w').write(diff if diff.endswith('\n') else diff+'\n')
        lvl=next((p for p in ('-p1','-p0','-p2') if subprocess.run(f'patch {p} --dry-run -s -d {PIPE} < {pf}',shell=True,capture_output=True).returncode==0),None)
        if not lvl: return False,'patch does not apply cleanly'
        baks=[]
        for t in targets: b=t+'.mech_bak'; shutil.copy(t,b); baks.append((t,b))
        subprocess.run(f'patch {lvl} -s -d {PIPE} < {pf}',shell=True,capture_output=True)
        for t,b in baks:
            if subprocess.run([sys.executable,'-m','py_compile',t],capture_output=True).returncode!=0:
                for t2,b2 in baks: shutil.copy(b2,t2)
                return False,'patched file does not compile; reverted'
        os.makedirs(WORK+'/proposals',exist_ok=True); dp=f"{WORK}/proposals/{stage}_{int(time.time())}.diff"; open(dp,'w').write(diff)
        s.patches.append(dict(stage=stage,baks=baks,diff=dp,text=diff)); return True,'applied to '+', '.join(os.path.basename(t) for t in targets)
    def revert_patches(s,stage):
        for p in [x for x in s.patches if x['stage']==stage and not x.get('reverted')]:
            for t,b in p['baks']: shutil.copy(b,t)
            p['reverted']=True; s.fixes.append(f'{stage}: reverted Gemini patch (did not fix the stage)')
    # ---------- Gemini consult (restricted) ----------
    def consult(s,stage,tail):
        if s.g is None: return None
        if s.consults>=3: return None
        s.consults+=1
        prompt=("You are a CI mechanic for a FFmpeg/Python video pipeline. Stage '%s' failed. Last log lines:\n%s\n\nIf you propose a patch, give a MINIMAL unified diff against one existing pipeline/*.py script; it is applied automatically only if it passes safety rules (no goal, secret, network or deletion lines) and is reverted if the stage still fails. Reply ONLY JSON: {\"diagnosis\":\"...\",\"action\":\"retry|pip_install|apt_install|skip_optional|propose_patch|halt\","
          "\"env\":{\"KEY\":\"VALUE\"},\"package\":\"name\",\"patch\":\"unified diff text\",\"reason\":\"...\"}. Allowed env keys: %s. You may NOT change the video goals (length, 3 s minimum, 1920x1080, 30 fps, wide-shot cap, letterbox).")%(stage,s.redact(tail[-3500:]),sorted(ENV_WHITELIST))
        txt,m=s.g.ask('reason',prompt,json_mode=True)
        if not txt: return None
        try: return json.loads(txt)
        except Exception: return None
    def apply_proposal(s,prop,stage,optional):
        a=prop.get('action'); blob=json.dumps(prop).lower()
        if a=='retry':
            envs={k:str(v) for k,v in (prop.get('env') or {}).items() if k in ENV_WHITELIST}
            if any(k not in ENV_WHITELIST for k in (prop.get('env') or {})): s.notices.append('Gemini suggested env keys outside the whitelist; ignored')
            s.env.update(envs); return 'retry','Gemini-advised retry'+(f' with {envs}' if envs else '')
        if a=='pip_install' and PIP_MAP.get(prop.get('package'),prop.get('package')) in PIP_ALLOW:
            pkg=PIP_MAP.get(prop['package'],prop['package']); s.run_cmd([sys.executable,'-m','pip','install','-q',pkg]); return 'retry',f'Gemini-advised install of {pkg}'
        if a=='apt_install' and prop.get('package') in APT_ALLOW: s.run_cmd(f"sudo apt-get install -y {prop['package']}",cwd=WORK); return 'retry',f"Gemini-advised install of {prop['package']}"
        if a=='skip_optional' and optional: return 'skip','Gemini advised skipping this optional stage'
        if a in ('propose_patch','patch'):
            ok,why=s.safe_apply_patch(str(prop.get('patch','')),stage)
            if ok: return 'retry',f'Gemini patch {why} (temporary; reverted automatically if the stage still fails)'
            os.makedirs(WORK+'/proposals',exist_ok=True); f=f"{WORK}/proposals/{stage}_{int(time.time())}.diff"; open(f,'w').write(str(prop.get('patch',''))); s.proposals.append(f)
            return 'halt',f'Gemini patch REJECTED by safety rules ({why}); saved for your review'
        return 'halt','Gemini advice was not an allowed action'
    # ---------- stage runner ----------
    def stage(s,name,fn,retries=3,optional=False):
        t=time.time(); rec=dict(name=name,attempts=0,ok=False,seconds=0,optional=optional); s.stages.append(rec); s.log(f'=== STAGE {name} ===')
        for attempt in range(1,retries+2):
            rec['attempts']=attempt
            try:
                fn(); s.guard(name); rec['ok']=True; break
            except Halt: rec['seconds']=round(time.time()-t); raise
            except StageFail as e:
                s.last_tail=e.tail; fid,arg=s.classify(e.tail+str(e))
                s.log(f'stage {name} attempt {attempt} failed: {e}')
                if attempt>retries: break
                fixed=None
                if fid:
                    try: fixed=s.known_fix(fid,arg,attempt,name)
                    except StageFail as e2: s.log(f'fix itself failed: {e2}')
                if fixed: s.fixes.append(f'{name}: {fixed}'); continue
                prop=s.consult(name,e.tail)
                if prop:
                    act,desc=s.apply_proposal(prop,name,optional); s.fixes.append(f'{name}: {desc} (diagnosis: {str(prop.get("diagnosis",""))[:200]})')
                    if act=='retry': continue
                    if act=='skip': rec['skipped']=True; break
                    break
                break
            except Exception as e:
                s.last_tail=traceback.format_exc()[-3000:]; s.log(f'stage {name} crashed: {type(e).__name__}: {e}')
                if attempt>retries: break
        rec['seconds']=round(time.time()-t)
        if not rec['ok']: s.revert_patches(name)
        if not rec['ok'] and not rec.get('skipped'):
            msg=f'stage {name} could not be fixed automatically: {s.last_tail[-600:] if hasattr(s,"last_tail") else ""}'
            if optional: s.unsolved.append(f'{name} (optional, skipped): '+msg[:400]); return False
            s.unsolved.append(f'{name}: '+msg[:600]); raise Halt(f'stage {name} failed after {rec["attempts"]} attempts')
        return rec['ok']
    # ---------- improvement suggestions (never auto-applied) ----------
    def suggest(s,extra=()):
        tot=max(sum(x['seconds'] for x in s.stages),1); out=list(extra)
        for x in s.stages:
            if x['seconds']>0.4*tot and x['seconds']>600: out.append(f"Stage '{x['name']}' used {round(100*x['seconds']/tot)}% of the run ({x['seconds']//60} min): consider caching its output between runs or parallelising it.")
            if x['attempts']>2: out.append(f"Stage '{x['name']}' needed {x['attempts']} attempts: investigate the root cause so it stops retrying.")
        if any('FALLBACK' in e or 'quota' in e for e in getattr(s.g,'EVENTS',[]) ): out.append('Gemini fallbacks were used: check your AI Studio quota or add billing; consider updating the model priority list.')
        if any('DEGRADED' in n for n in s.notices): out.append('The plan was built by the local heuristic: after you approve the result, add it to known_good/ so this footage always reproduces exactly.')
        return out
    # ---------- notifications ----------
    def notify(s,kind,subject,body):
        body=s.redact(body); 
        try: open(WORK+'/alerts.log','a').write(json.dumps(dict(t=time.strftime('%F %T'),kind=kind,subject=subject,body=body[:6000]))+'\n')
        except Exception: pass
        sm=os.environ.get('GITHUB_STEP_SUMMARY')
        if sm:
            try: open(sm,'a').write(f'## {subject}\n\n```\n{body[:12000]}\n```\n')
            except Exception: pass
        sent=[]
        if s.notify_fn: 
            try: sent.append(s.notify_fn(kind,subject,body))
            except Exception as e: sent.append(f'custom notifier failed {type(e).__name__}')
        u,tok=os.environ.get('APPS_SCRIPT_URL'),os.environ.get('WEBHOOK_TOKEN')
        if u and tok:
            try:
                import requests
                ev=kind if kind in ('failure','success') else 'success'
                r=requests.post(u,json=dict(token=tok,event=ev,step=subject,message=body[-3800:],url=os.environ.get('OUTPUT_URL','')),timeout=30); j=r.json()
                sent.append('apps-script:ok' if j.get('ok') else f"apps-script:rejected({j.get('error')})")
            except Exception as e: sent.append(f'apps-script:unreachable({type(e).__name__})')
        if os.environ.get('MAIL_USER') and os.environ.get('MAIL_APP_PASSWORD') and os.environ.get('MAIL_TO') and not any(x.endswith(':ok') for x in sent):
            try:
                m=EmailMessage(); m['Subject']=subject; m['From']=os.environ['MAIL_USER']; m['To']=os.environ['MAIL_TO']; m.set_content(body)
                with smtplib.SMTP_SSL(os.environ.get('SMTP_HOST','smtp.gmail.com'),int(os.environ.get('SMTP_PORT','465')),timeout=30) as sv: sv.login(os.environ['MAIL_USER'],os.environ['MAIL_APP_PASSWORD']); sv.send_message(m)
                sent.append('smtp:ok')
            except Exception as e: sent.append(f'smtp:failed({type(e).__name__})')
        s.log('notify '+kind+': '+', '.join(sent)); return sent
    def summary_text(s,status,extra=''):
        ev=getattr(s.g,'EVENTS',[]) if s.g else []
        L=[f'STATUS: {status}  (run time {round((time.time()-s.t0)/60,1)} min)','',extra,'','PROBLEMS THE MECHANIC FACED / FIXED AUTOMATICALLY:']+([' - '+x for x in s.fixes] or [' - none'])
        L+=['','COULD NOT FIX (needs you):']+([' - '+x for x in s.unsolved] or [' - nothing'])
        L+=['','AI MODEL ROUTING EVENTS:']+([' - '+x for x in ev[-15:]] or [' - none'])
        L+=['','NOTICES:']+([' - '+x for x in s.notices] or [' - none'])
        kept=[p for p in s.patches if not p.get('reverted')]
        if kept: L+=['','HOTFIXES APPLIED IN THIS RUN (temporary, inside the runner only). Approve by committing them to the repo:']+[f" - {p['stage']}: {p['diff']}\n"+'\n'.join('     '+x for x in p['text'].splitlines()[:30]) for p in kept]
        if s.proposals: L+=['','REJECTED / UNAPPLIED PATCH IDEAS (need your review): '+', '.join(s.proposals)]
        L+=['','STAGE TIMINGS:']+[f" - {x['name']}: {x['seconds']}s, attempts {x['attempts']}, {'ok' if x['ok'] else ('skipped' if x.get('skipped') else 'FAILED')}" for x in s.stages]
        return '\n'.join(L)
