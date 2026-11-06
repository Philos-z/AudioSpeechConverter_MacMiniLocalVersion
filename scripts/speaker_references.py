"""Source-bound reference selection; acoustic labels never establish identity.

Only real source samples are written. A failed candidate can use another run of
the same existing acoustic label, never another label or synthetic padding.
"""
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import tempfile

import numpy as np
import soundfile as sf


SCHEMA = 'speaker_reference_selection_v1'
REPORT_NAME = 'speaker_reference_selection.json'


class ReferenceSelectionError(ValueError):
    def __init__(self, speaker_id, reason_codes, candidate_failures=None, source_ids=None):
        self.speaker_id = speaker_id
        self.reason_codes = list(reason_codes)
        self.candidate_failures = list(candidate_failures or [])
        self.source_ids = list(source_ids or [])
        super().__init__('Reference selection failed for %s: %s' %
                         (speaker_id, ', '.join(self.reason_codes)))

    def as_dict(self):
        return {'speaker_id': self.speaker_id, 'selected': False,
                'source_ids': self.source_ids,
                'reason_codes': self.reason_codes,
                'candidate_failures': self.candidate_failures}


class _CandidateError(ValueError):
    pass


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate_reference_rows(assignments, source_duration):
    if not _finite(source_duration) or source_duration <= 0:
        raise _CandidateError('invalid_source_duration')
    if not isinstance(assignments, list) or not assignments:
        raise _CandidateError('empty_assignments')
    seen = set()
    for row in assignments:
        if not isinstance(row, dict):
            raise _CandidateError('invalid_assignments')
        start, end = row.get('start_s'), row.get('end_s')
        if not _finite(start) or not _finite(end):
            raise _CandidateError('non_finite_time')
        if not 0 <= start < end <= source_duration + 1e-6:
            raise _CandidateError('invalid_source_window')
        sid = row.get('segment_id')
        if not isinstance(sid, str) or not sid or sid in seen:
            raise _CandidateError('invalid_source_ids')
        seen.add(sid)


def reference_window(run, assignments, source_duration):
    """Return a safe real-source cut, preserving the usual window when safe.

    Ordinary padding is clipped at other ASR rows, including when short-context
    expansion is unavailable. Context expansion additionally leaves 30 ms from
    those rows and must contain the full original short run.
    """
    validate_reference_rows(assignments, source_duration)
    indices = run.get('indices')
    if (not isinstance(indices, list) or not indices
            or any(type(i) is not int or not 0 <= i < len(assignments) for i in indices)
            or len(set(indices)) != len(indices)):
        raise _CandidateError('invalid_run')
    start_s, end_s = run.get('start_s'), run.get('end_s')
    if (not _finite(start_s) or not _finite(end_s)
            or start_s != min(assignments[i]['start_s'] for i in indices)
            or end_s != max(assignments[i]['end_s'] for i in indices)):
        raise _CandidateError('invalid_source_window')
    members = set(indices)
    others = [row for i, row in enumerate(assignments) if i not in members]
    if any(assignments[i].get('overlap') for i in members):
        raise _CandidateError('overlapping_speech')
    if any(row['start_s'] < end_s and row['end_s'] > start_s for row in others):
        raise _CandidateError('overlapping_speech')
    previous = max([0.] + [row['end_s'] for row in others if row['end_s'] <= start_s])
    following = min([source_duration] + [row['start_s'] for row in others if row['start_s'] >= end_s])
    original_start = max(0., start_s - .03)
    original_end = min(source_duration, end_s + .03, original_start + 8)
    start = max(original_start, previous)
    end = min(original_end, following)
    if end - start >= .8 or end_s - start_s < .6:
        return start, end, None
    left = previous + .03 if previous else 0.
    right = following - .03 if following < source_duration else source_duration
    margin = (1.2 - (end_s - start_s)) / 2
    expanded_start = max(left, start_s - margin)
    expanded_end = min(right, end_s + margin)
    if (expanded_start > start_s or expanded_end < end_s
            or expanded_end - expanded_start < .8):
        return start, end, None
    return expanded_start, expanded_end, {
        'policy': 'short_asr_window_native_context_v1',
        'asr_window_s': [start_s, end_s],
        'original_reference_window_s': [original_start, original_end],
        'safe_source_bounds_s': [left, right],
        'source_ids': [assignments[i]['segment_id'] for i in indices],
        'synthetic_padding_samples': 0, 'speaker_labels_changed': False,
        'human_reviewed': False}


def _runs(assignments, speaker_id):
    runs = []
    for index, row in enumerate(assignments):
        if row.get('speaker_id') != speaker_id:
            continue
        if (runs and runs[-1]['indices'][-1] == index - 1
                and 0 <= row['start_s'] - runs[-1]['end_s'] < .8):
            runs[-1]['end_s'] = row['end_s']
            runs[-1]['indices'].append(index)
        else:
            runs.append({'start_s': row['start_s'], 'end_s': row['end_s'], 'indices': [index]})
    # Preserve the old longest-run choice and stable tie order whenever it works.
    ranked = sorted(runs, key=lambda run: -min(8, run['end_s'] - run['start_s']))
    seen = {tuple(run['indices']) for run in ranked}
    singles = [{'start_s': row['start_s'], 'end_s': row['end_s'], 'indices': [index]}
               for index, row in enumerate(assignments) if row.get('speaker_id') == speaker_id
               and (index,) not in seen]
    return ranked + sorted(singles, key=lambda run: -min(8, run['end_s'] - run['start_s']))


def _sha(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def _write_reference(folder, speaker_id, samples, rate, context):
    buffer = io.BytesIO()
    sf.write(buffer, samples, rate, format='WAV', subtype='PCM_24')
    encoded = buffer.getvalue()
    digest = hashlib.sha256(encoded).hexdigest()
    directory = folder / 'references'
    directory.mkdir(parents=True, exist_ok=True)
    if not directory.resolve().is_relative_to(folder):
        raise _CandidateError('invalid_reference_path')
    suffix = '_source_context_v1' if context else ''
    names = [speaker_id + suffix + '.wav']
    names += [speaker_id + (suffix or '_reference') + '_' + tail + '.wav'
              for tail in (digest[:16], digest)]
    for name in names:
        path = directory / name
        if not path.exists() and not path.is_symlink():
            break
        if not path.is_symlink() and _sha(path) == digest:
            return path, digest
    else:
        raise _CandidateError('reference_path_conflict')
    descriptor, temporary = tempfile.mkstemp(prefix='.reference-', suffix='.wav.part', dir=directory)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        # A complete file becomes visible in one operation; an old or racing
        # reference is never replaced. Interrupted private files are not reused.
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or _sha(path) != digest:
                raise _CandidateError('reference_path_conflict')
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path, digest


def prepare_reference(root, folder, reference_source, assignments, speaker_id):
    """Select, validate and preserve a reference; return one speakers.json item."""
    root, folder, reference_source = Path(root).resolve(), Path(folder).resolve(), Path(reference_source).resolve()
    if (not folder.is_relative_to(root) or not reference_source.is_relative_to(root)
            or not isinstance(speaker_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', speaker_id)):
        raise ReferenceSelectionError(speaker_id, ['invalid_reference_path'])
    try:
        info = sf.info(reference_source)
        validate_reference_rows(assignments, info.duration)
    except (_CandidateError, OSError, RuntimeError) as error:
        code = str(error) if isinstance(error, _CandidateError) else 'source_read_error'
        raise ReferenceSelectionError(speaker_id, [code]) from None
    own_ids = [row['segment_id'] for row in assignments if row.get('speaker_id') == speaker_id]
    candidates = _runs(assignments, speaker_id)
    failures = []
    source_stat = reference_source.stat()
    for run in candidates:
        attempt = {'start_s': run['start_s'], 'end_s': run['end_s'],
                   'source_ids': [assignments[i]['segment_id'] for i in run['indices']]}
        try:
            start, end, context = reference_window(run, assignments, info.duration)
            attempt['reference_window_s'] = [start, end]
            if end <= start:
                raise _CandidateError('invalid_source_window')
            with sf.SoundFile(reference_source) as handle:
                rate = handle.samplerate
                handle.seek(round(start * rate))
                samples = handle.read(round((end - start) * rate), dtype='float32', always_2d=True).mean(axis=1)
            current_stat = reference_source.stat()
            if (source_stat.st_dev, source_stat.st_ino, source_stat.st_size, source_stat.st_mtime_ns) != (
                    current_stat.st_dev, current_stat.st_ino, current_stat.st_size, current_stat.st_mtime_ns):
                raise _CandidateError('source_identity_changed')
            if len(samples) / rate < .8:
                raise _CandidateError('duration_too_short')
            if len(samples) / rate > 15:
                raise _CandidateError('duration_too_long')
            if not np.isfinite(samples).all():
                raise _CandidateError('non_finite_audio')
            if not samples.size or float(np.max(np.abs(samples))) <= .001:
                raise _CandidateError('insufficient_energy')
            path, digest = _write_reference(folder, speaker_id, samples, rate, context)
        except (_CandidateError, OSError, RuntimeError, ValueError) as error:
            code = str(error) if isinstance(error, _CandidateError) else 'source_read_error'
            attempt['reason_codes'] = [code]
            failures.append(attempt)
            if code == 'source_identity_changed':
                break
            continue
        item = {'reference': str(path.relative_to(root)), 'reviewed': True,
                'review_method': 'acoustic run selection; identities not verified',
                'reference_window_s': [start, end], 'reference_source': str(reference_source.relative_to(root)),
                'source_segments': own_ids,
                'reference_selection': {'policy': 'same_label_valid_candidate_v1',
                    'selected_source_ids': attempt['source_ids'],
                    'candidate_failures': failures, 'reference_sha256': digest,
                    'sample_rate': rate, 'frames': len(samples), 'synthetic_padding_samples': 0,
                    'speaker_labels_changed': False, 'human_reviewed': False}}
        if context:
            item['reference_context'] = context
        return item
    raise ReferenceSelectionError(speaker_id, ['no_valid_same_speaker_candidate' if candidates else 'missing_speaker'],
                                  failures, own_ids)


def selection_provenance_errors(root, reference_source, assignments, speaker_id, item, samples, rate):
    """Verify a new selection against its actual label, window and PCM source.

    Legacy items without selection metadata retain their existing quality gate;
    this function never presents them as having passed source provenance checks.
    """
    if 'reference_selection' not in item:
        return []
    selection = item.get('reference_selection')
    if not isinstance(selection, dict) or selection.get('policy') != 'same_label_valid_candidate_v1':
        return ['invalid_selection_provenance']
    root, reference_source = Path(root).resolve(), Path(reference_source).resolve()
    if not reference_source.is_relative_to(root):
        return ['selection_source_mismatch']
    errors = []
    try:
        declared_source = (root / item['reference_source']).resolve()
        if (not reference_source.is_relative_to(root) or declared_source != reference_source):
            errors.append('selection_source_mismatch')
        reference = (root / item['reference']).resolve()
        if (not reference.is_relative_to(root)
                or not isinstance(selection.get('reference_sha256'), str)
                or _sha(reference) != selection['reference_sha256']):
            errors.append('reference_identity_mismatch')
    except (KeyError, OSError, TypeError, ValueError):
        return sorted(set(errors + ['invalid_selection_provenance']))
    ids = selection.get('selected_source_ids')
    if (not isinstance(ids, list) or not ids or any(not isinstance(sid, str) for sid in ids)
            or len(ids) != len(set(ids))):
        return sorted(set(errors + ['invalid_selection_source_ids']))
    try:
        info = sf.info(reference_source)
        validate_reference_rows(assignments, info.duration)
        index = {row['segment_id']: i for i, row in enumerate(assignments)}
        if any(sid not in index for sid in ids):
            return sorted(set(errors + ['invalid_selection_source_ids']))
        indices = [index[sid] for sid in ids]
        if indices != sorted(indices) or any(assignments[i].get('speaker_id') != speaker_id for i in indices):
            return sorted(set(errors + ['selection_speaker_mismatch']))
        run = {'start_s': min(assignments[i]['start_s'] for i in indices),
               'end_s': max(assignments[i]['end_s'] for i in indices), 'indices': indices}
        start, end, context = reference_window(run, assignments, info.duration)
        window = item.get('reference_window_s')
        if (not isinstance(window, list) or len(window) != 2
                or any(not _finite(value) for value in window)
                or abs(window[0] - start) > 1e-8 or abs(window[1] - end) > 1e-8
                or item.get('reference_context') != context):
            errors.append('selection_window_mismatch')
        if (type(rate) is not int or rate != info.samplerate
                or selection.get('sample_rate') != rate
                or not isinstance(samples, np.ndarray) or samples.ndim != 1
                or selection.get('frames') != len(samples)):
            return sorted(set(errors + ['selection_audio_format_mismatch']))
        with sf.SoundFile(reference_source) as handle:
            handle.seek(round(start * rate))
            source_samples = handle.read(round((end - start) * rate), dtype='float32', always_2d=True).mean(axis=1)
        if (not np.isfinite(source_samples).all() or not np.isfinite(samples).all()):
            errors.append('selection_nonfinite_source_audio')
        elif len(source_samples) != len(samples):
            errors.append('selection_audio_format_mismatch')
        else:
            # Compare the same PCM24 representation used by the reference writer.
            # This includes PCM24's defined quantization and full-scale clipping.
            encoded = io.BytesIO()
            sf.write(encoded, source_samples, rate, format='WAV', subtype='PCM_24')
            encoded.seek(0)
            expected, _ = sf.read(encoded, dtype='float32')
            if not len(samples) or float(np.max(np.abs(expected - samples))) > 1.3e-7:
                errors.append('selection_source_audio_mismatch')
    except _CandidateError:
        errors.append('selection_window_mismatch')
    except (OSError, RuntimeError, ValueError, TypeError, KeyError):
        errors.append('selection_source_read_error')
    return sorted(set(errors))


def reference_bindings(root, folder, reference_source):
    """Hash actual files, including the source video rather than metadata alone."""
    root, folder = Path(root).resolve(), Path(folder).resolve()
    reference_source = Path(reference_source).resolve()
    source_json = folder / 'source.json'
    data = json.loads(source_json.read_text())
    source_video = Path(data['input']).resolve()
    paths = [source_json, source_video, reference_source, folder / 'segments.asr.json']
    paths += [folder / name for name in ('speaker_assignments.json', 'speakers.json', 'segments.plan.json')
              if (folder / name).is_file()]
    if (folder / 'speakers.json').is_file():
        manifest = json.loads((folder / 'speakers.json').read_text())
        for item in manifest.values() if isinstance(manifest, dict) else ():
            reference = item.get('reference') if isinstance(item, dict) else None
            if isinstance(reference, str):
                path = (root / reference).resolve()
                if path.is_relative_to(root) and path.is_file():
                    paths.append(path)
    result = {}
    for path in paths:
        path = path.resolve()
        if not path.is_relative_to(root):
            raise ValueError('Reference binding path is outside the project')
        result[str(path.relative_to(root))] = _sha(path)
    return result


def write_selection_report(root, folder, reference_source, results, failures):
    """Commit a current, bound outcome after all speakers have been evaluated."""
    root, folder = Path(root).resolve(), Path(folder).resolve()
    speakers = []
    for speaker_id, item in results.items():
        selection = item['reference_selection']
        speakers.append({'speaker_id': speaker_id, 'selected': True,
            'source_ids': selection['selected_source_ids'],
            'reference': item['reference'], 'reference_window_s': item['reference_window_s'],
            'reference_sha256': selection['reference_sha256'],
            'candidate_failures': selection['candidate_failures']})
    speakers.extend(error.as_dict() if isinstance(error, ReferenceSelectionError) else error for error in failures)
    speakers.sort(key=lambda row: row['speaker_id'])
    report = {'schema': SCHEMA, 'complete': True,
              'bindings': reference_bindings(root, folder, reference_source),
              'speakers': speakers, 'failures': [row for row in speakers if not row['selected']],
              'human_reviewed': False, 'identities_verified': False}
    for item in results.values():
        reference = (root / item['reference']).resolve()
        if not reference.is_relative_to(root):
            raise ValueError('Selected reference is outside the project')
        actual = _sha(reference)
        if actual != item['reference_selection']['reference_sha256']:
            raise ValueError('Selected reference changed before its report was committed')
        report['bindings'][str(reference.relative_to(root))] = actual
    path = folder / REPORT_NAME
    encoded = (json.dumps(report, ensure_ascii=False, indent=2) + '\n').encode()
    if path.is_file() and path.read_bytes() != encoded:
        history = folder / 'reference_selection_history'
        history.mkdir(exist_ok=True)
        previous = history / (_sha(path) + '.json')
        if not previous.exists():
            previous.write_bytes(path.read_bytes())
    descriptor, temporary = tempfile.mkstemp(prefix='.reference-selection-', dir=folder)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(encoded)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return report
