subprocess.run(['ffmpeg','-v','error','-y','-i',src,'-ac','1','-ar','22050',WAV],check=True,timeout=250)
w=wave.open(WAV); sr=w.getframerate(); n=w.getnframes()
x=np.frombuffer(w.readframes(n),dtype=np.int16).astype(np.float32)/32768
idx=np.where(np.abs(x)>0.01)[0]
if len(idx)==0: sys.exit('FATAL: audio is silent')
end=idx[-1]/sr; print('audible %.2fs to %.2fs of file %.2fs (trailing silence %.2fs)'%(idx[0]/sr,end,n/sr,n/sr-end))
hop=128; f,t,Z=stft(x,fs=sr,nperseg=1024,noverlap=1024-hop)
S=np.log1p(10*np.abs(Z)); flux=np.concatenate([[0],np.maximum(0,np.diff(S,axis=1)).sum(0)]); flux=np.maximum(flux-np.median(flux),0); h=hop/sr
ac=np.correlate(flux,flux,'full')[len(flux)-1:]; lo,hi=int(0.30/h),min(int(1.0/h),len(ac)-1)
cand=np.argsort(ac[lo:hi])[::-1][:5]+lo; print('autocorr candidates BPM:',[round(float(60/(c*h)),1) for c in cand])
def score(P,off,a,b):
    ks=np.arange(np.ceil((a-off)/P),np.floor((b-off)/P)); 
    if len(ks)==0: return 0.0
    i=np.clip(np.round((off+ks*P)/h).astype(int),1,len(flux)-2)
    return float(np.maximum.reduce([flux[i-1],flux[i],flux[i+1]]).sum()/len(ks))
def fit(a,b):
    best=max(((score(P,o,a,b),P,o) for P in np.arange(pmin,pmax,0.002) for o in np.arange(0,P,0.01)))
    _,P0,o0=best
    best=max(((score(P,o,a,b),P,o%P) for P in np.arange(P0-0.003,P0+0.003,0.0002) for o in np.arange(o0-0.012,o0+0.012,0.001)))
    return best
def fit_off(P,a,b,step=0.002):
    return max(((score(P,o,a,b),o) for o in np.arange(0,P,step)))
# 1) global fit: coarse P, then very fine P (step 0.00002) because phase error = dP x number_of_beats
_,P0,o0=fit(0,end)
best=max(((score(P,o,0,end),P,o%P) for P in np.arange(P0-0.0012,P0+0.0012,0.00002) for o in np.arange(o0-0.02,o0+0.02,0.001)))
_,P,o=best
# 2) per-segment phase with the SAME global period, plus each segment's independent period for reference
def pd(a,b,P): d=abs(a-b)%P; return min(d,P-d)
segs=[]; print('segment fits (phase measured against global P):')
for a in np.arange(0,max(end-15,1),30):
    b=min(a+30,end); sc,oo=fit_off(P,a,b); _,Pi,_=fit(a,b)
    segs.append(dict(a=float(a),P=float(Pi),off=float(oo))); print('%.0f-%.0f  off=%.3f (global %.3f, diff %+.0f ms)  own-period BPM=%.2f'%(a,b,oo,o,1000*((oo-o+P/2)%P-P/2),60/Pi))
Ps=[x['P'] for x in segs]; phase_ok=all(pd(x['off'],o,P)<0.04 for x in segs); period_ok=bool(max(Ps)-min(Ps)<0.003); const=bool(period_ok and phase_ok)
print(f'GLOBAL P={P:.5f} BPM={60/P:.3f} OFF={o:.3f} | period agrees: {period_ok} | phase agrees (<40ms): {phase_ok}',('| CONSTANT GRID OK' if const else '| NOT CONSTANT/PHASE UNSTABLE: do not lock a grid on this fit'))
print('NOTE: check half/double-tempo against the reference edit pacing before locking the grid.')
json.dump(dict(P=float(P),OFF=float(o),end=float(end),file_len=n/sr,constant=const,phase_spread=float(max(pd(x['off'],o,P) for x in segs)),segments=segs),open(os.environ.get('BEAT_OUT',os.path.join(os.path.dirname(os.path.abspath(__file__)),'beat.json')),'w'),indent=1)
