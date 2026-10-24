"""Explicit reuse of a saved, source-bound machine approval; never calls a model."""
import copy
import hashlib
from pathlib import Path

from proxy_client import VERSION, invariants, source_binding, continuity_join_suggestions
from website_recovery import _folder, _guard_context, _read, _safe, _sha

CONFIG = 'translation/prior_review_reuse.json'
SCHEMA = 'prior_review_reuse_v1'


def _text_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _equivalent(left, right):
    return isinstance(left, str) and ' '.join(left.split()).casefold() == ' '.join(right.split()).casefold()


def _one(items, sid):
    if not isinstance(items, list):
        raise ValueError('旧审核行格式错误')
    matches = [item for item in items if isinstance(item, dict) and item.get('id') == sid]
    if len(matches) != 1:
        raise ValueError('旧审核未唯一绑定当前行')
    return matches[0]


def apply_prior_reviews(root, folder, rows, updated, report):
    """Apply only explicitly named, hash-pinned saved responses to matching rows.

    The caller persists the returned rows and recomputes the report's file SHA.
    No diagnostics, cache, journal, budget or input object is changed here.
    Any stale configured binding is an error, never a silent fallback.
    """
    root, expected_folder = _folder(root, Path(folder).name)
    if Path(folder).absolute() != expected_folder:
        raise ValueError('旧审核复用任务位置不匹配')
    config_path = _safe(expected_folder, CONFIG)
    if not config_path.exists():
        return updated, report
    config = _read(config_path)
    if (not isinstance(config, dict) or set(config) != {'schema', 'input_source_sha256', 'rows'}
            or config.get('schema') != SCHEMA or not isinstance(config.get('rows'), dict)
            or not 1 <= len(config['rows']) <= 8):
        raise ValueError('旧审核复用配置不合法')
    source = _read(_safe(expected_folder, 'source.json'))
    source_path = Path(source['input'])
    if source_path.is_absolute():
        try:
            source_path = source_path.relative_to(root)
        except ValueError as error:
            raise ValueError('旧审核原片位置越界') from error
    source_path = _safe(root, source_path)
    source_sha = _sha(source_path)
    if source_sha != config.get('input_source_sha256'):
        raise ValueError('旧审核原片身份已改变')
    state_path = _safe(root, Path('web/state/jobs') / (expected_folder.name + '.json'))
    if state_path.is_file() and _read(state_path).get('source_sha256') != source_sha:
        raise ValueError('旧审核与网页原片身份不符')
    if (not isinstance(rows, list) or not isinstance(updated, list)
            or len(rows) != len(updated)
            or len({row['segment_id'] for row in rows}) != len(rows)
            or [row['segment_id'] for row in rows] != [row['segment_id'] for row in updated]
            or any(source_binding(row) != source_binding(after) for row, after in zip(rows, updated))):
        raise ValueError('旧审核复用前源行或行序已改变')
    if (report.get('translation_version') != VERSION or report.get('human_reviewed') is not False
            or report.get('review_method') != 'two_pass_machine_review_and_numeric_invariants'):
        raise ValueError('旧审核复用需要当前机器二审报告')
    lookup = {row['segment_id']: index for index, row in enumerate(rows)}
    verified = {}
    for sid, setting in config['rows'].items():
        if (sid not in lookup or not isinstance(setting, dict)
                or set(setting) != {'source_binding', 'diagnostic'}):
            raise ValueError('旧审核复用行配置不合法')
        index = lookup[sid]
        row = rows[index]
        if setting['source_binding'] != source_binding(row):
            raise ValueError('旧审核配置源绑定已改变')
        named = setting['diagnostic']
        if not isinstance(named, dict) or set(named) != {'path', 'sha256'}:
            raise ValueError('旧审核证据配置不合法')
        relative = Path(named['path'])
        if relative.parts[:2] != ('translation', 'diagnostics') or relative.suffix != '.json':
            raise ValueError('旧审核只能来自当前任务诊断目录')
        path = _safe(expected_folder, relative)
        if not path.is_file() or _sha(path) != named['sha256']:
            raise ValueError('旧审核证据内容已改变')
        diagnostic = _read(path)
        if (diagnostic.get('schema') != 'proxy_translation_diagnostic_v1'
                or diagnostic.get('version') != VERSION or diagnostic.get('stage') not in ('audit', 'repair')
                or diagnostic.get('status') != 'response_received'
                or diagnostic.get('human_reviewed') is not False
                or diagnostic.get('model') != report.get('model')):
            raise ValueError('旧审核证据不是兼容的真实机器二审')
        request = diagnostic.get('input', {})
        item = _one(request.get('items'), sid)
        approved = _one(diagnostic.get('response', {}).get('translations'), sid)
        if (item.get('source_binding') != source_binding(row) or item.get('text_de') != row['text_de']
                or item.get('text_source', row['text_de']) != row['text_de']
                or item.get('source_language', request.get('source_language', 'de')) != row.get('source_language', 'de')
                or approved.get('approved') is not True or approved.get('issues') != []):
            raise ValueError('旧审核没有明确批准当前源行')
        for offset, field in ((-1, 'previous_context'), (1, 'next_context')):
            neighbor = rows[index + offset] if 0 <= index + offset < len(rows) else None
            if not _equivalent(item.get(field), neighbor['text_de'] if neighbor else ''):
                raise ValueError('旧审核上下文实质改变')
        previous = rows[index-1] if index else None
        previous_binding = source_binding(previous) if previous else None
        if previous:
            previous_binding['text_de_sha256'] = _text_hash(item['previous_context'])
            if 'text_source_sha256' in previous_binding:
                previous_binding['text_source_sha256'] = previous_binding['text_de_sha256']
        if item.get('previous_source_binding') != previous_binding:
            raise ValueError('旧审核前行身份或上下文绑定不一致')
        text = approved.get('text_zh')
        if (not isinstance(text, str) or not text.strip() or len(text) > 4000
                or any(ord(char) < 32 for char in text)):
            raise ValueError('旧审核中文内容不合法')
        errors = invariants(row['text_de'], text, previous_context=_guard_context(rows, index),
                            source_language=row.get('source_language', 'de'))
        if errors:
            raise ValueError('旧审核中文未通过当前事实检查: ' + str(errors))
        verified[sid] = (text, {'source_binding': source_binding(row),
            'diagnostic': dict(named), 'translation_version': VERSION, 'model': diagnostic['model'],
            'reviewed_text_zh_sha256': _text_hash(text), 'stage': diagnostic['stage'],
            'context_comparison': 'casefold_and_whitespace_only', 'human_reviewed': False,
            'new_api_calls': 0})
    result = copy.deepcopy(updated)
    result_report = copy.deepcopy(report)
    reason = '显式复用既有二审译文；连续合组建议保持关闭'
    for row in result:
        sid = row['segment_id']
        if sid not in verified:
            continue
        text, evidence = verified[sid]
        prior = row.get('prior_review_reuse', {})
        evidence['superseded_output_text_zh_sha256'] = (prior.get('superseded_output_text_zh_sha256')
            if prior.get('diagnostic') == evidence['diagnostic'] else _text_hash(row['text_zh']))
        row['text_zh'] = text
        row['prior_review_reuse'] = evidence
        row['join_previous'] = False
        row['join_reason'] = reason
        row['topic'] = None
        suggestion = row.get('continuity_suggestion')
        if isinstance(suggestion, dict):
            suggestion.update(join_previous=False, join_previous_id=None, join_reason=reason,
                topic=None, machine_reviewed=False, human_reviewed=False,
                review_status='prior_review_reuse_translation_only', cache_provenance='explicit_prior_review_reuse')
    # Continuity hashes refer to the final Chinese, including adjacent bindings.
    for index, row in enumerate(result):
        suggestion = row.get('continuity_suggestion')
        if isinstance(suggestion, dict):
            suggestion['source_binding'] = source_binding(row, row['text_zh'])
            suggestion['previous_source_binding'] = source_binding(result[index-1], result[index-1]['text_zh']) if index else None
    result_report['prior_review_reuse'] = {'schema': SCHEMA, 'config_sha256': _sha(config_path),
        'input_source_sha256': source_sha, 'rows': {sid: value[1] for sid, value in verified.items()},
        'human_reviewed': False, 'new_api_calls': 0, 'budget_changed': False,
        'latest_diagnostics_and_cache_preserved': True,
        'latest_review_not_reinterpreted': True,
        'new_review_performed': False,
        'review_scope': 'saved_actual_approval_and_current_local_guards; no_new_review'}
    metadata = {row['segment_id']: row.get('continuity_suggestion') for row in result}
    if all(isinstance(value, dict) for value in metadata.values()):
        result_report['tts_join_suggestions'] = continuity_join_suggestions(result, metadata)
        summary = result_report.get('continuity_suggestions')
        if isinstance(summary, dict):
            summary['machine_reviewed_rows'] = sum(value.get('machine_reviewed') is True for value in metadata.values())
            summary['accepted_join_suggestions'] = sum(value.get('join_previous') is True for value in metadata.values())
        result_report['review_status'] = 'machine_second_pass_partial_continuity_review'
    return result, result_report
