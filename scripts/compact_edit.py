"""Prepare and render a 960x540, speech-led local news edit.

No model inference or loudness processing. The supplied mixed WAV must already
follow the returned EDL. Existing source media and timeline files are read only.
"""
import argparse
import fcntl
import functools
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import subprocess
import time

from PIL import Image, ImageFont
import soundfile as sf

FFMPEG = '/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg'
FONT = '/System/Library/Fonts/Supplemental/Arial Unicode.ttf'
FPS, WIDTH, HEIGHT = 25, 960, 540


def logo_style(path):
    serif=Path(path).name=='philos_club_serif_wordmark.png'
    current=serif or Path(path).name=='philos_club_wordmark.png'
    return {'width_px':144 if current else 84,'opacity':.55,'position':[16,24],
            'brand':'Philos.club' if current else 'legacy',
            'style':'serif_box_v2' if serif else 'transparent_sans_v1' if current else 'legacy'}


def _json_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary=path.with_suffix(path.suffix+'.partial')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
    temporary.replace(path)


DEFAULT_JOIN_GROUPS = [[f'line{i:04}' for i in range(first,last+1)]
                      for first,last in [(14,15),(17,19),(33,35),(37,38),(39,41),(42,43),
                                         (56,57),(61,63),(65,67),(70,71),(75,76),
                                         (101,102),(107,108),(109,110),(112,113),(114,116),(132,133)]]


def boundary_pause(text):
    """Return final source punctuation and its natural boundary quiet target."""
    match=re.search(r'''([，,：:；;。.!！？?])[”’"'）)\]】》」』\s]*$''',text.rstrip())
    punctuation=match.group(1) if match else ''
    target=.18 if punctuation in ('，',',','：',':','；',';') else (
        .35 if punctuation in ('。','.','!','！','?','？') else 0.)
    return punctuation,target


def build_edl(timeline_path, output_path=None, pause_s=.35, join_groups=None):
    path=Path(timeline_path);timeline=json.loads(path.read_text())
    source=json.loads((path.parent/'source.json').read_text());rows=timeline['segments']
    if not rows or pause_s < .35:raise ValueError('Require speech rows and >=0.35s pause')
    groups=DEFAULT_JOIN_GROUPS if join_groups is None else join_groups
    by_id={r['segment_id']:i for i,r in enumerate(rows)}
    starts={};members=set()
    for group in groups:
        if not all(identifier in by_id for identifier in group):continue
        positions=[by_id[identifier] for identifier in group]
        if positions!=list(range(positions[0],positions[0]+len(positions))):
            raise ValueError('Join groups must be adjacent and ordered')
        if members.intersection(group):raise ValueError('Overlapping join groups')
        starts[group[0]]=len(group);members.update(group)
    clips=[];utterances=[];issues=[];repairs=[];cursor=0
    intro_frames=max(0,math.floor(float(rows[0]['start_s'])*FPS+1e-8))
    if intro_frames:
        clips.append({'kind':'intro','source_start_s':0.,'source_end_s':intro_frames/FPS,
                      'source_duration_s':intro_frames/FPS,'freeze_s':0.,'output_start_s':0.,
                      'output_end_s':intro_frames/FPS,'frames':intro_frames})
        cursor=intro_frames
    i=0
    while i<len(rows):
        group=rows[i:i+starts.get(rows[i]['segment_id'],1)]
        lengths=[]
        for row in group:
            audio=sf.info(row['audio']);lengths.append(audio.frames/audio.samplerate)
        internal_gaps=[];punctuation_boundaries=[];targets=[]
        for n,row in enumerate(group):
            punctuation,target=boundary_pause(row['text_zh']) if n+1<len(group) else ('',0.)
            gap=0.
            if target:
                tail=float(row.get('edge_quiet_tail_s',0.))
                head=float(group[n+1].get('edge_quiet_head_s',0.))
                if not all(math.isfinite(x) and x>=0 for x in [tail,head]):
                    raise ValueError('Invalid generated quiet-edge durations')
                gap=max(0.,target-tail-head)
            internal_gaps.append(gap);punctuation_boundaries.append(punctuation);targets.append(target)
        speech_audio=sum(lengths);punctuation_gaps=sum(internal_gaps)
        comma_gaps=sum(g for g,p in zip(internal_gaps,punctuation_boundaries) if p in ('，',','))
        speech=speech_audio+punctuation_gaps
        frames=math.ceil((speech+pause_s)*FPS-1e-8);length=frames/FPS
        start=float(group[0]['start_s']);after=i+len(group)
        boundary=float(rows[after]['start_s']) if after<len(rows) else float(source['duration_s'])
        available=max(0.,boundary-start);source_length=min(length,available)
        freeze=max(0.,length-source_length);speech_excess=max(0.,speech-available)
        identifiers=[r['segment_id'] for r in group]
        if speech_excess>.35+1e-6:
            issues.append({'ids':identifiers,'reason':'speech_exceeds_source_window_by_more_than_0.35s',
                           'speech_s':speech,'available_source_s':available,'excess_s':speech_excess,
                           'available_speech_s':max(0,available+.35)})
        if freeze:
            repairs.append({'ids':identifiers,'kind':'freeze_last_frame_and_fade_background_to_silence',
                            'freeze_s':freeze,'speech_excess_s':speech_excess})
        clip={'kind':'speech','segment_ids':identifiers,'source_start_s':start,
              'source_end_s':start+source_length,'source_duration_s':source_length,'freeze_s':freeze,
              'output_start_s':cursor/FPS,'output_end_s':(cursor+frames)/FPS,'frames':frames,
              'speech_s':speech,'speech_audio_s':speech_audio,'internal_comma_gaps_s':comma_gaps,
              'internal_punctuation_gaps_s':punctuation_gaps,
              'pause_s':length-speech,'joined':len(group)>1}
        clips.append(clip)
        audio_offset=cursor/FPS
        for row,duration,gap,punctuation,target in zip(group,lengths,internal_gaps,punctuation_boundaries,targets):
            utterances.append({**row,'audio':str(Path(row['audio']).resolve()),'speech_s':duration,
                               'output_start_s':audio_offset,'output_speech_end_s':audio_offset+duration,
                               'output_end_s':audio_offset+duration+gap,
                               'punctuation_gap_after_s':gap,'boundary_pause_target_s':target,
                               'boundary_punctuation':punctuation,
                               'comma_gap_after_s':gap if punctuation in ('，',',') else 0.,
                               'clip_index':len(clips)-1})
            audio_offset+=duration+gap
        utterances[-1]['output_end_s']=(cursor+frames)/FPS
        cursor+=frames;i=after
    edl={'format':'landscape_compact_edit_v1','source_input':source['input'],
         'source_duration_s':source['duration_s'],'source_timeline':str(path.resolve()),
         'fps':FPS,'width':WIDTH,'height':HEIGHT,'source_picture_box':[0,0,WIDTH,HEIGHT],
         'pause_target_s':pause_s,'duration_s':cursor/FPS,'frames':cursor,
         'speech_segments':len(rows),'clips':clips,'utterances':utterances,'issues':issues,
         'freeze_repairs':repairs,'join_groups':groups,
         'timing_method':'WAV speech + guarded punctuation gaps (.18s comma/colon/semicolon; .35s sentence end) + >=0.35s per semantic group; ceil 25fps; no speech truncation',
         'background_policy':'trim same source intervals as clips, fade final 40ms before freeze and zero-pad freeze',
         'retained_music_policy':'opening only; original internal gaps and trailing music removed'}
    if len(utterances)!=len(rows):raise ValueError('Missing utterances')
    if output_path:_json_write(output_path,edl)
    return edl


def _stamp(value):
    ticks = round(value*100)
    hours, ticks = divmod(ticks, 360000)
    minutes, ticks = divmod(ticks, 6000)
    seconds, ticks = divmod(ticks, 100)
    return f'{hours}:{minutes:02}:{seconds:02}.{ticks:02}'


def _wrap(text, font, language, max_width=488):
    tokens = list(text) if language == 'zh' else re.findall(r'\S+\s*|\s+', text)
    lines, current = [], ''
    for token in tokens:
        if current and font.getlength((current+token).rstrip()) > max_width:
            lines.append(current.rstrip());current = token.lstrip()
        else:
            current += token
        if font.getlength(current.rstrip()) > max_width:
            fragment = ''
            for char in current:
                if fragment and font.getlength(fragment+char) > max_width:
                    lines.append(fragment);fragment = char
                else:
                    fragment += char
            current = fragment
    if current.strip():lines.append(current.rstrip())
    if ''.join(''.join(lines).split()) != ''.join(text.split()):
        raise ValueError('Wrapping altered subtitle text')
    return lines


def _escape(text):
    return text.replace('\\','\\\u2060').replace('{',r'\{').replace('}',r'\}')


@functools.lru_cache(maxsize=32)
def _subtitle_font(font_path, size):
    return ImageFont.truetype(font_path, size)


def fit_subtitle_layout(text_zh, text_de, *, segment_id='', font_path=FONT):
    """Fit one unchanged bilingual cue within the existing landscape safe box.

    Prefer 34/24. For long cues try only integer Chinese sizes 34..30 and
    source sizes 24..20, maximizing their sum (Chinese size breaks ties).
    Line breaks are visual whitespace; every non-whitespace character remains
    in its original order. This function changes no subtitle or audio timing.
    """
    if any(not isinstance(text,str) or not text.strip() for text in (text_zh,text_de)):
        raise ValueError('Empty or invalid subtitle text: '+str(segment_id))
    font_path=str(font_path)
    wraps={}
    for language,text,sizes in (('zh',text_zh,range(34,29,-1)),('de',text_de,range(24,19,-1))):
        for size in sizes:
            font=_subtitle_font(font_path,size)
            lines=_wrap(text,font,language,900)
            widths=[float(font.getlength(line)) for line in lines]
            wraps[language,size]=(lines,widths,len(lines)*sum(font.getmetrics()))
    pairs=sorted(((zh,de) for zh in range(34,29,-1) for de in range(24,19,-1)),
                 key=lambda pair:(sum(pair),pair[0],pair[1]),reverse=True)
    for zh_size,de_size in pairs:
        zh,zh_widths,zh_height=wraps['zh',zh_size]
        de,de_widths,de_height=wraps['de',de_size]
        de_top=520-de_height;zh_bottom=de_top-10;zh_top=zh_bottom-zh_height
        if (len(zh)>3 or len(de)>5 or zh_top<240
                or any(width>900 for width in zh_widths+de_widths)):
            continue
        return {'zh_lines':zh,'de_lines':de,'font_sizes':{'zh':zh_size,'de':de_size},
            'line_counts':{'zh':len(zh),'de':len(de)},
            'line_widths_px':{'zh':zh_widths,'de':de_widths},
            'zh_top_y':zh_top,'zh_bottom_y':zh_bottom,'de_top_y':de_top,'de_bottom_y':520,
            'adjusted':(zh_size,de_size)!=(34,24) or len(zh)>2 or len(de)>4,
            'max_width_px':900,'minimum_top_y':240,'language_gap_px':10,
            'selection_policy':'maximum_font_size_sum_then_chinese_size_v1'}
    raise ValueError('Subtitle cannot fit safe layout (Chinese 30+/3 lines, source 20+/5 lines): '+str(segment_id))


def build_compact_ass(edl, output_path):
    if isinstance(edl,(str,Path)):edl=json.loads(Path(edl).read_text())
    fonts={'zh':ImageFont.truetype(FONT,34),'de':ImageFont.truetype(FONT,24)}
    family=fonts['zh'].getname()[0]
    header=f'[Script Info]\nTitle: Landscape compact bilingual edit\nScriptType: v4.00+\nPlayResX: 960\nPlayResY: 540\nWrapStyle: 2\nScaledBorderAndShadow: yes\nYCbCr Matrix: TV.709\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: Chinese,{family},34,&H00FFFFFF,&H00FFFFFF,&H00101010,&H90000000,0,0,0,0,100,100,0,0,1,2,1,2,30,30,20,1\nStyle: German,{family},24,&H00FFFFFF,&H00FFFFFF,&H00101010,&H90000000,0,0,0,0,100,100,0,0,1,2,1,2,30,30,20,1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
    events=[];placements=[]
    for row in edl['utterances']:
        layout=fit_subtitle_layout(row['text_zh'],row['text_de'],segment_id=row['segment_id'])
        zh,de=layout['zh_lines'],layout['de_lines']
        zh_bottom,zh_top=layout['zh_bottom_y'],layout['zh_top_y']
        for lang,style,lines,bottom in [('zh','Chinese',zh,zh_bottom),('de','German',de,520)]:
            text=r'\N'.join(_escape(line) for line in lines)
            size=(chr(92)+'fs'+str(layout['font_sizes'][lang])) if layout['adjusted'] else ''
            position='{' + chr(92) + f'an2{chr(92)}pos(480,{bottom}){chr(92)}q2' + size + '}'
            events.append(f'Dialogue: 0,{_stamp(row["output_start_s"])},{_stamp(row["output_end_s"])},{style},{row["segment_id"]},0,0,0,,{position}{text}')
        placements.append({'id':row['segment_id'],'zh_lines':len(zh),'de_lines':len(de),
            'zh_top_y':zh_top,'zh_bottom_y':zh_bottom,'de_top_y':layout['de_top_y'],'de_bottom_y':520,
            'font_sizes':layout['font_sizes'],'zh_font_size':layout['font_sizes']['zh'],
            'de_font_size':layout['font_sizes']['de'],'line_widths_px':layout['line_widths_px'],
            'layout_adjusted':layout['adjusted']})
    target=Path(output_path);target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(header+'\n'.join(events)+'\n',encoding='utf-8')
    return {'file':str(target.resolve()),'events':len(events),'segments':len(placements),
            'font_name':family,'font_sizes':{'zh':34,'de':24},'placements':placements,'all_bounds_valid':True}


# Compatibility name for callers prepared before the landscape correction.
build_portrait_ass=build_compact_ass


def _call(command, log, progress_path=None, progress=None, duration_s=None):
    if progress_path is None:
        result=subprocess.run([str(x) for x in command],stdout=log,stderr=log)
        returncode=result.returncode
    else:
        command=[command[0],'-progress','pipe:1','-nostats',*command[1:]]
        process=subprocess.Popen([str(x) for x in command],stdout=subprocess.PIPE,
                                 stderr=log,text=True,bufsize=1)
        last_write=0.;state=dict(progress or {})
        for line in process.stdout:
            key,_,value=line.strip().partition('=')
            if key=='out_time_us':
                if value=='N/A':continue
                state['current_output_time_s']=max(0,float(value)/1e6)
                if duration_s:state['current_fraction']=min(1.,state['current_output_time_s']/duration_s)
            elif key=='speed':state['speed']=value
            elif key=='progress' and (time.monotonic()-last_write>1 or value=='end'):
                state['updated_at_unix']=time.time();_json_write(progress_path,state)
                last_write=time.monotonic()
        returncode=process.wait()
    if returncode:raise RuntimeError('Compact ffmpeg failed; see '+str(log.name))


def _filter_path(path):
    # ffmpeg filter quoting differs from shell quoting. No shell is used here.
    value = str(Path(path).resolve()).replace('\\','\\\\').replace(':','\\:').replace("'",r"'\''")
    return "'"+value+"'"


def render(edl, mixed_wav, output_path, ass_path, logo_path, work_dir):
    """Serialize writes with the optional ready-clip pre-cache worker."""
    work=Path(work_dir);work.mkdir(parents=True,exist_ok=True)
    with (work/'.render.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        return _render_locked(edl,mixed_wav,output_path,ass_path,logo_path,work)


def _render_locked(edl, mixed_wav, output_path, ass_path, logo_path, work_dir):
    if isinstance(edl,(str,Path)):edl=json.loads(Path(edl).read_text())
    if edl['issues']:raise ValueError('Unresolved EDL issues; shorten translations first')
    work=Path(work_dir);work.mkdir(parents=True,exist_ok=True)
    audio=sf.info(mixed_wav)
    if abs(audio.frames/audio.samplerate-edl['duration_s'])>.04:
        raise ValueError('Mixed WAV must follow exact EDL duration')
    logo=Image.open(logo_path).convert('RGBA')
    style=logo_style(logo_path);width=style['width_px']
    logo=logo.resize((width,max(1,round(logo.height*width/logo.width))),Image.Resampling.LANCZOS)
    logo.putalpha(logo.getchannel('A').point(lambda x:round(x*.55)))
    logo_out=work/'compact_logo.png';logo.save(logo_out)
    build_portrait_ass(edl,ass_path)
    source=Path(edl['source_input'])
    clips=[]
    progress_path=work/'render_progress.json'
    progress={'phase':'extracting_clips','clips_total':len(edl['clips']),'clips_completed':0,
              'duration_s':edl['duration_s'],'started_at_unix':time.time()}
    _json_write(progress_path,progress)
    with (work/'compact_render.log').open('a') as log:
        for index,row in enumerate(edl['clips']):
            progress.update({'phase':'extracting_clips','current_clip':index+1,
                             'clip_output_duration_s':row['frames']/FPS,
                             'clips_completed':index,'updated_at_unix':time.time()})
            _json_write(progress_path,progress)
            signature=hashlib.sha256(json.dumps([str(source),row['source_start_s'],row['frames'],row['source_duration_s'],row['freeze_s'],WIDTH,HEIGHT],sort_keys=True).encode()).hexdigest()[:12]
            target=work/f'clip_{index:04}_{signature}.mp4'
            if not target.exists() or not target.with_suffix('.done').exists():
                # Semantic regrouping can shift indices while leaving a cut exact.
                for candidate in work.glob(f'clip_*_{signature}.mp4'):
                    marker=candidate.with_suffix('.done')
                    if candidate!=target and marker.exists():
                        candidate.rename(target);marker.rename(target.with_suffix('.done'))
                        break
            if not target.exists() or not target.with_suffix('.done').exists():
                _call([FFMPEG,'-hide_banner','-loglevel','error','-y','-ss',row['source_start_s'],'-t',row['source_duration_s'],'-i',source,
                       '-an','-vf',f"fps=25,scale=960:540:flags=lanczos,setsar=1,tpad=stop_mode=clone:stop_duration={row['freeze_s']+.08:.6f}",
                       '-frames:v',row['frames'],'-c:v','h264_videotoolbox','-b:v','850k','-pix_fmt','yuv420p',target],
                      log,progress_path,progress,row['frames']/FPS)
                target.with_suffix('.done').write_text('complete\n')
            clips.append(target)
            progress['clips_completed']=index+1
            _json_write(progress_path,progress)
        concat=work/'compact_concat.txt'
        concat.write_text(''.join("file '"+str(p.resolve()).replace("'", "'\\''")+"'\n" for p in clips))
        picture=work/'compact_picture.mp4'
        progress.update({'phase':'concatenating_clips','updated_at_unix':time.time()})
        _json_write(progress_path,progress)
        _call([FFMPEG,'-hide_banner','-loglevel','error','-y','-f','concat','-safe','0','-i',concat,'-an','-c:v','copy',picture],log)
        output=Path(output_path);output.parent.mkdir(parents=True,exist_ok=True)
        # Concat container tails can add gaps; restore the EDL clock by frame index before subtitles.
        filters=f'[0:v:0]settb=1/25,setpts=N,ass=filename={_filter_path(ass_path)}[s];[s][2:v:0]overlay=16:24:shortest=1[v]'
        progress.update({'phase':'burning_subtitles_and_muxing_audio','updated_at_unix':time.time()})
        _json_write(progress_path,progress)
        _call([FFMPEG,'-hide_banner','-loglevel','error','-y','-i',picture,'-i',mixed_wav,'-loop','1','-i',logo_out,
               '-filter_complex',filters,'-map','[v]','-map','1:a:0','-c:v','h264_videotoolbox','-b:v','850k',
               '-r',FPS,'-fps_mode','cfr','-frames:v',edl['frames'],
               '-pix_fmt','yuv420p','-c:a','aac','-b:a','128k','-metadata:s:a:0','language=zho',
               '-disposition:a:0','default','-t',edl['duration_s'],'-movflags','+faststart',output],
              log,progress_path,progress,edl['duration_s'])
    spec=importlib.util.spec_from_file_location('_compact_export_size',Path(__file__).with_name('export_size.py'))
    size_policy=importlib.util.module_from_spec(spec);spec.loader.exec_module(size_policy)
    def size_progress(phase,attempt):
        progress.update(phase=phase,compression_attempt=attempt,updated_at_unix=time.time())
        _json_write(progress_path,progress)
    size_receipt=size_policy.enforce(output,update=size_progress)
    progress.update({'phase':'finished','clips_completed':len(clips),'output':str(output.resolve()),
                     'current_output_time_s':edl['duration_s'],'current_fraction':1.,'updated_at_unix':time.time()})
    _json_write(progress_path,progress)
    return {'output':str(output.resolve()),'duration_s':edl['duration_s'],'frames':edl['frames'],
            'segments':edl['speech_segments'],'logo':style,
            'picture_box':[0,0,960,540],'burned_bilingual_subtitles':True,
            'encoder':size_receipt.get('encoder','h264_videotoolbox'),
            'video_bitrate_target_bps':size_receipt.get('video_bitrate_target_bps',850000),
            'audio_bitrate_target_bps':128000,'size_limit':size_receipt,
            'render_progress':str(progress_path.resolve())}


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--root',type=Path,required=True);p.add_argument('--job',required=True)
    p.add_argument('--pause',type=float,default=.35);p.add_argument('--render',action='store_true')
    p.add_argument('--join-groups',type=Path)
    p.add_argument('--timeline',type=Path)
    p.add_argument('--mixed-wav',type=Path);p.add_argument('--output',type=Path)
    p.add_argument('--logo',type=Path)
    args=p.parse_args();job=args.root/'jobs'/args.job;out=job/'outputs/landscape_compact';out.mkdir(parents=True,exist_ok=True)
    timeline=args.timeline or (job/'timeline.punctuated.json' if (job/'timeline.punctuated.json').exists() else job/'timeline.json')
    edl=build_edl(timeline,out/'edit_decisions.json',args.pause,
                  json.loads(args.join_groups.read_text()) if args.join_groups else None)
    layout=build_portrait_ass(edl,out/'compact_bilingual.ass');_json_write(out/'subtitle_layout.json',layout)
    print(json.dumps({'duration_s':edl['duration_s'],'speech_segments':edl['speech_segments'],'issues':edl['issues'],'events':layout['events']},ensure_ascii=False),flush=True)
    if args.render:
        if not args.mixed_wav:p.error('--render requires --mixed-wav aligned to EDL')
        report=render(edl,args.mixed_wav,args.output or out/'landscape_compact_zh.mp4',out/'compact_bilingual.ass',
                      args.logo or args.root/'assets/philos_club_serif_wordmark.png',out/'render_cache')
        _json_write(out/'compact_export.json',report)


if __name__=='__main__':main()
