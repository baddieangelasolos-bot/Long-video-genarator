import os, sys, tempfile, shutil, subprocess, importlib
tmp=tempfile.mkdtemp(); os.environ['WORK_DIR']=tmp; os.environ['PIPE_DIR']=tmp+'/pipeline'; os.environ['MECH_SLEEP_SCALE']='0'
os.makedirs(tmp+'/pipeline')
open(tmp+'/pipeline/demo.py','w').write("def f():\n    x = 1\n    return x\nprint(f())\n")
sys.path.insert(0,os.path.join(os.path.dirname(__file__),'..','autonomy'))
import mechanic as M
res=[]
def chk(n,c): res.append(c); print(('PASS ' if c else 'FAIL ')+n)
def diff(new_line,path='pipeline/demo.py',old='    x = 1',fn='demo.py'):
    return f"--- a/{path}\n+++ b/{path}\n@@ -1,3 +1,3 @@\n def f():\n-{old}\n+{new_line}\n     return x\n"
def src(): return open(tmp+'/pipeline/demo.py').read()
m=M.Mechanic()
orig=src()
ok,why=m.safe_apply_patch(diff('    x = 2'),'t1'); chk('valid patch applied: '+why, ok and 'x = 2' in src())
m.revert_patches('t1'); chk('revert restores original', src()==orig)
for name,d in [('forbidden fps line',diff('    x = 30  # fps')),('forbidden 1920',diff('    x = 1920')),('forbidden secret',diff('    x = GEMINI_API_KEY')),
               ('forbidden network',diff('    x = requests.get(1)')),('deletion rmtree',diff('    shutil.rmtree(x)')),
               ('bad path ..',diff('    x = 2',path='../evil.py')),('absolute path',diff('    x = 2',path='/etc/evil.py')),
               ('non-py file',diff('    x = 2',path='pipeline/notes.txt')),('nonexistent file',diff('    x = 2',path='pipeline/ghost.py')),
               ('empty',''),('too large','\n'.join(['+x']*250)),
               ('syntax error',diff('    x = (')),('does not apply',diff('    x = 2',old='    y = 9'))]:
    ok,why=m.safe_apply_patch(d,'t2'); chk(f'rejected {name} ({why[:40]})', (not ok) and src()==orig)
three="--- a/pipeline/demo.py\n+++ b/pipeline/demo.py\n@@ -1 +1 @@\n-a\n+b\n--- a/pipeline/a.py\n+++ b/pipeline/a.py\n@@ -1 +1 @@\n-a\n+b\n--- a/pipeline/b.py\n+++ b/pipeline/b.py\n@@ -1 +1 @@\n-a\n+b\n"
ok,why=m.safe_apply_patch(three,'t3'); chk('rejected 3 files',not ok)
# stage runner end-to-end: failing stage + Gemini patch that does not fix -> reverted; patch that fixes -> kept
class G:
    def __init__(s,patch): s.p=patch
    def ask(s,role,prompt,json_mode=False):
        import json; return json.dumps({'diagnosis':'d','action':'propose_patch','patch':s.p}),'fake'
def mk(patch): 
    mm=M.Mechanic(gemini=G(patch)); return mm
def broken_stage():
    r=subprocess.run([sys.executable,tmp+'/pipeline/demo.py'],capture_output=True,text=True)
    if r.returncode or r.stdout.strip()!='9': raise M.StageFail('demo failed',r.stdout+r.stderr)
# make demo fail initially by returning wrong value
open(tmp+'/pipeline/demo.py','w').write("def f():\n    x = 1\n    return x\nprint(f())\n"); orig=src()
m2=mk(diff('    x = 5')); 
try: m2.stage('s_bad',broken_stage,retries=2); chk('unfixed stage halts',False)
except M.Halt: chk('unfixed stage halts',True)
chk('patch reverted after failed stage',src()==orig); chk('revert logged',any('reverted Gemini patch' in f for f in m2.fixes))
m3=mk(diff('    x = 9')); m3.stage('s_good',broken_stage,retries=2)
chk('good patch kept and stage ok', 'x = 9' in src() and m3.stages[-1]['ok'])
chk('good patch recorded with diff for email', len(m3.patches)==1 and os.path.exists(m3.patches[0]['diff']))
print(f"\n{sum(res)}/{len(res)} passed"); shutil.rmtree(tmp); sys.exit(0 if all(res) else 1)
