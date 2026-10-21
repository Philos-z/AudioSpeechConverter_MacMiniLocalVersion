"""Assemble dialogue and independent background on one speech-led EDL."""
import argparse
import gc
import json
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

from gain_cache import GainCache


def write_json(path,data):
    temporary=path.with_suffix('.partial.json')
    temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n');temporary.replace(path)


def validate_source_duration(sample_count,sample_rate,expected_s,identifier):
    expected=round(expected_s*sample_rate)
    if abs(sample_count-expected)>1:
        raise ValueError(f'AUDIO_DURATION_CHANGED_REBUILD_EDL: {identifier}: '
                         f'{sample_count} source frames != expected {expected} at {sample_rate}Hz')


def main():
    p=argparse.ArgumentParser();p.add_argument('--job',type=Path,required=True)
    p.add_argument('--edl',type=Path,required=True);p.add_argument('--background',type=Path,required=True)
    p.add_argument('--gain-cache',type=Path);p.add_argument('--output-dir',type=Path)
    p.add_argument('--write-gain-cache',type=Path)
    args=p.parse_args();started=time.perf_counter();job=args.job;edl=json.loads(args.edl.read_text())
    sys.path.insert(0,str(job.parent.parent/'scripts'))
    from video import call as media_call,parse_loudness,stamp
    def call(command):
        if str(command[0])=='ffmpeg':
            command=[command[0],'-threads','2','-filter_threads','2',*command[1:]]
        return media_call(command)
    cache=GainCache(args.gain_cache);stage_times={}
    if edl['issues']:raise ValueError('Unresolved edit decisions')
    out=args.output_dir or job/'outputs/landscape_compact';out.mkdir(parents=True,exist_ok=True)
    progress_path=out/'incremental_mix_progress.json'
    sr=48000;frames=edl['frames']*sr//edl['fps']
    dialogue=np.zeros((frames,2),dtype='float32');background=np.zeros_like(dialogue)
    progress={'phase':'aligning_dialogue','completed':0,'total':len(edl['utterances'])}
    placements=[];section=time.perf_counter()
    for i,row in enumerate(edl['utterances']):
        raw=Path(row['audio']);samples,rate=sf.read(raw,dtype='float32',always_2d=True)
        if not np.isfinite(samples).all():raise ValueError('Invalid dialogue')
        validate_source_duration(len(samples),rate,row['speech_s'],row['segment_id'])
        gain,cache_info=cache.get(raw)
        if rate!=sr:
            divisor=np.gcd(rate,sr);samples=resample_poly(samples,sr//divisor,rate//divisor,axis=0).astype('float32')
        if samples.shape[1]==1:samples=np.repeat(samples,2,axis=1)
        target=round(row['speech_s']*sr)
        samples=samples[:target]
        if len(samples)<target:samples=np.pad(samples,((0,target-len(samples)),(0,0)))
        samples*=10**(gain/20)
        # Very short fades preserve all words while avoiding clicks between generated files.
        fade=min(round(.002*sr),len(samples)//2)
        if fade:
            samples[:fade]*=np.linspace(0,1,fade,dtype='float32')[:,None]
            samples[-fade:]*=np.linspace(1,0,fade,dtype='float32')[:,None]
        start=round(row['output_start_s']*sr);end=start+target
        if end>frames:raise ValueError('Speech would be truncated')
        dialogue[start:end]+=samples
        placements.append({'id':row['segment_id'],'start_s':start/sr,'speech_end_s':end/sr,
                           'raw_audio':str(raw),'gain_db':gain,'samples':target,**cache_info})
        progress['completed']=i+1
        write_json(progress_path,progress)
    sf.write(out/'zh_dialogue.wav',dialogue,sr,subtype='PCM_24')
    stage_times['dialogue_alignment_and_write_s']=time.perf_counter()-section;section=time.perf_counter()
    with sf.SoundFile(args.background) as source:
        if source.samplerate!=sr or source.channels!=2:raise ValueError('Expected 48k stereo background')
        for clip in edl['clips']:
            start=round(clip['source_start_s']*sr);length=round(clip['source_duration_s']*sr)
            source.seek(start);samples=source.read(length,dtype='float32',always_2d=True)
            fade=min(round(.01*sr),len(samples)//2)
            if fade:
                samples[:fade]*=np.linspace(0,1,fade,dtype='float32')[:,None]
                tail=min(round((.04 if clip['freeze_s'] else .01)*sr),len(samples)//2)
                samples[-tail:]*=np.linspace(1,0,tail,dtype='float32')[:,None]
            where=round(clip['output_start_s']*sr)
            stop=min(frames,where+len(samples));background[where:stop]+=samples[:stop-where]
    sf.write(out/'background_compact.wav',background,sr,subtype='PCM_24')
    stage_times['background_retiming_and_write_s']=time.perf_counter()-section;section=time.perf_counter()
    dialogue+=background
    sf.write(out/'premix.wav',dialogue,sr,subtype='FLOAT')
    del dialogue,background;gc.collect()
    stage_times['premix_write_s']=time.perf_counter()-section;section=time.perf_counter()
    progress['phase']='mastering';write_json(progress_path,progress)
    result=call(['ffmpeg','-hide_banner','-i',out/'premix.wav','-af','loudnorm=I=-16:TP=-2:LRA=11:print_format=json','-f','null','-'])
    m=parse_loudness(result.stderr)
    filt=(f"loudnorm=I=-16:TP=-2:LRA=11:linear=true:measured_I={m['input_i']}:"
          f"measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}")
    call(['ffmpeg','-v','error','-y','-i',out/'premix.wav','-af',filt,'-ar',str(sr),'-c:a','pcm_s24le',out/'zh_mix.wav'])
    stage_times['mastering_s']=time.perf_counter()-section;section=time.perf_counter()
    call(['ffmpeg','-v','error','-y','-i',out/'zh_mix.wav','-c:a','aac','-b:a','128k',out/'zh_mix.m4a'])
    stage_times['audio_encode_s']=time.perf_counter()-section;section=time.perf_counter()
    for language in ['zh','de']:
        blocks=[f"{i+1}\n{stamp(row['output_start_s'])} --> {stamp(row['output_end_s'])}\n{row['text_'+language]}"
                for i,row in enumerate(edl['utterances'])]
        (out/f'{language}.srt').write_text('\n\n'.join(blocks)+'\n')
    write_json(out/'mix_report.json',{'edl':str(args.edl),'duration_s':edl['duration_s'],'frames':frames,
        'placements':placements,'background_source':str(args.background),'background_retimed_with_video':True,
        'speech_ducking':False,'tempo_processing':False,'generated_chinese_dialogue_truncated':False,
        'loudness_target_lufs':-16,'true_peak_target_dbtp':-2,
        'incremental_gain_cache':cache.stats,'stage_wall_s':stage_times,
        'wall_s':time.perf_counter()-started,'output_isolation':str(out.resolve())})
    progress['phase']='mixed';write_json(progress_path,progress)
    if args.write_gain_cache:cache.save(args.write_gain_cache)
    print('COMPACT_MIX_READY',out,flush=True)


if __name__=='__main__':main()
