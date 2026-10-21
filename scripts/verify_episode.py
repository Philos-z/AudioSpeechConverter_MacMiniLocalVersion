"""Verify complete media exports and real stream data; no subjective audio score."""
import argparse
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import time

import numpy as np
import soundfile as sf

from video import call,parse_loudness,probe


def stream_hash(file,selector):
    return call(['ffmpeg','-v','error','-i',file,'-map',selector,'-c','copy','-f','hash','-hash','sha256','-']).stdout.strip()


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--job',required=True)
    p.add_argument('--variant')
    args=p.parse_args();root=args.root.resolve();job=root/'jobs'/args.job;out=job/'outputs'
    if args.variant:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,60}',args.variant):p.error('Invalid variant')
        out=out/args.variant
    source=json.loads((job/'source.json').read_text())
    timeline=json.loads((job/'timeline.json').read_text())
    mix=json.loads(((out if args.variant else job)/'mix_report.json').read_text())
    report={'source_duration_s':source['duration_s'],'checks':{},'limitations':[
        'Engineering and ASR observations do not score timbre, prosody or subjective likeness.',
        'Acoustic speaker groups are not identity verification; background separation may contain artifacts.',
        'No lip-shape editing or exact cross-language word-level alignment.']}
    expected=source['duration_s'];durations={}
    video=out/'dubbed_full_zh.mp4'
    for name in ['dubbed_full_zh.mp4','zh_mix.m4a','zh_mix.wav','zh_dialogue.wav']:
        path=out/name
        if args.variant and name=='zh_dialogue.wav':path=job/'outputs'/name
        metadata=probe(path);length=float(metadata['format']['duration']);durations[name]=length
        if abs(length-expected)>.08:raise ValueError('DURATION_MISMATCH: '+name)
    report['checks']['duration_s']=durations
    metadata=probe(video)
    audio=[s for s in metadata['streams'] if s['codec_type']=='audio']
    subs=[s for s in metadata['streams'] if s['codec_type']=='subtitle']
    visual=next(s for s in metadata['streams'] if s['codec_type']=='video')
    if (visual['codec_name'],visual['width'],visual['height'])!=('h264',1280,720) or Fraction(visual['avg_frame_rate'])!=25:
        raise ValueError('VIDEO_FORMAT_MISMATCH')
    if len(audio)!=2 or len(subs)!=(0 if args.variant else 2):raise ValueError('STREAM_COUNT_MISMATCH')
    if [s.get('tags',{}).get('language') for s in audio]!=['zho','deu']:raise ValueError('AUDIO_LANGUAGE_MISMATCH')
    if audio[0]['disposition']['default']!=1 or audio[1]['disposition']['default']!=0:raise ValueError('AUDIO_DEFAULT_MISMATCH')
    if [s.get('tags',{}).get('language') for s in subs]!=([] if args.variant else ['zho','deu']):raise ValueError('SUBTITLE_LANGUAGE_MISMATCH')
    expected_defaults=[] if args.variant else [1,0]
    if [s['disposition']['default'] for s in subs]!=expected_defaults:raise ValueError('SUBTITLE_DEFAULT_MISMATCH')
    report['checks']['streams']={'audio_languages':['zho','deu'],'subtitle_languages':[s.get('tags',{}).get('language') for s in subs],
        'video_codec':visual['codec_name'],'width':visual['width'],'height':visual['height'],'frame_rate':visual.get('avg_frame_rate')}
    original_hash=stream_hash(source['input'],'0:a:0');backup_hash=stream_hash(video,'0:a:1')
    if original_hash!=backup_hash:raise ValueError('ORIGINAL_AUDIO_BITSTREAM_CHANGED')
    report['checks']['original_audio_bitstream_sha256']=original_hash
    if timeline['issues']:raise ValueError('TIMING_ISSUES_UNRESOLVED')
    prior=0.;maximum_drift=0.;keys=[]
    for row in timeline['segments']:
        if row['render_start_s']<prior or row['render_end_s']>expected+.001:raise ValueError('OVERLAP_OR_SPEECH_TRUNCATION')
        path=Path(row['audio']);info=json.loads(path.with_suffix('.json').read_text())
        if hashlib.sha256(path.read_bytes()).hexdigest()!=info['sha256']:raise ValueError('AUDIO_CACHE_HASH_MISMATCH')
        if info['profile']['calm_vector']!=[0,0,0,0,0,0,0,.35] or info['profile']['duration_factor']!=1:
            raise ValueError('SELECTED_STYLE_CHANGED')
        prior=row['render_end_s'];maximum_drift=max(maximum_drift,row['start_drift_s']);keys.append(row['cache_key'])
    if maximum_drift>.35:raise ValueError('EXCESSIVE_TIMING_DRIFT')
    source_rows=json.loads((job/'segments.json').read_text())['segments']
    if len(timeline['segments'])!=len(source_rows):raise ValueError('MISSING_RENDERED_SEGMENT')
    if [(r['segment_id'],r['text_zh']) for r in timeline['segments']] != [(r['segment_id'],r['text_zh']) for r in source_rows]:
        raise ValueError('STALE_TRANSLATION_TIMELINE')
    report['checks']['speech_placement']={'segments':len(keys),'max_start_delay_s':maximum_drift,'overlaps':0,'truncated_segments':0,'tempo_processing':False}
    for language in ['zh','de']:
        text=(out/f'{language}.srt').read_text()
        if text.count(' --> ')!=len(keys):raise ValueError('SUBTITLE_COUNT_MISMATCH')
    report['checks']['subtitles_per_language']=len(keys)
    for name,selector in [('dubbed_full_zh.mp4','0:a:0'),('zh_mix.m4a','0:a:0')]:
        result=call(['ffmpeg','-hide_banner','-i',out/name,'-map',selector,'-af','loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json','-f','null','-'])
        level=parse_loudness(result.stderr)
        integrated=float(level['input_i']);peak=float(level['input_tp'])
        if abs(integrated+16)>1 or peak>-1.5:raise ValueError(f'ENCODED_LEVEL_OUT_OF_RANGE: {name} {integrated}LUFS {peak}dBTP')
        report['checks'][name+'_levels']={'integrated_lufs':integrated,'true_peak_dbtp':peak,'lra_lu':float(level['input_lra'])}
    # A full decode checks all packets, including the preserved original track.
    decoded=call(['ffmpeg','-v','error','-i',video,'-map','0:v:0','-map','0:a:0','-map','0:a:1','-f','null','-'])
    if decoded.stderr.strip():raise ValueError('DECODE_DIAGNOSTIC: '+decoded.stderr[-1000:])
    report['checks']['full_decode']='passed: video and both audio tracks'
    # Check waveform integrity in three distinct regions of the master.
    peaks=[]
    with sf.SoundFile(out/'zh_mix.wav') as wav:
        for at in [25,expected/2,max(0,expected-30)]:
            wav.seek(round(at*wav.samplerate));chunk=wav.read(10*wav.samplerate,dtype='float32',always_2d=True)
            if not np.isfinite(chunk).all() or np.max(np.abs(chunk))>=1:raise ValueError('INVALID_MASTER_AUDIO')
            peaks.append({'at_s':at,'peak':float(np.max(np.abs(chunk))),'rms':float(np.sqrt(np.mean(chunk**2)))})
    report['checks']['sample_regions']=peaks
    observations=json.loads((job/'audio_qa_observations.json').read_text())
    missing=[key for key in keys if key not in observations]
    if missing:raise ValueError('CONTENT_QA_INCOMPLETE: '+str(len(missing)))
    report['checks']['asr_content_observations']={'covered':len(keys),'missing':0,'not_a_human_voice_quality_test':True}
    if args.variant:
        layout=json.loads((out/'subtitle_layout.json').read_text())
        report['checks']['bilingual_subtitles']={'burned_into_picture':True,'layout':layout,
            'soft_subtitle_tracks':0,'standalone_subtitle_files':['zh.srt','de.srt'],
            'visual_rendering_review_required':True}
        report['checks']['background_remix']=mix
    report['status']='engineering_checks_passed_full_listening_review_remaining'
    report['verified_at_unix']=time.time()
    ((out if args.variant else job)/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':main()
