"""Remix existing dialogue and separated background; never regenerates voices."""
import argparse
import gc
import json
import math
from pathlib import Path
import re
import shutil
import time

import numpy as np
import soundfile as sf

from video import call, confined, parse_loudness


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--root',type=Path,required=True);p.add_argument('--job',required=True)
    p.add_argument('--variant',default='bg_bilingual_v2')
    p.add_argument('--background-db',type=float,default=4.)
    p.add_argument('--adaptive-background',action='store_true')
    p.add_argument('--title-background-db',type=float)
    args=p.parse_args();root=args.root.resolve()
    if not all(re.fullmatch(r'[A-Za-z0-9_-]{1,60}',s) for s in [args.job,args.variant]):p.error('Invalid task/variant')
    if not -12<=args.background_db<=12:p.error('Background gain out of range')
    if args.title_background_db is not None and not -12<=args.title_background_db<=12:p.error('Title background gain out of range')
    job=confined(root,'jobs/'+args.job);base=job/'outputs';out=base/args.variant;out.mkdir(exist_ok=True)
    source=json.loads((job/'source.json').read_text());sr=48000;frames=round(source['duration_s']*sr)
    dialogue,rate=sf.read(base/'zh_dialogue.wav',dtype='float32',always_2d=True)
    background,bgrate=sf.read(job/'audio/background48k.wav',dtype='float32',always_2d=True)
    if rate!=sr or bgrate!=sr or dialogue.shape!=(frames,2):raise ValueError('Unexpected waveform dimensions')
    background=background[:frames]
    if len(background)<frames:background=np.pad(background,((0,frames-len(background)),(0,0)))
    gain=10**(args.background_db/20)
    adaptive_report=None
    if args.adaptive_background:
        from scipy.ndimage import gaussian_filter1d
        # A slow content envelope boosts audible background while leaving near-silence alone.
        count=math.ceil(frames/sr);levels=[]
        for i in range(count):
            block=background[i*sr:min(frames,(i+1)*sr)]
            levels.append(20*math.log10(max(1e-9,float(np.sqrt(np.mean(block*block))))))
        smoothed=gaussian_filter1d(np.asarray(levels,dtype='float32'),sigma=1.5,mode='nearest')
        dbs=args.background_db*np.clip((smoothed+60)/15,0,1)
        if args.title_background_db is not None:
            seconds=np.arange(count)+.5
            body=np.clip((seconds-18)/4,0,1)*np.clip((source['duration_s']-8-seconds)/4,0,1)
            dbs=dbs*body+args.title_background_db*(1-body)
        curve=np.empty(frames,dtype='float32')
        for i in range(count):
            stop=min(frames,(i+1)*sr)
            curve[i*sr:stop]=np.linspace(dbs[i],dbs[min(i+1,count-1)],stop-i*sr,endpoint=False,dtype='float32')
        np.multiply(curve,.05,out=curve);np.power(10.,curve,out=curve)
        background*=curve[:,None];del curve
        adaptive_report={'window_s':1,'smoothing_sigma_s':1.5,'no_boost_below_dbfs':-60,
            'full_boost_above_dbfs':-45,'body_max_gain_db':args.background_db,
            'title_gain_db':args.title_background_db,'title_transition_s':[18,22],
            'outro_transition_s':[source['duration_s']-12,source['duration_s']-8]}
        sf.write(out/'background_boosted_float.wav',background,sr,subtype='FLOAT')
        del background;gc.collect()
        # Limit background transients before they can force down the dialogue in the master.
        call(['ffmpeg','-v','error','-y','-i',out/'background_boosted_float.wav',
            '-af','alimiter=limit=0.501187:attack=5:release=150:level=0:latency=1',
            '-ar',str(sr),'-c:a','pcm_f32le',out/'background_independent.wav'])
        background,bgrate=sf.read(out/'background_independent.wav',dtype='float32',always_2d=True)
        if bgrate!=sr or background.shape!=dialogue.shape:raise ValueError('Limiter changed background timing')
    else:
        background*=gain
    # No speech-driven ducking; dialogue samples and timing remain unchanged.
    dialogue+=background
    if not np.isfinite(dialogue).all():raise ValueError('Invalid mix')
    sf.write(out/'premix.wav',dialogue,sr,subtype='FLOAT')
    del dialogue,background;gc.collect()
    result=call(['ffmpeg','-hide_banner','-i',out/'premix.wav','-af','loudnorm=I=-16:TP=-2:LRA=11:print_format=json','-f','null','-'])
    m=parse_loudness(result.stderr)
    filt=(f"loudnorm=I=-16:TP=-2:LRA=11:linear=true:measured_I={m['input_i']}:"
          f"measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}")
    call(['ffmpeg','-v','error','-y','-i',out/'premix.wav','-af',filt,'-ar',str(sr),'-c:a','pcm_s24le',out/'zh_mix.wav'])
    call(['ffmpeg','-v','error','-y','-i',out/'zh_mix.wav','-c:a','aac','-b:a','192k',out/'zh_mix.m4a'])
    for name in ['zh.srt','de.srt']:shutil.copy2(base/name,out/name)
    report={'variant':args.variant,'source_duration_s':source['duration_s'],'sample_rate':sr,'frames':frames,
        'dialogue_reused':str(base/'zh_dialogue.wav'),'tts_regenerated':False,'tempo_processing':False,
        'background_source':str(job/'audio/background48k.wav'),'background_gain_db':args.background_db,
        'background_gain_during_speech':gain,'background_gain_elsewhere':gain,'ducking':False,
        'adaptive_envelope':adaptive_report,'background_peak_limiter_dbfs':-6 if args.adaptive_background else None,
        'background_to_dialogue_change_during_speech_db':20*math.log10(gain/.45),
        'additional_music_added':False,'loudness_target_lufs':-16,'master_true_peak_target_dbtp':-2,
        'created_at_unix':time.time(),'status':'remixed_requires_listening_review'}
    (out/'mix_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print('REMIX_READY',out,flush=True)


if __name__=='__main__':main()
