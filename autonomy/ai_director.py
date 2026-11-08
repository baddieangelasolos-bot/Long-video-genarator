#!/usr/bin/env python3
"""AI director: (1) identify  - who/which club is in the source videos (Gemini reads visible text cues only),
(2) style     - Gemini WATCHES the reference video every run and the measured numbers are merged into pipeline/style.json.
usage: python3 ai_director.py identify | style        env: WORK_DIR PIPE_DIR SRC_DIR USE_AI GEMINI_API_KEY
Without a reference video the default style (the original rules) is written."""
import os, sys, re, json, math, subprocess, tempfile, shutil
HERE=os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0,HERE)
WORK=os.environ.get('WORK_DIR','/home/claude/work'); PIPE=os.environ.get('PIPE_DIR',WORK+'/pipeline'); SRC=os.environ.get('SRC_DIR',WORK+'/src'); AN=WORK+'/analysis'
os.makedirs(AN,exist_ok=True)
def log(*a): print('[director]',*a,flush=True)
def router():
    if os.environ.get('USE_AI','1')!='1': return None
    try: import gemini_router as r; return r
    except Exception as e: log('router unavailable',type(e).__name__); return None
def sources():
    return sorted(f[:-4] for f in os.listdir(SRC) if re.fullmatch(r's\d\d\.mp4',f)) or [f[:-4] for f in ('gal.mp4','dor.mp4','rma.mp4') if os.path.exists(f'{SRC}/{f}')]
def names():
    try: return json.load(open(SRC+'/names.json'))
    except Exception: return {}
def dur_of(p):
    r=subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','csv=p=0',p],capture_output=True,text=True)
    try: return float(r.stdout.strip())
    except Exception: return 0.0
# ------------------------------------------------------------------ identify
ID_PROMPT='''This is a contact sheet of {n} frames from one sports video (file name: "{fname}").
Name a person ONLY if a visible text cue says who they are: a name printed on a shirt together with the team, an on-screen caption or graphic, a scoreboard line, or the file name itself.
NEVER guess a person from a face or body. If there is no such cue write "UNKNOWN". The club or national team may be read from the kit, crest or caption.
Answer ONLY JSON: {{"player":"FULL NAME or UNKNOWN","evidence":"what text cue you used","confidence":"high|medium|low","club":"club or national team, or UNKNOWN","content":"match|training|interview|mixed"}}'''
def contact_sheet(src,n=12):
    from PIL import Image
    p=f'{SRC}/{src}.mp4'; d=dur_of(p); tw,th=384,216; cols=4; rows=math.ceil(n/cols); im=Image.new('RGB',(cols*tw,rows*th)); tmp=tempfile.mkdtemp()
    for i in range(n):
        t=d*(0.06+0.88*i/max(n-1,1)); fp=f'{tmp}/{i}.jpg'
        subprocess.run(['ffmpeg','-v','error','-y','-ss',f'{t:.2f}','-i',p,'-frames:v','1','-vf',f'scale={tw}:{th}:force_original_aspect_ratio=decrease,pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2','-q:v','4',fp],timeout=90)
        if os.path.exists(fp): im.paste(Image.open(fp),((i%cols)*tw,(i//cols)*th))
    out=f'{AN}/id_{src}.jpg'; im.save(out,quality=85); shutil.rmtree(tmp,ignore_errors=True); return out
def clean_name(s):
    s=re.sub(r"[^A-Za-zÀ-ÿ' .-]",'',str(s or '')).strip(); s=re.sub(r'\s+',' ',s)[:40]
    return '' if not s or s.upper() in ('UNKNOWN','N/A','NONE','NULL') else s.upper()
STOP={'highlights','highlight','goal','goals','clip','clips','video','videos','vs','hd','final','edit','full','match','game','skills','best','compilation','mp4','mov','new','copy','part','the','and'}
def name_from_files(nm):
    c={}
    for k,v in nm.items():
        if not re.fullmatch(r's\d\d',k): continue
        w=[x for x in re.findall(r"[A-Za-z]{3,}",os.path.splitext(v)[0]) if x.lower() not in STOP]
        if 1<=len(w)<=3: c[' '.join(w).upper()]=c.get(' '.join(w).upper(),0)+1
    return max(c,key=c.get) if c else ''
def identify():
    srcs=sources(); nm=names(); r=router(); votes={}; clubs={}; detail=[]
    ov={}
    try: ov=json.load(open(HERE+'/overrides.json'))
    except Exception: pass
    if r:
        for s in srcs[:6]:
            try:
                img=contact_sheet(s); txt,m=r.ask('vision',ID_PROMPT.format(n=12,fname=nm.get(s,s)),images=[img],json_mode=True)
                if not txt: continue
                j=json.loads(txt); pl=clean_name(j.get('player')); cl=clean_name(j.get('club')); w={'high':3,'medium':2,'low':1}.get(str(j.get('confidence','low')).lower(),1)
                detail.append(dict(src=s,player=pl,club=cl,confidence=j.get('confidence'),evidence=str(j.get('evidence',''))[:120],model=m))
                if pl: votes[pl]=votes.get(pl,0)+w
                if cl: clubs[cl]=clubs.get(cl,0)+w
            except Exception as e: log(f'identify {s}: {type(e).__name__}')
    player=max(votes,key=votes.get) if votes else ''; club=max(clubs,key=clubs.get) if clubs else ''; how='gemini' if player else ''
    if not player:
        f=name_from_files(nm)
        if f: player,how=f,'file names'
    if not player:
        env=clean_name(os.environ.get('PLAYER_NAME',''))     # the workflow default is a fixed name: only trust it if the file names mention that person
        low=' '.join(nm.values()).lower()
        if env and any(t.lower() in low for t in env.split() if len(t)>=4): player,how=env,'PLAYER_NAME (matches file names)'
    if clean_name(ov.get('player')): player,how=clean_name(ov['player']),'overrides.json'
    if clean_name(ov.get('club')): club=clean_name(ov['club'])
    if not player: player,how='HIGHLIGHTS','no name could be read (neutral title)'
    res=dict(player=player,club=club,how=how,detail=detail)
    json.dump(res,open(AN+'/identity.json','w'),indent=1); log(f'title: {player} | club: {club or "-"} | from {how}'); return res
# ------------------------------------------------------------------ style
DEFAULT_STYLE=dict(source='default',min_clip_s=3.0,pref_scale=1.0,bars_pct=5.09,transitions=dict(flash_white=1/3,rgb_split=1/3,hard_cut=1/3),punch_all=True,zoom_push=0.05,punch=0.09,sharpen=0.4,slow='some',tint='none',grade_target=None,wide_share_max=0.25,describe='default editing style and rules')
KNOWN_T=('hard_cut','flash_white','flash_black','rgb_split','zoom_punch','fade_black','invert_flash','blur_pulse')
ST_PROMPT='''You are a film editor. Watch this reference video completely (picture AND sound). Describe its EDITING STYLE so another editor can copy it with ffmpeg.
Use only these words. Answer ONLY JSON:
{"color_grade":"neutral|warm|cool|teal_orange|desaturated|vivid|high_contrast|black_white",
 "sharpness":"soft|normal|crisp","vignette":"none|light|strong","letterbox_percent":0,
 "transitions":[{"type":"hard_cut|flash_white|flash_black|rgb_split|zoom_punch|fade_black|invert_flash|blur_pulse","share":0.0}],
 "zoom_push_in":true,"slow_motion":"none|some|heavy","speed_ramps":false,"shake":false,
 "overlays":["text_title","end_card","logo","light_leaks","film_grain","none"],
 "pacing":"slow_cinematic|medium|fast_cuts|beat_synced","intro":"one sentence","outro":"one sentence","summary":"two sentences on the overall look and how the cuts and effects are used"}
Measured facts to stay consistent with: {facts}'''
def merge_style(profile,g):
    st=json.loads(json.dumps(DEFAULT_STYLE)); st['source']='reference'; g=g or {}
    body=(profile.get('shot_len_s') or {}).get('body') or {}
    if body.get('median'): st['pref_scale']=round(min(2.0,max(0.4,body['median']/4.0)),2)
    if body.get('p10'): st['min_clip_s']=round(min(3.0,max(1.2,body['p10']*0.9)),1)
    lb=profile.get('letterbox_pct'); gl=g.get('letterbox_percent')
    if isinstance(lb,(int,float)): st['bars_pct']=round(min(12.0,max(0.0,lb)),2)
    elif isinstance(gl,(int,float)): st['bars_pct']=round(min(12.0,max(0.0,float(gl))),2)
    tr={}
    for t in g.get('transitions') or []:
        k=str(t.get('type','')); 
        if k in KNOWN_T and k!='hard_cut':
            try: tr[k]=tr.get(k,0)+max(0.0,float(t.get('share',0)))
            except Exception: pass
    fl=(profile.get('flash_events') or {}).get('share_of_cuts_with_flash')
    effect_total=min(0.9,max(0.1,float(fl))) if isinstance(fl,(int,float)) else (0.5 if tr else None)
    if g.get('transitions') is not None and not tr and not fl: effect_total=0.1     # reference looks like plain cuts
    if effect_total is not None:
        if not tr: tr={'flash_white':1.0,'rgb_split':1.0}
        s=sum(tr.values()) or 1.0; w={k:round(effect_total*v/s,4) for k,v in tr.items()}; w['hard_cut']=round(1-sum(w.values()),4); st['transitions']=w; st['punch_all']=False
    st['zoom_push']=0.05 if g.get('zoom_push_in',True) else 0.0
    st['sharpen']={'soft':0.0,'normal':0.35,'crisp':0.6}.get(g.get('sharpness'),0.4)
    st['slow']=g.get('slow_motion') if g.get('slow_motion') in ('none','some','heavy') else 'some'
    cg=g.get('color_grade'); st['tint']=cg if cg in ('warm','cool','teal_orange','black_white') else 'none'
    gr=profile.get('grade') or {}; ex=profile.get('grade_extra') or {}
    if all(k in gr for k in ('mean_saturation','luma_mean','luma_std','vignette_corner_over_center')):
        st['grade_target']=dict(sat=gr['mean_saturation'],luma=gr['luma_mean'],std=gr['luma_std'],p5=ex.get('p5',13.0),p95=ex.get('p95',196.7),vig=gr['vignette_corner_over_center'])
    st['describe']=str(g.get('summary') or ('measured only (Gemini unavailable)'))[:400]; st['gemini']={k:g.get(k) for k in ('color_grade','pacing','intro','outro','overlays','speed_ramps','shake')} if g else None
    return st
def style():
    ref=SRC+'/ref.mp4'; out=PIPE+'/style.json'
    if not os.path.exists(ref):
        json.dump(DEFAULT_STYLE,open(out,'w'),indent=1); log('no reference video: default style and rules'); return DEFAULT_STYLE
    prof={}
    try: prof=json.load(open(PIPE+'/style_profile.json'))
    except Exception: log('no measured profile (analysis step failed)')
    g=None; r=router()
    if r:
        facts=json.dumps({k:prof.get(k) for k in ('duration','fps','resolution','cut_count','shot_len_s','flash_events','slowmo_dup_frame_share_body','grade','letterbox_pct')},default=str)[:1800]
        path=ref; tmp=None
        if os.path.getsize(ref)>90*2**20:
            tmp=tempfile.mktemp(suffix='.mp4'); subprocess.run(['ffmpeg','-v','error','-y','-i',ref,'-vf','scale=-2:540','-c:v','libx264','-crf','28','-preset','veryfast','-c:a','aac','-b:a','96k',tmp],timeout=900); path=tmp
        try:
            txt,m=r.ask_video('vision',ST_PROMPT.replace('{facts}',facts),path,json_mode=True,timeout=400)
            if txt: g=json.loads(txt); log('Gemini watched the reference video ('+str(m)+')'); json.dump(g,open(AN+'/style_ai.json','w'),indent=1)
            else: log('Gemini could not analyse the reference video: measured numbers only')
        except Exception as e: log('Gemini style analysis failed:',type(e).__name__)
        finally:
            if tmp and os.path.exists(tmp): os.remove(tmp)
    else: log('AI disabled/unavailable: measured numbers only')
    st=merge_style(prof,g); json.dump(st,open(out,'w'),indent=1); log('style:',json.dumps({k:st[k] for k in ('source','min_clip_s','pref_scale','bars_pct','transitions','tint','slow','sharpen')})); return st
if __name__=='__main__':
    cmd=sys.argv[1] if len(sys.argv)>1 else ''
    if cmd=='identify': identify()
    elif cmd=='style': style()
    else: sys.exit('usage: ai_director.py identify|style')
