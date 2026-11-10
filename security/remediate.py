#!/usr/bin/env python3
"""Auto-remediation (runs only from security-remediate.yml, started by the owner). For every current finding whose secret exists in the repo secrets:
python -> __import__("os").environ.get("NAME","") | yml -> ${{ secrets.NAME }} | md/txt -> <YOUR_NAME> placeholder. Checks python compiles, rescans, then the workflow commits.
env: SECRETS_JSON (toJSON(secrets)). Never prints values. Does not touch git history (rotating the key is the only fix for that)."""
import os, sys, json, subprocess, py_compile
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__))); import scan_secrets as S
try: SEC=json.loads(os.environ.get('SECRETS_JSON') or '{}')
except Exception: SEC={}
def have(n): return bool(n) and bool(SEC.get(n))
F=[f for f in S.scan_tree()]
if not F: print('nothing to fix: working tree is clean'); sys.exit(0)
by={}; skipped=[]
for f in F:
    if not have(f['secret_name']): skipped.append(f"{f['file']}:{f['line']} [{f['type']}] secret '{f['secret_name'] or '?'}' is not in the repository secrets yet"); continue
    by.setdefault(f['file'],[]).append(f)
changed=[]
for path,fs in by.items():
    full=os.path.join(S.ROOT,path); lines=open(full,encoding='utf-8').read().split('\n'); ext=os.path.splitext(path)[1].lower(); ok=True
    for f in sorted(fs,key=lambda x:(x['line'],-x['start_col'])):
        i=f['line']-1; ln=lines[i]; a,b=f['start_col'],f['end_col']
        if ext=='.py':
            if a>0 and b<len(ln) and ln[a-1] in '"\'' and ln[b]==ln[a-1]: a-=1; b+=1; rep=f'__import__("os").environ.get("{f["secret_name"]}","")'
            else: skipped.append(f"{path}:{f['line']} not a plain string literal: edit by hand"); ok=False; continue
        elif ext in ('.yml','.yaml'): rep='${{ secrets.%s }}'%f['secret_name']
        elif ext in ('.md','.txt'): rep='<YOUR_%s>'%f['secret_name']
        else: skipped.append(f"{path}:{f['line']} file type {ext or '?'} is not auto-fixable: edit by hand"); ok=False; continue
        lines[i]=ln[:a]+rep+ln[b:]
    if ok or any(l for l in lines):
        new='\n'.join(lines); open(full,'w',encoding='utf-8').write(new)
        if ext=='.py':
            try: py_compile.compile(full,doraise=True)
            except Exception as e:
                subprocess.run(['git','checkout','--',path],cwd=S.ROOT); skipped.append(f'{path}: the fix did not compile, reverted (edit by hand)'); continue
        changed.append(path)
print('changed:',changed or 'none')
for s in skipped: print('NOT FIXED:',s)
left=S.scan_tree()
print(f'{len(left)} finding(s) remain after the fix')
open(os.environ.get('GITHUB_OUTPUT','/dev/null'),'a').write(f"changed={'true' if changed else 'false'}\n")
sys.exit(0 if not left else 1)
