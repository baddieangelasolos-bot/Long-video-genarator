"""Local (no-AI) window check used by edl.py.
local_window_ok(src, start_s, end_s, fps) -> True  : no detected cut inside the window
                                             False : a cut/re-frame sits inside the window (0.05 s margin at both ends)
                                             None  : no scan data (full_<src>.npy missing) -> check skipped
Scan data comes from fullscan.py; cut rule is cutq.cuts (frame diff > max(14, 3 x local median + 6))."""
import os, sys
HERE=os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0,HERE)
import cutq
MARGIN=0.05
def local_window_ok(src,a,b,fps):
    if not os.path.exists(f'{cutq.S}/full_{src}.npy'): return None
    try: found=cutq.cuts(src,max(0.0,a-0.5),b+0.5,fps)
    except Exception: return None
    return not any(a+MARGIN<t<b-MARGIN for t,_ in found)
