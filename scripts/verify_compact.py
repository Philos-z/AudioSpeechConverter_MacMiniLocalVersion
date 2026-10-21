"""Validate compact edit geometry, exact EDL coverage, streams and encoded levels.

No speech model is loaded. A visual/listening review remains separate from these
engineering checks. Original audio bitstream identity is not expected after cuts.
"""
import argparse
from collections import Counter
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import time

import soundfile as sf


FFMPEG='/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg'
FFPROBE='/opt/homebrew/opt/ffmpeg-full/bin/ffprobe'


def _call(args):
    result=subprocess.run([str(x) for x in args],capture_output=True,text=True)
    if result.returncode:raise ValueError('MEDIA_CHECK_FAILED: '+result.stderr[-2000:])
    return result


def _assert(condition,message):
    if not condition:raise ValueError(message)


def _stamp(value):
    hours,minutes,seconds=value.split(':')
    return int(hours)*3600+int(minutes)*60+float(seconds)


def verify(edl_path,video_path,ass_path,mixed_wav=None,export_report=None,
           output_path=None,expected_segments=None,skip_levels=False):
    edl=json.loads(Path(edl_path).read_text());video=Path(video_path)
    report={'video':str(video.resolve()),'edl':str(Path(edl_path).resolve()),'checks':{},
            'limitations':['Engineering checks do not score naturalness, voice likeness, emotion, or semantic translation quality.',
                           'The original German track is cut or omitted; full-source bitstream hash equality is not applicable.',
                           'Burned subtitle and logo appearance require frame review; ASS and export metadata alone are not a visual proof.']}
    checks=report['checks'];clips=edl['clips'];utterances=edl['utterances']
    source_timeline=json.loads(Path(edl['source_timeline']).read_text())['segments']
    expected=expected_segments if expected_segments is not None else len(source_timeline)
    identifiers=[r['segment_id'] for r in utterances]
    _assert(not edl['issues'],'UNRESOLVED_EDL_ISSUES')
    _assert(len(identifiers)==expected and len(set(identifiers))==expected,'MISSING_OR_DUPLICATE_UTTERANCE')
    _assert(expected==edl['speech_segments'],'SPEECH_SEGMENT_COUNT_MISMATCH')
    lookup={r['segment_id']:r for r in source_timeline}
    maximum_freeze=0.;total_frames=0;previous_source_end=0.
    previous_audio_end=0.
    for index,clip in enumerate(clips):
        start,end=clip['output_start_s'],clip['output_end_s']
        _assert(abs(start-total_frames/25)<1e-7,'NONCONTIGUOUS_CLIP_TIMELINE')
        _assert(abs((end-start)-clip['frames']/25)<1e-7,'CLIP_FRAME_DURATION_MISMATCH')
        _assert(clip['source_start_s']>=previous_source_end-1e-6,'OVERLAPPING_SOURCE_CUTS')
        _assert(0<=clip['source_start_s']<clip['source_end_s']<=edl['source_duration_s']+1e-6,'INVALID_SOURCE_CUT')
        _assert(abs(clip['source_duration_s']-(clip['source_end_s']-clip['source_start_s']))<1e-6,'SOURCE_DURATION_MISMATCH')
        _assert(abs(clip['source_duration_s']+clip['freeze_s']-(end-start))<1e-6,'FREEZE_DURATION_MISMATCH')
        if clip['kind']=='speech':
            _assert(clip['pause_s']>=.35-1e-7,'INSUFFICIENT_END_PAUSE')
            _assert(clip['speech_s']-clip['source_duration_s']<=.35+1e-6,'SPEECH_EXCEEDS_FREEZE_TOLERANCE')
            group=[r for r in utterances if r['clip_index']==index]
            _assert([r['segment_id'] for r in group]==clip['segment_ids'],'JOIN_GROUP_COVERAGE_MISMATCH')
            audio_sum=sum(r['speech_s'] for r in group)
            gap_sum=sum(r.get('punctuation_gap_after_s',r.get('comma_gap_after_s',0.)) for r in group)
            comma_sum=sum(r.get('comma_gap_after_s',0.) for r in group)
            _assert(abs(audio_sum+gap_sum-clip['speech_s'])<1e-6,'JOIN_GROUP_DURATION_MISMATCH')
            _assert(abs(clip.get('speech_audio_s',audio_sum)-audio_sum)<1e-6,'JOIN_GROUP_AUDIO_DURATION_MISMATCH')
            _assert(abs(clip.get('internal_comma_gaps_s',comma_sum)-comma_sum)<1e-6,'JOIN_GROUP_COMMA_GAP_MISMATCH')
            _assert(abs(clip.get('internal_punctuation_gaps_s',gap_sum)-gap_sum)<1e-6,'JOIN_GROUP_PUNCTUATION_GAP_MISMATCH')
            for n,row in enumerate(group):
                gap=row.get('punctuation_gap_after_s',row.get('comma_gap_after_s',0.))
                expected_gap=0.;target=0.;punctuation=''
                if n+1<len(group):
                    match=re.search(r'''([，,：:；;。.!！？?])[”’"'）)\]】》」』\s]*$''',row['text_zh'].rstrip())
                    punctuation=match.group(1) if match else ''
                    if punctuation in ('，',',','：',':','；',';'):target=.18
                    elif punctuation in ('。','.','!','！','?','？'):target=.35
                    expected_gap=max(0.,target-float(row.get('edge_quiet_tail_s',0.))
                                     -float(group[n+1].get('edge_quiet_head_s',0.)))
                _assert(abs(gap-expected_gap)<1e-6,'CROSS_ROW_PUNCTUATION_GAP_INCORRECT')
                if 'boundary_pause_target_s' in row:
                    _assert(row['boundary_punctuation']==punctuation and abs(row['boundary_pause_target_s']-target)<1e-6,
                            'CROSS_ROW_BOUNDARY_PUNCTUATION_MISMATCH')
                if n+1<len(group):
                    _assert(abs(group[n+1]['output_start_s']-row['output_speech_end_s']-gap)<1e-6,
                            'CROSS_ROW_AUDIO_GAP_PLACEMENT_INCORRECT')
        maximum_freeze=max(maximum_freeze,clip['freeze_s'])
        previous_source_end=clip['source_end_s'];total_frames+=clip['frames']
    for row in utterances:
        clip=clips[row['clip_index']]
        _assert(row['output_start_s']>=previous_audio_end-1e-6,'OVERLAPPING_CHINESE_SPEECH')
        _assert(row['output_start_s']>=clip['output_start_s']-1e-6 and row['output_speech_end_s']<=clip['output_end_s']+1e-6,
                'SPEECH_TRUNCATION_AT_CLIP_BOUNDARY')
        _assert(row['output_end_s']>=row['output_speech_end_s']-1e-6 and row['output_end_s']<=clip['output_end_s']+1e-6,
                'INVALID_SUBTITLE_END')
        audio=sf.info(row['audio'])
        _assert(abs(audio.frames/audio.samplerate-row['speech_s'])<1e-6,'STALE_AUDIO_DURATION')
        if row.get('audio_sha256'):
            _assert(hashlib.sha256(Path(row['audio']).read_bytes()).hexdigest()==row['audio_sha256'],
                    'FINAL_AUDIO_SHA256_MISMATCH')
        _assert(abs(row['output_speech_end_s']-row['output_start_s']-row['speech_s'])<1e-6,'INCORRECT_AUDIO_PLACEMENT')
        _assert(row['segment_id'] in lookup and all(row['text_'+lang]==lookup[row['segment_id']]['text_'+lang] for lang in ['zh','de']),
                'SUBTITLE_TEXT_CHANGED_FROM_SOURCE_TIMELINE')
        previous_audio_end=row['output_speech_end_s']
    _assert(total_frames==edl['frames'] and abs(total_frames/25-edl['duration_s'])<1e-6,'EDL_TOTAL_FRAME_MISMATCH')
    checks['edl']={'speech_segments':len(identifiers),'clips':len(clips),'frames':total_frames,
                   'duration_s':edl['duration_s'],'max_freeze_s':maximum_freeze,
                   'overlaps':0,'truncated_speech':0,'join_groups':edl['join_groups']}
    ass=Path(ass_path).read_text(encoding='utf-8')
    _assert('PlayResX: 960' in ass and 'PlayResY: 540' in ass,'ASS_RESOLUTION_MISMATCH')
    _assert('WrapStyle: 2' in ass,'ASS_WRAP_STYLE_MISMATCH')
    _assert(not any(ord(c)<32 and c not in '\n\r\t' for c in ass),'ASS_CONTROL_CHARACTER_CORRUPTION')
    events=[]
    for line in ass.splitlines():
        if not line.startswith('Dialogue:'):continue
        fields=line.split(',',9)
        _assert(len(fields)==10,'MALFORMED_ASS_EVENT')
        events.append({'start':_stamp(fields[1]),'end':_stamp(fields[2]),'style':fields[3],
                       'id':fields[4],'text':fields[9]})
    _assert(len(events)==2*expected,'MISSING_BILINGUAL_EVENTS')
    counts=Counter((e['id'],e['style']) for e in events)
    for row in utterances:
        for style in ['Chinese','German']:
            _assert(counts[(row['segment_id'],style)]==1,'DUPLICATE_OR_MISSING_SUBTITLE_LANGUAGE')
            event=next(e for e in events if e['id']==row['segment_id'] and e['style']==style)
            _assert(abs(event['start']-row['output_start_s'])<=.006 and abs(event['end']-row['output_end_s'])<=.006,
                    'SUBTITLE_TIME_REMAP_MISMATCH')
            _assert(event['end']<=edl['duration_s']+.006 and event['end']>event['start'],'INVALID_SUBTITLE_TIME')
            _assert(chr(92)+'an2' in event['text'] and chr(92)+'pos(' in event['text'],'MISSING_EXPLICIT_SUBTITLE_POSITION')
            line_limit=2 if style=='Chinese' else 4
            _assert(event['text'].count(chr(92)+'N')+1<=line_limit,'SUBTITLE_LINE_COUNT_EXCEEDED')
    checks['subtitles']={'events':len(events),'per_language':expected,'aligned_to_edited_audio':True}
    metadata=json.loads(_call([FFPROBE,'-v','error','-show_streams','-show_format','-of','json',video]).stdout)
    visual=[s for s in metadata['streams'] if s['codec_type']=='video']
    audio=[s for s in metadata['streams'] if s['codec_type']=='audio']
    _assert(len(visual)==1 and len(audio)>=1,'UNEXPECTED_MEDIA_STREAMS')
    v=visual[0];a=audio[0]
    _assert(v['codec_name']=='h264' and (v['width'],v['height'])==(960,540),'VIDEO_FORMAT_MISMATCH')
    _assert(Fraction(v['avg_frame_rate'])==25,'VIDEO_FRAME_RATE_MISMATCH')
    _assert(a['codec_name']=='aac' and a.get('tags',{}).get('language')=='zho' and a['disposition']['default']==1,
            'CHINESE_DEFAULT_AUDIO_MISMATCH')
    _assert(abs(float(metadata['format']['duration'])-edl['duration_s'])<=.08,'ENCODED_DURATION_MISMATCH')
    if v.get('nb_frames','N/A')!='N/A':_assert(int(v['nb_frames'])==total_frames,'ENCODED_FRAME_COUNT_MISMATCH')
    checks['media']={'width':v['width'],'height':v['height'],'fps':str(v['avg_frame_rate']),
                     'frames':int(v['nb_frames']) if v.get('nb_frames','N/A')!='N/A' else None,
                     'duration_s':float(metadata['format']['duration']),'bytes':video.stat().st_size,
                     'video_bitrate_bps':v.get('bit_rate'),'audio_bitrate_bps':a.get('bit_rate')}
    _assert(video.stat().st_size<50_000_000,'VIDEO_EXCEEDS_50_DECIMAL_MB')
    checks['media']['max_bytes_exclusive']=50_000_000
    if mixed_wav:
        wav=sf.info(mixed_wav)
        _assert(abs(wav.frames/wav.samplerate-edl['duration_s'])<.001,'MASTER_AUDIO_DURATION_MISMATCH')
        checks['master_audio']={'sample_rate':wav.samplerate,'channels':wav.channels,'duration_s':wav.frames/wav.samplerate}
    if export_report:
        export=json.loads(Path(export_report).read_text())
        _assert(export['burned_bilingual_subtitles'] is True,'SUBTITLES_NOT_BURNED_IN_EXPORT_SETTINGS')
        expected_width=144 if export['logo'].get('brand')=='Philos.club' else 84
        _assert(export['logo']['width_px']==expected_width and abs(export['logo']['opacity']-.55)<1e-6,'LOGO_SETTINGS_MISMATCH')
        checks['export_settings']=export
    if not skip_levels:
        measurement=_call([FFMPEG,'-hide_banner','-threads','2','-i',video,'-map','0:a:0',
                           '-af','loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json','-f','null','-'])
        start=measurement.stderr.rfind('{')
        _assert(start>=0,'LOUDNESS_MEASUREMENT_MISSING')
        level=json.JSONDecoder().raw_decode(measurement.stderr[start:])[0]
        integrated=float(level['input_i']);peak=float(level['input_tp'])
        _assert(math.isfinite(integrated) and math.isfinite(peak),'INVALID_ENCODED_LEVELS')
        _assert(abs(integrated+16)<=1 and peak<=-1.5,'ENCODED_LEVELS_OUT_OF_RANGE')
        checks['encoded_levels']={'integrated_lufs':integrated,'true_peak_dbtp':peak,'lra_lu':float(level['input_lra'])}
    else:checks['encoded_levels']='not measured (probe mode)'
    decoded=_call([FFMPEG,'-v','error','-threads','2','-i',video,'-map','0:v:0','-map','0:a','-f','null','-'])
    _assert(not decoded.stderr.strip(),'FULL_DECODE_DIAGNOSTIC: '+decoded.stderr[-1000:])
    checks['full_decode']='video and all audio streams passed'
    report['status']='engineering_checks_passed_visual_and_listening_review_remaining'
    report['verified_at_unix']=time.time();report['video_sha256']=hashlib.sha256(video.read_bytes()).hexdigest()
    if output_path:
        target=Path(output_path);target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    return report


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--edl',type=Path,required=True);p.add_argument('--video',type=Path,required=True)
    p.add_argument('--ass',type=Path,required=True);p.add_argument('--mixed-wav',type=Path)
    p.add_argument('--export-report',type=Path);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--expected-segments',type=int);p.add_argument('--skip-levels',action='store_true')
    args=p.parse_args()
    r=verify(args.edl,args.video,args.ass,args.mixed_wav,args.export_report,args.output,args.expected_segments,args.skip_levels)
    print(json.dumps({'status':r['status'],'segments':r['checks']['edl']['speech_segments'],
                      'duration_s':r['checks']['media']['duration_s'],'levels':r['checks']['encoded_levels'],
                      'report':str(args.output.resolve())},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
