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
AUDIO_REVIEW_FAILURE_SCHEMA='current_complete_audio_review_failure_v1'
AUDIO_REVIEW_FAILURE_ORIGIN='local_complete_audio_quality'


def _audio_evidence_document(path):
    """Read a bound complete-audio proof within its separate 16 MiB limit."""
    limit=16*1024**2
    with Path(path).open('rb') as stream:
        raw=stream.read(limit+1)
    if len(raw)>limit:raise ValueError('Oversized audio chain evidence')
    return json.loads(raw)


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
        digest=hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
        value=digest.hexdigest()
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
    # This one job's saved-WAV decision reader can stop later media phases.
    # Include its actual revision even when an older timing artifact supplies
    # the diagnostic category; changing it never replenishes any budget.
    paths.append(root/'web/portable_private_hooks.py')
    paths += [root/'config'/name for name in ('local.json','web_proxy.json')]
    paths += [folder/name for name in ('source_plan_revision.json','segments.plan.json','segments.json',
        'translation/input.json','translation/source_review_hints.json','translation/source_quality_notes.json','translation/independent_content_review.json','translation/prior_review_reuse.json','translation/report.json','translation/checkpoint.json')]
    paths.append(folder/'revisions/source_review_v1/segments.plan.json')
    paths += [root/'web'/name for name in ('website_recovery.py','media_recovery.py','website_review.py',
                                         'translation_recovery.py','translation_reuse.py','source_audio_recheck.py')]
    # A fixed same-text audio fallback is relevant only to a bound acoustic
    # rejection. Its installation/change cannot unlock text or unknown faults.
    if failure_category=='audio':
        paths.append(root/'web/acoustic_fallback.py')
        paths.append(folder/'tts/media_recovery_journal.json')
        paths.append(root/'web/user_voice_adoption_20261103.py')
        paths.append(folder/'tts/user_voice_adoption/active.json')
    if failure_category=='reference':
        paths += [root/'scripts'/name for name in ('speaker_plan.py','speaker_references.py')]
        paths += [folder/name for name in ('source.json','segments.asr.json','speaker_assignments.json',
            'speakers.json','speaker_machine_review.json','speaker_reference_selection.json')]
        manifest=_read(folder/'speakers.json',{})
        for item in manifest.values() if isinstance(manifest,dict) else ():
            reference=item.get('reference') if isinstance(item,dict) else None
            if isinstance(reference,str):
                path=(root/reference).resolve()
                if path.is_relative_to(root):paths.append(path)
        for name in ('speaker_machine_review.json','speaker_reference_selection.json'):
            report=_read(folder/name,{})
            bindings=report.get('bindings',{}) if isinstance(report,dict) else {}
            for relative in bindings if isinstance(bindings,dict) else ():
                if isinstance(relative,str):
                    path=(root/relative).resolve()
                    if path.is_relative_to(root):paths.append(path)
    paths += [folder/name for name in ('tts/no_join_recovery.json','tts/picture_holds.json',
        'recovery/website_journal.json')]
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
        # Actual selected cache paths also cover prior German/legacy caches
        # and content failures, which have no successful cache_key receipt.
        cache=batch.get('cache_revalidation',{}) if isinstance(batch,dict) else {}
        relative=batch.get('cache_file') if isinstance(batch,dict) else None
        if relative is None and isinstance(cache,dict):relative=cache.get('path')
        if isinstance(relative,str):
            path=(root/relative).resolve()
            if (path.parent in {root/'cache/proxy_translation_v1',root/'cache/proxy_translation_v2'}
                    and re.fullmatch(r'[a-f0-9]{64}\.json',path.name)):
                paths.append(path)
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
    mixed=isinstance(report,dict) and report.get('review_method')=='machine_two_pass_plus_user_context_adoption'
    correct_rows=(isinstance(translated,list) and len(translated)==len(rows) and all(
        isinstance(before,dict) and isinstance(row,dict) and all(row.get(key)==before.get(key) for key in
            ('segment_id','speaker_id','start_s','end_s','text_de','text_source','source_language'))
        and isinstance(row.get('text_zh'),str) and bool(row['text_zh'].strip())
        and row.get('translation_reviewed') is True and row.get('human_reviewed') is False
        and (row.get('translation_review_method')=='proxy_second_pass_and_numeric_checks'
             or (mixed and row.get('translation_review_method')=='user_contextual_translation_adoption'))
        for before,row in zip(rows,translated)))
    if mixed and correct_rows:
        try:
            from pipeline import validate_translations
            validate_translations(rows,translated,report,folder=folder)
        except (OSError,ValueError,KeyError,TypeError,RuntimeError):
            return False
    return bool(checkpoint and checkpoint.get('completed')==len(rows) and rows and correct_rows and
        isinstance(report,dict) and report.get('human_reviewed') is False
        and report.get('review_method') in ('two_pass_machine_review_and_numeric_invariants',
                                          'machine_two_pass_plus_user_context_adoption')
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


def _reference_failure(root,folder,source_sha256):
    """Name only current, file-bound reference failures; never grants recovery."""
    root=Path(root).resolve();folder=Path(folder).resolve()
    for name,schema in (('speaker_machine_review.json','speaker_reference_review_v2'),
                        ('speaker_reference_selection.json','speaker_reference_selection_v1')):
        report=_read(folder/name,{})
        if not isinstance(report,dict) or report.get('schema')!=schema or report.get('complete') is not True:
            continue
        bindings=report.get('bindings')
        required={str((folder/file).relative_to(root)) for file in ('source.json','segments.asr.json')}
        required.update(str((folder/file).relative_to(root)) for file in
            ('speakers.json','speaker_assignments.json','segments.plan.json') if (folder/file).is_file())
        if not isinstance(bindings,dict) or not required<=set(bindings):continue
        valid=True
        for relative,expected in bindings.items():
            if not isinstance(relative,str) or not isinstance(expected,str) or not _SHA.fullmatch(expected):
                valid=False;break
            path=(root/relative).resolve()
            if not path.is_relative_to(root) or _sha(path)!=expected:
                valid=False;break
        source=_read(folder/'source.json',{})
        raw=source.get('input') if isinstance(source,dict) else None
        if not isinstance(raw,str):continue
        video=Path(raw).resolve()
        if (not valid or not video.is_relative_to(root)
                or bindings.get(str(video.relative_to(root)))!=source_sha256):continue
        speakers=report.get('speakers')
        if not isinstance(speakers,list):continue
        failed=[row for row in speakers if isinstance(row,dict) and
            (row.get('selected') is False or row.get('finite_duration_energy_passed') is False)]
        manifest=report.get('manifest_issues',[])
        if not failed and not manifest:continue
        raw_document=_read(folder/'segments.asr.json',{})
        if not isinstance(raw_document,dict):continue
        raw_rows=raw_document.get('segments',[])
        if not isinstance(raw_rows,list):continue
        windows=[];ids=[]
        for row in failed:
            sid=row.get('speaker_id')
            if not isinstance(sid,str) or not re.fullmatch(r'S\d{2,}',sid):continue
            ids.append(sid)
            source_ids=row.get('source_ids',[])
            if not isinstance(source_ids,list):continue
            for raw_row in raw_rows:
                if not isinstance(raw_row,dict) or raw_row.get('segment_id') not in source_ids:continue
                start,end=raw_row.get('start_s'),raw_row.get('end_s')
                if (type(start) in (int,float) and type(end) in (int,float)
                        and 0<=start<end<=172800):
                    windows.append({'id':sid,'speaker_id':sid,'start_s':start,'end_s':end,'checks':['参考声音']})
        return {'speaker_ids':ids,'source_windows':windows,
                'manifest_invalid':bool(manifest),'report':name}
    return None


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


def _audio_manifest_is_current(folder,plan_ref,manifest_ref,source_sha256):
    """Follow completed audio selections, without choosing by file age or ASR."""
    import media_recovery as media
    import website_recovery as recovery
    def checked(ref):
        if (not isinstance(ref,dict) or set(ref)!={'path','sha256'}
                or not isinstance(ref['path'],str) or not _SHA.fullmatch(str(ref['sha256']))):
            raise ValueError('Invalid audio chain reference')
        path=recovery._safe(folder,ref['path'])
        if recovery._sha(path)!=ref['sha256']:raise ValueError('Changed audio chain reference')
        return path
    document=_audio_evidence_document
    journal_path=recovery._safe(folder,'tts/media_recovery_journal.json')
    if journal_path.stat().st_size>16*1024**2:return False
    journal=media.journal(folder)
    edges={};destinations=set();generation_origins=[]
    for entry in journal['entries']:
        # Picture holds and encoded-master repairs do not select new voices.
        if entry.get('action')!='audio_regenerate' or entry.get('status')!='complete':continue
        binding=entry.get('binding',{})
        if binding.get('kind')=='native_generation_failure_torch25' and binding.get('plan')!=plan_ref:return False
        if binding.get('plan')!=plan_ref:continue
        if entry.get('token')!=media.key({'action':'audio_regenerate','binding':binding}):return False
        if binding.get('kind')=='native_generation_failure_torch25':
            # This producer creates a complete root from two real backends,
            # rather than replacing audio in a pre-existing complete manifest.
            import native_generation_recovery_20261105 as generation
            root=Path(folder).resolve().parent.parent
            _,_,_,_,policy,_=generation.context(root,folder,checked(plan_ref))
            if policy['source_sha256']!=source_sha256:return False
            request=document(generation.request_path(folder));result=entry.get('result',{})
            composition_path=media.job_file(folder,result.get('manifest'))
            composition_ref={'path':str(composition_path.relative_to(folder)),
                'sha256':recovery._sha(composition_path)}
            expected={'changed':True,'manifest':str(composition_path),'outputs':[composition_ref],
                'fallback_group_ids':[generation.FAILED],'text_changed':False,'human_listened':False,
                'full_content_checks_still_required':True}
            if (request.get('token')!=entry['token'] or request.get('binding')!=binding
                    or composition_path!=media.job_file(folder,generation.DIRECTORY+'/composition_manifest.json')
                    or result!=expected):return False
            generation.validate_manifest(root,folder,checked(plan_ref),composition_path)
            generation_origins.append(document(checked(composition_ref)))
            continue
        if binding.get('source_video_sha256')!=source_sha256:return False
        base=checked(binding.get('base_manifest'));result=entry.get('result',{})
        selection_path=media.job_file(folder,result.get('selection'))
        composition_path=media.job_file(folder,result.get('manifest'))
        selection_ref={'path':str(selection_path.relative_to(folder)),
            'sha256':recovery._sha(selection_path)}
        composition_ref={'path':str(composition_path.relative_to(folder)),
            'sha256':recovery._sha(composition_path)}
        if (result.get('schema')!='local_acoustic_fallback_v1'
                or result.get('outputs')!=[selection_ref,composition_ref]):return False
        selection=document(checked(selection_ref));composition=document(checked(composition_ref))
        request=document(checked(selection.get('request')))
        if (selection.get('schema')!='local_acoustic_fallback_v1'
                or selection.get('token')!=entry['token'] or selection.get('binding')!=binding
                or selection.get('composition')!=composition_ref
                or request.get('schema')!='local_acoustic_fallback_v1'
                or request.get('token')!=entry['token'] or request.get('binding')!=binding
                or result.get('group_ids')!=selection.get('selected_group_ids')
                or result.get('group_ids')!=request.get('group_ids')
                or composition.get('schema')!='local_acoustic_fallback_composition_v1'
                or composition.get('source_manifests',{}).get('base')!=binding['base_manifest']):return False
        base_key=(str(base.relative_to(folder)),binding['base_manifest']['sha256'])
        next_key=(composition_ref['path'],composition_ref['sha256'])
        if base_key in edges or next_key in destinations or base_key==next_key:return False
        edges[base_key]=next_key;destinations.add(next_key)
    # The current plan has one completed audio chain and one terminal manifest.
    # An intact older composition is still stale once a successor is complete.
    roots=set(edges)-destinations
    if len(roots)!=1:return False
    current=roots.pop();visited=set()
    root_document=document(checked({'path':current[0],'sha256':current[1]}))
    if generation_origins or root_document.get('schema')=='native_generation_failure_composition_v1':
        # The normal pipeline may snapshot a root under a content-keyed path.
        # Only its exact complete document can anchor the acoustic successors.
        if len(generation_origins)!=1 or root_document!=generation_origins[0]:return False
    while current in edges:
        if current in visited:return False
        visited.add(current);current=edges[current]
    return len(visited)==len(edges) and current==(manifest_ref['path'],manifest_ref['sha256'])


def _current_audio_review(root,folder,metadata,source_sha256=None):
    """Only the explicitly referenced complete current composition is relevant."""
    try:
        import website_recovery as recovery
        root=Path(root).resolve();folder=Path(folder).resolve()
        if (not isinstance(metadata,dict) or metadata.get('schema')!=AUDIO_REVIEW_FAILURE_SCHEMA
                or metadata.get('origin')!=AUDIO_REVIEW_FAILURE_ORIGIN
                or metadata.get('category')!='unconfirmed_content_after_fallback'
                or metadata.get('job_id')!=folder.name
                or folder!=recovery._safe(root,Path('jobs')/folder.name)):
            return None
        bindings=metadata.get('bindings')
        if not isinstance(bindings,dict) or set(bindings)!={'quality','plan','manifest','segments'}:return None
        paths={}
        for name,ref in bindings.items():
            if (not isinstance(ref,dict) or set(ref)!={'path','sha256'}
                    or not isinstance(ref['path'],str) or not _SHA.fullmatch(str(ref['sha256']))):return None
            path=recovery._safe(folder,ref['path'])
            if not path.is_file() or recovery._sha(path)!=ref['sha256']:return None
            paths[name]=path
        if (not re.fullmatch(r'tts/audio_quality_[a-f0-9]{16}\.json',bindings['quality']['path'])
                or bindings['plan']['path']!='tts/semantic_group_plan.json'
                or bindings['segments']['path']!='segments.json'
                or not paths['manifest'].is_relative_to(folder/'tts')):return None
        # A complete mixed manifest repeats formal model provenance for each
        # utterance and can legitimately exceed the generic 4 MiB UI limit.
        document=_audio_evidence_document
        quality,plan,manifest,segments=(document(paths[name]) for name in ('quality','plan','manifest','segments'))
        if not all(isinstance(value,dict) for value in (quality,plan,manifest,segments)):return None
        current=bindings['segments']['sha256'];qbind=quality.get('binding',{})
        if (quality.get('schema')!='website_audio_quality_v1' or quality.get('complete') is not True
                or quality.get('policy')!='layered_full_content_v1'
                or quality.get('all_groups_target_free_checked') is not True
                or quality.get('helper_code_sha256')!=recovery._sha(root/'web/speech_content_quality.py')
                or qbind.get('plan')!=bindings['plan'] or qbind.get('native_manifest')!=bindings['manifest']
                or qbind.get('source_segments_sha256')!=current or plan.get('job')!=folder.name
                or plan.get('source_segments',{}).get('sha256')!=current
                or Path(plan.get('source_segments',{}).get('path','')).resolve()!=paths['segments']
                or manifest.get('phase')!='complete' or manifest.get('job')!=folder.name
                or manifest.get('schema')!='local_acoustic_fallback_composition_v1'
                or manifest.get('pending_unique_keys')!=0):return None
        source=_read(folder/'source.json',{});video=Path(source.get('input',''))
        if not video.is_absolute() or not video.is_relative_to(root):return None
        video=recovery._safe(root,video.relative_to(root));video_sha=recovery._sha(video)
        if (metadata.get('source_sha256')!=video_sha
                or source_sha256 is not None and source_sha256!=video_sha):return None
        if not _audio_manifest_is_current(folder,bindings['plan'],bindings['manifest'],video_sha):return None
        groups=plan.get('tts_groups');native=manifest.get('utterances');checked=quality.get('rows')
        if not all(isinstance(value,list) and value for value in (groups,native,checked)):return None
        group_ids=[group.get('group_id') for group in groups]
        if (len(set(group_ids))!=len(group_ids) or [row.get('group_id') for row in checked]!=group_ids
                or [row.get('segment_id') for row in native]!=group_ids
                or quality.get('checked_utterances')!=len(groups)
                or quality.get('target_free_checked_utterances')!=len(groups)
                or manifest.get('selected_utterances')!=len(groups)):return None
        published={row['segment_id'] for row in segments.get('segments',[])}
        ids=[];pending_groups=[];used_fallback=False
        for group,audio,row in zip(groups,native,checked):
            members=group.get('member_ids');decision=row.get('content_evaluation',{})
            if (not isinstance(members,list) or not members or len(set(members))!=len(members)
                    or not set(members)<=published or not all(_SID.fullmatch(str(sid)) for sid in members)
                    or row.get('member_ids')!=members or row.get('audio_sha256')!=audio.get('sha256')
                    or not _SHA.fullmatch(str(audio.get('sha256','')))
                    or row.get('target_free_asr_used') is not True or row.get('human_listened') is not False
                    or row.get('generation_provenance')!={'plan':bindings['plan'],'native_manifest':bindings['manifest']}
                    or row.get('needs_repair') is not False or decision.get('needs_repair') is not False
                    or type(row.get('needs_review')) is not bool or row['needs_review']!=decision.get('needs_review')):return None
            if row['needs_review']:
                if decision.get('status')!='needs_review':return None
                ids.extend(members);pending_groups.append(group['group_id'])
                used_fallback|=audio.get('content',{}).get('schema')=='continuous_utterance_v1'
            elif decision.get('status')!='machine_content_screen_passed':return None
        if (not ids or len(ids)!=len(set(ids)) or not used_fallback
                or quality.get('needs_repair_ids')!=[] or quality.get('needs_repair_groups')!=[]
                or quality.get('needs_review_ids')!=ids
                or quality.get('needs_review_groups')!=pending_groups
                or metadata.get('segment_ids')!=ids
                or metadata.get('group_ids')!=pending_groups):return None
        return {'ids':ids,'group_ids':pending_groups,'bindings':bindings}
    except (OSError,ValueError,TypeError,KeyError,AttributeError):
        return None


def audio_content_review_metadata(root,folder,quality_path,plan_path,manifest_path):
    """Construct a typed current-review failure; grants no retry or generation."""
    import website_recovery as recovery
    root=Path(root).resolve();folder=Path(folder).resolve()
    def ref(path):
        path=Path(path).resolve()
        safe=recovery._safe(folder,path.relative_to(folder))
        return {'path':str(safe.relative_to(folder)),'sha256':recovery._sha(safe)}
    bindings={'quality':ref(quality_path),'plan':ref(plan_path),'manifest':ref(manifest_path),
        'segments':ref(folder/'segments.json')}
    quality=_read(Path(quality_path),{})
    source=_read(folder/'source.json',{})
    video=Path(source.get('input',''))
    if not video.is_absolute() or not video.is_relative_to(root):raise ValueError('当前配音检查的原片位置不合法')
    metadata={'schema':AUDIO_REVIEW_FAILURE_SCHEMA,'origin':AUDIO_REVIEW_FAILURE_ORIGIN,
        'category':'unconfirmed_content_after_fallback','job_id':folder.name,
        'source_sha256':recovery._sha(recovery._safe(root,video.relative_to(root))),
        'bindings':bindings,'segment_ids':quality.get('needs_review_ids',[]),
        'group_ids':quality.get('needs_review_groups',[])}
    if _current_audio_review(root,folder,metadata) is None:
        raise ValueError('当前完整配音复核证据或备用声音绑定不完整')
    return metadata


def make_failure_record(root,job,error,stage=None,checkpoint_before=None):
    folder,rows,by_id=_input(root,job);checkpoint=_bound_checkpoint(folder,rows)
    stage=stage or job.get('stage','unknown');kind='unknown';ids=[];checks=[];policy='allowed'
    title='处理需要检查';message='本次处理未完成，已有成果已保留。';action='可从断点继续；若再次出现，将保留本次诊断记录。'
    metadata=getattr(error,'failure_metadata',{})
    audio_metadata=(isinstance(metadata,dict) and (metadata.get('schema')==AUDIO_REVIEW_FAILURE_SCHEMA
        or metadata.get('origin')==AUDIO_REVIEW_FAILURE_ORIGIN)) or type(error).__name__=='AudioContentReviewRequired'
    audio_review=_current_audio_review(root,folder,metadata,job.get('source_sha256')) if audio_metadata else None
    if audio_review and not set(audio_review['ids'])<=set(by_id):audio_review=None
    transport=(isinstance(metadata,dict) and metadata.get('schema')=='bounded_stage_failure_v1'
        and metadata.get('origin')=='proxy_transport' and metadata.get('stage')=='translation')
    text=str(error)
    revision=_read(folder/'source_plan_revision.json',{})
    revision=revision if isinstance(revision,dict) else {}
    blocked=revision.get('blocking_unresolved',[])
    local_translation=None;row_checks={};reference=None
    if type(error).__name__=='Cancelled' or '任务已取消' in text:
        kind='cancelled';title='任务已取消';message='任务已停止，已完成步骤保留。';action='需要时可以继续此任务。'
    elif transport and metadata.get('category') in {'transport_timeout','connection_reset','http_response'}:
        kind='transport';title='翻译服务连接暂时异常';message='出现了有实际请求证据的连接故障。';action='连接恢复后可以继续，自动重试仍受原有次数预算限制。'
    elif (isinstance(error,OSError) and getattr(error,'errno',None)==28) or '空间不足' in text:
        kind='storage';title='磁盘余量不足';message='磁盘空间未满足本任务的安全余量。';action='腾出空间后可以继续，原视频与旧成果不会被自动删除。'
    elif (stage in {'区分说话人与选择参考声音','检查参考声音'}
            and (reference:=_reference_failure(root,folder,job.get('source_sha256')))):
        kind='reference';policy='after_change';checks=['参考声音'];title='人物参考声音待处理'
        message=('人物参考清单不完整，未继续本轮配音。' if reference['manifest_invalid'] else
                 '人物 '+ '、'.join(reference['speaker_ids'])+' 的参考声音未通过检查。')
        action='已尝试安全的同人物参考；请检查原片位置或参考数据后从断点继续。'
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
    elif audio_metadata:
        # Invalid typed evidence stays an unknown failure. In particular it
        # cannot fall through to a scan of unrelated historical quality files.
        if audio_review:
            kind='audio';policy='after_change';ids=audio_review['ids'];checks=['完整声音内容未确认']
            title='完整声音内容尚未确认'
            message='当前完整复核仍有声音内容未确认，已保留两版完整声音、译稿及识别证据。'
            action='请集中试听下方问题句并决定采用哪版声音；完成决定后再继续，直接重试不会跳过检查。'
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
    if reference:windows=reference['source_windows']
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
    if reference:record['speaker_ids']=reference['speaker_ids']
    if audio_review:
        record['diagnostic_source']='current_complete_audio_quality'
        record['audio_review_bindings']=audio_review['bindings']
    return record


def persist_failure(root,jid,record):
    folder=Path(root)/'jobs'/jid/'recovery_diagnostics';folder.mkdir(parents=True,exist_ok=True)
    path=folder/(str(time.time_ns())+'.json');temp=path.with_suffix('.pending')
    temp.write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n');temp.chmod(0o600);temp.replace(path)
    return path


def public_failure(root,job):
    record=job.get('failure_record')
    if not isinstance(record,dict) or record.get('schema')!='local_failure_diagnosis_v1':return None
    keys=('category','title','message','next_action','segment_ids','speaker_ids','source_windows','checks','completed','total','retry_policy','progress_floor')
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
