"""Resumable full-episode calm dubbing and local export.

Input: reviewed segments.json, acoustic speakers.json, and local processing request.
Natural utterances are cached; timing problems are reported without truncating speech.
"""
import argparse
import contextlib
import gc
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from video import call, confined, parse_loudness, probe, stamp, validate_rows


def write_json(path, data):
    tmp=path.with_suffix(path.suffix+'.partial')
    tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n');tmp.replace(path)


def utterance_cache_key(row, reference_sha256, profile):
    payload={'text':row['text_zh'],'reference_sha256':reference_sha256,'profile':profile}
    if row.get('tts_pronunciation_text'):payload['tts_text_override']=row['tts_pronunciation_text']
    if row.get('tts_preserve_commas') is True:payload['punctuation_mode']='preserve_commas'
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()



def tts_row_with_defaults(row,config):
    if 'tts_preserve_commas' not in row and config.get('tts_punctuation_mode')=='preserve_commas':
        return {**row,'tts_preserve_commas':True}
    return row


def effective_punctuation_mode(row):
    return 'preserve_commas' if row.get('tts_preserve_commas') is True else 'remove_optional_commas'


def tts_input_text(row):
    text=row.get('tts_pronunciation_text',row['text_zh'])
    if row.get('tts_preserve_commas') is True:return text
    return text.replace('，','').replace('、','')


def read_job(root, job):
    source=json.loads((job/'source.json').read_text())
    data=json.loads((job/'segments.json').read_text())
    rows=data['segments'];validate_rows(rows,source['duration_s'])
    permission=json.loads((job/'processing_request.json').read_text())
    if permission.get('processing_authorized') is not True or permission.get('scope')!='private_local_evaluation':
        raise ValueError('LOCAL_PROCESSING_REQUEST_REQUIRED')
    speakers=json.loads((job/'speakers.json').read_text())
    for r in rows:
        if not speakers[r['speaker_id']].get('reviewed'):
            raise ValueError('SPEAKER_SELECTION_UNREVIEWED')
        confined(root,speakers[r['speaker_id']]['reference'])
    return source, rows, speakers


def timing(rows, generated, duration, max_drift=.35):
    timeline=[];issues=[];prior_end=0.
    for i,row in enumerate(rows):
        info=generated.get(row['segment_id'])
        if not info:
            issues.append({'id':row['segment_id'],'reason':'not_generated'});continue
        length=info['duration_s']
        start=max(row['start_s'],prior_end+.03 if timeline else 0)
        end=start+length
        next_start=rows[i+1]['start_s'] if i+1<len(rows) else duration
        drift=start-row['start_s']
        if drift>max_drift or end>next_start+max_drift-.03 or end>duration:
            issues.append({'id':row['segment_id'],'text_zh':row['text_zh'],
                'source_start_s':row['start_s'],'source_end_s':row['end_s'],
                'natural_duration_s':length,'planned_start_s':start,'start_drift_s':drift,
                'available_s':max(.1,next_start-row['start_s']-.05),
                'reason':'shorten_translation_or_review_boundary'})
        timeline.append({**row,'render_start_s':start,'render_end_s':end,'start_drift_s':drift,
                         'audio':info['file'],'duration_s':length,'cache_key':info['cache_key']})
        prior_end=end
    return timeline,issues


def synthesize(args,root,job):
    import numpy as np
    import soundfile as sf
    os.environ.setdefault('HF_HOME',str(root/'models/.hf'))
    os.environ.setdefault('HF_HUB_OFFLINE','1');os.environ.setdefault('TRANSFORMERS_OFFLINE','1')
    os.environ.setdefault('PYTORCH_ENABLE_MPS_FALLBACK','0')
    os.environ.setdefault('PYTORCH_MPS_HIGH_WATERMARK_RATIO','.7');os.environ.setdefault('PYTORCH_MPS_LOW_WATERMARK_RATIO','.5')
    os.environ.setdefault('TOKENIZERS_PARALLELISM','false')
    os.environ.setdefault('NLTK_DATA',str(root/'models/nltk_data'))
    os.environ.setdefault('MPLCONFIGDIR',str(root/'models/.runtime/matplotlib'))
    os.environ.setdefault('MODELSCOPE_CACHE',str(root/'models/.modelscope'))
    os.environ.setdefault('XDG_CACHE_HOME',str(root/'models/.runtime'))
    import torch
    torch.set_num_threads(2)
    source,rows,speakers=read_job(root,job)
    config=json.loads((root/'config/local.json').read_text())
    rows=[tts_row_with_defaults(row,config) for row in rows]
    calm_strength=float(config.get('tts_calm_strength',.35))
    if not 0<=calm_strength<=.8:raise ValueError('INVALID_CALM_STRENGTH')
    profile={'engine':'IndexTTS-2.5','revision':'c39ce5ba981572cb187443877ff559dfb246ce63',
        'speaker_mode':'local acoustic references','calm_vector':[0,0,0,0,0,0,0,calm_strength],
        'emo_alpha':1.,'duration_factor':1.,'num_beams':1,'seed':42,'max_mel_tokens':500,
        'text_preprocessing':('preserve optional commas and list commas by default; explicit row flags override'
            if config.get('tts_punctuation_mode')=='preserve_commas' else
            'remove optional commas and list commas, retain sentence stops'),
        'user_selection':'calm preview selected by user on 2026-10-20'}
    folder=job/'tts/cache';folder.mkdir(parents=True,exist_ok=True)
    generated={};pending=[];refs={}
    for row in rows:
        ref=confined(root,speakers[row['speaker_id']]['reference'])
        refs[row['speaker_id']]=ref
        refsha=hashlib.sha256(ref.read_bytes()).hexdigest()
        key=utterance_cache_key(row,refsha,profile)
        base=folder/f"{row['segment_id']}_{key[:14]}"
        wav=base.with_suffix('.wav');metadata=base.with_suffix('.json')
        if wav.exists() and metadata.exists():
            info=json.loads(metadata.read_text())
            if info.get('cache_key')==key and info.get('sha256')==hashlib.sha256(wav.read_bytes()).hexdigest():
                generated[row['segment_id']]=info;continue
        pending.append((row,key,wav,metadata))
    status={'phase':'synthesizing','total':len(rows),'completed':len(generated),'new_runs':[],
        'profile':profile,'errors':[],'started_at_unix':time.time()}
    write_json(job/'progress.json',status)
    if pending:
        if shutil.disk_usage(root).free<6*2**30:raise ValueError('INSUFFICIENT_DISK')
        if args.device=='mps':
            if not torch.backends.mps.is_available():raise ValueError('MPS_UNAVAILABLE')
            torch.mps.set_per_process_memory_fraction(.7)
        sys.path.insert(0,str(root/'vendor/index-tts'))
        with (job/'logs/model.log').open('a') as model_log,contextlib.redirect_stdout(model_log),contextlib.redirect_stderr(model_log):
            from indextts.infer_v2_5 import IndexTTS2
            model=root/'models/indextts-2.5'
            tts=IndexTTS2(cfg_path=str(model/'config.yaml'),model_dir=str(model),device=args.device,
                use_bf16=False,use_cuda_kernel=False,use_deepspeed=False,use_accel=False,
                use_torch_compile=False,use_qwen_emo=False)
        status['actual_device']=tts.device
        status['submodel_devices']={name:str(next(getattr(tts,name).parameters()).device)
            for name in ['gpt','semantic_model','semantic_codec','s2mel','campplus_model','bigvgan']}
        for row,key,wav,metadata in pending:
            identifier=row['segment_id']
            if shutil.disk_usage(root).free<6*2**30:raise ValueError('INSUFFICIENT_DISK')
            gc.collect()
            if args.device=='mps':torch.mps.empty_cache()
            torch.manual_seed(42);np.random.seed(42)
            partial=wav.with_name(wav.stem+'.partial.wav')
            began=time.perf_counter()
            try:
                with (job/'logs/model.log').open('a') as log,contextlib.redirect_stdout(log),contextlib.redirect_stderr(log),torch.inference_mode():
                    tts.infer(spk_audio_prompt=str(refs[row['speaker_id']]),text=tts_input_text(row),lang='ZH',
                        output_path=str(partial),emo_vector=profile['calm_vector'],emo_alpha=1.,
                        duration_factor=1.,num_beams=1,use_random=False,max_mel_tokens=500)
                if args.device=='mps':torch.mps.synchronize()
                samples,sr=sf.read(partial)
                if not len(samples) or not np.isfinite(samples).all() or np.max(np.abs(samples))<.001:
                    raise ValueError('INVALID_TTS_WAVEFORM')
                length=len(samples)/sr
                if length>18:raise ValueError('UNEXPECTEDLY_LONG_OUTPUT_REVIEW_REQUIRED')
                partial.replace(wav)
                info={'id':identifier,'file':str(wav),'cache_key':key,'sha256':hashlib.sha256(wav.read_bytes()).hexdigest(),
                    'text':row['text_zh'],'sample_rate':sr,'duration_s':length,'inference_s':time.perf_counter()-began,
                    'speaker':row['speaker_id'],'profile':profile,'effective_punctuation_mode':effective_punctuation_mode(row),
                    'quality':'calm style user selected; this sentence unreviewed'}
                write_json(metadata,info);generated[identifier]=info
                status['new_runs'].append({k:info[k] for k in ['id','duration_s','inference_s']})
                status['completed']=len(generated);status['source_position_s']=row['end_s']
                print('GENERATED',identifier,len(generated),'/',len(rows),f"{length:.2f}s",flush=True)
            except Exception as exc:
                status['errors'].append({'id':identifier,'type':type(exc).__name__,'message':str(exc)[-1200:]})
                print('FAILED',identifier,type(exc).__name__,str(exc)[-200:],flush=True)
                if len(status['errors'])>=3:
                    write_json(job/'progress.json',status);raise
            write_json(job/'progress.json',status)
        if args.device=='mps':
            status['mps_end_driver_bytes']=torch.mps.driver_allocated_memory()
    timeline,issues=timing(rows,generated,source['duration_s'])
    write_json(job/'timeline.json',{'segments':timeline,'issues':issues,'duration_s':source['duration_s'],
        'method':'natural duration, preserve source starts, max 0.35s delay, no tempo changes'})
    write_json(job/'timing_issues.json',issues)
    status['phase']='needs_timing_review' if issues else 'ready_to_mix'
    status['timing_issue_count']=len(issues);status['finished_at_unix']=time.time()
    write_json(job/'progress.json',status)
    print('SYNTHESIS_DONE',len(generated),'timing issues',len(issues),flush=True)


def mix(args,root,job):
    import numpy as np
    import soundfile as sf
    source,rows,speakers=read_job(root,job)
    timeline=json.loads((job/'timeline.json').read_text())
    if timeline['issues']:raise ValueError('UNRESOLVED_TIMING_ISSUES')
    sr=48000;frames=round(source['duration_s']*sr)
    dialogue=np.zeros((frames,2),dtype='float32')
    speech_mask=np.zeros(frames,dtype='float32')
    placements=[];out=job/'outputs';out.mkdir(exist_ok=True)
    aligned=job/'tts/aligned';aligned.mkdir(parents=True,exist_ok=True)
    for row in timeline['segments']:
        raw=Path(row['audio']);stereo=aligned/(row['segment_id']+'.wav')
        measurement=call(['ffmpeg','-hide_banner','-i',raw,'-af','loudnorm=I=-19:TP=-3:LRA=11:print_format=json','-f','null','-'])
        level=parse_loudness(measurement.stderr)
        gain=min(-19-float(level['input_i']),-3-float(level['input_tp']))
        call(['ffmpeg','-v','error','-y','-i',raw,'-af',f'volume={gain:.6f}dB','-ac','2','-ar',str(sr),'-c:a','pcm_f32le',stereo])
        audio,rate=sf.read(stereo,dtype='float32',always_2d=True)
        start=round(row['render_start_s']*sr);end=start+len(audio)
        if end>frames:raise ValueError('SPEECH_WOULD_BE_TRUNCATED')
        dialogue[start:end]+=audio
        speech_mask[max(0,start-round(.08*sr)):min(frames,end+round(.12*sr))]=1
        placements.append({'id':row['segment_id'],'start_s':start/sr,'end_s':end/sr,'gain_db':gain,
                           'source_start_s':row['start_s'],'start_drift_s':row['start_drift_s']})
    sf.write(out/'zh_dialogue.wav',dialogue,sr,subtype='PCM_24')
    bgsource=job/'separated/htdemucs/original/no_vocals.wav'
    if not bgsource.is_file():raise ValueError('SEPARATED_BACKGROUND_REQUIRED')
    background_file=job/'audio/background48k.wav'
    call(['ffmpeg','-v','error','-y','-i',bgsource,'-ar',str(sr),'-ac','2','-c:a','pcm_f32le',background_file])
    background,rate=sf.read(background_file,dtype='float32',always_2d=True)
    background=background[:frames]
    if len(background)<frames:background=np.pad(background,((0,frames-len(background)),(0,0)))
    # Smooth a conservative duck, preserving full background between speech and in the titles.
    from scipy.ndimage import uniform_filter1d
    mask=uniform_filter1d(speech_mask,size=round(.12*sr),mode='nearest')
    gain=1-mask*.55
    mixed=dialogue+background*gain[:,None]
    sf.write(out/'premix.wav',mixed,sr,subtype='FLOAT')
    del mixed,background,dialogue,speech_mask,mask,gain
    gc.collect()
    first=call(['ffmpeg','-hide_banner','-i',out/'premix.wav','-af','loudnorm=I=-16:TP=-2:LRA=11:print_format=json','-f','null','-'])
    m=parse_loudness(first.stderr)
    filt=(f"loudnorm=I=-16:TP=-2:LRA=11:linear=true:measured_I={m['input_i']}:"
          f"measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}")
    call(['ffmpeg','-v','error','-y','-i',out/'premix.wav','-af',filt,'-ar',str(sr),'-c:a','pcm_s24le',out/'zh_mix.wav'])
    call(['ffmpeg','-v','error','-y','-i',out/'zh_mix.wav','-c:a','aac','-b:a','192k',out/'zh_mix.m4a'])
    for language in ['zh','de']:
        blocks=[]
        for i,row in enumerate(timeline['segments']):
            start=row['render_start_s'] if language=='zh' else row['start_s']
            end=row['render_end_s'] if language=='zh' else row['end_s']
            blocks.append(f"{i+1}\n{stamp(start)} --> {stamp(end)}\n{row['text_'+language]}")
        (out/f'{language}.srt').write_text('\n\n'.join(blocks)+'\n',encoding='utf-8')
    write_json(job/'mix_report.json',{'placements':placements,'sample_rate':sr,'frames':frames,
        'background_source':str(bgsource),'background_gain_during_speech':.45,'background_gain_elsewhere':1.,
        'speech_resampled_only':True,'tempo_processing':False,'loudness_target_lufs':-16,
        'master_true_peak_target_dbtp':-2,'encoded_true_peak_acceptance_max_dbtp':-1.5,
        'status':'mixed_requires_listening_review'})
    print('MIX_READY',out,flush=True)


def export(args,root,job):
    source,rows,speakers=read_job(root,job);out=job/'outputs'
    font=Path('/System/Library/Fonts/PingFang.ttc')
    if not font.exists():font=Path('/System/Library/Fonts/Supplemental/Arial Unicode.ttf')
    from PIL import Image,ImageDraw,ImageFont
    label=Image.new('RGBA',(292,42),(0,0,0,150))
    ImageDraw.Draw(label).text((9,6),'AI 中文译配 · 本地测试',font=ImageFont.truetype(str(font),22),fill='white')
    label_file=job/'ai_label.png';label.save(label_file)
    output=out/'dubbed_full_zh.mp4'
    command=['ffmpeg','-hide_banner','-y','-i',source['input'],'-i',out/'zh_mix.wav','-i',out/'zh.srt','-i',out/'de.srt',
        '-loop','1','-i',label_file,'-filter_complex','[0:v:0][4:v:0]overlay=24:20:shortest=1[v]',
        '-map','[v]','-map','1:a:0','-map','0:a:0','-map','2:0','-map','3:0',
        '-c:v','h264_videotoolbox','-b:v','2800k','-pix_fmt','yuv420p',
        '-c:a:0','aac','-b:a:0','192k','-c:a:1','copy','-c:s','mov_text',
        '-metadata:s:a:0','language=zho','-metadata:s:a:0','title=AI Chinese dubbing - calm',
        '-metadata:s:a:1','language=deu','-metadata:s:a:1','title=Original German',
        '-metadata:s:s:0','language=zho','-metadata:s:s:1','language=deu',
        '-disposition:a:0','default','-disposition:a:1','0','-disposition:s:0','default','-disposition:s:1','0',
        '-metadata','title=AI Chinese dubbing - private local evaluation','-t',str(source['duration_s']),'-movflags','+faststart',output]
    with (job/'logs/export.log').open('w') as log:
        result=subprocess.run([str(v) for v in command],stdout=log,stderr=log)
    if result.returncode:raise RuntimeError('EXPORT_FAILED: see logs/export.log')
    from export_size import enforce
    size_limit=enforce(output)
    write_json(job/'export_report.json',{'status':'exported_requires_listening_review','probe':probe(output),
        'input_duration_s':source['duration_s'],'ai_label':'AI 中文译配 · 本地测试',
        'encoder':size_limit.get('encoder','h264_videotoolbox'),'size_limit':size_limit})
    print('EXPORTED',output,flush=True)


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['synthesize','mix','export'])
    p.add_argument('--root',type=Path,required=True);p.add_argument('--job',required=True)
    p.add_argument('--device',choices=['mps','cpu','cuda:0'],default='mps')
    args=p.parse_args();root=args.root.resolve();job=confined(root,'jobs/'+args.job)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,60}',args.job):p.error('Invalid job name')
    for folder in ['logs','outputs']: (job/folder).mkdir(exist_ok=True)
    {'synthesize':synthesize,'mix':mix,'export':export}[args.action](args,root,job)


if __name__=='__main__':main()
