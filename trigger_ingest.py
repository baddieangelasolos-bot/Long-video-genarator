#!/usr/bin/env python3
"""Trigger the ingest workflow remotely and wait for it to finish.
  env GITHUB_PAT  fine-grained token. workflow_dispatch needs  Actions: Read and write.  (repository_dispatch needs Contents: Read and write - avoid.)
  python3 trigger_ingest.py OWNER/REPO [--tag v1.0.0] [--files files.json] [--keep-old] [--event repository_dispatch] [--no-wait]
  files.json = [["name.mp4","<driveFileId>",<bytes>], ...]   (omit = built-in 5 files)"""
import os, sys, json, time, argparse, datetime
import requests
API=os.environ.get('GITHUB_API','https://api.github.com'); WF='ingest_drive.yml'
def H(): 
    p=os.environ.get('GITHUB_PAT','').strip()
    if not p: sys.exit('ERROR: GITHUB_PAT not set')
    return {'Authorization':'Bearer '+p,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28','User-Agent':'video-pipeline-trigger'}
def explain(r,what):
    if r.status_code in (200,204): return
    hint={401:'token rejected/expired',403:'token lacks permission: needs Actions: Read and write (workflow_dispatch) or Contents: Read and write (repository_dispatch)',
          404:'repo/workflow not found or not visible to this token; the workflow file must be on the DEFAULT branch',
          422:'workflow has no workflow_dispatch trigger on that ref, or inputs invalid'}.get(r.status_code,'')
    sys.exit(f'ERROR {what}: HTTP {r.status_code} {hint} | {r.text[:200]}')
def main():
    ap=argparse.ArgumentParser(); ap.add_argument('repo'); ap.add_argument('--tag',default='v1.0.0'); ap.add_argument('--files'); ap.add_argument('--keep-old',action='store_true')
    ap.add_argument('--event',default='workflow_dispatch',choices=['workflow_dispatch','repository_dispatch']); ap.add_argument('--no-wait',action='store_true'); ap.add_argument('--status',action='store_true'); ap.add_argument('--timeout',type=int,default=2400); a=ap.parse_args()
    if a.status:      # read-only: show the latest run, dispatch nothing
        rr=requests.get(f'{API}/repos/{a.repo}/actions/workflows/{WF}/runs',headers=H(),params={'per_page':1},timeout=30); explain(rr,'list runs')
        runs=rr.json().get('workflow_runs',[])
        if not runs: print('no runs yet'); return 0
        x=runs[0]; print('latest run',x['id'],'| status:',x['status'],'| conclusion:',x.get('conclusion'),'| started:',x['created_at'],'|',x.get('html_url','')); return 0 if x.get('conclusion') in (None,'success') else 1
    files=json.load(open(a.files)) if a.files else None; purge='false' if a.keep_old else 'true'
    try:
        r=requests.get(f'{API}/repos/{a.repo}',headers=H(),timeout=30); explain(r,'repo lookup'); branch=r.json().get('default_branch','main')
        t0=datetime.datetime.now(datetime.timezone.utc)
        if a.event=='workflow_dispatch':
            body={'ref':branch,'inputs':{'tag':a.tag,'purge_all':purge,**({'files_json':json.dumps(files)} if files else {})}}
            r=requests.post(f'{API}/repos/{a.repo}/actions/workflows/{WF}/dispatches',headers=H(),json=body,timeout=30)
        else:
            body={'event_type':'ingest-media','client_payload':{'tag':a.tag,'purge_all':purge,**({'files':files} if files else {})}}
            r=requests.post(f'{API}/repos/{a.repo}/dispatches',headers=H(),json=body,timeout=30)
        explain(r,'dispatch'); print('dispatched',a.event,'on',branch)
        if a.no_wait: return 0
        end=time.time()+a.timeout; run=None
        while time.time()<end:
            time.sleep(float(os.environ.get('POLL_SECONDS','15')))
            rr=requests.get(f'{API}/repos/{a.repo}/actions/workflows/{WF}/runs',headers=H(),params={'event':a.event,'per_page':5},timeout=30); explain(rr,'list runs')
            for x in rr.json().get('workflow_runs',[]):
                if datetime.datetime.fromisoformat(x['created_at'].replace('Z','+00:00'))>=t0-datetime.timedelta(seconds=10): run=x; break
            if run and run['status']=='completed': print('run',run['id'],'conclusion:',run['conclusion'],run.get('html_url','')); return 0 if run['conclusion']=='success' else 1
            print('waiting...',run['status'] if run else 'run not started yet',flush=True)
        print('TIMEOUT waiting for run'); return 3
    except requests.exceptions.ConnectionError as e: sys.exit('ERROR network blocked/unreachable: '+str(e)[:150])
if __name__=='__main__': sys.exit(main())
