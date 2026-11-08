#!/usr/bin/env python3
"""If the downloaded sources are byte-identical to a stored plan's sources, reuse that proven plan (edl_input + delogo + grade). Exit 0 = applied, 3 = no match."""
import json, hashlib, os, sys, shutil
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__))); SRC=os.environ.get('SRC_DIR','/home/claude/work/src'); PIPE=os.environ.get('PIPE_DIR','/home/claude/work/pipeline')
def sha(p):
    h=hashlib.sha256()
    with open(p,'rb') as f:
        for b in iter(lambda:f.read(1<<20),b''): h.update(b)
    return h.hexdigest()
def signature():
    """Identity of the input set. The old fixed project files keep their original signature; any new set (s01.mp4... + music + reference) gets its own, so a new upload can never be mistaken for the stored plan."""
    import re
    dyn=sorted(f for f in os.listdir(SRC) if re.fullmatch(r's\d\d\.mp4',f))
    files=[n for n in ('gal.mp4','dor.mp4','rma.mp4','music.m4a')] if not dyn else dyn+['music.m4a']+(['ref.mp4'] if os.path.exists(f'{SRC}/ref.mp4') else [])
    return hashlib.sha256('|'.join(f'{n}:{sha(f"{SRC}/{n}")}' for n in files).encode()).hexdigest()
KG=next((d for d in (ROOT+'/known_good',ROOT+'/autonomy/known_good',os.path.dirname(os.path.abspath(__file__))+'/known_good') if os.path.exists(d+'/index.json')),ROOT+'/known_good')
if __name__=='__main__':
    if not os.path.exists(KG+'/index.json'): print('known_good/index.json not found in the repo (looked in known_good/ and autonomy/known_good/)'); sys.exit(3)
    idx=json.load(open(KG+'/index.json')); sig=signature()
    for plan in idx['plans']:
        if plan['signature']==sig:
            d=KG+'/'+plan['dir']; shutil.copy(d+'/edl_input.json',PIPE+'/edl_input.json')
            cfg=json.load(open(PIPE+'/config.json')); k=json.load(open(d+'/config.json')); cfg['delogo']=k['delogo']; cfg['grade']=k['grade']; json.dump(cfg,open(PIPE+'/config.json','w'),indent=1)
            print('KNOWN_GOOD plan applied:',plan['dir'],'player',plan['player'],'club',plan['club']); sys.exit(0)
    print('no known-good plan matches these sources (signature',sig[:12],')'); sys.exit(3)
