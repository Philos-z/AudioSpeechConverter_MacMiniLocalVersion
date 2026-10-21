"""Replace a brand from clean picture frames; retain accepted subtitles/audio.

No ASR, translation, inference, timeline edits, or old-watermark covering.
"""
import argparse
import json
from pathlib import Path
import tempfile
import time
from PIL import Image
from export_size import FFMPEG, probe, run, sha, audio_hash, audio_payload_bytes, video_budget, validate_streams


def export(picture, accepted, subtitles, logo, output, report):
    picture,accepted,subtitles,logo,output,report=map(lambda p:Path(p).resolve(),(picture,accepted,subtitles,logo,output,report))
    if output.exists() or output in (picture,accepted):raise FileExistsError('Preserve original and existing outputs')
    before=probe(accepted);clean=probe(picture)
    video=next(s for s in before['streams'] if s['codec_type']=='video')
    raw=next(s for s in clean['streams'] if s['codec_type']=='video')
    frames=int(video['nb_frames']);duration=frames/25
    if (video['width'],video['height'],video['avg_frame_rate'])!=(960,540,'25/1'):
        raise ValueError('Require the accepted 960x540/25 fps export')
    if int(raw['nb_frames'])!=frames or (raw['width'],raw['height'])!=(960,540):
        raise ValueError('Clean picture frame coverage differs from accepted export')
    sources={str(p):sha(p) for p in (picture,accepted,subtitles,logo)}
    audio_identity=audio_hash(accepted);payload=audio_payload_bytes(accepted)
    bitrate=video_budget(duration,payload);output.parent.mkdir(parents=True,exist_ok=True)
    began=time.monotonic();attempts=[]
    with tempfile.TemporaryDirectory(prefix='.rebrand_',dir=output.parent) as folder:
        work=Path(folder);mark=Image.open(logo).convert('RGBA')
        mark=mark.resize((144,round(mark.height*144/mark.width)),Image.Resampling.LANCZOS)
        if mark.height>26:raise ValueError('Wordmark would overlap the source date')
        mark.putalpha(mark.getchannel('A').point(lambda x:round(x*.55)))
        mark_path=work/'mark.png';mark.save(mark_path)
        escaped=str(subtitles).replace('\\','\\\\').replace(':','\\:').replace("'","\\'")
        filters=f"[0:v:0]setpts=N/(25*TB),ass=filename='{escaped}'[s];[s][2:v:0]overlay=16:24:shortest=1,format=yuv420p[v]"
        target=work/'candidate.mp4'
        for attempt in range(1,4):
            common=[FFMPEG,'-hide_banner','-loglevel','error','-nostats','-y','-i',picture,'-i',accepted,
                '-loop','1','-framerate','25','-i',mark_path,'-filter_complex',filters,'-map','[v]',
                '-c:v','libx264','-preset','slow','-threads','4','-b:v',bitrate,'-pix_fmt','yuv420p',
                '-color_range','tv','-r','25','-fps_mode','cfr','-frames:v',frames,'-t',duration,
                '-passlogfile',work/'pass']
            print(json.dumps({'phase':'video_pass_1','attempt':attempt}),flush=True)
            run([*common,'-pass','1','-an','-f','null','/dev/null'])
            print(json.dumps({'phase':'video_pass_2','attempt':attempt}),flush=True)
            run([*common,'-map','1:a','-c:a','copy','-map_metadata','1','-metadata','title=Philos.club',
                '-pass','2','-movflags','+faststart',target])
            attempts.append({'attempt':attempt,'video_bitrate_bps':bitrate,'bytes':target.stat().st_size})
            if target.stat().st_size<50_000_000:break
            bitrate=int(bitrate*.90)
        else:raise ValueError('Final video is not below 50 MB')
        validate_streams(before,probe(target))
        if audio_hash(target)!=audio_identity:raise ValueError('Accepted audio packets changed')
        run([FFMPEG,'-v','error','-xerror','-i',target,'-map','0:v','-map','0:a','-f','null','-'])
        if any(sha(Path(path))!=value for path,value in sources.items()):raise ValueError('Original input changed')
        result={'schema':'clean_picture_rebrand_v1','output':str(output),'duration_s':duration,'frames':frames,
            'bytes':target.stat().st_size,'sha256':sha(target),'encoder':'libx264','burned_bilingual_subtitles':True,
            'logo':{'brand':'Philos.club','width_px':144,'height_px':mark.height,'opacity':.55,'position':[16,24],
                    'style':'serif_box_v2' if logo.name=='philos_club_serif_wordmark.png' else 'transparent_sans_v1'},
            'size_limit':{'max_bytes_exclusive':50_000_000,'target_bytes':48_000_000,'attempts':attempts},
            'source_files':sources,'clean_picture_frame_clock_rebuilt':True,'audio_packet_sha256':audio_identity,
            'audio_packets_identical':True,'subtitles_unchanged':True,'full_decode_passed':True,
            'tts_or_proxy_called':False,'wall_s':time.monotonic()-began}
        target.replace(output)
    report.parent.mkdir(parents=True,exist_ok=True);report.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'output':str(output),'bytes':result['bytes'],'audio_packets_identical':True}),flush=True)
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser()
    for name in ('picture','accepted','subtitles','logo','output','report'):p.add_argument('--'+name,type=Path,required=True)
    a=p.parse_args();export(a.picture,a.accepted,a.subtitles,a.logo,a.output,a.report)
