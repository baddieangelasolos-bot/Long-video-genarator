#!/usr/bin/env python3
"""Zips the run's logs (secrets redacted) and emails them. Never fails the job. stdlib only.
env: MAIL_USER MAIL_APP_PASSWORD MAIL_TO(optional) RUN_URL JOB_STATUS PIPELINE_OUTCOME  ; EMAIL_LOGS_DRYRUN=1 writes /tmp/email_logs.eml instead of sending"""
import os, io, glob, zipfile, smtplib, ssl
from email.message import EmailMessage
W=os.environ.get('WORK_DIR','/home/claude/work')
SECRETS=[v for k in ('APPS_SCRIPT_URL','WEBHOOK_TOKEN','GEMINI_API_KEY','MAIL_APP_PASSWORD','GH_TOKEN','MAIL_TO','MAIL_USER') for v in [os.environ.get(k,'')] if len(v)>=8]
def red(b):
    t=b.decode('utf-8','replace')
    for v in SECRETS: t=t.replace(v,'***')
    return t.encode()
def main():
    job=os.environ.get('JOB_STATUS',''); outc=os.environ.get('PIPELINE_OUTCOME',''); exp_failed=os.path.exists(W+'/build/export_failed')
    problem=job!='success' or outc!='success' or exp_failed
    if not problem and os.environ.get('EMAIL_LOGS_ALWAYS')!='1': print('[email_logs] run was clean, nothing to send'); return 0
    user,pw=os.environ.get('MAIL_USER'),os.environ.get('MAIL_APP_PASSWORD'); to=os.environ.get('MAIL_TO') or user
    files=sorted(set(glob.glob(W+'/*.log')+glob.glob(W+'/build/*.log')+glob.glob(W+'/analysis/*.json')+glob.glob(W+'/proposals/*')))
    buf=io.BytesIO(); n=0
    with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:
        for f in files:
            if os.path.isfile(f) and os.path.getsize(f)<50*2**20:
                d=open(f,'rb').read()[-2*2**20:]; z.writestr(os.path.relpath(f,W),red(d)); n+=1
    tail=''
    for name in ('build.log','alerts.log'):
        try: tail+=f'\n--- tail of {name} ---\n'+red(open(f'{W}/{name}','rb').read()[-6000:]).decode()
        except Exception: pass
    body=(f"Pipeline problem detected.\njob status: {job or '?'}\npipeline step: {outc or 'did not run'}\ndrive export failed: {exp_failed}\n"
          f"run: {os.environ.get('RUN_URL','?')}\nlog files bundled: {n}\n"+(tail or '\n(no log files: the job failed before the pipeline started - open the run link, the failed step shows the error)\n'))
    m=EmailMessage(); m['Subject']='[Video pipeline] FAILED - logs attached'+(' (Drive export)' if exp_failed and outc=='success' else ''); m['From']=user or to; m['To']=to; m.set_content(body)
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
