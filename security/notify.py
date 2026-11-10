#!/usr/bin/env python3
"""Red-alert email. Reads findings.json (masked previews only; a secret value is never put in the mail). stdlib only.
env: MAIL_USER MAIL_APP_PASSWORD MAIL_TO  GITHUB_REPOSITORY GITHUB_SERVER_URL RUN_URL"""
import os, sys, json, smtplib, ssl, html
from email.message import EmailMessage
f=sys.argv[1] if len(sys.argv)>1 else 'findings.json'
try: F=json.load(open(f))
except Exception: F=[]
if not F: print('[notify] nothing to report'); sys.exit(0)
user,pw,to=os.environ.get('MAIL_USER'),os.environ.get('MAIL_APP_PASSWORD'),os.environ.get('MAIL_TO')
if not (user and pw and to): print('[notify] MAIL_USER / MAIL_APP_PASSWORD / MAIL_TO secrets not available here (e.g. a fork pull request): no mail sent'); sys.exit(0)
repo=os.environ.get('GITHUB_REPOSITORY',''); srv=os.environ.get('GITHUB_SERVER_URL','https://github.com'); run=os.environ.get('RUN_URL','')
done=f'{srv}/{repo}/actions/workflows/security-remediate.yml'; secrets_page=f'{srv}/{repo}/settings/secrets/actions'
rows=''.join(f"<tr><td>{html.escape(x['type'])}</td><td>{html.escape(x['file'])}:{x['line']}</td><td>{html.escape(x['preview'])}</td><td>{html.escape(x['where'])}</td><td><b>{html.escape(x['secret_name'] or 'pick a name')}</b></td></tr>" for x in F)
hist=any('history' in x['where'] for x in F)
txt='\n'.join(f"- {x['type']} in {x['file']}:{x['line']} ({x['preview']}) [{x['where']}] -> secret: {x['secret_name'] or 'pick a name'}" for x in F)
body=f"""<h2 style="color:#b00020">RED ALERT: exposed information in {html.escape(repo)}</h2>
<p>The security guard found {len(F)} item(s). Values are masked here on purpose.</p>
<table border="1" cellpadding="6" cellspacing="0"><tr><th>Type</th><th>Where</th><th>Preview</th><th>Found in</th><th>Secret to create</th></tr>{rows}</table>
<h3>What to do (about 3 minutes)</h3>
<ol><li><b>Treat any exposed key as stolen. Create a NEW key</b> (new Gemini key, new Gmail app password, new Apps Script token) and revoke the old one. {'Some items are only in git history: removing them from the code does NOT remove them from history, so revoking the old key is the only real fix.' if hist else ''}</li>
<li>Open <a href="{secrets_page}">Settings, Secrets and variables, Actions</a> and add the NEW value under the exact secret name in the table (New repository secret).</li>
<li>Tap the button below, then tap the green "Run workflow" button on that page. The guard will remove the exposed value from the code, switch the code to the secret, check it compiles, and push the clean version.</li></ol>
<p><a href="{done}" style="background:#0b8043;color:#fff;padding:14px 26px;border-radius:8px;text-decoration:none;font-weight:bold;font-size:16px">DONE - I added the secret(s)</a></p>
<p style="color:#555">Why a button that opens GitHub instead of a hidden web link: GitHub itself checks that it is really you (login, 2FA). A hidden link that pushes code could be abused by anyone who found it.</p>
<p>Run: <a href="{run}">{run}</a></p>"""
m=EmailMessage(); m['Subject']=f'[SECURITY RED ALERT] {len(F)} exposed item(s) in {repo}'; m['From']=user; m['To']=to
m.set_content('RED ALERT: exposed information found in '+repo+'\n'+txt+f'\n\n1) Create NEW keys and revoke old ones.\n2) Add them at {secrets_page}\n3) Open {done} and tap Run workflow.\nRun: {run}\n'); m.add_alternative(body,subtype='html')
try:
    with smtplib.SMTP_SSL(os.environ.get('SMTP_HOST','smtp.gmail.com'),int(os.environ.get('SMTP_PORT','465')),timeout=40,context=ssl.create_default_context()) as sv: sv.login(user,pw); sv.send_message(m)
    print('[notify] alert mail sent')
except Exception as e: print('[notify] mail failed:',type(e).__name__)
