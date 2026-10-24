"""Pure storage estimates for the current local pipeline's resumed stages.

The caller supplies hashes freshly computed from disk and the signatures it
would pass to Flow.cached. A saved receipt alone is never verified evidence.
This module performs no I/O, deletes nothing, and grants no recovery rights.
After deleting a suggested temporary file, measure real free space and call
preflight again; APFS sharing and concurrent swap make planned bytes uncertain.
"""
import math
from collections.abc import Mapping
from pathlib import PurePosixPath
import re

SCHEMA='resume_remaining_stage_space_v1'
GIB=1024**3
RESERVE_BYTES=8*GIB
RUNTIME_HARD_FLOOR_BYTES=2*GIB
FIXED_MARGIN_BYTES=256*1024**2
SHA=re.compile(r'^[0-9a-f]{64}$')

# Match pipeline.space_preflight and the formats actually written by
# background_job, group_compact_adapter.mix, compact_edit.render/export_size.
# The future mix/encoded/TTS costs are NEVER credited, even with cached TTS.
STAGE_RATES={'prepare':320000,'separation':705600,'background':1152000}
FUTURE_RATES={'mix_four_wav_intermediates':1248000,
              'three_encoded_video_copies':320000,
              'native_pcm_and_audio_container':88000}
REQUIRED_OUTPUTS={
    'prepare':('source.json','audio/original.wav','audio/asr16k.wav'),
    'separation':('separated/htdemucs/original/vocals.wav',
                  'separated/htdemucs/original/no_vocals.wav'),
    'background':('audio/background_independent.wav','audio/background_report.json')}
BACKGROUND_TEMPORARIES=('audio/background48k.wav','audio/background_boosted_float.wav')


def _positive(value,name,allow_zero=False):
    if (isinstance(value,bool) or not isinstance(value,(int,float))
            or not math.isfinite(value) or value<0 or (not allow_zero and value==0)):
        raise ValueError(name+'无效')
    return value


def _job_path(value):
    if not isinstance(value,str):raise ValueError('任务位置无效')
    path=PurePosixPath(value)
    if not path.is_absolute() or '..' in path.parts:raise ValueError('任务位置无效')
    return path


def _file_matches(output):
    return (isinstance(output,Mapping) and isinstance(output.get('sha256'),str)
            and SHA.fullmatch(output['sha256']) is not None
            and output.get('actual_sha256')==output['sha256']
            and type(output.get('bytes')) is int and output['bytes']>0
            and output.get('regular_file') is True and output.get('is_symlink') is False)


def reusable_stage(stage,evidence,*,job_path,source_sha256):
    """Validate caller-observed evidence for one exact current cache decision.

    evidence: source_sha256, fingerprint, expected_fingerprint, reusable=True,
    will_rebuild=False, outputs=[path, sha256, actual_sha256, bytes,
    regular_file=True, is_symlink=False]. Paths must be exact owned outputs.
    """
    base=_job_path(job_path)
    if stage not in REQUIRED_OUTPUTS or not isinstance(evidence,Mapping):return False
    fingerprint=evidence.get('fingerprint')
    if (not isinstance(source_sha256,str) or SHA.fullmatch(source_sha256) is None
            or evidence.get('source_sha256')!=source_sha256
            or not isinstance(fingerprint,str) or SHA.fullmatch(fingerprint) is None
            or evidence.get('expected_fingerprint')!=fingerprint
            or evidence.get('reusable') is not True or evidence.get('will_rebuild') is not False):return False
    outputs=evidence.get('outputs')
    if not isinstance(outputs,(list,tuple)) or not outputs:return False
    seen={}
    for output in outputs:
        if not _file_matches(output) or not isinstance(output.get('path'),str):return False
        path=PurePosixPath(output['path'])
        if not path.is_absolute() or '..' in path.parts or path in seen:return False
        seen[path]=output
    required={base/name for name in REQUIRED_OUTPUTS[stage]}
    return set(seen)==required


def output_duration_bound(source_duration_s,*,speech_duration_s=None,source_rows=None,intro_s=0.,
                          planned_output_duration_s=None):
    """Never assume shorter speech reduces storage; include all group tails.

    The current renderer uses ceil((speech+.35)*25)/25 per group. There can
    be at most one group per source row after no-join recovery, so .39s/row
    safely includes tail and frame rounding. Picture holds change source
    versus frozen frames within that group; they add no separate duration.
    Newly generated audio or a longer EDL requires a fresh preflight.
    """
    source=_positive(source_duration_s,'视频时长');intro=_positive(intro_s,'片头时长',True)
    candidates=[source]
    if speech_duration_s is not None:
        speech=_positive(speech_duration_s,'完整配音时长')
        if type(source_rows) is not int or not 1<=source_rows<=20000:raise ValueError('原句数量无效')
        candidates.append(intro+speech+source_rows*(.35+1/25))
    elif source_rows is not None:raise ValueError('缺少完整配音时长')
    if planned_output_duration_s is not None:candidates.append(_positive(planned_output_duration_s,'导出时长'))
    return max(candidates)


def cleanup_candidates(*,job_path,source_sha256,stages,temporary_files):
    """Return exact owned temporary paths eligible for caller-managed cleanup.

    These two background scratch WAVs are never read by later stages. The
    independent master, raw input, separation stems and old exports are excluded.
    The caller must recheck each proposed hash/type and stage decision directly
    before removal; a later cache miss means rebuilding the background stage.
    """
    base=_job_path(job_path)
    if not isinstance(stages,Mapping) or not isinstance(temporary_files,(list,tuple)):return []
    if not all(reusable_stage(stage,stages.get(stage),job_path=job_path,source_sha256=source_sha256)
               for stage in ('prepare','background')):return []
    allowed={base/name for name in BACKGROUND_TEMPORARIES};seen=set();result=[]
    for item in temporary_files:
        if not _file_matches(item) or not isinstance(item.get('path'),str):continue
        path=PurePosixPath(item['path'])
        if '..' in path.parts or path not in allowed or path in seen:continue
        seen.add(path)
        result.append({'path':str(path),'bytes':item['bytes'],'sha256':item['sha256'],
            'producer':'background_job','reason':'verified_independent_background_replaces_this_scratch_file',
            'requires_immediate_stage_and_sha_recheck':True})
    return result


def preflight(source_duration_s,free_bytes,*,job_path,source_sha256,stages=None,
              speech_duration_s=None,source_rows=None,intro_s=0.,planned_output_duration_s=None):
    """Return a conservative resume estimate without changing the 8/2GiB floors.

    If no upstream stage can be reused, retain the original rounded cold peak.
    Reuse credits are fixed producer budgets, never sums of existing file sizes.
    A separate remaining-component schema removes cold-only rounding once a
    verified stage is skipped, while retaining every future render/mix cost.
    """
    source=_positive(source_duration_s,'视频时长')
    if type(free_bytes) is not int or free_bytes<0:raise ValueError('可用空间无效')
    if not isinstance(source_sha256,str) or SHA.fullmatch(source_sha256) is None:raise ValueError('原片身份无效')
    _job_path(job_path)
    if stages is None:stages={}
    if not isinstance(stages,Mapping):raise ValueError('阶段证据无效')
    duration=output_duration_bound(source,speech_duration_s=speech_duration_s,source_rows=source_rows,
        intro_s=intro_s,planned_output_duration_s=planned_output_duration_s)
    reused={stage:reusable_stage(stage,stages.get(stage),job_path=job_path,source_sha256=source_sha256)
            for stage in STAGE_RATES}
    future={name:math.ceil(duration*rate) for name,rate in FUTURE_RATES.items()}
    upstream={stage:(0 if reused[stage] else math.ceil(source*rate)) for stage,rate in STAGE_RATES.items()}
    credits={stage:(math.ceil(source*rate) if reused[stage] else 0) for stage,rate in STAGE_RATES.items()}
    component_peak=sum(future.values())+sum(upstream.values())+FIXED_MARGIN_BYTES
    cold_peak=math.ceil(max(source,duration)*4*1024**2)+FIXED_MARGIN_BYTES
    peak=component_peak if any(reused.values()) else max(cold_peak,component_peak)
    required=RESERVE_BYTES+peak;sufficient=free_bytes>=required
    result={'schema':SCHEMA,'source_duration_s':source,'storage_duration_bound_s':duration,
        'free_bytes':free_bytes,'protected_reserve_bytes':RESERVE_BYTES,
        'runtime_hard_floor_bytes':RUNTIME_HARD_FLOOR_BYTES,'fixed_margin_bytes':FIXED_MARGIN_BYTES,
        'future_components_bytes':future,'remaining_upstream_components_bytes':upstream,
        'verified_reusable_stages':[stage for stage,valid in reused.items() if valid],
        'reused_stage_budget_bytes':credits,'existing_outputs_credit_bytes':sum(credits.values()),
        'original_rounded_cold_peak_bytes':cold_peak,'estimated_new_peak_bytes':peak,
        'required_free_bytes':required,'sufficient':sufficient,
        'deficit_bytes':max(0,required-free_bytes),'runtime_hard_floor_satisfied':free_bytes>=RUNTIME_HARD_FLOOR_BYTES,
        'shorter_output_assumed':False,'cleanup_bytes_credited':0,'old_assets_deleted':False,
        'recheck_on_changed_source_stage_signature_audio_or_longer_edl':True,
        'renderer_accounting':'group_mix_4wav_48k_stereo_plus_3_encoded_copies_plus_native_aac_plus_256mib_scratch_v1'}
    if not sufficient:
        result['message']=(f'空间不足：当前可用{free_bytes/GIB:.2f} GiB，断点续跑预计新增峰值{peak/GIB:.2f} GiB，'
            f'另保留8.00 GiB安全余量；还需{(required-free_bytes)/GIB:.2f} GiB。已有素材保留。')
    return result


def require_sufficient(*args,**kwargs):
    result=preflight(*args,**kwargs)
    if not result['sufficient']:raise RuntimeError(result['message'])
    return result
