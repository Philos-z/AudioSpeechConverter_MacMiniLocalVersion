"""Generic machine-preview subtitle cues for intact TTS groups.

Singletons use complete WAV duration without ASR. Multi-row groups use a single
local CPU small/int8 Whisper instance, without target/prompt/prefix/hotwords.
Only exact unique adjacent whole-word anchors >=.70 may choose subtitle onsets.
No phoneme accuracy, audio-cut safety, proportional timing or waveform edit is
claimed. Uncertain groups retain every source row and explicitly require review.
"""
import argparse
import functools
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import wave

SCHEMA='generic_group_subtitle_alignment_v1'
STATUS='aligned_for_machine_preview'
INDEX_SCHEMA='generic_group_subtitle_alignment_manifest_v1'
ASR_SCHEMA='target_free_group_whisper_observation_v1'
OPTIONS={'language':'zh','beam_size':3,'condition_on_previous_text':False,'vad_filter':False,
         'word_timestamps':True,'initial_prompt':None,'prefix':None,'hotwords':None}


def read(path):return json.loads(Path(path).read_text())
def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()
def key(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def check(value,message):
    if not value:raise ValueError(message)
def save(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(path.suffix+'.partial');temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');temporary.replace(path)
def path_record(path):
    path=Path(path).resolve();return {'path':str(path),'sha256':sha(path)}
@functools.lru_cache(maxsize=256)
def _in_process_sha(path,device,inode,size,mtime_ns):
    return sha(path)


def verify_file(path,expected):
    p=Path(path).resolve();st=p.stat()
    check(_in_process_sha(str(p),st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns)==expected,'Bound file SHA changed: '+str(p))


def source_binding(plan):
    check(plan['schema']=='pretts_semantic_group_plan_v1','Unexpected group plan schema')
    binding=plan['source_binding'];rows=plan['source_segments']
    if plan.get('planner_version')=='generic_pretts_semantic_groups_v1':
        item=plan['source_speakers'];verify_file(item['path'],item['sha256'])
    verify_file(rows['path'],rows['sha256'])
    check(read(rows['path'])['segments']==plan['source_rows'],'Plan no longer preserves original source row objects')
    verify_file(binding['source_receipt_path'],binding['source_receipt_sha256'])
    source=read(binding['source_receipt_path']);video=Path(binding['source_video_path']).resolve()
    check(Path(source['input']).resolve()==video,'Source JSON points at another video')
    check(video.stat().st_size==binding['source_video_bytes'],'Bound source video byte count changed')
    verify_file(video,binding['source_video_sha256'])
    check(abs(source['duration_s']-binding['source_duration_s'])<1e-7 and source['audio_track']==binding['source_audio_track'],'Source duration/audio stream changed')


def normalize_factory(root):
    # Text-only OpenCC, loaded lazily; no Torch/MLX import is required.
    try:from opencc import OpenCC
    except ImportError:
        for site in (Path(root)/'vendor/index-tts/.venv/lib').glob('python*/site-packages'):
            if str(site) not in sys.path:sys.path.append(str(site))
        from opencc import OpenCC
    cc=OpenCC('t2s')
    def normalize(text):
        expanded=re.sub(r'<([^<>|]+)\|[^<>]*>',lambda m:m.group(1),text)
        return ''.join(c.lower() for c in cc.convert(expanded) if c.isalnum())
    return normalize


def audio_binding(group,native,manifest_path):
    path=Path(native['audio']).resolve();digest=sha(path);sidecar=path.with_suffix('.json');entry=read(sidecar)
    check(native['sha256']==entry['sha256']==digest,'Generated audio SHA mismatch')
    check(entry.get('schema') in ('native_mlx_whole_utterance_v1','continuous_utterance_v1'),'Unknown group generation provenance')
    content=entry['content'];check(content==native['content'] and entry['cache_key']==key(content),'Generation content/sidecar mismatch')
    check(content['text_zh']==group['text_zh'] and content['effective_tts_text']==(group.get('tts_pronunciation_text') or group['text_zh']),
          'Generated group text/pronunciation differs from plan')
    check(content['reference_sha256']==group['reference']['sha256']==sha(group['reference']['path']),'Group reference SHA changed')
    check(native.get('speaker_id')==group['speaker_id'],'Generated group belongs to another speaker')
    with wave.open(str(path)) as handle:
        frames,rate,channels=handle.getnframes(),handle.getframerate(),handle.getnchannels()
    check(frames>0 and channels==1 and frames==native['frames'] and rate==native['sample_rate'],'Group WAV geometry changed')
    check(abs(native['duration_s']-frames/rate)<=1/rate,'Stale generated group duration')
    return {'path':str(path),'sha256':digest,'duration_s':frames/rate,'sample_rate':rate,'frames':frames,
            'reference_sha256':content['reference_sha256'],'native_sidecar':str(sidecar),'native_sidecar_sha256':sha(sidecar),
            'generation_schema':entry['schema']}


@functools.lru_cache(maxsize=4)
def environment_versions(root):
    paths=[str(p) for p in (Path(root)/'envs/asr/lib').glob('python*/site-packages')]
    packages={d.metadata.get('Name','').lower().replace('_','-'):d.version for d in importlib.metadata.distributions(path=paths)}
    return {name:packages.get(name) or importlib.metadata.version(name) for name in ('faster-whisper','ctranslate2')}


@functools.lru_cache(maxsize=4)
def _model_asset_hashes(model_path,file_stats):
    # In-process memo only, invalidated by each asset's size/mtime. Every new
    # CLI invocation computes actual hashes; no persistent stat-only trust.
    model=Path(model_path)
    return tuple((name,size,sha(model/name)) for name,size,_mtime in file_stats)


def asr_profile(root):
    config=read(Path(root)/'config/local.json');model=Path(config['asr_model']).resolve()
    check(model.is_dir(),'Local Whisper model missing; no download is permitted')
    files=tuple((p.name,p.stat().st_size,p.stat().st_mtime_ns) for p in sorted(model.iterdir()) if p.is_file() and not p.name.startswith('.'))
    assets=[{'name':name,'bytes':size,'sha256':digest} for name,size,digest in _model_asset_hashes(str(model),files)]
    check(any(p['name']=='model.bin' for p in assets),'Local Whisper model.bin missing')
    return {'schema':ASR_SCHEMA,'model_path':str(model),'model_assets':assets,'device':'cpu','compute_type':'int8',
        'cpu_threads':2,'num_workers':1,'local_files_only':True,'target_conditioning':False,
        'transcribe_options':dict(OPTIONS),'versions':environment_versions(root)}


def validate_observation(observation,audio,profile):
    signature=key({'audio_sha256':audio['sha256'],'profile':profile})
    check(observation.get('schema')==ASR_SCHEMA and observation.get('signature')==signature and
          observation.get('audio_sha256')==audio['sha256'] and observation.get('profile')==profile,'Stale target-free ASR cache identity')
    check(observation.get('phase')=='complete' and isinstance(observation.get('segments'),list),'Incomplete target-free ASR observation')
    for segment in observation['segments']:
        check(isinstance(segment['text'],str) and isinstance(segment['words'],list),'Malformed Whisper segment')
        for word in segment['words']:
            check(isinstance(word['word'],str) and all(math.isfinite(word[k]) for k in ('start_s','end_s','probability')),'Malformed Whisper word')
    return signature


class Observer:
    """One model lifetime for the entire job; text is never passed to ASR."""
    def __init__(self,root,cache_dir):
        self.root=Path(root);self.cache=Path(cache_dir);self.model=None;self.profile=None
        self.model_load_s=0.;self.inference_s=0.;self.hits=0;self.misses=0
    def observe(self,audio):
        if self.profile is None:self.profile=asr_profile(self.root)
        signature=key({'audio_sha256':audio['sha256'],'profile':self.profile});path=self.cache/(signature+'.json')
        if path.is_file():
            observation=read(path);validate_observation(observation,audio,self.profile);self.hits+=1
            return observation,path,True
        if self.model is None:
            os.environ['HF_HUB_OFFLINE']='1';os.environ['TRANSFORMERS_OFFLINE']='1'
            from faster_whisper import WhisperModel
            actual={name:importlib.metadata.version(name) for name in self.profile['versions']}
            check(actual==self.profile['versions'],'Run generic subtitle alignment in the configured ASR environment')
            began=time.perf_counter()
            self.model=WhisperModel(self.profile['model_path'],device='cpu',compute_type='int8',cpu_threads=2,num_workers=1,local_files_only=True)
            self.model_load_s=time.perf_counter()-began
        began=time.perf_counter()
        segments,_=self.model.transcribe(audio['path'],**OPTIONS)
        decoded=[{'id':s.id,'start_s':s.start,'end_s':s.end,'text':s.text,'tokens':s.tokens,'avg_logprob':s.avg_logprob,
                  'no_speech_prob':s.no_speech_prob,'words':[{'word':w.word,'start_s':w.start,'end_s':w.end,'probability':w.probability}
                                                         for w in s.words or []]} for s in segments]
        elapsed=time.perf_counter()-began;self.inference_s+=elapsed;self.misses+=1
        observation={'schema':ASR_SCHEMA,'phase':'complete','signature':signature,'audio_sha256':audio['sha256'],
                     'profile':self.profile,'segments':decoded,'inference_s':elapsed}
        validate_observation(observation,audio,self.profile);save(path,observation)
        return observation,path,False


def choose_boundary(expected,character_boundary,words,normalize,duration):
    """Select actual next-word onset; reject ambiguous/weak/merged boundaries."""
    units=[];cursor=0
    for i,word in enumerate(words):
        text=normalize(word['word']);a,b=cursor,cursor+len(text);cursor=b
        if text:units.append({'index':i,'text':text,'a':a,'b':b,'word':word})
    recognized=''.join(v['text'] for v in units);candidates=[]
    for left_count in range(2,min(8,character_boundary)+1):
        for right_count in range(2,min(8,len(expected)-character_boundary)+1):
            left=expected[character_boundary-left_count:character_boundary];right=expected[character_boundary:character_boundary+right_count]
            anchor=left+right
            if expected.count(anchor)!=1 or recognized.count(anchor)!=1:continue
            first=recognized.index(anchor);cut=first+left_count;last=first+len(anchor)
            picked=[v for v in units if v['a']>=first and v['b']<=last]
            if not picked or picked[0]['a']!=first or picked[-1]['b']!=last or ''.join(v['text'] for v in picked)!=anchor:continue
            left_units=[v for v in picked if v['b']<=cut];right_units=[v for v in picked if v['a']>=cut]
            if not left_units or not right_units or left_units[-1]['b']!=cut or right_units[0]['a']!=cut:continue
            # Adjacent nonempty observed units, without an inserted/repeated word.
            positions=[units.index(v) for v in picked]
            if positions!=list(range(positions[0],positions[-1]+1)):continue
            evidence=[];previous=0.;valid=True
            for unit in picked:
                word=unit['word'];a,b,p=word['start_s'],word['end_s'],word['probability']
                if not (all(math.isfinite(v) for v in (a,b,p)) and 0<=a<b<=duration+.04 and .7<=p<=1 and a>=previous-1e-9):valid=False;break
                previous=b;evidence.append({'word_index':unit['index'],'word':word['word'],'normalized_word':unit['text'],
                    'start_s':a,'end_s':b,'probability':p,'side':'left' if unit['b']<=cut else 'right'})
            if not valid:continue
            onset=right_units[0]['word']['start_s'];left_end=left_units[-1]['word']['end_s']
            if not 0<onset<duration or left_end>onset+1e-9:continue
            candidates.append({'character_boundary':character_boundary,'audio_switch_s':onset,
                'whisper_right_word_onset_s':onset,'whisper_left_word_end_s':left_end,
                'left_anchor':left,'right_anchor':right,'target_free_exact_anchor':anchor,'observed_anchor_words':evidence,
                'minimum_anchor_word_probability':min(v['probability'] for v in evidence),
                'anchor_unique':True,'intervening_unmatched_words':False,'visual_switch_only_not_an_audio_cut_window':True})
    check(candidates,'No unique exact whole-word boundary anchors with probability >=.70 and valid nonoverlapping word times; review required')
    # All candidates for one source boundary must identify the same observed onset.
    onsets={round(c['audio_switch_s'],9) for c in candidates}
    check(len(onsets)==1,'Exact anchors identify conflicting observed boundary onsets')
    return max(candidates,key=lambda c:(min(len(c['left_anchor']),len(c['right_anchor'])),len(c['target_free_exact_anchor'])))


def make_alignment(root,plan_path,manifest_path,group,plan,manifest,observation=None,observation_path=None,cache_hit=False):
    native=next(r for r in manifest['utterances'] if r['segment_id']==group['group_id'])
    audio=audio_binding(group,native,manifest_path);duration=audio['duration_s'];members=group['members']
    check(members and [m['segment_id'] for m in members]==group['member_ids'],'Original source members changed')
    receipt={'schema':SCHEMA,'status':STATUS,'group_id':group['group_id'],'member_ids':group['member_ids'],'job':plan['job'],
        'root':str(Path(root).resolve()),'plan':path_record(plan_path),'source_segments_sha256':plan['source_segments']['sha256'],
        'audio':audio,'native_manifest':{**path_record(manifest_path),'segment_id':group['group_id']},
        'boundaries':[],'assistant_reviewed':True,'human_reviewed':False,'waveform_modified':False,'proportional_timing_used':False,
        'new_internal_silence_s':0.,'content_or_listening_approved':False,
        'uncertainty_note':'Machine subtitle timing only. ASR word times are approximate and are not phoneme alignment or safe audio-cut boundaries. Listening and visual review remain required.'}
    if len(members)==1:
        receipt['timing_method']='single_member_full_audio_duration_v1'
        receipt['alignment_receipt']={'method':receipt['timing_method'],'asr_required':False,'model_loaded_for_this_group':False}
    else:
        check(observation is not None and observation_path is not None,'Multi-row group needs real target-free Whisper evidence')
        normalize=normalize_factory(root);expected=normalize(group['text_zh']);words=[w for s in observation['segments'] for w in s['words']]
        profile=observation['profile'];validate_observation(observation,audio,profile)
        check(profile.get('target_conditioning') is False and profile.get('transcribe_options')==OPTIONS,'Target-free ASR options changed')
        check(normalize(''.join(w['word'] for w in words))==normalize(''.join(s['text'] for s in observation['segments'])),'Raw Whisper word/segment transcript differs')
        normalized_members=[normalize(m['text_zh']) for m in members]
        check(''.join(normalized_members)==expected,'Source member normalization differs from generated text')
        previous=0.;char_cursor=0
        for i,member in enumerate(members[:-1]):
            char_cursor+=len(normalized_members[i])
            check(member['character_end']==char_cursor==members[i+1]['character_start'],'Source character index differs from pronunciation-expanded normalized members')
            boundary=choose_boundary(expected,char_cursor,words,normalize,duration)
            check(boundary['audio_switch_s']>previous,'Observed subtitle boundaries are not strictly increasing')
            boundary['after_segment_id']=member['segment_id'];receipt['boundaries'].append(boundary);previous=boundary['audio_switch_s']
        receipt['timing_method']='target_free_whisper_word_onset_v1'
        receipt['alignment_receipt']={'method':receipt['timing_method'],'asr_required':True,
            'observation':path_record(observation_path),'asr_signature':observation['signature'],'asr_profile':profile,
            'confidence_method':'Exact unique adjacent whole-word anchors, word probability >=.70, actual next-sentence word onset',
            'target_text_used_for_recognition':False,'proportional_word_subdivision_used':False}
    switches=[0.]+[b['audio_switch_s'] for b in receipt['boundaries']]+[duration]
    receipt['subtitle_cues']=[]
    for member,a,b in zip(members,switches,switches[1:]):
        check(a<b,'Empty or reversed subtitle cue')
        cue={k:member[k] for k in ('segment_id','speaker_id','text_zh','text_de','source_ids','source_start_s','source_end_s')}
        cue.update(audio_start_s=a,audio_end_s=b);receipt['subtitle_cues'].append(cue)
    return receipt


def validate_receipt(receipt):
    check(receipt.get('schema')==SCHEMA and receipt.get('status')==STATUS,'Generic subtitle receipt is not ready')
    check(receipt.get('assistant_reviewed') is True and receipt.get('human_reviewed') is False and receipt.get('waveform_modified') is False,
          'Generic machine preview review/audio flags changed')
    root=Path(receipt['root']);plan_path=Path(receipt['plan']['path']);manifest_path=Path(receipt['native_manifest']['path'])
    check(sha(plan_path)==receipt['plan']['sha256'] and sha(manifest_path)==receipt['native_manifest']['sha256'],'Bound group plan/generation manifest changed')
    plan,manifest=read(plan_path),read(manifest_path);source_binding(plan)
    from group_compact_adapter import validate_plan
    validate_plan(plan)
    check(manifest['phase']=='complete','Generation manifest incomplete')
    group=next(g for g in plan['tts_groups'] if g['group_id']==receipt['group_id'])
    observation=None;observation_path=None
    if len(group['members'])>1:
        r=receipt['alignment_receipt'];observation_path=Path(r['observation']['path'])
        check(sha(observation_path)==r['observation']['sha256'],'Target-free raw observation changed')
        observation=read(observation_path);profile=asr_profile(root)
        check(observation['profile']==profile,'Whisper model/options/version identity changed')
    rebuilt=make_alignment(root,plan_path,manifest_path,group,plan,manifest,observation,observation_path)
    check(rebuilt==receipt,'Generic cue receipt differs from actual audio/word/source evidence')
    return True


def align_job(root,plan_path,manifest_path,output,cache_dir=None,groups=None,progress=None,plan_only=False):
    began=time.perf_counter();root=Path(root).resolve();plan_path=Path(plan_path).resolve();manifest_path=Path(manifest_path).resolve();output=Path(output).resolve()
    plan,manifest=read(plan_path),read(manifest_path);source_binding(plan)
    from group_compact_adapter import validate_plan
    validate_plan(plan)
    check(manifest.get('phase')=='complete','All selected group audio must be complete before alignment')
    selected=set(groups or [g['group_id'] for g in plan['tts_groups']]);chosen=[g for g in plan['tts_groups'] if g['group_id'] in selected]
    check(chosen and len(chosen)==len(selected),'Unknown/empty group selection')
    check(len({r['segment_id'] for r in manifest['utterances']})==len(manifest['utterances']),'Duplicate generated group IDs')
    check(selected.issubset({r['segment_id'] for r in manifest['utterances']}),'Missing generated group audio')
    for group in chosen:
        check(re.fullmatch(r'[A-Za-z0-9_.-]+',group['group_id']) is not None,'Unsafe group file identity')
        native=next(r for r in manifest['utterances'] if r['segment_id']==group['group_id']);audio_binding(group,native,manifest_path)
    output.mkdir(parents=True,exist_ok=True)
    if not plan_only and (output/'alignment_manifest.json').is_file():
        try:
            old=read(output/'alignment_manifest.json');count=sum(len(g['members']) for g in chosen)
            check(old.get('schema')==INDEX_SCHEMA and old.get('phase')=='complete' and old.get('all_groups_ready') is True,'Incomplete index')
            check(old.get('root')==str(root) and old.get('plan')==path_record(plan_path) and old.get('native_manifest')==path_record(manifest_path),'Stale index identity')
            check(old.get('selected_group_ids')==[g['group_id'] for g in chosen] and old.get('source_plan_row_count')==len(plan['source_rows']) and old.get('source_row_count')==count and old.get('tts_group_count')==len(chosen) and old.get('partial_preview')==(count!=len(plan['source_rows'])),'Stale index scope')
            check([r['group_id'] for r in old['groups']]==[g['group_id'] for g in chosen],'Changed group coverage')
            for item,group in zip(old['groups'],chosen):
                path=output/(group['group_id']+'.json')
                check(item.get('status')==STATUS and item.get('member_ids')==group['member_ids'] and item.get('receipt')==path_record(path),'Changed cue receipt')
                receipt=read(path)
                check(receipt['plan']==path_record(plan_path) and receipt['native_manifest']=={**path_record(manifest_path),'segment_id':group['group_id']},'Stale cue generation')
                validate_receipt(receipt)
        except (ValueError,KeyError,StopIteration,OSError,TypeError):pass
        else:
            save(output/'alignment_run_metrics.json',{'schema':'generic_group_alignment_run_metrics_v1','phase':'complete','complete_index_reused':True,'original_index_bytes_retained':True,'asr_model_loaded':False,'asr_inference_s':0.,'wall_s':time.perf_counter()-began})
            return old
    index={'schema':INDEX_SCHEMA,'phase':'plan_only' if plan_only else 'aligning','root':str(root),'plan':path_record(plan_path),
           'native_manifest':path_record(manifest_path),'selected_group_ids':[g['group_id'] for g in chosen],
           'source_plan_row_count':len(plan['source_rows']),'source_row_count':sum(len(g['members']) for g in chosen),
           'tts_group_count':len(chosen),'partial_preview':sum(len(g['members']) for g in chosen)!=len(plan['source_rows']),
           'all_groups_ready':False,'human_reviewed':False,'waveform_modified':False,'proportional_timing_used':False,'groups':[]}
    if plan_only:
        index.update(asr_groups=sum(len(g['members'])>1 for g in chosen),asr_model_loaded=False)
        save(output/'alignment_manifest.json',index);return index
    observer=Observer(root,cache_dir or root/'cache/generic_group_subtitle_asr_v1')
    for group in chosen:
        observation=None;observation_path=None;hit=False
        try:
            if len(group['members'])>1:
                native=next(r for r in manifest['utterances'] if r['segment_id']==group['group_id']);audio=audio_binding(group,native,manifest_path)
                observation,observation_path,hit=observer.observe(audio)
            receipt=make_alignment(root,plan_path,manifest_path,group,plan,manifest,observation,observation_path,hit)
        except (ValueError,KeyError,StopIteration) as error:
            receipt={'schema':SCHEMA,'status':'needs_alignment_review','group_id':group['group_id'],'member_ids':group['member_ids'],
                     'plan':path_record(plan_path),'native_manifest':path_record(manifest_path),'source_members':group['members'],
                     'reason':str(error),'subtitle_cues':[],'human_reviewed':False,'waveform_modified':False,'proportional_timing_used':False}
        path=output/(group['group_id']+'.json');save(path,receipt)
        index['groups'].append({'group_id':group['group_id'],'member_ids':group['member_ids'],'status':receipt['status'],
            'receipt':path_record(path),'timing_method':receipt.get('timing_method'),'asr_cache_hit':hit,'reason':receipt.get('reason')})
        event={'phase':'aligning_group_subtitles','groups_completed':len(index['groups']),'groups_total':len(chosen),
               'source_rows_completed':sum(len(r['member_ids']) for r in index['groups']),'current_group':group['group_id'],'status':receipt['status']}
        if progress:progress(event)
        save(output/'alignment_manifest.json',index)
    ready=all(g['status']==STATUS for g in index['groups'])
    index.update(phase='complete' if ready else 'needs_alignment_review',all_groups_ready=ready,
        asr_model_loaded=observer.model is not None,asr_model_load_s=observer.model_load_s,asr_inference_s=observer.inference_s,
        asr_cache_hits=observer.hits,asr_cache_misses=observer.misses,wall_s=time.perf_counter()-began)
    save(output/'alignment_manifest.json',index)
    save(output/'alignment_run_metrics.json',{'schema':'generic_group_alignment_run_metrics_v1','phase':index['phase'],'complete_index_reused':False,'original_index_bytes_retained':False,'asr_model_loaded':index['asr_model_loaded'],'asr_model_load_s':observer.model_load_s,'asr_inference_s':observer.inference_s,'asr_cache_hits':observer.hits,'asr_cache_misses':observer.misses,'wall_s':time.perf_counter()-began})
    return index


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('root','plan','manifest','output'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--cache-dir',type=Path);p.add_argument('--group',action='append');p.add_argument('--plan-only',action='store_true');p.add_argument('--progress-json',action='store_true')
    args=p.parse_args();progress=(lambda event:print(json.dumps(event,ensure_ascii=False),flush=True)) if args.progress_json else None
    result=align_job(args.root,args.plan,args.manifest,args.output,args.cache_dir,args.group,progress,args.plan_only)
    print(json.dumps({'phase':result['phase'],'manifest':str((args.output/'alignment_manifest.json').resolve()),'source_rows':result['source_row_count'],
        'tts_groups':result['tts_group_count'],'all_groups_ready':result['all_groups_ready'],'asr_model_loaded':result['asr_model_loaded']},ensure_ascii=False),flush=True)
    if not args.plan_only and not result['all_groups_ready']:raise SystemExit(3)


if __name__=='__main__':main()
