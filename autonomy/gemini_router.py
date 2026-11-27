#!/usr/bin/env python3
"""Gemini fallback router for the runner. Discovers REAL model ids from the API (names are never trusted),
ranks up to MAX_TIERS tiers (v5: 16 = 1 primary + 15 fallbacks) and fails over on quota/rate/server errors. Never raises; returns (None,None) if every tier fails.
Every failover is recorded in EVENTS so the Mechanic can put it in the alert email.
v5 order: USER_PRIORITY (v4 names in their old order, new free-tier names added), then any other discovered model newest first,
then the paid-only models (PAID_ONLY) last: a free key gets quota 0 there, so they are only worth a call when nothing else answers.
Gemma models get no JSON mode (Google refuses it for them): the JSON is read out of their text answer."""
import os, re, json, time, base64
import requests
BASE=os.environ.get('GEMINI_API_BASE','https://generativelanguage.googleapis.com/v1beta')
USER_PRIORITY=['gemini-3.8-flash','gemini-3.6-flash','gemini-3-flash-preview','gemini-4-hydrogen','gemini-3.5-flash-lite','gemini-2.5-flash','gemini-2.5-pro',
               'gemini-3.1-flash-lite','gemini-2.5-flash-lite','gemma-4-31b-it','gemma-4-26b-a4b-it','gemini-flash-latest','gemini-flash-lite-latest']
PAID_ONLY=re.compile(r'gemini-4-(carbon|argon|neon)|gemini-3\.1-pro|gemini-pro-latest|customtools',re.I)   # need a paid billing tier (owner's model list, Nov 2026)
MAX_TIERS=int(os.environ.get('GEMINI_MAX_TIERS','16'))
EXCLUDE=re.compile(r'embedding|tts|image|imagen|veo|aqa|robotics|learnlm|gemma-[123]\b|gemma-[123]-|live|transcribe|audio|computer-use|native|thinking-exp|decision|lyria|genie|omni',re.I)
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
    ok=[m for m in ids if (m.startswith('gemini-') or m.startswith('gemma-4')) and not EXCLUDE.search(m)]
    pri=[m for m in USER_PRIORITY if m in ok]; rest=[m for m in ok if m not in pri and not PAID_ONLY.search(m)]
    paid=[m for m in ok if m not in pri and PAID_ONLY.search(m)]
    def k(m):
        v=re.match(r'gemini-(\d+(?:\.\d+)?)',m); ver=float(v.group(1)) if v else 0.0
        tier=2 if 'flash-lite' in m else 0 if 'flash' in m else 1
        return (-ver,1 if re.search(r'preview|exp',m) else 0,tier,m)
    rest.sort(key=k); paid.sort(key=k); order=pri+rest
    if role=='reason': order=[m for m in order if '-pro' in m]+[m for m in order if '-pro' not in m]
    if role=='json': order=[m for m in order if 'lite' in m or 'flash' in m]+[m for m in order if not('lite' in m or 'flash' in m)]
    return (order+paid)[:MAX_TIERS]
class _Skip(Exception): pass
def _call(model,prompt,images,json_mode,timeout,extra_parts=()):
    parts=[{'text':prompt}]+list(extra_parts)
    for p in images or []: parts.append({'inline_data':{'mime_type':'image/jpeg','data':base64.b64encode(open(p,'rb').read()).decode()}})
    body={'contents':[{'parts':parts}]}
    if json_mode and not model.startswith('gemma-'): body['generationConfig']={'responseMimeType':'application/json'}
    r=requests.post(f'{BASE}/models/{model}:generateContent',headers={'x-goog-api-key':key()},json=body,timeout=timeout)
    if r.status_code==429:
        if re.search(r'limit:\s*0\b',r.text or ''):      # quota 0 = not in this key's tier (paid-only model): do not ask again this run
            _cool[model]=time.time()+6*3600; raise _Skip(f'{model}: not in your free tier (quota 0, 429)')
        try: ra=float(r.headers.get('Retry-After',60))
        except Exception: ra=60
        _cool[model]=time.time()+min(max(ra,20),900); raise _Skip(f'{model}: quota/rate limit (429)')
    if r.status_code in (400,404): _cool[model]=time.time()+3600; raise _Skip(f'{model}: HTTP {r.status_code}')
    if r.status_code in (401,403): _cool[model]=time.time()+3600; raise _Skip(f'{model}: auth/permission {r.status_code}')
    if r.status_code>=500: _cool[model]=time.time()+300; raise _Skip(f'{model}: server error {r.status_code}')
    j=r.json(); txt=j['candidates'][0]['content']['parts'][0]['text']
    if json_mode: txt=json_text(txt)
    return txt
def json_text(txt):
    """The JSON in an answer: whole text, or without code fences, or the first {...} / [...] block (Gemma answers in plain text). Raises if none."""
    t=txt.strip(); t=re.sub(r'^```(?:json)?|```$','',t,flags=re.M).strip()
    try: json.loads(t); return t
    except ValueError: pass
    for a,b in (('{','}'),('[',']')):
        i,j=t.find(a),t.rfind(b)
        if 0<=i<j:
            try: json.loads(t[i:j+1]); return t[i:j+1]
            except ValueError: pass
    json.loads(t); return t
def wait_hint(role='vision'):
    """Seconds until the first tier of this role leaves its cooldown (0 = one is ready now, None = no tiers at all)."""
    tiers=rank(discover(),role)
    if not tiers: return None
    now=time.time(); return max(0.0,min(_cool.get(m,0)-now for m in tiers))
def ask(role,prompt,images=(),json_mode=False,timeout=120,extra_parts=()):
    ids=discover()
    if not ids and key(): ids=discover(True)      # a failed model listing is cached as empty: look once more before giving up
    tiers=rank(ids,role)
    if not tiers: return None,None
    for i,m in enumerate(tiers):
        if _cool.get(m,0)>time.time(): continue
        try:
            txt=_call(m,prompt,images,json_mode,min(timeout,75) if i<len(tiers)-1 else timeout,extra_parts)
            if i>0: ev(f'FALLBACK: primary {tiers[0]} unavailable, answered by tier {i+1} ({m})')
            return txt,m
        except _Skip as e: ev(str(e)+' -> next tier')
        except Exception as e:
            if 'Timeout' in type(e).__name__ or 'Connection' in type(e).__name__: _cool[m]=time.time()+900      # circuit breaker: an overloaded model is skipped for 15 min instead of costing 2 min per request
            ev(f'{m}: {type(e).__name__} {str(e)[:80]} -> next tier')
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
    """Writes analysis/model_report.json and returns text for the alert email: which real models exist and the tier order per role."""
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
