"""Evidence-bound, CPU-only language correction for genuinely rejected source rows.

Two different installed recognizers must agree.  This produces a candidate for
the normal editor and independent audit, never a translation approval.
"""
import collections
import copy
import difflib
import hashlib
import math
from pathlib import Path
import re
import shutil
import subprocess
import wave

import website_recovery as recovery
from source_language import normalize_language

SCHEMA = 'automatic_dual_cpu_language_v1'
OPTIONS = {'language': None, 'task': 'transcribe', 'vad_filter': False,
           'word_timestamps': True, 'beam_size': 5, 'condition_on_previous_text': False}
MIN_PROBABILITY = .55
MIN_AGREEMENT = .65
MAX_WINDOW_SECONDS = 30.


def _require(value, message):
    if not value:
        raise ValueError('自动语言复核：' + message)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _row_identity(row):
    from proxy_client import source_binding
    _require(isinstance(row, dict) and isinstance(row.get('text_de'), str)
        and row.get('text_source', row['text_de']) == row['text_de'], '当前源行不完整')
    return {**source_binding(row), 'source_language': normalize_language(row.get('source_language', 'de'), False),
        'slot_end_s': row.get('slot_end_s', row.get('end_s')), 'overlap': row.get('overlap'),
        'source_ids': row.get('source_ids'),
        'protected_user_source_flags': {name: row.get(name) is True for name in
            ('source_word_confirmed_by_user', 'source_words_verified', 'user_contextual_adoption',
             'user_context_adopted', 'human_reviewed')}}


def _verified_plan(folder, source_sha256):
    """Read committed snapshots without importing the pipeline loader again."""
    from proxy_client import source_binding
    original_path = recovery._safe(folder, 'segments.plan.json')
    original_document = recovery._read(original_path)
    _require(isinstance(original_document, dict), '原始源计划不是对象')
    originals = original_document.get('segments')
    _require(isinstance(originals, list) and originals
        and all(isinstance(item, dict) and isinstance(item.get('segment_id'), str) for item in originals)
        and len({item['segment_id'] for item in originals}) == len(originals), '原始源计划缺行或重复')
    receipt = recovery._safe(folder, 'source_plan_revision.json')
    if not receipt.is_file():
        return originals, originals, None
    revision = recovery._read(receipt)
    derived_path = recovery._safe(folder, 'revisions/source_review_v1/segments.plan.json')
    _require(isinstance(revision, dict) and revision.get('schema') == 'source_plan_revision_v1'
        and revision.get('input_source_sha256') == source_sha256
        and revision.get('original_plan_sha256') == _sha(original_path)
        and revision.get('revised_plan_sha256') == _sha(derived_path), '当前派生计划没有绑定真实原片与原计划')
    derived_document = recovery._read(derived_path)
    _require(isinstance(derived_document, dict), '派生源计划不是对象')
    rows = derived_document.get('segments')
    changes = revision.get('changes')
    by_id = {item['segment_id']: item for item in originals}
    _require(isinstance(rows, list) and isinstance(changes, list) and changes, '派生计划缺少逐行变更')
    changed = {}
    for change in changes:
        _require(isinstance(change, dict) and change.get('segment_id') in by_id
            and change['segment_id'] not in changed and change.get('action') in {'replace_source_text', 'exclude_asr_duplicate'}
            and change.get('original_text_sha256') == hashlib.sha256(by_id[change['segment_id']]['text_de'].encode()).hexdigest()
            and isinstance(change.get('evidence'), list) and change['evidence'], '源计划变更范围或原文身份不符')
        for ref in change['evidence']:
            _reference(folder, ref, folder / 'revisions/source_review_v1/evidence')
        if change['action'] == 'exclude_asr_duplicate':
            _require(type(change.get('zero_duration_words')) is int and change['zero_duration_words'] >= 4
                and change.get('crop_asr_confirms_no_repetition') is True, '排除相邻原声没有真实重复证据')
        changed[change['segment_id']] = change
    _require(all(isinstance(item, dict) for item in rows)
        and [item.get('segment_id') for item in rows] == [item['segment_id'] for item in originals
            if changed.get(item['segment_id'], {}).get('action') != 'exclude_asr_duplicate'], '派生计划改变了源行覆盖或顺序')
    for item in rows:
        before, change = by_id[item['segment_id']], changed.get(item['segment_id'])
        if change is None:
            _require(item == before, '未登记的源行被改变')
            continue
        _require(change['action'] == 'replace_source_text' and isinstance(item.get('text_de'), str)
            and item['text_de'].strip() and item.get('text_source') == item['text_de']
            and change.get('replacement_text_sha256') == hashlib.sha256(item['text_de'].encode()).hexdigest(), '源计划替换正文不符')
        allowed = {'text_de', 'text_source'}
        if item.get('source_language', 'de') != before.get('source_language', 'de'):
            proof = recovery._read(_reference(folder, change.get('language_evidence'), folder / 'revisions/source_review_v1/evidence'))
            _require(change.get('original_source_language') == before.get('source_language', 'de')
                and change.get('replacement_source_language') == item.get('source_language')
                and isinstance(proof, dict) and proof.get('schema') == 'machine_row_language_evidence_v1'
                and proof.get('segment_id') == before['segment_id'] and proof.get('source_binding') == source_binding(before)
                and proof.get('input_source_sha256') == source_sha256, '语言变更没有绑定原始源行')
            allowed.add('source_language')
        _require({name: value for name, value in item.items() if name not in allowed}
            == {name: value for name, value in before.items() if name not in allowed}, '源计划改变了时间、人物或未授权字段')
    return rows, originals, revision


def _approved_precommit_transition(root, folder, source, video, old_rows, current_rows,
                                   revision, language_sid, source_sha256):
    """An old whole input may differ only by one actual approved transaction.

    Reconstruct all changed source rows from its stored complete candidates,
    exact old bindings and actual independent audit, rather than accepting an
    arbitrary mixture of old and new row languages or context.
    """
    from proxy_client import source_binding
    from translation_recovery import source_alternative_errors
    _require(isinstance(revision, dict) and [item['segment_id'] for item in old_rows]
        == [item['segment_id'] for item in current_rows], '提交前快照改变了源行覆盖或顺序')
    old = {item['segment_id']: item for item in old_rows}
    current = {item['segment_id']: item for item in current_rows}
    changed_ids = [sid for sid in old if _row_identity(old[sid]) != _row_identity(current[sid])]
    _require(language_sid in changed_ids and 1 <= len(changed_ids) <= recovery.MAX_ROWS,
             '提交前快照不是当前有限源文修订事务')
    changes = {item['segment_id']: item for item in revision['changes']}
    shared = None
    for sid in changed_ids:
        _require(sid in changes and changes[sid].get('action') == 'replace_source_text',
                 '旧输入有未登记的源文差异')
        fixed = lambda item: {key: value for key, value in _row_identity(item).items()
            if key not in {'text_de_sha256', 'text_source_sha256', 'source_language'}}
        _require(fixed(old[sid]) == fixed(current[sid]), '同事务修订改变了人物、时间或用户约束')
        refs = {(item['path'], item['sha256']) for item in changes[sid]['evidence']}
        shared = refs if shared is None else shared.intersection(refs)
    _require(bool(shared), '多行差异不属于同一份源文选用证据')
    for proof_path, proof_sha in sorted(shared):
        selection_path = _reference(folder, {'path': proof_path, 'sha256': proof_sha},
                                    folder / 'revisions/source_review_v1/evidence')
        selection = recovery._read(selection_path)
        if not isinstance(selection, dict) or selection.get('schema') != 'machine_source_recheck_selection_v1':
            continue
        _require(selection.get('input_source_sha256') == source_sha256
            and selection.get('human_reviewed') is False and selection.get('source_words_verified') is False,
            '共同源文选用记录不是当前原片机器复核')
        selected, candidates, bindings = (selection.get(name) for name in
            ('selected', 'source_candidates', 'source_bindings'))
        _require(all(isinstance(value, dict) and set(changed_ids) <= set(value)
            for value in (selected, candidates, bindings)), '共同选用记录缺少变更行')
        directory = selection_path.parent
        journal = recovery._journal(folder)
        event = next((item for item in journal['events']
            if directory.name == 'translation_' + item['id'] and item.get('action') == 'translation'
            and item.get('proxy_calls') == 2), None)
        _require(event is not None, '共同源文修订没有真实两请求恢复记录')
        scopes = [event] if event.get('status') == 'succeeded' else []
        phase = event.get('partial_review')
        if isinstance(phase, dict) and phase.get('status') == 'succeeded':
            scopes.append(phase)
        _require(any(set(changed_ids) <= set(item.get('segment_ids', [])) for item in scopes),
                 '差异超出已完成同次恢复范围')
        request = recovery._read(recovery._safe(directory, 'request.json'))
        editor = recovery._read(recovery._safe(directory, 'editor.json'))
        audit_path = recovery._safe(directory, 'audit.json')
        audit = recovery._read(audit_path)
        saved = event.get('saved_review', {})
        _require(all(_sha(recovery._safe(directory, name + '.json')) == saved.get(name + '_sha256')
            for name in ('request', 'editor', 'audit'))
            and selection.get('audit_sha256') == _sha(audit_path)
            and isinstance(request, dict) and request.get('source_sha256') == source_sha256,
            '共同恢复请求或实际独立审校已改变')
        from translation_recovery import _response
        _response(editor, request.get('items', []))
        _require(isinstance(audit, dict) and isinstance(audit.get('translations'), list), '独立审校格式不符')
        audit_rows = {item.get('id'): item for item in audit['translations'] if isinstance(item, dict)}
        request_rows = {item.get('id'): item for item in request.get('items', []) if isinstance(item, dict)}
        _require(len(audit_rows) == len(audit['translations']), '独立审校有重复行')
        for sid in changed_ids:
            prior, after, item, candidate = old[sid], current[sid], selected[sid], candidates[sid]
            supplied, approved = request_rows.get(sid), audit_rows.get(sid)
            _require(isinstance(item, dict) and isinstance(candidate, dict) and isinstance(supplied, dict)
                and isinstance(approved, dict) and approved.get('approved') is True and approved.get('issues') == []
                and approved.get('text_zh') == item.get('text_zh')
                and approved.get('source_choice') == item.get('source_choice')
                and bindings[sid] == supplied.get('source_binding') == source_binding(prior)
                and supplied.get('text_source') == prior['text_de']
                and supplied.get('source_language') == prior.get('source_language', 'de'),
                '旧源行、选用候选或独立批准不属于同次真实输入')
            choice = item.get('source_choice')
            _require(choice == 'exact' and isinstance(candidate.get('alternatives'), dict)
                and candidate['alternatives'].get('original') == prior['text_de']
                and candidate['alternatives'].get(choice) == supplied.get('source_alternatives', {}).get(choice) == after['text_de']
                and candidate.get('input_source_sha256') == source_sha256
                and candidate.get('original_text_sha256') == hashlib.sha256(prior['text_de'].encode()).hexdigest()
                and candidate.get('human_reviewed') is False and candidate.get('source_words_verified') is False,
                '变更不是这条完整真实原窗候选')
            candidate_ref = request.get('source_recheck_records', {}).get(sid)
            candidate_path = _reference(folder, candidate_ref, directory)
            original_candidate = recovery._read(candidate_path)
            _require({key: value for key, value in candidate.items() if key != 'excluded_source_choices'}
                == {key: value for key, value in original_candidate.items() if key != 'excluded_source_choices'},
                '选用候选与实际保存的源文复核不同')
            exact = [value for value in candidate.get('evidence', []) if value.get('kind') == 'exact']
            _require(len(exact) == 1 and exact[0].get('full_text') == after['text_de'], '候选没有完整原窗正文')
            _window(root, folder, prior, exact[0], source, video, old_rows, minimum_seconds=.02)
            proposal = candidate.get('language_proposal')
            if proposal:
                _require(isinstance(proposal, dict) and proposal.get('candidate') == after['text_de']
                    and proposal.get('original_language') == prior.get('source_language', 'de')
                    and proposal.get('language') == after.get('source_language', 'de')
                    and proposal.get('language_evidence') == changes[sid].get('language_evidence')
                    and not candidate_errors(prior['text_de'], after['text_de'], prior.get('source_language', 'de')),
                    '选用后的语言不属于实际双模型候选')
            else:
                _require(after.get('source_language', 'de') == prior.get('source_language', 'de')
                    and not source_alternative_errors(prior['text_de'], after['text_de'], prior.get('source_language', 'de')),
                    '普通原文修订改变了语言或受保护事实')
        return True
    raise ValueError('自动语言复核：多行输入差异没有共同的实际源文选用事务')


def _current_source(root, folder, row, source_sha256, *, committed=None):
    """Revalidate actual source bytes, state and the current evidence-bound plan."""
    root = Path(root).resolve()
    original_folder = Path(folder)
    _require(not original_folder.is_symlink(), '任务位置不允许符号链接')
    root, expected = recovery._folder(root, original_folder.name)
    _require(original_folder.resolve() == expected.resolve(), '任务不属于当前工程')
    folder = expected
    source_path = recovery._safe(folder, 'source.json')
    source = recovery._read(source_path)
    _require(isinstance(source, dict), '原片登记不是对象')
    video = Path(source.get('input', ''))
    ancestor = next((path for path in video.parents if path.resolve() == root), None)
    _require(video.is_absolute() and ancestor is not None, '原片位置不合法')
    video = recovery._safe(root, video.relative_to(ancestor))
    _require(video.is_file() and recovery._SHA.fullmatch(str(source_sha256))
        and _sha(video) == source_sha256, '当前原片真实SHA与请求不符')
    state_path = recovery._safe(root, Path('web/state/jobs') / (folder.name + '.json'))
    # Supervised episode children may have no independent website state. They
    # still bind directly to the actual immutable clip above.
    if state_path.is_file():
        state = recovery._read(state_path)
        _require(isinstance(state, dict) and state.get('source_sha256') == source_sha256,
                 '网页登记的原片身份不符')
    duration, track = source.get('duration_s'), source.get('audio_track')
    _require(_finite(duration) and duration > 0 and type(track) is int and track >= 0,
             '原片时长或音轨不合法')
    original = recovery._safe(folder, 'segments.plan.json')
    rows, originals, revision = _verified_plan(folder, source_sha256)
    _require(isinstance(rows, list) and rows and len(rows) <= 20000
        and len({item.get('segment_id') for item in rows}) == len(rows), '当前源计划缺行或重复')
    ids = {item['segment_id']: item for item in rows}
    identity = _row_identity(row)
    if committed is None:
        _require(row.get('segment_id') in ids and _row_identity(ids[row['segment_id']]) == identity,
                 '传入行与当前原片源计划不符')
    else:
        before = next((item for item in originals if item['segment_id'] == row.get('segment_id')), None)
        current = ids.get(row.get('segment_id'))
        _require(before == row and revision is not None and current is not None,
                 '历史语言修订没有绑定原始行与当前派生行')
        change = next((item for item in revision['changes'] if item['segment_id'] == row['segment_id']), None)
        _require(change is not None and change.get('action') == 'replace_source_text'
            and change.get('language_evidence') == committed['proof_ref']
            and current.get('text_de') == current.get('text_source') == committed['candidate']
            and current.get('source_language') == committed['language']
            and change.get('original_source_language') == row.get('source_language', 'de')
            and change.get('replacement_source_language') == committed['language'], '当前语言变更不是这份历史证据')
    for item in rows:
        _require(_finite(item.get('start_s')) and _finite(item.get('end_s'))
            and 0 <= item['start_s'] < item['end_s'] <= duration + .05,
            '源计划存在无效时间')
    input_path = recovery._safe(folder, 'translation/input.json')
    expected_input = [_row_identity(item) for item in rows]
    actual_input = expected_input
    if input_path.is_file():
        _, inputs = recovery._rows(folder)
        actual_input = [_row_identity(item) for item in inputs]
        if committed is None:
            _require(actual_input == expected_input, '当前翻译输入与源计划身份不一致')
        else:
            # Source recovery commits the derived plan before the controller
            # rebuilds translation/input. Admit the exact bound predecessor
            # for this row, while every other row must remain current.
            predecessor = [_row_identity(item) for item in committed.get('precommit_rows', [])]
            _require(committed.get('precommit_input_sha256') == recovery._hash(predecessor),
                     '提交前完整输入快照身份不符')
            if actual_input != expected_input:
                _require(actual_input == predecessor, '当前翻译输入不是已绑定的提交前或提交后完整输入')
                single = [identity if item['segment_id'] == row['segment_id'] else _row_identity(item)
                    for item in rows]
                if predecessor != single:
                    _approved_precommit_transition(root, folder, source, video, committed['precommit_rows'],
                        rows, revision, row['segment_id'], source_sha256)
    binding = {'source_receipt_sha256': _sha(source_path), 'input_source_sha256': source_sha256,
        'source_audio_track': track, 'source_duration_s': duration,
        'source_plan_sha256': _sha(recovery._safe(folder, 'revisions/source_review_v1/segments.plan.json'))
            if recovery._safe(folder, 'source_plan_revision.json').is_file() else _sha(original),
        'immutable_original_plan_sha256': _sha(original), 'row_identity': identity,
        'source_input_rows_sha256': recovery._hash(actual_input),
        'immutable_original_row_identity': _row_identity(next(item for item in originals
            if item['segment_id'] == row['segment_id']))}
    return root, folder, source, video, rows, binding


def _protected_source(folder, row):
    """A user decision cannot be downgraded to an automatic source candidate."""
    flags = ('source_word_confirmed_by_user', 'source_words_verified', 'user_contextual_adoption',
             'user_context_adopted', 'human_reviewed', 'user_editorial_omission', 'user_editorial_revision')
    if any(row.get(flag) is True for flag in flags) or row.get('translation_review_method') in (
            'user_contextual_translation_adoption','user_editorial_omission','user_editorial_revision'):
        return True
    hints_path = recovery._safe(folder, 'translation/source_review_hints.json')
    if hints_path.is_file():
        hints = recovery._read(hints_path)
        _require(isinstance(hints, dict) and hints.get('schema') == 'source_review_hints_v1'
            and isinstance(hints.get('rows'), dict), '原文约束格式不合法')
        hint = hints['rows'].get(row['segment_id'], {})
        _require(isinstance(hint, dict), '当前原文约束不是对象')
        if any(hint.get(flag) is True for flag in flags):
            return True
    adoption = recovery._safe(folder, 'translation/user_context_adoption.json')
    if adoption.is_file():
        value = recovery._read(adoption)
        _require(isinstance(value, dict), '用户采纳记录格式不合法')
        if value.get('segment_id') == row['segment_id'] and (value.get('user_context_adopted') is True
                or value.get('authority') == 'user_context_adoption'):
            return True
    revision = recovery._safe(folder, 'source_plan_revision.json')
    if revision.is_file():
        value = recovery._read(revision)
        for item in value.get('website_manual_corrections', []):
            if row['segment_id'] in item.get('source_word_confirmed_by_user_ids', []):
                return True
    return False


def _decode_source_pcm(video, start, end, track):
    """Decode the actual complete source window; no model or network is involved."""
    ffmpeg = shutil.which('ffmpeg') or '/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg'
    process = subprocess.run([ffmpeg, '-v', 'error', '-nostdin', '-ss', str(start), '-i', str(video),
        '-t', str(end - start), '-map', '0:a:' + str(track), '-vn', '-ac', '1', '-ar', '16000',
        '-c:a', 'pcm_s16le', '-f', 's16le', 'pipe:1'], check=True, capture_output=True, timeout=90)
    _require(bool(process.stdout), '未解码到真实原声PCM')
    return process.stdout


def _window(root, folder, row, proof, source, video, rows, *, minimum_seconds=2.):
    """SHA alone cannot establish that a substituted WAV came from this row."""
    from source_audio_recheck import exact_window_matches
    _require(isinstance(proof, dict) and proof.get('kind') == 'exact', '只允许完整原窗候选')
    start, end = proof.get('window_start_s'), proof.get('window_end_s')
    _require(_finite(start) and _finite(end) and 0 <= start < end <= source['duration_s'] + .0001
        and minimum_seconds <= end - start <= MAX_WINDOW_SECONDS and row.get('overlap') is False
        and exact_window_matches(proof, row, rows, source['duration_s']), '不是当前完整安全原窗')
    _require(all(item['segment_id'] == row['segment_id'] or
        item['end_s'] <= start + 1e-6 or item['start_s'] >= end - 1e-6 for item in rows),
        '音窗借用了相邻原句')
    clip = recovery._safe(folder, proof.get('clip', ''))
    _require(clip.is_file() and clip.is_relative_to(folder / 'revisions/source_review_v1/evidence')
        and _sha(clip) == proof.get('clip_sha256'), '原窗位置或音频SHA不符')
    with wave.open(str(clip), 'rb') as stream:
        _require(stream.getcomptype() == 'NONE' and stream.getnchannels() == 1
            and stream.getsampwidth() == 2 and stream.getframerate() == 16000,
            '原窗必须为真实16kHz单声道16位PCM')
        frames = stream.getnframes()
        pcm = stream.readframes(frames)
    _require(frames > 0 and len(pcm) == frames * 2
        and abs(frames / 16000 - (end - start)) <= 1 / 16000 + 1e-6,
        '原窗被裁短、截断或改变时长')
    _require(pcm == _decode_source_pcm(video, start, end, source['audio_track']),
             '实际WAV不等于当前原片完整音窗PCM')
    return clip


def _model_assets(root, path):
    path = Path(path).resolve()
    configured = Path(recovery._read(root / 'config/local.json')['asr_model']).resolve()
    medium_root = (Path.home() / '.cache/huggingface/hub/models--Systran--faster-whisper-medium/snapshots').resolve()
    _require(path.is_dir() and (path == configured or path.parent == medium_root)
        and (path / 'model.bin').is_file(), '不是已配置或已安装的独立本机识别模型')
    return [{'name': item.name, 'bytes': item.stat().st_size, 'sha256': _sha(item)}
        for item in sorted(path.iterdir()) if item.is_file() and not item.name.startswith('.')]


def _reference(folder, ref, directory):
    _require(isinstance(ref, dict) and set(ref) == {'path', 'sha256'}
        and isinstance(ref['path'], str) and recovery._SHA.fullmatch(str(ref['sha256'])), '证据引用不完整')
    path = recovery._safe(folder, ref['path'])
    _require(path.is_file() and path.is_relative_to(directory) and _sha(path) == ref['sha256'],
             '证据位置或SHA不符')
    return path


def _save_once(path, value):
    if path.is_file():
        _require(recovery._read(path) == value, '既有独立证据不能覆盖')
    else:
        recovery._save(path, value)


def _sha(path):
    return recovery._sha(Path(path))


def _normal(text):
    return ''.join(c for c in text.casefold() if c.isalnum())


def candidate_errors(original, candidate, language):
    """Never use language correction to discard known quantities or logic."""
    errors = []
    if (not isinstance(original, str) or not original.strip() or not isinstance(candidate, str)
            or not 1 <= len(candidate.strip()) <= 600
            or any(ord(char) < 32 and char not in '\t\n' for char in candidate)):
        return ['语言修订候选不是完整有界原句']
    try:
        language = normalize_language(language, False)
    except ValueError:
        return ['原语言不明确，不能自动替换源文']
    # The existing semantic/numeric guard is complete only for this known
    # German-misclassification path. Other original languages stay pending;
    # auto-detection does not authorize discarding their unknown qualifiers.
    if language != 'de':
        errors.append('非德语源文的跨语言事实保护尚未实现，不能自动替换')
    numbers = lambda text: collections.Counter(re.findall(r'\d+', text))
    if numbers(original) != numbers(candidate):
        errors.append('自动语言修订不能增加、删除或改变原文显式数字')
    if language == 'de':
        from proxy_client import NEG_DE, CONDITION_DE, numeric_mentions
        # Wrong-language words may be spurious; material qualifiers still need
        # explicit review instead of silently disappearing in this path.
        if NEG_DE.search(original) or CONDITION_DE.search(original):
            errors.append('带显式否定或条件的跨语言修订需要进一步事实复核')
        if numeric_mentions(original, 'de'):
            errors.append('带数量或单位的跨语言修订需要进一步事实复核')
    return errors


def consensus(observations, original_language):
    if not isinstance(observations, list) or len(observations) != 2:
        return None
    if any(not isinstance(item, dict) or not recovery._SHA.fullmatch(str(item.get('model_sha256', '')))
           for item in observations) or len({o['model_sha256'] for o in observations}) != 2:
        return None
    languages = []
    for item in observations:
        probability = item.get('language_probability')
        if (type(probability) not in (int, float) or not math.isfinite(probability)
                or not MIN_PROBABILITY <= probability <= 1
                or item.get('options') != OPTIONS or item.get('device') != 'cpu'
                or item.get('compute_type') != 'int8' or item.get('prompted') is not False
                or item.get('local_files_only') is not True
                or not isinstance(item.get('text'), str) or not 1 <= len(item['text'].strip()) <= 600):
            return None
        segments = item.get('segments')
        if (not isinstance(segments, list) or not segments or len(segments) > 300
                or any(not isinstance(segment, dict) or not isinstance(segment.get('text'), str)
                    or not _finite(segment.get('start_s')) or not _finite(segment.get('end_s'))
                    or not 0 <= segment['start_s'] < segment['end_s']
                    for segment in segments)
                or ''.join(segment['text'] for segment in segments).strip() != item['text']
                or any(right['start_s'] < left['end_s'] - .05 for left, right in zip(segments, segments[1:]))):
            return None
        try:
            languages.append(normalize_language(item.get('language'), allow_auto=False))
        except (TypeError, ValueError):
            return None
    if languages[0] != languages[1] or languages[0] == original_language:
        return None
    if not _normal(observations[0]['text']) or not _normal(observations[1]['text']):
        return None
    agreement = difflib.SequenceMatcher(None, _normal(observations[0]['text']),
                                       _normal(observations[1]['text']), autojunk=False).ratio()
    if agreement < MIN_AGREEMENT:
        return None
    return {'language': languages[0], 'text': observations[0]['text'].strip(),
            'normalized_agreement': agreement}


def detect_on_window(root, folder, row, exact_proof, primary_model, primary_path,
                     evidence_dir, source_sha256, secondary_factory=None):
    """Run only on the original complete safe window, without target text."""
    root, folder, source, video, rows, binding = _current_source(root, folder, row, source_sha256)
    if (_protected_source(folder, row) or row.get('source_language', 'de') != 'de'
            or binding['row_identity'] != binding['immutable_original_row_identity']):
        return None
    evidence_dir = Path(evidence_dir)
    _require(evidence_dir.is_absolute() and evidence_dir.is_relative_to(folder), '证据目录越界')
    evidence_dir = recovery._safe(folder, evidence_dir.relative_to(folder))
    _require(evidence_dir.is_relative_to(folder / 'revisions/source_review_v1/evidence'), '不是当前源文复核证据目录')
    clip = _window(root, folder, row, exact_proof, source, video, rows, minimum_seconds=.02)
    # Short complete windows remain available to ordinary exact-source review.
    # Validate their real PCM first, then skip only the two-model language
    # exception; a short corrupt/neighbour clip must never acquire a bypass.
    if exact_proof['window_end_s'] - exact_proof['window_start_s'] < 2:
        return None
    configured = Path(recovery._read(root/'config/local.json')['asr_model']).resolve()
    primary_path = Path(primary_path).resolve()
    if configured == primary_path or not (configured/'model.bin').is_file():
        return None
    primary_assets, secondary_assets = _model_assets(root, primary_path), _model_assets(root, configured)
    if _sha(primary_path / 'model.bin') == _sha(configured / 'model.bin'):
        return None
    if secondary_factory is None:
        from faster_whisper import WhisperModel
        secondary_factory = lambda path: WhisperModel(str(path), device='cpu', compute_type='int8',
            cpu_threads=2, num_workers=1, local_files_only=True)
    secondary = secondary_factory(configured)
    observations = []
    for label, model, path in (('primary', primary_model, primary_path),
                               ('secondary', secondary, configured)):
        segments, info = model.transcribe(str(clip), **OPTIONS)
        segments = list(segments)
        observations.append({'role': label, 'model_path': str(path), 'model_sha256': _sha(path/'model.bin'),
            'model_assets': primary_assets if label == 'primary' else secondary_assets,
            'language': info.language, 'language_probability': float(info.language_probability),
            'text': ''.join(s.text for s in segments).strip(),
            'segments': [{'text': s.text, 'start_s': float(s.start), 'end_s': float(s.end)} for s in segments],
            'device': 'cpu', 'compute_type': 'int8', 'local_files_only': True,
            'prompted': False, 'options': dict(OPTIONS)})
    from proxy_client import source_binding
    _require(_current_source(root, folder, row, source_sha256)[-1] == binding
        and not _protected_source(folder, row), '识别期间当前原片、源行或用户约束改变')
    _window(root, folder, row, exact_proof, source, video, rows)
    _require(_model_assets(root, primary_path) == primary_assets
        and _model_assets(root, configured) == secondary_assets, '识别期间模型资产改变')
    window_proof = {name: exact_proof[name] for name in ('kind', 'clip', 'clip_sha256',
        'window_start_s', 'window_end_s', 'method', 'original_start_s', 'original_end_s',
        'no_neighbor_sentence_included', 'no_next_sentence_included') if name in exact_proof}
    record = {'schema': SCHEMA, 'input_source_sha256': source_sha256,
        'source_binding': source_binding(row), 'input_binding': binding, 'window_proof': window_proof,
        'precommit_source_rows': copy.deepcopy(rows),
        'audio': {'path': str(clip.relative_to(folder)),
        'sha256': _sha(clip), 'start_s': exact_proof['window_start_s'], 'end_s': exact_proof['window_end_s']},
        'observations': observations, 'human_reviewed': False, 'source_words_verified': False,
        'helper_sha256': _sha(__file__)}
    raw_path = evidence_dir/(row['segment_id']+'_automatic_language_observations_'+recovery._hash(record)[:16]+'.json')
    _save_once(raw_path, record)
    chosen = consensus(observations, row.get('source_language', 'de'))
    if not chosen or candidate_errors(row['text_de'], chosen['text'], row.get('source_language', 'de')):
        return None
    ref = {'path': str(raw_path.relative_to(folder)), 'sha256': _sha(raw_path)}
    primary = observations[0]
    proof = {'schema': 'machine_row_language_evidence_v1', 'method': SCHEMA,
        'segment_id': row['segment_id'], 'input_source_sha256': source_sha256,
        'source_binding': source_binding(row), 'original_source_language': row.get('source_language', 'de'),
        'detected_source_language': chosen['language'],
        'replacement_text_sha256': hashlib.sha256(chosen['text'].encode()).hexdigest(),
        'detection': {'mode': 'auto', 'forced_language': None, 'language': chosen['language'],
            'language_probability': primary['language_probability'], 'model': 'local_cpu_primary_auto',
            'audio': record['audio'], 'report': ref},
        'transcription': {'mode': 'auto', 'forced_language': None, 'report': ref},
        'normalized_agreement': chosen['normalized_agreement'],
        'human_reviewed': False, 'source_words_verified': False}
    _require(all(all(segment['end_s'] <= exact_proof['window_end_s'] - exact_proof['window_start_s'] + .05
        for segment in item['segments']) for item in observations), '识别段落超出真实原窗')
    proof_path = evidence_dir/(row['segment_id']+'_automatic_language_evidence_'+recovery._hash(proof)[:16]+'.json')
    _save_once(proof_path, proof)
    return {'original_language': row.get('source_language', 'de'), 'language': chosen['language'],
            'candidate': chosen['text'], 'language_evidence': {'path': str(proof_path.relative_to(folder)),
            'sha256': _sha(proof_path)}}


def _verify_proposal(root, folder, row, candidate, proposal, source_sha256, *, committed=None):
    root, folder, source, video, rows, binding = _current_source(root, folder, row, source_sha256, committed=committed)
    if committed is None:
        _require(not _protected_source(folder, row), '已用户确认或采纳的源行不能自动改语言')
    else:
        _require(not any(binding['row_identity']['protected_user_source_flags'].values()),
                 '历史自动修订的原始行已经人工确认')
    if (not isinstance(proposal, dict) or proposal.get('candidate') != candidate
            or proposal.get('original_language') != row.get('source_language', 'de')
            or candidate_errors(row['text_de'], candidate, row.get('source_language', 'de'))):
        raise ValueError('自动语言候选未通过原句事实保护')
    path = _reference(folder, proposal.get('language_evidence'), folder/'revisions/source_review_v1/evidence')
    proof = recovery._read(path)
    from proxy_client import source_binding
    if (not isinstance(proof, dict) or proof.get('schema') != 'machine_row_language_evidence_v1'
            or proof.get('method') != SCHEMA or proof.get('segment_id') != row['segment_id']
            or proof.get('original_source_language') != row.get('source_language', 'de')
            or proof.get('input_source_sha256') != source_sha256
            or proof.get('source_binding') != source_binding(row)
            or proof.get('human_reviewed') is not False or proof.get('source_words_verified') is not False
            or proof.get('replacement_text_sha256') != hashlib.sha256(candidate.encode()).hexdigest()):
        raise ValueError('自动语言证据与当前原片原句不符')
    detection, transcription = proof.get('detection'), proof.get('transcription')
    _require(all(isinstance(item, dict) and item.get('mode') == 'auto'
        and 'forced_language' in item and item['forced_language'] is None
        for item in (detection, transcription)), '强制转写不能改标自动检测')
    _require(detection.get('report') == transcription.get('report'), '检测与全文转写不是同一双模型原始记录')
    raw_path = _reference(folder, detection.get('report'), path.parent)
    _require(raw_path.parent == path.parent, '双模型原始记录不在同一复核目录')
    raw = recovery._read(raw_path)
    _require(isinstance(raw, dict), '双模型原始记录不是对象')
    audio = raw.get('audio', {})
    if (not isinstance(raw, dict) or raw.get('schema') != SCHEMA or raw.get('input_source_sha256') != source_sha256
            or raw.get('source_binding') != source_binding(row)
            or raw.get('helper_sha256') != _sha(__file__)
            or raw.get('human_reviewed') is not False or raw.get('source_words_verified') is not False
            or not isinstance(audio, dict) or set(audio) != {'path', 'sha256', 'start_s', 'end_s'}
            or audio != detection.get('audio')):
        raise ValueError('双模型语言音窗与输入绑定不符')
    raw_binding = raw.get('input_binding')
    if committed is None:
        _require(raw_binding == binding, '当前源计划或原始源行已改变')
    else:
        _require(isinstance(raw_binding, dict)
            and recovery._SHA.fullmatch(str(raw_binding.get('source_plan_sha256', '')))
            and raw_binding.get('source_input_rows_sha256') == committed.get('precommit_input_sha256')
            and {key: value for key, value in raw_binding.items()
                if key not in {'source_plan_sha256', 'source_input_rows_sha256'}}
                == {key: value for key, value in binding.items()
                    if key not in {'source_plan_sha256', 'source_input_rows_sha256'}}
            and raw_binding.get('row_identity') == raw_binding.get('immutable_original_row_identity'),
            '历史语言证据没有绑定不可变原计划、当前原片和这条原始行')
    window = raw.get('window_proof')
    _require(isinstance(window, dict) and window.get('clip') == audio['path']
        and window.get('clip_sha256') == audio['sha256']
        and window.get('window_start_s') == audio['start_s']
        and window.get('window_end_s') == audio['end_s'], '完整原窗凭据与当前WAV不一致')
    _window(root, folder, row, window, source, video, rows)
    observations = raw.get('observations', [])
    configured = Path(recovery._read(root/'config/local.json')['asr_model']).resolve()
    _require(isinstance(observations, list) and len(observations) == 2
        and [item.get('role') for item in observations if isinstance(item, dict)] == ['primary', 'secondary'],
        '不是两份独立的原始本机观察')
    _require(Path(observations[1].get('model_path', '')).resolve() == configured,
             '第二识别器不是已配置本机模型')
    for item in observations:
        model_path = Path(item.get('model_path', '')).resolve()
        if (_model_assets(root, model_path) != item.get('model_assets')
                or _sha(model_path/'model.bin') != item.get('model_sha256')
                or item.get('local_files_only') is not True):
            raise ValueError('语言复核不属于已安装本机模型')
        _require(isinstance(item.get('segments'), list)
            and all(isinstance(segment, dict) and _finite(segment.get('end_s'))
                and segment['end_s'] <= audio['end_s'] - audio['start_s'] + .05
                for segment in item['segments']), '识别段落超出当前完整原窗')
    chosen = consensus(observations, row.get('source_language', 'de'))
    if (not chosen or chosen['text'] != candidate or chosen['language'] != proposal.get('language')
            or chosen['language'] != proof.get('detected_source_language')
            or chosen['language'] != detection.get('language')
            or not _finite(proof.get('normalized_agreement'))
            or proof['normalized_agreement'] != chosen['normalized_agreement']
            or not _finite(detection.get('language_probability'))
            or detection['language_probability'] != observations[0]['language_probability']
            or detection.get('model') != 'local_cpu_primary_auto'):
        raise ValueError('双模型语言判断或全文不一致')
    _require(_current_source(root, folder, row, source_sha256, committed=committed)[-1] == binding
        and (committed is not None or not _protected_source(folder, row)), '验证期间当前原片、源行或用户约束改变')
    return proposal['language']


def verify_proposal(root, folder, row, candidate, proposal, source_sha256):
    """Validate a not-yet-committed candidate against the current old source row."""
    return _verify_proposal(root, folder, row, candidate, proposal, source_sha256)


def verify_committed_language_evidence(root, folder, before, replacement, proof_ref, source_sha256,
                                     original_rows_or_allocation=None):
    """Replay one committed language change without recursively loading pipeline.

    `replacement` may be the full derived row or its exact source text. The
    caller's original allocation, when supplied, must equal the immutable plan.
    Current revision/change/proof identity and current derived text/language
    are checked independently, even if translation/input already uses it.
    """
    requested = Path(folder)
    root, folder = recovery._folder(root, requested.name)
    _require(not requested.is_symlink() and requested.resolve() == folder.resolve(), '历史证据调用越出当前任务')
    proof = recovery._read(_reference(folder, proof_ref, folder / 'revisions/source_review_v1/evidence'))
    _require(isinstance(proof, dict), '历史语言凭据不是对象')
    language = proof.get('detected_source_language')
    candidate = replacement.get('text_de') if isinstance(replacement, dict) else replacement
    proposal = {'candidate': candidate, 'original_language': before.get('source_language', 'de'),
        'language': language, 'language_evidence': proof_ref}
    committed = {'proof_ref': proof_ref, 'candidate': candidate, 'language': language}
    detection = proof.get('detection')
    _require(isinstance(detection, dict), '历史语言检测记录不完整')
    raw = recovery._read(_reference(folder, detection.get('report'), folder / 'revisions/source_review_v1/evidence'))
    _require(isinstance(raw, dict) and isinstance(raw.get('input_binding'), dict), '历史输入绑定不完整')
    committed['precommit_input_sha256'] = raw['input_binding'].get('source_input_rows_sha256')
    committed['precommit_rows'] = raw.get('precommit_source_rows')
    _require(isinstance(committed['precommit_rows'], list) and committed['precommit_rows']
        and all(isinstance(item, dict) for item in committed['precommit_rows'])
        and recovery._hash([_row_identity(item) for item in committed['precommit_rows']])
            == committed['precommit_input_sha256'], '历史提交前源行快照不完整')
    _, _, _, _, current, _ = _current_source(root, folder, before, source_sha256, committed=committed)
    originals = recovery._read(recovery._safe(folder, 'segments.plan.json'))['segments']
    if original_rows_or_allocation is not None:
        _require(original_rows_or_allocation == originals, '调用者提供的历史分配不是不可变原计划')
    if isinstance(replacement, dict):
        actual = next(item for item in current if item['segment_id'] == before['segment_id'])
        _require(_row_identity(replacement) == _row_identity(actual), '调用者派生行与当前完整源行不一致')
    return _verify_proposal(root, folder, before, candidate, proposal, source_sha256, committed=committed)


def validate_exact_source_window(root, folder, row, exact_proof, source_sha256, rows=None):
    """Shared read-only PCM proof for source-recheck resolution and diagnostics."""
    root, folder, source, video, current, binding = _current_source(root, folder, row, source_sha256)
    if rows is not None:
        _require([_row_identity(item) for item in rows] == [_row_identity(item) for item in current],
                 '调用者音窗分配与当前源计划不符')
    clip = _window(root, folder, row, exact_proof, source, video, current, minimum_seconds=.02)
    _require(_current_source(root, folder, row, source_sha256)[-1] == binding, '验证音窗期间原片或源计划改变')
    return {'path': str(clip.relative_to(folder)), 'sha256': _sha(clip),
        'start_s': exact_proof['window_start_s'], 'end_s': exact_proof['window_end_s'],
        'complete_source_pcm_verified': True, 'human_reviewed': False, 'source_words_verified': False}
