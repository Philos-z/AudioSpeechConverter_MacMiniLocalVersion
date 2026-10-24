"""Bounded website media recovery. Never cuts audio, weakens alignment or edits code.

All mutations are job-local, input-bound and journalled. Audio quality is a
machine anomaly screen, never an approval of pronunciation, voice or listening.
"""
import argparse
from contextlib import contextmanager
import copy
import difflib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import wave

JOURNAL_SCHEMA = 'bounded_media_recovery_journal_v1'
MAX_RECOVERIES = 6
SHA = re.compile(r'^[a-f0-9]{64}$')
MUTABLE_TRANSLATION = frozenset({
    'text_zh', 'tts_pronunciation_text', 'translation_reviewed', 'translation_model',
    'translation_review_method', 'translation_status', 'translation_provenance',
    'translation_error', 'translation_context', 'translation_review',
    'translation_review_metadata', 'join_previous', 'join_reason', 'topic',
    'continuity_suggestion'})


def require(value, message):
    if not value:
        raise ValueError('MEDIA_RECOVERY: ' + message)


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def key(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def job_file(folder, value, exists=True):
    """Reject symlinks, traversal and sibling-job evidence before resolving."""
    folder = Path(folder).absolute()
    require(not folder.is_symlink() and not folder.parent.is_symlink(), 'Symlink job path')
    require(isinstance(value, (str, Path)), 'Missing job-local path')
    value = Path(value)
    require('..' not in value.parts, 'Path traversal')
    path = value if value.is_absolute() else folder / value
    require(path.is_relative_to(folder), 'Evidence escapes the current job')
    current = folder
    for part in path.relative_to(folder).parts:
        current = current / part
        require(not current.is_symlink(), 'Symlink policy/evidence path')
    if exists:
        require(path.is_file(), 'Missing job-local evidence: ' + str(path.relative_to(folder)))
    return path


def scope(root, folder):
    root = Path(root).resolve()
    folder = Path(folder).absolute()
    require(folder.parent == root / 'jobs' and re.fullmatch(r'[A-Za-z0-9_-]{1,120}', folder.name),
        'Expected one exact project job directory')
    job_file(folder, 'segments.json')
    return root, folder


def record(folder, path):
    path = job_file(folder, path)
    return {'path': str(path.relative_to(folder)), 'sha256': sha(path)}


def checked_record(folder, proof):
    require(isinstance(proof, dict) and SHA.fullmatch(str(proof.get('sha256', ''))), 'Invalid evidence SHA')
    path = job_file(folder, proof.get('path'))
    require(sha(path) == proof['sha256'], 'Bound evidence SHA changed')
    return path


@contextmanager
def journal_lock(folder):
    path = job_file(folder, 'tts/media_recovery_journal.lock', exists=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'a+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def journal(folder):
    path = job_file(folder, 'tts/media_recovery_journal.json', exists=False)
    if not path.exists():
        return {'schema': JOURNAL_SCHEMA, 'job': folder.name, 'max_recoveries': MAX_RECOVERIES, 'entries': []}
    value = read(path)
    require(value.get('schema') == JOURNAL_SCHEMA and value.get('job') == folder.name
        and value.get('max_recoveries') == MAX_RECOVERIES and isinstance(value.get('entries'), list),
        'Malformed lifetime media recovery journal; never reset it on retry')
    require(len(value['entries']) <= MAX_RECOVERIES and len({e['token'] for e in value['entries']}) == len(value['entries']),
        'Invalid lifetime recovery count')
    return value


def reserve_recovery(folder, action, binding):
    """Reserve before a recovery mutation. Identical clicks cannot reset the budget.

    A pending identical reservation can be resumed after a process interruption.
    The caller must keep its normal per-job worker lock while doing the action.
    """
    folder = Path(folder).absolute()
    require(action in {'alignment_split', 'source_rebind', 'picture_holds', 'audio_text_repair',
        'audio_regenerate', 'encoded_master_repair'}, 'Unknown recovery action')
    require(isinstance(binding, dict) and bool(binding), 'Exact nonempty recovery input binding required')
    token = key({'action': action, 'binding': binding})
    with journal_lock(folder):
        value = journal(folder)
        for entry in value['entries']:
            if entry['token'] == token:
                require(entry['action'] == action and entry['binding'] == binding, 'Recovery token collision')
                return {**copy.deepcopy(entry), 'reused': True}
        require(len(value['entries']) < MAX_RECOVERIES, 'Lifetime media recovery budget exhausted (6); review required')
        entry = {'token': token, 'action': action, 'binding': copy.deepcopy(binding), 'status': 'pending',
            'created_at_unix': time.time()}
        value['entries'].append(entry)
        save(job_file(folder, 'tts/media_recovery_journal.json', exists=False), value)
        return {**entry, 'reused': False}


def finish_recovery(folder, token, result):
    folder = Path(folder).absolute()
    require(isinstance(result, dict), 'Recovery result must be an object')
    with journal_lock(folder):
        value = journal(folder)
        entry = next((e for e in value['entries'] if e['token'] == token), None)
        require(entry is not None, 'Recovery was not reserved')
        if entry['status'] == 'complete':
            require(entry['result'] == result, 'Completed recovery result cannot be rewritten')
            return result
        require(entry['status'] == 'pending', 'Unknown recovery reservation status')
        entry.update(status='complete', result=copy.deepcopy(result), finished_at_unix=time.time())
        save(job_file(folder, 'tts/media_recovery_journal.json', exists=False), value)
    return result


def reused_result(folder, reservation):
    if reservation['status'] != 'complete':
        return None
    result = copy.deepcopy(reservation['result'])
    for proof in result.get('outputs', []):
        checked_record(folder, proof)
    return {**result, 'changed': False, 'reused': True}


def plan_binding(root, folder, plan_path, current=True):
    root, folder = scope(root, folder)
    path = job_file(folder, plan_path)
    plan = read(path)
    require(plan.get('schema') == 'pretts_semantic_group_plan_v1' and plan.get('job') == folder.name,
        'Wrong source plan job/schema')
    source_path = job_file(folder, plan['source_segments']['path'])
    require(source_path == folder / 'segments.json', 'Plan uses a different source-row file')
    if current:
        require(sha(source_path) == plan['source_segments']['sha256']
            and read(source_path)['segments'] == plan['source_rows'], 'Source plan is stale')
    speakers = job_file(folder, plan['source_speakers']['path'])
    require(speakers == folder / 'speakers.json' and sha(speakers) == plan['source_speakers']['sha256'],
        'Speaker identity changed')
    binding = plan['source_binding']
    receipt = job_file(folder, binding['source_receipt_path'])
    require(receipt == folder / 'source.json' and sha(receipt) == binding['source_receipt_sha256'], 'Source receipt changed')
    source = read(receipt)
    video = Path(binding['source_video_path']).resolve()
    require(Path(source['input']).resolve() == video and video.is_file()
        and video.stat().st_size == binding['source_video_bytes'] and sha(video) == binding['source_video_sha256'],
        'Original video identity changed')
    rows = plan['source_rows']
    ids = [r['segment_id'] for r in rows]
    require(ids and len(ids) == len(set(ids)) and [sid for g in plan['tts_groups'] for sid in g['member_ids']] == ids,
        'Plan must cover every ordered source row exactly once')
    require(len({g['group_id'] for g in plan['tts_groups']}) == len(plan['tts_groups']), 'Duplicate TTS groups')
    return root, folder, plan, path


def archive(folder, path, label):
    path = job_file(folder, path)
    destination = job_file(folder, 'tts/media_recovery/history/' + label + '_' + sha(path) + '.json', exists=False)
    if destination.exists():
        require(sha(destination) == sha(path), 'Recovery history changed')
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(path.read_bytes())
        destination.chmod(0o600)
    return destination


def rebind_no_join(root, folder, old_plan_path):
    """Rebind exclusions, archive old picture permissions after Chinese-only edits."""
    root, folder = scope(root, folder)
    policy_path = job_file(folder, 'tts/no_join_recovery.json', exists=False)
    picture_path = job_file(folder, 'tts/picture_holds.json', exists=False)
    if not policy_path.exists() and not picture_path.exists():
        return {'changed': False, 'reason': 'no_media_policies'}
    new_sha = sha(folder / 'segments.json')
    stale_no_join = policy_path.exists() and read(policy_path).get('source_segments_sha256') != new_sha
    stale_picture = picture_path.exists() and read(picture_path).get('source_segments_sha256') != new_sha
    if not stale_no_join and not stale_picture:
        return {'changed': False, 'reason': 'already_current'}
    root, folder, old, old_path = plan_binding(root, folder, old_plan_path, current=False)
    current = read(folder / 'segments.json')['segments']
    def identity(row):
        return {k: v for k, v in row.items() if k not in MUTABLE_TRANSLATION}
    require(len(current) == len(old['source_rows']) and all(identity(a) == identity(b)
        for a, b in zip(current, old['source_rows'])), 'Source identity changed; cannot rebind media policies')
    policy = read(policy_path) if stale_no_join else None
    if policy:
        require(policy.get('schema') == 'scoped_no_join_recovery_v1' and policy.get('job') == folder.name
            and policy.get('source_segments_sha256') == old['source_segments']['sha256']
            and policy.get('source_speakers_sha256') == old['source_speakers']['sha256'], 'Old no-join policy/plan identity differs')
        require(policy.get('groups') and policy.get('evidence'), 'No-join policy lacks explicit evidence')
        for proof in policy['evidence']:
            checked_record(folder, proof)
    if stale_picture:
        picture = read(picture_path)
        require(picture.get('schema') == 'scoped_group_picture_holds_v1' and picture.get('job') == folder.name
            and picture.get('source_segments_sha256') == old['source_segments']['sha256']
            and picture.get('source_video_sha256') == old['source_binding']['source_video_sha256'],
            'Old picture policy/source identity differs')
    reservation = reserve_recovery(folder, 'source_rebind', {'old_plan_sha256': sha(old_path),
        'old_source_sha256': old['source_segments']['sha256'], 'source_segments_sha256': new_sha,
        'source_speakers_sha256': old['source_speakers']['sha256']})
    cached = reused_result(folder, reservation)
    if cached:
        return cached
    outputs = []
    archived_picture = None
    if policy:
        backup = archive(folder, policy_path, 'no_join_before_rebind')
        policy['source_segments_sha256'] = new_sha
        policy.setdefault('source_rebindings', []).append({'previous_source_segments_sha256': old['source_segments']['sha256'],
            'new_source_segments_sha256': new_sha, 'old_plan': record(folder, old_path),
            'source_identity_preserved': True, 'previous_policy': record(folder, backup)})
        save(policy_path, policy)
        outputs.extend([record(folder, policy_path), record(folder, backup)])
    if stale_picture:
        archived_picture = record(folder, archive(folder, picture_path, 'picture_holds_before_source_change'))
        outputs.append(archived_picture)
        picture_path.unlink()  # Only an obsolete permission; source/audio/frame evidence remains.
    return finish_recovery(folder, reservation['token'], {'changed': True, 'source_identity_preserved': True,
        'archived_picture_policy': archived_picture, 'outputs': outputs})


def alignment_failure_scope(root, folder, plan_path, index_path, expected_manifest_path=None):
    """Read-only validation of a complete, current needs-review alignment run."""
    root, folder, plan, plan_path = plan_binding(root, folder, plan_path)
    index_path = job_file(folder, index_path)
    index = read(index_path)
    require(index.get('schema') == 'generic_group_subtitle_alignment_manifest_v1'
        and index.get('phase') == 'needs_alignment_review' and index.get('all_groups_ready') is False
        and index.get('partial_preview') is False, 'Only an actual failed complete alignment index may open recovery')
    require(index.get('plan') == {'path': str(plan_path), 'sha256': sha(plan_path)}
        and index.get('selected_group_ids') == [g['group_id'] for g in plan['tts_groups']]
        and index.get('source_row_count') == len(plan['source_rows'])
        and index.get('source_plan_row_count') == len(plan['source_rows'])
        and index.get('tts_group_count') == len(plan['tts_groups']), 'Alignment index uses a different plan/scope')
    manifest_path = job_file(folder, index['native_manifest']['path'])
    if expected_manifest_path is not None:
        require(manifest_path == job_file(folder, expected_manifest_path),
            'Alignment index uses another audio manifest')
    require(sha(manifest_path) == index['native_manifest']['sha256'], 'Alignment audio manifest changed')
    manifest = read(manifest_path)
    require(manifest.get('phase') == 'complete', 'Generation is incomplete')
    actual = {g['group_id']: g for g in plan['tts_groups']}
    require([g['group_id'] for g in index['groups']] == list(actual), 'Alignment groups incomplete/reordered')
    failed, unresolved = [], []
    for item in index['groups']:
        group = actual[item['group_id']]
        require(item.get('member_ids') == group['member_ids'], 'Alignment member identity changed')
        receipt_path = checked_record(folder, item['receipt'])
        receipt = read(receipt_path)
        require(receipt.get('schema') == 'generic_group_subtitle_alignment_v1'
            and receipt.get('group_id') == group['group_id'] and receipt.get('member_ids') == group['member_ids']
            and receipt.get('status') == item.get('status') and receipt.get('plan') == index['plan'], 'Alignment failure receipt differs')
        if item['status'] == 'aligned_for_machine_preview':
            continue
        require(item['status'] == 'needs_alignment_review', 'Unrecognized alignment status')
        if len(group['member_ids']) > 1:
            require(receipt.get('native_manifest') == index['native_manifest']
                and receipt.get('source_members') == group.get('members'), 'Failed alignment provenance changed')
            generated = [r for r in manifest.get('utterances', []) if r['segment_id'] == group['group_id']]
            require(len(generated) == 1, 'Failed alignment native group missing or duplicated')
            import generic_group_alignment as alignment
            alignment.audio_binding(group, generated[0], manifest_path)
            failed.append({'member_ids': list(group['member_ids'])})
        else:
            unresolved.extend(group['member_ids'])
    require(failed or unresolved, 'Needs-review index has no failed group')
    return {'root': root, 'folder': folder, 'plan': plan, 'plan_path': plan_path,
        'index_path': index_path, 'manifest_path': manifest_path, 'failed': failed,
        'unresolved': unresolved, 'group_count': len(actual)}


def repair_alignment(root, folder, plan_path, index_path):
    bound = alignment_failure_scope(root, folder, plan_path, index_path)
    root, folder, plan, plan_path = (bound[k] for k in ('root', 'folder', 'plan', 'plan_path'))
    index_path, manifest_path = bound['index_path'], bound['manifest_path']
    failed, unresolved = bound['failed'], bound['unresolved']
    if not failed:
        return {'changed': False, 'unresolved_ids': unresolved, 'reason': 'no_failed_multirow_groups'}
    reservation = reserve_recovery(folder, 'alignment_split', {'plan_sha256': sha(plan_path),
        'alignment_index_sha256': sha(index_path), 'native_manifest_sha256': sha(manifest_path),
        'source_segments_sha256': plan['source_segments']['sha256'], 'failed_groups': failed})
    cached = reused_result(folder, reservation)
    if cached:
        return cached
    evidence = archive(folder, index_path, 'failed_alignment')
    policy_path = job_file(folder, 'tts/no_join_recovery.json', exists=False)
    old_groups, proofs, history = [], [], []
    if policy_path.exists():
        old = read(policy_path)
        require(old.get('schema') == 'scoped_no_join_recovery_v1' and old.get('job') == folder.name
            and old.get('source_segments_sha256') == plan['source_segments']['sha256']
            and old.get('source_speakers_sha256') == plan['source_speakers']['sha256'], 'Existing no-join policy is stale')
        for proof in old['evidence']:
            checked_record(folder, proof)
        old_groups, proofs = old['groups'], old['evidence']
        history = [record(folder, archive(folder, policy_path, 'no_join_before_split'))]
    used = {sid for g in old_groups for sid in g['member_ids']}
    additional = []
    for group in failed:
        if group in old_groups:
            continue
        require(not used.intersection(group['member_ids']), 'New failed group overlaps an older recovery scope')
        used.update(group['member_ids'])
        additional.append(group)
    policy = {'schema': 'scoped_no_join_recovery_v1', 'job': folder.name,
        'source_segments_sha256': plan['source_segments']['sha256'],
        'source_speakers_sha256': plan['source_speakers']['sha256'], 'groups': old_groups + additional,
        'evidence': proofs + [record(folder, evidence)], 'history': history,
        'reason': 'Actual target-free subtitle alignment failed; split only those complete source sentences. Existing WAVs remain untouched.',
        'human_reviewed': False, 'waveform_modified': False}
    save(policy_path, policy)
    return finish_recovery(folder, reservation['token'], {'changed': bool(additional), 'unresolved_ids': unresolved,
        'split_groups': additional, 'preserved_other_groups': bound['group_count'] - len(additional),
        'outputs': [record(folder, policy_path), record(folder, evidence)]})


def wav_metrics(path, expected):
    """Validate PCM; flat-energy audio needs target-free speech verification.

    ZCR remains diagnostic: a high-ZCR noise component must not exempt a flat,
    unintelligible waveform from ASR, as observed in the 00:56 delivery defect.
    """
    import numpy as np
    path = Path(path)
    require(sha(path) == expected['sha256'], 'Generated WAV SHA changed')
    with wave.open(str(path), 'rb') as handle:
        frames, rate, channels, width = handle.getnframes(), handle.getframerate(), handle.getnchannels(), handle.getsampwidth()
        require(handle.getcomptype() == 'NONE' and channels == 1 and width in (1, 2, 3, 4)
            and frames > 0 and rate > 0, 'Unsupported/empty generated PCM')
        raw = handle.readframes(frames)
    require(frames == expected['frames'] and rate == expected['sample_rate']
        and expected.get('channels', channels) == channels and len(raw) == frames * channels * width,
        'Generated WAV geometry/truncation mismatch')
    require(abs(frames / rate - expected['duration_s']) <= 1 / rate, 'Generated WAV duration mismatch')
    if width == 1:
        signal = (np.frombuffer(raw, dtype=np.uint8).astype(float) - 128) / 128
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        n = b[:, 0] | b[:, 1] << 8 | b[:, 2] << 16
        signal = np.where(n >= 8388608, n - 16777216, n).astype(float) / 8388608
    else:
        signal = np.frombuffer(raw, dtype='<i' + str(width)).astype(float) / (2 ** (width * 8 - 1))
    require(bool(np.isfinite(signal).all()), 'Nonfinite generated PCM')
    block = max(1, int(.02 * rate))
    require(len(signal) >= block, 'Generated audio too short for a 20ms quality window')
    windows = signal[:len(signal) // block * block].reshape(-1, block)
    rms = np.sqrt(np.mean(windows * windows, axis=1))
    db = 20 * np.log10(np.maximum(rms, 1e-8))
    dynamic = float(np.percentile(db, 95) - np.percentile(db, 5))
    zcr = float(np.mean(np.diff(np.signbit(signal)) != 0)) if len(signal) > 1 else 0.
    return {'frames': frames, 'sample_rate': rate, 'channels': channels, 'sample_width': width,
        'finite_samples': True, 'energy_dynamic_range_db': dynamic, 'zero_crossing_rate': zcr,
        'acoustically_suspicious': dynamic < 12.,
        'peak': float(np.max(np.abs(signal)))}


def audio_quality(root, plan_path, manifest_path, output, observer=None, normalize=None):
    """CPU only. ASR only suspects, with no target/prompt, never a listening approval."""
    root = Path(root).resolve()
    plan_path = Path(plan_path).absolute()
    plan = read(plan_path)
    folder = root / 'jobs' / plan['job']
    root, folder, plan, plan_path = plan_binding(root, folder, plan_path)
    output = job_file(folder, output, exists=False)
    manifest_path = job_file(folder, manifest_path)
    manifest = read(manifest_path)
    require(manifest.get('phase') == 'complete', 'Audio manifest is incomplete')
    native = manifest.get('utterances', [])
    require([r['segment_id'] for r in native] == [g['group_id'] for g in plan['tts_groups']], 'Manifest group coverage/order differs')
    import generic_group_alignment as alignment
    if normalize is None:
        normalize = alignment.normalize_factory(root)
    results, repair_ids = [], []
    binding = {'plan': record(folder, plan_path), 'native_manifest': record(folder, manifest_path),
        'source_segments_sha256': plan['source_segments']['sha256']}
    for group, audio in zip(plan['tts_groups'], native):
        bound = alignment.audio_binding(group, audio, manifest_path)
        metrics = wav_metrics(bound['path'], {**audio, 'channels': 1})
        result = {'group_id': group['group_id'], 'member_ids': group['member_ids'],
            'audio_sha256': bound['sha256'], **metrics, 'target_free_asr_used': False,
            'needs_repair': False, 'human_listened': False}
        if metrics['acoustically_suspicious']:
            if observer is None:
                configured = Path(read(root / 'config/local.json')['asr_model']).resolve()
                require(any(re.search(r'(^|[-_])small($|[-_])', part, re.I) for part in configured.parts),
                    'A configured local small Whisper model is required; no download or larger fallback is allowed')
                observer = alignment.Observer(root, folder / 'tts/media_recovery/target_free_quality_asr')
            observation, path, hit = observer.observe(bound)
            recognized = ''.join(s['text'] for s in observation['segments'])
            similarity = difflib.SequenceMatcher(None, normalize(group['text_zh']), normalize(recognized)).ratio()
            result.update(target_free_asr_used=True, target_free_recognized=recognized,
                normalized_text_similarity=similarity, observation=record(folder, path),
                asr_cache_hit=hit, needs_repair=similarity < .4)
            if result['needs_repair']:
                repair_ids.extend(group['member_ids'])
        results.append(result)
        save(output, {'schema': 'website_audio_quality_v1', 'binding': binding, 'complete': False,
            'rows': results, 'needs_repair_ids': repair_ids, 'human_listened': False})
    result = {'schema': 'website_audio_quality_v1', 'binding': binding, 'complete': True,
        'rows': results, 'checked_utterances': len(results), 'needs_repair_ids': repair_ids,
        'needs_repair_groups': [r['group_id'] for r in results if r['needs_repair']],
        'flagged_groups': [{'group_id': r['group_id'], 'member_ids': r['member_ids']} for r in results if r['needs_repair']],
        'helper_code_sha256': sha(__file__),
        'status': 'needs_repair' if repair_ids else 'machine_anomaly_screen_passed',
        'waveform_modified': False, 'human_listened': False,
        'method': 'Exact native WAV provenance/geometry/finite PCM for every group; dynamic<12dB triggers verification regardless of ZCR; only suspect groups receive local target-free CPU small/int8 ASR; ASR similarity<.4 requires repair. Not voice/pronunciation/listening approval.'}
    save(output, result)
    return result


def frame_is_usable(gray):
    import numpy as np
    values = np.frombuffer(gray, dtype=np.uint8)
    require(values.size == 160 * 90, 'Incomplete decoded picture evidence')
    low, high = float(np.percentile(values, 5)), float(np.percentile(values, 95))
    standard = float(np.std(values))
    black, white, flat = high < 20., low > 235., standard < 5. and high - low < 12.
    return {'usable': not (black or white or flat), 'black': black, 'white': white,
        'flat': flat, 'p05': low, 'p95': high, 'standard_deviation': standard}


def extract_frame(ffmpeg, source, timestamp, output):
    output.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run([str(ffmpeg), '-v', 'error', '-nostdin', '-threads', '1', '-ss', '%.9f' % timestamp,
        '-i', str(source), '-frames:v', '1', '-q:v', '2', '-y', str(output)], capture_output=True, timeout=90)
    require(result.returncode == 0 and output.is_file() and output.stat().st_size > 0, 'Could not extract actual source frame')
    decoded = subprocess.run([str(ffmpeg), '-v', 'error', '-nostdin', '-threads', '1', '-i', str(output),
        '-frames:v', '1', '-vf', 'scale=160:90,format=gray', '-f', 'rawvideo', '-'], capture_output=True, timeout=30)
    require(decoded.returncode == 0, 'Could not decode picture evidence')
    return frame_is_usable(decoded.stdout)


def prepare_picture_holds(root, folder, plan_path, source_path, edl_path, audit_path, policy=None):
    root, folder, plan, plan_path = plan_binding(root, folder, plan_path)
    require(isinstance(policy, dict) and policy.get('enabled') is True and policy.get('picture_holds') is True,
        'Website policy does not allow bounded last-frame holds')
    source_path, edl_path, audit_path = [job_file(folder, p) for p in (source_path, edl_path, audit_path)]
    source, edl, audit = read(source_path), read(edl_path), read(audit_path)
    require(source_path == folder / 'source.json' and sha(source_path) == plan['source_binding']['source_receipt_sha256'], 'Source JSON differs')
    require(audit.get('plan') == str(plan_path) and audit.get('plan_sha256') == sha(plan_path)
        and audit.get('source_json_sha256') == sha(source_path), 'Actual adapter audit belongs to another source/plan')
    require(edl.get('fps') == 25 and Path(edl.get('source_input', '')).resolve() == Path(source['input']).resolve()
        and edl.get('issues') == audit.get('issues') and edl.get('partial_preview') is False,
        'Actual EDL/audit coverage or source differs')
    issues = audit['issues']
    if not issues:
        return {'changed': False, 'reason': 'no_source_window_issues'}
    require(all(x.get('reason') == 'whole_group_audio_exceeds_source_window' for x in issues), 'Unrelated adapter error cannot authorize a picture hold')
    groups = {g['group_id']: g for g in plan['tts_groups']}
    rows = plan['source_rows']
    positions = {r['segment_id']: i for i, r in enumerate(rows)}
    clips = {c['segment_ids'][0]: c for c in edl['clips'] if c.get('kind') == 'speech'}
    audio = {a['group_id']: a for a in edl['utterances']}
    require(list(audio) == list(groups) and list(clips) == list(groups), 'EDL group coverage/order differs')
    require(len({x['group_id'] for x in issues}) == len(issues), 'Duplicate source-window issues')
    reservation = reserve_recovery(folder, 'picture_holds', {'plan_sha256': sha(plan_path),
        'source_segments_sha256': plan['source_segments']['sha256'], 'source_video_sha256': plan['source_binding']['source_video_sha256'],
        'edl_sha256': sha(edl_path), 'audit_sha256': sha(audit_path), 'policy_sha256': key(policy)})
    cached = reused_result(folder, reservation)
    if cached:
        return cached
    from group_compact_adapter import FFMPEG, picture_hold_policy
    entries, proofs, frame_checks, unresolved, overlong_ids, blank_frame_ids = [], [], [], [], [], []
    existing = picture_hold_policy(root, plan, source)
    new_members = {tuple(groups[x['group_id']]['member_ids']) for x in issues}
    if existing:
        audio_by_members = {tuple(a['member_ids']): a for a in audio.values()}
        for members, entry in existing['groups'].items():
            if members in new_members:
                continue
            require(members in audio_by_members and audio_by_members[members]['audio_sha256'] == entry['audio_sha256'],
                'Previously authorized picture hold audio changed; regenerate bound evidence')
            entries.append(copy.deepcopy(entry))
            proofs.append(record(folder, entry['frame_evidence']['path']))
    folder_out = job_file(folder, 'tts/media_recovery/picture_' + reservation['token'][:16], exists=False)
    for issue in issues:
        gid = issue['group_id']
        require(gid in groups, 'Unknown source-window group')
        group, clip, utterance = groups[gid], clips[gid], audio[gid]
        ids = group['member_ids']
        start = rows[positions[ids[0]]]['start_s']
        after = positions[ids[-1]] + 1
        boundary = rows[after]['start_s'] if after < len(rows) else source['duration_s']
        require(ids == [r['segment_id'] for r in rows[positions[ids[0]]:after]]
            and clip.get('source_member_ids') == ids and utterance.get('member_ids') == ids
            and abs(group['source_start_s'] - start) < 1e-7, 'Picture hold source members/windows changed')
        pcm = {'sha256': utterance['audio_sha256'], 'frames': utterance.get('frames', utterance.get('audio_frames')),
            'sample_rate': utterance.get('sample_rate', utterance.get('audio_sample_rate')), 'channels': 1, 'duration_s': utterance['duration_s']}
        wav_metrics(utterance['audio'], pcm)
        speech = pcm['frames'] / pcm['sample_rate']
        available = boundary - start
        frames = math.ceil((speech + .35) * 25 - 1e-8)
        length = frames / 25
        require(available > 0 and speech - available > .35 + 1e-6
            and abs(issue['speech_s'] - speech) < 1e-7 and abs(issue['available_source_s'] - available) < 1e-7
            and clip['frames'] == frames and abs(clip['speech_s'] - speech) < 1e-7
            and abs(clip['source_start_s'] - start) < 1e-7 and abs(clip['source_end_s'] - boundary) < 1e-7,
            'Actual WAV/source/frame overrun geometry differs')
        picked = None
        for retreat in (0., .04, .12, .2, .32, .4, .48):
            picture_end = boundary - retreat
            freeze = length - (picture_end - start)
            if freeze > 4. + 1e-7 or picture_end < start + .04 - 1e-7:
                break
            timestamp = max(start, picture_end - .04)
            frame = job_file(folder, folder_out / (gid + '_%03d.jpg' % round(retreat * 100)), exists=False)
            require(re.fullmatch(r'[A-Za-z0-9_.-]+', gid), 'Unsafe frame identity')
            metrics = extract_frame(FFMPEG, source['input'], timestamp, frame)
            frame_checks.append({'group_id': gid, 'frame_time_s': timestamp, 'retreat_s': retreat, **metrics})
            if metrics['usable']:
                picked = (picture_end, freeze, timestamp, frame)
                break
        if picked is None:
            unresolved.extend(ids)
            if length - available > 4. + 1e-7:
                overlong_ids.extend(ids)
            else:
                blank_frame_ids.extend(ids)
            continue
        picture_end, freeze, timestamp, frame = picked
        proof = record(folder, frame)
        entry = {'member_ids': ids, 'audio_sha256': utterance['audio_sha256'],
            'source_start_s': start, 'source_boundary_s': boundary,
            'max_freeze_s': min(4., math.ceil((freeze - 1e-8) * 25) / 25),
            'frame_evidence': proof, 'frame_time_s': timestamp}
        if picture_end < boundary - 1e-7:
            entry['picture_source_end_s'] = picture_end
        entries.append(entry)
        proofs.append(proof)
    checks_path = folder_out / 'frame_checks.json'
    save(checks_path, {'schema': 'machine_picture_hold_frame_checks_v1', 'frames': frame_checks,
        'unresolved_ids': unresolved, 'human_reviewed': False,
        'method': 'Actual source last frame, CPU grayscale black/white/flat screening, bounded retreat <=.48s; not full visual content review.'})
    if unresolved:
        result = {'changed': False, 'unresolved_ids': unresolved, 'overlong_ids': overlong_ids,
            'blank_frame_ids': blank_frame_ids,
            'reason': 'no_usable_actual_frame' if blank_frame_ids else 'hold_exceeds_4s',
            'outputs': [record(folder, checks_path)]}
        return finish_recovery(folder, reservation['token'], result)
    policy_path = job_file(folder, 'tts/picture_holds.json', exists=False)
    if policy_path.exists():
        archive(folder, policy_path, 'picture_holds_before_replace')
    authorization = {'schema': 'scoped_group_picture_holds_v1', 'job': folder.name,
        'source_segments_sha256': plan['source_segments']['sha256'],
        'source_video_sha256': plan['source_binding']['source_video_sha256'],
        'assistant_reviewed': True, 'human_reviewed': False, 'groups': entries,
        'website_policy_sha256': key(policy), 'frame_checks': record(folder, checks_path),
        'review_note': 'Website-authorized picture-only holds with exact source/member/audio bindings and actual-frame blank checks; no speech cuts, speed changes or human listening approval.'}
    save(policy_path, authorization)
    return finish_recovery(folder, reservation['token'], {'changed': True, 'unresolved_ids': [],
        'overlong_ids': [], 'blank_frame_ids': [], 'authorized_groups': len(entries), 'max_authorized_hold_s': max(x['max_freeze_s'] for x in entries),
        'outputs': [record(folder, policy_path), record(folder, checks_path)] + proofs})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    quality = commands.add_parser('audio-quality')
    for name in ('root', 'plan', 'manifest', 'output'):
        quality.add_argument('--' + name, type=Path, required=True)
    picture = commands.add_parser('picture-holds')
    for name in ('root','plan','source','edl','audit','output'):
        picture.add_argument('--'+name,type=Path,required=True)
    picture.add_argument('--policy-json',required=True)
    args = parser.parse_args()
    if args.command == 'audio-quality':
        result = audio_quality(args.root, args.plan, args.manifest, args.output)
        print(json.dumps({'status': result['status'], 'checked_utterances': result['checked_utterances'],
            'needs_repair_ids': result['needs_repair_ids'], 'output': str(args.output.resolve())}, ensure_ascii=False))
    elif args.command == 'picture-holds':
        plan=read(args.plan);folder=Path(args.root).resolve()/'jobs'/plan['job']
        output=job_file(folder,args.output,exists=False)
        result=prepare_picture_holds(args.root,folder,args.plan,args.source,args.edl,args.audit,
            policy=json.loads(args.policy_json))
        save(output,result)
        print(json.dumps({k:v for k,v in result.items() if k!='outputs'},ensure_ascii=False))


if __name__ == '__main__':
    main()
