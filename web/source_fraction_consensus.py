"""Narrow machine evidence for a decimal ASR error in a German fraction.

This does not approve a source edit or a translation.  The normal editor and
independent audit must still select the complete exact-window candidate.  It
never changes the general numeric guard: only the one proven decimal/fraction
pair may be masked before that guard runs.

``attach`` is called after the ordinary medium recheck; ``verify`` must be
called before source commit, while the original row is still current.  Saved
proofs retain the complete precommit input for later independent inspection.
"""
import collections
import copy
import hashlib
import math
from pathlib import Path
import re

import source_language_consensus as language
import website_recovery as recovery

SCHEMA = 'dual_cpu_decimal_fraction_v1'
OPTIONS = {'language': 'de', 'task': 'transcribe', 'vad_filter': False,
           'word_timestamps': True, 'beam_size': 5,
           'condition_on_previous_text': False}
_MARKER = 'ASRFRACTIONPAIR'
_DECIMAL = re.compile(r'(?<![\w.,])([1-9]),([2-9])(?![\w.,])(?=\s+(?:der|von)\b)', re.I)
_NUMERATORS = {'ein': 1, 'eins': 1, 'eine': 1, 'einen': 1, 'einem': 1,
               'einer': 1, 'eines': 1, 'zwei': 2, 'drei': 3, 'vier': 4,
               'fünf': 5, 'sechs': 6, 'sieben': 7, 'acht': 8, 'neun': 9}
_DENOMINATORS = {'halb': 2, 'halbe': 2, 'halben': 2, 'halbes': 2,
                 'hälfte': 2, 'hälften': 2, 'drittel': 3, 'viertel': 4,
                 'fünftel': 5, 'sechstel': 6, 'siebtel': 7, 'achtel': 8,
                 'neuntel': 9}
_FRACTION = re.compile(r'\b(' + '|'.join(_NUMERATORS) + r')\s+('
                        + '|'.join(_DENOMINATORS) + r')(n)?\b', re.I)
_QUALIFIERS = (r'\betwa\b', r'\bungefähr\b', r'\brund\b', r'\b(?:circa|ca)\b',
               r'\bfast\b', r'\bbis\s+zu\b', r'\büber\b', r'\bunter\b',
               r'\bmindestens\b', r'\bhöchstens\b', r'\bknapp\b', r'\bnur\b',
               r'\bmehr\s+als\b', r'\bweniger\s+als\b')


def _require(value, message):
    if not value:
        raise ValueError('分数原文复核：' + message)


def _sha(path):
    return recovery._sha(Path(path))


def _text_sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def mask_fraction_pair(original, candidate):
    """Return two span-masked full texts, or None for an ineligible pair.

    Only a single standalone ``d,d der/von`` proportion is eligible.  A money,
    unit or ordinary decimal is not.  There must be one matching written
    fraction, with the same single-digit numerator and denominator.  Every
    character outside the two spans survives unchanged.
    """
    if not all(isinstance(value, str) and value.strip() and len(value) <= 600
               and _MARKER not in value and not any(ord(c) < 32 and c not in '\t\n'
                                                   for c in value)
               for value in (original, candidate)):
        return None
    decimals, fractions = list(_DECIMAL.finditer(original)), list(_FRACTION.finditer(candidate))
    if len(decimals) != 1 or len(fractions) != 1:
        return None
    decimal, fraction = decimals[0], fractions[0]
    numerator = _NUMERATORS[fraction[1].casefold()]
    denominator = _DENOMINATORS[fraction[2].casefold()]
    if (int(decimal[1]), int(decimal[2])) != (numerator, denominator):
        return None
    # The candidate must remain a proportion, too: a fraction alone cannot
    # authorize changing the original referent into an amount or unit.
    after = candidate[fraction.end():]
    if not re.match(r'\s+(?:der|von)\b', after, re.I):
        return None
    return (original[:decimal.start()] + _MARKER + original[decimal.end():],
            candidate[:fraction.start()] + _MARKER + candidate[fraction.end():])


def _candidate_safe(original, candidate):
    masked = mask_fraction_pair(original, candidate)
    if masked is None:
        return False
    # Import only at call time: translation_recovery imports this module in
    # the normal candidate-validation path.
    from translation_recovery import source_alternative_errors
    if source_alternative_errors(*masked, 'de'):
        return False
    # Preserve added numbers as well as missing ones, even if the old generic
    # guard happens to normalize them to an existing mention.
    digits = lambda value: collections.Counter(re.findall(r'\d+', value))
    if digits(masked[0]) != digits(masked[1]):
        return False
    return all(len(re.findall(pattern, masked[0], re.I))
               == len(re.findall(pattern, masked[1], re.I)) for pattern in _QUALIFIERS)


def _base_record(record):
    return {key: copy.deepcopy(value) for key, value in record.items()
            if key not in {'fraction_proposal', 'excluded_source_choices'}}


def _installed_models(root, primary_path):
    hub = Path.home() / '.cache/huggingface/hub'
    medium_root = (hub / 'models--Systran--faster-whisper-medium/snapshots').resolve()
    small_root = (hub / 'models--Systran--faster-whisper-small/snapshots').resolve()
    primary = Path(primary_path).resolve()
    secondary = Path(recovery._read(root / 'config/local.json')['asr_model']).resolve()
    _require(primary.parent == medium_root and secondary.parent == small_root,
             '只复用已安装medium并使用已配置small，不下载模型')
    assets = [language._model_assets(root, path) for path in (primary, secondary)]
    _require(primary != secondary and _sha(primary / 'model.bin') != _sha(secondary / 'model.bin'),
             '两个识别器必须使用不同模型权重')
    return primary, secondary, assets


def _eligible_record(row, record, source_sha):
    _require(isinstance(record, dict), '源窗复核记录不是对象')
    candidate = record.get('alternatives', {}).get('exact')
    _require(record.get('alternatives', {}).get('original') == row['text_de']
        and record.get('original_text_sha256') == _text_sha(row['text_de'])
        and record.get('input_source_sha256') == source_sha
        and record.get('method') == 'local_cpu_asr_exact_and_contextual_windows_v1'
        and record.get('human_reviewed') is False and record.get('source_words_verified') is False
        and not record.get('language_proposal'), '不是当前原始行的普通机器复核')
    decoding = record.get('decoding')
    _require(isinstance(decoding, dict) and decoding.get('hotwords') == []
        and decoding.get('hinted_candidate_used') is False
        and decoding.get('human_verified') is False, '带词汇提示的复核不能变成无提示证据')
    exact = [item for item in record.get('evidence', [])
             if isinstance(item, dict) and item.get('kind') == 'exact']
    _require(len(exact) == 1 and exact[0].get('full_text') == candidate
        and exact[0].get('bounded_text', candidate) == candidate, '候选不是同一完整原窗全文')
    proof = exact[0]
    _require(not any(name in proof for name in ('asr_hotwords', 'initial_prompt', 'prefix',
                                              'prompted_machine_transcription')),
             '原窗带提示，不能形成分数例外')
    _require(_candidate_safe(row['text_de'], candidate), '分数以外的数字、限定或逻辑改变')
    return candidate, proof


def _current(root, folder, row, source_sha):
    result = language._current_source(root, folder, row, source_sha)
    root, folder, source, video, rows, binding = result
    _require(row.get('source_language', 'de') == 'de'
        and not language._protected_source(folder, row)
        and binding['row_identity'] == binding['immutable_original_row_identity'],
             '只允许未修订且未被人工确认的原始德语行')
    return result


def _observation_valid(observation, expected_path, expected_assets, duration):
    _require(isinstance(observation, dict) and observation.get('model_path') == str(expected_path)
        and observation.get('model_sha256') == _sha(expected_path / 'model.bin')
        and observation.get('model_assets') == expected_assets
        and observation.get('device') == 'cpu' and observation.get('compute_type') == 'int8'
        and observation.get('local_files_only') is True and observation.get('prompted') is False
        and observation.get('options') == OPTIONS
        and isinstance(observation.get('text'), str), '识别模型、参数或无提示声明不符')
    segments = observation.get('segments')
    _require(isinstance(segments, list) and 1 <= len(segments) <= 300
        and all(isinstance(item, dict) and isinstance(item.get('text'), str)
            and type(item.get('start_s')) in (int, float) and math.isfinite(item['start_s'])
            and type(item.get('end_s')) in (int, float) and math.isfinite(item['end_s'])
            and 0 <= item['start_s'] < item['end_s'] <= duration + .05 for item in segments)
        and all(right['start_s'] >= left['end_s'] - .05 for left, right in zip(segments, segments[1:]))
        and ''.join(item['text'] for item in segments).strip() == observation['text'],
             'small全文或识别段时间不是完整安全原窗')


def attach(root, folder, row, record, evidence_dir, source_sha):
    """Return a copied recheck record with a machine fraction proposal, if any.

    No eligible decimal/fraction pair means no extra recognition or model
    import.  A valid saved proposal is reused without another recognition.
    Integrity failures raise; a genuine secondary disagreement returns the
    unmodified copied record while preserving the machine observations.
    """
    result = copy.deepcopy(record)
    if not isinstance(record, dict) or not isinstance(row, dict):
        return result
    candidate = record.get('alternatives', {}).get('exact')
    if not _candidate_safe(row.get('text_de'), candidate):
        return result
    if record.get('fraction_proposal'):
        verify(root, folder, row, record, candidate, source_sha)
        return result
    root, folder, source, video, rows, binding = _current(root, folder, row, source_sha)
    candidate, exact = _eligible_record(row, record, source_sha)
    evidence_dir = Path(evidence_dir)
    _require(evidence_dir.is_absolute() and evidence_dir.is_relative_to(folder), '证据目录越界')
    evidence_dir = recovery._safe(folder, evidence_dir.relative_to(folder))
    _require(evidence_dir.is_relative_to(folder / 'revisions/source_review_v1/evidence'),
             '证据不属于当前源文复核目录')
    audio = language.validate_exact_source_window(root, folder, row, exact, source_sha, rows=rows)
    primary, secondary, assets = _installed_models(root, record.get('model_path', ''))
    _require(record.get('model_sha256') == _sha(primary / 'model.bin'), '既有medium模型SHA不符')
    from faster_whisper import WhisperModel
    model = WhisperModel(str(secondary), device='cpu', compute_type='int8', cpu_threads=2,
                         num_workers=1, local_files_only=True)
    segments, _ = model.transcribe(str(folder / audio['path']), **OPTIONS)
    segments = list(segments)
    observation = {'role': 'independent_small', 'model_path': str(secondary),
        'model_sha256': _sha(secondary / 'model.bin'), 'model_assets': assets[1],
        'text': ''.join(item.text for item in segments).strip(),
        'segments': [{'text': item.text, 'start_s': float(item.start), 'end_s': float(item.end)}
                     for item in segments], 'device': 'cpu', 'compute_type': 'int8',
        'local_files_only': True, 'prompted': False, 'options': dict(OPTIONS)}
    _observation_valid(observation, secondary, assets[1], audio['end_s'] - audio['start_s'])
    _require(_current(root, folder, row, source_sha)[-1] == binding
        and _installed_models(root, primary)[2] == assets, '识别期间原片、输入或模型改变')
    language.validate_exact_source_window(root, folder, row, exact, source_sha, rows=rows)
    from proxy_client import source_binding
    proof = {'schema': SCHEMA, 'segment_id': row['segment_id'],
        'input_source_sha256': source_sha, 'source_binding': source_binding(row),
        'input_binding': binding, 'precommit_source_rows': copy.deepcopy(rows),
        'base_record': _base_record(record), 'base_record_sha256': recovery._hash(_base_record(record)),
        'original_text_sha256': _text_sha(row['text_de']), 'candidate': candidate,
        'candidate_sha256': _text_sha(candidate), 'audio': audio,
        'medium': {'role': 'reused_medium_exact', 'model_path': str(primary),
                   'model_sha256': _sha(primary / 'model.bin'), 'model_assets': assets[0],
                   'text': candidate, 'window_proof': copy.deepcopy(exact),
                   'no_target_or_lexical_prompt': True},
        'small': observation, 'helper_sha256': _sha(__file__),
        'recheck_helper_sha256': _sha(Path(__file__).with_name('source_audio_recheck.py')),
        'human_reviewed': False, 'source_words_verified': False}
    path = evidence_dir / (row['segment_id'] + '_fraction_observations_'
                           + recovery._hash(proof)[:16] + '.json')
    language._save_once(path, proof)
    if not _candidate_safe(row['text_de'], observation['text']):
        return result
    # Both observations must identify the same fraction; the small full text
    # is evidence, never a selectable replacement for the medium full text.
    result['fraction_proposal'] = {'schema': SCHEMA, 'candidate': candidate,
        'fraction_evidence': {'path': str(path.relative_to(folder)), 'sha256': _sha(path)},
        'human_reviewed': False, 'source_words_verified': False}
    verify(root, folder, row, result, candidate, source_sha)
    return result


def verify(root, folder, row, record, text, source_sha):
    """Validate a current precommit proposal; return False only if absent.

    Invalid or stale claimed evidence raises ValueError, rather than silently
    falling back to a numeric exemption.  This function runs no recognizer.
    """
    if not isinstance(record, dict) or not record.get('fraction_proposal'):
        return False
    root, folder, source, video, rows, binding = _current(root, folder, row, source_sha)
    candidate, exact = _eligible_record(row, record, source_sha)
    proposal = record['fraction_proposal']
    _require(isinstance(proposal, dict) and proposal.get('schema') == SCHEMA
        and proposal.get('candidate') == candidate == text
        and proposal.get('human_reviewed') is False and proposal.get('source_words_verified') is False,
             '选用文本或机器候选声明不符')
    path = language._reference(folder, proposal.get('fraction_evidence'),
                               folder / 'revisions/source_review_v1/evidence')
    proof = recovery._read(path)
    from proxy_client import source_binding
    _require(isinstance(proof, dict) and proof.get('schema') == SCHEMA
        and proof.get('segment_id') == row['segment_id']
        and proof.get('input_source_sha256') == source_sha
        and proof.get('source_binding') == source_binding(row)
        and proof.get('input_binding') == binding and proof.get('precommit_source_rows') == rows
        and proof.get('base_record') == _base_record(record)
        and proof.get('base_record_sha256') == recovery._hash(_base_record(record))
        and proof.get('original_text_sha256') == _text_sha(row['text_de'])
        and proof.get('candidate') == candidate and proof.get('candidate_sha256') == _text_sha(candidate)
        and proof.get('helper_sha256') == _sha(__file__)
        and proof.get('recheck_helper_sha256') == _sha(Path(__file__).with_name('source_audio_recheck.py'))
        and proof.get('human_reviewed') is False and proof.get('source_words_verified') is False,
             '原始行、完整输入、原复核记录或检查代码改变')
    audio = language.validate_exact_source_window(root, folder, row, exact, source_sha, rows=rows)
    _require(proof.get('audio') == audio, '原窗路径、时间、SHA或真实PCM改变')
    primary, secondary, assets = _installed_models(root, record.get('model_path', ''))
    medium = proof.get('medium')
    _require(isinstance(medium, dict) and medium == {
        'role': 'reused_medium_exact', 'model_path': str(primary),
        'model_sha256': _sha(primary / 'model.bin'), 'model_assets': assets[0],
        'text': candidate, 'window_proof': exact, 'no_target_or_lexical_prompt': True}
        and record.get('model_sha256') == _sha(primary / 'model.bin'),
             '既有medium全文、模型或音窗改变')
    small = proof.get('small')
    _observation_valid(small, secondary, assets[1], audio['end_s'] - audio['start_s'])
    _require(small.get('role') == 'independent_small'
        and _candidate_safe(row['text_de'], small['text']), 'small与medium分数或其他事实不一致')
    _require(_current(root, folder, row, source_sha)[-1] == binding
        and _installed_models(root, primary)[2] == assets, '验证期间原片、输入或模型改变')
    return True
