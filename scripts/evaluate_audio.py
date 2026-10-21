"""Offline CPU-small-ASR text and waveform observations, not a listening score.

Accepts benchmark_tts report.json and continuous_tts manifest.json. Import and
--plan-only do not load an ASR model. No input audio or manifest is modified.
"""
import argparse
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import sys
import time

SCHEMA='audio_observation_v1'
LIMITATIONS=('ASR does not validate timbre similarity, emotion, naturalness, diction effort, or subjective listening quality. '
             'Character errors, missing spans and repetition flags are recognition observations, not proof of synthesis errors. '
             'Low-energy intervals can include breaths or soft speech; sample peak is not reconstructed true peak.')


def save(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(path.suffix+'.partial')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');temporary.replace(path)


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def expand_phoneme_labels(text):
    """Compare spoken Chinese labels, never literal pinyin-control syntax."""
    return re.sub(r'<([^<>|]+)\|[^<>]*>',lambda match:match.group(1),text)


def expected_text(row):
    content=row.get('content') or {}
    value=(row.get('text_zh') or content.get('text_zh') or row.get('raw_source_text')
           or row.get('text') or content.get('effective_tts_text'))
    if not isinstance(value,str) or not value.strip():raise ValueError('Missing expected text')
    return expand_phoneme_labels(value)


def entries_from(path):
    path=Path(path).resolve();data=json.loads(path.read_text())
    rows=data if isinstance(data,list) else data.get('results',data.get('utterances'))
    if rows is None:raise ValueError('Expected benchmark results or continuous utterances: '+str(path))
    output=[]
    for index,row in enumerate(rows):
        record={'source_manifest':str(path),'name':row.get('name') or row.get('segment_id') or row.get('id') or str(index),
                'source_index':index,'source_error':row.get('error')}
        try:record['expected']=expected_text(row)
        except ValueError as error:record['input_error']=str(error)
        audio=row.get('audio') or row.get('file') or row.get('output_file')
        if audio:
            audio=Path(audio)
            if not audio.is_absolute():audio=path.parent/audio
            record['audio']=str(audio.resolve())
        else:record['input_error']='Missing audio path (planned tasks are not generated audio)'
        record['cache_key']=row.get('cache_key')
        record['settings']={key:row[key] for key in ('steps','emotion','reference','status','experimental_preview') if key in row}
        output.append(record)
    return output


def levenshtein_trace(expected,recognized):
    matrix=[[0]*(len(recognized)+1) for _ in range(len(expected)+1)]
    for i in range(len(expected)+1):matrix[i][0]=i
    for j in range(len(recognized)+1):matrix[0][j]=j
    for i,x in enumerate(expected,1):
        for j,y in enumerate(recognized,1):
            matrix[i][j]=min(matrix[i-1][j]+1,matrix[i][j-1]+1,matrix[i-1][j-1]+(x!=y))
    i,j=len(expected),len(recognized);operations=[]
    while i or j:
        if i and j and matrix[i][j]==matrix[i-1][j-1]+(expected[i-1]!=recognized[j-1]):
            if expected[i-1]!=recognized[j-1]:operations.append({'operation':'substitution','expected_index':i-1,
                'recognized_index':j-1,'expected':expected[i-1],'recognized':recognized[j-1]})
            i-=1;j-=1
        elif i and matrix[i][j]==matrix[i-1][j]+1:
            operations.append({'operation':'deletion','expected_index':i-1,'recognized_index':j,'expected':expected[i-1],'recognized':''});i-=1
        else:
            operations.append({'operation':'insertion','expected_index':i,'recognized_index':j-1,'expected':'','recognized':recognized[j-1]});j-=1
    operations.reverse()
    for item in operations:
        a,b=item['expected_index'],item['recognized_index']
        item['expected_context']=expected[max(0,a-4):min(len(expected),a+5)]
        item['recognized_context']=recognized[max(0,b-4):min(len(recognized),b+5)]
    return {'distance':matrix[-1][-1],'cer':matrix[-1][-1]/max(1,len(expected)),
        'substitutions':sum(x['operation']=='substitution' for x in operations),
        'deletions':sum(x['operation']=='deletion' for x in operations),
        'insertions':sum(x['operation']=='insertion' for x in operations),'operations':operations}


def repeated_spans(expected,recognized):
    found=set()
    for length in range(4,min(12,len(recognized)//2)+1):
        for start in range(len(recognized)-length+1):
            text=recognized[start:start+length]
            if recognized.count(text)>max(1,expected.count(text)):found.add(text)
    return sorted([text for text in found if not any(text!=other and text in other for other in found)],key=lambda x:(-len(x),x))


def import_local_helpers(root):
    """Only import already installed packages; append, never replace env paths."""
    previous=list(sys.path)
    try:
        sys.path.extend(str(path) for path in sorted((root/'vendor/index-tts/.venv/lib').glob('python*/site-packages')))
        import numpy as np
        import soundfile as sf
        from opencc import OpenCC
        import cn2an
    finally:sys.path[:]=previous
    cc=OpenCC('t2s')
    def normalize(text):
        text=cc.convert(expand_phoneme_labels(text))
        try:
            text=re.sub(r'\d+(?:\.\d+)?[十百千万亿]+',lambda m:str(cn2an.cn2an(m.group(),'smart')),text)
            text=cn2an.transform(text,'cn2an')
        except (ValueError,KeyError):pass
        return re.sub(r'[^\u3400-\u9fff0-9a-z]','',text.lower())
    return np,sf,normalize


def waveform_metrics(path,np,sf,threshold_db=-40.,minimum_s=.08):
    audio,sr=sf.read(path,dtype='float32',always_2d=True)
    if not len(audio):raise ValueError('Empty audio')
    if not np.isfinite(audio).all():raise ValueError('Non-finite waveform')
    def db(value):return 20*math.log10(max(float(value),1e-12))
    absolute=np.abs(audio);peak=float(absolute.max());rms=float(np.sqrt(np.mean(audio.astype(np.float64)**2)))
    frame=max(1,round(.020*sr));hop=max(1,round(.010*sr));threshold=10**(threshold_db/20)
    intervals=[];start=None;last_end=0.
    for position in range(0,len(audio),hop):
        block=audio[position:min(len(audio),position+frame)]
        energy=float(np.sqrt(np.mean(block.astype(np.float64)**2)))
        if energy<=threshold:
            if start is None:start=position/sr
            last_end=min(len(audio),position+frame)/sr
        elif start is not None:
            if last_end-start>=minimum_s:intervals.append({'start_s':start,'end_s':last_end,'duration_s':last_end-start})
            start=None
    if start is not None and last_end-start>=minimum_s:intervals.append({'start_s':start,'end_s':last_end,'duration_s':last_end-start})
    duration=len(audio)/sr
    return {'sample_rate':sr,'channels':audio.shape[1],'frames':len(audio),'duration_s':duration,
        'finite':True,'sample_peak':peak,'sample_peak_dbfs':db(peak),'rms_dbfs':db(rms),
        'samples_at_or_above_0_9999':int(np.sum(absolute>=.9999)),
        'clipping_sample_fraction':float(np.mean(absolute>=.9999)),
        'near_fullscale_sample_fraction':float(np.mean(absolute>=.9999)),
        'legacy_clipping_sample_fraction_note':'Threshold-count only; not proof of PCM hard clipping or audible distortion.',
        'low_energy_threshold_dbfs':threshold_db,'low_energy_frame_ms':20,'low_energy_hop_ms':10,
        'minimum_reported_low_energy_s':minimum_s,'low_energy_intervals':intervals,
        'low_energy_total_s':sum(x['duration_s'] for x in intervals),
        'low_energy_longest_s':max((x['duration_s'] for x in intervals),default=0.),
        'leading_low_energy_s':intervals[0]['duration_s'] if intervals and intervals[0]['start_s']==0 else 0.,
        'trailing_low_energy_s':intervals[-1]['duration_s'] if intervals and intervals[-1]['end_s']>=duration-.001 else 0.,
        'interpretation':'Energy-only pause proxy, not phoneme alignment or a perceptual judgement.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True);parser.add_argument('--input',type=Path,action='append',required=True)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--plan-only',action='store_true')
    parser.add_argument('--cache-dir',type=Path);parser.add_argument('--low-energy-db',type=float,default=-40.)
    parser.add_argument('--minimum-pause',type=float,default=.08)
    args=parser.parse_args();root=args.root.resolve();records=[]
    for source in args.input:records.extend(entries_from(source))
    report={'schema':SCHEMA,'limitations':LIMITATIONS,'inputs':[str(p.resolve()) for p in args.input],
        'asr_model_loaded':False,'gpu_used':False,'model_device':'cpu','compute_type':'int8','cpu_threads':2,
        'method':'Traditional→simplified Chinese; cn2an number normalization; strip punctuation; Levenshtein character alignment.',
        'planned_records':len(records),'rows':[],'created_at_unix':time.time()}
    if args.plan_only:
        report.update(phase='plan_only',rows=records);save(args.output,report)
        print(json.dumps({'phase':'plan_only','records':len(records),'asr_model_loaded':False},ensure_ascii=False));return
    np,sf,normalize=import_local_helpers(root)
    config=json.loads((root/'config/local.json').read_text());model_path=Path(config['asr_model']).resolve()
    if not model_path.is_dir():raise ValueError('Local ASR model missing; downloads forbidden')
    os.environ['HF_HUB_OFFLINE']='1';os.environ['TRANSFORMERS_OFFLINE']='1'
    from faster_whisper import WhisperModel
    versions={name:importlib.metadata.version(name) for name in ['faster-whisper','ctranslate2']}
    model=None;cache=args.cache_dir or args.output.parent/'asr_evaluation_cache';cache.mkdir(parents=True,exist_ok=True)
    asr_profile={'model':str(model_path),'device':'cpu','compute_type':'int8','beam_size':3,
                 'language':'zh','condition_on_previous_text':False,'vad_filter':False,'versions':versions}
    report['asr_profile']=asr_profile;began=time.monotonic()
    for record in records:
        row=dict(record)
        if record.get('source_error') or record.get('input_error'):
            row.update(status='source_failed_or_incomplete');report['rows'].append(row);save(args.output,report);continue
        try:
            audio=Path(record['audio'])
            if not audio.is_file():raise ValueError('Audio file missing')
            sha=digest(audio);row['sha256']=sha
            metrics=waveform_metrics(audio,np,sf,args.low_energy_db,args.minimum_pause)
            signature=hashlib.sha256(json.dumps({'audio_sha256':sha,'asr_profile':asr_profile},sort_keys=True).encode()).hexdigest()
            cached=cache/(signature+'.json')
            if cached.exists():observation=json.loads(cached.read_text());row['asr_cache_hit']=True
            else:
                if model is None:
                    model=WhisperModel(str(model_path),device='cpu',compute_type='int8',cpu_threads=2,num_workers=1,local_files_only=True)
                    report['asr_model_loaded']=True
                started=time.monotonic();segments,_=model.transcribe(str(audio),language='zh',beam_size=3,condition_on_previous_text=False,vad_filter=False)
                decoded=[{'start_s':segment.start,'end_s':segment.end,'text':segment.text} for segment in segments]
                observation={'signature':signature,'recognized':''.join(segment['text'] for segment in decoded).strip(),
                             'segments':decoded,'elapsed_s':time.monotonic()-started}
                save(cached,observation);row['asr_cache_hit']=False
            expected=record['expected'];recognized=observation['recognized'];a,b=normalize(expected),normalize(recognized)
            differences=levenshtein_trace(a,b);repeats=repeated_spans(a,b)
            numbers_a,numbers_b=re.findall(r'\d+(?:\.\d+)?',a),re.findall(r'\d+(?:\.\d+)?',b)
            flags=[]
            if differences['deletions']:flags.append('ASR_missing_character_candidates')
            if repeats:flags.append('ASR_repetition_candidates')
            if numbers_a!=numbers_b:flags.append('ASR_numeric_difference_may_include_formatting')
            if differences['cer']>.25:flags.append('large_ASR_difference_requires_manual_review')
            row.update(status='observed',recognized=recognized,normalized_expected=a,normalized_recognized=b,
                comparison=differences,expected_number_strings=numbers_a,recognized_number_strings=numbers_b,
                repeated_span_candidates=repeats,review_flags=flags,waveform=metrics,asr_segments=observation['segments'],
                asr_inference_seconds=observation['elapsed_s'])
            print(json.dumps({'name':row['name'],'cer':round(differences['cer'],4),'expected':expected,'recognized':recognized,'flags':flags},ensure_ascii=False),flush=True)
        except Exception as error:row.update(status='evaluation_error',error={'type':type(error).__name__,'message':str(error)})
        report['rows'].append(row);report['elapsed_s']=time.monotonic()-began;save(args.output,report)
    observed=[row for row in report['rows'] if row['status']=='observed']
    denominator=sum(len(row['normalized_expected']) for row in observed)
    report.update(phase='complete',observed_count=len(observed),error_or_skipped_count=len(records)-len(observed),
        aggregate_cer=sum(row['comparison']['distance'] for row in observed)/max(1,denominator),
        elapsed_s=time.monotonic()-began,subjective_quality_verified=False)
    save(args.output,report)
    print(json.dumps({'phase':'complete','observed':len(observed),'skipped_or_errors':len(records)-len(observed),
                     'aggregate_cer':report['aggregate_cer'],'asr_model_loaded':report['asr_model_loaded']},ensure_ascii=False))

if __name__=='__main__':main()
