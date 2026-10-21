"""Job-local website human corrections and private original-audio previews.

Original ASR/plan, verified segments, times and speaker identity are never
rewritten. Preferred Chinese is evidence-bound advice for a fresh Proxy review.
"""
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import uuid

import website_recovery as recovery

MAX_ROWS = 8
MAX_TEXT = 600
_ALLOWED_STATUS = {'failed', 'canceled'}
_TARGETS = {'translation/source_review_hints.json', 'source_plan_revision.json',
            'revisions/source_review_v1/segments.plan.json', 'tts/no_join_recovery.json', 'tts/picture_holds.json'}
_PROTECTED = {'segments.plan.json', 'segments.json', 'source.json', 'translation/input.json',
              'tts/media_recovery_journal.json', 'recovery/website_journal.json'}
_JOURNAL = 'recovery/manual_review_journal.json'


class ReviewInputError(ValueError):
    pass


class ReviewConflictError(RuntimeError):
    pass


def require(value, message):
    if not value:
        raise ReviewInputError(message)


def _text_sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _state(root, jid, require_source=True):
    path = recovery._safe(root, Path('web/state/jobs') / (jid + '.json'))
    require(path.is_file(), '任务状态不存在')
    state = recovery._read(path)
    require(isinstance(state, dict) and state.get('id') == jid, '任务状态身份不符')
    require(state.get('status') in _ALLOWED_STATUS, '仅失败或已取消的任务可以核对，处理中的任务及已完成成果不能修改')
    if require_source:
        require(recovery._SHA.fullmatch(str(state.get('source_sha256', ''))), '原视频身份缺失，不能安全核对')
    return state, path


def _source(root, folder, state):
    path = recovery._safe(folder, 'source.json')
    require(path.is_file(), '任务原视频记录不存在')
    source = recovery._read(path)
    value = Path(source.get('input', ''))
    require(value.is_absolute(), '原视频位置不合法')
    ancestor = next((p for p in value.parents if p.resolve() == root), None)
    require(ancestor is not None, '原视频不在此项目的已登记素材中')
    video = recovery._safe(root, value.relative_to(ancestor))
    require(video.is_file() and recovery._sha(video) == state['source_sha256'], '原视频与任务身份不符')
    require(type(source.get('duration_s')) in (int, float) and math.isfinite(source['duration_s'])
        and source['duration_s'] > 0 and type(source.get('audio_track')) is int and source['audio_track'] >= 0,
        '原视频时长或音轨不合法')
    return source, video


def _load(root, folder, state):
    original_path = recovery._safe(folder, 'segments.plan.json')
    require(original_path.is_file(), '原文计划尚未建立')
    original_doc = recovery._read(original_path)
    original = original_doc.get('segments')
    require(isinstance(original, list) and original, '原文计划为空')
    revision_path = recovery._safe(folder, 'source_plan_revision.json')
    revision = recovery._read(revision_path) if revision_path.is_file() else None
    if revision is not None:
        require(isinstance(revision, dict), '原文修订格式不合法')
        recovery._safe(folder, 'revisions/source_review_v1/segments.plan.json')
        for change in revision.get('changes', []):
            require(isinstance(change, dict), '原文修订证据不合法')
            for proof in change.get('evidence', []):
                require(isinstance(proof, dict) and isinstance(proof.get('path'), str), '原文修订证据不合法')
                recovery._safe(folder, proof['path'])
        from pipeline import reviewed_source_plan
        try:
            rows, _ = reviewed_source_plan(folder, original_path, state['source_sha256'])
        except (ValueError, RuntimeError, OSError, KeyError) as error:
            raise ReviewInputError('现有原文修订与原计划绑定不符，未覆盖资料') from error
        derived_doc = recovery._read(recovery._safe(folder, 'revisions/source_review_v1/segments.plan.json'))
    else:
        rows, derived_doc = original, copy.deepcopy(original_doc)
    ids = [r.get('segment_id') for r in rows]
    require(len(ids) == len(set(ids)) and all(isinstance(sid, str) and recovery._SID.fullmatch(sid) for sid in ids), '当前原文编号不合法')
    for row in rows:
        require(isinstance(row.get('text_de'), str) and row['text_de'].strip()
            and row.get('text_source', row['text_de']) == row['text_de'], '当前原文不完整')
        require(all(type(row.get(k)) in (int, float) and math.isfinite(row[k]) for k in ('start_s', 'end_s'))
            and 0 <= row['start_s'] < row['end_s'], '原文时间不合法')
    if revision:
        known = set(ids)
        for name in ('unresolved', 'blocking_unresolved'):
            values = revision.get(name, [])
            require(isinstance(values, list) and all(isinstance(sid, str) for sid in values)
                and len(values) == len(set(values)) and set(values) <= known, '原文疑词编号不合法')
        require(set(revision.get('blocking_unresolved', [])) <= set(revision.get('unresolved', [])), '原文疑词阻断范围不合法')
    return original_doc, copy.deepcopy(derived_doc), copy.deepcopy(rows), copy.deepcopy(revision)


def _journal(folder):
    path = recovery._safe(folder, _JOURNAL)
    value = recovery._read(path) if path.is_file() else {'schema': 'website_manual_review_journal_v1', 'job': folder.name, 'events': []}
    require(isinstance(value, dict) and value.get('schema') == 'website_manual_review_journal_v1'
        and value.get('job') == folder.name and isinstance(value.get('events'), list), '人工核对事务记录不完整')
    for event in value['events']:
        require(isinstance(event, dict) and isinstance(event.get('id'), str) and re.fullmatch(r'[a-f0-9]{32}', event['id'])
            and event.get('status') in {'applying', 'saved', 'rolled_back', 'conflict'}, '人工核对事务记录损坏')
    return value


def _publish(folder, journal):
    recovery._save(recovery._safe(folder, _JOURNAL), journal)


def _hash(folder, relative):
    path = recovery._safe(folder, relative)
    return recovery._sha(path) if path.is_file() else None


def _protected(folder, targets):
    names = _PROTECTED | _TARGETS
    return {name: _hash(folder, name) for name in sorted(names - set(targets))}


def _same_state(root, folder, event):
    _state(root, folder.name)
    path = recovery._safe(root, Path('web/state/jobs') / (folder.name + '.json'))
    if recovery._sha(path) != event['state_sha256']:
        raise ReviewConflictError('核对期间任务状态已改变，未覆盖新资料')


def _transaction_items(folder, event):
    items = event.get('transaction')
    require(isinstance(items, list) and 1 <= len(items) <= 5
        and len({x.get('target') for x in items}) == len(items), '人工核对事务目标不合法')
    for item in items:
        require(item.get('target') in _TARGETS and item.get('operation') in {'write', 'delete'}
            and recovery._SHA.fullmatch(str(item.get('prepared_sha256', ''))), '人工核对事务位置不合法')
        if item['operation'] == 'write':
            require(item.get('after_sha256') == item['prepared_sha256'], '人工核对写入身份不合法')
        else:
            require(item['target'] in {'tts/no_join_recovery.json', 'tts/picture_holds.json'}
                and item.get('after_sha256') is None and recovery._SHA.fullmatch(str(item.get('before_sha256', ''))),
                '仅旧媒体许可可以归档移除')
        stem = 'recovery/manual/' + event['id'] + '/'
        require(item.get('after') == stem + 'after_' + item['target'].replace('/', '__')
            and item.get('before') == stem + 'before_' + item['target'].replace('/', '__'), '人工核对事务证据位置不合法')
        after = recovery._safe(folder, item['after'])
        require(after.is_file() and recovery._sha(after) == item['prepared_sha256'], '人工核对事务内容哈希不符')
        if item.get('before_sha256') is not None:
            before = recovery._safe(folder, item['before'])
            require(before.is_file() and recovery._sha(before) == item['before_sha256'], '人工核对旧版本哈希不符')
    return items


def _rollback(folder, event):
    clean = True
    for item in reversed(_transaction_items(folder, event)):
        current = _hash(folder, item['target'])
        if current == item['before_sha256']:
            continue
        if current != item['after_sha256']:
            clean = False  # A concurrent writer owns it; never overwrite it.
            continue
        target = recovery._safe(folder, item['target'])
        if item['before_sha256'] is None:
            target.unlink()
        else:
            recovery._atomic(target, recovery._safe(folder, item['before']).read_bytes())
    return clean


def _apply(root, folder, journal, event):
    try:
        _same_state(root, folder, event)
        current_state, _ = _state(root, folder.name)
        _source(root, folder, current_state)
        for name, expected in event['protected_files'].items():
            require(name in _PROTECTED | _TARGETS,
                '人工核对源文件保护位置不合法')
            if _hash(folder, name) != expected:
                raise ReviewConflictError('核对期间源文或译稿已改变，未覆盖新资料')
        proof = event.get('evidence', {})
        expected = 'revisions/source_review_v1/evidence/website_manual_' + event['id'] + '.json'
        require(isinstance(proof, dict) and proof.get('path') == expected
            and _hash(folder, expected) == proof.get('sha256'), '人工核对提交证据已改变')
        items = _transaction_items(folder, event)
        for item in items:
            if _hash(folder, item['target']) not in (item['before_sha256'], item['after_sha256']):
                raise ReviewConflictError('核对期间修订资料已改变，未覆盖新资料')
        for item in items:
            _same_state(root, folder, event)
            actual = _hash(folder, item['target'])
            if actual not in (item['before_sha256'], item['after_sha256']):
                raise ReviewConflictError('核对期间修订资料已改变，未覆盖新资料')
            if actual != item['after_sha256']:
                if item['operation'] == 'delete':
                    recovery._safe(folder, item['target']).unlink()
                else:
                    recovery._atomic(recovery._safe(folder, item['target']), recovery._safe(folder, item['after']).read_bytes())
        _same_state(root, folder, event)
        for name, expected in event['protected_files'].items():
            if _hash(folder, name) != expected:
                raise ReviewConflictError('提交核对时源文资料已改变，已回退本次修改')
        event.update(status='saved', finished_at=datetime.now(timezone.utc).isoformat())
        _publish(folder, journal)
    except BaseException:
        clean = _rollback(folder, event)
        event.update(status='rolled_back' if clean else 'conflict', finished_at=datetime.now(timezone.utc).isoformat())
        _publish(folder, journal)
        raise


def _recover(root, folder):
    journal = _journal(folder)
    for event in journal['events']:
        if event['status'] == 'applying':
            _apply(root, folder, journal, event)
    return journal


def resume_pending(root, jid):
    """Finish an existing interrupted manual transaction before queueing a retry.

    No source/derived files or source hash are required when no transaction is
    pending. This entry point never creates a new correction or resets a budget.
    The server calls it while retaining its per-job condition lock.
    """
    require(isinstance(jid, str) and recovery._JID.fullmatch(jid) and jid not in ('.', '..'), '任务编号不合法')
    root = Path(root).resolve()
    folder = recovery._safe(root, Path('jobs') / jid)
    _state(root, jid, require_source=False)
    journal_path = recovery._safe(folder, _JOURNAL)
    if not journal_path.is_file():
        return {'resumed': False, 'transactions': 0, 'human_reviewed': False, 'media_recovery_budget_reset': False}
    require(folder.is_dir(), '人工核对事务所属任务不存在')
    with recovery._locked(folder):
        _state(root, jid, require_source=False)
        journal = _journal(folder)
        pending = [event for event in journal['events'] if event['status'] == 'applying']
        require(len(pending) <= 1, '存在多个未提交人工核对事务，需保留资料后核对')
        for event in pending:
            protected = event.get('protected_files')
            require(isinstance(protected, dict) and
                {'tts/media_recovery_journal.json', 'recovery/website_journal.json'} <= set(protected),
                '待恢复人工事务缺少持久预算保护，未继续')
            _apply(root, folder, journal, event)
        return {'resumed': bool(pending), 'transactions': len(pending),
            'human_reviewed': False, 'media_recovery_budget_reset': False}


def _requested_ids(value, rows):
    require(isinstance(value, list) and 1 <= len(value) <= MAX_ROWS and all(isinstance(s, str) for s in value)
        and len(value) == len(set(value)), '每次请选择1至8个不同的句子')
    known = {r['segment_id'] for r in rows}
    require(set(value) <= known, '核对编号不在本任务当前原文中')
    return value


def _failed_ids(folder, state, revision):
    candidates = []
    record = state.get('failure_record')
    if isinstance(record, dict):
        candidates.extend(record.get('segment_ids', []))
    if revision:
        candidates.extend(revision.get('blocking_unresolved', []))
        candidates.extend(revision.get('unresolved', []))
    for journal_name, field in [('recovery/website_journal.json', 'events'), ('tts/media_recovery_journal.json', 'entries')]:
        path = recovery._safe(folder, journal_name)
        if path.is_file():
            value = recovery._read(path)
            for entry in reversed(value.get(field, [])):
                if not isinstance(entry, dict):
                    continue
                result = entry.get('result', {})
                if isinstance(result, dict):
                    for name in ('unresolved_ids', 'overlong_ids', 'blank_frame_ids'):
                        candidates.extend(result.get(name, []))
                if entry.get('status') in {'failed', 'interrupted'}:
                    candidates.extend(entry.get('segment_ids', []))
    for path in sorted(recovery._safe(folder, 'tts').glob('audio_quality_*.json'), key=lambda p: p.stat().st_mtime_ns, reverse=True)[:1]:
        path = recovery._safe(folder, path.relative_to(folder))
        value = recovery._read(path)
        candidates.extend(value.get('needs_repair_ids', []))
    return list(dict.fromkeys(sid for sid in candidates if isinstance(sid, str)))


def review_rows(root, jid, ids=None):
    root, folder = recovery._folder(root, jid)
    with recovery._locked(folder):
        state, _ = _state(root, jid)
        _source(root, folder, state)
        _recover(root, folder)
        _, _, rows, revision = _load(root, folder, state)
        by_id = {r['segment_id']: r for r in rows}
        chosen = _requested_ids(ids, rows) if ids is not None else [sid for sid in _failed_ids(folder, state, revision) if sid in by_id][:MAX_ROWS]
        uncertainty = set((revision or {}).get('unresolved', []))
        chinese = {}
        segments = recovery._safe(folder, 'segments.json')
        if segments.is_file():
            value = recovery._read(segments)
            chinese = {r.get('segment_id'): r.get('text_zh', '') for r in value.get('segments', []) if isinstance(r, dict)}
        return [{'id': sid, 'start_s': by_id[sid]['start_s'], 'end_s': by_id[sid]['end_s'],
            'text_source': by_id[sid]['text_de'], 'text_zh': chinese.get(sid, by_id[sid].get('text_zh', '')),
            'uncertain': sid in uncertainty} for sid in chosen]


def _corrections(value):
    require(isinstance(value, list) and 1 <= len(value) <= MAX_ROWS, '每次可以保存1至8句核对')
    seen, result = set(), []
    for item in value:
        require(isinstance(item, dict) and not set(item) - {'id', 'source_text', 'preferred_translation'}
            and isinstance(item.get('id'), str) and item['id'] not in seen
            and bool(set(item) & {'source_text', 'preferred_translation'}), '核对内容有未知字段、重复编号或缺少文字')
        clean = {'id': item['id']}
        for field in ('source_text', 'preferred_translation'):
            if field not in item:
                continue
            text = item[field]
            require(isinstance(text, str) and 1 <= len(text.strip()) <= MAX_TEXT
                and not any(ord(c) < 32 and c not in '\n\t' for c in text), '核对文字应为1至600字')
            clean[field] = text.strip()
        seen.add(item['id'])
        result.append(clean)
    return result


def save_corrections(root, jid, corrections):
    corrections = _corrections(corrections)
    root, folder = recovery._folder(root, jid)
    with recovery._locked(folder):
        state, state_path = _state(root, jid)
        state_sha = recovery._sha(state_path)
        _source(root, folder, state)
        journal = _recover(root, folder)
        initial_files = _protected(folder, [])
        original_doc, derived_doc, rows, revision = _load(root, folder, state)
        ids = _requested_ids([item['id'] for item in corrections], rows)
        original = {r['segment_id']: r for r in original_doc['segments']}
        by_id = {r['segment_id']: r for r in rows}
        old_rows = copy.deepcopy(rows)
        hints = recovery._hints(folder, old_rows)
        event_id = uuid.uuid4().hex
        evidence_name = 'revisions/source_review_v1/evidence/website_manual_' + event_id + '.json'
        source_ids, preferred_ids = [], []
        for item in corrections:
            sid = item['id']
            if 'source_text' in item:
                by_id[sid]['text_de'] = item['source_text']
                by_id[sid]['text_source'] = item['source_text']
                source_ids.append(sid)
            if 'preferred_translation' in item:
                preferred_ids.append(sid)
        from proxy_client import invariants
        from website_recovery import _guard_context
        indexes = {r['segment_id']: n for n, r in enumerate(rows)}
        chinese_path = recovery._safe(folder, 'segments.json')
        chinese_doc = recovery._read(chinese_path) if chinese_path.is_file() else {'segments': []}
        old_chinese = {r['segment_id']: r.get('text_zh') for r in chinese_doc.get('segments', [])}
        checks = {}
        actual_source_changes = []
        for item in corrections:
            sid, row = item['id'], by_id[item['id']]
            old_hint = hints['rows'].get(sid, {})
            source_changed = row['text_de'] != old_rows[indexes[sid]]['text_de']
            if source_changed:
                actual_source_changes.append(sid)
                hints['rows'].pop(sid, None)  # Old-source advice cannot be rebound blindly.
            if 'preferred_translation' in item:
                target = item['preferred_translation']
                errors = invariants(row['text_de'], target, previous_context=_guard_context(rows, indexes[sid]),
                    source_language=row.get('source_language', state.get('source_language', 'de')))
                requirements = old_hint.get('required_strings', []) if not source_changed else []
                exact = old_chinese.get(sid)
                prior_wording = exact is not None and ''.join(requirements) == exact
                for required in requirements:
                    if not prior_wording and required != exact and required not in target:
                        errors.append('已确认文字约束未保留')
                checks[sid] = errors
                require(not errors, '首选译文未通过数字、单位、否定或条件检查：' + sid)
                hints['rows'][sid] = {'source_text_sha256': _text_sha(row['text_de']),
                    'note': '用户在网站明确提供首选译文；仍须Proxy重新翻译、独立二审和本地事实约束检查，不代表全片人工审校。首选完整中文：' + target,
                    'required_strings': [target[n:n + 80] for n in range(0, len(target), 80)]}
            elif sid in source_ids:
                hints['rows'][sid] = {'source_text_sha256': _text_sha(row['text_de']),
                    'note': '用户在网站明确核对本句原词；请仅据当前原文重新翻译和独立二审，不代表全片人工审校。',
                    'required_strings': old_hint.get('required_strings', []) if not source_changed else []}
        archive_names = [name for name in ('tts/no_join_recovery.json', 'tts/picture_holds.json')
            if actual_source_changes and initial_files.get(name) is not None]
        archive_records = [{'path': 'recovery/manual/' + event_id + '/before_' + name.replace('/', '__'),
            'sha256': initial_files[name], 'previous_active_path': name} for name in archive_names]
        evidence = {'schema': 'website_human_correction_evidence_v1', 'job': jid, 'id': event_id,
            'created_at': datetime.now(timezone.utc).isoformat(), 'input_source_sha256': state['source_sha256'],
            'corrections': corrections, 'source_word_confirmed_by_user_ids': source_ids,
            'preferred_translation_ids': preferred_ids, 'local_invariant_checks': checks,
            'archived_media_permissions': archive_records, 'media_recovery_budget_reset': False,
            'human_reviewed': False, 'scope': 'Only these submitted source words or translation preferences; no full-film approval.'}
        evidence_data = recovery._bytes(evidence)
        proof = {'path': evidence_name, 'sha256': hashlib.sha256(evidence_data).hexdigest()}
        outputs = {}
        if source_ids:
            changes = copy.deepcopy((revision or {}).get('changes', []))
            positions = {c['segment_id']: i for i, c in enumerate(changes)}
            for sid in source_ids:
                change = {'segment_id': sid, 'action': 'replace_source_text',
                    'original_text_sha256': _text_sha(original[sid]['text_de']),
                    'replacement_text_sha256': _text_sha(by_id[sid]['text_de']),
                    'evidence': [proof], 'reason': 'User explicitly corrected this source sentence in the local website.'}
                if sid in positions:
                    require(changes[positions[sid]]['action'] == 'replace_source_text', '不能恢复或改写已证实的重复排除行')
                    change['previous_evidence'] = copy.deepcopy(changes[positions[sid]].get('evidence', []))
                    changes[positions[sid]] = change
                else:
                    changes.append(change)
            derived_doc['segments'] = rows
            derived_doc['human_reviewed'] = False
            derived_data = recovery._bytes(derived_doc)
            revision = copy.deepcopy(revision or {'schema': 'source_plan_revision_v1',
                'original_plan_sha256': recovery._sha(recovery._safe(folder, 'segments.plan.json')),
                'input_source_sha256': state['source_sha256'], 'unresolved': [], 'blocking_unresolved': []})
            revision.update(changes=changes, revised_plan_sha256=hashlib.sha256(derived_data).hexdigest(), human_reviewed=False)
            for name in ('unresolved', 'blocking_unresolved'):
                revision[name] = [sid for sid in revision.get(name, []) if sid not in source_ids]
            revision.setdefault('website_manual_corrections', []).append({'evidence': proof, 'source_word_confirmed_by_user_ids': source_ids,
                'human_reviewed': False})
            outputs['revisions/source_review_v1/segments.plan.json'] = derived_doc
            outputs['source_plan_revision.json'] = revision
        hints['website_manual_review'] = {'schema': 'website_manual_hint_receipt_v1', 'evidence': proof,
            'input_source_sha256': state['source_sha256'], 'source_rows_sha256': recovery._hash(rows),
            'source_plan_sha256': (revision or {}).get('revised_plan_sha256') or recovery._sha(recovery._safe(folder, 'segments.plan.json')),
            'human_reviewed': False, 'requires_proxy_review': True}
        outputs['translation/source_review_hints.json'] = hints
        for name in archive_names:
            outputs[name] = None  # Archive permission, never bind an old scope to corrected source.
        # Prepare immutable before/after evidence before touching live pointers.
        if any(_hash(folder, name) != expected for name, expected in initial_files.items()) or recovery._sha(state_path) != state_sha:
            raise ReviewConflictError('核对期间源文或任务状态已改变，未覆盖新资料')
        protected = {name: expected for name, expected in initial_files.items() if name not in outputs}
        recovery._atomic(recovery._safe(folder, evidence_name), evidence_data)
        transaction = []
        # The revision pointer is committed last, after its derived source and hints.
        order = ['tts/no_join_recovery.json', 'tts/picture_holds.json',
                 'revisions/source_review_v1/segments.plan.json', 'translation/source_review_hints.json', 'source_plan_revision.json']
        for name in order:
            if name not in outputs:
                continue
            prefix = 'recovery/manual/' + event_id + '/'
            before_name, after_name = prefix + 'before_' + name.replace('/', '__'), prefix + 'after_' + name.replace('/', '__')
            path = recovery._safe(folder, name)
            before_sha = initial_files[name]
            if _hash(folder, name) != before_sha:
                raise ReviewConflictError('核对期间修订资料已改变，未覆盖新资料')
            if before_sha is not None:
                previous_data = path.read_bytes()
                if hashlib.sha256(previous_data).hexdigest() != before_sha:
                    raise ReviewConflictError('核对期间修订资料已改变，未覆盖新资料')
                recovery._atomic(recovery._safe(folder, before_name), previous_data)
            deleting = outputs[name] is None
            data = recovery._bytes({'schema': 'website_manual_permission_archive_v1',
                'operation': 'remove_obsolete_active_permission', 'target': name, 'archived_before_sha256': before_sha}
                if deleting else outputs[name])
            prepared_sha = hashlib.sha256(data).hexdigest()
            recovery._atomic(recovery._safe(folder, after_name), data)
            transaction.append({'target': name, 'operation': 'delete' if deleting else 'write',
                'before': before_name, 'before_sha256': before_sha, 'after': after_name,
                'prepared_sha256': prepared_sha, 'after_sha256': None if deleting else prepared_sha})
        event = {'id': event_id, 'status': 'applying', 'state_sha256': state_sha,
            'protected_files': protected, 'transaction': transaction, 'evidence': proof,
            'source_corrected_ids': source_ids, 'translation_hint_ids': preferred_ids}
        journal['events'].append(event)
        _publish(folder, journal)
        _apply(root, folder, journal, event)
        return {'saved': True, 'rows': len(ids), 'source_corrected_ids': source_ids,
            'translation_hint_ids': preferred_ids, 'archived_media_permissions': [Path(name).stem for name in archive_names],
            'media_recovery_budget_reset': False, 'human_reviewed': False, 'requires_proxy_review': True}


def audio_preview(root, jid, sid):
    root, folder = recovery._folder(root, jid)
    with recovery._locked(folder):
        state, state_path = _state(root, jid)
        _recover(root, folder)
        _, _, rows, _ = _load(root, folder, state)
        _requested_ids([sid], rows)
        row = next(r for r in rows if r['segment_id'] == sid)
        source, video = _source(root, folder, state)
        start = max(0., row['start_s'] - 1.)
        end = min(source['duration_s'], row['end_s'] + 1., start + 30.)
        require(0 <= start < end <= source['duration_s'] and end - start <= 30., '原声预览时间不合法')
        binding = {'schema': 'website_original_audio_preview_v1', 'job': jid, 'id': sid,
            'source_sha256': state['source_sha256'], 'start_s': start, 'end_s': end,
            'audio_track': source['audio_track'], 'recipe': 'local_cpu_mp3_96k_44100_mono_v1'}
        name = recovery._hash(binding)
        path = recovery._safe(folder, 'review_audio/' + name + '.mp3')
        metadata = recovery._safe(folder, 'review_audio/' + name + '.json')
        if path.is_file() and metadata.is_file():
            previous = recovery._read(metadata)
            if previous.get('binding') == binding and previous.get('audio_sha256') == recovery._sha(path) and path.stat().st_size > 0:
                return path
        before_state = recovery._sha(state_path)
        from pipeline import FFMPEG
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = recovery._safe(folder, 'review_audio/' + name + '.' + uuid.uuid4().hex + '.partial.mp3')
        try:
            result = subprocess.run([FFMPEG, '-v', 'error', '-nostdin', '-threads', '1', '-ss', '%.6f' % start,
                '-i', str(video), '-t', '%.6f' % (end - start), '-map', '0:a:' + str(source['audio_track']),
                '-vn', '-ac', '1', '-ar', '44100', '-b:a', '96k', '-y', str(temporary)],
                capture_output=True, timeout=30)
            require(result.returncode == 0 and temporary.is_file() and temporary.stat().st_size > 0, '原声预览生成失败，未发送音频到外部')
            _state(root, jid)
            require(recovery._sha(video) == state['source_sha256'], '生成预览期间原视频身份改变，未保存预览')
            if recovery._sha(state_path) != before_state:
                raise ReviewConflictError('生成预览期间任务状态已改变，请刷新页面')
            os.replace(temporary, path)
            recovery._save(metadata, {'binding': binding, 'audio_sha256': recovery._sha(path),
                'bytes': path.stat().st_size, 'audio_sent_to_proxy': False, 'human_listened': False})
            return path
        except subprocess.TimeoutExpired as error:
            raise ReviewInputError('原声预览超过30秒生成预算，未发送音频到外部') from error
        finally:
            if temporary.exists():
                temporary.unlink()
