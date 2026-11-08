#!/usr/bin/env python3
"""Gemini fallback router for the runner. Discovers REAL model ids from the API (names are never trusted),
ranks up to 10 tiers (1 primary + 9 fallbacks) and fails over on quota/rate/server errors. Never raises; returns (None,None) if every tier fails.
Every failover is recorded in EVENTS so the Mechanic can put it in the alert email."""
import os, re, json, time, base64
import requests
BASE=os.environ.get('GEMINI_API_BASE','https://generativelanguage.googleapis.com/v1beta')
USER_PRIORITY=['gemini-3.8-flash','gemini-3.6-flash','gemini-3-flash-preview','gemini-3.5-flash-lite','gemini-2.5-flash','gemini-2.5-pro']
EXCLUDE=re.compile(r'embedding|tts|image|imagen|veo|aqa|robotics|learnlm|gemma|live|transcribe|audio|computer-use|native|thinking-exp',re.I)
EVENTS=[]; _cool={}; _cache={'ids':None}
def key(): return os.environ.get('GEMINI_API_KEY','').strip()
def ev(m): EVENTS.append(time.strftime('%H:%M:%S ')+m); print('[router]',m,flush=True)
def discover(force=False):
    if _cache['ids'] is not None and not force: return _cache['ids']
    ids=[]
    if not key(): ev('no GEMINI_API_KEY secret: AI tiers disabled'); _cache['ids']=[]; return []
    tok=None
    try:
        while True:
            r=requests.get(BASE+'/models',headers={'x-goog-api-key':key()},params={'pageSize':100,**({'pageToken':tok} if tok else {})},timeout=30)
            if r.status_code!=200: ev(f'model discovery failed HTTP {r.status_code}'); break
            j=r.json()
            for m in j.get('models',[]):
                if 'generateContent' in m.get('supportedGenerationMethods',[]): ids.append(m['name'].split('/')[-1])
            tok=j.get('nextPageToken')
            if not tok: break
    except Exception as e: ev(f'model discovery error {type(e).__name__}')
    _cache['ids']=ids; return ids
def rank(ids,role='vision'):
    ok=[m for m in ids if m.startswith('gemini-') and not EXCLUDE.search(m)]
    pri=[m for m in USER_PRIORITY if m in ok]; rest=[m for m in ok if m not in pri]
    def k(m):
        v=re.match(r'gemini-(\d+(?:\.\d+)?)',m); ver=float(v.group(1)) if v else 0.0
        tier=2 if 'flash-lite' in m else 0 if 'flash' in m else 1
        return (-ver,1 if re.search(r'preview|exp',m) else 0,tier,m)
    rest.sort(key=k); order=pri+rest
    if role=='reason': order=[m for m in order if '-pro' in m]+[m for m in order if '-pro' not in m]
    if role=='json': order=[m for m in order if 'lite' in m or 'flash' in m]+[m for m in order if not('lite' in m or 'flash' in m)]
    return order[:10]
class _Skip(Exception): pass
def _call(model,prompt,images,json_mode,timeout,extra_parts=()):
    parts=[{'text':prompt}]+list(extra_parts)
    for p in images or []: parts.append({'inline_data':{'mime_type':'image/jpeg','data':base64.b64encode(open(p,'rb').read()).decode()}})
    body={'contents':[{'parts':parts}]}
    if json_mode: body['generationConfig']={'responseMimeType':'application/json'}
    r=requests.post(f'{BASE}/models/{model}:generateContent',headers={'x-goog-api-key':key()},json=body,timeout=timeout)
    if r.status_code==429:
        try: ra=float(r.headers.get('Retry-After',60))
        except Exception: ra=60
        _cool[model]=time.time()+min(max(ra,20),900); raise _Skip(f'{model}: quota/rate limit (429)')
    if r.status_code in (400,404): _cool[model]=time.time()+3600; raise _Skip(f'{model}: HTTP {r.status_code}')
    if r.status_code in (401,403): _cool[model]=time.time()+3600; raise _Skip(f'{model}: auth/permission {r.status_code}')
    if r.status_code>=500: _cool[model]=time.time()+60; raise _Skip(f'{model}: server error {r.status_code}')
    j=r.json(); txt=j['candidates'][0]['content']['parts'][0]['text']
    if json_mode:
        t=txt.strip(); t=re.sub(r'^```(?:json)?|```$','',t,flags=re.M).strip(); json.loads(t); txt=t
    return txt
def ask(role,prompt,images=(),json_mode=False,timeout=120,extra_parts=()):
    ids=discover(); tiers=rank(ids,role)
    if not tiers: return None,None
    for i,m in enumerate(tiers):
        if _cool.get(m,0)>time.time(): continue
        try:
            txt=_call(m,prompt,images,json_mode,timeout,extra_parts)
            if i>0: ev(f'FALLBACK: primary {tiers[0]} unavailable, answered by tier {i+1} ({m})')
            return txt,m
        except _Skip as e: ev(str(e)+' -> next tier')
        except Exception as e: ev(f'{m}: {type(e).__name__} {str(e)[:80]} -> next tier')
    ev('ALL Gemini tiers failed for this request'); return None,None
# ---------- whole-video understanding (Gemini Files API) ----------
UPLOAD_BASE=os.environ.get('GEMINI_UPLOAD_BASE','https://generativelanguage.googleapis.com/upload/v1beta')
def upload_file(path,mime='video/mp4',timeout=900):
    """Resumable upload to the Files API, waits until the video is processed (ACTIVE). Returns the file resource dict."""
    size=os.path.getsize(path)
    h={'x-goog-api-key':key(),'X-Goog-Upload-Protocol':'resumable','X-Goog-Upload-Command':'start','X-Goog-Upload-Header-Content-Length':str(size),'X-Goog-Upload-Header-Content-Type':mime}
    r=requests.post(UPLOAD_BASE+'/files',headers=h,json={'file':{'display_name':os.path.basename(path)[:60]}},timeout=60)
    url=r.headers.get('X-Goog-Upload-URL')
    if not url: raise RuntimeError(f'file upload start refused HTTP {r.status_code}')
    with open(path,'rb') as f:
        r2=requests.post(url,headers={'x-goog-api-key':key(),'Content-Length':str(size),'X-Goog-Upload-Offset':'0','X-Goog-Upload-Command':'upload, finalize'},data=f,timeout=timeout)
    if r2.status_code!=200: raise RuntimeError(f'file upload failed HTTP {r2.status_code}')
    fj=r2.json()['file']
    for _ in range(int(os.environ.get('GEMINI_FILE_POLLS','72'))):
        st=requests.get(f"{BASE}/{fj['name']}",headers={'x-goog-api-key':key()},timeout=30).json()
        if st.get('state')=='ACTIVE': return st
        if st.get('state')=='FAILED': raise RuntimeError('Gemini could not process the video')
        time.sleep(float(os.environ.get('GEMINI_FILE_POLL_SLEEP','5')))
    raise RuntimeError('Gemini video processing timed out')
def delete_file(name):
    try: requests.delete(f'{BASE}/{name}',headers={'x-goog-api-key':key()},timeout=30)
    except Exception: pass
def ask_video(role,prompt,video_path,mime='video/mp4',json_mode=True,timeout=300):
    """Gemini WATCHES the whole video (frames + motion + audio). Returns (text, model) or (None, None). The uploaded copy is deleted afterwards."""
    if not discover(): return None,None
    try: fj=upload_file(video_path,mime)
    except Exception as e: ev(f'video upload failed: {type(e).__name__} {str(e)[:100]}'); return None,None
    try: return ask(role,prompt,json_mode=json_mode,timeout=timeout,extra_parts=[{'file_data':{'mime_type':fj.get('mimeType',mime),'file_uri':fj['uri']}}])
    finally: delete_file(fj['name'])
def report(roles=('vision','json','reason')):
    """Writes analysis/model_report.json and returns text for the alert email: which real models exist and the 10-tier order per role."""
    ids=discover(); rep={'discovered':len(ids),'tiers':{r:rank(ids,r) for r in roles}}
    primary=USER_PRIORITY[0]
    rep['primary_wanted']=primary; rep['primary_available']=primary in ids
    rep['unavailable_priority']=[m for m in USER_PRIORITY if m not in ids]
    try:
        os.makedirs('/home/claude/work/analysis',exist_ok=True); json.dump(rep,open('/home/claude/work/analysis/model_report.json','w'),indent=1)
    except Exception: pass
    lines=[f'Gemini models discovered: {len(ids)}']
    if not ids: lines.append('NO AI TIERS AVAILABLE (missing key, blocked, or quota): local fallback will be used and quality is degraded.')
    elif not rep['primary_available']: lines.append(f'Primary {primary} does not exist for this key; tier 1 for vision is {rep["tiers"]["vision"][0]}.')
    if rep['unavailable_priority']: lines.append('Requested but not found: '+', '.join(rep['unavailable_priority']))
    lines.append('Vision tiers: '+' > '.join(rep['tiers']['vision']))
    return '\n'.join(lines)
if __name__=='__main__': print(report())
