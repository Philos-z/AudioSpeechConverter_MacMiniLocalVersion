"""Local failure evidence and repeat protection. No network or code execution.

This module never grants an automatic retry or changes recovery.py's policy.
Only a current, input-bound content rejection may stop an unchanged manual
retry. Network, cancellation, resource and unknown failures remain resumable.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time

SOURCE_GATE='机器译稿已保存；原文词义仍待核对：'
TRANSLATION_STAGE='机器翻译与第二遍审校'
LOCAL_TRANSLATION_STAGE='Proxy处理局部译稿并独立复核'
_SID=re.compile(r'^line\d{4,}$')
_SHA=re.compile(r'^[a-f0-9]{64}$')
_HASHES={}


def _read(path,default=None):
    try:
        if Path(path).stat().st_size>4*1024**2:return default
        return json.loads(Path(path).read_text())
    except (OSError,ValueError):return default


def _sha(path):
    path=Path(path)
    try:
        stat=path.stat();key=(str(path),stat.st_mtime_ns,stat.st_size,stat.st_ino)
        cached=_HASHES.get(key)
        if cached is not None:return cached
        value=hashlib.sha256(path.read_bytes()).hexdigest()
        if len(_HASHES)>512:_HASHES.clear()
        _HASHES[key]=value
        return value
    except OSError:return None


def checkpoint_marker(root,jid):
    path=Path(root)/'jobs'/jid/'translation/checkpoint.json'
    try:return (path.stat().st_mtime_ns,path.stat().st_size,_sha(path))
    except OSError:return None


def _input(root,job):
    folder=Path(root)/'jobs'/job['id']
    data=_read(folder/'translation/input.json',{})
    rows=data.get('segments',[]) if isinstance(data,dict) else []
    if not isinstance(rows,list):return folder,[],{}
    valid=[r for r in rows if isinstance(r,dict) and _SID.fullmatch(str(r.get('segment_id','')))]
    return folder,rows,{r['segment_id']:r for r in valid}


def _bound_checkpoint(folder,rows):
    checkpoint=_read(folder/'translation/checkpoint.json',{})
    if not isinstance(checkpoint,dict):return {}
    try:identity=hashlib.sha256(json.dumps(rows,sort_keys=True,ensure_ascii=False,
        separators=(',',':'),allow_nan=False).encode()).hexdigest()
    except (TypeError,ValueError):return {}
    return checkpoint if (checkpoint.get('schema')=='translation_checkpoint_v1'
        and checkpoint.get('input_rows_sha256')==identity and checkpoint.get('total')==len(rows)) else {}


def _revision_fingerprint(root,job,*,failure_category=None):
    root=Path(root).resolve();folder,rows,by_id=_input(root,job)
    paths=[root/'web'/name for name in ('pipeline.py','proxy_client.py','numeric_calendar_context.py','source_language.py')]
    paths += [root/'config'/name for name in ('local.json','web_proxy.json')]
    paths += [folder/name for name in ('source_plan_revision.json','segments.plan.json','segments.json',
        'translation/input.json','translation/source_review_hints.json','translation/source_quality_notes.json','translation/independent_content_review.json','translation/prior_review_reuse.json','translation/report.json','translation/checkpoint.json')]
    paths.append(folder/'revisions/source_review_v1/segments.plan.json')
    paths += [root/'web'/name for name in ('website_recovery.py','media_recovery.py','website_review.py',
                                         'translation_recovery.py','translation_reuse.py','source_audio_recheck.py')]
    # A fixed same-text audio fallback is relevant only to a bound acoustic
    # rejection. Its installation/change cannot unlock text or unknown faults.
    if failure_category=='audio':paths.append(root/'web/acoustic_fallback.py')
    paths += [folder/name for name in ('tts/no_join_recovery.json','tts/picture_holds.json',
        'tts/media_recovery_journal.json','recovery/website_journal.json')]
    revision=_read(folder/'source_plan_revision.json',{})
    if isinstance(revision,dict):
        changes=revision.get('changes',[])
        for change in changes if isinstance(changes,list) else []:
            evidence=change.get('evidence',[]) if isinstance(change,dict) else []
            for proof in evidence if isinstance(evidence,list) else []:
                if not isinstance(proof,dict) or not isinstance(proof.get('path'),str):continue
                path=(folder/proof['path']).resolve()
                if path.is_relative_to(folder/'revisions'):paths.append(path)
    checkpoint=_bound_checkpoint(folder,rows);keys=set()
    batches=checkpoint.get('batches',{});batches=batches if isinstance(batches,dict) else {}
    for batch in batches.values():
        key=batch.get('cache_key') if isinstance(batch,dict) else None
        if isinstance(key,str) and _SHA.fullmatch(key):keys.add(key)
    # Legacy failed checkpoints omit the key; actual text-only diagnostics bind
    # it to this job's current rows. Unrelated jobs' cache activity is excluded.
    for path in sorted((folder/'translation/diagnostics').glob('*.json')):
        record=_read(path,{})
        if not isinstance(record,dict):continue
        supplied=record.get('input');items=supplied.get('items',[]) if isinstance(supplied,dict) else []
        if not isinstance(items,list) or not items or not all(isinstance(item,dict) and item.get('id') in by_id and
                item.get('text_de')==by_id[item['id']].get('text_de') for item in items):continue
        paths.append(path);key=record.get('batch_key')
        if isinstance(key,str) and _SHA.fullmatch(key):keys.add(key)
    paths += [root/'cache/proxy_translation_v2'/(key+'.json') for key in sorted(keys)]
    files={str(path.relative_to(root)):_sha(path) for path in paths if path.is_relative_to(root)}
    value={'source_sha256':job.get('source_sha256'),'options':job.get('options',{}),'files':files}
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def _translation_receipt_valid(folder,checkpoint,rows):
    ledger=_read(folder/'pipeline_receipts.json',{});report=_read(folder/'translation/report.json',{})
    item=ledger.get('translation',{}) if isinstance(ledger,dict) else {}
    outputs=item.get('outputs',[]) if isinstance(item,dict) else []
    if not isinstance(outputs,list) or not outputs or not all(isinstance(x,dict) for x in outputs):return False
    expected={str((folder/'segments.json').resolve()),str((folder/'translation/report.json').resolve())}
    source_hash=hashlib.sha256(json.dumps(rows,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    data=_read(folder/'segments.json',{});translated=data.get('segments',[]) if isinstance(data,dict) else []
    correct_rows=(isinstance(translated,list) and len(translated)==len(rows) and all(
        isinstance(before,dict) and isinstance(row,dict) and all(row.get(key)==before.get(key) for key in
            ('segment_id','speaker_id','start_s','end_s','text_de','text_source','source_language'))
        and isinstance(row.get('text_zh'),str) and bool(row['text_zh'].strip())
        and row.get('translation_reviewed') is True and row.get('human_reviewed') is False
        and row.get('translation_review_method')=='proxy_second_pass_and_numeric_checks'
        for before,row in zip(rows,translated)))
    return bool(checkpoint and checkpoint.get('completed')==len(rows) and rows and correct_rows and
        isinstance(report,dict) and report.get('human_reviewed') is False
        and report.get('review_method')=='two_pass_machine_review_and_numeric_invariants'
        and report.get('source_input_rows_sha256')==source_hash
        and report.get('source_speakers_sha256')==_sha(folder/'speakers.json')
        and report.get('source_revision_receipt_sha256')==_sha(folder/'source_plan_revision.json')
        and report.get('source_segments_sha256')==_sha(folder/'segments.json') and
        {x.get('path') for x in outputs if isinstance(x,dict)}==expected and
        all(_sha(x['path'])==x.get('sha256') for x in outputs))


def _check_labels(issues):
    """Expose fixed labels, never model-written explanations or capabilities."""
    labels=set()
    for issue in issues if isinstance(issues,list) else []:
        if not isinstance(issue,str):continue
        for words,label in ((('数字','数量'),'数字'),(('单位',),'单位'),(('否定',),'否定'),
                (('条件',),'条件'),(('比例','百分比'),'比例'),(('专名','人名'),'专名'),
                (('原文','原词','转写','转录','识别','词义','含义','语义不完整'),'原文识别')):
            if any(word in issue for word in words):labels.add(label)
    return sorted(labels)


def _local_translation_failure(root,job,folder,rows,by_id):
    """Read current content rejections and owned recovery evidence only.

    Journal reason strings cannot establish a network failure or retry right.
    A historical event may explain a rejection only while its request still
    binds the current rows, source, code and review constraints.
    """
    if not rows:return None
    try:
        import website_recovery as recovery
        from translation_recovery import pending,source_pending
        rejected=pending(folder,rows)
        source_ids=set(source_pending(folder,rows))
        ids=[row['segment_id'] for row in rows if row.get('segment_id') in set(rejected)|source_ids]
        if not ids:return None
        journal=recovery._journal(folder)
        bound=[]
        policy=recovery.normalize_policy(job.get('options',{}).get('website_recovery',{}))
        for event in journal['events']:
            if (event['action']!='translation' or event['status']!='failed'
                    or not set(event['segment_ids'])&set(ids)):continue
            evidence=recovery._safe(folder,'revisions/source_review_v1/evidence/translation_'+event['id'])
            request=_read(recovery._safe(evidence,'request.json'),{})
            if (not isinstance(request,dict) or request.get('schema')!='translation_rejection_recovery_v1'
                    or request.get('action')!='translation'
                    or recovery._hash({k:v for k,v in request.items() if k!='items'})!=event['input_fingerprint']
                    or request.get('input_rows_sha256')!=recovery._hash(rows)
                    or request.get('source_sha256')!=job.get('source_sha256')
                    or request.get('policy')!=policy
                    or request.get('source_revision')!=_sha(folder/'source_plan_revision.json')
                    or request.get('quality_notes_sha256')!=_sha(folder/'translation/source_quality_notes.json')
                    or request.get('independent_content_review_sha256')!=_sha(folder/'translation/independent_content_review.json')
                    or request.get('code')!={name:_sha(Path(root)/'web'/name) for name in
                        ('translation_recovery.py','source_audio_recheck.py','proxy_client.py','numeric_calendar_context.py')}):continue
            items=request.get('items')
            if (not isinstance(items,list) or [item.get('id') for item in items if isinstance(item,dict)]!=event['segment_ids']
                    or any(item.get('id') not in by_id or item.get('text_source')!=by_id[item['id']].get('text_de')
                           for item in items)):continue
            bound.append((event,evidence))
        latest=bound[-1] if bound else None
        same_input=(sum(event['input_fingerprint']==latest[0]['input_fingerprint']
                        for event in journal['events'] if event['action']=='translation') if latest else 0)
        exhausted=len(journal['events'])>=recovery.MAX_ACTIONS or same_input>=recovery.MAX_PER_INPUT
        issues={sid:_check_labels(rejected.get(sid,{}).get('checks',[])+rejected.get(sid,{}).get('review_issues',[])) for sid in ids}
        for sid in ids:
            if sid in source_ids and '原文识别' not in issues[sid]:issues[sid].append('原文识别')
            if not issues[sid]:issues[sid]=['独立二审']
        if latest and latest[0].get('reason')=='invariant_rejected':
            local=_read(recovery._safe(latest[1],'local_checks.json'),{})
            checks=local.get('checks',{}) if isinstance(local,dict) else {}
            if isinstance(checks,dict):
                for sid in ids:
                    labels=_check_labels(checks.get(sid,[]))
                    if labels:issues[sid]=sorted(set(issues[sid]+labels))
        return {'ids':ids,'issues':issues,'exhausted':exhausted,'source_pending':bool(source_ids),
                'invariant_rejected':bool(latest and latest[0].get('reason')=='invariant_rejected')}
    except (OSError,ValueError,TypeError,KeyError,AttributeError):
        # Broken/stale evidence must neither invent a cause nor block a retry.
        return None


def make_failure_record(root,job,error,stage=None,checkpoint_before=None):
    folder,rows,by_id=_input(root,job);checkpoint=_bound_checkpoint(folder,rows)
    stage=stage or job.get('stage','unknown');kind='unknown';ids=[];checks=[];policy='allowed'
    title='处理需要检查';message='本次处理未完成，已有成果已保留。';action='可从断点继续；若再次出现，将保留本次诊断记录。'
    metadata=getattr(error,'failure_metadata',{})
    transport=(isinstance(metadata,dict) and metadata.get('schema')=='bounded_stage_failure_v1'
        and metadata.get('origin')=='proxy_transport' and metadata.get('stage')=='translation')
    text=str(error)
    revision=_read(folder/'source_plan_revision.json',{})
    revision=revision if isinstance(revision,dict) else {}
    blocked=revision.get('blocking_unresolved',[])
    local_translation=None;row_checks={}
    if type(error).__name__=='Cancelled' or '任务已取消' in text:
        kind='cancelled';title='任务已取消';message='任务已停止，已完成步骤保留。';action='需要时可以继续此任务。'
    elif transport and metadata.get('category') in {'transport_timeout','connection_reset','http_response'}:
        kind='transport';title='翻译服务连接暂时异常';message='出现了有实际请求证据的连接故障。';action='连接恢复后可以继续，自动重试仍受原有次数预算限制。'
    elif (isinstance(error,OSError) and getattr(error,'errno',None)==28) or '空间不足' in text:
        kind='storage';title='磁盘余量不足';message='磁盘空间未满足本任务的安全余量。';action='腾出空间后可以继续，原视频与旧成果不会被自动删除。'
    elif (text.startswith(SOURCE_GATE) and revision.get('schema')=='source_plan_revision_v1'
            and isinstance(blocked,list) and blocked and all(sid in by_id for sid in blocked)):
        kind='source_review';policy='after_change';ids=blocked
        title='等待原文核对';message='机器译稿已保存，原片中仍有词义未确认。'
        action='请核对下方原声位置并更新原文修订；确认后刷新此页继续。直接重试不会消除词义疑点。'
    elif (stage==LOCAL_TRANSLATION_STAGE and
            (local_translation:=_local_translation_failure(root,job,folder,rows,by_id))):
        kind='translation_review';policy='after_change';ids=local_translation['ids']
        row_checks=local_translation['issues'];checks=[label for values in row_checks.values() for label in values]
        title='局部译稿检查待处理'
        message=('局部恢复未通过数字、单位或逻辑检查。' if local_translation['invariant_rejected'] else
            '局部译稿的原文识别仍待核对。' if local_translation['source_pending'] else
            '局部译稿仍未通过独立第二遍审校。')
        action='请核对下方问题句及原声位置，保存有效更正后再继续。已通过的批次保留，重复点击不会跳过检查。'
        if local_translation['exhausted']:
            title='局部译稿恢复次数已用完'
            message+='本任务或同一输入的自动恢复次数已用完，已有成果和检查证据保留。'
    elif '配音' in stage or '字幕' in stage or '画面' in text or 'Proxy处理局部' in stage:
        plan=_read(folder/'tts/semantic_group_plan.json',{})
        current=_sha(folder/'segments.json')
        pending=[]
        for path in sorted((folder/'tts').glob('audio_quality_*.json')):
            proof=_read(path,{})
            if (isinstance(proof,dict) and proof.get('complete') is True
                    and proof.get('binding',{}).get('source_segments_sha256')==current):
                pending += proof.get('needs_repair_ids',[])
        timing=_read(folder/'timing_review_required.json',{})
        if (isinstance(timing,dict) and timing.get('source_segments_sha256')==current
                and timing.get('plan_sha256')==_sha(folder/'tts/semantic_group_plan.json')):
            groups={g.get('group_id'):g.get('member_ids',[]) for g in plan.get('tts_groups',[]) if isinstance(g,dict)}
            pending += [sid for issue in timing.get('issues',[]) for sid in groups.get(issue.get('group_id'),[])]
            if pending:kind='timing'
        ids=sorted({sid for sid in pending if sid in by_id})
        if ids:
            kind='timing' if kind=='timing' else 'audio';policy='after_change'
            title='局部画面时长待处理' if kind=='timing' else '局部配音待处理'
            message='已保留全部原句和完整声音，当前局部恢复仍未通过。'
            action='可在网站听原句并核对译稿，保存更正后继续；自动恢复次数不会因重复点击重置。'
    elif (stage==TRANSLATION_STAGE and checkpoint and
            checkpoint_marker(root,job['id'])!=checkpoint_before):
        batches=checkpoint.get('batches',{});batches=batches if isinstance(batches,dict) else {}
        failed=[batch for batch in batches.values()
            if isinstance(batch,dict) and batch.get('status')=='failed'
            and batch.get('error_type') in {'InvariantRejectedError','ReviewRejectedError'}]
        from translation_recovery import pending
        ids=list(pending(folder,rows))
        if ids:
            kind='translation_review';policy='after_change';title='内容检查待处理'
            message='译稿检查仍有未解决项目，已保存通过的批次。'
            action='请核对提示句子的原文、译文或校验规则；修正后再继续，避免重复请求相同内容。'
            for batch in failed:
                for sid,issues in (batch.get('checks',{}) or {}).items():
                    if sid not in by_id:continue
                    for issue in issues:
                        checks += [label for word,label in [('数字','数字'),('单位','单位'),('否定','否定'),
                            ('条件','条件'),('比例','比例'),('专名','专名')] if word in str(issue)]
    windows=[{'id':sid,'start_s':by_id[sid].get('start_s'),'end_s':by_id[sid].get('end_s'),
              **({'checks':row_checks[sid]} if sid in row_checks else {})} for sid in ids]
    verified_translation=(kind=='source_review' and _translation_receipt_valid(folder,checkpoint,rows))
    has_counts=(kind=='translation_review' or verified_translation)
    record={'schema':'local_failure_diagnosis_v1','category':kind,'title':title,'message':message,
        'next_action':action,'segment_ids':ids,'source_windows':windows,'checks':sorted(set(checks)),
        'retry_policy':policy,'stage':stage,'error_type':type(error).__name__,
        'confirmed_code_bug':False,'automatic_code_edits_performed':False,
        'local_evidence':{'job_relative':{'checkpoint':'translation/checkpoint.json',
            'input':'translation/input.json','source_revision':'source_plan_revision.json'},
            'project_relative':{'execution_log':'reports/web_app/jobs/'+job['id']+'.log',
                'pipeline':'web/pipeline.py','translation_client':'web/proxy_client.py'}},
        'completed':checkpoint.get('completed') if has_counts else None,
        'total':checkpoint.get('total') if has_counts else None,
        'revision_fingerprint':_revision_fingerprint(root,job,failure_category=kind),
        'created_at':datetime.now(timezone.utc).isoformat()}
    if verified_translation:record['progress_floor']=58.0
    return record


def persist_failure(root,jid,record):
    folder=Path(root)/'jobs'/jid/'recovery_diagnostics';folder.mkdir(parents=True,exist_ok=True)
    path=folder/(str(time.time_ns())+'.json');temp=path.with_suffix('.pending')
    temp.write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n');temp.chmod(0o600);temp.replace(path)
    return path


def public_failure(root,job):
    record=job.get('failure_record')
    if not isinstance(record,dict) or record.get('schema')!='local_failure_diagnosis_v1':return None
    keys=('category','title','message','next_action','segment_ids','source_windows','checks','completed','total','retry_policy','progress_floor')
    result={key:record[key] for key in keys if key in record}
    result['can_retry']=(record.get('retry_policy')!='after_change' or
        record.get('revision_fingerprint')!=_revision_fingerprint(root,job,failure_category=record.get('category')))
    if record.get('retry_policy')=='after_change' and result['can_retry']:
        result['next_action']='已检测到新的修订，可继续任务并重新检查受影响的步骤。'
    return result


def retry_message(root,job):
    failure=public_failure(root,job)
    if failure and not failure['can_retry']:
        return failure['title']+'：当前没有新的修订，继续会遇到同一问题。请先核对原文、修正译稿或检查规则。'
    return None
