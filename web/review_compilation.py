"""CPU-only verification of explicitly scoped review records; no network or mutation."""
import copy
import hashlib
import json
import re
from pathlib import Path

SCHEMA='targeted_timing_proxy_review_compilation_v1'
GROUNDED_PURPOSE='source_grounded_operator_timing_review_v1'
NARROW_PURPOSE='source_grounded_final_narrow_review_v1'
FILES={'grounded':'translation/timing_review/source_grounded_operator_review.json',
       'narrow':'translation/timing_review/final_narrow_resubmission.json'}
FAILED_NARROW='translation/timing_review/final_narrow_review.json'
TEXT_FIELDS={'id','text_zh','approved','issues'}
GROUP_FIELDS={'member_ids','approved','same_topic','topic_boundary_crossed','quote_speaker_change','reason'}

def read(path):return json.loads(Path(path).read_text())
def registration(folder):
    folder=Path(folder).resolve();path=folder.parent.parent/'config/source_scopes.local.json'
    if folder.parent.name!='jobs' or not path.is_file() or path.is_symlink() or path.resolve()!=path:fail('No local review-compilation scope is registered')
    value=read(path)
    if not isinstance(value,dict) or value.get('schema')!='local_source_scopes_v1':fail('Invalid local source-scope schema')
    item=value.get('review_compilation');fields={'job','narrow_ids','cpu_source_id','grounded_translation_count','grounded_group_count'}
    if not isinstance(item,dict) or set(item)!=fields or item['job']!=folder.name:fail('No valid local review-compilation scope is registered')
    ids=item['narrow_ids']
    if (not isinstance(ids,list) or not ids
            or not all(isinstance(s,str) and re.fullmatch(r'line\d{4}',s) for s in ids) or len(set(ids))!=len(ids)
            or item['cpu_source_id'] not in ids):fail('Invalid registered review source IDs')
    if (any(type(item[k]) is not int or item[k]<=0 for k in ('grounded_translation_count','grounded_group_count'))
            or item['grounded_translation_count']<len(ids)):fail('Invalid registered review coverage')
    return item
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def fp(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
def fail(message):raise ValueError(message)
def confined(folder,relative):
    if not isinstance(relative,str) or Path(relative).is_absolute() or '..' in Path(relative).parts:fail('Review path must be job-relative')
    value=folder/relative
    if value.is_symlink() or value.resolve()!=value or not value.is_relative_to(folder) or not value.is_file():fail('Review path escapes job or is missing/symlink')
    return value

def index(items,field,fields=None):
    if not isinstance(items,list):fail('Review items must be arrays')
    result={}
    for item in items:
        if not isinstance(item,dict) or fields is not None and set(item)!=fields:fail('Review item has missing/extra fields')
        value=item.get(field)
        if field=='member_ids':
            if not isinstance(value,list) or len(value)<2 or not all(isinstance(x,str) and x for x in value) or len(set(value))!=len(value):fail('Invalid group IDs')
            value=tuple(value)
        elif not isinstance(value,str) or not value:fail('Invalid review ID')
        if value in result:fail('Duplicate review ID')
        result[value]=item
    return result

def actual(record,purpose,job):
    if (record.get('schema')!='targeted_timing_proxy_review_v1' or record.get('status')!='response_received'
            or record.get('human_reviewed') is not False or record.get('purpose')!=purpose):fail('Not an actual fixed-purpose review')
    calls=record.get('requests')
    if not isinstance(calls,list) or len(calls)!=1 or calls[0].get('text_only') is not True:fail('Exactly one real text-only call per underlying record is required')
    request=record.get('request');response=record.get('response')
    if not isinstance(request,dict) or request.get('job')!=job or request.get('purpose')!=purpose or not isinstance(response,dict) or set(response)!={'translations','groups'}:fail('Underlying job or response differs')
    ti=index(request.get('translations'),'id');tr=index(response['translations'],'id',TEXT_FIELDS)
    gi=index(request.get('groups'),'member_ids');gr=index(response['groups'],'member_ids',GROUP_FIELDS)
    if set(ti)!=set(tr) or set(gi)!=set(gr):fail('Actual response has missing/extra IDs')
    for sid,item in tr.items():
        if type(item['approved']) is not bool or not isinstance(item['issues'],list) or not all(isinstance(x,str) for x in item['issues']):fail('Invalid actual decision')
        if item['text_zh']!=ti[sid].get('proposed_text_zh'):fail('Actual response rewrote proposal')
    if purpose==NARROW_PURPOSE and (record.get('explicit_operator_resubmission') is not True or record.get('old_failed_call_still_counts') is not True or record.get('same_request_as_failed_record') is not True or record.get('automatic_timing_attempts_remain_spent')!=2 or record.get('operator_narrow_max_calls')!=1):fail('Only the explicitly initiated one-off resubmission is allowed')
    return request,ti,tr,gi,gr

def approved_text(item):return item.get('approved') is True and item.get('issues')==[]
def approved_group(item):return (item.get('approved') is True and item.get('same_topic') is True
    and item.get('topic_boundary_crossed') is False and item.get('quote_speaker_change') is False
    and isinstance(item.get('reason'),str) and bool(item['reason'].strip()))

def bound_file(folder,item,path_key='path'):
    if not isinstance(item,dict) or not isinstance(item.get(path_key),str):fail('Missing source receipt')
    value=Path(item[path_key])
    if not value.is_absolute() or value.is_symlink() or value.resolve()!=value or not value.is_relative_to(folder/'translation') or sha(value)!=item.get('sha256'):fail('Source evidence path/SHA changed')
    return value

def verify_grounded_files(folder,grounded):
    grounding=grounded.get('source_grounding');request=grounded['request']
    if (not isinstance(grounding,dict) or request.get('source_grounding')!=grounding
            or grounded.get('automatic_timing_attempts_remain_spent')!=2
            or grounded.get('operator_review_max_calls')!=1):fail('Underlying source-grounding/budgets differ')
    for field in ('source_manifest','official_xml','official_page'):bound_file(folder,grounding.get(field))
    source=grounding.get('source_video_binding',{});video=Path(source.get('path',''))
    actual_source=Path(read(folder/'source.json')['input']).resolve()
    if video!=actual_source or not video.is_file() or sha(video)!=source.get('sha256'):fail('Actual source video changed')
    previous=grounding.get('prior_timing_reviews')
    if not isinstance(previous,list) or len(previous)!=2:fail('Prior automatic review receipts missing')
    expected={'translation/timing_review/actual_proxy_review.json','translation/timing_review/actual_proxy_review_attempt2.json'}
    paths=set()
    for item in previous:
        absolute=Path(item.get('path',''))
        if not absolute.is_absolute() or not absolute.is_relative_to(folder):fail('Prior automatic path escapes job')
        relative=str(absolute.relative_to(folder));path=confined(folder,relative)
        if sha(path)!=item.get('sha256'):fail('Prior automatic review changed')
        paths.add(relative)
    if paths!=expected:fail('Prior automatic records are not the two fixed originals')

def verify_cpu_source(folder,item,grounded_item,grounded):
    registered=registration(folder)
    evidence=item.get('source_evidence')
    if not isinstance(evidence,dict) or evidence.get('kind')!='independent_target_free_cpu_source_v1':fail('Missing independent CPU source evidence')
    report_path=bound_file(folder,evidence.get('report'));audio_path=bound_file(folder,evidence.get('audio'))
    report=read(report_path)
    if (report.get('schema')!='independent_source106_local_turbo_target_free_crosscheck_v1'
            or report.get('job')!=folder.name or report.get('segment_id')!=registered['cpu_source_id']
            or report.get('gpu_used') is not False or report.get('target_conditioning') is not False
            or report.get('recognition_count')!=1 or report.get('downloaded') is not False
            or report.get('proxy_calls')!=0 or report.get('production_or_original_audio_modified') is not False):fail('CPU source report is not the independent one-shot observation')
    opts=report.get('options',{});model=report.get('model',{})
    if (opts.get('language')!='de' or opts.get('initial_prompt') is not None
            or opts.get('condition_on_previous_text') is not False or opts.get('fp16') is not False
            or model.get('device')!='cpu' or model.get('actual_parameter_devices')!=['cpu']):fail('Source decoder was prompted or used non-CPU model')
    if report.get('audio',{}).get('sha256')!=sha(audio_path):fail('CPU decoded audio SHA differs')
    if (report.get('source_video',{}).get('sha256')!=grounded['source_grounding']['source_video_binding']['sha256']
            or report.get('raw_segments_sha256')!=grounded['request']['base_segments_sha256']
            or report.get('original_source_row',{}).get('text_de')!=grounded_item['text_de']):fail('CPU source belongs to another input/raw line')
    canonical=report.get('recognized_text','').strip()
    if not canonical or item.get('verified_source_text_de')!=canonical or evidence.get('canonical_text_de')!=canonical:fail('Canonical source differs from actual CPU result')

def derive(folder,record,final_request):
    folder=Path(folder).resolve();registered=registration(folder);narrow_ids=frozenset(registered['narrow_ids'])
    if (record.get('schema')!=SCHEMA or record.get('status')!='compiled_from_actual_approvals'
            or record.get('human_reviewed') is not False or record.get('final_request')!=final_request
            or 'requests' in record):fail('Not an explicit local compilation')
    if not isinstance(final_request,dict) or final_request.get('job')!=folder.name:fail('Compiled request belongs to another job')
    sources=record.get('sources')
    if not isinstance(sources,dict) or set(sources)!=set(FILES):fail('Only the two fixed actual records may supply approvals')
    underlying={}
    for label,relative in FILES.items():
        item=sources[label]
        if not isinstance(item,dict) or set(item)!={'relative_path','sha256'} or item.get('relative_path')!=relative:fail('Source is not the fixed job record')
        path=confined(folder,relative)
        if sha(path)!=item.get('sha256'):fail('Actual source review SHA changed')
        underlying[label]=read(path)
    g,gt,gr,gg,ggresp=actual(underlying['grounded'],GROUNDED_PURPOSE,folder.name)
    n,nt,nr,ng,ngresp=actual(underlying['narrow'],NARROW_PURPOSE,folder.name)
    previous=underlying['narrow'].get('resubmission_of')
    if not isinstance(previous,dict) or set(previous)!={'relative_path','sha256'} or previous.get('relative_path')!=FAILED_NARROW:fail('Resubmission must retain the fixed failed call')
    failed_path=confined(folder,FAILED_NARROW)
    if sha(failed_path)!=previous.get('sha256'):fail('Historical failed-call record changed')
    failed=read(failed_path)
    if (failed.get('schema')!='targeted_timing_proxy_review_v1' or failed.get('status')!='failed_no_retry' or failed.get('error_class')!='AttributeError' or 'response' in failed or failed.get('purpose')!=NARROW_PURPOSE or failed.get('human_reviewed') is not False or failed.get('request')!=n):fail('Only the exact response-lost local bug is eligible; failed evidence stays immutable')
    if record.get('failed_call')!=previous:fail('Compilation must separately disclose the lost real call')
    if (len(gt)!=registered['grounded_translation_count'] or len(gg)!=registered['grounded_group_count'] or set(nt)!=narrow_ids or ng or ngresp):fail('Only the registered grounded and narrow scopes are allowed')
    if set(sid for sid,r in gr.items() if not approved_text(r))!=narrow_ids:fail('Grounded record must have exactly the two actual rejected translations')
    if any(gr[sid]['approved'] is not False or not gr[sid]['issues'] for sid in narrow_ids):fail('The two source decisions must be actual explicit rejections')
    if not all(approved_group(v) for v in ggresp.values()) or not all(approved_text(v) for v in nr.values()):fail('Rejected/ambiguous approvals cannot be compiled')
    ft=index(final_request.get('translations'),'id');fg=index(final_request.get('groups'),'member_ids')
    if set(ft)!=set(gt) or fg!=gg:fail('Final scope or approved group rows/context changed')
    for key in ('job','base_segments_sha256','source_speakers_sha256'):
        if final_request.get(key)!=g.get(key) or n.get(key)!=g.get(key):fail('Raw base/job/speaker binding changed')
    if n.get('source_segments_sha256')!=final_request.get('source_segments_sha256'):fail('Narrow did not review this final full prediction')
    serialized=(json.dumps(final_request,ensure_ascii=False,indent=2)+'\n').encode()
    if n.get('final_request_sha256')!=hashlib.sha256(serialized).hexdigest():fail('Narrow does not bind the exact frozen full request bytes')
    top={k:v for k,v in final_request.items() if k not in ('translations','groups','source_segments_sha256')}
    if top!={k:v for k,v in g.items() if k not in ('translations','groups','source_segments_sha256')}:fail('Final request changed unrelated source/purpose/evidence')
    if n.get('source_grounded_record')!=sources['grounded']:fail('Narrow does not bind its actual grounded predecessor')
    verify_grounded_files(folder,underlying['grounded'])
    out=[];approval_text=[]
    for sid,item in ft.items():
        if sid not in narrow_ids:
            if item!=gt[sid] or not approved_text(gr[sid]):fail('An unrelated previously approved item changed')
            chosen=gr[sid];label='grounded'
        else:
            if item!=nt[sid]:fail('Final narrow text/source is not the actual narrow request')
            allowed={'proposed_text_zh'}|({'verified_source_text_de','source_evidence'} if sid==registered['cpu_source_id'] else set())
            if {k:v for k,v in item.items() if k not in allowed}!={k:v for k,v in gt[sid].items() if k not in allowed}:fail('Narrow changed raw source/context or unrelated evidence')
            if sid==registered['cpu_source_id']:verify_cpu_source(folder,item,gt[sid],underlying['grounded'])
            chosen=nr[sid];label='narrow'
        if chosen['text_zh']!=item.get('proposed_text_zh'):fail('Final Chinese differs from its actual approved response')
        out.append(copy.deepcopy(chosen));approval_text.append({'id':sid,'source':label,'source_id':sid})
    group_response=[copy.deepcopy(ggresp[tuple(x['member_ids'])]) for x in final_request['groups']]
    approval_groups=[{'member_ids':x['member_ids'],'source':'grounded','source_member_ids':x['member_ids']} for x in final_request['groups']]
    expected_map={'translations':approval_text,'groups':approval_groups}
    if record.get('approval_map')!=expected_map:fail('Approval map does not match the real per-ID origins')
    if (record.get('underlying_actual_call_count')!=2 or record.get('prior_automatic_call_count')!=2
            or record.get('lost_response_actual_call_count')!=1 or record.get('lifetime_actual_call_count')!=5 or record.get('network_requests_made_by_compilation')!=0):fail('Actual calls must be counted honestly')
    return {x['id']:x for x in out},group_response

def validate(folder,record,final_request):
    texts,groups=derive(folder,record,final_request)
    response={'translations':[texts[x['id']] for x in final_request['translations']],'groups':groups}
    if record.get('response')!=response:fail('Stored local compilation differs from recomputed real approvals')
    return texts,groups

def compile_record(folder,final_request):
    """Return only a verified in-memory local compilation; caller saves explicitly."""
    folder=Path(folder).resolve();registered=registration(folder);narrow_ids=frozenset(registered['narrow_ids']);sources={label:{'relative_path':rel,'sha256':sha(confined(folder,rel))} for label,rel in FILES.items()}
    record={'schema':SCHEMA,'status':'compiled_from_actual_approvals','human_reviewed':False,
        'final_request':copy.deepcopy(final_request),'sources':sources,
        'approval_map':{'translations':[{'id':x['id'],'source':'narrow' if x['id'] in narrow_ids else 'grounded','source_id':x['id']} for x in final_request['translations']],
            'groups':[{'member_ids':x['member_ids'],'source':'grounded','source_member_ids':x['member_ids']} for x in final_request['groups']]},
        'underlying_actual_call_count':2,'prior_automatic_call_count':2,'lost_response_actual_call_count':1,'lifetime_actual_call_count':5,
        'failed_call':copy.deepcopy(read(confined(folder,FILES['narrow']))['resubmission_of']),
        'network_requests_made_by_compilation':0}
    texts,groups=derive(folder,record,final_request);record['response']={'translations':[texts[x['id']] for x in final_request['translations']],'groups':groups};validate(folder,record,final_request);return record
