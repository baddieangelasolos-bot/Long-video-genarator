#!/usr/bin/env python3
"""Zips the run's logs (secrets redacted) and emails them after EVERY run (failed or not). Never fails the job. stdlib only.
env: MAIL_USER MAIL_APP_PASSWORD MAIL_TO(optional) RUN_URL JOB_STATUS PIPELINE_OUTCOME
     EMAIL_LOGS_ALWAYS=0 sends only when the run had a problem (default: always) ; EMAIL_LOGS_DRYRUN=1 writes /tmp/email_logs.eml instead of sending
v4: the body starts with the PLANNING BRAIN block (which Gemini model planned the video, who failed before it), or with the BRAIN UNAVAILABLE report"""
import os, io, re, glob, json, zipfile, smtplib, ssl
from email.message import EmailMessage
W=os.environ.get('WORK_DIR','/home/claude/work')
SECRETS=[v for k in ('APPS_SCRIPT_URL','WEBHOOK_TOKEN','GEMINI_API_KEY','MAIL_APP_PASSWORD','GH_TOKEN','MAIL_TO','MAIL_USER') for v in [os.environ.get(k,'')] if len(v)>=8]
def red(b):
    t=b.decode('utf-8','replace')
    for v in SECRETS: t=t.replace(v,'***')
    return t.encode()
KEY=re.compile(r'WARNING|ERROR|Traceback|\[router\]|FALLBACK|unanswered|retry round|shot catalog:|duplicate scan:|plan report:|master plan:|master render:|title:|source video|HALT|CRASH|failed|timed out',re.I)
def key_lines(text,limit=5000):
    """The lines that explain a run (warnings, router failovers, labelling stats, plan report), newest last, so the mail body answers 'what happened' without opening the zip."""
    out=[]
    for l in text.splitlines():
        l=l.strip()
        if l and KEY.search(l) and (not out or out[-1]!=l[:300]): out.append(l[:300])
    t='\n'.join(out)
    return t if len(t)<=limit else '...(older lines cut)\n'+t[-limit:]
def _json(txt):
    try: return json.loads(txt)
    except Exception: return None
BRAIN_LBL={'ok':'valid plan','invalid':'answered, plan broke the rules','bad_json':'answer was not valid JSON','quota':'quota / rate limit','transient':'busy or timed out','skip':'not usable with this key','other':'error'}
def brain_block(rd,build_log=''):
    """(text, when). The model that planned the video and the models that failed before it; or the BRAIN UNAVAILABLE report. Older runs without the new fields still work."""
    bu=_json(rd('analysis/brain_unavailable.json'))
    if isinstance(bu,dict) and bu.get('text'): return bu['text'],str(bu.get('subject_when') or '')
    pl=((_json(rd('build/timeline.json')) or {}).get('_planner')) or {}
    name=str(pl.get('planner') or '')
    if not name or name=='none': return '',''
    L=['PLANNING BRAIN (video script and plan, master template)']
    if name.startswith('gemini:'):
        used=str(pl.get('model_used') or name.split(':',1)[1]); brain=pl.get('brain_models') or []; slot=pl.get('slot') or ((brain.index(used)+1) if used in brain else None)
        L.append(f"  Model that planned this video: {used}"+(f" (planning slot {slot} of {len(brain)}: {' > '.join(brain)})" if slot and brain else ''))
    elif name=='offline_safe_mode': L.append('  Planned by the OFFLINE planner, no Gemini model (low-quality fallback).')
    else: L.append('  Planner: '+name)
    att=[a for a in ((pl.get('model_chain_report') or {}).get('attempts') or []) if isinstance(a,dict)]
    if att: L.append('  Attempts in order: '+' -> '.join(f"{a.get('model','?')}: {BRAIN_LBL.get(a.get('result'),a.get('result'))}"+(f" (HTTP {a['http']})" if a.get('http') else '') for a in att))
    ns=(pl.get('model_chain_report') or {}).get('not_served')
    if ns: L.append('  Not served for this key (skipped): '+', '.join(ns))
    ids=sorted({str(d.get('model')) for d in ((_json(rd('analysis/identity.json')) or {}).get('detail') or []) if isinstance(d,dict) and d.get('model')})
    if ids: L.append('  Player identification: '+', '.join(ids))
    cats=re.findall(r'CATALOG (\{.*\})',build_log); cs=_json(cats[-1]) if cats else None
    if isinstance(cs,dict):
        L.append(f"  Shot labelling: {cs.get('labelled','?')} of {cs.get('rows','?')} windows labelled"+(f" ({cs.get('retry_rounds')} retry round(s))" if cs.get('retry_rounds') else ''))
        L+=['    '+str(e)[:160] for e in (cs.get('ai_events') or [])[:6]]
    return '\n'.join(L),''
def run_status(alerts):
    """'SUCCESS' / 'SUCCESS WITH WARNINGS' from the last success mail the pipeline logged, else ''."""
    for l in reversed(alerts.splitlines()):
        try: subj=json.loads(l).get('subject','')
        except Exception: continue
        mm=re.search(r'\] (SUCCESS(?: WITH WARNINGS)?)',subj)
        if mm: return mm.group(1)
    return ''
def main():
    job=os.environ.get('JOB_STATUS',''); outc=os.environ.get('PIPELINE_OUTCOME',''); exp_failed=os.path.exists(W+'/build/export_failed')
    problem=job!='success' or outc!='success' or exp_failed
    if not problem and os.environ.get('EMAIL_LOGS_ALWAYS','1')=='0': print('[email_logs] run was clean and EMAIL_LOGS_ALWAYS=0, nothing to send'); return 0
    user,pw=os.environ.get('MAIL_USER'),os.environ.get('MAIL_APP_PASSWORD'); to=os.environ.get('MAIL_TO') or user
    files=sorted(set(glob.glob(W+'/*.log')+glob.glob(W+'/build/*.log')+glob.glob(W+'/analysis/*.json')+glob.glob(W+'/proposals/*')+glob.glob(W+'/build/timeline.json')+glob.glob(W+'/build/*.report.json')+glob.glob(W+'/shots/*_rows.json')))
    buf=io.BytesIO(); n=0
    with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:
        for f in files:
            if os.path.isfile(f) and os.path.getsize(f)<50*2**20:
                d=open(f,'rb').read()[-2*2**20:]; z.writestr(os.path.relpath(f,W),red(d)); n+=1
    def rd(name):
        try: return red(open(f'{W}/{name}','rb').read()[-400000:]).decode()
        except Exception: return ''
    bl,al=rd('build.log'),rd('alerts.log'); status=run_status(al)
    try: brain,when=brain_block(rd,bl)
    except Exception as e: brain,when='',''; print('[email_logs] brain block skipped:',type(e).__name__)
    halted=os.path.exists(W+'/analysis/brain_unavailable.json') and bool(when or brain)
    if problem:
        tail=''.join(f'\n--- tail of {name} ---\n{txt[-6000:]}' for name,txt in (('build.log',bl),('alerts.log',al)) if txt)
        body=((brain+'\n\n') if brain else '')+(f"Pipeline problem detected.\njob status: {job or '?'}\npipeline step: {outc or 'did not run'}\ndrive export failed: {exp_failed}\n"
              f"run: {os.environ.get('RUN_URL','?')}\nlog files bundled: {n}\n"+(tail or '\n(no log files: the job failed before the pipeline started - open the run link, the failed step shows the error)\n'))
        subject='[Video pipeline] FAILED - logs attached'+(' (Drive export)' if exp_failed and outc=='success' else '')
        if halted: subject='[Video pipeline] BRAIN UNAVAILABLE - run again after '+(when or 'the time in this mail')+' - logs attached'
    else:
        kl=key_lines(bl)
        body=(f"Run finished: {status or 'SUCCESS'}.\n"+(f"\n{brain}\n\n" if brain else '')+f"run: {os.environ.get('RUN_URL','?')}\nlog files bundled: {n} (pipeline-logs.zip: build.log has every stage's full output, alerts.log every mail, analysis/ the catalog and plan)\n"
              +(f"\n--- key lines from build.log ---\n{kl}\n" if kl else ''))
        subject=f"[Video pipeline] Run logs attached ({status or 'SUCCESS'})"
    m=EmailMessage(); m['Subject']=subject; m['From']=user or to; m['To']=to; m.set_content(body)
    if n and len(buf.getvalue())<20*2**20: m.add_attachment(buf.getvalue(),maintype='application',subtype='zip',filename='pipeline-logs.zip')
    if os.environ.get('EMAIL_LOGS_DRYRUN')=='1': open('/tmp/email_logs.eml','wb').write(bytes(m)); print('[email_logs] dry run written'); return 0
    if not (user and pw): print('[email_logs] MAIL_USER / MAIL_APP_PASSWORD secrets missing: cannot email'); return 0
    try:
        with smtplib.SMTP_SSL(os.environ.get('SMTP_HOST','smtp.gmail.com'),int(os.environ.get('SMTP_PORT','465')),timeout=40,context=ssl.create_default_context()) as sv: sv.login(user,pw); sv.send_message(m)
        print('[email_logs] sent to',to)
    except Exception as e: print('[email_logs] send failed:',type(e).__name__)
    return 0
if __name__=='__main__':
    try: raise SystemExit(main())
    except SystemExit: raise
    except Exception as e: print('[email_logs] error',type(e).__name__); raise SystemExit(0)
