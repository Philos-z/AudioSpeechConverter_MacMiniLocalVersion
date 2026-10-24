"""Bounded local Torch regeneration of actual acoustic failures; never edits text.

The caller owns its normal per-job worker lock and runs returned argv serially.
No model, network request or subprocess is started by this module.
"""
import copy
import difflib
import hashlib
import json
import math
from pathlib import Path

import media_recovery as media
import generic_group_alignment as alignment

SCHEMA = 'local_acoustic_fallback_v1'
COMPOSITION = 'local_acoustic_fallback_composition_v1'
POLICY = {'cfm_steps':25, 'seed':42, 'dtype':'float32', 'use_bf16':False,
          'emo_vector':None, 'emo_alpha':1., 'duration_factor':1., 'num_beams':1,
          'use_random':False, 'use_accel':False, 'use_deepspeed':False,
          'use_cuda_kernel':False, 'use_torch_compile':False,
          'punctuation_policy':'preserve_entire_utterance', 'postprocessing':'none'}


def _file(root, path):
    return media.job_file(root, path)


def _write_once(path, value):
    if path.exists():
        media.require(media.read(path)==value, 'Existing acoustic fallback evidence changed')
    else:
        media.save(path, value)


def _manifest(root, plan, path):
    manifest=media.read(path)
    media.require(manifest.get('phase')=='complete' and manifest.get('pending_unique_keys')==0,
                  'Acoustic manifest is not complete')
    rows=manifest.get('utterances',[])
    media.require([r['segment_id'] for r in rows]==[g['group_id'] for g in plan['tts_groups']],
                  'Acoustic manifest coverage/order changed')
    bindings=[]
    for group, row in zip(plan['tts_groups'],rows):
        _file(root,row['audio']);_file(root,Path(row['audio']).with_suffix('.json'))
        _file(root,group['reference']['path'])
        bound=alignment.audio_binding(group,row,path)
        media.require(row.get('cache_key')==alignment.key(row['content']), 'Manifest cache key changed')
        bindings.append({'group_id':group['group_id'],**bound})
    return manifest,bindings


def _inputs(root,folder,plan_path,base_path):
    root,folder,plan,plan_path=media.plan_binding(root,folder,plan_path)
    base_path=media.job_file(folder,base_path)
    base,audio=_manifest(root,plan,base_path)
    return root,folder,plan,plan_path,base_path,base,audio


def _quality(root,folder,plan,plan_path,base_path,base,audio,quality_path):
    quality_path=media.job_file(folder,quality_path);quality=media.read(quality_path)
    expected={'plan':media.record(folder,plan_path),'native_manifest':media.record(folder,base_path),
              'source_segments_sha256':plan['source_segments']['sha256']}
    media.require(quality.get('schema')=='website_audio_quality_v1' and quality.get('complete') is True
        and quality.get('binding')==expected and quality.get('human_listened') is False
        and quality.get('waveform_modified') is False, 'Acoustic quality receipt is stale/incomplete')
    groups=plan['tts_groups'];rows=quality.get('rows',[])
    media.require(len(rows)==len(groups)==quality.get('checked_utterances')
        and [r['group_id'] for r in rows]==[g['group_id'] for g in groups], 'Quality coverage changed')
    selected=[];normalize=None
    for group,row,native,bound in zip(groups,rows,base['utterances'],audio):
        media.require(type(row.get('needs_repair')) is bool and row.get('member_ids')==group['member_ids']
            and row.get('audio_sha256')==bound['sha256'] and row.get('frames')==bound['frames']
            and row.get('sample_rate')==bound['sample_rate'] and row.get('channels')==1
            and row.get('finite_samples') is True, 'Quality row/audio provenance changed')
        if not row['needs_repair']:continue
        media.require(bound['generation_schema']=='native_mlx_whole_utterance_v1',
                      'Torch acoustic fallback already failed; stop without changing text')
        media.require(row.get('target_free_asr_used') is True and row.get('acoustically_suspicious') is True,
                      'Acoustic flag lacks observed waveform/ASR evidence')
        metrics=media.wav_metrics(bound['path'],native)
        media.require(metrics['acoustically_suspicious'] and all(
            math.isfinite(row.get(name,float('nan'))) and abs(row[name]-metrics[name])<1e-6
            for name in ('energy_dynamic_range_db','zero_crossing_rate')), 'Acoustic flag metrics changed')
        observation=media.read(media.checked_record(folder,row.get('observation')))
        profile=observation.get('profile',{})
        media.require(profile.get('device')=='cpu' and profile.get('compute_type')=='int8'
            and profile.get('local_files_only') is True and profile.get('target_conditioning') is False
            and profile.get('transcribe_options')==alignment.OPTIONS, 'Not target-free CPU ASR evidence')
        alignment.validate_observation(observation,bound,profile)
        recognized=''.join(segment['text'] for segment in observation['segments'])
        if normalize is None:normalize=alignment.normalize_factory(root)
        similarity=difflib.SequenceMatcher(None,normalize(group['text_zh']),normalize(recognized)).ratio()
        media.require(recognized==row.get('target_free_recognized') and similarity<.4
            and abs(similarity-row.get('normalized_text_similarity',float('inf')))<1e-7,
            'Acoustic ASR flag does not match current text/audio')
        selected.append(group)
    ids=[g['group_id'] for g in selected];members=[sid for g in selected for sid in g['member_ids']]
    media.require(1<=len(ids)<=8 and quality.get('needs_repair_groups')==ids
        and quality.get('needs_repair_ids')==members
        and quality.get('flagged_groups')==[{'group_id':g['group_id'],'member_ids':g['member_ids']} for g in selected]
        and quality.get('status')=='needs_repair', 'Only 1 to 8 actual flagged groups may regenerate')
    return quality_path,selected


def _group_key(plan,group):
    return media.key({'video':plan['source_binding']['source_video_sha256'],
        'source_ids':group['source_ids'],'speaker_id':group['speaker_id'],
        'text_zh':group['text_zh'],'text_de':group['text_de'],
        'reference_sha256':group['reference']['sha256']})


def _quality_snapshot(folder,path):
    """A cache hit is a runtime detail, not a new acoustic failure identity."""
    value=copy.deepcopy(media.read(path))
    for row in value['rows']:row.pop('asr_cache_hit',None)
    value=json.loads(json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False))
    encoded=(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n').encode()
    relative='tts/acoustic_fallback/quality/'+media.key(value)+'.json'
    return {'path':relative,'sha256':hashlib.sha256(encoded).hexdigest()},value


def _request(folder,token):
    directory=media.job_file(folder,'tts/acoustic_fallback/'+token,exists=False)
    return directory,media.read(media.job_file(directory,'request.json'))


def _check_request(root,folder,plan_path,base_path,request):
    media.require(request.get('schema')==SCHEMA, 'Wrong acoustic request schema')
    binding=request['binding']
    media.require(binding['plan']==media.record(folder,plan_path)
        and binding['base_manifest']==media.record(folder,base_path), 'Acoustic request input changed')
    quality=media.read(media.checked_record(folder,binding['quality']))
    for row in quality['rows']:
        if row['needs_repair']:media.checked_record(folder,row['observation'])
    for proof in request['child_files']:
        path=_file(root,proof['path'])
        media.require(media.sha(path)==proof['sha256'], 'Fallback child data changed')
    media.require(media.sha(_file(root,'scripts/continuous_tts.py'))==binding['runner_sha256'],
                  'Torch runner changed during fallback')


def prepare(root,folder,plan_path,base_stable_manifest,quality_path):
    root,folder,plan,plan_path,base_path,base,audio=_inputs(root,folder,plan_path,base_stable_manifest)
    quality_path,selected=_quality(root,folder,plan,plan_path,base_path,base,audio,quality_path)
    quality_record,quality_snapshot=_quality_snapshot(folder,quality_path)
    runner=_file(root,'scripts/continuous_tts.py');python=root/'vendor/index-tts/.venv/bin/python'
    media.require(python.is_file(), 'Formal Torch Python environment missing')
    group_keys=[_group_key(plan,g) for g in selected]
    binding={'schema':SCHEMA,'plan':media.record(folder,plan_path),'base_manifest':media.record(folder,base_path),
        'quality':quality_record,'source_video_sha256':plan['source_binding']['source_video_sha256'],
        'base_audio':audio,'group_ids':[g['group_id'] for g in selected],'group_keys':group_keys,
        'parameters':POLICY,'runner_sha256':media.sha(runner)}
    token=media.key({'action':'audio_regenerate','binding':binding})
    for entry in media.journal(folder)['entries']:
        if entry['action']=='audio_regenerate' and entry['token']!=token:
            media.require(not set(group_keys).intersection(entry['binding'].get('group_keys',[])),
                          'The same source sound group may use Torch fallback only once')
    reservation=media.reserve_recovery(folder,'audio_regenerate',binding)
    _write_once(media.job_file(folder,quality_record['path'],exists=False),quality_snapshot)
    directory=media.job_file(folder,'tts/acoustic_fallback/'+token,exists=False)
    child_id='web_acoustic_'+token[:24];child=root/'jobs'/child_id
    media.job_file(root,child/'segments.json',exists=False)
    speakers=media.read(media.job_file(folder,'speakers.json'));chosen={}
    for group in selected:
        sid=group['speaker_id'];info=speakers[sid]
        media.require(info.get('reviewed') is True and _file(root,info['reference'])==_file(root,group['reference']['path']),
                      'Fallback speaker reference changed')
        chosen[sid]=copy.deepcopy(info)
    permission=media.read(media.job_file(folder,'processing_request.json'))
    media.require(permission.get('processing_authorized') is True and permission.get('scope')=='private_local_evaluation',
                  'Existing local processing authorization required')
    documents={'segments.json':{'schema_version':2,'segments':[copy.deepcopy(g['tts_row']) for g in selected]},
               'speakers.json':chosen,'processing_request.json':permission}
    child_files=[]
    for name,value in documents.items():
        path=media.job_file(root,child/name,exists=False);_write_once(path,value)
        child_files.append({'path':str(path),'sha256':media.sha(path)})
    output=directory/'torch_run'
    argv=[str(python),str(runner),'--root',str(root),'--job',child_id,'--output',str(output),
          '--max-segments',str(len(selected)),'--device','mps','--cfm-steps','25','--emotion-mode','reference']
    request={'schema':SCHEMA,'token':reservation['token'],'binding':binding,'child_job':child_id,
        'child_files':child_files,'argv':argv,'fallback_manifest':str(output/'manifest.json'),
        'group_ids':binding['group_ids'],'human_listened':False,'text_changed':False}
    _write_once(directory/'request.json',request)
    return request


def _torch_rows(root,folder,plan,request,manifest_path):
    manifest_path=media.job_file(folder,manifest_path)
    media.require(str(manifest_path)==request['fallback_manifest'], 'Unexpected fallback output location')
    manifest=media.read(manifest_path);ids=request['group_ids']
    media.require(manifest.get('schema')=='continuous_utterance_v1' and manifest.get('job')==request['child_job']
        and manifest.get('phase')=='complete' and manifest.get('pending_unique_keys')==0
        and manifest.get('selected_utterances')==len(ids), 'Torch fallback is incomplete or belongs to another job')
    parameters=manifest.get('parameters',{})
    media.require(all(parameters.get(k)==v for k,v in POLICY.items()) and parameters.get('device')=='mps'
        and parameters.get('use_qwen_emo') is False and manifest.get('experimental_preview') is False,
        'Fallback must be Torch25 FP32 seed42 reference emotion')
    native=manifest.get('utterances',[])
    media.require([r['segment_id'] for r in native]==ids, 'Fallback must contain exactly the selected groups')
    selected={g['group_id']:g for g in plan['tts_groups']};bindings=[]
    for row in native:
        group=selected[row['segment_id']]
        media.require(row.get('content',{}).get('schema')=='continuous_utterance_v1'
            and row['content'].get('parameters')==parameters, 'Wrong Torch sidecar generation parameters')
        _file(root,row['audio']);_file(root,Path(row['audio']).with_suffix('.json'))
        media.require(_file(root,row['reference'])==_file(root,group['reference']['path']), 'Torch reference path changed')
        bound=alignment.audio_binding(group,row,manifest_path)
        media.require(bound['generation_schema']=='continuous_utterance_v1'
            and row.get('cache_key')==alignment.key(row['content']), 'Not formal Torch-generated audio')
        media.wav_metrics(bound['path'],row)
        bindings.append({'group_id':group['group_id'],**bound})
    return manifest_path,manifest,bindings


def finish(root,folder,plan_path,base_stable_manifest,quality_path,fallback_manifest):
    root,folder,plan,plan_path,base_path,base,audio=_inputs(root,folder,plan_path,base_stable_manifest)
    quality_path,selected=_quality(root,folder,plan,plan_path,base_path,base,audio,quality_path)
    quality_record,_=_quality_snapshot(folder,quality_path)
    matches=[e for e in media.journal(folder)['entries'] if e['action']=='audio_regenerate'
        and e['binding'].get('plan')==media.record(folder,plan_path)
        and e['binding'].get('base_manifest')==media.record(folder,base_path)
        and e['binding'].get('quality')==quality_record]
    media.require(len(matches)==1, 'Missing/ambiguous acoustic reservation')
    entry=matches[0];directory,request=_request(folder,entry['token'])
    media.require(request['binding']==entry['binding'] and entry['binding']['base_audio']==audio
        and request['group_ids']==[g['group_id'] for g in selected], 'Acoustic reservation changed')
    _check_request(root,folder,plan_path,base_path,request)
    fallback_path,fallback,new_audio=_torch_rows(root,folder,plan,request,fallback_manifest)
    replacements={r['segment_id']:r for r in fallback['utterances']}
    rows=[copy.deepcopy(replacements.get(r['segment_id'],r)) for r in base['utterances']]
    composition={'schema':COMPOSITION,'composition':{'one_inference_run':False,'native_base_kept':True,
        'fallback_backend':'formal_torch25_fp32','replaced_group_ids':request['group_ids'],
        'base_manifest_sha256':media.sha(base_path),'fallback_manifest_sha256':media.sha(fallback_path)},
        'phase':'complete','job':folder.name,'pending_unique_keys':0,
        'selected_utterances':len(rows),'utterances':rows,
        'parameters':{'composition':'preserved_base_with_selected_formal_torch25',
                      'base':base.get('parameters'),'fallback':fallback['parameters']},
        'source_manifests':{'base':media.record(folder,base_path),'fallback':media.record(folder,fallback_path)},
        'base_generation_statistics':{k:copy.deepcopy(base[k]) for k in
            ('selected_utterances','initial_cache_hit_unique_keys','generated_unique_keys','run_wall_seconds','model_loaded') if k in base},
        'replaced_group_ids':request['group_ids'],'human_listened':False,'text_changed':False,
        'requires_new_alignment_and_quality_checks':True,'single_native_generation_claimed':False}
    composition_path=directory/'composition_manifest.json';_write_once(composition_path,composition)
    selection={'schema':SCHEMA,'token':entry['token'],'binding':entry['binding'],
        'request':media.record(folder,directory/'request.json'),'composition':media.record(folder,composition_path),
        'fallback_manifest':media.record(folder,fallback_path),'fallback_audio':new_audio,
        'selected_group_ids':request['group_ids'],'human_listened':False,'text_changed':False,
        'requires_new_alignment_and_quality_checks':True}
    selection_path=directory/'selection.json';_write_once(selection_path,selection)
    result={'schema':SCHEMA,'changed':True,'manifest':str(composition_path),'selection':str(selection_path),
        'group_ids':request['group_ids'],'outputs':[media.record(folder,selection_path),media.record(folder,composition_path)],
        'human_listened':False,'text_changed':False,'requires_new_alignment_and_quality_checks':True}
    media.finish_recovery(folder,entry['token'],result)
    return result


def resolve(root,folder,plan_path,base_stable_manifest):
    root,folder,plan,plan_path,current,base,audio=_inputs(root,folder,plan_path,base_stable_manifest)
    visited=set()
    for _ in range(media.MAX_RECOVERIES):
        matches=[e for e in media.journal(folder)['entries'] if e['action']=='audio_regenerate'
            and e['status']=='complete' and e['binding'].get('base_manifest',{}).get('path')==str(current.relative_to(folder))]
        if not matches:return current
        media.require(len(matches)==1, 'Ambiguous acoustic compositions')
        entry=matches[0];media.require(entry['token'] not in visited,'Acoustic composition cycle');visited.add(entry['token'])
        media.require(entry['binding']['base_manifest']==media.record(folder,current)
            and entry['binding']['plan']==media.record(folder,plan_path)
            and entry['binding']['base_audio']==audio, 'Completed acoustic input/source identity changed')
        directory,request=_request(folder,entry['token']);_check_request(root,folder,plan_path,current,request)
        selection_path=media.job_file(folder,entry['result']['selection']);selection=media.read(selection_path)
        media.require(selection['binding']==entry['binding'] and selection['request']==media.record(folder,directory/'request.json'),
                      'Completed acoustic selection changed')
        media.reused_result(folder,entry)
        fallback_path,fallback,new_audio=_torch_rows(root,folder,plan,request,selection['fallback_manifest']['path'])
        media.require(selection['fallback_manifest']==media.record(folder,fallback_path)
            and selection['fallback_audio']==new_audio,'Selected Torch audio changed')
        next_path=media.checked_record(folder,selection['composition']);composition,next_audio=_manifest(root,plan,next_path)
        expected={r['segment_id']:r for r in fallback['utterances']}
        media.require(composition.get('schema')==COMPOSITION and composition.get('replaced_group_ids')==request['group_ids']
            and composition['utterances']==[expected.get(r['segment_id'],r) for r in base['utterances']],
            'Composition replaced unflagged audio or lost exact selected Torch provenance')
        current,base,audio=next_path,composition,next_audio
    raise ValueError('MEDIA_RECOVERY: acoustic composition chain exceeds lifetime budget')
