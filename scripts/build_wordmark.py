"""Render Philos.club in the reference's serif, ink, gold and white-box style."""
from pathlib import Path
import argparse
from PIL import Image, ImageDraw, ImageFont

TEXT='Philos.club'
FONT=Path(__file__).resolve().parents[1]/'assets/fonts/PlayfairDisplay-Variable.ttf'

def build(output):
    # The old logo is a supplied raster, without embedded font metadata.
    # Playfair Display 700 is selected by measured glyph comparison, not by
    # claiming access to the original type file. OFL accompanies the font.
    for size in range(116,20,-1):
        font=ImageFont.truetype(str(FONT),size);font.set_variation_by_axes([700])
        box=font.getbbox(TEXT)
        tracking=-.025*size
        width=sum(font.getlength(c) for c in TEXT)+tracking*(len(TEXT)-1)
        if width<=540 and box[3]-box[1]<=84:break
    mark=Image.new('RGBA',(576,104),(255,255,255,255))
    x=(576-width)/2;y=(104-(box[3]-box[1]))/2-box[1]
    draw=ImageDraw.Draw(mark)
    for character in TEXT:
        color=(229,171,79,255) if character=='.' else (28,44,43,255)
        draw.text((x,y),character,font=font,fill=color)
        x+=font.getlength(character)+tracking
    path=Path(output);path.parent.mkdir(parents=True,exist_ok=True);mark.save(path)
    return {'brand':TEXT,'width':mark.width,'height':mark.height,'font':'Playfair Display',
            'weight':700,'tracking_em':-.025,'style':'serif_box_v2','opacity_applied_later':.55}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,default=Path(__file__).resolve().parents[1]/'assets/philos_club_serif_wordmark.png')
    print(build(p.parse_args().output))
