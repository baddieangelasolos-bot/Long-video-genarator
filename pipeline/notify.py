import os, json, time, sys
def notify(event,step,message,url=''):
    """Always logs locally (alerts.log). Also POSTs to the Apps Script webhook if reachable. Never raises."""
    rec=dict(t=time.strftime('%Y-%m-%d %H:%M:%S'),event=event,step=step,message=message,url=url)
    try: open(__import__('os').path.join(__import__('os').path.dirname(__import__('os').path.abspath(__file__)),'alerts.log'),'a').write(json.dumps(rec)+'\n')
    except Exception: pass
    u,tok=os.environ.get('APPS_SCRIPT_URL'),os.environ.get('WEBHOOK_TOKEN')
    if not(u and tok): return 'logged-local'
    try:
        import requests; r=requests.post(u,json=dict(token=tok,event=event,step=step,message=message[-3000:],url=url),timeout=20)
        try: j=r.json()
        except Exception: return 'logged-local (webhook returned non-JSON; check deployment access)'
        return 'webhook:ok' if j.get('ok') else 'logged-local (webhook rejected: '+str(j.get('error'))+')'
    except Exception as e: return 'logged-local (webhook unreachable: '+type(e).__name__+')'
if __name__=='__main__': print(notify(*sys.argv[1:5]))
