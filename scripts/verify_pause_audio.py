"""Read-only CPU verification of final punctuation audio and safe edge trims.

Run after punctuation_audit.json and timeline.punctuated.json exist. Does not
load speech models, transcribe, synthesize, modify WAVs, or imply listening QA.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
import time

import numpy as np
import soundfile as sf

from comma_chunk_tts import join_chunks, trim_generated_silence
from punctuation_pauses import insert_silence

PCM_TOL = 1.3e-7
QUIET_PEAK = .0005
EXPECTED_COUNTS = {'repaired': 33, 'no_comma': 29, 'comma_chunked': 73}


def load(path):
    return json.loads(Path(path).read_text())


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def waveform(path):
    samples, rate = sf.read(path, dtype='float32', always_2d=False)
    require(len(samples) > 0 and np.isfinite(samples).all(), 'Empty/nonfinite audio: ' + str(path))
    return samples, rate


def peak(samples):
    return float(np.max(np.abs(samples))) if len(samples) else 0.


def compare(expected, actual, label):
    require(expected.shape == actual.shape, label + ': shape/sample-count mismatch')
    error = float(np.max(np.abs(expected.astype(np.float64) - actual.astype(np.float64))))
    require(error <= PCM_TOL, f'{label}: PCM reconstruction error {error:.9g} > {PCM_TOL}')
    return error


def check_trim(source, sr, info):
    """Independently check removed peaks and retained guards against raw samples."""
    absolute = np.max(np.abs(source), axis=1) if source.ndim == 2 else np.abs(source)
    active = np.flatnonzero(absolute > QUIET_PEAK)
    require(len(active) > 0, 'No active audio before trim')
    first, last = int(active[0]), int(active[-1])
    guard = round(.020 * sr)
    start, stop = max(0, first - guard), min(len(source), last + 1 + guard)
    expected = {
        'original_samples': len(source), 'retained_samples': stop-start,
        'trim_head_samples': start, 'trim_tail_samples': len(source)-stop,
        'quiet_head_guard_samples': first-start,
        'quiet_tail_guard_samples': stop-last-1,
    }
    for key, value in expected.items():
        require(info.get(key) == value, f'Trim metadata mismatch: {key}')
    require(peak(source[:start]) <= QUIET_PEAK, 'Audible samples deleted from head')
    require(peak(source[stop:]) <= QUIET_PEAK, 'Audible samples deleted from tail')
    require(first-start == min(guard, first), 'Available 20ms head guard not retained')
    require(stop-last-1 == min(guard, len(source)-last-1), 'Available 20ms tail guard not retained')
    return {'trimmed_samples': start+len(source)-stop,
            'head_guard_available_s': (first-start)/sr,
            'tail_guard_available_s': (stop-last-1)/sr,
            'short_source_guard_edges': int(first < guard)+int(len(source)-last-1 < guard)}


def selected_reports(job):
    base = load(job/'tts/punctuated/punctuation_report.json')
    again = load(job/'tts/punctuated_v3/punctuation_report.json')
    require(not base.get('pending_ids') and not again.get('pending_ids'), 'Pending punctuation reports')
    result = {row['id']: row for row in base['utterances']}
    for row in again['utterances']:
        if row['status'] == 'repaired':
            result[row['id']] = row
    return result


def verify_dsp(report, output, sr):
    raw = Path(report['raw_file'])
    require(digest(raw) == report['raw_sha256'], 'DSP raw SHA mismatch')
    samples, raw_sr = waveform(raw)
    require(raw_sr == sr, 'DSP changed sample rate')
    commas = report['commas']
    require(commas, 'DSP row has no comma boundaries')
    require(report.get('voice_samples_deleted') == 0, 'DSP declares deleted voice samples')
    require(report.get('rate_or_pitch_changed') is False, 'DSP declares rate/pitch change')
    total = 0
    for comma in commas:
        require(comma.get('alignment_status') == 'trusted_word_boundary', 'DSP untrusted word boundary')
        require(comma.get('status') in ('insert_pause', 'already_has_pause'), 'DSP unsafe boundary status')
        cut, extra = int(comma['cut_sample']), int(comma['added_samples'])
        require(0 <= cut <= len(samples) and extra >= 0, 'DSP invalid cut/extra length')
        require(comma['minimum_rms'] <= comma['threshold_rms'] + 1e-12, 'DSP energy gate failed')
        target = report.get('target_pause_s', .18)
        require(comma['existing_quiet_s'] + extra/sr >= target-1/sr, 'DSP gap below target')
        total += extra
    rebuilt = insert_silence(samples, sr, commas)
    require(len(output) == len(samples)+total, 'DSP deleted samples or added an unrecorded length')
    offset = 0
    for comma in sorted(commas, key=lambda row: row['cut_sample']):
        extra = comma['added_samples']; start = comma['cut_sample']+offset
        require(peak(output[start:start+extra]) == 0., 'DSP inserted region is not digital silence')
        offset += extra
    return compare(rebuilt, output, 'DSP'), total/sr, len(commas)


def verify_chunks(report, output, sr):
    chunks = []
    short_guards = 0
    texts = []
    for chunk in report['chunks']:
        path = Path(chunk['file'])
        require(digest(path) == chunk['sha256'], 'Raw chunk SHA mismatch')
        samples, rate = waveform(path)
        require(rate == sr, 'Chunk sample-rate mismatch')
        chunks.append(samples); texts.append(chunk['tts_text'])
    require(''.join(texts) == report['tts_text'], 'Chunk text concatenation changed source text')
    rebuilt, info = join_chunks(chunks, sr, .18)
    require(info['chunk_trims'] == report['join_info']['chunk_trims'], 'Chunk trim metadata changed')
    require(info['joins'] == report['join_info']['joins'], 'Chunk join metadata changed')
    for source, trim in zip(chunks, info['chunk_trims']):
        short_guards += check_trim(source, sr, trim)['short_source_guard_edges']
    error = compare(rebuilt, output, 'Chunk join')
    joins = info['joins']; quiet_peaks = []; gaps = []
    for join in joins:
        index = join['after_chunk']
        prior_tail = info['chunk_trims'][index]['quiet_tail_guard_samples']
        next_head = info['chunk_trims'][index+1]['quiet_head_guard_samples']
        start = join['insert_start_sample']-prior_tail
        stop = join['insert_start_sample']+join['inserted_samples']+next_head
        require(0 <= start < stop <= len(output), 'Actual chunk gap outside output')
        gap = (stop-start)/sr
        require(abs(gap-.18) <= 1/sr+1e-10, 'Actual chunk gap not 180ms')
        actual_peak = peak(output[start:stop])
        require(actual_peak <= QUIET_PEAK, 'Actual chunk gap peak exceeds .0005')
        quiet_peaks.append(actual_peak); gaps.append(gap)
    return error, quiet_peaks, gaps, short_guards


def quiet_edges(samples, sr):
    levels = np.max(np.abs(samples), axis=1) if samples.ndim == 2 else np.abs(samples)
    active = np.flatnonzero(levels > QUIET_PEAK)
    require(len(active) > 0, 'Final waveform has no active samples')
    return {'head_s': int(active[0])/sr, 'tail_s': (len(samples)-int(active[-1])-1)/sr,
            'rate': sr}


def punctuation_target(text):
    # Follow the EDL's current final-character rule; do not infer hidden boundaries.
    last = text.rstrip()[-1:]
    if last in ('，', ',', '：', ':', '；', ';'):
        return .18
    if last in ('。', '.', '？', '?', '！', '!'):
        return .35
    return 0.


def verify_edl_gaps(job, timeline, measured_edges):
    path = job/'outputs/landscape_compact/edit_decisions.json'
    if not path.exists():
        return {'status': 'not_available', 'reason': 'EDL not generated; no cross-row mix verification yet.'}
    edl = load(path); rows = edl['utterances']; clips = edl['clips']
    ids = [row['segment_id'] for row in rows]
    require(ids == [row['segment_id'] for row in timeline], 'EDL lost or reordered utterances')
    source = {row['segment_id']: row for row in timeline}
    lookup = {row['segment_id']: row for row in rows}
    for group in edl.get('join_groups', []):
        if all(identifier in lookup for identifier in group):
            require(len({lookup[identifier]['clip_index'] for identifier in group}) == 1,
                    'Declared join group is split across clips: '+str(group))
    grouped = {}; gaps = []; comma_count = 0; punctuation_count = 0; total_added = 0.
    for index, row in enumerate(rows):
        identifier = row['segment_id']; original = source[identifier]
        require(Path(row['audio']).resolve() == Path(original['audio']).resolve(), 'EDL selected different WAV: '+identifier)
        require(abs(row['speech_s']-original['duration_s']) <= 1e-7, 'EDL stale audio duration: '+identifier)
        group = grouped.setdefault(row['clip_index'], []); group.append(row)
        if index+1 == len(rows) or rows[index+1]['clip_index'] != row['clip_index']:
            require(abs(float(row.get('punctuation_gap_after_s', row.get('comma_gap_after_s', 0.)))) <= 1e-9,
                    'Group tail has a second internal punctuation gap: '+identifier)
            continue
        after = rows[index+1]
        target = punctuation_target(row['text_zh'])
        tail = float(row.get('edge_quiet_tail_s', 0.))
        head = float(after.get('edge_quiet_head_s', 0.))
        require(np.isfinite([head, tail]).all() and min(head, tail) >= 0., 'Invalid EDL quiet-edge durations')
        actual_tail = measured_edges[identifier]['tail_s']
        actual_head = measured_edges[after['segment_id']]['head_s']
        tol = 2/min(measured_edges[identifier]['rate'], measured_edges[after['segment_id']]['rate'])
        require(tail <= actual_tail+tol and head <= actual_head+tol,
                'EDL claims quiet protection absent in actual WAV: '+identifier)
        expected = max(0., target-tail-head) if target else 0.
        added = float(row.get('punctuation_gap_after_s', row.get('comma_gap_after_s', 0.)))
        require(abs(added-expected) <= 1e-7, 'Wrong cross-row punctuation gap: '+identifier)
        if 'boundary_pause_target_s' in row:
            require(abs(row['boundary_pause_target_s']-target) <= 1e-9, 'Wrong punctuation target: '+identifier)
        comma = row['text_zh'].rstrip().endswith(('，', ','))
        if 'comma_gap_after_s' in row:
            require(abs(row['comma_gap_after_s']-(expected if comma else 0.)) <= 1e-7,
                    'Comma-only metadata includes another punctuation mark: '+identifier)
        end = row['output_start_s']+row['speech_s']
        require(abs(row['output_speech_end_s']-end) <= 1e-7, 'Wrong EDL speech endpoint: '+identifier)
        require(abs(after['output_start_s']-end-added) <= 1e-7, 'Cross-row output start omits gap: '+identifier)
        require(abs(row['output_end_s']-end-added) <= 1e-7, 'Cross-row subtitle omits gap: '+identifier)
        if target:
            actual_total = actual_tail+actual_head+added
            require(actual_total >= target-tol, 'Actual quiet gap below punctuation target: '+identifier)
            # Extra naturally quiet source samples are acceptable; inserted silence
            # must still be exactly the deficit computed from protected edge guards.
            require(actual_total <= max(target, actual_tail+actual_head)+.004+tol,
                    'Cross-row gap unnecessarily enlarged: '+identifier)
            punctuation_count += 1; comma_count += int(comma)
            gaps.append({'id': identifier, 'after': after['segment_id'], 'start_s': end,
                         'stop_s': after['output_start_s'], 'added_s': added,
                         'actual_quiet_s': actual_total, 'target_s': target,
                         'source_start_s': row['output_start_s'], 'source_duration_s': row['speech_s']})
            total_added += added
    for clip_index, group in grouped.items():
        clip = clips[clip_index]
        generic = sum(float(row.get('punctuation_gap_after_s', row.get('comma_gap_after_s', 0.))) for row in group)
        commas = sum(float(row.get('comma_gap_after_s', 0.)) for row in group)
        require(abs(clip['speech_s']-sum(row['speech_s'] for row in group)-generic) <= 1e-6,
                'Clip duration omits punctuation silence')
        if 'internal_punctuation_gaps_s' in clip:
            require(abs(clip['internal_punctuation_gaps_s']-generic) <= 1e-7, 'Clip generic-gap summary mismatch')
        if 'internal_comma_gaps_s' in clip:
            require(abs(clip['internal_comma_gaps_s']-commas) <= 1e-7, 'Clip comma-gap summary mismatch')
        tail = clip['output_end_s']-group[-1]['output_speech_end_s']
        require(.35-1e-7 <= tail < .35+1/edl['fps']+1e-6, 'Group-tail pause missing or counted twice')
    result = {'status': 'passed', 'cross_row_punctuation_boundaries': punctuation_count,
              'cross_row_comma_boundaries': comma_count, 'cross_row_inserted_s': total_added,
              'dialogue_blank_gaps_checked': 0, 'maximum_dialogue_blank_gap_peak': 0.,
              'mix_gap_verification': 'not_available'}
    dialogue = path.parent/'zh_dialogue.wav'
    if dialogue.exists():
        with sf.SoundFile(dialogue) as mixed:
            require(mixed.samplerate == 48000, 'Dialogue mix is not 48kHz')
            require(abs(len(mixed)/mixed.samplerate-edl['duration_s']) <= 1/mixed.samplerate+1e-8,
                    'Dialogue WAV belongs to an older EDL duration')
            for gap in gaps:
                # Match the mixer's separately rounded placements exactly.
                lo = round(gap['source_start_s']*mixed.samplerate)+round(gap['source_duration_s']*mixed.samplerate)
                hi = round(gap['stop_s']*mixed.samplerate)
                require(hi >= lo, 'Punctuation rows overlap after 48k rounding: '+gap['id'])
                if hi == lo:
                    continue
                mixed.seek(lo); blank = mixed.read(hi-lo, dtype='float32', always_2d=True)
                require(len(blank) == hi-lo, 'Mix ends before punctuation gap: '+gap['id'])
                level = peak(blank)
                require(level == 0., 'EDL blank gap contains dialogue samples: '+gap['id'])
                result['maximum_dialogue_blank_gap_peak'] = max(result['maximum_dialogue_blank_gap_peak'], level)
                result['dialogue_blank_gaps_checked'] += 1
        result['mix_gap_verification'] = 'passed'
    return result


def check_current_text(row, placed, current, original):
    """Current reviewed segments own text; raw timeline remains an immutable audit."""
    require(row['text'] == placed['text_zh'] == current['text_zh'], 'Final/current text mismatch')
    if row['method'] != 'comma_chunked':
        require(current['text_zh'] == original['text_zh'],
                'Changed text must use regenerated fallback audio, not old DSP/no-comma audio')


def verify(job):
    audit = load(job/'punctuation_audit.json')
    timeline = load(job/'timeline.punctuated.json')['segments']
    raw_rows = load(job/'timeline.json')['segments']
    current_rows = load(job/'segments.json')['segments']
    current = {row['segment_id']: row for row in current_rows}
    selected = selected_reports(job)
    rows = audit['utterances']
    ids = [row['id'] for row in rows]
    expected_ids = [row['segment_id'] for row in raw_rows]
    require([row['segment_id'] for row in current_rows] == expected_ids, 'Current segments lost/reordered raw utterances')
    require(len(rows) == 135 and len(set(ids)) == 135, 'Expected 135 unique audit utterances')
    require(ids == expected_ids, 'Audit omitted/reordered original utterances')
    require([row['segment_id'] for row in timeline] == ids, 'Timeline omitted/reordered audit utterances')
    counts = dict(Counter(row['method'] for row in rows))
    require(counts == EXPECTED_COUNTS, 'Unexpected method counts: '+str(counts))
    require(audit['counts'] == counts, 'Audit count summary inconsistent')
    require(audit.get('utterance_count') == 135, 'Audit count field inconsistent')
    stats = {'utterances_checked': 0, 'method_counts': counts, 'output_sha256_checked': 0,
             'current_text_changes_vs_raw_timeline': sum(row['text_zh'] != current[row['segment_id']]['text_zh'] for row in raw_rows),
             'raw_tts_sha256_checked': 0, 'chunk_joins_checked': 0, 'dsp_commas_checked': 0,
             'dsp_added_seconds': 0., 'edge_silence_samples_removed': 0,
             'short_source_guard_edges': 0, 'maximum_pcm_reconstruction_error': 0.,
             'chunk_quiet_gap_seconds_min': None, 'chunk_quiet_gap_seconds_max': None,
             'maximum_chunk_quiet_gap_peak': 0.}
    failures = []; all_gaps = []; measured_edges = {}
    for row, placed, original in zip(rows, timeline, raw_rows):
        identifier = row['id']
        try:
            check_current_text(row, placed, current[identifier], original)
            method = row['method']
            report = selected[identifier]
            if method == 'comma_chunked':
                require(report['status'] == 'needs_tts_punctuation_regen', 'Chunk fallback not required by report')
                report = load(job/'tts/comma_chunks/final'/f'{identifier}.json')
                require(report['status'] == 'comma_chunked', 'Chunk alias not complete')
                require(report['raw_source_text'] == row['text'], 'Chunk alias text mismatch')
            else:
                require(report['status'] == method, 'Audit method/report mismatch')
                require(report['text'] == row['text'], 'DSP/no-comma report text mismatch')
                require(report['raw_cache_key'] == original['cache_key'], 'Raw TTS cache key changed')
            source_path = Path(row['pause_source_audio'])
            source_sha = digest(source_path)
            require(source_sha == row['pause_source_sha256'], 'Pause-source SHA mismatch')
            require(source_path.resolve() == Path(report['output_file']).resolve(), 'Wrong pause-source file')
            recorded_sha = report.get('output_sha256') or report.get('sha256') or report.get('raw_sha256')
            require(source_sha == recorded_sha, 'Selected report/output SHA mismatch')
            paused, sr = waveform(source_path)
            if method == 'comma_chunked':
                error, peaks, gaps, guards = verify_chunks(report, paused, sr)
                stats['chunk_joins_checked'] += len(gaps)
                stats['short_source_guard_edges'] += guards
                stats['maximum_chunk_quiet_gap_peak'] = max([stats['maximum_chunk_quiet_gap_peak']]+peaks)
                all_gaps.extend(gaps)
            elif method == 'repaired':
                error, added, commas = verify_dsp(report, paused, sr)
                stats['dsp_added_seconds'] += added; stats['dsp_commas_checked'] += commas
                stats['raw_tts_sha256_checked'] += 1
            else:
                require(source_sha == report['raw_sha256'], 'No-comma raw audio modified')
                require(source_path.resolve() == Path(report['raw_file']).resolve(), 'No-comma output is not raw cache')
                error = 0.; stats['raw_tts_sha256_checked'] += 1
            stats['maximum_pcm_reconstruction_error'] = max(stats['maximum_pcm_reconstruction_error'], error)
            final_path = Path(row['audio']); final_sha = digest(final_path)
            require(final_sha == row['sha256'] == placed['audio_sha256'], 'Final WAV SHA mismatch')
            require(final_path.resolve() == Path(placed['audio']).resolve(), 'Final timeline/audio path mismatch')
            final, final_sr = waveform(final_path)
            require(final_sr == sr, 'Edge trim changed sample rate')
            measured_edges[identifier] = quiet_edges(final, sr)
            edge = row['edge_silence_trim']
            result = check_trim(paused, sr, edge)
            rebuilt, rebuilt_info = trim_generated_silence(paused, sr)
            require(edge == rebuilt_info, 'Final edge metadata does not reconstruct')
            expected = rebuilt if result['trimmed_samples'] else paused
            edge_error = compare(expected, final, 'Final edge trim')
            stats['maximum_pcm_reconstruction_error'] = max(stats['maximum_pcm_reconstruction_error'], edge_error)
            stats['edge_silence_samples_removed'] += result['trimmed_samples']
            stats['short_source_guard_edges'] += result['short_source_guard_edges']
            require(abs(len(final)/sr-row['duration_s']) <= 1/sr+1e-9, 'Audit duration mismatch')
            require(abs(len(final)/sr-placed['duration_s']) <= 1/sr+1e-9, 'Timeline duration mismatch')
            require(abs(placed['render_end_s']-placed['render_start_s']-len(final)/sr) <= 1/sr+1e-9,
                    'Timeline uses pre-pause duration')
            stats['output_sha256_checked'] += 1; stats['utterances_checked'] += 1
        except Exception as error:
            failures.append({'id': identifier, 'error': str(error)})
    if all_gaps:
        stats['chunk_quiet_gap_seconds_min'] = min(all_gaps)
        stats['chunk_quiet_gap_seconds_max'] = max(all_gaps)
    edl_result = {'status': 'not_checked', 'reason': 'WAV checks failed'}
    if not failures:
        try:
            edl_result = verify_edl_gaps(job, timeline, measured_edges)
        except Exception as error:
            failures.append({'stage': 'edl_punctuation_gaps', 'error': str(error)})
            edl_result = {'status': 'failed'}
    return {'status': 'passed' if not failures and stats['utterances_checked'] == 135 else 'failed',
            'job': str(job), 'stats': stats, 'edl_punctuation_gaps': edl_result,
            'final_edl_mix_verified': edl_result.get('mix_gap_verification') == 'passed', 'failures': failures,
            'pcm_comparison_tolerance': PCM_TOL, 'quiet_gap_peak_limit': QUIET_PEAK,
            'gpu_models_loaded': False, 'asr_models_loaded': False,
            'subjective_full_listening_performed': False,
            'limits': ['Engineering waveform/hash checks do not establish speech accuracy, voice similarity or naturalness.',
                       '20ms guards are retained when present in the source; sources beginning/ending in speech may have less available silence.',
                       'DSP reconstruction preserves sample count and order but permits the recorded 2ms boundary fades.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    require(args.job.is_absolute(), '--job must be an absolute job directory')
    try:
        report = verify(args.job.resolve())
    except Exception as error:
        report = {'status': 'failed', 'job': str(args.job), 'failures': [{'error': str(error)}],
                  'subjective_full_listening_performed': False}
    report['verified_at_unix'] = time.time()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temp = args.output.with_suffix(args.output.suffix+'.partial')
    temp.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n'); temp.replace(args.output)
    print(json.dumps({'status': report['status'], 'stats': report.get('stats'),
                      'failures': report['failures'], 'output': str(args.output)}, ensure_ascii=False))
    sys.exit(0 if report['status'] == 'passed' else 1)


if __name__ == '__main__':
    main()
