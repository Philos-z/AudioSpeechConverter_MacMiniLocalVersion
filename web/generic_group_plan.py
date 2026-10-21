"""Plan complete, job-local semantic groups before TTS; stdlib only.

Only an explicit approved machine suggestion may join adjacent source rows.
Invalid/conflicting advice falls back to singleton rows, never missing speech.
Long individual source sentences remain intact and block automatic synthesis.
"""
import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import re

SCHEMA = 'pretts_semantic_group_plan_v1'
VERSION = 'generic_pretts_semantic_groups_v1'
POLICY = {'max_source_span_s': 15., 'max_source_gap_s': 1.5,
          'max_normalized_characters': 80, 'cross_speaker_grouping': False,
          'automatic_topic_merging': False, 'cross_quote_speaker_grouping': False,
          'proportional_subtitle_fallback': False}
DEFAULT_PROFILE = 'hybrid-gpt-preserve-25-pad-eager'
SHARED_CACHE_NAMESPACE = 'native_mlx_semantic_groups_v2'
CACHE_REUSE_POLICY = {
    'schema': 'semantic_group_shared_content_cache_v2',
    'physical_namespace': SHARED_CACHE_NAMESPACE,
    'scope': 'project_shared_content_addressed',
    'audio_reuse': 'mlx_tts.content_payload fingerprint only; exact content, audio SHA, PCM and profile guards verified on every hit',
    'audio_identity_fields': ['text_zh', 'effective_tts_text', 'reference_sha256', 'model_identity', 'parameters'],
    'speaker_reuse': 'mlx_tts.speaker_payload fingerprint only; exact reference/model/preprocessing identity, NPZ SHA and internal source/revision verified',
    'plan_scope_is_cache_namespace': False,
    'source_and_permission_binding': 'immutable job-local plan and child inputs; never inferred from a shared cache hit',
    'profile_or_model_change_reuses_audio': False,
    'old_per_plan_v1_cache_imported': False,
    'old_cache_mutated_or_removed': False,
    'cache_files_copied_without_validation': False,
}


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.partial')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    temporary.replace(path)


def normalized(text):
    expanded = re.sub(r'<([^<>|]+)\|[^<>]*>', lambda match: match[1], text)
    if '<' in expanded or '>' in expanded:
        raise ValueError('Malformed pronunciation annotation')
    return ''.join(character for character in expanded if character.isalnum())


def confined(root, path):
    value = (root / path).resolve()
    if not value.is_relative_to(root):
        raise ValueError('Job reference or data path escapes project root')
    return value


def suggestion_receipt(folder, job, source_sha, speakers_sha, path=None):
    default = (folder / 'translation/report.json').resolve()
    path = Path(path).resolve() if path else default
    if not path.exists():
        if path != default:
            raise ValueError('Explicit semantic suggestion file is missing')
        return [], None
    document = read(path)
    if not isinstance(document, dict):
        raise ValueError('Semantic suggestions must be an object')
    if path != default and (document.get('job') != job or
                            document.get('source_segments_sha256') != source_sha or
                            document.get('source_speakers_sha256') != speakers_sha):
        raise ValueError('External semantic advice requires exact job/source SHA binding')
    for field, expected in [('job', job), ('source_segments_sha256', source_sha),
                            ('source_speakers_sha256', speakers_sha)]:
        if field in document and document[field] != expected:
            raise ValueError('Semantic advice belongs to another job or source')
    proposals = document.get('tts_join_suggestions', [])
    if not isinstance(proposals, list):
        raise ValueError('tts_join_suggestions must be a list')
    usable = (document.get('job') == job and document.get('source_segments_sha256') == source_sha
        and document.get('source_speakers_sha256') == speakers_sha
        and document.get('review_status') in ('machine_second_pass_approved',
                                              'machine_second_pass_partial_continuity_review'))
    return proposals, {'path': str(path), 'sha256': sha(path),
                       'proposals_key': 'tts_join_suggestions',
                       'job': job, 'source_segments_sha256': source_sha,
                       'source_speakers_sha256': speakers_sha,
                       'advice_usable': usable, 'review_status': document.get('review_status'),
                       'context_binding': 'same-job translation report' if path == default
                                          else 'explicit external job/source SHA'}


def validate_advice(proposal, rows, indices, refs):
    """Return explicit source positions; refuse uncertain semantic boundaries."""
    if not isinstance(proposal, dict):
        raise ValueError('proposal_is_not_an_object')
    ids = proposal.get('member_ids')
    if not isinstance(ids, list) or len(ids) < 2 or not all(isinstance(i, str) for i in ids):
        raise ValueError('at_least_two_explicit_member_ids_required')
    if len(set(ids)) != len(ids) or any(sid not in indices for sid in ids):
        raise ValueError('unknown_or_duplicate_member_id')
    positions = [indices[sid] for sid in ids]
    if positions != list(range(positions[0], positions[0] + len(ids))):
        raise ValueError('members_not_source_adjacent_and_ordered')
    members = [rows[i] for i in positions]
    speaker = members[0]['speaker_id']
    if any(row['speaker_id'] != speaker for row in members):
        raise ValueError('cross_speaker_join_forbidden')
    if proposal.get('speaker_id', speaker) != speaker or proposal.get('reference_sha256', refs[speaker]['sha256']) != refs[speaker]['sha256']:
        raise ValueError('proposal_speaker_or_reference_binding_mismatch')
    if proposal.get('approved') is not True or proposal.get('same_topic') is not True or \
       proposal.get('topic_boundary_crossed') is not False or proposal.get('quote_speaker_change') is not False:
        raise ValueError('explicit_approved_same_topic_no_quote_change_evidence_required')
    if not isinstance(proposal.get('reason'), str) or not proposal['reason'].strip():
        raise ValueError('semantic_reason_missing')
    if any(row.get('overlap', False) for row in members):
        raise ValueError('overlapping_speech_requires_review')
    if any(row.get('topic_boundary_before') or row.get('quote_speaker_change') for row in members[1:]):
        raise ValueError('known_topic_or_quote_boundary_requires_review')
    for field in ('topic_id', 'quote_speaker_id'):
        values = {row[field] for row in members if row.get(field) is not None}
        if len(values) > 1:
            raise ValueError('known_' + field + '_change_forbidden')
    gaps = [b['start_s'] - a['end_s'] for a, b in zip(members, members[1:])]
    if any(gap < 0 or gap > POLICY['max_source_gap_s'] for gap in gaps):
        raise ValueError('source_gap_or_overlap_outside_policy')
    if members[-1]['end_s'] - members[0]['start_s'] > POLICY['max_source_span_s']:
        raise ValueError('source_span_exceeds_15s')
    if sum(len(normalized(row['text_zh'])) for row in members) > POLICY['max_normalized_characters']:
        raise ValueError('group_text_exceeds_80_normalized_characters')
    return positions


def build_plan(root, job, suggestions_path=None, profile=DEFAULT_PROFILE, profile_options=None):
    root = Path(root).resolve()
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', job):
        raise ValueError('Invalid parent job ID')
    if not isinstance(profile, str) or not profile.strip():
        raise ValueError('Explicit nonempty TTS profile required')
    profile_options = copy.deepcopy(profile_options or {})
    folder = confined(root, Path('jobs') / job)
    source_path, speakers_path = folder / 'segments.json', folder / 'speakers.json'
    document, speakers, source = read(source_path), read(speakers_path), read(folder / 'source.json')
    rows = document['segments']
    if not isinstance(rows, list) or not rows:
        raise ValueError('Nonempty machine-reviewed source rows required')
    source_sha, ids, refs, asr_ids = sha(source_path), [], {}, []
    duration = source['duration_s']
    if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        raise ValueError('Invalid source duration')
    permission = read(folder / 'processing_request.json')
    if permission.get('processing_authorized') is not True or permission.get('scope') != 'private_local_evaluation':
        raise ValueError('Existing scoped local-processing authorization required')
    video = Path(source['input']).resolve()
    video_sha = sha(video)
    for row in rows:
        sid, speaker = row['segment_id'], row['speaker_id']
        if not isinstance(sid, str) or not sid or sid in ids:
            raise ValueError('Unknown/duplicate source-row ID')
        ids.append(sid)
        if row.get('translation_reviewed') is not True or row.get('speaker_reviewed') is not True:
            raise ValueError('Unreviewed source row cannot enter TTS plan: ' + sid)
        if not all(isinstance(row.get(field), str) and row[field].strip() for field in ('text_zh', 'text_de')):
            raise ValueError('Empty source or translated text: ' + sid)
        a, b = row['start_s'], row['end_s']
        if not all(isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t) for t in (a, b)) or not 0 <= a < b <= duration + .05:
            raise ValueError('Invalid original source times: ' + sid)
        if len(ids) > 1 and a < rows[len(ids) - 2]['start_s']:
            raise ValueError('Original source rows are not chronological')
        originals = row.get('source_ids')
        if not isinstance(originals, list) or not originals or not all(isinstance(i, str) and i for i in originals):
            raise ValueError('Original ASR source IDs required: ' + sid)
        asr_ids.extend(originals)
        effective = row.get('tts_pronunciation_text') or row['text_zh']
        if not isinstance(effective, str) or normalized(effective) != normalized(row['text_zh']):
            raise ValueError('Pronunciation annotation changes source display text: ' + sid)
        if speaker not in refs:
            info = speakers[speaker]
            if info.get('reviewed') is not True:
                raise ValueError('Unreviewed job-local speaker reference')
            path = confined(root, Path(info['reference']))
            if not path.is_file() or path.suffix.lower() == '.npz':
                raise ValueError('Original job-local reference audio required')
            refs[speaker] = {'path': str(path), 'sha256': sha(path),
                'speaker_scope': {'job': job, 'speaker_id': speaker,
                    'source_segments_sha256': source_sha, 'source_video_sha256': video_sha},
                'applies_to_other_jobs_or_speakers': False}
    if len(asr_ids) != len(set(asr_ids)):
        raise ValueError('Original ASR source coverage contains duplicates')
    proposals, receipt = suggestion_receipt(folder, job, source_sha, sha(speakers_path), suggestions_path)
    indices = {sid: i for i, sid in enumerate(ids)}
    candidates, rejected = {}, []
    for index, proposal in enumerate(proposals):
        try:
            if not receipt['advice_usable']:
                raise ValueError('semantic_advice_missing_exact_job_source_reference_binding_or_review_status')
            if isinstance(proposal, dict):
                if proposal.get('job', job) != job or proposal.get('source_segments_sha256', source_sha) != source_sha:
                    raise ValueError('proposal_job_or_source_binding_mismatch')
            positions = validate_advice(proposal, rows, indices, refs)
            candidates[index] = positions
        except ValueError as error:
            rejected.append({'proposal_index': index, 'reason': str(error), 'proposal': copy.deepcopy(proposal)})
    owners = {}
    for index, positions in candidates.items():
        for position in positions:
            owners.setdefault(position, []).append(index)
    conflicts = {i for owners_at_row in owners.values() if len(owners_at_row) > 1 for i in owners_at_row}
    for index in sorted(conflicts):
        rejected.append({'proposal_index': index, 'reason': 'overlapping_semantic_proposals_ambiguous',
                         'proposal': copy.deepcopy(proposals[index])})
        del candidates[index]
    starts = {positions[0]: (index, positions) for index, positions in candidates.items()}
    groups, policy_issues, position = [], [], 0
    while position < len(rows):
        index, positions = starts.get(position, (None, [position]))
        members = [rows[i] for i in positions]
        text, effective = ''.join(r['text_zh'] for r in members), ''.join(r.get('tts_pronunciation_text') or r['text_zh'] for r in members)
        member_ids = [r['segment_id'] for r in members]
        speaker = members[0]['speaker_id']
        span = members[-1]['end_s'] - members[0]['start_s']
        group_id = 'ttsgrp_' + f'{len(groups) + 1:04d}_' + fingerprint({'job': job,
            'source_sha': source_sha, 'members': member_ids, 'text_zh': text,
            'effective_tts_text': effective, 'reference_sha256': refs[speaker]['sha256']})[:12]
        evidence = {'kind': 'explicit_scoped_adjacent_same_speaker' if index is not None else 'single_source_sentence_no_join',
            'boundary_review_method': 'proxy_second_pass_semantic_join' if index is not None else 'no_join_requested',
            'same_topic': True, 'topic_boundary_crossed': False, 'quote_speaker_change': False,
            'reason': proposals[index]['reason'].strip() if index is not None else 'Preserve one complete original source row',
            'proposal_index': index, 'suggestion_receipt': copy.deepcopy(receipt) if index is not None else None,
            'source_gaps_s': [b['start_s'] - a['end_s'] for a, b in zip(members, members[1:])],
            **{k: POLICY[k] for k in ('max_source_span_s', 'max_source_gap_s', 'max_normalized_characters')}}
        offset, member_receipts = 0, []
        for row in members:
            count = len(normalized(row['text_zh']))
            member_receipts.append({'segment_id': row['segment_id'], 'speaker_id': speaker,
                'text_zh': row['text_zh'], 'text_de': row['text_de'], 'source_ids': copy.deepcopy(row['source_ids']),
                'tts_pronunciation_text': row.get('tts_pronunciation_text'),
                'source_start_s': row['start_s'], 'source_end_s': row['end_s'],
                'character_start': offset, 'character_end': offset + count})
            offset += count
        tts_row = {'segment_id': group_id, 'speaker_id': speaker, 'start_s': members[0]['start_s'],
            'end_s': members[-1]['end_s'], 'text_zh': text, 'text_de': ' '.join(r['text_de'] for r in members),
            'tts_pronunciation_text': effective, 'member_ids': member_ids,
            'source_ids': [sid for r in members for sid in r['source_ids']], 'overlap': any(r.get('overlap', False) for r in members),
            'translation_reviewed': True, 'speaker_reviewed': True, 'human_reviewed': False,
            'translation_review_method': 'unchanged reviewed rows; explicit semantic joins only'}
        issues = []
        if span > POLICY['max_source_span_s']:issues.append('single_source_row_exceeds_15s')
        if offset > POLICY['max_normalized_characters']:issues.append('single_source_row_exceeds_80_normalized_characters')
        if tts_row['overlap']:issues.append('overlapping_source_row_requires_review')
        if issues:policy_issues.append({'group_id': group_id, 'member_ids': member_ids, 'issues': issues})
        groups.append({'group_id': group_id, 'speaker_id': speaker, 'member_ids': member_ids,
            'source_ids': tts_row['source_ids'], 'source_start_s': tts_row['start_s'], 'source_end_s': tts_row['end_s'],
            'source_span_s': span, 'text_zh': text, 'text_de': tts_row['text_de'],
            'tts_pronunciation_text': effective, 'normalized_characters': offset, 'reference': copy.deepcopy(refs[speaker]),
            'join_evidence': evidence, 'members': member_receipts, 'tts_row': tts_row,
            'subtitle_alignment_method': 'single_member_full_audio_duration_v1' if len(members) == 1 else 'requires_real_acoustic_boundary_alignment',
            'ready_for_tts': not issues})
        position = positions[-1] + 1
    actual = [sid for group in groups for sid in group['member_ids']]
    if actual != ids:
        raise AssertionError('Planner failed exact ordered source coverage')
    scope = fingerprint({'planner_version': VERSION, 'planner_code_sha256': sha(__file__),
        'source_segments_sha256': source_sha, 'source_video_sha256': video_sha, 'job': job,
        'speakers_sha256': sha(speakers_path), 'references': refs, 'policy': POLICY,
        'groups': [g['tts_row'] for g in groups], 'profile': profile, 'profile_options': profile_options,
        'suggestion_receipt': receipt})
    child_id = 'web_ttsgroups_' + scope[:24]
    return {'schema': SCHEMA, 'planner_version': VERSION, 'job': job, 'root': str(root),
        'source_segments': {'path': str(source_path), 'sha256': source_sha},
        'source_speakers': {'path': str(speakers_path), 'sha256': sha(speakers_path)},
        'source_binding': {'source_receipt_path': str(folder / 'source.json'), 'source_receipt_sha256': sha(folder / 'source.json'),
            'source_video_path': str(video), 'source_video_sha256': video_sha, 'source_video_bytes': video.stat().st_size,
            'source_duration_s': duration, 'source_audio_track': source['audio_track']},
        'source_document_metadata': {k: v for k, v in document.items() if k != 'segments'},
        'source_rows': copy.deepcopy(rows), 'tts_groups': groups, 'selected_member_ids': ids,
        'unselected_member_ids': [], 'full_tts_group_plan_complete': True, 'partial_preview': False,
        'policy': {**POLICY, 'group_before_tts': True}, 'semantic_suggestion_receipt': receipt,
        'rejected_suggestions': sorted(rejected, key=lambda r: r['proposal_index']), 'policy_issues': policy_issues,
        'ready_for_tts': not policy_issues, 'human_reviewed': False,
        'coverage': {'original_source_rows': len(rows), 'planned_tts_groups': len(groups),
            'selected_source_rows': len(rows), 'all_source_rows_preserved': True,
            'every_source_row_exactly_once_in_original_order': True, 'original_asr_source_ids': len(asr_ids)},
        'character_index_policy': 'Half-open Unicode alphanumeric character offsets after pronunciation-label expansion and punctuation removal',
        'tts_job': {'id': child_id, 'job': child_id, 'directory': str(root / 'jobs' / child_id), 'parent_job': job,
            # The scope binds immutable source/permission provenance, not the
            # physical audio/NPZ directory. The runner's own verified content
            # keys invalidate only the utterances whose synthesis inputs change.
            'cache_scope_key': scope, 'plan_provenance_key': scope,
            'cache_dir': str(root / 'cache' / SHARED_CACHE_NAMESPACE),
            'cache_reuse_policy': copy.deepcopy(CACHE_REUSE_POLICY),
            'cache_dir_argument_required': True, 'profile': profile, 'profile_options': profile_options,
            'processing_permission_path': str(folder / 'processing_request.json'),
            'processing_permission_sha256': sha(folder / 'processing_request.json'),
            'original_job_segments_modified': False, 'materialization': 'explicit_create_tts_job_call'}}


def materialize_tts_job(plan, plan_path):
    """Explicit call only; preserve original job and do not load any model."""
    if read(plan_path) != plan:
        raise ValueError('In-memory group plan differs from the actual saved plan')
    if not plan['ready_for_tts']:
        raise ValueError('Unresolved source-row policy issues; no child TTS job created')
    receipt = plan.get('semantic_suggestion_receipt')
    rebuilt = build_plan(plan['root'], plan['job'], receipt['path'] if receipt else None,
        plan['tts_job']['profile'], plan['tts_job']['profile_options'])
    if rebuilt != plan:
        raise ValueError('Derived group text/coverage/reference/profile/cache scope differs from valid source reconstruction')
    root, child = Path(plan['root']), Path(plan['tts_job']['directory'])
    if child.resolve() != (root / 'jobs' / plan['tts_job']['id']).resolve():
        raise ValueError('Child TTS job path mismatch')
    checks = [(plan['source_segments']['path'], plan['source_segments']['sha256']),
        (plan['source_speakers']['path'], plan['source_speakers']['sha256']),
        (plan['source_binding']['source_receipt_path'], plan['source_binding']['source_receipt_sha256']),
        (plan['source_binding']['source_video_path'], plan['source_binding']['source_video_sha256']),
        (plan['tts_job']['processing_permission_path'], plan['tts_job']['processing_permission_sha256'])]
    for path, expected in checks:
        if sha(path) != expected:raise ValueError('Parent source/permission changed since grouping')
    if read(plan['source_segments']['path'])['segments'] != plan['source_rows']:
        raise ValueError('Parent source rows differ from planned source objects')
    for group in plan['tts_groups']:
        if sha(group['reference']['path']) != group['reference']['sha256']:
            raise ValueError('Job-local original reference changed')
    speakers = read(plan['source_speakers']['path'])
    used = {g['speaker_id'] for g in plan['tts_groups']}
    permission = read(plan['tts_job']['processing_permission_path'])
    permission.update(parent_job=plan['job'], child_role='semantic_groups_tts_only',
        group_plan_path=str(Path(plan_path).resolve()), group_plan_sha256=sha(plan_path))
    binding = {'schema': 'generic_group_tts_child_binding_v1', 'parent_job': plan['job'],
        'group_plan_path': str(Path(plan_path).resolve()), 'group_plan_sha256': sha(plan_path),
        'cache_scope_key': plan['tts_job']['cache_scope_key'], 'profile': plan['tts_job']['profile'],
        'profile_options': plan['tts_job']['profile_options'], 'cache_dir': plan['tts_job']['cache_dir'],
        'plan_provenance_key': plan['tts_job']['plan_provenance_key'],
        'cache_reuse_policy': copy.deepcopy(plan['tts_job']['cache_reuse_policy'])}
    outputs = {'segments.json': {'schema_version': 2, 'segments': [g['tts_row'] for g in plan['tts_groups']],
                  'parent_job': plan['job'], 'human_reviewed': False},
        'speakers.json': {sid: speakers[sid] for sid in speakers if sid in used},
        'source.json': read(plan['source_binding']['source_receipt_path']),
        'processing_request.json': permission, 'group_plan_binding.json': binding}
    # Existing immutable child inputs must agree before writing anything. TTS
    # outputs and caches may already be present; never reset or remove them.
    for name, value in outputs.items():
        if (child / name).exists() and read(child / name) != value:
            raise ValueError('Existing child TTS job inputs differ; refuse overwrite')
    child.mkdir(parents=True, exist_ok=True)
    for name, value in outputs.items():
        if not (child / name).exists():save(child / name, value)
    return {'job': plan['tts_job']['id'], 'directory': str(child), 'files': list(outputs),
            'cache_dir': plan['tts_job']['cache_dir'], 'model_loaded': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--job', required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--suggestions', type=Path)
    parser.add_argument('--profile', default=DEFAULT_PROFILE)
    parser.add_argument('--profile-options-json', default='{}')
    parser.add_argument('--create-tts-job', action='store_true')
    args = parser.parse_args()
    options = json.loads(args.profile_options_json)
    if not isinstance(options, dict):raise ValueError('Profile options must be an object')
    plan = build_plan(args.root, args.job, args.suggestions, args.profile, options)
    save(args.out, plan)
    child = materialize_tts_job(plan, args.out) if args.create_tts_job else None
    print(json.dumps({'coverage': plan['coverage'], 'ready_for_tts': plan['ready_for_tts'],
        'rejected_suggestions': len(plan['rejected_suggestions']), 'tts_child': child}, ensure_ascii=False))


if __name__ == '__main__':
    main()
