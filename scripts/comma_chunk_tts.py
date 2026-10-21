"""Resumable comma-clause TTS with explicit 180ms quiet joins.

Uses one original IndexTTS instance, selected calm profile and unchanged speed.
Only genuine commas split clauses; list punctuation is retained. Raw input caches
are never changed. Only sub -66dBFS generated leading/trailing samples are trimmed.
"""
import argparse
import contextlib
import fcntl
import gc
import hashlib
import json
import os
from pathlib import Path
import sys
import time

import numpy as np
import soundfile as sf

VERSION='comma_clause_join_v1'
SILENCE_PEAK=.0005
GUARD_S=.020


def read_json(path):return json.loads(Path(path).read_text())

def write_json(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.partial');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n');tmp.replace(path)

def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def fingerprint(data):return hashlib.sha256(json.dumps(data,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def split_commas(text):
    """Keep source punctuation; never split a phoneme tag or numeric separator."""
    chunks=[];start=0;inside=False
    for index,char in enumerate(text):
        if char=='<':inside=True
        elif char=='>':inside=False
        numeric_separator=(char==',' and index>0 and index+1<len(text)
                           and text[index-1].isdigit() and text[index+1].isdigit())
        if char in ('，',',') and not inside and not numeric_separator:
            part=text[start:index+1]
            if not any(c.isalnum() for c in part):raise ValueError('EMPTY_COMMA_CLAUSE')
            chunks.append(part);start=index+1
    if start<len(text):
        part=text[start:]
        if any(c.isalnum() for c in part):chunks.append(part)
        elif chunks:chunks[-1]+=part
    if not chunks and text.strip():chunks=[text]
    if not chunks or ''.join(chunks)!=text:raise ValueError('CLAUSE_SPLIT_ALTERED_TEXT')
    return chunks


def trim_generated_silence(samples,sr):
    samples=np.asarray(samples,dtype=np.float32)
    if not len(samples) or not np.isfinite(samples).all():raise ValueError('INVALID_CHUNK_WAVEFORM')
    peaks=np.max(np.abs(samples),axis=1) if samples.ndim==2 else np.abs(samples)
    active=np.flatnonzero(peaks>SILENCE_PEAK)
    if not len(active) or float(np.max(peaks))<.001:raise ValueError('SILENT_CHUNK')
    first,last=int(active[0]),int(active[-1]);guard=round(GUARD_S*sr)
    start=max(0,first-guard);end=min(len(samples),last+1+guard)
    # An exact peak bound, rather than an ASR/VAD estimate, protects quiet voices.
    if start and np.max(peaks[:start])>SILENCE_PEAK:raise AssertionError('ACTIVE_HEAD_TRIM')
    if end<len(samples) and np.max(peaks[end:])>SILENCE_PEAK:raise AssertionError('ACTIVE_TAIL_TRIM')
    result=np.array(samples[start:end],copy=True)
    head=first-start;tail=end-(last+1);fade=round(.002*sr)
    if head>=fade and fade:
        shape=(-1,1) if result.ndim==2 else (-1,)
        result[:fade]*=np.linspace(0,1,fade,dtype=np.float32).reshape(shape)
    if tail>=fade and fade:
        shape=(-1,1) if result.ndim==2 else (-1,)
        result[-fade:]*=np.linspace(1,0,fade,dtype=np.float32).reshape(shape)
    return result,{'original_samples':len(samples),'retained_samples':len(result),
        'trim_head_samples':start,'trim_tail_samples':len(samples)-end,
        'quiet_head_guard_samples':head,'quiet_tail_guard_samples':tail,
        'silence_peak_threshold':SILENCE_PEAK,'protected_guard_s':GUARD_S}


def join_chunks(chunks,sr,pause_s=.18):
    trimmed=[trim_generated_silence(samples,sr) for samples in chunks]
    target=round(pause_s*sr);pieces=[];joins=[];cursor=0
    for index,(samples,info) in enumerate(trimmed):
        if index:
            previous=trimmed[index-1][1]
            protected=previous['quiet_tail_guard_samples']+info['quiet_head_guard_samples']
            added=max(0,target-protected)
            pieces.append(np.zeros((added,)+samples.shape[1:],dtype=np.float32))
            joins.append({'after_chunk':index-1,'insert_start_sample':cursor,
                'inserted_samples':added,'inserted_s':added/sr,'protected_quiet_samples':protected,
                'total_quiet_gap_samples':added+protected,'total_quiet_gap_s':(added+protected)/sr})
            cursor+=added
        pieces.append(samples);cursor+=len(samples)
    result=np.concatenate(pieces,axis=0)
    assert len(result)==sum(x[1]['retained_samples'] for x in trimmed)+sum(x['inserted_samples'] for x in joins)
    return result,{'chunk_trims':[x[1] for x in trimmed],'joins':joins,'samples':len(result),
        'duration_s':len(result)/sr,'target_pause_s':pause_s,
        'speech_rate_changed':False,'pitch_changed':False,
        'trim_rule':'Only generated edge samples with absolute peak <=0.0005; 20ms quiet guards retained.'}


class LazyEngine:
    def __init__(self,root,job,profile,device):
        self.root=root;self.job=job;self.profile=profile;self.device=device;self.tts=None
    def get(self):
        if self.tts is not None:return self.tts
        root=self.root
        for key,value in {'HF_HOME':root/'models/.hf','NLTK_DATA':root/'models/nltk_data',
            'MPLCONFIGDIR':root/'models/.runtime/matplotlib','MODELSCOPE_CACHE':root/'models/.modelscope',
            'XDG_CACHE_HOME':root/'models/.runtime'}.items():os.environ.setdefault(key,str(value))
        os.environ['HF_HUB_OFFLINE']='1';os.environ['TRANSFORMERS_OFFLINE']='1'
        os.environ['PYTORCH_ENABLE_MPS_FALLBACK']='0'
        os.environ.setdefault('PYTORCH_MPS_HIGH_WATERMARK_RATIO','.7');os.environ.setdefault('PYTORCH_MPS_LOW_WATERMARK_RATIO','.5')
        os.environ.setdefault('TOKENIZERS_PARALLELISM','false')
        import torch
        torch.set_num_threads(2)
        if self.device=='mps':
            if not torch.backends.mps.is_available():raise ValueError('MPS_UNAVAILABLE')
            torch.mps.set_per_process_memory_fraction(.7)
        sys.path.insert(0,str(root/'vendor/index-tts'))
        from indextts.infer_v2_5 import IndexTTS2
        model=root/'models/indextts-2.5'
        with (self.job/'logs/comma_chunk_model.log').open('a') as log,contextlib.redirect_stdout(log),contextlib.redirect_stderr(log):
            self.tts=IndexTTS2(cfg_path=str(model/'config.yaml'),model_dir=str(model),device=self.device,
                use_bf16=False,use_cuda_kernel=False,use_deepspeed=False,use_accel=False,
                use_torch_compile=False,use_qwen_emo=False)
        return self.tts
    def generate(self,text,reference,destination):
        import torch
        tts=self.get();gc.collect()
        if self.device=='mps':torch.mps.empty_cache()
        torch.manual_seed(42);np.random.seed(42)
        with (self.job/'logs/comma_chunk_model.log').open('a') as log,contextlib.redirect_stdout(log),contextlib.redirect_stderr(log),torch.inference_mode():
            tts.infer(spk_audio_prompt=str(reference),text=text,lang='ZH',output_path=str(destination),
                emo_vector=self.profile['calm_vector'],emo_alpha=float(self.profile.get('emo_alpha',1)),
                duration_factor=1.,num_beams=1,use_random=False,max_mel_tokens=500)
        if self.device=='mps':torch.mps.synchronize()


def render_row(row,reference,profile,engine,folder,pause_s=.18):
    text=row.get('tts_pronunciation_text') or row['text_zh']
    texts=split_commas(text);refsha=digest(reference)
    plan={'version':VERSION,'text':row['text_zh'],'tts_text':text,'chunks':texts,
        'reference_sha256':refsha,'profile':profile,'pause_s':pause_s,
        'silence_peak_threshold':SILENCE_PEAK,'guard_s':GUARD_S}
    key=fingerprint(plan);final=folder/'final'/f"{row['segment_id']}_{key[:14]}.wav"
    metadata=final.with_suffix('.json')
    if final.exists() and metadata.exists():
        old=read_json(metadata)
        if old.get('cache_key')==key and old.get('sha256')==digest(final):
            write_json(folder/'final'/(row['segment_id']+'.json'),old)
            return old
    cache=folder/'chunks';cache.mkdir(parents=True,exist_ok=True);samples=[];records=[];sr=None
    for index,part in enumerate(texts):
        part_key=fingerprint({'version':VERSION,'tts_text':part,'reference_sha256':refsha,'profile':profile})
        wav=cache/(part_key[:20]+'.wav');meta=wav.with_suffix('.json')
        info=read_json(meta) if meta.exists() else {}
        if not wav.exists() or info.get('cache_key')!=part_key or info.get('sha256')!=digest(wav):
            partial=wav.with_name(wav.stem+'.partial.wav');began=time.monotonic()
            engine.generate(part,reference,partial)
            audio,rate=sf.read(partial,dtype='float32');trim_generated_silence(audio,rate)
            if len(audio)/rate>18:raise ValueError('CHUNK_LONGER_THAN_18_SECONDS')
            partial.replace(wav)
            info={'cache_key':part_key,'tts_text':part,'file':str(wav),'sha256':digest(wav),
                'sample_rate':rate,'duration_s':len(audio)/rate,'inference_s':time.monotonic()-began,
                'profile':profile,'reference_sha256':refsha}
            write_json(meta,info)
        audio,rate=sf.read(wav,dtype='float32')
        if sr is not None and sr!=rate:raise ValueError('CHUNK_SAMPLE_RATE_MISMATCH')
        sr=rate;samples.append(audio);records.append(info)
    joined,join_info=join_chunks(samples,sr,pause_s)
    final.parent.mkdir(parents=True,exist_ok=True)
    partial=final.with_name(final.stem+'.partial.wav');sf.write(partial,joined,sr,subtype='PCM_24');partial.replace(final)
    report={'id':row['segment_id'],'text':row['text_zh'],'speaker':row['speaker_id'],'status':'comma_chunked',
        'raw_source_text':row['text_zh'],'tts_text':text,'pause_s':pause_s,
        'cache_key':key,'file':str(final.resolve()),'output_file':str(final.resolve()),'sha256':digest(final),
        'sample_rate':sr,'duration_s':len(joined)/sr,'effective_punctuation_mode':'comma_clause_join_v1',
        'profile':profile,'chunks':records,'join_info':join_info,'subjective_listening_performed':False}
    write_json(metadata,report)
    write_json(folder/'final'/(row['segment_id']+'.json'),report)
    return report


def queued_ids(args):
    result=list(args.ids or [])
    if args.queue_file and args.queue_file.exists():
        data=read_json(args.queue_file)
        if isinstance(data,dict):data=data.get('ids',[])
        if not isinstance(data,list) or not all(isinstance(x,str) for x in data):raise ValueError('QUEUE_MUST_BE_ID_LIST')
        result+=data
    return list(dict.fromkeys(result))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True);parser.add_argument('--job',required=True)
    parser.add_argument('--ids',nargs='*');parser.add_argument('--queue-file',type=Path)
    parser.add_argument('--watch-queue',action='store_true');parser.add_argument('--idle-timeout',type=float,default=1200)
    parser.add_argument('--pause',type=float,default=.18);parser.add_argument('--device',choices=['mps','cpu'],default='mps')
    args=parser.parse_args()
    if args.watch_queue and not args.queue_file:parser.error('--watch-queue requires --queue-file')
    if not .12<=args.pause<=.25:parser.error('--pause must be 0.12–0.25s')
    root=args.root.resolve();job=root/'jobs'/args.job;folder=job/'tts/comma_chunks';folder.mkdir(parents=True,exist_ok=True)
    (job/'logs').mkdir(exist_ok=True)
    lock=(folder/'runner.lock').open('w')
    try:fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:raise ValueError('COMMA_CHUNK_RUNNER_ALREADY_ACTIVE')
    from episode import read_job
    _,_,speakers=read_job(root,job)
    profile=read_json(job/'progress.json')['profile']
    if profile.get('duration_factor')!=1. or profile.get('seed')!=42:raise ValueError('UNEXPECTED_CALM_PROFILE')
    engine=LazyEngine(root,job,profile,args.device)
    status={'version':VERSION,'phase':'running','started_at_unix':time.time(),'profile':profile,
        'pause_s':args.pause,'results':{},'errors':{},'target_ids':[],'completed':0}
    status_path=job/'comma_chunk_progress.json';idle_since=time.monotonic()
    while True:
        rows=read_json(job/'segments.json')['segments'];by_id={row['segment_id']:row for row in rows}
        ids=queued_ids(args);status['target_ids']=ids
        unknown=[identifier for identifier in ids if identifier not in by_id]
        if unknown:raise ValueError('UNKNOWN_IDS: '+str(unknown))
        pending=[identifier for identifier in ids if identifier not in status['results'] and identifier not in status['errors']]
        for identifier in pending:
            row=by_id[identifier];reference=Path(speakers[row['speaker_id']]['reference'])
            if not reference.is_absolute():reference=root/reference
            try:
                result=render_row(row,reference,profile,engine,folder,args.pause)
                status['results'][identifier]=result;status['completed']=len(status['results'])
                print('COMMA_CHUNK_DONE',identifier,status['completed'],'/',len(ids),f"{result['duration_s']:.3f}s",flush=True)
            except Exception as error:
                status['errors'][identifier]={'type':type(error).__name__,'message':str(error)[-1200:]}
                print('COMMA_CHUNK_FAILED',identifier,type(error).__name__,str(error)[-200:],flush=True)
                if len(status['errors'])>=3:status['phase']='failed';write_json(status_path,status);raise
            idle_since=time.monotonic();status['updated_at_unix']=time.time();write_json(status_path,status)
        if not args.watch_queue or (folder/'stop').exists() or time.monotonic()-idle_since>=args.idle_timeout:break
        status['phase']='watching_queue';write_json(status_path,status);time.sleep(2)
    status['phase']='needs_review' if status['errors'] else 'complete';status['finished_at_unix']=time.time()
    write_json(status_path,status)
    print(json.dumps({'phase':status['phase'],'completed':status['completed'],'errors':status['errors']},ensure_ascii=False),flush=True)

if __name__=='__main__':main()
