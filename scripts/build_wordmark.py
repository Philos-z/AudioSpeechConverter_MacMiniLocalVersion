"""Render the editable Philos.club text mark on a transparent canvas."""
from pathlib import Path
import argparse
from PIL import Image, ImageDraw, ImageFont

TEXT='Philos.club'
FONT='/System/Library/Fonts/Supplemental/Arial Bold.ttf'

def build(output):
    font=ImageFont.truetype(FONT,96)
    box=font.getbbox(TEXT,stroke_width=4)
    mark=Image.new('RGBA',(box[2]-box[0]+16,box[3]-box[1]+16),(0,0,0,0))
    ImageDraw.Draw(mark).text((8-box[0],8-box[1]),TEXT,font=font,fill=(255,255,255,255),
        stroke_width=4,stroke_fill=(0,0,0,170))
    mark=mark.resize((576,round(mark.height*576/mark.width)),Image.Resampling.LANCZOS)
    path=Path(output);path.parent.mkdir(parents=True,exist_ok=True);mark.save(path)
    return {'brand':TEXT,'width':mark.width,'height':mark.height,'transparent':True}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,default=Path(__file__).resolve().parents[1]/'assets/philos_club_wordmark.png')
    print(build(p.parse_args().output))
