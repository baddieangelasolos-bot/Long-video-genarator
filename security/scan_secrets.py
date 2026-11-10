#!/usr/bin/env python3
"""Secret / personal-data scanner (stdlib only). Never prints a full secret: only file, line, type and a masked preview.
usage: scan_secrets.py [--history] [--json out.json]   exit 0 = clean, 1 = findings
Allowlist: security/allowlist.txt (one fnmatch path pattern per line)."""
import os, re, sys, json, subprocess, fnmatch
ROOT=subprocess.run(['git','rev-parse','--show-toplevel'],capture_output=True,text=True).stdout.strip() or os.getcwd()
FAKE_EMAIL_DOMAINS={'example.com','example.org','x.com','localhost','users.noreply.github.com','noreply.github.com'}
PATTERNS=[  # (type, regex, secret name to create, group index of the value)
 ('google_api_key',r'AIza[0-9A-Za-z_\-]{30,}','GEMINI_API_KEY',0),
 ('github_token',r'(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}','GH_PAT',0),
 ('openai_style_key',r'\bsk-[A-Za-z0-9_\-]{30,}','API_KEY',0),
 ('aws_key',r'\bAKIA[0-9A-Z]{16}\b','AWS_ACCESS_KEY_ID',0),
 ('slack_token',r'\bxox[abprs]-[A-Za-z0-9-]{10,}','SLACK_TOKEN',0),
 ('private_key',r'-----BEGIN [A-Z ]*PRIVATE KEY-----','PRIVATE_KEY',0),
 ('telegram_bot_token',r'\b\d{8,10}:[A-Za-z0-9_\-]{35}\b','TELEGRAM_BOT_TOKEN',0),
 ('apps_script_url',r'https://script\.google\.com/macros/s/(AKfy[A-Za-z0-9_\-]{20,})/exec','APPS_SCRIPT_URL',0),
 ('webhook_url',r'https://(?:hooks\.slack\.com/services|discord(?:app)?\.com/api/webhooks|hook\.[a-z0-9.-]*make\.com)/[A-Za-z0-9/_\-]{16,}','MAKE_WEBHOOK_URL',0),
 ('chat_id',r'(?i)chat_?id["\']?\s*[:=]\s*["\']?(-?\d{6,})','CHAT_ID',1),
 ('generic_secret',r'''(?i)\b([A-Za-z_]*(?:password|passwd|secret|token|api_?key)[A-Za-z_]*)\s*[:=]\s*["']([A-Za-z0-9/+_\-=.]{16,})["']''','',2),
 ('drive_id',r'''["'](1[A-Za-z0-9_\-]{32})["']''','DRIVE_FILES_JSON',1),
 ('email',r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}','MAIL_TO',0),
]
SKIP_DIRS={'.git','__pycache__','node_modules'}
def allow():
    p=os.path.join(ROOT,'security','allowlist.txt'); pats=[]
    if os.path.exists(p): pats=[l.strip() for l in open(p) if l.strip() and not l.startswith('#')]
    return pats
def mask(v): v=v.strip('"\''); return (v[:3]+'***'+f' ({len(v)} chars)') if len(v)>6 else '***'
def scan_text(text,path,commit=None):
    out=[]
    for ln,line in enumerate(text.splitlines(),1):
        if len(line)>4000: continue
        for typ,rx,name,g in PATTERNS:
            for m in re.finditer(rx,line):
                val=m.group(g) if g else m.group(0)
                if typ=='email':
                    dom=val.split('@')[1].lower()
                    if dom in FAKE_EMAIL_DOMAINS or dom.endswith('.noreply.github.com') or val.lower().startswith('me@'): continue
                if typ=='generic_secret':
                    if re.search(r'environ|getenv|secrets\.|\$\{|process\.env|<|YOUR_',line[m.start():m.end()+20]) or len(set(val))<6: continue
                    name=re.sub(r'[^A-Z0-9]+','_',m.group(1).upper()).strip('_')
                if typ=='apps_script_url' and '<' in val: continue
                s=m.start(g) if g else m.start(); e=m.end(g) if g else m.end()
                out.append(dict(file=path,line=ln,type=typ,secret_name=name,preview=mask(val),start_col=s,end_col=e,commit=commit))
    return out
def tracked():
    r=subprocess.run(['git','ls-files'],capture_output=True,text=True,cwd=ROOT); files=r.stdout.split('\n') if r.returncode==0 else []
    if not any(files):
        files=[os.path.relpath(os.path.join(d,f),ROOT) for d,ds,fs in os.walk(ROOT) if not (set(d.split(os.sep))&SKIP_DIRS) for f in fs]
    return [f for f in files if f]
def scan_tree():
    al=allow(); res=[]
    for f in tracked():
        if any(fnmatch.fnmatch(f,p) for p in al) or f.startswith('tests/'): continue
        p=os.path.join(ROOT,f)
        try:
            if os.path.getsize(p)>1_500_000: continue
            raw=open(p,'rb').read()
        except Exception: continue
        if b'\0' in raw[:2000]: continue
        res+=scan_text(raw.decode('utf-8','replace'),f)
    return res
def scan_history():
    r=subprocess.run(['git','log','--all','-p','--no-color','-U0','--format=@@COMMIT %h'],capture_output=True,text=True,cwd=ROOT,errors='replace'); res=[]; commit=''; path=''; al=allow()
    for line in r.stdout.splitlines():
        if line.startswith('@@COMMIT '): commit=line[9:]
        elif line.startswith('+++ b/'): path=line[6:]
        elif line.startswith('+') and not line.startswith('+++'):
            if path.startswith('tests/') or any(fnmatch.fnmatch(path,p) for p in al): continue
            for f in scan_text(line[1:],path,commit): f['line']=0; res.append(f)
    seen=set(); out=[]
    for f in res:
        k=(f['file'],f['type'],f['preview'],f['commit'])
        if k not in seen: seen.add(k); out.append(f)
    return out
if __name__=='__main__':
    hist='--history' in sys.argv; outp=sys.argv[sys.argv.index('--json')+1] if '--json' in sys.argv else None
    cur=scan_tree(); old=[]
    if hist:
        cur_keys={(f['file'],f['type'],f['preview']) for f in cur}
        old=[f for f in scan_history() if (f['file'],f['type'],f['preview']) not in cur_keys]
    for f in cur: f['where']='current files'
    for f in old: f['where']='git history only (commit %s)'%f['commit']
    allf=cur+old
    if outp: json.dump(allf,open(outp,'w'),indent=1)
    for f in allf: print(f"[{f['type']}] {f['file']}:{f['line']} {f['preview']}  <- {f['where']}  (secret name: {f['secret_name'] or 'choose one'})")
    print(f'{len(allf)} finding(s)' if allf else 'CLEAN: no exposed secrets or personal data found')
    sys.exit(1 if cur else (2 if old else 0))   # 1 = exposed in current files (blocks), 2 = only in old commits (warning mail, cannot be removed from history: revoke the key)
