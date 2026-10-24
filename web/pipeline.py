"""Single-job local dubbing orchestration; framework imports stay in child stages.

Public interface: run_job(root, job, update, run_command, is_cancelled).
Every subprocess uses the supplied supervisor (logs, cancellation/process groups).
Machine-reviewed drafts retain human_reviewed=False and unknown source rights.
"""
import argparse
import ast
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_EXCEPTION, CancelledError
import fcntl
import errno
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
from urllib.parse import urlparse
from source_language import normalize_language, language_label, source_language_metadata

VERSION='local_web_natural_pipeline_v1'
DEFAULT_TTS_PROFILE='hybrid-gpt-preserve-25-pad-eager'
DEFAULT_TTS_GROUPING='semantic_groups_v1'
DEFAULT_MASTERING_POLICY='constant_if_safe_v1'
LEGACY_MASTERING_POLICY='legacy_loudnorm_v1'
MASTERING_POLICIES=(LEGACY_MASTERING_POLICY,DEFAULT_MASTERING_POLICY)
PROFILES=('native-half25','hybrid-fp32-25','hybrid-fp16-25',
          'hybrid-fp16-25-pad-eager',DEFAULT_TTS_PROFILE)
FFMPEG='/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg'
FFPROBE='/opt/homebrew/opt/ffmpeg-full/bin/ffprobe'
DEMUCS_WEIGHT='955717e8-8726e21a.th'
DEMUCS_SHA='8726e21a993978c7ba086d3872e7608d7d5bfca646ca4aca459ffda844faa8b4'
SPACE_BYTES_PER_SOURCE_SECOND=4*1024**2
SPACE_FIXED_MARGIN_BYTES=256*1024**2



STAGE_EVENT_NONCE_ENV='PHILOS_STAGE_EVENT_NONCE'
_FAILURE_BASE_KEYS=frozenset({'schema','origin','stage','category'})


class StageFailure(RuntimeError):
    """An owned child failed with nonce-bound, final transport evidence."""
    def __init__(self,stage,message,failure_metadata):
        super().__init__(message)
        self.stage=stage
        self.failure_metadata=dict(failure_metadata)


class WebsiteRepairReady(RuntimeError):
    """A fixed local action changed bound data; resume with ordinary SHA checks."""
    def __init__(self,action,details):
        super().__init__('网站已保存局部恢复数据，将从断点继续')
        self.action=action;self.details=details


def implementation_signature(names,source_path=None):
    """Unrelated orchestration edits must not invalidate expensive stage data."""
    tree=ast.parse(Path(source_path or __file__).read_text())
    functions={n.name:n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.ClassDef))}
    return key([ast.dump(functions[name],include_attributes=False) for name in names])


def speaker_implementation_signature(source_path=None):
    return implementation_signature(('single_speaker','check_speakers','regroup_job',
        'regroup_sentence_rows','german_sentence_end'),source_path)


def _failure_event(text,nonce):
    if not isinstance(text,str) or len(text)>2048:return None
    try:event=json.loads(text)
    except (ValueError,TypeError):return None
    if (not isinstance(event,dict) or event.get('nonce')!=nonce
            or event.get('event') not in ('proxy_transport_failure','proxy_stage_failure')):return None
    metadata={k:v for k,v in event.items() if k not in ('event','nonce')}
    category=metadata.get('category')
    extra=({'http_status'} if category=='http_response' else {'exception_type'}
           if category=='transport_timeout' else {'exception_type','errno'} if category=='connection_reset' else set())
    if (not extra or set(metadata)-_FAILURE_BASE_KEYS-extra
            or not _FAILURE_BASE_KEYS<=set(metadata)
            or metadata.get('schema')!='bounded_stage_failure_v1'
            or metadata.get('origin')!='proxy_transport' or metadata.get('stage')!='translation'):
        return None
    if category=='http_response':
        if type(metadata.get('http_status')) is not int or metadata['http_status'] not in {408,425,429,500,502,503,504}:return None
    elif category=='transport_timeout':
        if metadata.get('exception_type') not in ('TimeoutError','socket.timeout'):return None
    else:
        exception_type=metadata.get('exception_type');actual_errno=metadata.get('errno')
        if exception_type not in ('ConnectionResetError','ConnectionAbortedError','OSError'):return None
        if actual_errno is not None and type(actual_errno) is not int:return None
        if exception_type=='OSError' and actual_errno not in (errno.ECONNRESET,errno.ECONNABORTED):return None
    return event['event'],metadata


def _recovery_module(root):
    path=script(root,'recovery.py')
    spec=importlib.util.spec_from_file_location('philos_bounded_recovery',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def _private_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    if path.is_symlink():raise ValueError('恢复记录不能指向符号链接')
    descriptor,temporary=tempfile.mkstemp(prefix='.'+path.name+'.',dir=path.parent)
    try:
        with os.fdopen(descriptor,'w') as stream:
            os.fchmod(stream.fileno(),0o600)
            json.dump(value,stream,ensure_ascii=False,indent=2);stream.write('\n')
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def read(path):return json.loads(Path(path).read_text())


def _voice_pilot_module(root):
    path=script(root,'early_voice_pilot.py')
    spec=importlib.util.spec_from_file_location('philos_early_voice_pilot',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def raw_logits_usage(manifest):
    """Report actual finite checks; their synchronization cost is in TTS wall.

    No per-check timer was installed in the verified helper. Do not invent a
    separate incremental overhead or subtract it from the measured CLI time.
    """
    profile=manifest.get('parameters',{}).get('experimental_profile',{})
    if profile.get('name')!=DEFAULT_TTS_PROFILE:return None
    records=[row.get('experimental_execution',{}).get('semantic_logits_guard',{})
             for row in manifest['utterances']]
    if not records:raise RuntimeError('配音数值检查记录为空，不继续导出')
    total=0
    for record in records:
        transaction=record.get('transaction',{});calls=transaction.get('sampling_calls')
        if (record.get('signature')!=profile.get('semantic_logits_guard') or type(calls)is not int or calls<1
                or transaction.get('checked_calls')!=calls or transaction.get('original_sampler_calls')!=calls
                or transaction.get('nonfinite_rejected') is not False
                or transaction.get('first_raw_logits',{}).get('all_finite') is not True):
            raise RuntimeError('配音数值检查记录不完整，不继续导出')
        total+=calls
    return {'policy':'all_raw_semantic_logits_finite_before_sampling','checked_steps':total,
        'first_logits_cpu_snapshots':len(records),'all_observed_finite':True,
        'synchronization_overhead_included_in_tts_wall_s':True,
        'incremental_overhead_separately_measured':False,'incremental_overhead_s':None,
        'listening_quality_approved':False}


def group_manifest_identity(manifest):
    if manifest.get('phase')!='complete' or manifest.get('pending_unique_keys')!=0:
        raise RuntimeError('连续配音清单尚未完整，不建立字幕缓存')
    fields=('segment_id','speaker_id','cache_key','content','sha256','frames','sample_rate','duration_s','audio')
    return {'utterances':[{k:row[k] for k in fields} for row in manifest['utterances']],
        'parameters':manifest.get('parameters')}


def stable_group_manifest(folder,manifest_path):
    """Keep an actual complete receipt or explicitly annotated audio composition.

    The snapshot remains an unedited real generation manifest. Current-run TTS
    counters still come from manifest_path, while caption validation verifies
    every actual WAV, sidecar and reference against this immutable receipt.
    """
    actual=read(manifest_path);identity=group_manifest_identity(actual)
    snapshot=Path(folder)/'tts/group_manifest_snapshots'/key(identity)/'manifest.json'
    if snapshot.is_file():
        if group_manifest_identity(read(snapshot))!=identity:
            raise RuntimeError('已保存的连续配音清单与真实声音身份不符')
    else:save(snapshot,actual)
    return snapshot


def tts_signature(segments,speakers,profile,emotion,runner):
    return {'segments':digest(segments),'speakers':digest(speakers),'profile':profile,
            'emotion':emotion,'runner':digest(runner)}
def save(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(path.suffix+'.partial')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');temporary.replace(path)
def digest(path):
    result=hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda:source.read(1024*1024),b''):result.update(block)
    return result.hexdigest()
def key(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def space_preflight(duration_s,free_bytes,reserve_gib=8.):
    """Conservative cold-job peak; never assume shorter speech saves storage."""
    if not math.isfinite(duration_s) or duration_s<=0:raise ValueError('视频时长无效，无法估算空间')
    if not math.isfinite(reserve_gib) or reserve_gib<0:raise ValueError('空间安全余量设置无效')
    components={'original_48k_stereo_pcm24':288000,'asr_16k_mono_pcm16':32000,
        'demucs_two_float32_stems_44k1':705600,'background_three_stereo_float48k':1152000,
        'mix_four_wav_intermediates':1248000,'three_encoded_video_copies':320000,
        'native_24k_mono_pcm24_and_audio_container':88000}
    peak=math.ceil(duration_s*SPACE_BYTES_PER_SOURCE_SECOND)+SPACE_FIXED_MARGIN_BYTES
    reserve=math.ceil(reserve_gib*1024**3);required=peak+reserve
    result={'schema':'web_job_space_preflight_v1','source_duration_s':duration_s,'free_bytes':int(free_bytes),
        'estimated_components_bytes_per_source_second':components,
        'rounded_peak_bytes_per_source_second':SPACE_BYTES_PER_SOURCE_SECOND,
        'fixed_margin_bytes':SPACE_FIXED_MARGIN_BYTES,'estimated_new_peak_bytes':peak,
        'protected_reserve_bytes':reserve,'required_free_bytes':required,'sufficient':free_bytes>=required,
        'shorter_output_assumed':False,'old_assets_deleted':False,'existing_outputs_credit_bytes':0}
    if not result['sufficient']:
        gib=1024**3;deficit=(required-free_bytes)/gib
        raise RuntimeError(f'空间不足：当前可用{free_bytes/gib:.2f} GiB，预计本任务新增峰值{peak/gib:.2f} GiB，'
            f'另保留{reserve/gib:.2f} GiB安全余量；请至少再腾出{deficit:.2f} GiB后重试。未启动重模型，也未删除旧素材。')
    return result


def script(root,name):
    candidates=[Path(__file__).resolve().parent/name,Path(root)/'scripts'/name,Path(__file__).resolve().parents[1]/'efficiency'/name]
    found=next((p for p in candidates if p.is_file()),None)
    if found is None:raise RuntimeError('缺少本地流程组件：'+name)
    return found


def mastering_policy(options):
    # Persisted jobs lacking the new option keep the original mastering path.
    policy=options.get('mastering_policy',LEGACY_MASTERING_POLICY)
    if policy not in MASTERING_POLICIES:raise ValueError('不支持的混音母带策略')
    return policy


def mastering_signature(root,adapter,policy):
    mastering_policy({'mastering_policy':policy})
    helper=script(root,'optional_constant_master.py')
    spec=importlib.util.spec_from_file_location('philos_optional_master_identity',helper)
    implementation=importlib.util.module_from_spec(spec);spec.loader.exec_module(implementation)
    actual=implementation.identity(legacy_code_path=Path(adapter))
    helper_sha=digest(helper)
    if actual.get('helper_sha256')!=helper_sha or actual.get('legacy_code_sha256')!=digest(adapter):
        raise RuntimeError('混音策略组件身份不一致')
    return {'policy':policy,'helper_sha256':helper_sha,'implementation':actual}


def geometry(probe):
    video=next((s for s in probe['streams'] if s['codec_type']=='video'),None)
    if not video or not any(s['codec_type']=='audio' for s in probe['streams']):raise RuntimeError('素材必须同时包含视频和声音')
    width,height=int(video['width']),int(video['height'])
    rotation=float(video.get('tags',{}).get('rotate',0) or 0)
    for item in video.get('side_data_list',[]):rotation=float(item.get('rotation',rotation))
    if round(rotation)%180:width,height=height,width
    if abs(width/height-16/9)>.01:
        raise RuntimeError('首版自动导出目前只支持16:9横屏；已保留源方向，未拉伸或假称导出成功。4:3和竖屏待适配。')
    return {'width':960,'height':540,'fps':25,'preserve_source_orientation':True}


def reviewed_source_plan(folder,original_plan,source_sha256):
    """Apply an evidence-bound derived plan, keeping all original files intact."""
    folder=Path(folder).resolve();original_plan=Path(original_plan)
    original=read(original_plan)['segments'];receipt=folder/'source_plan_revision.json'
    if not receipt.is_file():return original,None
    revision=read(receipt);derived=folder/'revisions/source_review_v1/segments.plan.json'
    if (revision.get('schema')!='source_plan_revision_v1'
            or revision.get('original_plan_sha256')!=digest(original_plan)
            or revision.get('input_source_sha256')!=source_sha256
            or revision.get('revised_plan_sha256')!=digest(derived)):
        raise RuntimeError('源文修订与原视频或原计划绑定不符；未改写原始成果')
    changes=revision.get('changes');revised=read(derived)['segments']
    if not isinstance(changes,list) or not changes:raise ValueError('源文修订缺少逐条证据')
    by_id={row['segment_id']:row for row in original};change_map={}
    for change in changes:
        sid=change.get('segment_id');action=change.get('action');evidence=change.get('evidence')
        if (sid not in by_id or sid in change_map or action not in {'replace_source_text','exclude_asr_duplicate'}
                or not isinstance(evidence,list) or not evidence
                or change.get('original_text_sha256')!=hashlib.sha256(by_id[sid]['text_de'].encode()).hexdigest()):
            raise ValueError('源文修订编号、原文或证据不完整')
        for proof in evidence:
            path=folder/proof['path'];actual=path.resolve()
            if (path.is_symlink() or not actual.is_relative_to(folder/'revisions/source_review_v1/evidence')
                    or not actual.is_file() or digest(actual)!=proof['sha256']):
                raise ValueError('源文修订证据位置或哈希不符')
        if action=='exclude_asr_duplicate' and (change.get('zero_duration_words',0)<4
                or change.get('crop_asr_confirms_no_repetition') is not True):
            raise ValueError('重复识别排除证据不足')
        change_map[sid]=change
    expected=[r['segment_id'] for r in original if change_map.get(r['segment_id'],{}).get('action')!='exclude_asr_duplicate']
    if [r['segment_id'] for r in revised]!=expected:raise ValueError('源文修订改变了未授权的台词顺序或编号')
    for row in revised:
        sid=row['segment_id'];before=by_id[sid];change=change_map.get(sid)
        if change:
            if (not isinstance(row.get('text_de'),str) or not row['text_de'].strip()
                    or row.get('text_source')!=row['text_de']
                    or change.get('replacement_text_sha256')!=hashlib.sha256(row['text_de'].encode()).hexdigest()
                    or {k:v for k,v in row.items() if k not in {'text_de','text_source'}}
                        !={k:v for k,v in before.items() if k not in {'text_de','text_source'}}):
                raise ValueError('源文修订改变了时间、人物或未批准字段')
        elif row!=before:raise ValueError('源文修订改变了未列入修订的台词')
    return revised,{'receipt':str(receipt),'receipt_sha256':digest(receipt),
        'original_rows':len(original),'derived_rows':len(revised),'changes':len(changes),
        'original_asr_and_plan_preserved':True,'unresolved':revision.get('unresolved',[]),
        'blocking_unresolved':revision.get('blocking_unresolved',[])}


def validate_translations(before,after,report):
    if len(before)!=len(after) or len({r['segment_id'] for r in after})!=len(before):raise RuntimeError('机器译稿编号或条数不完整')
    if report.get('human_reviewed') is not False or report.get('review_method')!='two_pass_machine_review_and_numeric_invariants':
        raise RuntimeError('译稿没有实际机器二审及数字检查记录')
    source={r['segment_id']:r for r in before}
    for original in before:
        if 'text_source' in original and original['text_source']!=original['text_de']:
            raise RuntimeError('原文兼容字段内容不一致')
        language=original.get('source_language')
        if language and report.get('source_language', 'de' if language=='de' else None)!=language:
            raise RuntimeError('译稿审校语言与实际原文不一致')
    for row in after:
        original=source.get(row['segment_id'])
        if not original or any(row.get(k)!=original.get(k) for k in ('speaker_id','start_s','end_s','text_de')):
            raise RuntimeError('机器译稿改变了源台词或时间/说话人归属')
        if (('text_source' in original and row.get('text_source')!=original['text_source'])
                or ('source_language' in original and row.get('source_language')!=original['source_language'])
                or ('text_source' in row and row['text_source']!=row['text_de'])):
            raise RuntimeError('机器译稿改变了原文或源语言字段')
        if (not isinstance(row.get('text_zh'),str) or not row['text_zh'].strip()
                or row.get('translation_reviewed') is not True or row.get('human_reviewed') is not False
                or row.get('translation_review_method')!='proxy_second_pass_and_numeric_checks'):
            raise RuntimeError('机器译稿未通过可核验的第二遍审校')


def infer_groups(rows):
    groups=[];current=[]
    for row in rows:
        if current:
            previous=current[-1]
            unfinished=not re.search(r'[。.!！？?][”’"）)]*\s*$',previous['text_zh'])
            join=(unfinished and previous['speaker_id']==row['speaker_id']
                and row['start_s']-previous['end_s']<=1.2 and row['end_s']-current[0]['start_s']<=24)
            if not join:
                if len(current)>1:groups.append([r['segment_id'] for r in current])
                current=[]
        current.append(row)
    if len(current)>1:groups.append([r['segment_id'] for r in current])
    return groups



def reviewed_natural_groups(root,folder,rows,profile,emotion):
    """Optional job-scoped picture groups; retain every existing whole WAV.

    This is not continuous-group TTS. The usual adapter still checks actual
    audio duration, sample SHA and the unchanged .35s timing bound.
    """
    folder=Path(folder).resolve();path=folder/'natural_group_review.json'
    if not path.is_file():return infer_groups(rows)
    review=read(path)
    if (not isinstance(review,dict) or review.get('review_status')!='machine_second_pass_approved'
            or review.get('human_reviewed') is not False):
        raise ValueError('相邻画面分组尚未通过实际机器审校')
    binding={'job':folder.name,'source_segments_sha256':digest(folder/'segments.json'),
             'source_speakers_sha256':digest(folder/'speakers.json')}
    if any(review.get(field)!=value for field,value in binding.items()):
        raise ValueError('相邻画面分组不是当前台词与人物的审校结果')
    proof=review.get('actual_proxy_review',{})
    relative=proof.get('relative_path') if isinstance(proof,dict) else None
    if not isinstance(relative,str) or Path(relative).is_absolute():
        raise ValueError('相邻画面分组缺少实际审校证据')
    evidence=(folder/relative).resolve();translation_root=(folder/'translation').resolve()
    if (not translation_root.is_relative_to(folder) or not evidence.is_relative_to(translation_root)
            or not evidence.is_file() or digest(evidence)!=proof.get('sha256')):
        raise ValueError('相邻画面分组的审校记录路径或SHA不匹配')
    record=read(evidence)
    if not isinstance(record,dict):raise ValueError('相邻画面分组审校记录格式无效')
    compiled=record.get('schema')=='targeted_timing_proxy_review_compilation_v1'
    request=record.get('final_request',{}) if compiled else record.get('request',{})
    response=record.get('response',{})
    if compiled:
        spec=importlib.util.spec_from_file_location('philos_actual_review_compilation',Path(__file__).with_name('review_compilation.py'))
        validator=importlib.util.module_from_spec(spec);spec.loader.exec_module(validator)
        validator.validate(folder,record,request)
    if ((not compiled and (record.get('schema')!='targeted_timing_proxy_review_v1'
                or record.get('status')!='response_received' or record.get('human_reviewed') is not False
                or not isinstance(record.get('requests'),list) or len(record['requests'])!=1
                or not isinstance(record['requests'][0],dict) or record['requests'][0].get('text_only') is not True))
            or not isinstance(request,dict) or not isinstance(response,dict)
            or any(request.get(field)!=value for field,value in binding.items())):
        raise ValueError('相邻画面分组缺少本任务当前台词与人物的实际Proxy审校记录')
    proposals=review.get('tts_join_suggestions',[])
    requests=request.get('groups');responses=response.get('groups')
    if (not isinstance(proposals,list) or not isinstance(requests,list)
            or not isinstance(responses,list)):
        raise ValueError('相邻画面分组记录格式无效或没有获批建议')
    def indexed(items):
        result={}
        for item in items:
            ids=item.get('member_ids') if isinstance(item,dict) else None
            if (not isinstance(ids,list) or len(ids)<2 or not all(isinstance(sid,str) for sid in ids)
                    or len(set(ids))!=len(ids) or tuple(ids) in result):
                raise ValueError('相邻画面分组审校记录包含重复或无效编号')
            result[tuple(ids)]=item
        return result
    requested=indexed(requests);responded=indexed(responses);indexed(proposals)
    if set(requested)!=set(responded):
        raise ValueError('相邻画面分组响应包含未请求或缺失的建议')
    if any(type(item.get('approved')) is not bool for item in responses):
        raise ValueError('相邻画面分组响应缺少明确审批')
    if proposals!=[item for item in responses if item['approved']]:
        raise ValueError('相邻画面分组不是实际响应中全部获批的建议')
    projection=('segment_id','speaker_id','start_s','end_s','text_de','text_zh')
    current={row['segment_id']:{k:row[k] for k in projection} for row in rows}
    if len(current)!=len(rows):raise ValueError('当前原台词编号重复')
    for ids,item in requested.items():
        if any(sid not in current for sid in ids) or item.get('rows')!=[current[sid] for sid in ids]:
            raise ValueError('相邻画面分组不是对当前原文、译文与人物的实际审校结果')
    if not proposals:return infer_groups(rows)
    spec=importlib.util.spec_from_file_location('philos_natural_group_review',Path(__file__).with_name('generic_group_plan.py'))
    planner=importlib.util.module_from_spec(spec);spec.loader.exec_module(planner)
    plan=planner.build_plan(Path(root),folder.name,path,profile=profile,profile_options={'emotion_mode':emotion})
    if plan.get('rejected_suggestions') or plan.get('source_rows')!=rows:
        raise ValueError('相邻画面分组越过人物、话题、引用或源时间边界')
    groups=[g['member_ids'] for g in plan['tts_groups'] if len(g['member_ids'])>1]
    for original in infer_groups(rows):
        conflicts=[group for group in groups if set(group)&set(original)]
        if conflicts and conflicts!=[original]:
            raise ValueError('相邻画面分组与原有半句分组发生部分重叠')
        if not conflicts:groups.append(original)
    positions={row['segment_id']:index for index,row in enumerate(rows)}
    return sorted(groups,key=lambda ids:positions[ids[0]])


def german_sentence_end(text,source_language='de'):
    tail=text.rstrip().rstrip('"\'”’»›)]}」』）】》').rstrip()
    if not re.search(r'[.!?…。！？؟۔।॥]$',tail):return False
    # Common abbreviations do not prove a sentence ending by themselves.
    abbreviations=(r'(?:\b(?:Dr|Prof|bzw|usw|ca|Nr|Str)|z\.\s*B|u\.\s*a)\.$'
        if source_language=='de' else r'\b(?:Dr|Prof|Mr|Mrs|Ms|St|etc|vs)\.$')
    if re.search(abbreviations,tail,re.I):return False
    return True


def regroup_sentence_rows(assigned,duration,max_span_s=24.,max_gap_s=1.2,source_language='de'):
    """Join raw ASR fragments up to actual German sentence endings, same voice.

    Never split a raw fragment or lose source IDs to meet an arbitrary duration.
    Incomplete >24s/source-speaker boundaries stay explicit for later review.
    """
    if not assigned:raise RuntimeError('没有可编排的原文台词')
    grouped=[];issues=[]
    expected=[sid for row in assigned for sid in row.get('source_ids',[row['segment_id']])]
    if len(set(expected))!=len(expected):raise RuntimeError('原始 ASR 编号重复，不能安全重新编排')
    for row in assigned:
        if (('text_source' in row and row['text_source']!=row['text_de'])
                or ('source_language' in row and row['source_language']!=source_language)):
            raise RuntimeError('原文兼容字段或源语言不一致，未重新编排')
        text=row['text_de'].strip()
        if not text:raise RuntimeError('原始 ASR 台词为空，不能静默丢弃')
        ids=list(row.get('source_ids',[row['segment_id']]))
        join=False
        if grouped:
            previous=grouped[-1]
            join=(not german_sentence_end(previous['text_de'],source_language) and previous['speaker_id']==row['speaker_id']
                and -.04<=row['start_s']-previous['end_s']<=max_gap_s
                and row['end_s']-previous['start_s']<=max_span_s)
        if join:
            previous['text_de']+=' '+text;previous['end_s']=row['end_s'];previous['source_ids'].extend(ids)
        else:
            grouped.append({'segment_id':f'line{len(grouped)+1:04d}','speaker_id':row['speaker_id'],
                'start_s':row['start_s'],'end_s':row['end_s'],'text_de':text,'text_zh':'','source_ids':ids,
                'translation_reviewed':False,'speaker_reviewed':True,'human_reviewed':False,
                'speaker_review_method':'machine acoustic assignment; no human or identity verification',
                'overlap':bool(row.get('overlap',False))})
    for index,row in enumerate(grouped):
        boundary=grouped[index+1]['start_s'] if index+1<len(grouped) else duration
        row['slot_end_s']=min(boundary-.06,row['end_s']+.8,duration)
        row['target_characters']=max(5,int(max(.1,row['slot_end_s']-row['start_s'])*3.8))
        row['text_source']=row['text_de'];row['source_language']=source_language
        row['source_sentence_complete']=german_sentence_end(row['text_de'],source_language)
        row['german_sentence_complete']=row['source_sentence_complete']
        if not row['german_sentence_complete']:
            issues.append({'segment_id':row['segment_id'],'reason':'incomplete_source_sentence_at_speaker_gap_or_24s_boundary'})
        if row['end_s']-row['start_s']>max_span_s:
            issues.append({'segment_id':row['segment_id'],'reason':'single_raw_asr_fragment_exceeds_24s_preserved_intact'})
    actual=[sid for row in grouped for sid in row['source_ids']]
    if actual!=expected:raise RuntimeError('原文句子编排改变了原始台词顺序或覆盖范围')
    return {'schema_version':2,'segments':grouped,'source':'unchanged source ASR fragments',
        'speaker_method':'same-speaker machine acoustic assignments','identities_verified':False,'human_reviewed':False,
        **source_language_metadata(source_language),
        'grouping_method':'complete source sentence punctuation; same speaker; contiguous <=1.2s; max24s without slicing ASR',
        'source_ids_count':len(actual),'issues':issues}


def make_receipt(root,folder,background):
    source=read(folder/'source.json');rows=read(folder/'segments.json')['segments'];shape=geometry(source['probe'])
    assets={}
    for name,path in [('source_video',Path(source['input'])),('independent_background',background),
                      ('logo',root/'assets/philos_club_serif_wordmark.png')]:
        assets[name]={'path':str(path.resolve()),'bytes':path.stat().st_size,'sha256':digest(path)}
    receipt={'schema':'new_web_job_asset_receipt_v1','assets':assets,
        'visual':{**shape,'join_groups':infer_groups(rows),'pause_target_s':.35,
            'zh_font_size':34,'de_font_size':24,'logo_width':144,'logo_opacity':.55,'logo_brand':'Philos.club',
            'logo_style':'serif_box_v2'},
        'picture_cache':{'rows':[]},'human_reviewed':False,'source_rights_verified':False}
    path=folder/'native_asset_receipt.json';save(path,receipt);return path


class Flow:
    def __init__(self,root,job,update,run_command,is_cancelled):
        self.root=Path(root).resolve();self.job=job;self.update=update;self.runner=run_command;self.cancelled=is_cancelled
        self.name=job['id']
        if not re.fullmatch(r'web_[A-Za-z0-9_-]{1,54}',self.name):raise ValueError('无效的新任务编号')
        self.folder=self.root/'jobs'/self.name;self.options=job.get('options') or {};self.progress=0
        self.metrics={'version':VERSION,'stage_wall_s':{},'stage_cache_hits':[], 'human_reviewed':False}
        self.warnings=['机器译配草稿，尚未经过人工试听。素材改编和声音使用授权状态未知，未标记为已授权。',
            '说话人是声学分组；Demucs可能残留原对白或分离伪影。']
        self.started=time.perf_counter();self.ledger={}
        from website_recovery import normalize_policy
        self.website_policy=normalize_policy(self.options.get('website_recovery',{}))
        self._owned_website_text_argv=None
        self.deadline=self.started+14400
        self._last_space_check=-math.inf;self._space_low=False
        self._state_lock=threading.RLock()
        self.py=self.root/'vendor/index-tts/.venv/bin/python'
        self.mlx_py=self.root/'vendor/mlx-indextts2-inference/.venv/bin/python'
        self.asr_py=self.root/'envs/asr/bin/python'
    def check(self):
        if self.cancelled():raise RuntimeError('任务已取消，已保留中间结果')
        if time.perf_counter()>self.deadline:raise RuntimeError('本次运行已达到4小时时间预算，已保留断点')
        if self.resource_low():raise RuntimeError('磁盘空间不足：运行中剩余空间低于2GiB，已停止并保留原素材')
    def resource_low(self):
        if time.perf_counter()-self._last_space_check>1:
            self._last_space_check=time.perf_counter()
            self._space_low=shutil.disk_usage(self.root).free<2*1024**3
        return self._space_low
    def emit(self,stage,progress,details=None):
        # Keep the monotonic update and its callback in one critical section.
        # Otherwise a slower thread can publish an older progress value last.
        with self._state_lock:
            self.check();self.progress=max(self.progress,min(100,float(progress)))
            self.update(stage,self.progress,details)
    def command(self,stage,start,end,argv,env=None,on_line=None,timeout=14400,
                should_abort=None,report_progress=True,review_exit=None):
        def check_command():
            self.check()
            if should_abort and should_abort():raise RuntimeError('并行分析另一项失败，已停止当前步骤')
        check_command()
        if report_progress:self.emit(stage,start)
        def line(text):
            check_command()
            if on_line:on_line(text)
        owned_translation=([str(x) for x in argv]==[str(x) for x in self.internal('_translate')]
            or tuple(str(x) for x in argv)==self._owned_website_text_argv)
        nonce=secrets.token_hex(32) if owned_translation else None
        observed=[];confirmed=[]
        original_line=line
        def line(text):
            if nonce:
                event=_failure_event(text,nonce)
                if event:
                    (observed if event[0]=='proxy_transport_failure' else confirmed).append(event[1])
            original_line(text)
        environment={**os.environ,**(env or {})}
        # Never inherit a prior child's capability token.
        environment.pop(STAGE_EVENT_NONCE_ENV,None)
        if nonce:environment[STAGE_EVENT_NONCE_ENV]=nonce
        environment['PATH']='/opt/homebrew/bin:/opt/homebrew/opt/ffmpeg-full/bin:'+environment.get('PATH','/usr/bin:/bin')
        kwargs={'cwd':str(self.root),'env':environment,'on_line':line,
            'timeout':min(timeout,max(1,self.deadline-time.perf_counter())),
            'should_abort':lambda:self.resource_low() or bool(should_abort and should_abort())}
        began=time.perf_counter()
        try:result=self.runner([str(x) for x in argv],**kwargs)
        except Exception:
            self.check()
            raise
        finally:
            with self._state_lock:
                timings=self.metrics['stage_wall_s'];timings[stage]=timings.get(stage,0)+time.perf_counter()-began
        check_command()
        if result.get('returncode')!=0:
            # Code 3 is reserved by the subtitle CLI for a fully written review
            # result. Only its caller can supply the current-input validator;
            # crashes, signals and transport errors keep the normal failure path.
            if result.get('returncode')==3 and review_exit is not None and review_exit() is True:
                if report_progress:self.emit(stage,end)
                return result
            # The final exception must itself be a transport failure. Another
            # concurrent batch's transport observation cannot override a hard
            # translation/review rejection raised by the actual failing batch.
            evidence=confirmed[-1] if confirmed else None
            if evidence is not None and evidence in observed:
                raise StageFailure(stage,stage+'未完成；已保留任务数据和日志',evidence)
            raise RuntimeError(stage+'未完成；已保留任务数据和日志')
        if report_progress:self.emit(stage,end)
        return result
    def cached(self,name,signature,outputs):
        item=self.ledger.get(name)
        if item and item.get('outputs') and item.get('fingerprint')==key(signature) and all(Path(p['path']).is_file() and digest(p['path'])==p['sha256'] for p in item['outputs']):
            with self._state_lock:self.metrics['stage_cache_hits'].append(name)
            return True
        return False
    def remember(self,name,signature,outputs):
        with self._state_lock:
            self.ledger[name]={'fingerprint':key(signature),'outputs':[{'path':str(Path(p).resolve()),'sha256':digest(p)} for p in outputs]}
            save(self.folder/'pipeline_receipts.json',self.ledger)
    def export_phase_signature(self,candidate,adapter,dependencies,phase,video_name):
        value={'edl':digest(candidate/'edit_decisions.json'),'phase':phase,'adapter':digest(adapter),
            'candidate':str(candidate.resolve()),**dependencies}
        if phase in ('render','verify'):value['master']=digest(candidate/'zh_mix.wav')
        if phase=='verify':
            value['video']=digest(candidate/video_name)
            value['verifier']=digest(script(self.root,'verify_natural_compact.py'))
        return value
    def recover_encoded_master(self,candidate,adapter,dependencies):
        """The local helper proves actual encoded failure and a complete remux.

        Reconcile durable completion before looking at old mix/render receipts:
        a process can stop after committing media but before updating this ledger.
        The ordinary final verifier is never marked complete by this operation.
        """
        prior_path=candidate/'audio_master_recovery.json'
        prior=read(prior_path) if prior_path.is_file() else {}
        self.command('自动校正编码后的音频响度',self.progress,self.progress,
            [self.asr_py,'-B',script(self.root,'encoded_master_recovery.py'),
             '--root',self.root,'--job',self.name,'--output',candidate],timeout=900)
        report=read(candidate/'audio_master_recovery.json')
        if not report.get('changed'):return False
        expected={'job':self.name,'candidate':str(candidate.resolve()),
            'edl_sha256':digest(candidate/'edit_decisions.json'),
            'master_sha256':digest(candidate/'zh_mix.wav'),
            'output_sha256':digest(candidate/'group_zh_candidate.mp4')}
        if (any(report.get(k)!=v for k,v in expected.items())
                or report.get('video_bitstream_unchanged') is not True
                or (candidate/'audio_master_recovery.pending.json').exists()):
            raise RuntimeError('音频回退收据未绑定当前完整媒体；已保留检查记录')
        self.remember('export_mix',self.export_phase_signature(candidate,adapter,dependencies,'mix',
            'group_zh_candidate.mp4'),[candidate/name for name in
                ('group_mix_report.json','zh_mix.wav','zh_mix.m4a','zh.srt','de.srt')])
        self.remember('export_render',self.export_phase_signature(candidate,adapter,dependencies,'render',
            'group_zh_candidate.mp4'),[candidate/'group_export.json',candidate/'group_zh_candidate.mp4'])
        self.metrics['encoded_master_recovery']={'report':str(candidate/'audio_master_recovery.json'),
            'report_sha256':digest(candidate/'audio_master_recovery.json'),
            'video_bitstream_unchanged':True,'full_verifier_still_required':True}
        return {**report,'reused':prior.get('changed') is True
            and prior.get('output_sha256')==report['output_sha256']}
    def internal(self,action,*args):return [self.py,Path(__file__).resolve(),action,'--root',self.root,'--job',self.name,*args]
    def translate_with_recovery(self,planned,on_line):
        from failure_triage import checkpoint_marker
        before=checkpoint_marker(self.root,self.name)
        try:
            return self.command('机器翻译与第二遍审校',46,58,self.internal('_translate'),on_line=on_line,timeout=7200)
        except RuntimeError as error:
            self.check()
            if isinstance(error,StageFailure) or not self.website_policy['enabled'] or self.website_policy['source_mode']!='contextual':
                raise
            if checkpoint_marker(self.root,self.name)==before:
                raise
            from translation_recovery import pending
            if not pending(self.folder,planned):
                raise
            if not self.text_repair('translation'):
                raise
            # The existing outer loop rebuilds identities and reuses all
            # verified stage/batch outputs; it also enforces the shared limit.
            raise WebsiteRepairReady('translation_review',{'bound_rejection_repaired':True}) from None
    def text_repair(self,phase,ids=None,reason=None):
        argv=[self.asr_py,'-B',script(self.root,'website_recovery.py'),phase,
            '--root',self.root,'--job',self.name,'--policy-json',json.dumps(self.website_policy)]
        if ids is not None:argv.extend(['--ids',json.dumps(ids)])
        if reason is not None:argv.extend(['--reason',reason])
        hints=self.folder/'translation/source_review_hints.json'
        before=digest(hints) if hints.is_file() else None
        self._owned_website_text_argv=tuple(str(x) for x in argv)
        try:
            self.command('Proxy处理局部译稿并独立复核',self.progress,self.progress,argv,timeout=900 if phase=='translation' else 600)
        finally:self._owned_website_text_argv=None
        changed=(digest(hints) if hints.is_file() else None)!=before
        if phase=='compact' and not changed:
            raise RuntimeError('Proxy已核对该句，但没有可进一步采用的压短修订；请在网站核对原句或译稿，已有声音均保留')
        return changed
    def media_recovery(self):
        return __import__('media_recovery')
    def align_group_subtitles(self,group_plan,stable_manifest,align_dir):
        alignment_index=align_dir/'alignment_manifest.json'
        def review_exit():
            self.media_recovery().alignment_failure_scope(self.root,self.folder,group_plan,
                alignment_index,expected_manifest_path=stable_manifest)
            return True
        self.command('对齐逐句双语字幕',79,81,[self.asr_py,'-B',script(self.root,'generic_group_alignment.py'),
            '--root',self.root,'--plan',group_plan,'--manifest',stable_manifest,'--output',align_dir,
            '--cache-dir',self.root/'cache/group_subtitle_whisper_v1','--progress-json'],
            review_exit=review_exit)
        return read(alignment_index)
    def space_reserve(self,config):
        return (self.website_policy['reserve_gib'] if self.website_policy['enabled']
            else float(config.get('minimum_free_gib',8)))
    def current_space_preflight(self,config,source,source_sha,prepare_signature,planned_output_duration_s=None):
        """Credit only producer stages that will pass the current cache check."""
        if self.space_reserve(config)!=8:
            return space_preflight(max(source['duration_s'],planned_output_duration_s or 0),
                shutil.disk_usage(self.root).free,self.space_reserve(config))
        from resume_storage import preflight,cleanup_candidates,REQUIRED_OUTPUTS,BACKGROUND_TEMPORARIES
        stages={}
        def proof(name,signature):
            item=self.ledger.get(name,{})
            if item.get('fingerprint')!=key(signature):return False
            expected={str(self.folder/p) for p in REQUIRED_OUTPUTS[name]}
            outputs=item.get('outputs')
            if not isinstance(outputs,list) or {p.get('path') for p in outputs}!=expected:return False
            files=[]
            for saved in outputs:
                path=Path(saved['path'])
                if not path.is_file() or any(p.is_symlink() for p in (path,*path.parents)):return False
                actual=digest(path)
                if actual!=saved.get('sha256'):return False
                files.append({**saved,'actual_sha256':actual,'bytes':path.stat().st_size,
                    'regular_file':True,'is_symlink':False})
            stages[name]={'source_sha256':source_sha,'fingerprint':item['fingerprint'],
                'expected_fingerprint':key(signature),'reusable':True,'will_rebuild':False,'outputs':files}
            return True
        def refresh():
            stages.clear()
            if proof('prepare',prepare_signature):
                signature={'audio':digest(self.folder/'audio/original.wav'),'weight_sha256':DEMUCS_SHA,
                    'segment':5,'shifts':0,'overlap':.25,'device':'mps'}
                if proof('separation',signature):
                    signature={'no_vocals_sha':digest(self.folder/'separated/htdemucs/original/no_vocals.wav'),
                        'duration':source['duration_s'],'code':implementation_signature(('background_job',)),
                        'gain_body_db':10,'gain_titles_db':4}
                    proof('background',signature)
        def estimate():
            return preflight(float(source['duration_s']),shutil.disk_usage(self.root).free,
                job_path=str(self.folder),source_sha256=source_sha,stages=stages,
                planned_output_duration_s=planned_output_duration_s)
        def temporary(path):
            if not path.is_file() or any(p.is_symlink() for p in (path,*path.parents)):return None
            observed=path.stat();sha=digest(path)
            if file_state(path.stat())!=file_state(observed):return None
            return {'path':str(path),'sha256':sha,'actual_sha256':sha,'bytes':observed.st_size,
                'regular_file':True,'is_symlink':False}
        def file_state(value):
            return (value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns)
        def eligible():
            files=[item for name in BACKGROUND_TEMPORARIES
                   if (item:=temporary(self.folder/name)) is not None]
            return cleanup_candidates(job_path=str(self.folder),source_sha256=source_sha,
                stages=stages,temporary_files=files)
        refresh();result=estimate()
        report=self.folder/('export_space_preflight.json' if planned_output_duration_s is not None else 'space_preflight.json')
        # Persist failures too: an earlier successful report must not describe
        # a later failed estimate. No planned deletion is treated as free space.
        save(report,result)
        if not result['sufficient']:
            candidates=eligible()
            if candidates:
                source_path=Path(source['input'])
                if (not source_path.is_file() or any(p.is_symlink() for p in (source_path,*source_path.parents))
                        or digest(source_path)!=source_sha):
                    raise RuntimeError('原片身份已改变，未清理临时文件')
                receipt=self.folder/'recovery/storage_cleanup'/(str(time.time_ns())+'.json')
                record={'schema':'verified_background_scratch_cleanup_v1','source_sha256':source_sha,
                    'before':result,'candidates':candidates,'removed':[],'status':'checking',
                    'old_assets_deleted':False,'protected_reserve_bytes':8*1024**3}
                save(receipt,record)
                for candidate in candidates:
                    self.check();refresh()
                    if digest(source_path)!=source_sha:raise RuntimeError('清理前原片身份已改变')
                    current=next((item for item in eligible() if item==candidate),None)
                    if current is None:break
                    path=Path(candidate['path']);observed=path.stat()
                    expected={k:candidate[k] for k in ('path','sha256','bytes')}
                    expected.update(actual_sha256=candidate['sha256'],regular_file=True,is_symlink=False)
                    if temporary(path)!=expected:break
                    if path.is_symlink() or file_state(path.stat())!=file_state(observed):break
                    path.unlink()
                    record['removed'].append(candidate);record['status']='removed_verified_scratch'
                    save(receipt,record)
                refresh();result=estimate()
                record.update(status='complete',after=result,actual_free_bytes_after=result['free_bytes'])
                save(receipt,record)
                result['scratch_cleanup_receipt']=str(receipt)
        save(report,result)
        if not result['sufficient']:raise RuntimeError(result['message'])
        return result
    def stages(self):
        self.check()
        mastering=mastering_policy(self.options)
        pilot_limit=self.options.get('voice_pilot_groups',0)
        if type(pilot_limit) is not int or pilot_limit not in (0,2):
            raise ValueError('无效的选音小样设置')
        for executable in (self.py,self.mlx_py,self.asr_py,Path(FFMPEG),Path(FFPROBE)):
            if not executable.is_file():raise RuntimeError('缺少已安装的本地运行环境：'+str(executable))
        requested_language=normalize_language(self.options.get('source_language','de'))
        if self.options.get('target_language','zh')!='zh':raise RuntimeError('当前输出语言为中文')
        episode_max=self.options.get('episode_max_seconds',1800)
        if type(episode_max) not in (int,float) or not math.isfinite(episode_max) or not 1<=episode_max<=1800:
            raise ValueError('每集时长上限必须在1至1800秒以内')
        profile=self.options.get('experimental_profile',DEFAULT_TTS_PROFILE)
        if profile not in PROFILES:raise ValueError('不支持的25步配音配置')
        if not (self.root/'assets/philos_club_serif_wordmark.png').is_file():raise RuntimeError('项目logo素材缺失')
        self.folder.mkdir(parents=True,exist_ok=True)
        marker=self.folder/'web_job.json'
        identity={k:self.job.get(k) for k in ('id','input_path','source_url')}
        if marker.exists() and read(marker).get('identity')!=identity:raise RuntimeError('原任务输入已改变，请新建任务，避免混用旧缓存')
        save(marker,{'identity':identity,'title':self.job.get('title',''),'human_reviewed':False,'rights_verified':False})
        if (self.folder/'pipeline_receipts.json').is_file():self.ledger=read(self.folder/'pipeline_receipts.json')
        for sub in ('audio','logs','outputs','references','translation','tts'):(self.folder/sub).mkdir(exist_ok=True)
        source_file=self.acquire_source()
        source_sha=digest(source_file)
        config=read(self.root/'config/local.json');source_json=self.folder/'source.json'
        prepare_signature={'source_sha256':source_sha,'audio_track':int(self.options.get('audio_track',0)),'recipe':'48k_stereo_16k_asr_v1'}
        if not self.cached('prepare',prepare_signature,[]):
            probe=json.loads(self.command('读取视频信息',5,6,[FFPROBE,'-v','error','-show_streams','-show_format','-of','json',source_file])['stdout'])
            geometry(probe);duration=float(probe['format']['duration'])
            if not math.isfinite(duration) or duration<=0:
                raise RuntimeError('视频时长超出当前本机单任务限制')
            track=prepare_signature['audio_track'];audio=[s for s in probe['streams'] if s['codec_type']=='audio']
            if not 0<=track<len(audio):raise RuntimeError('所选音轨不存在')
            video=next(s for s in probe['streams'] if s['codec_type']=='video')
            if abs(float(video.get('start_time',0)))>.05 or abs(float(audio[track].get('start_time',0)))>.05:
                raise RuntimeError('素材音视频起始时间偏移较大，需先校正后再自动配音')
            save(source_json,{'input':str(source_file.resolve()),'audio_track':track,'duration_s':duration,'probe':probe})
            if duration>episode_max and self.options.get('split_long_source',False):
                return self.process_episodes(config,source_file,source_sha,probe,duration,track,requested_language,episode_max)
            if duration>float(config.get('max_video_seconds',1800)):
                raise RuntimeError('视频较长，请通过网页新建任务，按每集最多30分钟自动分集')
            self.metrics['space_preflight']=space_preflight(duration,shutil.disk_usage(self.root).free,
                self.space_reserve(config))
            save(self.folder/'space_preflight.json',self.metrics['space_preflight'])
            self.command('提取原声音轨',6,9,[FFMPEG,'-v','error','-y','-i',source_file,'-map',f'0:a:{track}','-vn','-ac','2','-ar','48000','-c:a','pcm_s24le',self.folder/'audio/original.wav'])
            self.command('准备语音识别音轨',9,10,[FFMPEG,'-v','error','-y','-i',self.folder/'audio/original.wav','-ac','1','-ar','16000','-c:a','pcm_s16le',self.folder/'audio/asr16k.wav'])
            self.remember('prepare',prepare_signature,[source_json,self.folder/'audio/original.wav',self.folder/'audio/asr16k.wav'])
        source=read(source_json);geometry(source['probe'])
        if 'space_preflight' not in self.metrics:
            self.metrics['space_preflight']=self.current_space_preflight(config,source,source_sha,prepare_signature)
            save(self.folder/'space_preflight.json',self.metrics['space_preflight'])
        save(self.folder/'processing_request.json',{'processing_authorized':True,'scope':'private_local_evaluation',
            'requested_via':'explicit local web start','source_rights_verified':False,'human_reviewed':False,
            'third_party_rights_independently_verified':False,'publication_performed':False})
        save(self.folder/'rights.json',{'media_authorized':None,'voice_authorized':None,'scope':'private draft; source rights unverified'})
        asr=self._analyze_audio(config)
        detected=read(asr)
        actual_language=normalize_language(detected.get('source_language') or detected.get('language') or requested_language,allow_auto=False)
        metadata={**source_language_metadata(actual_language),'requested_source_language':requested_language}
        if any(source.get(k)!=v for k,v in metadata.items()):
            source.update(metadata);save(source_json,source)
            self.remember('prepare',prepare_signature,[source_json,self.folder/'audio/original.wav',self.folder/'audio/asr16k.wav'])
        plan=self.folder/'segments.plan.json';speaker_signature={'asr_sha':digest(asr),'vocals_sha':digest(self.folder/'separated/htdemucs/original/vocals.wav'),'code':digest(script(self.root,'speaker_plan.py')),
            'sentence_regroup':'raw_assignments_sentence24_v1','regroup_code':speaker_implementation_signature()}
        correction=self.folder/'speaker_corrections.json'
        if correction.is_file():
            helper=script(self.root,'speaker_corrections.py')
            speaker_signature.update(correction_sha256=digest(correction),correction_helper_sha256=digest(helper))
        if not self.cached('speakers',speaker_signature,[]):
            if len(read(asr)['segments'])==1:argv=self.internal('_single_speaker')
            else:argv=[self.py,script(self.root,'speaker_plan.py'),'--root',self.root,'--job',self.name]
            self.command('区分说话人与选择参考声音',40,45,argv)
            self.command('检查参考声音',45,46,self.internal('_check_speakers'))
            self.command('按原文完整句重新编排',46,46,self.internal('_regroup_sentences'))
            refs=[self.root/s['reference'] for s in read(self.folder/'speakers.json').values()]
            self.remember('speakers',speaker_signature,[plan,self.folder/'speakers.json',self.folder/'sentence_group_receipt.json',*refs])
        planned,source_revision=reviewed_source_plan(self.folder,plan,digest(Path(source['input'])))
        if source_revision:
            self.metrics['source_plan_revision']=source_revision
            self.warnings.append('已使用有原片证据的派生源文修订；原始识别和计划均保留。')
            if source_revision['unresolved']:
                self.warnings.append('部分原文仍有待核对：'+','.join(source_revision['unresolved']))
        translation_input=self.folder/'translation/input.json'
        if (source_revision and source_revision['blocking_unresolved']
                and self.website_policy['enabled'] and self.website_policy['source_mode']=='contextual'):
            save(translation_input,{'segments':planned,**source_language_metadata(actual_language)})
            while source_revision['blocking_unresolved']:
                before=tuple(source_revision['blocking_unresolved'])
                self.text_repair('source')
                planned,source_revision=reviewed_source_plan(self.folder,plan,digest(Path(source['input'])))
                if tuple(source_revision['blocking_unresolved'])==before:
                    raise RuntimeError('Proxy未能消除当前原词阻碍，请在网站核对原句后继续')
            self.metrics['source_plan_revision']=source_revision
        preview=float(self.options.get('preview_seconds',0) or 0)
        if preview:
            if not math.isfinite(preview) or preview<=0:raise ValueError('预览长度无效')
            planned=[r for r in planned if r['start_s']<preview] or planned[:1]
            self.warnings.append('本任务仅生成完整台词边界内的前段预览，未生成后续整片。')
        save(translation_input,{'segments':planned,**source_language_metadata(actual_language)})
        old_group_plan=self.folder/'tts/semantic_group_plan.json'
        previous_group_plan=None
        if old_group_plan.is_file():
            previous_group_plan=self.folder/'recovery/group_plan_snapshots'/(digest(old_group_plan)+'.json')
            if not previous_group_plan.exists():save(previous_group_plan,read(old_group_plan))
        translated=self.folder/'segments.json';translation_report=self.folder/'translation/report.json'
        tsig={'plan':key(planned),'speakers':digest(self.folder/'speakers.json'),
            'source_review_hints_sha256':digest(self.folder/'translation/source_review_hints.json') if (self.folder/'translation/source_review_hints.json').is_file() else None,
            'prior_review_reuse_sha256':digest(self.folder/'translation/prior_review_reuse.json') if (self.folder/'translation/prior_review_reuse.json').is_file() else None,
            'prior_review_reuse_code':digest(Path(__file__).with_name('translation_reuse.py')) if (self.folder/'translation/prior_review_reuse.json').is_file() else None,
            'source_plan_revision_sha256':digest(self.folder/'source_plan_revision.json') if (self.folder/'source_plan_revision.json').is_file() else None,
            'source_language':actual_language,'proxy_client_code':digest(Path(__file__).with_name('proxy_client.py')),'method':'actual_two_pass_numeric_checks'}
        if not self.cached('translation',tsig,[]):
            def tr_line(text):
                try:event=json.loads(text)
                except (ValueError,TypeError):return
                if event.get('event')=='translation_progress':self.emit('机器翻译与第二遍审校',46+12*event['completed']/max(1,event['total']),{'completed':event['completed'],'total':event['total'],'human_reviewed':False})
            self.translate_with_recovery(planned,tr_line)
            validate_translations(planned,read(translated)['segments'],read(translation_report))
            self.remember('translation',tsig,[translated,translation_report])
        validate_translations(planned,read(translated)['segments'],read(translation_report))
        from translation_recovery import source_pending
        source_rechecks=source_pending(self.folder,planned)
        if source_rechecks:
            if self.website_policy['enabled'] and self.website_policy['source_mode']=='contextual':
                if self.text_repair('translation',ids=source_rechecks[:8]):
                    raise WebsiteRepairReady('source_recheck_before_tts',{'affected_count':min(8,len(source_rechecks))})
            raise RuntimeError('原文识别疑点仍待复核，未启动配音：'+','.join(source_rechecks))
        if previous_group_plan and self.website_policy['enabled']:
            self.media_recovery().rebind_no_join(self.root,self.folder,previous_group_plan)
        if source_revision and source_revision['blocking_unresolved']:
            raise RuntimeError('机器译稿已保存；原文词义仍待核对：'+','.join(source_revision['blocking_unresolved'])+
                '。未启动配音或最终导出，不能标记为最终完成。')
        rows=read(translated)['segments']
        subtitle_preflight=self.folder/'subtitle_preflight.json'
        layout_signature={'segments':digest(translated),'renderer':digest(self.root/'scripts/compact_edit.py'),
            'checker':digest(script(self.root,'subtitle_preflight.py'))}
        if not self.cached('subtitle_layout',layout_signature,[]):
            self.command('检查双语字幕排版',59,59,[self.py,'-B',script(self.root,'subtitle_preflight.py'),
                '--root',self.root,'--job',self.name])
            layout=read(subtitle_preflight)
            if (layout.get('phase')!='complete' or layout.get('source_rows')!=len(rows)
                    or layout.get('segments_sha256')!=digest(translated)
                    or layout.get('renderer_sha256')!=digest(self.root/'scripts/compact_edit.py')):
                raise RuntimeError('双语字幕预检未完整通过，已保留译稿')
            self.remember('subtitle_layout',layout_signature,[subtitle_preflight])
        grouping=self.options.get('tts_grouping','sentence_v1')
        if grouping not in ('sentence_v1',DEFAULT_TTS_GROUPING):raise ValueError('无效的连续配音策略')
        background=self.folder/'audio/background_independent.wav'
        bsig={'no_vocals_sha':digest(self.folder/'separated/htdemucs/original/no_vocals.wav'),'duration':source['duration_s'],'code':implementation_signature(('background_job',)),'gain_body_db':10,'gain_titles_db':4}
        if not self.cached('background',bsig,[]):
            self.command('准备独立场景背景',58,60,self.internal('_background'),timeout=1800)
            self.remember('background',bsig,[background,self.folder/'audio/background_report.json'])
        receipt=make_receipt(self.root,self.folder,background)
        emotion=self.options.get('emotion_mode','reference')
        if emotion not in ('reference','calm'):raise ValueError('无效语气设置')
        grouped=grouping==DEFAULT_TTS_GROUPING
        group_plan=self.folder/'tts/semantic_group_plan.json'
        tts_job=self.name;tts_rows=rows
        if grouped:
            self.command('规划连续语意配音',60,61,[self.py,'-B',script(self.root,'generic_group_plan.py'),
                '--root',self.root,'--job',self.name,'--out',group_plan,'--profile',profile,
                '--profile-options-json',json.dumps({'emotion_mode':emotion}),'--create-tts-job'])
            planned_groups=read(group_plan)
            if (planned_groups.get('full_tts_group_plan_complete') is not True
                    or planned_groups.get('ready_for_tts') is not True
                    or planned_groups.get('source_rows')!=rows):
                raise RuntimeError('连续配音分组仍需核对；已保留全部原句，未截字或遗漏字幕')
            tts_job=planned_groups['tts_job']['id']
            tts_rows=[g['tts_row'] for g in planned_groups['tts_groups']]
            self.metrics['continuous_grouping']={'mode':grouping,'source_rows':len(rows),
                'tts_groups':len(tts_rows),'plan_sha256':digest(group_plan),
                'original_source_rows_preserved':True,'new_internal_silence_s':0.,
                'human_reviewed':False}
        native_out=self.folder/('tts/semantic_native25' if grouped else 'tts/native25')
        native_manifest=native_out/'manifest.json'
        tts_folder=self.root/'jobs'/tts_job
        tts_sig=tts_signature(tts_folder/'segments.json',tts_folder/'speakers.json',profile,emotion,script(self.root,'mlx_tts.py'))
        # Publish complete original groups during this same runner lifetime.
        # This creates no additional synthesis, model load, cut or join.
        pilot=None;pilot_done=False;pilot_payload=None
        old_pilot=self.folder/'outputs/voice_pilot/pilot_receipt.json'
        if pilot_limit and grouped and old_pilot.is_file():
            published=read(old_pilot)
            if (published.get('schema')=='early_voice_pilot_v1' and published.get('job_id')==self.name
                    and published.get('human_reviewed') is False and published.get('complete_job') is False
                    and (published.get('source_tts_job')!=tts_job
                        or published.get('full_selected_count')!=len(tts_rows))):
                # Historical small samples remain available; they are never
                # republished as evidence for a revised group/audio identity.
                pilot_limit=0
                self.metrics['early_voice_pilot']={'enabled':False,'historical_pilot_preserved':True,
                    'reason':'source_group_plan_changed','current_voice_approval_claimed':False}
        if pilot_limit and grouped:
            helper=script(self.root,'early_voice_pilot.py')
            pilot=_voice_pilot_module(self.root)
            self.metrics['early_voice_pilot']={'enabled':True,'limit':pilot_limit,
                'helper_sha256':digest(helper),'published_groups':0,'human_reviewed':False,
                'complete_job':False,'additional_model_loads':0,'additional_synthesis_calls':0,
                'background_included':False,'validation_copy_wall_s':0.}
        def publish_pilot():
            nonlocal pilot_done,pilot_payload
            if pilot is None or pilot_done:return
            began=time.perf_counter()
            payload=pilot.publish_available(self.root,self.folder,native_manifest,tts_job,tts_rows,pilot_limit)
            self.metrics['early_voice_pilot']['validation_copy_wall_s']+=time.perf_counter()-began
            if payload is None:return
            if pilot_payload is not None and payload['receipt_sha256']==pilot_payload['receipt_sha256']:return
            pilot_payload=payload
            count=len(payload['artifacts']);now=time.perf_counter()-self.started
            metrics=self.metrics['early_voice_pilot']
            if not metrics['published_groups']:metrics['first_ready_wall_s']=now
            metrics.update(published_groups=count,last_ready_wall_s=now,
                receipt_sha256=payload['receipt_sha256'])
            pilot_done=count==min(pilot_limit,len(tts_rows))
            self.emit('选音小样可试听，继续生成完整配音',self.progress,{'voice_pilot':payload})
        # Always let the stdlib-only native planner validate the current model,
        # helper/version/reference identities. All verified hits import no model.
        def tts_line(text):
            try:event=json.loads(text)
            except (ValueError,TypeError):return
            if event.get('event')=='utterance_complete':
                done=len(tts_rows)-int(event['remaining']);self.emit('生成中文配音草稿',61+18*done/max(1,len(tts_rows)),{'completed':done,'total':len(tts_rows),'source_rows':len(rows),'segment_id':event.get('segment_id'),'human_reviewed':False})
            elif event.get('event') in ('model_loaded','reference_ready','finished'):
                self.emit('生成中文配音草稿',self.progress,{'tts_event':event,'human_reviewed':False})
            if event.get('event') in ('utterance_complete','model_loaded','finished'):publish_pilot()
        tts_argv=[self.mlx_py,'-B',script(self.root,'mlx_tts.py'),'--root',self.root,'--job',tts_job,
            '--output',native_out,'--max-segments',len(tts_rows),'--experimental-profile',profile,'--emotion-mode',emotion]
        if grouped:tts_argv.extend(['--cache-dir',planned_groups['tts_job']['cache_dir']])
        self.command('生成中文配音草稿',61,79,tts_argv,on_line=tts_line)
        manifest=read(native_manifest)
        publish_pilot()
        if pilot_payload is not None:
            pilot_receipt=read(pilot_payload['receipt_path'])
            final_rows={row['segment_id']:row for row in manifest['utterances']}
            if any(final_rows[item['segment_id']]['cache_key']!=item['cache_key']
                    or final_rows[item['segment_id']]['sha256']!=item['sha256']
                    for item in pilot_receipt['groups']):
                raise RuntimeError('试听小样与最终配音身份不一致，不继续导出')
            self.metrics['early_voice_pilot']['final_native_audio_identical']=True
        save(self.folder/'join_groups.native.json',reviewed_natural_groups(self.root,self.folder,rows,profile,emotion) if not grouped else infer_groups(rows))
        if manifest.get('phase')!='complete' or manifest.get('pending_unique_keys')!=0:raise RuntimeError('配音未完整生成，不继续导出')
        logits_usage=raw_logits_usage(manifest)
        if logits_usage is not None:self.metrics['raw_logits_checks']=logits_usage
        self.remember('native_tts',tts_sig,[native_manifest,*[Path(r['audio']) for r in manifest['utterances']]])
        export_manifest=self.resolve_native_audio(native_manifest,grouped=grouped)
        if grouped:
            import acoustic_fallback
            base_stable_manifest=stable_group_manifest(self.folder,native_manifest)
            export_manifest=acoustic_fallback.resolve(self.root,self.folder,group_plan,base_stable_manifest)
        if export_manifest!=native_manifest:manifest=read(export_manifest)
        adapter=script(self.root,'group_compact_adapter.py' if grouped else 'natural_compact_adapter.py')
        export_dependencies={'mastering':mastering_signature(self.root,adapter,mastering),
            'renderer':digest(self.root/'scripts/compact_edit.py'),
            'size_policy':digest(self.root/'scripts/export_size.py'),'max_bytes_exclusive':50_000_000}
        if grouped:
            export_dependencies['background_adapter']=digest(script(self.root,'natural_compact_adapter.py'))
        export_identity={'audio':[(r['segment_id'],r['cache_key'],r['sha256']) for r in manifest['utterances']],
            'receipt':digest(receipt),'adapter':digest(adapter),'segments':digest(translated),
            'natural_join_groups':digest(self.folder/'join_groups.native.json'),**export_dependencies}
        if export_manifest!=native_manifest and not grouped:
            export_identity.update(native_audio_selection=digest(self.folder/'native_audio_selections.json'),
                native_audio_resolver=digest(script(self.root,'resolve_native_export.py')))
        subtitle_plan=self.folder/'subtitle_pages.json'
        if not grouped and subtitle_plan.is_file():
            export_identity.update(subtitle_pages=digest(subtitle_plan),
                subtitle_page_helper=digest(script(self.root,'subtitle_pages.py')))
        picture_holds=self.folder/'picture_hold_review.json'
        if not grouped and picture_holds.is_file():
            export_identity.update(picture_hold_review=digest(picture_holds),
                picture_hold_helper=digest(script(self.root,'picture_holds.py')))
        if grouped:
            stable_manifest=stable_group_manifest(self.folder,export_manifest)
            # A second local repair must extend the existing immutable
            # composition, preserving any earlier successful Torch fallback.
            repair_base=export_manifest if manifest.get('schema')==acoustic_fallback.COMPOSITION else base_stable_manifest
            if manifest.get('schema')==acoustic_fallback.COMPOSITION:
                torch_groups=sum(row['content'].get('schema')=='continuous_utterance_v1'
                    for row in manifest['utterances'])
                self.metrics['audio_composition']={'one_inference_run':False,
                    'torch25_fallback_groups':torch_groups,'preserved_native_groups':len(manifest['utterances'])-torch_groups,
                    'same_approved_text_and_reference':True,'human_listened':False}
                export_identity['acoustic_fallback_resolver']=digest(script(self.root,'acoustic_fallback.py'))
            align_dir=self.folder/'tts'/('subtitle_alignment_'+digest(group_plan)[:12]+'_'+stable_manifest.parent.name[:12])
            alignment_index=align_dir/'alignment_manifest.json'
            alignment=self.align_group_subtitles(group_plan,stable_manifest,align_dir)
            if alignment.get('all_groups_ready') is not True:
                if self.website_policy['enabled']:
                    repaired=self.media_recovery().repair_alignment(self.root,self.folder,group_plan,alignment_index)
                    if repaired.get('changed'):raise WebsiteRepairReady('split_failed_groups',repaired)
                    ids=repaired.get('unresolved_ids',[])
                    if ids:
                        self.text_repair('compact',ids[:8],'acoustic')
                        raise WebsiteRepairReady('compact_failed_speech',{'source_rows':ids[:8]})
                raise RuntimeError('部分连续配音的字幕边界需核对；已保留全部台词和完整声音')
            if self.website_policy['enabled']:
                quality=self.folder/'tts'/('audio_quality_'+key(group_manifest_identity(manifest))[:16]+'.json')
                self.command('检查配音声学异常',81,81,[self.asr_py,'-B',script(self.root,'media_recovery.py'),
                    'audio-quality','--root',self.root,'--plan',group_plan,'--manifest',repair_base,
                    '--output',quality],timeout=1800)
                checked=read(quality)
                if checked.get('complete') is not True:raise RuntimeError('配音声学检查尚未完成，已保留全部音频')
                self.metrics['audio_quality']={k:checked[k] for k in checked if k not in {'rows','source_rows','flagged_groups'}}
                ids=checked.get('needs_repair_ids',[])
                if ids:
                    bad_groups=set(checked.get('needs_repair_groups',[]))
                    if any(row['segment_id'] in bad_groups and
                            row['content'].get('schema')!='native_mlx_whole_utterance_v1'
                            for row in manifest['utterances']):
                        raise RuntimeError('备用配音仍未通过声学检查；已保留两版声音、完整译稿和逐句证据')
                    # A waveform failure is not evidence that the approved
                    # translation needs editing. Retry only those intact groups
                    # with the preserved formal Torch25 backend, then rerun all
                    # alignment/media checks on the explicit composition.
                    request=acoustic_fallback.prepare(self.root,self.folder,group_plan,
                        repair_base,quality)
                    self.command('本机重新生成异常配音',self.progress,self.progress,request['argv'],timeout=1800)
                    repaired=acoustic_fallback.finish(self.root,self.folder,group_plan,
                        repair_base,quality,Path(request['fallback_manifest']))
                    raise WebsiteRepairReady('regenerate_failed_speech',repaired)
            export_identity.update(group_plan=digest(group_plan),alignment_index=digest(alignment_index))
            hold_policy=self.folder/'tts/picture_holds.json'
            export_identity['picture_holds']=digest(hold_policy) if hold_policy.is_file() else None
            run_metrics_path=align_dir/'alignment_run_metrics.json'
            alignment_run=read(run_metrics_path) if run_metrics_path.is_file() else alignment
            self.metrics['continuous_grouping']['subtitle_alignment']={k:alignment_run[k] for k in
                ('wall_s','asr_model_loaded','asr_model_load_s','asr_inference_s','asr_cache_hits','complete_index_reused') if k in alignment_run}
        candidate=self.folder/'outputs'/(('groups_' if grouped else 'natural_')+key(export_identity)[:12])
        audit_name='group_adapter_report.json' if grouped else 'adapter_report.json'
        if not (candidate/audit_name).is_file():
            if grouped:
                argv=[self.py,'-B',adapter,'prepare','--root',self.root,'--plan',group_plan,
                    '--alignment-index',alignment_index,'--source',source_json,'--receipt',receipt,'--output',candidate,'--include-intro']
            else:
                argv=[self.py,'-B',adapter,'prepare','--root',self.root,'--job',self.name,'--output',candidate,'--receipt',receipt,'--manifest',export_manifest,'--join-groups',self.folder/'join_groups.native.json','--allow-job-output']
            self.command('编排完整语意与双语字幕',81,83,argv)
        audit=read(candidate/audit_name)
        if audit['issues']:
            save(self.folder/'timing_review_required.json',{'issues':audit['issues'],'human_reviewed':False,
                'audio_truncated':False,'source_segments_sha256':digest(translated),
                'plan_sha256':digest(group_plan) if grouped else None})
            if grouped and self.website_policy['enabled'] and self.website_policy['picture_holds']:
                repair_result=self.folder/'tts/picture_recovery_result.json'
                self.command('检查局部画面并保留完整配音',self.progress,self.progress,
                    [self.asr_py,'-B',script(self.root,'media_recovery.py'),'picture-holds','--root',self.root,
                     '--plan',group_plan,'--source',source_json,'--edl',candidate/'edit_decisions.json',
                     '--audit',candidate/audit_name,'--policy-json',json.dumps(self.website_policy),
                     '--output',repair_result],timeout=900)
                repaired=read(repair_result)
                if repaired.get('changed'):raise WebsiteRepairReady('reviewed_picture_holds',repaired)
                ids=repaired.get('overlong_ids',[])
                if ids:
                    self.text_repair('compact',ids[:8],'timing')
                    raise WebsiteRepairReady('compact_picture_overrun',{'source_rows':ids[:8]})
            raise RuntimeError('部分中文台词超出安全画面时间；已保留完整音频和逐句清单，未截字或强行加速')
        if grouped and any((candidate/name).is_file() for name in
                ('audio_master_recovery.pending.json','audio_master_recovery.json')):
            self.recover_encoded_master(candidate,adapter,export_dependencies)
        for phase,stage,lo,hi in [('mix','混合连续背景与中文配音',83,89),('render','导出540p双语视频',89,97),('verify','检查完整媒体',97,99)]:
            names=({'mix':'group_mix_report.json','render':'group_export.json','verify':'group_verification.json'} if grouped
                else {'mix':'natural_mix_report.json','render':'compact_export.json','verify':'verification.json'})
            video_name='group_zh_candidate.mp4' if grouped else 'landscape_natural_candidate.mp4'
            proof=candidate/names[phase]
            phase_sig=self.export_phase_signature(candidate,adapter,export_dependencies,phase,video_name)
            # Audio/video proof also binds their actual bytes before reuse.
            cached=self.cached('export_'+phase,phase_sig,[])
            if phase in ('render','verify') and (candidate/video_name).is_file() and (candidate/video_name).stat().st_size>=50_000_000:
                cached=False
            if not cached:
                if phase=='mix':
                    # New speech can be longer than the source estimate. Check
                    # the actual EDL before allocating any future mix/render WAVs.
                    current_edl=read(candidate/'edit_decisions.json')
                    actual_duration=max(float(audit['duration_s']),float(current_edl['duration_s']),
                        float(current_edl['frames'])/float(current_edl['fps']))
                    self.metrics['export_space_preflight']=self.current_space_preflight(config,source,
                        source_sha,prepare_signature,planned_output_duration_s=actual_duration)
                    save(self.folder/'export_space_preflight.json',self.metrics['export_space_preflight'])
                def media_line(text):
                    try:event=json.loads(text)
                    except (ValueError,TypeError):return
                    if event.get('event')!='render_progress':return
                    state=event.get('phase')
                    if state=='extracting_clips':fraction=.55*event.get('clips_completed',0)/max(1,event.get('clips_total',1))
                    elif state=='concatenating_clips':fraction=.56
                    elif state=='burning_subtitles_and_muxing_audio':fraction=.6+.4*event.get('current_fraction',0)
                    elif state in ('compressing_video_pass_1','compressing_video_pass_2'):fraction=.96
                    elif state=='finished':fraction=1.
                    else:fraction=0.
                    self.emit(stage,lo+(hi-lo)*min(1,max(0,fraction)),event)
                argv=[self.py,'-B',adapter,phase,'--root',self.root,'--output',candidate]
                if not grouped:argv.extend(['--job',self.name])
                if phase=='mix':argv.extend(['--mastering-policy',mastering])
                if phase=='render' and not grouped:argv.append('--progress-json')
                try:
                    self.command(stage,lo,hi,argv,on_line=media_line if phase=='render' else None)
                except RuntimeError:
                    self.check()
                    # The helper independently scans the current file. Error
                    # wording never authorizes changing an otherwise valid mix.
                    if grouped and phase=='verify' and self.website_policy['enabled']:
                        recovered=self.recover_encoded_master(candidate,adapter,export_dependencies)
                        if recovered and not recovered.get('reused'):
                            raise WebsiteRepairReady('repair_encoded_master',recovered) from None
                    raise
                outputs=[proof]
                if phase=='mix':outputs += [candidate/'zh_mix.wav',candidate/'zh_mix.m4a',candidate/'zh.srt',candidate/'de.srt']
                if phase=='render':outputs += [candidate/video_name]
                self.remember('export_'+phase,phase_sig,outputs)
        completed_mix=read(candidate/('group_mix_report.json' if grouped else 'natural_mix_report.json'))
        completed_verify=read(candidate/('group_verification.json' if grouped else 'verification.json'))
        mastering_proof=completed_mix.get('mastering',{})
        self.metrics['mastering']={'requested_policy':mastering,
            'actual_strategy':mastering_proof.get('actual_strategy','legacy_loudnorm_v1'),
            'fallback_reason':mastering_proof.get('fallback_reason'),
            'constant_pcm_verified':mastering_proof.get('constant_candidate_pcm_passed',False),
            'helper_wall_s':mastering_proof.get('wall_s'),
            'final_verification_status':completed_verify.get('status'),
            'final_encoded_levels':completed_verify.get('encoded_levels') if grouped else completed_verify.get('checks',{}).get('encoded_levels'),
            'final_encoded_scan_retained':True,'subjective_listening_approved':False}
        self.metrics.update(wall_seconds=time.perf_counter()-self.started,source_duration_s=source['duration_s'],
            output_duration_s=audit['duration_s'],segments=len(rows),preview=bool(preview),experimental_profile=profile,
            human_reviewed=False,end_to_end_80_percent_verified=False,tts=read(native_manifest).get('timings',{}))
        save(self.folder/'web_pipeline_metrics.json',self.metrics)
        self.emit('机器译配草稿已完成',100,{'human_reviewed':False,'segments':len(rows)})
        self.metrics.update(**source_language_metadata(actual_language))
        save(self.folder/'web_pipeline_metrics.json',self.metrics)
        source_subtitle='de.srt' if actual_language=='de' else 'source_'+actual_language+'.srt'
        if source_subtitle!='de.srt':shutil.copyfile(candidate/'de.srt',candidate/source_subtitle)
        artifacts=[{'label':label,'path':str((candidate/name).resolve()),'kind':kind} for label,name,kind in [
            ('中文双语视频草稿',video_name,'video'),('中文配音与背景','zh_mix.m4a','audio'),
            ('中文字幕','zh.srt','subtitle'),(language_label(actual_language)+'字幕',source_subtitle,'subtitle'),('工程检查',names['verify'],'report')]]
        for artifact in artifacts:artifact['sha256']=digest(Path(artifact['path']))
        return {'artifacts':artifacts,'source_sha256':source_sha,'duration_s':audit['duration_s'],'warnings':self.warnings,'metrics':self.metrics,
                **source_language_metadata(actual_language),'human_reviewed':False,'status':'machine_reviewed_draft'}
    def process_episodes(self,config,source_file,source_sha,probe,duration,track,requested_language,episode_max,
                         source_window_max=None,replan_attempt=0):
        """Transcribe once, then run speech-safe episodes under the held GPU lock.

        Child processes belong to the parent supervisor for cancellation. Child
        jobs do not reenter run_job's flock or publish sibling private paths.
        """
        from episode_planner import plan_episodes, slice_asr, EpisodePlanningError
        self.metrics['space_preflight']=space_preflight(duration,shutil.disk_usage(self.root).free,
            self.space_reserve(config))
        save(self.folder/'space_preflight.json',self.metrics['space_preflight'])
        asr_audio=self.folder/'audio/full_source_asr16k.wav'
        asr=self.folder/'segments.full_source.asr.json'
        signature={'source_sha256':source_sha,'audio_track':track,'language':requested_language,
            'model':config['asr_model'],'asr_code':digest(script(self.root,'asr.py'))}
        if not self.cached('full_source_asr',signature,[]):
            self.command('准备长视频识别音轨',6,7,[FFMPEG,'-v','error','-y','-i',source_file,
                '-map',f'0:a:{track}','-vn','-ac','1','-ar','16000','-c:a','pcm_s16le',asr_audio])
            self.command('识别完整原文与分集边界',7,16,[self.asr_py,script(self.root,'asr.py'),
                '--input',asr_audio,'--model',config['asr_model'],'--output',asr,'--language',requested_language],
                env={'OMP_NUM_THREADS':'2','MKL_NUM_THREADS':'2','OPENBLAS_NUM_THREADS':'2'},timeout=43200)
            self.metrics['full_source_asr_runs']=self.metrics.get('full_source_asr_runs',0)+1
            self.remember('full_source_asr',signature,[asr_audio,asr])
        full_asr=read(asr)
        actual=normalize_language(full_asr.get('source_language') or full_asr.get('language'),allow_auto=False)
        try:plan=plan_episodes(full_asr,duration,max_seconds=source_window_max or episode_max,fps=25)
        except EpisodePlanningError as error:
            save(self.folder/'episode_planning_failure.json',{'code':error.code,'detail':error.detail})
            raise RuntimeError('无法在可靠的句间或词间停顿安全分集，请检查识别时间戳后重试；原素材已保留') from error
        plan.update(source_sha256=source_sha,full_asr_sha256=digest(asr),audio_track=track,
            planner_sha256=digest(script(self.root,'episode_planner.py')))
        plan_path=self.folder/'episode_plan.json';save(plan_path,plan)
        plan_key=key(plan)[:12];episodes=plan['episodes'];total=len(episodes)
        source=read(self.folder/'source.json')
        source.update(**source_language_metadata(actual),requested_source_language=requested_language,episode_count=total)
        save(self.folder/'source.json',source)
        self.emit(f'已按停顿分为{total}集',17,{'episode_count':total,**source_language_metadata(actual)})
        artifacts=[];summaries=[];children=[]
        for episode in episodes:
            self.check();number=episode['episode_index'];prefix=f'第{number}/{total}集 · '
            lo=17+82*(number-1)/total;hi=17+82*number/total
            folder=self.folder/'episode_inputs';folder.mkdir(exist_ok=True)
            clip=folder/f'episode_{number:03d}_{plan_key}.mp4'
            receipt=folder/f'episode_{number:03d}_{plan_key}.json'
            clip_signature={'plan':key(plan),'episode':number,'source_sha256':source_sha,
                'export_code':digest(script(self.root,'episode_planner.py'))}
            cache_name=f'episode_source_{number:03d}'
            if not self.cached(cache_name,clip_signature,[]):
                if clip.exists():clip.rename(clip.with_name(clip.stem+'.superseded_'+secrets.token_hex(4)+'.mp4'))
                self.command(prefix+'准备原片',lo,lo,[self.asr_py,script(self.root,'episode_planner.py'),'export',
                    '--source',source_file,'--output',clip,'--plan',plan_path,'--episode',str(number),'--receipt',receipt],timeout=14400)
                self.remember(cache_name,clip_signature,[clip,receipt])
            child_asr_dir=self.folder/'episode_asr';child_asr_dir.mkdir(exist_ok=True)
            child_asr=child_asr_dir/f'episode_{number:03d}_{plan_key}.json'
            save(child_asr,slice_asr(full_asr,episode));clip_sha=digest(clip)
            child_options={**self.options,'source_language':actual,'split_long_source':False,
                'episode_max_seconds':episode_max,'voice_pilot_groups':0}
            child_job={'id':self.name+f'_e{number:03d}_'+plan_key[:8],
                'title':self.job.get('title','视频译配')+f' · 第{number}集','input_path':str(clip),
                'source_sha256':clip_sha,'options':child_options,
                '_episode_asr':{'path':str(child_asr),'sha256':digest(child_asr),
                    'clip_sha256':clip_sha,'parent_id':self.name}}
            def update(stage,progress,details=None):
                safe=dict(details or {});safe.pop('voice_pilot',None)
                self.emit(prefix+stage,lo+(hi-lo)*progress/100,
                    {**safe,'episode_index':number,'episode_count':total,**source_language_metadata(actual)})
            # The enclosing run_job already owns the only heavy-model lock.
            child_started=time.perf_counter()
            result=_run_with_recovery(self.root,child_job,update,self.runner,self.cancelled,child_started,0,
                deadline=self.deadline)
            if result['duration_s']>episode_max+.001:
                if replan_attempt>=2:
                    raise RuntimeError('本集自然配音后仍超过时长上限，需检查分集边界；中间结果已保留')
                smaller=math.floor(episode['duration_s']*episode_max/result['duration_s']*.95*25)/25
                if smaller<1:raise RuntimeError('无法在正常语速下满足分集时长，需要检查台词')
                save(self.folder/f'episode_replan_{replan_attempt+1}.json',{
                    'reason':'natural_dubbing_exceeds_episode_max','output_duration_s':result['duration_s'],
                    'source_duration_s':episode['duration_s'],'new_source_window_max':smaller,
                    'source_plan':plan,'originals_preserved':True})
                self.emit('按实际中文时长缩短分集',lo)
                return self.process_episodes(config,source_file,source_sha,probe,duration,track,
                    requested_language,episode_max,smaller,replan_attempt+1)
            public_dir=self.folder/'outputs'/f'episode_{number:03d}';public_dir.mkdir(parents=True,exist_ok=True)
            for item in result['artifacts']:
                origin=Path(item['path']).resolve()
                if not origin.is_relative_to(self.root/'jobs'/child_job['id']) or digest(origin)!=item['sha256']:
                    raise RuntimeError('分集导出文件来源或校验不符')
                if item['kind']=='video' and origin.stat().st_size>=50_000_000:
                    raise RuntimeError('分集视频超过50MB，未发布')
                target=public_dir/origin.name
                if target.exists() and digest(target)!=item['sha256']:
                    target.rename(target.with_name(target.stem+'.superseded_'+secrets.token_hex(4)+target.suffix))
                if not target.exists():
                    try:os.link(origin,target)
                    except OSError:shutil.copyfile(origin,target)
                artifacts.append({**item,'path':str(target),'label':f'第{number}集 · '+item['label']})
            summaries.append({'episode_index':number,'source_start_s':episode['source_start_s'],
                'source_end_s':episode['source_end_s'],'source_duration_s':episode['duration_s'],
                'duration_s':result['duration_s'],**source_language_metadata(actual),'human_reviewed':False})
            children.append({'job':child_job['id'],'metrics':result.get('metrics',{}),**summaries[-1]})
            save(self.folder/'episode_progress.json',{'plan_sha256':key(plan),'completed_episodes':number,
                'episode_count':total,'episodes':summaries,'human_reviewed':False})
        self.metrics.update(wall_seconds=time.perf_counter()-self.started,source_duration_s=duration,
            output_duration_s=sum(e['duration_s'] for e in summaries),episode_count=total,episodes=children,
            full_source_asr_runs=self.metrics.get('full_source_asr_runs',0),
            episode_asr_reused=True,heavy_models_serial=True,**source_language_metadata(actual))
        self.metrics['duration_replan_attempts']=replan_attempt
        save(self.folder/'web_pipeline_metrics.json',self.metrics)
        self.emit('全部分集机器草稿已完成',100,{'episode_count':total})
        return {'artifacts':artifacts,'source_sha256':source_sha,'duration_s':self.metrics['output_duration_s'],
            'episode_count':total,'episodes':summaries,**source_language_metadata(actual),
            'warnings':self.warnings,'metrics':self.metrics,'human_reviewed':False,'status':'machine_reviewed_draft'}

    def resolve_native_audio(self,native_manifest,grouped=False):
        """Resolve an explicit scoped receipt on CPU; retain actual TTS metrics."""
        selection=self.folder/'native_audio_selections.json'
        if not selection.exists():return native_manifest
        if grouped:raise RuntimeError('逐句声音选择凭据不能应用到连续配音分组')
        helper=script(self.root,'resolve_native_export.py')
        resolved=self.folder/'tts/native25/resolved_export.json'
        self.command('核验选定句的完整配音',79,79,[self.py,'-B',helper,'resolve',
            '--root',self.root,'--job',self.name,'--manifest',native_manifest,'--output',resolved])
        result=read(resolved)
        if (result.get('phase')!='complete' or result.get('pending_unique_keys')!=0
                or result.get('composition',{}).get('one_inference_run') is not False
                or result.get('composition',{}).get('no_synthesis_performed') is not True):
            raise RuntimeError('选定声音的来源核验不完整，不继续导出')
        self.metrics['native_audio_composition']={
            'manifest':str(resolved),'composition_identity_sha256':result['composition_identity_sha256'],
            'selected_segment_ids':[result['composition']['registered_correction']],
            'source_runs_combined_as_one':False,'tts_metrics_from_original_native_run':True,
            'human_listening_approved':False}
        return resolved

    def acquire_source(self):
        if self.job.get('input_path'):
            source=Path(self.job['input_path']).expanduser().resolve()
            if not source.is_file():raise RuntimeError('上传或指定的本地视频不存在')
            return source
        url=self.job.get('source_url','');parsed=urlparse(url)
        if parsed.scheme not in ('http','https') or not parsed.hostname:raise RuntimeError('请输入有效的HTTP/HTTPS视频网址')
        tool=next((p for p in [self.root/'tools/yt-dlp',self.py.parent/'yt-dlp'] if p.is_file()),None)
        if tool is None:
            found=shutil.which('yt-dlp');tool=Path(found) if found else None
        if tool is None:raise RuntimeError('网址下载工具尚未安装，请先由本机管理员准备yt-dlp入口')
        inputs=self.root/'inputs'/self.name;inputs.mkdir(parents=True,exist_ok=True)
        existing=list(inputs.glob('source.*'));video=next((p for p in existing if p.suffix.lower() in ('.mp4','.mkv','.webm','.mov')),None)
        if video:return video
        reserve=math.ceil(self.space_reserve(read(self.root/'config/local.json'))*1024**3)
        margin=512*1024**2
        def low_space():return shutil.disk_usage(self.root).free<reserve+margin
        if low_space():raise RuntimeError('空间不足以接收视频，需先腾出空间；未删除已有素材')
        budget=shutil.disk_usage(self.root).free-reserve-margin
        try:
            self.command('下载视频到本机',0,5,[tool,'--ignore-config','--no-playlist','--no-overwrites','--newline','--js-runtimes','node',
                '--max-filesize',str(budget),'--format','bv*[height<=720]+ba/b[height<=720]/b','--merge-output-format','mp4',
                '--output',str(inputs/'source.%(ext)s'),'--',url],timeout=3600,should_abort=low_space)
        except RuntimeError as error:
            if low_space():raise RuntimeError('下载时可用空间不足，已停止并保留原素材，请腾出空间后重试') from error
            raise
        video=next((p for p in inputs.glob('source.*') if p.suffix.lower() in ('.mp4','.mkv','.webm','.mov')),None)
        if video is None:raise RuntimeError('下载结束但未找到完整视频文件')
        return video
    def _analyze_audio(self,config):
        """Overlap CPU-only ASR with the sole MPS separator; join before TTS.

        Workers never save the shared receipt file. Even on failure, collect
        every future after cancelling/terminating siblings, then record only
        verified successful outputs for a SHA-checked resume.
        """
        began=time.perf_counter();abort=threading.Event();states={'asr':'pending','separation':'pending'}
        elapsed={};records={};failure=None
        asr=self.folder/'segments.asr.json'
        signature={'audio_sha':digest(self.folder/'audio/asr16k.wav'),
            'model':config['asr_model'],'code':digest(script(self.root,'asr.py')),
            'source_language':normalize_language(self.options.get('source_language','de'))}
        prepared=self.job.get('_episode_asr')
        if prepared:
            path=Path(prepared['path']).resolve()
            parent=self.root/'jobs'/prepared['parent_id']
            if (not path.is_relative_to(parent/'episode_asr') or not path.is_file()
                    or digest(path)!=prepared['sha256']
                    or prepared['clip_sha256']!=digest(Path(self.job['input_path']))):
                raise RuntimeError('分集原文与当前视频绑定不符，未复用识别结果')
            signature['prepared_asr_sha256']=prepared['sha256']
        def state(name,value):
            with self._state_lock:
                states[name]=value
                done=sum(v in ('complete','cached') for v in states.values())
                self.emit('分析原文台词与场景声音',10+15*done,
                    {'parallel_stages':dict(states),'asr_device':'cpu','separation_device':'mps',
                     'workers':2,'concurrent_tts':False})
        def worker(name):
            started=time.perf_counter()
            try:
                state(name,'running')
                if name=='asr':
                    hit=self.cached('asr',signature,[])
                    if not hit:
                        if prepared:save(asr,read(path))
                        else:
                            self.command('识别原文台词',10,25,[self.asr_py,script(self.root,'asr.py'),
                                '--input',self.folder/'audio/asr16k.wav','--model',config['asr_model'],'--output',asr,
                                '--language',signature['source_language']],
                                env={'OMP_NUM_THREADS':'2','MKL_NUM_THREADS':'2','OPENBLAS_NUM_THREADS':'2'},
                                should_abort=abort.is_set,report_progress=False)
                    if not read(asr).get('segments'):raise RuntimeError('没有识别到可配音的原文台词')
                    record={'name':'asr','signature':signature,'outputs':[asr],'cache_hit':hit}
                else:
                    record=self.separate(remember_result=False,report_progress=False,should_abort=abort.is_set)
                state(name,'cached' if record['cache_hit'] else 'complete')
                return record
            finally:
                with self._state_lock:elapsed[name]=time.perf_counter()-started
        executor=ThreadPoolExecutor(max_workers=2,thread_name_prefix='audio-analysis')
        futures={}
        try:
            for name in states:futures[executor.submit(worker,name)]=name
            done,_=wait(futures,return_when=FIRST_EXCEPTION)
            failure=next((future.exception() for future in done if not future.cancelled()
                          and future.exception() is not None),None)
        except BaseException as error:failure=error
        finally:
            if failure is not None:
                abort.set()
                for future in futures:future.cancel()
            # Do not advance to speakers/TTS while a silent sibling still runs.
            executor.shutdown(wait=True,cancel_futures=True)
            for future,name in futures.items():
                try:records[name]=future.result()
                except BaseException as error:
                    if failure is None and not isinstance(error,CancelledError):failure=error
                    states[name]='failed' if error is failure else 'cancelled'
            # Sequential, after every future has joined: no .partial-file race.
            for record in records.values():
                if not record['cache_hit']:self.remember(record['name'],record['signature'],record['outputs'])
            wall=time.perf_counter()-began
            self.metrics.setdefault('parallel_wall_s',{})['audio_analysis']=wall
            self.metrics['audio_analysis']={'scheduling':'cpu_asr_and_single_mps_demucs',
                'workers':2,'stage_elapsed_s':elapsed,'parallel_wall_s':wall,
                'elapsed_times_are_overlapping_not_additive':True,'states':states,
                'status':('cancelled' if self.cancelled() else 'failed') if failure is not None else 'complete','all_futures_joined':True,
                'concurrent_tts':False}
            save(self.folder/'web_pipeline_metrics.json',self.metrics)
        if failure is not None:raise failure
        self.check()
        return asr
    def separate(self,remember_result=True,report_progress=True,should_abort=None):
        weight=self.root/'models/.torch/hub/checkpoints'/DEMUCS_WEIGHT
        if not weight.is_file() or digest(weight)!=DEMUCS_SHA:raise RuntimeError('本地Demucs权重缺失或SHA不符；未自动下载模型')
        signature={'audio':digest(self.folder/'audio/original.wav'),'weight_sha256':DEMUCS_SHA,'segment':5,'shifts':0,'overlap':.25,'device':'mps'}
        stem=self.folder/'separated/htdemucs/original'
        outputs=[stem/'vocals.wav',stem/'no_vocals.wav']
        record={'name':'separation','signature':signature,'outputs':outputs,'cache_hit':False}
        if self.cached('separation',signature,[]):
            record['cache_hit']=True;return record
        environment={**os.environ,'TORCH_HOME':str(self.root/'models/.torch'),'PYTORCH_ENABLE_MPS_FALLBACK':'0',
            'OMP_NUM_THREADS':'2','HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1'}
        self.command('分离人声与场景背景',25,40,[self.py,'-m','demucs','-n','htdemucs','--two-stems','vocals','-d','mps',
            '--segment','5','--overlap','0.25','--shifts','0','--float32','-o',self.folder/'separated',self.folder/'audio/original.wav'],
            env=environment,should_abort=should_abort,report_progress=report_progress)
        if not all(path.is_file() for path in outputs):raise RuntimeError('分离未生成两条完整声音轨道')
        if remember_result:self.remember('separation',signature,outputs)
        return record


def run_job(root,job,update,run_command,is_cancelled):
    root=Path(root).resolve();lockfile=root/'cache/.web_pipeline.lock';lockfile.parent.mkdir(parents=True,exist_ok=True)
    with lockfile.open('a+') as lock:
        began=time.perf_counter();announced=False
        while True:
            if is_cancelled():raise RuntimeError('任务已取消，尚未启动模型')
            try:
                fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not announced:
                    update('等待本机配音资源',0,{'models_loaded':False})
                    announced=True
                time.sleep(.25)
        waited=time.perf_counter()-began
        return _run_with_recovery(root,job,update,run_command,is_cancelled,began,waited)


def _run_with_recovery(root,job,update,run_command,is_cancelled,started,waited,deadline=None):
    # No model gets arbitrary file/code/config access. Advice is one supervised
    # CPU child; the parent validates its exact persisted decision again. Each
    # retry retains this GPU lock and runs the same job/options through the
    # original SHA-verifying stage receipts and per-batch translation caches.
    attempts=[];recoveries=[];pending_attempt=None;controller=None;progress=0.;data_rounds=[]
    recovery_wall=0.;folder=root/'jobs'/job['id']
    def emit(stage,value,details=None):
        nonlocal progress
        progress=max(progress,float(value));update(stage,progress,details)
    def outcome(status):
        nonlocal pending_attempt
        if controller is not None and pending_attempt is not None:
            controller.record_outcome(pending_attempt,status)
            pending_attempt=None
    def attempt_record(flow,status,elapsed,error=None):
        entry={'attempt':len(attempts)+1,'status':status,'wall_seconds':elapsed,
            'stage_wall_s':dict(flow.metrics.get('stage_wall_s',{})),
            'stage_cache_hits':list(flow.metrics.get('stage_cache_hits',[]))}
        if 'parallel_wall_s' in flow.metrics:entry['parallel_wall_s']=dict(flow.metrics['parallel_wall_s'])
        if isinstance(error,StageFailure):entry['failure_stage']=error.failure_metadata['stage']
        attempts.append(entry)
    def metrics(current,status):
        value=dict(current);times={}
        for attempt in attempts:
            for stage,elapsed in attempt['stage_wall_s'].items():times[stage]=times.get(stage,0.)+elapsed
        if recovery_wall:times['固定恢复策略']=recovery_wall
        value.update(stage_wall_s=times,resource_wait_seconds=waited,
            wall_seconds=time.perf_counter()-started,status=status,
            pipeline_attempts=attempts,recovery={'enabled':True,'advice_wall_seconds':recovery_wall,
                'decision_mode':'deterministic_transport_policy_v1','proxy_advice_calls':0,
                'records':recoveries,'data_only_actions':True,'arbitrary_code_or_config_changes':False,
                'verified_output_reuse_action_enabled':False,'failed_and_advice_wait_included':True,
                'website_data_repairs':data_rounds,'max_data_repair_rounds_per_run':6,
                'wall_budget_seconds':14400})
        save(folder/'web_pipeline_metrics.json',value)
        return value
    while True:
        flow=Flow(root,job,emit,run_command,is_cancelled);attempt_began=time.perf_counter()
        flow.deadline=deadline if deadline is not None else started+14400
        try:
            result=flow.stages()
        except Exception as error:
            cancelled=bool(is_cancelled())
            attempt_record(flow,'cancelled' if cancelled else 'failed',time.perf_counter()-attempt_began,error)
            outcome('cancelled' if cancelled else 'failed')
            if not cancelled and isinstance(error,WebsiteRepairReady):
                if len(data_rounds)>=6:
                    metrics(flow.metrics,'failed')
                    raise RuntimeError('已达到本次网站局部恢复次数预算，全部断点和原因已保存') from None
                data_rounds.append({'action':error.action,'status':'bound_data_saved'})
                metrics(flow.metrics,'recovering')
                emit('局部问题已处理，从已核验断点继续',progress,{'recovery_action':error.action,
                    'recovery_round':len(data_rounds),'models_parallel':False,'human_reviewed':False})
                continue
            if cancelled or not isinstance(error,StageFailure):
                metrics(flow.metrics,'cancelled' if cancelled else 'failed');raise
            recovery=_recovery_module(root)
            policy=recovery.classify_error(error.stage,error,error.failure_metadata)
            if not policy['retryable']:
                metrics(flow.metrics,'failed');raise
            controller=recovery.RecoveryController(root,job['id'])
            # Only typed, nonce-verified transport evidence reaches this fixed
            # local policy. Translation and media recovery have their own
            # bounded paths; a model never decides permissions or retry budgets.
            packet={'schema':'bounded_recovery_packet_v1','stage':policy['stage'],
                'error_class':policy['error_class'],'failure_metadata':error.failure_metadata}
            private=folder/'recovery';packet_path=private/('packet_'+str(len(recoveries)+1)+'.json')
            decision_path=private/('decision_'+str(len(recoveries)+1)+'.json')
            _private_json(packet_path,packet)
            record={'stage':policy['stage'],'fingerprint':policy['fingerprint'],
                'status':'advising','decision':None};recoveries.append(record)
            emit('检查临时连接故障并尝试恢复',progress,
                {'recovery_attempt':len(recoveries),'allowed_action':'retry_same_stage','model_or_code_changes_allowed':False})
            advice_began=time.perf_counter()
            try:
                # A unique filename prevents an earlier decision from being
                # accepted after a failed or cancelled advisory subprocess.
                if decision_path.exists():decision_path.unlink()
                flow.command('执行固定恢复策略',progress,progress,[flow.asr_py,'-B',script(root,'recovery.py'),
                    'decide','--root',root,'--job',job['id'],'--packet',packet_path,'--out',decision_path],
                    timeout=60,should_abort=is_cancelled,report_progress=False)
                decision=read(decision_path)
                record['decision']={k:decision.get(k) for k in
                    ('action','reason_code','policy_code','attempts_consumed','attempt_id','local_policy_allowed')}
                if (decision.get('action')!='retry_same_stage'
                        or not controller.validate_retry_decision(decision,error.stage,error,error.failure_metadata)):
                    record['status']='stopped';metrics(flow.metrics,'failed');raise error
                pending_attempt=decision['attempt_id'];record['status']='retry_authorized'
            except Exception:
                if record['status']=='advising':record['status']='cancelled' if is_cancelled() else 'advice_failed'
                # Reservation precedes AI. Cancellation/timeout therefore does
                # not refund the lifetime retry budget or leave it reusable.
                if pending_attempt is None:
                    try:
                        journal=read(controller.path)
                        latest=journal.get('attempts',[])[-1]
                        if (latest.get('fingerprint')==policy['fingerprint'] and latest.get('outcome') is None):
                            controller.record_outcome(latest['attempt_id'],'cancelled' if is_cancelled() else 'stopped')
                    except (OSError,ValueError,KeyError,IndexError,TypeError):pass
                if is_cancelled():raise RuntimeError('任务已取消，已保留中间结果') from None
                raise error
            finally:
                recovery_wall+=time.perf_counter()-advice_began
                metrics(flow.metrics,'cancelled' if is_cancelled() else 'recovering' if pending_attempt else 'failed')
            if is_cancelled():
                outcome('cancelled');metrics(flow.metrics,'cancelled')
                raise RuntimeError('任务已取消，已保留中间结果')
            emit('从已核验的完成步骤继续',progress,{'recovery_attempt':pending_attempt,'same_job_and_options':True})
        else:
            attempt_record(flow,'completed',time.perf_counter()-attempt_began)
            outcome('completed')
            result['metrics']=metrics(result.get('metrics',{}),'complete')
            return result


def single_speaker(root,folder):
    import numpy as np
    import soundfile as sf
    rows=read(folder/'segments.asr.json')['segments'];source=folder/'separated/htdemucs/original/vocals.wav'
    if len(rows)!=1:raise RuntimeError('Single-speaker shortcut requires exactly one recognized segment')
    row=rows[0];start=max(0,row['start_s']-.03);end=min(row['end_s']+.03,start+8)
    with sf.SoundFile(source) as handle:
        rate=handle.samplerate;handle.seek(round(start*rate));samples=handle.read(round((end-start)*rate),dtype='float32',always_2d=True).mean(axis=1)
    reference=folder/'references/S00.wav';sf.write(reference,samples,rate,subtype='PCM_24')
    save(folder/'speakers.json',{'S00':{'reference':str(reference.relative_to(root)),'reviewed':False,'human_reviewed':False,'review_method':'machine single detected segment; pending quality check'}})
    save(folder/'segments.plan.json',{'segments':[{**row,'segment_id':'line0001','source_ids':[row['segment_id']],'speaker_id':'S00',
        'speaker_reviewed':True,'speaker_review_method':'machine acoustic label, no identity verification','human_reviewed':False,
        'target_characters':max(5,int((row['end_s']-row['start_s'])*3.8))}],'identities_verified':False})


def check_speakers(root,folder):
    import numpy as np
    import soundfile as sf
    speakers=read(folder/'speakers.json');audit=[]
    for sid,item in speakers.items():
        path=(root/item['reference']).resolve()
        if not path.is_relative_to(root):raise RuntimeError('参考声音路径超出当前项目')
        samples,rate=sf.read(path,dtype='float32');duration=len(samples)/rate
        valid=bool(.8<=duration<=15 and samples.size and np.isfinite(samples).all() and np.max(np.abs(samples))>.001)
        item.update(reviewed=valid,human_reviewed=False,review_method='machine acoustic-run and finite/duration/energy checks; identity not verified')
        audit.append({'speaker_id':sid,'duration_s':duration,'finite_duration_energy_passed':valid,'human_reviewed':False})
    save(folder/'speakers.json',speakers);save(folder/'speaker_machine_review.json',{'speakers':audit,'human_reviewed':False,'identities_verified':False})
    if not all(row['finite_duration_energy_passed'] for row in audit):raise RuntimeError('部分人物参考声音太短或无有效声音，未继续克隆配音')


def translate_job(root,folder):
    from proxy_client import translate_segments,ProxyTransportError,emit_transport_failure
    data=read(folder/'translation/input.json');rows=data['segments']
    language=normalize_language(data.get('source_language','de'),allow_auto=False)
    review_scope=None
    if (folder/'translation/source_review_hints.json').is_file():
        source=read(folder/'source.json')
        review_scope,_=reviewed_source_plan(folder,folder/'segments.plan.json',digest(Path(source['input'])))
        if rows!=review_scope[:len(rows)]:raise ValueError('预览译稿不是当前源计划的完整前缀')
    def progress(done,total):print(json.dumps({'event':'translation_progress','completed':done,'total':total}),flush=True)
    try:
        updated,report=translate_segments(rows,root,progress=progress,diagnostic_dir=folder/'translation/diagnostics',
            source_language=language,review_scope=review_scope)
    except ProxyTransportError as error:
        emit_transport_failure(error.failure_metadata,event='proxy_stage_failure')
        raise
    from translation_reuse import apply_prior_reviews
    updated,report=apply_prior_reviews(root,folder,rows,updated,report)
    validate_translations(rows,updated,report)
    revision_path=folder/'source_plan_revision.json'
    if revision_path.is_file():
        revision=read(revision_path);pending=set(revision.get('blocking_unresolved',[]))
        report.update(source_revision_receipt_sha256=digest(revision_path),
            blocking_unresolved_source_rows=sorted(pending),
            source_transcript_uncertainties=revision.get('unresolved',[]))
        if pending:
            report['review_status']='machine_second_pass_complete_source_review_pending'
            updated=[{**row,'source_review_required':True} if row['segment_id'] in pending else row for row in updated]
    save(folder/'segments.json',{'schema_version':2,'segments':updated,**source_language_metadata(language),'human_reviewed':False,
        'translation_status':'machine_second_pass_complete_draft','review_method':report['review_method']})
    report.update(job=folder.name,source_segments_sha256=digest(folder/'segments.json'),
        source_speakers_sha256=digest(folder/'speakers.json'))
    save(folder/'translation/report.json',report)


def regroup_job(root,folder):
    assignments=folder/'speaker_assignments.json';plan=folder/'segments.plan.json'
    original=read(assignments if assignments.is_file() else plan)
    if not (folder/'segments.plan.bounded.json').exists():save(folder/'segments.plan.bounded.json',read(plan))
    source=read(folder/'source.json')
    grouped=regroup_sentence_rows(original['segments'],source['duration_s'],source_language=source.get('source_language','de'))
    save(plan,grouped)
    save(folder/'sentence_group_receipt.json',{'schema':'german_sentence_group_receipt_v1',
        'input_path':str((assignments if assignments.is_file() else folder/'segments.plan.bounded.json').resolve()),
        'input_sha256':digest(assignments if assignments.is_file() else folder/'segments.plan.bounded.json'),
        'plan_path':str(plan.resolve()),'plan_sha256':digest(plan),'source_ids_count':grouped['source_ids_count'],
        'source_ids_preserved_in_order':True,'text_content_rewritten':False,'human_reviewed':False,
        'method':grouped['grouping_method'],'issues':grouped['issues'],
        'groups':[{k:row[k] for k in ('segment_id','speaker_id','source_ids','start_s','end_s','text_de','german_sentence_complete')}
            for row in grouped['segments']]})
    print(json.dumps({'event':'sentence_plan_ready','segments':len(grouped['segments']),
        'source_ids_count':grouped['source_ids_count'],'incomplete_sentence_issues':grouped['issues'],
        'human_reviewed':False},ensure_ascii=False),flush=True)


def background_job(root,folder):
    import numpy as np
    import soundfile as sf
    from scipy.ndimage import gaussian_filter1d
    source=read(folder/'source.json');duration=source['duration_s'];frames=round(duration*48000)
    # Called inside a supervised child; this FFmpeg is a child of the same group.
    import subprocess
    raw=folder/'audio/background48k.wav';separated=folder/'separated/htdemucs/original/no_vocals.wav'
    subprocess.run([FFMPEG,'-v','error','-threads','2','-y','-i',str(separated),'-ac','2','-ar','48000','-c:a','pcm_f32le',str(raw)],check=True)
    samples,rate=sf.read(raw,dtype='float32',always_2d=True)
    if rate!=48000 or samples.shape[1]!=2 or len(samples)<1 or not np.isfinite(samples).all():raise RuntimeError('分离背景音轨无效')
    missing=frames-len(samples)
    if missing>48000 or missing<-48000:raise RuntimeError('原视频与背景音轨长度差异过大，未强行补齐')
    if missing>0:
        tail=samples[-min(len(samples),24000):]
        samples=np.concatenate((samples,np.tile(tail,(math.ceil(missing/len(tail)),1))[:missing]))
    else:samples=samples[:frames]
    levels=np.array([20*math.log10(max(1e-9,float(np.sqrt(np.mean(samples[i:i+48000]**2))))) for i in range(0,frames,48000)],dtype='float32')
    smoothed=gaussian_filter1d(levels,1.5,mode='nearest');rows=read(folder/'segments.json')['segments']
    centres=np.arange(len(levels))+.5;body=((centres>=rows[0]['start_s'])&(centres<=rows[-1]['end_s'])).astype('float32')
    max_gain=4+6*body;dbs=max_gain*np.clip((smoothed+60)/15,0,1)
    curve=np.interp(np.arange(frames)/48000,centres,dbs).astype('float32');samples*=np.power(10.,curve*.05)[:,None]
    boosted=folder/'audio/background_boosted_float.wav';sf.write(boosted,samples,48000,subtype='FLOAT')
    output=folder/'audio/background_independent.wav'
    subprocess.run([FFMPEG,'-v','error','-threads','2','-y','-i',str(boosted),'-af','alimiter=limit=0.501187:attack=5:release=150:level=0:latency=1','-ar','48000','-c:a','pcm_f32le',str(output)],check=True)
    info=sf.info(output)
    if info.frames!=frames:raise RuntimeError('背景限幅后时间轴发生变化')
    save(folder/'audio/background_report.json',{'source':str(separated),'sample_rate':48000,'frames':frames,
        'body_max_gain_db':10,'title_max_gain_db':4,'gate_below_dbfs':-60,'full_boost_above_dbfs':-45,
        'smoothing_sigma_s':1.5,'speech_ducking':False,'tail_context_extended_samples':max(0,missing),'zero_padding_samples':0,
        'peak_limiter_dbfs':-6,'human_reviewed':False,'separation_quality_unverified':True})


def _main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=('_single_speaker','_check_speakers','_regroup_sentences','_translate','_background'))
    p.add_argument('--root',type=Path,required=True);p.add_argument('--job',required=True);args=p.parse_args()
    root=args.root.resolve();folder=root/'jobs'/args.job
    if not re.fullmatch(r'web_[A-Za-z0-9_-]{1,54}',args.job):raise ValueError('Invalid job')
    {'_single_speaker':single_speaker,'_check_speakers':check_speakers,'_regroup_sentences':regroup_job,
        '_translate':translate_job,'_background':background_job}[args.action](root,folder)


if __name__=='__main__':_main()
