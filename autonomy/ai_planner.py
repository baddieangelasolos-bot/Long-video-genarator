#!/usr/bin/env python3
"""Clip planner: replaces Claude's visual selection. Steps: rows (shots.py ROWS_ONLY) -> classify (Gemini vision via gemini_router, else local heuristic) -> select clips by fixed rules -> edl_input.json.
python3 ai_planner.py PLAYER   (reads beat.json, rows json, /tmp/fr_<src> frames made by shots2.py; writes pipeline/edl_input.json and plan_notes.json)"""
import json, os, sys, re, math, glob, time
import numpy as np
WORK=os.environ.get('WORK_DIR','/home/claude/work'); PIPE=os.environ.get('PIPE_DIR',WORK+'/pipeline'); SHOTS=WORK+'/shots'
REJECT_FLAGS={'blur','referee_bench','foul','player_lost_ball','net_mesh','internal_cut'}
INTRO_ORDER=['bus','tunnel','aerial','locker','crest','trophy','warmup']
PREF={'close':12,'medium':8,'wide':9,'face':8}
CLASS_MAP={'close_dribble':'close','close_skill':'close','medium_action':'medium','wide_goal':'wide','face':'face','celebration':'celebration','medal':'celebration','pre_match':'intro'}
def load_rows(srcs):
    rows=[]
    for s in srcs: rows+=json.load(open(f'{SHOTS}/{s}_rows.json'))
    return rows
def frame_stats(src,row):
    """green fraction of the mid frame (from shots2 frames at 2 fps)."""
    t=(row['s']+row['e'])/2; fp=f'/tmp/fr_{src}/{int(round(t*2))+1:05d}.jpg'
    try:
        from PIL import Image
        a=np.asarray(Image.open(fp).convert('RGB').resize((64,36))).astype(float); r,g,b=a[...,0],a[...,1],a[...,2]
        return float(((g>r+8)&(g>b+8)&(g>60)).mean())
    except Exception: return None
def classify_local(rows,durs):
    """Degraded fallback: no vision. Position in the source + green fraction + motion. Flagged in plan_notes."""
    out={}; intro_i=0
    for r in rows:
        d=durs[r['src']]; pos=r['s']/d; g=frame_stats(r['src'],r); g=0.6 if g is None else g
        c='wide_other'; kind='none'
        if pos<0.12 and g<0.55 and intro_i<len(INTRO_ORDER): c='pre_match'; kind=INTRO_ORDER[intro_i]; intro_i+=1
        elif pos>0.93: c='celebration'
        elif g<0.45: c='face'
        elif g<0.72 and r['motion']>=3: c='medium_action'
        elif g>=0.72 and r['motion']<2.6: c='wide_goal'
        else: c='close_dribble' if r['motion']>=3 else 'wide_other'
        out[r['id']]=dict(cls=c,kind=kind,quality=3,flags=[])
    return out
PROMPT='''You review a contact sheet from a football highlights video of the player {player}. Each ROW is one shot: a label cell on the left and 4 frames spread over the shot.
Rows from top to bottom have these ids: {ids}.
For EVERY row return an object. Classes: close_dribble (player large, on the ball), close_skill, face (face/portrait close-up), celebration, medal, pre_match (bus, tunnel, locker room, aerial, crest, trophy, warm-up),
wide_goal (wide tactical shot following the ball that ends in a shot/goal/assist), wide_other, medium_action, medium_idle, reject.
intro_kind (only for pre_match): bus|tunnel|aerial|locker|crest|trophy|warmup|none. quality 1-5 (5 = best). flags (list, may be empty): blur, referee_bench, foul, player_lost_ball, net_mesh, internal_cut (frames look like different shots).
Answer ONLY JSON: {{"rows":[{{"id":"..","class":"..","intro_kind":"..","quality":3,"flags":[]}}]}}'''
def classify_ai(rows,player,router):
    out={}; by_sheet={}
    srcs=sorted({r['src'] for r in rows})
    for s in srcs:
        rs=[r for r in rows if r['src']==s]
        for i in range(0,len(rs),8): by_sheet.setdefault((s,i//8),rs[i:i+8])
    for (s,k),rs in sorted(by_sheet.items()):
        img=f'{SHOTS}/{s}_sheet_{k:02d}.jpg'
        if not os.path.exists(img): continue
        txt,m=router.ask('vision',PROMPT.format(player=player,ids=', '.join(r['id'] for r in rs)),images=[img],json_mode=True)
        time.sleep(float(os.environ.get('AI_THROTTLE','4')))
        if not txt: continue
        try:
            for o in json.loads(txt)['rows']:
                out[o['id']]=dict(cls=o.get('class','reject'),kind=o.get('intro_kind','none'),quality=int(o.get('quality',3)),flags=[f for f in o.get('flags',[]) if f in REJECT_FLAGS])
        except Exception: continue
    return out
def build_candidates(rows,cl,fps):
    C=[]
    for r in rows:
        c=cl.get(r['id'])
        if not c or c['quality']<3 or set(c['flags'])&REJECT_FLAGS: continue
        group=CLASS_MAP.get(c['cls'])
        if c['cls'] in ('wide_other','medium_idle','reject') or group is None: continue
        C.append(dict(id=r['id'],src=r['src'],s=r['s'],e=r['e'],avail=r['e']-r['s']-0.05,group=group,kind=c['kind'],q=c['quality'],fps=fps[r['src']],motion=r['motion']))
    return C
def fit(c,b_pref,P,floor_override=None):
    floor=1.0 if c['group']=='wide' else (0.6 if c['fps']>=50 else 0.8)
    if floor_override: floor=floor_override
    for b in range(b_pref,6,-1):
        need=b*P; sp=min(1.0,c['avail']/need); sp=math.floor(sp*100)/100
        if sp>=floor: return b,sp
    return None
def take(pool,used,prev_src,b_pref,P,floor_override=None):
    cands=sorted([c for c in pool if c['id'] not in used],key=lambda c:(-c['q'],-c['avail']))
    order=[c for c in cands[:4] if c['src']!=prev_src]+[c for c in cands[:4] if c['src']==prev_src]+cands[4:]
    for c in order:
        f=fit(c,b_pref,P,floor_override)
        if f: return c,f
    return None,None
def row_of(c,b,sp,cid,sec,zb=None,ay=None):
    z=1.15 if (c['group']=='medium' and zb is None) else 1.0; a=0.6 if z>1.0 else 0.5
    return [cid,sec,c['src'],round(c['s']+0.05,2),sp,b,zb or z,0.5,ay or a]
def rebalance(items,P,target,notes,label):
    """items = [[row,cand],...]. Add/remove beats clip by clip (each stays >= 7 beats and inside its window) until the section hits the target exactly."""
    diff=target-sum(r[5] for r,c in items); guard=0
    while diff!=0 and guard<300:
        guard+=1; moved=False
        for r,c in (items if diff>0 else items[::-1]):
            nb=r[5]+(1 if diff>0 else -1)
            if nb<7: continue
            f=fit(c,nb,P)
            if f and f[0]==nb: r[5]=nb; r[4]=f[1]; diff+=-1 if diff>0 else 1; moved=True
            if diff==0: break
        if not moved: break
    if diff!=0: notes.append(f'{label} beat remainder {diff} (not enough room in the chosen windows)')
def select(C,P,final_len,notes):
    total=int(final_len/P); intro_b=int(round(min(17.4,0.12*final_len)/P)); fin_b=int(round(min(26.5,0.18*final_len)/P)); body_b=total-intro_b-fin_b
    used=set(); prev=None
    # intro
    pool=[c for c in C if c['group']=='intro']; order={k:i for i,k in enumerate(INTRO_ORDER)}; pool.sort(key=lambda c:(order.get(c['kind'],99),-c['q']))
    n=max(1,intro_b//8); base=intro_b//n; lens=[base]*n; lens[-1]+=intro_b-base*n; intro=[]
    for k,L in enumerate(lens,1):
        c=next((x for x in pool if x['id'] not in used and fit(x,L,P)),None)
        if not c: c=next((x for x in pool if x['id'] not in used and fit(x,7,P)),None)
        if not c: notes.append('intro shortfall: not enough pre-match clips'); break
        b,sp=fit(c,L,P) or fit(c,7,P); used.add(c['id']); intro.append([row_of(c,b,sp,f'i{k}','intro',1.0,0.5),c]); prev=c['src']
    rebalance(intro,P,intro_b,notes,'intro')
    # finale
    fpool=[c for c in C if c['group']=='celebration']; fin=[]; targets=([13]+[7]*4)[:max(0,min(5,fin_b//8-1))]
    for k,L in enumerate(targets,1):
        c,f=take(fpool,used,None,L,P)
        if not c: break
        b,sp=f; used.add(c['id']); fin.append([row_of(c,b,sp,f'f{k}','finale'),c])
    rest=fin_b-sum(r[5] for r,c in fin); c,f=take(fpool,used,None,max(rest,7),P,0.55)
    if c: b,sp=f; used.add(c['id']); fin.append([row_of(c,b,sp,f'f{len(fin)+1}','finale'),c])
    else: notes.append('finale shortfall: not enough celebration clips')
    rebalance(fin,P,fin_b,notes,'finale')
    # body
    pools={g:[c for c in C if c['group']==g] for g in ('close','medium','wide','face')}
    cap={'wide':0.25,'face':0.2}; beats={g:0 for g in pools}; seq=['close','close','wide','medium','close','face','close','wide','medium','close','face']; i=0; body=[]; bsum=0; guard=0; prev=None
    while bsum<body_b and guard<400:
        guard+=1; g=seq[i%len(seq)]; i+=1; picked=None
        for gg in [g]+[x for x in ('close','medium','wide','face') if x!=g]:
            if gg in cap and beats[gg]+PREF[gg]>cap[gg]*body_b: continue
            c,f=take(pools[gg],used,prev,PREF[gg],P)
            if c: picked=(gg,c,f); break
        if not picked:
            c,f=take([x for v in pools.values() for x in v if not(x['group'] in cap and beats[x['group']]+7>cap[x['group']]*body_b)],used,prev,7,P)
            if not c: break
            picked=(c['group'],c,f)
        gg,c,(b,sp)=picked; used.add(c['id']); beats[gg]+=b; bsum+=b; prev=c['src']; body.append([row_of(c,b,sp,f'n{len(body)+1:02d}','body'),c])
    rebalance(body,P,body_b,notes,'body')
    bsum=sum(r[5] for r,c in body)
    if bsum<body_b: notes.append(f'BODY SHORTFALL {bsum}/{body_b} beats: not enough usable clips; human review or more footage needed')
    wide=sum(r[5] for r,c in body if c['group']=='wide'); face=sum(r[5] for r,c in body if c['group']=='face')
    for sec,items in (('intro',intro),('body',body),('finale',fin)): pass
    return [r for r,c in intro+body+fin], dict(total=total,intro=intro_b,body=body_b,finale=fin_b,wide_share=round(wide/max(bsum,1),3),face_share=round(face/max(bsum,1),3))
def main():
    player=sys.argv[1] if len(sys.argv)>1 else 'the player'; b=json.load(open(PIPE+'/beat.json')); P=b['P']; final_len=float(os.environ.get('FINAL_LEN') or math.floor(b['end']*2)/2)
    info=json.load(open(WORK+'/analysis/src_info.json')); srcs=[s for s in ('gal','dor','rma') if s in info]; rows=load_rows(srcs); notes=[]
    durs={s:info[s]['dur'] for s in srcs}; fps={s:info[s]['fps'] for s in srcs}
    cl={}
    if os.environ.get('USE_AI','1')=='1':
        try:
            sys.path.insert(0,os.path.dirname(os.path.abspath(__file__))); import gemini_router as router
            cl=classify_ai(rows,player,router); notes+=router.EVENTS
        except Exception as e: notes.append(f'AI classification unavailable: {type(e).__name__}')
    miss=[r for r in rows if r['id'] not in cl]
    if miss:
        loc=classify_local(miss,durs); cl.update(loc); notes.append(f'DEGRADED: {len(miss)} of {len(rows)} shots classified by the local heuristic (no vision). Human review of the result is recommended.')
    C=build_candidates(rows,cl,fps); clips,stats=select(C,P,final_len,notes)
    json.dump(dict(final_len=final_len,clips=clips),open(PIPE+'/edl_input.json','w')); json.dump(dict(stats=stats,notes=notes,classified_by_ai=len(cl)-len(miss),total_rows=len(rows)),open(WORK+'/analysis/plan_notes.json','w'),indent=1)
    print('plan:',stats,'| notes:',notes[-3:])
if __name__=='__main__': main()
