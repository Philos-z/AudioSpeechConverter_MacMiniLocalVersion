"""Deterministic daily-queue observations and derived state, with no executor.

Only ``projection`` and the legacy ``next_action`` summary can be reconciled.
Jobs, holds, decisions, frozen sources, budgets, outboxes and automation settings
are inputs, never writable outputs of this module. HTTP is GET-only and local.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import math
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request

from task_takeover import InstanceLock, write_json

STATE_SCHEMA = 'tagesschau_daily_automation_state_v1'
OBSERVATION_SCHEMA = 'tagesschau_daily_tick_observation_v1'
PLAN_SCHEMA = 'tagesschau_daily_tick_plan_v1'
PROJECTION_SCHEMA = 'tagesschau_daily_projection_v1'
CONFIG = 'config/tagesschau_daily_automation.json'
JOB_ID = re.compile(r'web_\d{8}_\d{6}_[a-f0-9]{8}')
SHA = re.compile(r'[a-f0-9]{64}')
MAX_JSON = 16 * 1024 ** 2
RESOLVED_DECISIONS = {'answered', 'consumed_and_committed', 'review_passed_committed',
    'adopted_exact_current_voices_completed', 'completed', 'reviewed_partial'}
WAITING_DECISIONS = {'awaiting_answer', 'asked_once', 'asked_once_waiting_actual_reply',
    'waiting_actual_reply', 'awaiting_actual_reply', 'asked'}


class StateError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise StateError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate_json_key')
        result[key] = value
    return result


def parse_json(data):
    require(len(data) <= MAX_JSON, 'json_too_large')
    try:
        return json.loads(data, object_pairs_hook=_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(StateError('nonfinite_json')))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise StateError('invalid_json') from error


def confined(root, value):
    require(isinstance(value, (str, Path)) and bool(str(value)), 'invalid_path')
    root = Path(root).resolve()
    path = Path(value)
    path = path if path.is_absolute() else root / path
    require(path.is_relative_to(root) and path.resolve().is_relative_to(root), 'path_outside_root')
    require(path.absolute() == path.resolve(), 'symlink_or_noncanonical_path')
    return path


def read_file(root, path):
    path = confined(root, path)
    require(path.is_file() and path.stat().st_size <= MAX_JSON, 'missing_or_large_json')
    data = path.read_bytes()
    require(len(data) <= MAX_JSON, 'json_too_large')
    return parse_json(data), digest(data)


def timestamp(value):
    require(isinstance(value, str), 'invalid_observation_time')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as error:
        raise StateError('invalid_observation_time') from error
    require(result.tzinfo is not None, 'observation_time_requires_timezone')
    return result.astimezone(timezone.utc)


def utc_now():
    return datetime.now(timezone.utc)


def loopback_base(value):
    require(isinstance(value, str), 'invalid_api_base')
    try:
        url = urllib.parse.urlsplit(value)
        host, port = url.hostname, url.port
    except ValueError as error:
        raise StateError('invalid_api_base') from error
    require(url.scheme in {'http', 'https'} and host and url.username is None
        and url.password is None and not url.query and not url.fragment
        and url.path in {'', '/'}, 'invalid_api_base')
    # Avoid DNS and proxy environment variables even for the localhost alias.
    if host == 'localhost':
        host = '127.0.0.1'
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise StateError('api_must_be_loopback_literal_or_localhost') from error
    require(address.is_loopback, 'api_must_be_loopback')
    require(port is not None and 1 <= port <= 65535, 'api_requires_explicit_port')
    authority = '[' + host + ']' if address.version == 6 else host
    return f'{url.scheme}://{authority}:{port}'


def load_config(root):
    root = Path(root).resolve()
    config, sha = read_file(root, CONFIG)
    require(isinstance(config, dict) and config.get('schema') == 'tagesschau_daily_automation_config_v1',
        'unsupported_config_schema')
    configured_root = config.get('root')
    require(isinstance(configured_root, str) and bool(configured_root.strip()), 'missing_config_root')
    configured_root = Path(configured_root)
    if not configured_root.is_absolute():
        configured_root = root / configured_root
    require(configured_root.resolve() == root, 'config_root_mismatch')
    loopback_base(config.get('api_base'))
    state_path = confined(root, config.get('state_path', ''))
    require(state_path.suffix == '.json' and state_path.is_relative_to(root / 'web/state'),
        'invalid_state_location')
    return config, sha


def validate_state(state):
    require(isinstance(state, dict) and state.get('schema') == STATE_SCHEMA, 'unsupported_state_schema')
    require(type(state.get('generation')) is int and state['generation'] >= 0, 'invalid_generation')
    for field in ('watched', 'daily_intents', 'episodes', 'notifications'):
        require(isinstance(state.get(field, {}), dict), 'invalid_' + field)
    conflicts = []
    for job_id, watch in state.get('watched', {}).items():
        require(JOB_ID.fullmatch(job_id) and isinstance(watch, dict), 'invalid_watch')
        require(watch.get('job_id', job_id) == job_id, 'watch_identity_mismatch')
        for field in ('email', 'user_hold', 'pending_voice_decision', 'pending_editorial_decision', 'pending_final_voice_choice'):
            require(field not in watch or watch[field] is None or isinstance(watch[field], dict),
                'invalid_watch_' + field)
    for date, intent in state.get('daily_intents', {}).items():
        require(isinstance(intent, dict), 'invalid_intent')
        job_id, episode_key = intent.get('job_id'), intent.get('episode_key')
        episode = state.get('episodes', {}).get(episode_key, {})
        require(isinstance(episode, dict), 'invalid_episode')
        if job_id and (job_id not in state.get('watched', {}) or
                (episode.get('job_id') and episode['job_id'] != job_id)):
            conflicts.append('intent_binding_conflict:' + date)
        if job_id and state.get('watched', {}).get(job_id, {}).get('episode_key', episode_key) != episode_key:
            conflicts.append('watch_episode_conflict:' + date)
    for key, episode in state.get('episodes', {}).items():
        require(isinstance(episode, dict), 'invalid_episode')
        job_id = episode.get('job_id')
        if job_id and (job_id not in state.get('watched', {}) or
                state['watched'][job_id].get('episode_key', key) != key):
            conflicts.append('episode_binding_conflict:' + key)
    return sorted(set(conflicts))


def snapshot(root):
    """One short queue transaction; no HTTP or model work under its lock."""
    root = Path(root).resolve()
    config, config_sha = load_config(root)
    path = confined(root, config['state_path'])
    with InstanceLock(path.with_suffix('.lock')):
        state, state_sha = read_file(root, path)
        validate_state(state)
    return state, {'root': str(root), 'config_sha256': config_sha,
        'state_sha256': state_sha, 'generation': state['generation']}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise StateError('redirect_rejected')


class LocalGet:
    """Only GET liveness and individual job snapshots; no /health or executor."""
    def __init__(self, api_base, timeout=3):
        self.base = loopback_base(api_base)
        self.timeout = timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def __call__(self, route):
        require(route == '/api/liveness' or re.fullmatch(r'/api/jobs/' + JOB_ID.pattern, route),
            'unsupported_read_route')
        request = urllib.request.Request(self.base + route, method='GET', headers={'Accept': 'application/json'})
        with self.opener.open(request, timeout=self.timeout) as response:
            require(response.status == 200 and response.url == self.base + route, 'unexpected_http_response')
            return parse_json(response.read(MAX_JSON + 1))


def _runtime(job):
    require(isinstance(job, dict) and JOB_ID.fullmatch(str(job.get('id', ''))), 'invalid_job')
    progress = job.get('progress')
    require(type(progress) in (int, float) and math.isfinite(progress) and 0 <= progress <= 100,
        'invalid_job_progress')
    require(job.get('status') in {'queued', 'running', 'failed', 'canceled', 'completed'}, 'invalid_job_status')
    return {key: job.get(key) for key in ('id', 'status', 'stage', 'progress', 'updated_at')}


def _reference(root, reference, files):
    require(isinstance(reference, dict), 'missing_evidence_reference')
    value = reference.get('path') or reference.get('report')
    require(isinstance(value, str) and SHA.fullmatch(str(reference.get('sha256', ''))), 'invalid_evidence_reference')
    path = confined(root, value)
    document, actual = read_file(root, path)
    require(actual == reference['sha256'], 'evidence_sha_mismatch')
    files[str(path.relative_to(root))] = actual
    return document


def _proof(function):
    try:
        return {'valid': True, **function()}
    except (StateError, OSError, TypeError, KeyError, ValueError):
        return {'valid': False, 'reason': 'missing_invalid_or_unbound_evidence'}


def _completion(root, job_id, watch, local, files):
    document = _reference(root, watch.get('completion_verification'), files)
    require(isinstance(document, dict) and document.get('job_id', document.get('job')) == job_id,
        'completion_job_mismatch')
    schema = document.get('schema')
    if schema == 'final_tagesschau_completion_verified_v1':
        tail = document.get('source_ending_retained')
        tail_retained = tail is True or (isinstance(tail, dict)
            and tail.get('source_ending_retained') is True
            and tail.get('audio_and_picture_share_same_clip') is True)
        require(document.get('all_final_media_and_archive_sha_verified') is True
            and document.get('whole_video_full_decode_passed') is True
            and tail_retained, 'incomplete_legacy_completion')
        video = document['video']
        size, video_sha = video['bytes'], video['sha256']
        require(document.get('source_sha256') == local.get('source_sha256') == watch.get('source_sha256'),
            'completion_source_mismatch')
    elif schema == 'actual_complete_media_archive_verification_v1':
        require(document.get('api_and_local_completed') is True
            and document.get('full_decode_current_same_sha_passed') is True
            and document.get('bilingual_cues_complete') is True, 'incomplete_completion')
        require(isinstance(document.get('artifacts'), list) and len(document['artifacts']) >= 5,
            'missing_archive_artifacts')
        _reference(root, document.get('archive_manifest'), files)
        size, video_sha = document['bytes'], document['video_sha256']
    else:
        raise StateError('unsupported_completion_schema')
    require(type(size) is int and 0 < size < 50000000 and SHA.fullmatch(str(video_sha)), 'invalid_final_video')
    require(local.get('status') == 'completed', 'local_not_completed')
    artifacts = local.get('artifacts', [])
    require(any(a.get('kind') == 'video' and a.get('sha256') == video_sha for a in artifacts),
        'completion_video_not_current')
    return {'video_sha256': video_sha}


def _email(root, job_id, watch, config, completion, files):
    email = watch.get('email', {})
    require(email.get('status') == 'sent' and completion.get('valid'), 'email_not_confirmed')
    proof = _reference(root, email.get('receipt'), files)
    require(proof.get('job_id') == job_id and proof.get('to', proof.get('recipient')) ==
        email.get('to') == config.get('notification', {}).get('to'), 'email_recipient_or_job_mismatch')
    require(proof.get('subject') == email.get('subject') and proof.get('body_sha256') == email.get('body_sha256')
        and SHA.fullmatch(str(email.get('body_sha256', ''))), 'email_body_binding_mismatch')
    video_sha = email.get('video_sha256', email.get('final_video_sha256'))
    require(video_sha == completion.get('video_sha256'), 'email_video_binding_mismatch')
    if proof.get('schema') == 'tagesschau_completion_email_receipt_v1':
        require(proof.get('status') == 'sent' and proof.get('final_video_sha256') == video_sha,
            'legacy_send_not_confirmed')
        result = _reference(root, proof.get('tool_result'), files)
        require(result.get('isError') is False and any(block.get('type') == 'text'
            and block.get('text') == 'Action completed.' for block in result.get('content', [])),
            'legacy_tool_send_not_successful')
        intent = _reference(root, proof.get('intent'), files)
        require(intent.get('schema') == 'tagesschau_completion_email_intent_v1'
            and intent.get('job_id') == job_id and intent.get('to') == email['to']
            and intent.get('subject') == email['subject']
            and isinstance(intent.get('body'), str)
            and digest(intent['body'].encode('utf-8')) == intent.get('body_sha256') == email['body_sha256']
            and intent.get('final_video_sha256') == video_sha and intent.get('save_to_sent_items') is True
            and proof.get('send_attempts') == 1, 'legacy_send_intent_changed')
    elif proof.get('schema') == 'actual_outlook_sent_items_confirmed_v1':
        require(proof.get('send_tool_success') is True and proof.get('message_id_from_sent_items')
            and proof.get('full_body_matches_after_outlook_whitespace_folding') is True,
            'sent_items_not_confirmed')
        for key in ('send_result', 'sent_items_query', 'sent_full_message'):
            _reference(root, proof.get(key), files)
    else:
        raise StateError('unsupported_email_receipt')
    return {}


def _legacy_final_adoption(root, job_id, watch, files):
    """Recognize legacy supersession via its exact, bounded evidence chain."""
    old = watch.get('pending_voice_decision', {})
    final = watch.get('pending_final_voice_choice', {})
    require(old.get('status') == 'superseded_by_final_adoption_reply'
        and final.get('status') == 'answered_adopted', 'not_final_adoption')
    require(final.get('adoption_is_not_new_regeneration_authorization') is True
        and final.get('line0195_seed43_authorized') is False, 'adoption_authority_changed')
    require(old.get('scope_ref') == final.get('original_scope_ref')
        and old.get('scope_sha256') == final.get('original_scope_sha256'), 'adoption_scope_chain_changed')
    scope_ref = {'path': old.get('scope_ref'), 'sha256': old.get('scope_sha256')}
    scope = _reference(root, scope_ref, files)
    reply = _reference(root, {'path': final.get('decision_ref'), 'sha256': final.get('decision_sha256')}, files)
    metadata = scope.get('typed_current_failure_metadata', {})
    ids = metadata.get('segment_ids')
    require(scope.get('job') == metadata.get('job_id') == reply.get('job') == job_id
        and metadata.get('source_sha256') == reply.get('source_video_sha256') == watch.get('source_sha256'),
        'adoption_job_or_source_changed')
    require(reply.get('schema') == 'current_job_user_voice_adoption_v1'
        and reply.get('decision_origin') == 'actual_human_user_reply'
        and reply.get('answer_verbatim') == final.get('answer_verbatim')
        and bool(final.get('answer_verbatim'))
        and reply.get('question') == final.get('question')
        and reply.get('new_regeneration_authorized') is False
        and reply.get('seed43_authorized') is False, 'not_exact_adoption_reply')
    original = reply.get('original_scope', {})
    require(original.get('sha256') == scope_ref['sha256']
        and confined(root, original.get('path')) == confined(root, scope_ref['path']), 'reply_scope_changed')
    require(isinstance(ids, list) and bool(ids) and len(set(ids)) == len(ids)
        and ids == final.get('human_listened_scope')
        and ids == [row.get('id') for row in reply.get('rows', [])], 'adoption_row_scope_changed')
    corrected = final.get('corrected_candidate_ids', [])
    require(isinstance(corrected, list) and len(set(corrected)) == len(corrected)
        and set(corrected).issubset(ids)
        and final.get('other_original_ids') == [sid for sid in ids if sid not in corrected],
        'adoption_choice_scope_changed')
    delivery_ref = reply.get('corrected_delivery', {})
    require(confined(root, delivery_ref.get('path')) == confined(root, final.get('corrected_delivery_ref')),
        'adoption_delivery_changed')
    _reference(root, delivery_ref, files)
    return {'scope': ids}


def _decisions(root, job_id, watch, files):
    result = {}
    old_voice = watch.get('pending_voice_decision')
    legacy = isinstance(old_voice, dict) and old_voice.get('status') == 'superseded_by_final_adoption_reply'
    adoption = _proof(lambda: _legacy_final_adoption(root, job_id, watch, files)) if legacy else None
    if adoption is not None:
        result['pending_final_voice_choice'] = {'effective_status': 'resolved' if adoption['valid'] else 'evidence_invalid',
            'source_status': watch.get('pending_final_voice_choice', {}).get('status'),
            'do_not_reask': True, 'scope': adoption.get('scope', [])}
    for key in ('pending_editorial_decision', 'pending_voice_decision'):
        decision = watch.get(key)
        if not isinstance(decision, dict):
            continue
        if key == 'pending_voice_decision' and adoption is not None:
            result[key] = {'effective_status': 'resolved' if adoption['valid'] else 'evidence_invalid',
                'source_status': decision.get('status'), 'do_not_reask': True,
                'scope': adoption.get('scope', [])}
            continue
        proof = _proof(lambda: {'response': _reference(root, decision.get('response_ref'), files)})
        # The response object is not copied into compact observations.
        status = decision.get('status')
        if status == 'deferred_by_user':
            effective = 'deferred_by_user' if proof['valid'] else 'evidence_invalid'
        elif status in RESOLVED_DECISIONS or decision.get('authorization_consumed') is True:
            effective = 'resolved' if proof['valid'] and decision.get('user_answer') else 'evidence_invalid'
        elif status in WAITING_DECISIONS or decision.get('question_asked_once') is True or decision.get('asked_once') is True:
            effective = 'awaiting_actual_reply'
        else:
            effective = 'unresolved_decision_state'
        result[key] = {'effective_status': effective, 'source_status': status,
            'do_not_reask': bool(decision.get('do_not_reask') or decision.get('do_not_reask_after_question')
                or decision.get('asked_once') or decision.get('question_asked_once')),
            'scope': decision.get('scope', [])}
    return result


def observe(root, state, binding, *, get=None, now=None):
    """Collect bounded, local observations outside the queue lock."""
    root = Path(root).resolve()
    config, sha = load_config(root)
    require(sha == binding['config_sha256'] and str(root) == binding['root'], 'configuration_changed')
    get = get or LocalGet(config['api_base'])
    at = now or utc_now()
    require(at.tzinfo is not None, 'observation_time_requires_timezone')
    expected_identity = digest(str(root).encode())
    try:
        live = get('/api/liveness')
        require(isinstance(live, dict) and live.get('version') == 'philos_lan_web_v1'
            and live.get('project_identity') == expected_identity
            and type(live.get('worker_alive')) is bool and type(live.get('stopping')) is bool,
            'liveness_identity_mismatch')
        liveness = {key: live[key] for key in ('version', 'project_identity', 'worker_alive', 'stopping')}
        liveness['valid'] = True
    except (StateError, OSError, ValueError, TypeError):
        liveness = {'valid': False, 'reason': 'liveness_unavailable_or_wrong_identity'}
    jobs, files = {}, {}
    for job_id, watch in sorted(state.get('watched', {}).items()):
        row = {'valid': False, 'local': None, 'api': None}
        try:
            path = Path('web/state/jobs') / (job_id + '.json')
            local, file_sha = read_file(root, path)
            files[str(path)] = file_sha
            require(local.get('id') == job_id, 'local_job_identity_mismatch')
            require(not watch.get('source_url') or local.get('source_url') == watch['source_url'],
                'job_source_url_changed')
            require(not watch.get('source_sha256') or local.get('source_sha256') == watch['source_sha256'],
                'job_source_sha_changed')
            row['local'] = _runtime(local)
            if liveness['valid']:
                api = get('/api/jobs/' + job_id)
                row['api'] = _runtime(api)
                require(row['api'] == row['local'], 'api_local_snapshot_mismatch')
            require(row['api'] is not None, 'api_not_observed')
            # Re-read after HTTP: do not silently combine two runtime revisions.
            require(read_file(root, path)[1] == file_sha, 'job_changed_during_observation')
            row['valid'] = True
            row['completion'] = _proof(lambda: _completion(root, job_id, watch, local, files))
            row['email'] = _proof(lambda: _email(root, job_id, watch, config, row['completion'], files))
        except (StateError, OSError, ValueError, TypeError, KeyError):
            row['reason'] = 'job_missing_changed_or_api_local_mismatch'
        row['decisions'] = _decisions(root, job_id, watch, files)
        jobs[job_id] = row
    return {'schema': OBSERVATION_SCHEMA, 'observed_at': at.astimezone(timezone.utc).isoformat(),
        'binding': copy.deepcopy(binding), 'liveness': liveness, 'jobs': jobs,
        'files': dict(sorted(files.items())), 'external_mutations_performed': False}


def derive(state, observation):
    """Pure projection: no I/O and no permission or budget inference from prose."""
    conflicts = validate_state(state)
    require(isinstance(observation, dict) and observation.get('schema') == OBSERVATION_SCHEMA,
        'unsupported_observation_schema')
    require(observation.get('external_mutations_performed') is False, 'invalid_observation_boundary')
    jobs, actions, decisions = {}, [], []
    if not observation.get('liveness', {}).get('valid'):
        conflicts.append('service_observation_unavailable')
    live = observation.get('liveness', {})
    operational = live.get('valid') is True and live.get('worker_alive') is True and live.get('stopping') is False
    if live.get('valid') is True and not operational:
        conflicts.append('service_worker_not_operational')
    for job_id, watch in sorted(state.get('watched', {}).items()):
        observed = observation.get('jobs', {}).get(job_id, {})
        decision_rows = observed.get('decisions', {})
        holds = [watch.get('user_hold', {})] + [intent.get('user_hold', {})
            for intent in state.get('daily_intents', {}).values() if intent.get('job_id') == job_id]
        pending = [key for key, item in decision_rows.items() if item.get('effective_status') == 'awaiting_actual_reply']
        invalid = [key for key, item in decision_rows.items() if item.get('effective_status') in
            {'evidence_invalid', 'unresolved_decision_state'}]
        for key in sorted(decision_rows):
            decisions.append({'job_id': job_id, 'field': key, **copy.deepcopy(decision_rows[key])})
        if not observed.get('valid'):
            conflicts.append('job_observation_unavailable:' + job_id)
        if invalid:
            conflicts.append('decision_evidence_invalid:' + job_id)
        runtime = observed.get('local') or {}
        row = {'runtime': copy.deepcopy(runtime), 'observation_valid': observed.get('valid') is True,
            'decisions': copy.deepcopy(decision_rows)}
        if any(isinstance(hold, dict) and (hold.get('automatic_resumption_allowed') is False
                or hold.get('status') == 'deferred_by_user') for hold in holds) or any(
                    value.get('effective_status') == 'deferred_by_user' for value in decision_rows.values()):
            kind = 'preserve_user_hold'
        elif not observed.get('valid') or (runtime.get('status') in {'running', 'queued'} and not operational):
            kind = 'inspect_observation_conflict'
        elif runtime.get('status') in {'running', 'queued'}:
            kind = 'observe_running_controller'
        elif runtime.get('status') == 'canceled':
            kind = 'preserve_canceled_job'
        elif pending or invalid:
            kind = 'preserve_pending_decision' if pending else 'inspect_decision_evidence'
        elif runtime.get('status') == 'completed':
            if not observed.get('completion', {}).get('valid'):
                kind = 'verify_completion_evidence'
            elif observed.get('email', {}).get('valid'):
                kind = 'completed_notified'
            else:
                email = watch.get('email', {})
                attempts = email.get('send_attempts', email.get('attempts', 0))
                if email.get('status') == 'pending' and type(attempts) is int and attempts == 0:
                    kind = 'connector_send_candidate'
                else:
                    kind = 'connector_verify_sent_items'
        else:
            kind = 'inspect_failure_no_automatic_retry'
        row['next_action'] = kind
        jobs[job_id] = row
        actions.append({'kind': kind, 'job_id': job_id, 'execution_allowed': False,
            'notification_needed': kind in {'inspect_observation_conflict', 'inspect_decision_evidence',
                'verify_completion_evidence', 'connector_send_candidate', 'connector_verify_sent_items',
                'inspect_failure_no_automatic_retry'} and not pending})
    intents = {}
    for date, intent in sorted(state.get('daily_intents', {}).items()):
        status = intent.get('status')
        kind = 'preserve_intent'
        if status in {'submitting', 'submission_unknown'}:
            kind = 'reconcile_submission_without_repost'
        elif status == 'waiting_previous':
            kind = 'preserve_waiting_previous'
        elif status in {'waiting_source', 'waiting_storage'}:
            kind = 'preserve_frozen_wait'
        intents[date] = {'status': status, 'episode_key': intent.get('episode_key'),
            'job_id': intent.get('job_id'), 'next_action': kind}
        if kind != 'preserve_intent':
            actions.append({'kind': kind, 'intent_date': date, 'execution_allowed': False,
                'notification_needed': kind == 'reconcile_submission_without_repost'})
    notifications = {}
    for key, item in sorted(state.get('notifications', {}).items()):
        job_id = item.get('job_id') if isinstance(item, dict) else None
        candidates = jobs.get(job_id, {}).get('decisions', {})
        scope = item.get('scope') if isinstance(item, dict) else None
        matching = [value for value in candidates.values() if scope is not None and value.get('scope') == scope]
        if len(matching) == 1:
            effective = matching[0]['effective_status']
        elif job_id in jobs and jobs[job_id]['next_action'] == 'completed_notified':
            effective = 'resolved_completed_notified'
        elif job_id in jobs and jobs[job_id]['next_action'] == 'preserve_user_hold':
            effective = 'deferred_by_user'
        else:
            effective = 'unresolved_mapping'
        notifications[key] = {'job_id': job_id, 'source_status': item.get('status') if isinstance(item, dict) else None,
            'effective_status': effective, 'historical_event_preserved': True}
    priority = {'observe_running_controller': 1, 'preserve_pending_decision': 2,
        'inspect_observation_conflict': 3, 'inspect_decision_evidence': 3,
        'verify_completion_evidence': 4, 'connector_verify_sent_items': 5,
        'connector_send_candidate': 6, 'inspect_failure_no_automatic_retry': 7}
    actions.sort(key=lambda a: (priority.get(a['kind'], 20), a.get('job_id', ''), a.get('intent_date', '')))
    active = [item for item in decisions if item['effective_status'] not in {'resolved', 'deferred_by_user'}]
    return {'schema': PROJECTION_SCHEMA, 'jobs': jobs, 'intents': intents,
        'active_decisions': active, 'notification_resolution': notifications,
        'next_actions': actions, 'conflicts': sorted(set(conflicts)),
        'external_mutations_performed': False, 'executor_available': False}


def summary(projection):
    labels = {'observe_running_controller': '让原固定控制器继续健康运行',
        'preserve_pending_decision': '保留已提问的待答决定，不重问或自动重试',
        'inspect_observation_conflict': '核对本地与API观察差异，不执行接续动作',
        'inspect_decision_evidence': '核对决定证据，不推定新授权',
        'verify_completion_evidence': '核对完成与归档证据，暂不发送完成邮件',
        'connector_send_candidate': '完成证据已绑定，邮件候选待原connector流程核对',
        'connector_verify_sent_items': '从原connector核对Sent Items，不重复发信',
        'inspect_failure_no_automatic_retry': '核对真实失败与既有预算，不自动重试',
        'preserve_user_hold': '保留用户暂缓', 'preserve_canceled_job': '保留用户取消',
        'completed_notified': '已完成并通知，保留全部收据',
        'preserve_waiting_previous': '保留前期等待与冻结节目',
        'reconcile_submission_without_repost': '核对提交不明状态，不重复POST',
        'preserve_frozen_wait': '保留已冻结意图，等待原流程检查', 'preserve_intent': '保留当前意图'}
    return '；'.join((item.get('job_id') or item.get('intent_date', '')) + '：' + labels[item['kind']]
        for item in projection['next_actions']) or '当前没有已领取的待跟进任务。'


def plan(state, observation):
    projection = derive(state, observation)
    binding = observation.get('binding', {})
    require(binding.get('generation') == state['generation'], 'observation_generation_mismatch')
    return {'schema': PLAN_SCHEMA, 'binding': copy.deepcopy(binding),
        'observation': copy.deepcopy(observation),
        'allowed_patch': {'projection': projection, 'next_action': summary(projection)},
        'proposed_external_actions': [], 'external_mutations_performed': False}


def _recheck_observation(root, observation, *, now):
    config, config_sha = load_config(root)
    binding = observation.get('binding', {})
    require(binding.get('root') == str(root) and binding.get('config_sha256') == config_sha,
        'observation_root_or_config_changed')
    age = (now - timestamp(observation.get('observed_at'))).total_seconds()
    require(-30 <= age <= 300, 'observation_stale_or_future')
    files = observation.get('files')
    require(isinstance(files, dict), 'missing_file_bindings')
    for path, expected in files.items():
        require(SHA.fullmatch(str(expected)) and read_file(root, path)[1] == expected,
            'observed_file_changed')
    return config


def _replay_observation(root, state, observation):
    """Recompute every claimed result from current files, without networking.

    Saved API snapshots are observations rather than an action capability. They
    must match each freshly read local job; valid/proof flags and the file set
    are independently reconstructed instead of trusted from offline JSON.
    """
    def saved_get(route):
        if route == '/api/liveness':
            live = observation.get('liveness', {})
            require(live.get('valid') is True, 'saved_liveness_unavailable')
            return live
        require(route.startswith('/api/jobs/'), 'unsupported_saved_route')
        return observation.get('jobs', {}).get(route.rsplit('/', 1)[1], {}).get('api')
    rebuilt = observe(root, state, observation['binding'], get=saved_get,
        now=timestamp(observation.get('observed_at')))
    require(rebuilt == observation, 'observation_not_reproducible_from_bound_files')


def apply_projection(root, tick_plan, *, now=None):
    """Double CAS plus independent recomputation; never executes planned work."""
    root = Path(root).resolve()
    require(tick_plan.get('schema') == PLAN_SCHEMA, 'unsupported_plan_schema')
    observation = tick_plan.get('observation', {})
    config = _recheck_observation(root, observation, now=now or utc_now())
    path = confined(root, config['state_path'])
    try:
        with InstanceLock(path.with_suffix('.lock')):
            state, sha = read_file(root, path)
            binding = tick_plan.get('binding', {})
            if state.get('generation') != binding.get('generation') or sha != binding.get('state_sha256'):
                return {'status': 'conflict', 'reason': 'queue_generation_or_sha_changed', 'changed': False}
            # Rebinding may read small JSON files but never calls HTTP under lock.
            _recheck_observation(root, observation, now=now or utc_now())
            _replay_observation(root, state, observation)
            fresh = plan(state, observation)
            require(fresh == tick_plan, 'plan_not_reproducible')
            patch = fresh['allowed_patch']
            require(set(patch) == {'projection', 'next_action'}, 'patch_outside_projection')
            if all(state.get(key) == value for key, value in patch.items()):
                return {'status': 'unchanged', 'changed': False, 'generation': state['generation']}
            updated = copy.deepcopy(state)
            updated.update(patch)
            updated['generation'] += 1
            # Everything except these derived fields and CAS generation is byte-value preserved.
            allowed = {'projection', 'next_action', 'generation'}
            require({k: v for k, v in state.items() if k not in allowed} ==
                {k: v for k, v in updated.items() if k not in allowed}, 'protected_queue_fields_changed')
            write_json(path, updated)
            return {'status': 'applied', 'changed': True, 'generation': updated['generation']}
    except BlockingIOError:
        return {'status': 'busy', 'changed': False}
