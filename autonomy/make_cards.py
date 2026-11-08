#!/usr/bin/env python3
"""python3 make_cards.py "PLAYER NAME" "CLUB NAME" OUTDIR -> title.png + endcard.png (1920x1080 RGBA, tracked caps, glow + shadow)."""
import sys, os
from PIL import Image, ImageDraw, ImageFont, ImageFilter
FONTS=['/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf','/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf']
def font(sz):
    for p in FONTS:
        if os.path.exists(p): return ImageFont.truetype(p,sz)
    return ImageFont.load_default()
def tracked(d,text,f,xc,y,track,fill):
    ws=[d.textlength(c,font=f) for c in text]; x=xc-(sum(ws)+track*(len(text)-1))/2
    for c,w in zip(text,ws): d.text((x,y),c,font=f,fill=fill); x+=w+track
def layer(texts,W=1920,H=1080,grad=False):
    base=Image.new('RGBA',(W,H),(0,0,0,0))
    if grad:
        g=Image.new('RGBA',(W,H),(0,0,0,0)); gd=ImageDraw.Draw(g)
        for yy in range(int(H*.55),H): gd.line([(0,yy),(W,yy)],fill=(0,0,0,int(185*(yy-H*.55)/(H*.45))))
        base=Image.alpha_composite(base,g)
    gl,sh,fg=[Image.new('RGBA',(W,H),(0,0,0,0)) for _ in range(3)]
    for text,size,y,track,col in texts:
        f=font(size); tracked(ImageDraw.Draw(gl),text,f,W/2,y,track,(255,255,255,150)); tracked(ImageDraw.Draw(sh),text,f,W/2+4,y+7,track,(0,0,0,200)); tracked(ImageDraw.Draw(fg),text,f,W/2,y,track,col)
    gl=gl.filter(ImageFilter.GaussianBlur(14)); sh=sh.filter(ImageFilter.GaussianBlur(6))
    for l in (gl,sh,fg): base=Image.alpha_composite(base,l)
    return base
if __name__=='__main__':
    name,club,out=sys.argv[1].upper(),sys.argv[2].upper(),sys.argv[3]; os.makedirs(out,exist_ok=True)
    size=104 if len(name)<=14 else max(60,int(104*14/len(name)))
    layer([(name,size,455 if club else 500,22,(255,255,255,255))]+([(club,36,585,14,(225,225,235,235))] if club else [])).save(out+'/title.png')
    layer([('THANKS FOR WATCHING',80,820,16,(255,255,255,255))],grad=True).save(out+'/endcard.png'); print('cards written to',out)
