#!/usr/bin/env python3
"""python3 finalize.py CARDS_DIR -> patches pipeline/edl.json generically: title on the first two intro clips, end card + full-frame + fade-out on the last clip."""
import json, sys, os
P=os.environ.get('PIPE_DIR',os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),'pipeline')); cards=sys.argv[1]; e=json.load(open(P+'/edl.json'))
intro=[c for c in e if c['sec']=='intro'] or e[:2]
if intro: intro[0]['card']=cards+'/title.png'; intro[0]['card_fade']=[1.74,0.6]
if len(intro)>1: intro[1]['card']=cards+'/title.png'; intro[1]['card_fade']=[0,0.05]
e[-1]['card']=cards+'/endcard.png'; e[-1]['card_fade']=[1.7,0.8]; e[-1]['fullframe']=True; e[-1]['last_fade']=True
json.dump(e,open(P+'/edl.json','w'),indent=1); print('finalized',len(e),'clips; end-card clip',e[-1]['id'])
