"""Exact target locks with independently verified, distinct approval sources."""
import hashlib
import math
from pathlib import Path

import website_recovery as recovery


def adoption_invariant_errors(folder, row, target, record, *, previous_context=''):
    """Preserve global guards; account only for an authenticated finite omission."""
    folder = Path(folder).resolve()
    if 'scoped_subset_ref' in record:
        from tagesschau_editorial_partial_20261104 import adoption_invariant_errors as partial
        return partial(folder.parent.parent, folder, row, target, record,
                       previous_context=previous_context)
    if 'scoped_review_receipt' in record:
        from tagesschau_editorial_20261104 import adoption_invariant_errors as scoped
        return scoped(folder.parent.parent, folder, row, target, record,
                      previous_context=previous_context)
    from proxy_client import invariants
    return invariants(row['text_de'], target, previous_context=previous_context,
                      source_language=row.get('source_language', 'de'))


def approved_target_errors(row, target, hint):
    """Ordinary name hints remain substring constraints, never full-text locks."""
    flags=('machine_explicit_continuation','user_contextual_adoption','machine_saved_translation_lock',
           'user_editorial_omission','user_editorial_revision')
    if not isinstance(hint, dict) or not any(hint.get(flag) is True for flag in flags):
        return []
    if sum(hint.get(flag) is True for flag in flags)!=1:
        return ['整句约束不能混淆用户采纳、机器续办及既有真实审校锁的来源']
    expected = hint.get('approved_text_zh')
    required = hint.get('required_strings')
    source = row.get('text_source', row.get('text_de'))
    if (not isinstance(expected, str) or not 1 <= len(expected) <= 600
            or expected != expected.strip()
            or hint.get('approved_text_zh_sha256') != hashlib.sha256(expected.encode()).hexdigest()
            or not isinstance(source, str)
            or hint.get('source_text_sha256') != hashlib.sha256(source.encode()).hexdigest()
            or not isinstance(required, list) or not 1 <= len(required) <= 8
            or any(not isinstance(text, str) or not 1 <= len(text) <= 80 or not text.strip()
                   for text in required)
            or ''.join(required) != expected):
        return ['批准中文整句约束不完整或与源文不符']
    if target != expected:
        return ['译文必须与已核验批准中文整句完全一致']
    return []


def validate_saved_translation_lock(row, hint, folder):
    """Verify a genuine saved approval; never grants a new review approval."""
    if not isinstance(hint,dict) or hint.get('machine_saved_translation_lock') is not True:
        return None
    from proxy_client import VERSION,source_binding
    if approved_target_errors(row,hint.get('approved_text_zh'),hint):
        raise ValueError('既有审校整句锁的全文、哈希或源文约束不完整')
    if any(hint.get(key) is True for key in ('human_reviewed','source_words_verified','human_listened')):
        raise ValueError('既有机器审校整句锁不能标成人工核验')
    folder=Path(folder).resolve();root=folder.parent.parent
    if folder!=recovery._safe(root,Path('jobs')/folder.name):
        raise ValueError('既有审校锁不属于当前任务')
    ref=hint.get('saved_proxy_approval')
    if (not isinstance(ref,dict) or set(ref)!={'path','sha256'} or not isinstance(ref.get('path'),str)
            or not recovery._SHA.fullmatch(str(ref.get('sha256','')))):
        raise ValueError('既有审校锁缺少真实批准引用')
    relative=Path(ref['path'])
    if relative.parent!=Path('translation/diagnostics') or relative.suffix!='.json':
        raise ValueError('既有批准只能引用本任务原始审校诊断')
    path=recovery._safe(folder,relative)
    if not path.is_file() or recovery._sha(path)!=ref['sha256']:
        raise ValueError('既有审校批准文件或SHA不符')
    record=recovery._read(path)
    if (not isinstance(record,dict) or record.get('schema')!='proxy_translation_diagnostic_v1'
            or record.get('version')!=VERSION or record.get('stage') not in {'audit','repair'}
            or record.get('human_reviewed') is not False
            or type(record.get('created_at_unix')) not in (int,float)
            or not math.isfinite(record['created_at_unix'])):
        raise ValueError('既有文件不是实际保存的机器审校记录')
    supplied,response=record.get('input'),record.get('response')
    if (not isinstance(supplied,dict) or not isinstance(supplied.get('items'),list)
            or not isinstance(response,dict) or not isinstance(response.get('translations'),list)):
        raise ValueError('既有审校输入或响应不完整')
    sid=row['segment_id'];binding=source_binding(row);source_text=row.get('text_source',row.get('text_de'))
    inputs=[item for item in supplied['items'] if isinstance(item,dict) and item.get('id')==sid]
    outputs=[item for item in response['translations'] if isinstance(item,dict) and item.get('id')==sid]
    if len(inputs)!=1 or len(outputs)!=1:
        raise ValueError('既有审校没有唯一对应当前源行')
    item,answer=inputs[0],outputs[0]
    if (item.get('source_binding')!=binding or item.get('text_source',item.get('text_de'))!=source_text
            or ('text_de' in item and item['text_de']!=source_text)
            or item.get('source_language',supplied.get('source_language','de'))!=row.get('source_language','de')
            or answer.get('approved') is not True or answer.get('issues')!=[]
            or answer.get('text_zh')!=hint['approved_text_zh']):
        raise ValueError('既有审校未明确批准当前同源完整中文')
    source=recovery._read(recovery._safe(folder,'source.json'))
    video=Path(source.get('input',''))
    if not video.is_absolute() or not video.is_relative_to(root):
        raise ValueError('既有整句锁原片位置不合法')
    video=recovery._safe(root,video.relative_to(root));source_sha=recovery._sha(video)
    state=recovery._safe(root,Path('web/state/jobs')/(folder.name+'.json'))
    if state.is_file() and recovery._read(state).get('source_sha256')!=source_sha:
        raise ValueError('既有整句锁与任务原片身份不符')
    for value in (record.get('input_source_sha256'),supplied.get('input_source_sha256')):
        if value is not None and value!=source_sha:raise ValueError('既有审校记录属于不同原片')
    from pipeline import reviewed_source_plan
    plan,_=reviewed_source_plan(folder,recovery._safe(folder,'segments.plan.json'),source_sha)
    original=[current for current in plan if current.get('segment_id')==sid]
    if len(original)!=1 or source_binding(original[0])!=binding:
        raise ValueError('既有整句锁源文不属于当前已核验计划')
    return {'path':ref['path'],'sha256':ref['sha256'],'stage':record['stage'],
            'created_at_unix':record['created_at_unix'],'input_source_sha256':source_sha,
            'new_review_performed':False}


def validate_approved_targets(rows, folder):
    """Check actual rows immediately before publication or TTS preparation."""
    path = recovery._safe(Path(folder), 'translation/source_review_hints.json')
    if not path.is_file():
        return
    document = recovery._read(path)
    if (not isinstance(document, dict) or document.get('schema') != 'source_review_hints_v1'
            or not isinstance(document.get('rows'), dict)):
        raise RuntimeError('译稿整句约束记录不合法')
    failed=[]
    for row in rows:
        hint=document['rows'].get(row['segment_id'])
        try:validate_saved_translation_lock(row,hint,folder)
        except ValueError as error:
            raise RuntimeError('既有审校整句锁来源不合法：'+row['segment_id']+'；'+str(error)) from None
        if approved_target_errors(row,row.get('text_zh'),hint):failed.append(row['segment_id'])
    if failed:
        raise RuntimeError('实际译稿与已核验批准整句不一致：'+','.join(failed))
