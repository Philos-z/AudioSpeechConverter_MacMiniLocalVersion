"""Burn Chinese-over-German captions and mux the revised local mix."""
import argparse
import json
from pathlib import Path
import re
import subprocess

from bilingual_subtitles import build_ass
from video import confined, probe


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True)
    p.add_argument('--job',required=True);p.add_argument('--variant',default='bg_bilingual_v2')
    p.add_argument('--logo-file',type=Path);p.add_argument('--logo-width',type=int)
    p.add_argument('--logo-opacity',type=float,default=.55)
    p.add_argument('--no-ai-label',action='store_true')
    args=p.parse_args();root=args.root.resolve()
    if args.logo_file is None:args.logo_file=root/'assets/philos_club_wordmark.png'
    brand=args.logo_file.name=='philos_club_wordmark.png'
    if args.logo_width is None:args.logo_width=144 if brand else 112
    if not all(re.fullmatch(r'[A-Za-z0-9_-]{1,60}',s) for s in [args.job,args.variant]):p.error('Invalid task/variant')
    if not 40<=args.logo_width<=320 or not 0<args.logo_opacity<=1:p.error('Invalid logo settings')
    job=confined(root,'jobs/'+args.job);out=job/'outputs'/args.variant
    if not (out/'mix_report.json').exists():raise ValueError('Revised mix not ready')
    source=json.loads((job/'source.json').read_text())
    ffmpeg=Path('/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg')
    if not ffmpeg.is_file():raise FileNotFoundError('FFmpeg with libass is required')
    ass=out/'bilingual.ass';layout=build_ass(job/'timeline.json',ass)
    (out/'subtitle_layout.json').write_text(json.dumps(layout,ensure_ascii=False,indent=2)+'\n')
    ass_filter=str(ass).replace('\\','\\\\').replace(':','\\:').replace("'","\\'")
    command=[ffmpeg,'-hide_banner','-y','-i',source['input'],'-i',out/'zh_mix.wav']
    logo_report=None
    if args.logo_file:
        logo=confined(root,str(args.logo_file.resolve().relative_to(root)))
        command+=['-loop','1','-i',logo]
        position=[16,24] if brand else [20,18]
        filters=(f"[2:v]scale={args.logo_width}:-1:flags=lanczos,format=rgba,colorchannelmixer=aa={args.logo_opacity}[logo];"
            f"[0:v][logo]overlay={position[0]}:{position[1]}:shortest=1,ass=filename='{ass_filter}'[v]")
        logo_report={'source':str(logo),'width_px':args.logo_width,'opacity':args.logo_opacity,'position':position,
                     'brand':'Philos.club' if brand else 'legacy'}
    elif not args.no_ai_label:
        command+=['-loop','1','-i',job/'ai_label.png']
        filters=f"[0:v][2:v]overlay=24:20:shortest=1,ass=filename='{ass_filter}'[v]"
    else:
        filters=f"[0:v]ass=filename='{ass_filter}'[v]"
    command+=['-filter_complex',filters,
        '-map','[v]','-map','1:a:0','-map','0:a:0',
        '-c:v','h264_videotoolbox','-b:v','2800k','-pix_fmt','yuv420p',
        '-c:a:0','aac','-b:a:0','192k','-c:a:1','copy',
        '-metadata:s:a:0','language=zho','-metadata:s:a:0','title=AI Chinese dubbing - enhanced background',
        '-metadata:s:a:1','language=deu','-metadata:s:a:1','title=Original German',
        '-disposition:a:0','default','-disposition:a:1','0',
        '-metadata','title=AI Chinese dubbing - burned bilingual captions - private local evaluation',
        '-t',str(source['duration_s']),'-movflags','+faststart',out/'dubbed_full_zh.mp4']
    with (out/'export.log').open('w') as log:
        result=subprocess.run([str(x) for x in command],stdout=log,stderr=log)
    if result.returncode:raise RuntimeError('Bilingual export failed: see variant export.log')
    from export_size import enforce
    size_limit=enforce(out/'dubbed_full_zh.mp4')
    report={'status':'exported_requires_listening_review','burned_bilingual_subtitles':True,
        'custom_logo':logo_report,'ai_label_visible':not args.logo_file and not args.no_ai_label,
        'languages_order':['zh','de'],'soft_subtitle_tracks':0,
        'source_duration_s':source['duration_s'],'probe':probe(out/'dubbed_full_zh.mp4'),
        'size_limit':size_limit}
    (out/'export_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print('BILINGUAL_EXPORTED',out,flush=True)


if __name__=='__main__':main()
