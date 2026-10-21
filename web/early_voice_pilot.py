"""Publish intact, verified prefix WAVs while the same full native run continues.

Standard library only. No synthesis, media conversion, concatenation or gain.
The source manifest may still be loading/synthesizing; its phase is never edited.
"""
import contextlib
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import sys
import wave

SCHEMA = 'early_voice_pilot_v1'
NATIVE_SCHEMA = 'native_mlx_whole_utterance_v1'
PROFILE = 'hybrid-gpt-preserve-25-pad-eager'
SHA = re.compile(r'^[0-9a-f]{64}$')


def _check(value, message):
    if not value:
        raise ValueError('EARLY_VOICE_PILOT: ' + message)


def _sha(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + '\n').encode()


def _atomic(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.partial')
    _check(not temporary.is_symlink(), 'Unsafe temporary receipt path')
    temporary.write_bytes(_json_bytes(value))
    temporary.replace(path)


def _inside(path, boundary, label):
    result = Path(path).resolve()
    _check(result.is_relative_to(boundary), label + ' is outside the allowed folder')
    return result


def _finite(value):
    return type(value) in (float, int) and math.isfinite(value)


@contextlib.contextmanager
def _native_runtime(root):
    """Use the deployed stdlib native cache validator; restore import search path."""
    previous = list(sys.path)
    try:
        scripts = root / 'scripts'
        path = _inside(scripts / 'mlx_tts.py', root, 'Native validator')
        sys.path.insert(0, str(scripts))
        spec = importlib.util.spec_from_file_location('_early_pilot_native_cache_validator', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.path[:] = previous


def _manifest_rows(manifest, expected_job, expected_rows):
    _check(manifest.get('schema') == NATIVE_SCHEMA and manifest.get('job') == expected_job,
           'Wrong native schema or source TTS job')
    _check(manifest.get('phase') in ('planned', 'loading', 'synthesis', 'complete'),
           'Unknown native phase')
    rows = manifest.get('utterances')
    _check(isinstance(expected_rows, list) and bool(expected_rows) and isinstance(rows, list) and
           len(rows) == len(expected_rows) and type(manifest.get('selected_utterances')) is int and
           manifest['selected_utterances'] == len(expected_rows), 'Full selected coverage changed')
    ids = [r.get('segment_id') for r in expected_rows]
    _check(all(isinstance(sid, str) and bool(sid) for sid in ids) and len(set(ids)) == len(ids),
           'Expected source IDs are missing or duplicated')
    parameters = manifest.get('parameters')
    _check(isinstance(parameters, dict), 'Missing effective native parameters')
    profile = parameters.get('experimental_profile')
    _check(isinstance(profile, dict) and profile.get('name') == PROFILE and
           parameters.get('diffusion_steps') == profile.get('diffusion_steps') == 25 and
           parameters.get('seed') == 42 and parameters.get('speed') == 1 and
           parameters.get('output_subtype') == 'PCM_24', 'Unregistered or weakened voice profile')
    raw = profile.get('semantic_logits_guard', {})
    _check(raw.get('check_mode') == 'all_steps' and raw.get('stop_after_first_sample') is False and
           raw.get('sampling_math_or_rng_changed') is False,
           'Original all-step raw semantic guard is required')
    first_identity = None
    for index, (entry, source) in enumerate(zip(rows, expected_rows)):
        content = entry.get('content')
        _check(isinstance(content, dict) and entry.get('segment_id') == source['segment_id'] and
               entry.get('speaker_id') == source.get('speaker_id') and
               type(entry.get('source_index')) is int and entry['source_index'] == index and
               entry.get('source_start_s') == source.get('start_s') and
               entry.get('source_end_s') == source.get('end_s'),
               'Native full row identity/order differs at index ' + str(index))
        _check(content.get('schema') == NATIVE_SCHEMA and content.get('parameters') == parameters and
               isinstance(source.get('text_zh'), str) and bool(source['text_zh'].strip()) and
               content.get('text_zh') == source['text_zh'] and
               content.get('effective_tts_text') == (source.get('tts_pronunciation_text') or source['text_zh']),
               'Native text/pronunciation/parameters differ: ' + source['segment_id'])
        identity = content.get('model_identity')
        _check(isinstance(identity, dict) and bool(identity), 'Missing native model identity')
        if first_identity is None:
            first_identity = identity
        _check(identity == first_identity, 'Mixed native model identities')
    return rows, parameters, profile


def _guard(entry, row, profile):
    evidence = entry.get('experimental_execution', {})
    _check(evidence == row.get('experimental_execution') and evidence.get('profile') == PROFILE,
           'Manifest and original sidecar guard evidence differ')
    semantic = evidence.get('semantic_logits_guard', {})
    transaction = semantic.get('transaction', {})
    counts = [transaction.get(name) for name in ('sampling_calls', 'checked_calls', 'original_sampler_calls')]
    _check(semantic.get('signature') == profile['semantic_logits_guard'] and
           all(type(n) is int and n > 0 for n in counts) and len(set(counts)) == 1 and
           isinstance(transaction.get('utterance_id'), str) and bool(transaction['utterance_id'].strip()) and
           transaction.get('nonfinite_rejected') is False and transaction.get('diagnostic_only') is False,
           'Original all-step raw semantic guard is incomplete')
    first = transaction.get('first_raw_logits', {})
    _check(type(first.get('values')) is int and first['values'] > 0 and
           first.get('finite_count') == first['values'] and first.get('all_finite') is True and
           all(first.get(name) == 0 for name in ('nan_count', 'positive_inf_count', 'negative_inf_count')),
           'Original first raw logits are not fully finite')
    guard = evidence.get('preclip_guard', {})
    _check(guard.get('conv_post_calls') == 1 and guard.get('ceiling') == profile.get('safe_ceiling') == .95 and
           _finite(guard.get('protected_peak')) and 0 <= guard['protected_peak'] <= .95 and
           guard.get('prevents_internal_clamp_for_this_tensor') is True and
           guard.get('time_or_pitch_changed') is False and guard.get('compressor_used') is False,
           'Original whole-utterance preclip guard is incomplete')
    _check(evidence.get('cfm_execution') == profile.get('cfm_execution') and
           all(evidence.get('cfm_padding', {}).get(k) == v for k, v in profile.get('cfm_padding', {}).items()),
           'Original eager/reflection padding evidence differs')


def _verified_group(root, cache, row, index, native, profile):
    content = row['content']
    key = row.get('cache_key')
    _check(isinstance(key, str) and SHA.fullmatch(key) is not None and native.fingerprint(content) == key,
           'Native content fingerprint differs')
    audio = _inside(row['audio'], root / 'cache', 'Source WAV')
    expected_audio = _inside(cache / 'audio' / (key + '.wav'), root / 'cache', 'Keyed WAV')
    _check(audio == expected_audio, 'Source WAV is not its exact keyed cache path')
    sidecar = _inside(cache / 'audio' / (key + '.json'), root / 'cache', 'Source sidecar')
    reference = _inside(row['reference'], root, 'Original voice reference')
    _check(reference.is_file() and reference.suffix.lower() != '.npz' and
           _sha(reference) == content.get('reference_sha256'), 'Original source reference changed')
    checked, reason = native.cached_entry(cache / 'audio', key, content)
    _check(checked is not None, 'Original native cache validator rejected audio: ' + str(reason))
    sidecar_bytes = sidecar.read_bytes()
    actual_entry = json.loads(sidecar_bytes)
    _check(actual_entry == checked and checked.get('schema') == NATIVE_SCHEMA and
           checked.get('cache_key') == key and checked.get('content') == content,
           'Original sidecar changed during native verification')
    actual_sha = _sha(audio)
    _check(checked.get('sha256') == row.get('sha256') == actual_sha,
           'Original WAV bytes differ from native manifest/sidecar')
    with wave.open(str(audio), 'rb') as handle:
        frames, rate, channels, width = (handle.getnframes(), handle.getframerate(),
                                         handle.getnchannels(), handle.getsampwidth())
    _check(type(row.get('frames')) is int and frames > 0 and
           frames == row['frames'] == checked.get('frames') and
           rate == row.get('sample_rate') == checked.get('sample_rate') and rate > 0 and
           channels == row.get('channels') == checked.get('channels') == 1 and width == 3 and
           _finite(row.get('duration_s')) and _finite(checked.get('duration_s')) and
           abs(frames / rate - row['duration_s']) < 1 / rate and
           abs(frames / rate - checked['duration_s']) < 1 / rate,
           'Original PCM24 header/frame/duration evidence differs')
    _guard(checked, row, profile)
    return {'segment_id': row['segment_id'], 'speaker_id': row['speaker_id'], 'source_index': index,
            'cache_key': key, 'sha256': actual_sha,
            'generated_utterance_id': checked['experimental_execution']['semantic_logits_guard']['transaction']['utterance_id'],
            'source_audio': str(audio),
            'source_sidecar': str(sidecar), 'source_sidecar_sha256': hashlib.sha256(sidecar_bytes).hexdigest(),
            'reference': str(reference), 'reference_sha256': content['reference_sha256'],
            'frames': frames, 'sample_rate': rate, 'channels': channels, 'duration_s': frames / rate}


def _copy_exact(source, destination, expected_sha):
    _check(not destination.is_symlink(), 'Published WAV must not be a symlink')
    if destination.exists():
        _check(not destination.is_symlink() and _sha(destination) == expected_sha,
               'Refuse to overwrite a different published WAV')
        return
    temporary = destination.with_name(destination.name + '.partial')
    _check(not temporary.is_symlink(), 'Unsafe temporary pilot path')
    digest = hashlib.sha256()
    try:
        with Path(source).open('rb') as incoming, temporary.open('wb') as outgoing:
            for block in iter(lambda: incoming.read(1024 * 1024), b''):
                outgoing.write(block)
                digest.update(block)
        _check(digest.hexdigest() == expected_sha and _sha(temporary) == expected_sha,
               'Source WAV changed while copying')
        temporary.replace(destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def publish_available(root, folder, manifest_path, expected_job, expected_rows, limit=2):
    root = Path(root).resolve()
    folder = Path(folder).resolve()
    _check(folder.parent == root / 'jobs' and bool(folder.name), 'Pilot must belong to its original parent job')
    _check(type(limit) is int and 1 <= limit <= 2, 'Only one or two complete groups may be published')
    manifest_path = _inside(manifest_path, root / 'jobs', 'Source manifest')
    source_bytes = manifest_path.read_bytes()
    manifest = json.loads(source_bytes)
    rows, parameters, profile = _manifest_rows(manifest, expected_job, expected_rows)
    # Runner order is grouped by speaker cache. Do not publish group2 before
    # group1: artifact indices/URLs must remain a stable source-order prefix.
    ready = []
    for index, row in enumerate(rows[:limit]):
        if row.get('status') not in ('generated', 'cached'):
            break
        ready.append((index, row))
    if not ready:
        return None
    cache = _inside(manifest['cache_root'], root / 'cache', 'Native cache')
    with _native_runtime(root) as native:
        _check(native.build_experimental_profile(PROFILE, root) == profile,
               'Deployed native profile/helper identity changed')
        groups = [_verified_group(root, cache, row, index, native, profile) for index, row in ready]
    output = folder / 'outputs/voice_pilot'
    _check(output.resolve() == output and not output.is_symlink(), 'Pilot output must be a regular isolated folder')
    receipt_path = output / 'pilot_receipt.json'
    _check(not receipt_path.is_symlink(), 'Pilot receipt must be a regular JSON file')
    artifacts = [{'label': '选音小样' + str(index + 1) + '（完整意群，纯人声）',
                  'path': str(output / (str(group['source_index'] + 1).zfill(2) + '_' + group['cache_key'] + '.wav')),
                  'kind': 'audio', 'sha256': group['sha256']} for index, group in enumerate(groups)]
    previous = None
    if receipt_path.exists():
        previous = json.loads(receipt_path.read_bytes())
        _check(previous.get('schema') == SCHEMA and previous.get('job_id') == folder.name and
               previous.get('complete_job') is False and previous.get('human_reviewed') is False,
               'Existing pilot receipt is unrelated or claims complete/human approval')
        old = previous.get('artifacts')
        _check(isinstance(old, list) and 1 <= len(old) <= len(artifacts) and old == artifacts[:len(old)],
               'Published prefix cannot be reordered, replaced or reduced')
        for item in old:
            path = Path(item['path'])
            _check(not path.is_symlink() and path.is_file() and _sha(path) == item['sha256'],
                   'Published WAV bytes changed')
        if old == artifacts:
            _check(previous.get('groups') == groups and previous.get('parameters') == parameters and
                   previous.get('full_selected_count') == len(rows), 'Published evidence identity changed')
            # A retried parent Flow needs the same verified identity even
            # though no bytes/receipt change; its caller deduplicates by SHA.
            return {'schema': SCHEMA, 'artifacts': artifacts, 'receipt_path': str(receipt_path),
                    'receipt_sha256': _sha(receipt_path), 'complete_job': False, 'human_reviewed': False}
    output.mkdir(parents=True, exist_ok=True)
    for group, artifact in zip(groups, artifacts):
        _copy_exact(group['source_audio'], Path(artifact['path']), group['sha256'])
    now = datetime.now(timezone.utc).isoformat()
    receipt = {'schema': SCHEMA, 'job_id': folder.name, 'source_tts_job': expected_job,
               'artifacts': artifacts, 'groups': groups, 'complete_job': False, 'human_reviewed': False,
               'source_manifest_path': str(manifest_path),
               'source_manifest_sha256': hashlib.sha256(source_bytes).hexdigest(),
               'source_manifest_phase_at_publication': manifest['phase'],
               'full_selected_count': len(rows), 'published_group_ids': [g['segment_id'] for g in groups],
               'parameters': parameters, 'byte_identical_whole_utterances': True,
               'source_waveforms_modified': False, 'models_started': False,
               'created_at': previous['created_at'] if previous else now, 'updated_at': now}
    _atomic(receipt_path, receipt)
    return {'schema': SCHEMA, 'artifacts': artifacts, 'receipt_path': str(receipt_path),
            'receipt_sha256': _sha(receipt_path), 'complete_job': False, 'human_reviewed': False}
