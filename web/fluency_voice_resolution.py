"""Keep existing, fully screened voices under a job's fluency-first policy.

This deterministic controller decision uses saved full-WAV evidence only. It
does not generate speech, approve pronunciation, or imply human listening.
Legacy jobs do not inherit a later default policy.
"""
from pathlib import Path

import media_recovery as media

POLICY = 'fluency_first_v1'
SCHEMA = 'fluency_first_existing_voice_policy_resolution_v1'
_HOLD_STATUSES = frozenset({'deferred_by_user', 'user_hold', 'held', 'hold',
    'paused', 'paused_by_user', 'stop_after_rejected', 'canceled', 'cancelled'})


def _not_held(value):
    """Only current decision nodes are relevant; history never grants authority."""
    media.require(isinstance(value, dict), 'Invalid current voice-policy state')
    media.require(value.get('status') not in _HOLD_STATUSES
        and value.get('automatic_resumption_allowed') is not False
        and not value.get('cancel_requested_at')
        and not value.get('cancel_requested'), 'Explicit hold/cancellation remains in force')
    for name in ('user_hold', 'pending_voice_decision', 'pending_editorial_decision',
                 'pending_final_voice_choice'):
        if value.get(name) is not None:
            _not_held(value[name])


def _policy_state(root, folder):
    from editorial_policy import assert_unheld, policy_state_for_folder
    # Episode children have no independent web job record. The common reader
    # validates their immutable parent/episode-plan/clip/ASR binding first.
    state = policy_state_for_folder(folder)
    media.require(isinstance(state, dict), 'Invalid job policy state')
    if (state.get('options') or {}).get('editorial_policy') != POLICY:
        return None
    # Use the same current pause and pending-decision guards as text review.
    # This runs again after the full PCM scan through _policy_state below.
    assert_unheld(folder)
    media.require(state.get('id') == folder.name, 'Voice policy belongs to another job')
    _not_held(state)
    parent = state.get('_editorial_parent_id')
    identities = {folder.name}
    if parent is not None:
        media.require(isinstance(parent, str) and parent != folder.name
            and isinstance(state.get('_editorial_binding'), dict), 'Invalid parent editorial authority')
        identities.add(parent)
        path = media.checked_record(root, state['_editorial_binding'])
    else:
        path = media.job_file(root, root / 'web/state/jobs' / (folder.name + '.json'))
    queue_path = media.job_file(root, root / 'web/state/tagesschau_daily_automation.json', False)
    guards = []
    if queue_path.exists():
        queue = media.read(queue_path)
        media.require(isinstance(queue, dict) and all(isinstance(queue.get(name, {}), dict)
            for name in ('watched', 'daily_intents', 'episodes')), 'Invalid current queue guards')
        for job_id in sorted(identities):
            watch = queue.get('watched', {}).get(job_id)
            if watch is not None:
                _not_held(watch)
                guards.append({'kind': 'watch', 'job_id': job_id, 'effective_hold': False})
        for day, intent in sorted(queue.get('daily_intents', {}).items()):
            if isinstance(intent, dict) and intent.get('job_id') in identities:
                _not_held(intent)
                guards.append({'kind': 'intent', 'date': day,
                    'job_id': intent['job_id'], 'effective_hold': False})
        for episode_key, episode in sorted(queue.get('episodes', {}).items()):
            if isinstance(episode, dict) and episode.get('job_id') in identities:
                _not_held(episode)
                guards.append({'kind': 'episode', 'episode_key': episode_key,
                    'job_id': episode['job_id'], 'effective_hold': False})
    return state, path, guards


def accept_quality(root, folder, plan_path, manifest_path, quality_path, checked):
    """Return a separate policy receipt, or None for jobs without this policy.

    Malformed/stale evidence and repair-level defects raise ValueError. Nothing
    in this function clears a rejection, consumes a recovery, or edits a WAV.
    """
    root, folder = Path(root).resolve(), Path(folder).absolute()
    media.require(folder.parent == root / 'jobs', 'Expected the current job directory')
    selected = _policy_state(root, folder)
    if selected is None:
        return None
    state, state_path, guards = selected
    import speech_content_quality as quality

    root, folder, plan, plan_path = media.plan_binding(root, folder, plan_path)
    source_sha = plan['source_binding']['source_video_sha256']
    media.require(media.SHA.fullmatch(str(state.get('source_sha256', '')))
        and state['source_sha256'] == source_sha, 'Job/source policy binding changed')
    manifest_path = media.job_file(folder, manifest_path)
    quality_path = media.job_file(folder, quality_path)
    manifest = quality.validated_manifest(root, folder, plan, plan_path, manifest_path)
    groups, native = plan['tts_groups'], manifest['utterances']
    binding = {'plan': media.record(folder, plan_path),
        'native_manifest': media.record(folder, manifest_path),
        'source_segments_sha256': plan['source_segments']['sha256']}
    media.require(isinstance(checked, dict) and checked == media.read(quality_path)
        and checked.get('schema') == 'website_audio_quality_v1'
        and checked.get('policy') == quality.POLICY
        and checked.get('helper_code_sha256') == media.sha(root / 'web/speech_content_quality.py')
        and checked.get('thresholds') == quality.THRESHOLDS
        and checked.get('binding') == binding and checked.get('complete') is True
        and checked.get('all_groups_target_free_checked') is True
        and type(checked.get('checked_utterances')) is int
        and type(checked.get('target_free_checked_utterances')) is int
        and checked['checked_utterances'] == checked.get('target_free_checked_utterances') == len(groups)
        and checked.get('human_listened') is False and checked.get('waveform_modified') is False
        and checked.get('voice_identity_approved') is False
        and checked.get('emotion_approved') is False, 'Incomplete or stale full voice-quality report')
    rows = checked.get('rows')
    media.require(isinstance(rows, list) and len(rows) == len(groups) == len(native)
        and [r.get('group_id') for r in rows] == [g['group_id'] for g in groups]
        and [r.get('segment_id') for r in native] == [g['group_id'] for g in groups],
        'Voice quality must cover all ordered current groups')
    pending = [row for row in rows if row.get('needs_review') is True]
    media.require(checked.get('needs_repair_ids') == checked.get('needs_repair_groups') == []
        and checked.get('flagged_groups') == []
        and checked.get('needs_review_ids') == [sid for row in pending for sid in row['member_ids']]
        and checked.get('needs_review_groups') == [row['group_id'] for row in pending]
        and checked.get('status') == ('needs_content_review' if pending else 'machine_content_screen_passed'),
        'Repair defects or inconsistent quality summary remain blocked')
    if not pending:
        return None
    normalize = quality.normalizer(root)
    audio_proofs, observation_proofs, adopted = [], [], []
    for group, entry, row in zip(groups, native, rows):
        media.require(type(row.get('needs_repair')) is bool
            and type(row.get('needs_review')) is bool and row['needs_repair'] is False
            and row.get('human_listened') is False and row.get('target_free_asr_used') is True
            and row.get('generation_provenance') == {'plan': binding['plan'],
                'native_manifest': binding['native_manifest']}, 'Voice row provenance changed')
        audio = quality.bound_audio(root, folder, plan, plan_path, manifest_path, group, entry)
        metrics = media.wav_metrics(audio['path'], entry)
        small_path = media.checked_record(folder, row.get('observation'))
        small = media.read(small_path)
        quality.validate_observation(small, audio, 'small', root, True)
        observations = {'small': media.record(folder, small_path)}
        medium = None
        if row.get('medium_observation') is not None:
            medium_path = media.checked_record(folder, row['medium_observation'])
            medium = media.read(medium_path)
            quality.validate_observation(medium, audio, 'medium', root, True)
            observations['medium'] = media.record(folder, medium_path)
        decision = quality.evaluate_group(group['text_zh'], audio, metrics, small, medium, normalize)
        faults, _ = quality.objective_findings(metrics, audio['duration_s'])
        media.require(not faults and row.get('group_id') == group['group_id']
            and row.get('member_ids') == group['member_ids']
            and row.get('audio_sha256') == entry['sha256'] == audio['sha256']
            and row.get('content_evaluation') == decision
            and row['needs_repair'] == decision['needs_repair'] is False
            and row['needs_review'] == decision['needs_review']
            and row.get('target_free_recognized') == decision['small']['recognized']
            and row.get('normalized_text_similarity') == decision['small']['similarity']
            and all(row.get(name) == value for name, value in metrics.items()),
            'Saved decision differs from current complete PCM/text/ASR evidence')
        audio_proofs.append({'group_id': group['group_id'], 'member_ids': group['member_ids'],
            'speaker_id': group['speaker_id'], 'source_ids': group['source_ids'],
            'text_zh_sha256': media.key(group['text_zh']), 'audio': audio})
        observation_proofs.append({'group_id': group['group_id'], **observations})
        if not row['needs_review']:
            continue
        media.require(medium is not None and decision.get('status') == 'needs_review'
            and decision.get('reasons') == ['recognizer_uncertainty_without_sufficient_repair_evidence'],
            'Only fully screened pronunciation/recognizer uncertainty is delegable')
        adopted.append({'group_id': group['group_id'], 'member_ids': group['member_ids'],
            'audio_sha256': audio['sha256'], 'original_reasons': decision['reasons'],
            'original_advisories': decision['advisories'],
            'decision': 'keep_existing_complete_voice_accept_pronunciation_or_asr_uncertainty'})
    # Re-read mutable authorities after scanning; no stale hold/cancel or content
    # can be accepted just because it changed during a long PCM verification.
    media.require(_policy_state(root, folder) == selected, 'Policy/hold state changed during verification')
    media.require(media.record(folder, plan_path) == binding['plan']
        and media.record(folder, manifest_path) == binding['native_manifest']
        and media.sha(folder / 'segments.json') == binding['source_segments_sha256']
        and media.read(quality_path) == checked, 'Voice evidence changed during verification')
    report = {'schema': SCHEMA, 'policy': POLICY, 'job_id': folder.name,
        'authority': 'current_job_explicit_editorial_policy',
        # Mutable progress/timestamps/notifications are deliberately excluded
        # from the resolution/cache identity; this is a policy-scope proof.
        'policy_authority': {'path': str(state_path.relative_to(root)),
            'job_id': state['id'], 'editorial_policy': POLICY, 'source_sha256': source_sha,
            **({'parent_job_id': state['_editorial_parent_id'],
                'episode_binding': state['_editorial_binding']} if state.get('_editorial_parent_id') else {})},
        'current_queue_guards': guards,
        'source_sha256': source_sha, 'binding': binding,
        'quality': media.record(folder, quality_path), 'complete_audio': audio_proofs,
        'saved_observations': observation_proofs, 'checked_utterances': len(groups),
        'controller_accepted_risk_ids': checked['needs_review_ids'], 'adopted': adopted,
        'machine_status_preserved': checked['status'], 'machine_findings_rewritten': False,
        'machine_approved': False, 'human_listened': False, 'whole_video_human_reviewed': False,
        'subjective_fluency_verified': False,
        'voice_identity_approved': False, 'emotion_approved': False, 'pronunciation_approved': False,
        'waveform_modified': False, 'semantic_rewrite_performed': False,
        'new_model_calls': 0, 'new_media_actions': 0, 'new_generation_authorized': False,
        'reader_sha256': media.sha(Path(__file__)),
        'decision': 'controller_keeps_existing_voices_under_fluency_first_policy'}
    output = folder / 'tts/policy_resolution' / POLICY / (media.key(report) + '.json')
    if output.exists():
        media.require(media.read(media.job_file(folder, output)) == report, 'Immutable policy resolution changed')
    else:
        media.save(media.job_file(folder, output, exists=False), report)
    return report
