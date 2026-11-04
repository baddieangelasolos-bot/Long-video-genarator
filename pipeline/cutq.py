import numpy as np, sys, json
S='/home/claude/work/src'
def cuts(name,a,b,fps,thr=14.0,k=3.0):
    d=np.load(f'{S}/full_{name}.npy'); out=[]
    for i in range(max(int(a*fps),9),min(int(b*fps),len(d)-9)):
        loc=np.median(np.concatenate([d[i-8:i],d[i+1:i+9]]))
        if d[i]>max(thr,k*loc+6): out.append((round((i+1)/fps,2),round(float(d[i]),1)))
    m=[]
    for t,v in out:
        if m and t-m[-1][0]<0.1:
            if v>m[-1][1]: m[-1]=(t,v)
        else: m.append((t,v))
    return m
if __name__=='__main__':   # python3 cutq.py name a b fps
    print(cuts(sys.argv[1],float(sys.argv[2]),float(sys.argv[3]),float(sys.argv[4])))
