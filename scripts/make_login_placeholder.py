import random, math
from PIL import Image, ImageDraw, ImageFilter
random.seed(7)
W,H=2400,1500
img=Image.new("RGB",(W,H),(22,20,19))
d=ImageDraw.Draw(img,"RGBA")
# vertical warm/dark gradient
for y in range(H):
    t=y/H
    d.line([(0,y),(W,y)],fill=(int(30-12*t),int(26-10*t),int(24-10*t),255))
# windows (center-left) with dusk sky
wx0,wx1,wy0,wy1=820,1500,180,980
for y in range(wy0,wy1):
    t=(y-wy0)/(wy1-wy0)
    c=(int(70+130*t),int(62+60*t),int(80-10*t))
    d.line([(wx0,y),(wx1,y)],fill=c+(255,))
# city silhouettes
x=wx0
while x<wx1:
    w=random.randint(40,110); h=random.randint(120,420)
    d.rectangle([x,wy1-h,x+w,wy1],fill=(40,36,40,235))
    for _ in range(random.randint(3,12)):
        lx=random.randint(x+4,x+w-8); ly=random.randint(wy1-h+8,wy1-10)
        d.rectangle([lx,ly,lx+5,ly+4],fill=(255,200,130,200))
    x+=w+random.randint(4,20)
# mullions
for mx in range(wx0,wx1+1,170): d.rectangle([mx-10,wy0,mx+10,wy1],fill=(18,16,16,255))
for my in (wy0,wy0+330,wy1): d.rectangle([wx0,my-10,wx1,my+10],fill=(18,16,16,255))
# right wall screens (cool glow)
d.rounded_rectangle([1650,200,2350,700],20,fill=(36,44,58,255))
d.rounded_rectangle([1700,250,2050,520],10,fill=(58,78,104,255))
d.ellipse([2100,300,2300,500],fill=(110,60,70,255))
# table
d.polygon([(500,1010),(1900,960),(2100,1130),(300,1180)],fill=(92,68,50,255))
# people silhouettes with warm skin highlights
def person(cx,cy,s,shirt):
    d.ellipse([cx-150*s,cy+60*s,cx+150*s,cy+520*s],fill=shirt+(255,))
    d.ellipse([cx-58*s,cy-70*s,cx+58*s,cy+70*s],fill=(150,98,72,255))
    d.ellipse([cx-62*s,cy-84*s,cx+62*s,cy-10*s],fill=(28,22,20,255))
#person(1180,560,1.25,(70,68,72))
#person(1500,700,1.1,(120,70,80))
#person(1760,520,1.15,(40,38,40))
# laptop glow
#d.polygon([(1640,840),(1860,820),(1880,990),(1660,1000)],fill=(150,150,160,255))
# pendant lamps
for lx,ly in ((1000,90),(1350,170),(800,40)):
    d.polygon([(lx-120,ly),(lx+120,ly),(lx+90,ly-110),(lx-90,ly-110)],fill=(25,22,20,255))
    d.ellipse([lx-120,ly-14,lx+120,ly+26],fill=(255,190,110,255))
img=img.filter(ImageFilter.GaussianBlur(26))
d=ImageDraw.Draw(img,"RGBA")
# warm bokeh after blur
for _ in range(38):
    x=random.randint(700,2400); y=random.randint(100,900); r=random.randint(10,38)
    a=random.randint(20,60)
    d.ellipse([x-r,y-r,x+r,y+r],fill=(255,random.randint(150,210),110,a))
for lx,ly in ((1000,96),(1350,176),(800,46)):
    for r,a in ((160,40),(90,70),(40,110)): d.ellipse([lx-r,ly-r*.35,lx+r,ly+r*.35],fill=(255,196,120,a))
# foreground plants (left bottom, right) very blurred
fg=Image.new("RGBA",(W,H),(0,0,0,0)); f=ImageDraw.Draw(fg)
for _ in range(40):
    x=random.choice([random.randint(0,900),random.randint(2000,2400)]); y=random.randint(1150,1500)
    f.ellipse([x-90,y-40,x+90,y+40],fill=(24,38,24,200))
for _ in range(25):
    x=random.randint(2150,2400); y=random.randint(600,1100)
    f.ellipse([x-60,y-30,x+60,y+30],fill=(40,60,30,170))
fg=fg.filter(ImageFilter.GaussianBlur(30))
img=Image.alpha_composite(img.convert("RGBA"),fg).convert("RGB").filter(ImageFilter.GaussianBlur(6))
# grain
noise=Image.effect_noise((W,H),18).convert("RGB")
img=Image.blend(img,noise,0.035)
img.save("full.png")
for w in (2400,1600,960):
    im=img.resize((w,int(H*w/W)),Image.LANCZOS)
    im.save(f"login-hero-{w}.webp",quality=72,method=6)
    im.save(f"login-hero-{w}.jpg",quality=74,optimize=True,progressive=True)
