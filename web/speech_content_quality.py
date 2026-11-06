"""Serial, target-free CPU screening of every complete generated utterance.

This is content/PCM evidence, not a measurement of perceived voice identity,
emotion, pronunciation quality or human listening acceptance. The thresholds
are conservative engineering policy, not a calibrated accuracy guarantee.
"""
import copy
from decimal import Decimal
import difflib
import json
import math
import os
from pathlib import Path
import re
import time
import unicodedata

import generic_group_alignment as alignment
import media_recovery as media

POLICY = 'layered_full_content_v1'
EVALUATION_SCHEMA = 'target_free_full_utterance_content_evaluation_v1'
THRESHOLDS = {'pass_similarity': .85, 'pass_target_coverage': .85,
    'major_missing_characters': 6, 'major_missing_fraction': .25,
    'repeated_extra_characters': 6, 'severe_similarity': .4,
    'confident_word_probability': .65, 'cross_model_similarity': .7,
    'silence_dbfs': -55., 'near_silence_dbfs': -65., 'silent_fraction': .98,
    'clipped_fraction': .05, 'continuous_clip_s': .06,
    'internal_silence_min_s': 3., 'internal_silence_fraction': .35}
_DIGITS = {'零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4,
    '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
_UNITS = {'十': 10, '百': 100, '千': 1000, '万': 10000, '亿': 100000000}


def _chinese_number(value):
    """Only normalize number glyphs, without changing units or comparison words."""
    if '点' in value and value[-1:] in ('万', '亿'):
        number = Decimal(_chinese_number(value[:-1])) * _UNITS[value[-1]]
        return format(number.normalize(), 'f')
    sign = '-' if value.startswith('负') else ''
    value = value.removeprefix('负')
    pieces = value.split('点')
    if len(pieces) > 2 or not pieces[0]:
        return value
    whole = pieces[0]
    if not any(c in _UNITS for c in whole):
        number = ''.join(str(_DIGITS.get(c, c)) for c in whole)
        number = number.lstrip('0') or '0'
    else:
        total, section, digit = 0, 0, 0
        for chunk in re.findall(r'[0-9]+|.', whole):
            if chunk.isdigit() or chunk in _DIGITS:
                digit = int(chunk) if chunk.isdigit() else _DIGITS[chunk]
            else:
                unit = _UNITS[chunk]
                if unit < 10000:
                    section += (digit or 1) * unit
                elif unit == 10000:
                    total += (section + digit or 1) * unit
                    section = 0
                else:
                    total = (total + section + digit or 1) * unit
                    section = 0
                digit = 0
        number = str(total + section + digit)
    if len(pieces) == 2 and pieces[1] and all(c in _DIGITS or c.isdigit() for c in pieces[1]):
        number += '.' + ''.join(str(_DIGITS.get(c, c)) for c in pieces[1])
    return sign + number


def normalizer(root):
    # Load the existing text-only OpenCC dependency through the established
    # local environment lookup, then retain number punctuation until parsed.
    alignment.normalize_factory(root)
    from opencc import OpenCC
    convert = OpenCC('t2s').convert
    def normalize(text):
        text = re.sub(r'<([^<>|]+)\|[^<>]*>', lambda match: match.group(1), text)
        text = unicodedata.normalize('NFKC', convert(text)).lower()
        text = re.sub(r'(?<=\d),(?=\d{3}(?:\D|$))', '', text)
        text = re.sub(r'(-?\d+(?:\.\d+)?)\s*%', lambda match: '百分之' + match.group(1), text)
        text = re.sub(r'(?<=\d)\.(?=\d)', '点', text)
        text = re.sub(r'-(?=\d)', '负', text)
        text = re.sub(r'零下(?=[零〇一二两三四五六七八九十百千万亿0-9])', '负', text)
        text = re.sub(r'负?[零〇一二两三四五六七八九十百千万亿0-9]+(?:点[零〇一二两三四五六七八九0-9]+[万亿]?)?',
                      lambda match: _chinese_number(match.group()), text)
        text = re.sub(r'(?<=\d)到(?=-?\d)', '至', text)
        return ''.join(char for index, char in enumerate(text) if char.isalnum()
            or char == '.' and 0 < index < len(text) - 1 and text[index - 1].isdigit() and text[index + 1].isdigit()
            or char == '-' and index + 1 < len(text) and text[index + 1].isdigit())
    return normalize


def _check(value, message):
    media.require(value, 'Speech content: ' + message)


def root_file(root, path):
    """A project path still rejects traversal and every symlink component."""
    return media.job_file(Path(root).resolve(), path)


def _native_child(root, folder, plan, plan_path, manifest):
    """Shared content is reusable; source and permission remain job-local."""
    from generic_group_plan import CACHE_REUSE_POLICY, SHARED_CACHE_NAMESPACE
    child = plan.get('tts_job', {})
    _check(isinstance(child, dict) and child.get('parent_job') == folder.name
        and child.get('id') == child.get('job')
        and isinstance(child.get('id'), str) and re.fullmatch(r'web_ttsgroups_[a-f0-9]{24}', child['id']),
        'Shared native manifest lacks the current materialized child')
    directory = root / 'jobs' / child['id']
    cache = root / 'cache' / SHARED_CACHE_NAMESPACE
    _check(child.get('directory') == str(directory) and child.get('cache_dir') == str(cache)
        and child.get('cache_reuse_policy') == CACHE_REUSE_POLICY
        and child.get('processing_permission_path') == str(folder / 'processing_request.json')
        and media.sha(root_file(root, child['processing_permission_path'])) == child.get('processing_permission_sha256'),
        'Shared cache source/permission scope changed')
    expected_binding = {'schema': 'generic_group_tts_child_binding_v1', 'parent_job': folder.name,
        'group_plan_path': str(plan_path), 'group_plan_sha256': media.sha(plan_path),
        **{name: child.get(name) for name in ('cache_scope_key', 'profile', 'profile_options',
            'cache_dir', 'plan_provenance_key', 'cache_reuse_policy')}}
    _check(media.read(root_file(root, directory / 'group_plan_binding.json')) == expected_binding,
        'Shared native child belongs to another plan/source')
    _check(media.read(root_file(root, directory / 'segments.json')) == {
        'schema_version': 2, 'segments': [group['tts_row'] for group in plan['tts_groups']],
        'parent_job': folder.name, 'human_reviewed': False}, 'Shared native child source rows changed')
    _check(media.read(root_file(root, directory / 'source.json')) == media.read(folder / 'source.json'),
        'Shared native child original source changed')
    speakers = media.read(folder / 'speakers.json')
    selected = {group['speaker_id'] for group in plan['tts_groups']}
    _check(media.read(root_file(root, directory / 'speakers.json')) == {
        sid: value for sid, value in speakers.items() if sid in selected}, 'Shared native child speaker binding changed')
    permission = media.read(root_file(root, child['processing_permission_path']))
    _check(permission.get('processing_authorized') is True and permission.get('scope') == 'private_local_evaluation',
        'Shared native child processing permission missing')
    permission.update(parent_job=folder.name, child_role='semantic_groups_tts_only',
        group_plan_path=str(plan_path), group_plan_sha256=media.sha(plan_path))
    _check(media.read(root_file(root, directory / 'processing_request.json')) == permission,
        'Shared native child processing scope changed')
    _check(manifest.get('schema') == 'native_mlx_whole_utterance_v1'
        and manifest.get('job') == child['id'] and manifest.get('cache_root') == str(cache)
        and manifest.get('pending_unique_keys') == 0
        and manifest.get('selected_utterances') == len(plan['tts_groups']), 'Shared native manifest belongs to another TTS source')
    for index, (group, entry) in enumerate(zip(plan['tts_groups'], manifest['utterances'])):
        _check(entry.get('source_index') == index and entry.get('source_start_s') == group['source_start_s']
            and entry.get('source_end_s') == group['source_end_s']
            and entry.get('reference') == group['reference']['path']
            and entry.get('content', {}).get('parameters') == manifest.get('parameters')
            and (entry['content']['parameters'].get('experimental_profile') or {}).get('name') == child.get('profile'),
            'Shared native row/voice profile differs from the current source plan')


def validated_manifest(root, folder, plan, plan_path, manifest_path):
    manifest_path = media.job_file(folder, manifest_path)
    manifest = media.read(manifest_path)
    _check(manifest.get('phase') == 'complete'
        and [entry.get('segment_id') for entry in manifest.get('utterances', [])]
            == [group['group_id'] for group in plan['tts_groups']], 'Incomplete ordered group coverage')
    from user_voice_adoption_20261103 import SCHEMA as USER_AUDIO_SCHEMA, validate_manifest as validate_user_manifest
    if manifest.get('schema') == USER_AUDIO_SCHEMA:
        validate_user_manifest(root,folder,plan_path,manifest_path)
        return manifest
    import native_generation_recovery_20261105 as generation_recovery
    if manifest.get('schema') == generation_recovery.SCHEMA:
        generation_recovery.validate_manifest(root,folder,plan_path,manifest_path)
        return manifest
    shared = any(not Path(entry['audio']).is_relative_to(folder) for entry in manifest['utterances'])
    if not shared:
        return manifest
    if manifest.get('schema') == 'local_acoustic_fallback_composition_v1':
        base = manifest_path
        visited = set()
        for _ in range(media.MAX_RECOVERIES + 1):
            _check(base not in visited, 'Cyclic shared sound composition')
            visited.add(base)
            item = media.read(base)
            if item.get('schema') != 'local_acoustic_fallback_composition_v1':
                break
            base = media.checked_record(folder, item.get('source_manifests', {}).get('base'))
        else:
            raise ValueError('Speech content: Shared sound composition exceeds the recovery budget')
        if media.read(base).get('schema') == generation_recovery.SCHEMA:
            generation_recovery.validate_manifest(root,folder,plan_path,base)
        else:
            _native_child(root, folder, plan, plan_path, media.read(base))
        from acoustic_fallback import resolve
        _check(resolve(root, folder, plan_path, base) == manifest_path,
            'Shared Torch audio is not the current source-bound recovery selection')
    else:
        _native_child(root, folder, plan, plan_path, manifest)
    return manifest


def bound_audio(root, folder, plan, plan_path, manifest_path, group, native):
    """Admit only current-job WAVs or exact registered shared cache keys."""
    path = root_file(root, native['audio'])
    root_file(root, path.with_suffix('.json'))
    root_file(root, group['reference']['path'])
    if not path.is_relative_to(folder):
        from early_voice_pilot import _native_runtime
        schema = native.get('content', {}).get('schema')
        key = native.get('cache_key')
        _check(isinstance(key, str) and media.SHA.fullmatch(key)
            and key == alignment.key(native.get('content')), 'Shared generated content key changed')
        if schema == 'native_mlx_whole_utterance_v1':
            directory = root / 'cache/native_mlx_semantic_groups_v2/audio'
            _check(path == directory / (key + '.wav'), 'Native WAV is not its registered shared key path')
            # This imports only the deployed stdlib cache validator, never MLX/Torch.
            with _native_runtime(Path(__file__).resolve().parents[1]) as runtime:
                entry, reason = runtime.cached_entry(directory, key, native['content'])
        elif schema == 'continuous_utterance_v1':
            directory = root / 'cache/continuous_tts_v1'
            _check(path == directory / (key + '.wav'), 'Torch WAV is not its registered shared key path')
            with _native_runtime(Path(__file__).resolve().parents[1]):
                from continuous_tts import cached_entry
                entry, reason = cached_entry(directory, key, native['content'])
        else:
            raise ValueError('Speech content: Unknown shared cache generation schema')
        _check(entry is not None and entry.get('sha256') == native.get('sha256'),
            'Original shared cache validator rejected audio: ' + str(reason))
    return alignment.audio_binding(group, native, manifest_path,plan_path=plan_path,job=plan['job'])


def profile_for_model(root, model_path, tier):
    """Hash only locally present model assets; never accepts a model repository ID."""
    model = Path(model_path).resolve()
    _check(tier in {'small', 'medium'} and model.is_dir() and (model / 'model.bin').is_file(),
           'Installed local ' + tier + ' model is missing; no download permitted')
    _check(any(re.search(r'(^|[-_])' + tier + r'($|[-_])', part, re.I) for part in model.parts),
           'Expected local ' + tier + ' model')
    files = tuple((p.name, p.stat().st_size, p.stat().st_mtime_ns)
                  for p in sorted(model.iterdir()) if p.is_file() and not p.name.startswith('.'))
    assets = [{'name': name, 'bytes': size, 'sha256': digest}
              for name, size, digest in alignment._model_asset_hashes(str(model), files)]
    return {'schema': alignment.ASR_SCHEMA, 'model_path': str(model), 'model_assets': assets,
        'device': 'cpu', 'compute_type': 'int8', 'cpu_threads': 2, 'num_workers': 1,
        'local_files_only': True, 'target_conditioning': False,
        'transcribe_options': dict(alignment.OPTIONS), 'versions': alignment.environment_versions(root)}


def local_model(root, tier):
    if tier == 'small':
        path = Path(media.read(Path(root) / 'config/local.json')['asr_model']).resolve()
        profile_for_model(root, path, tier)
        return path
    candidates = sorted((Path.home() / '.cache/huggingface/hub/models--Systran--faster-whisper-medium/snapshots').glob('*'))
    return next((path for path in candidates if (path / 'model.bin').is_file()), None)


class Observer(alignment.Observer):
    """One CPU/int8 recognizer per tier; unload small before loading medium."""
    def __init__(self, root, cache_dir, tier='small', model_path=None):
        super().__init__(root, cache_dir)
        self.tier, self.model_path = tier, model_path
        self.invalid_cache_records = []

    def observe(self, audio):
        if self.profile is None:
            model = self.model_path or local_model(self.root, self.tier)
            _check(model is not None, 'Installed medium model unavailable')
            self.profile = profile_for_model(self.root, model, self.tier)
        signature = alignment.key({'audio_sha256': audio['sha256'], 'profile': self.profile})
        path = media.job_file(self.root / 'jobs', self.cache / (signature + '.json'), False)
        if path.is_file():
            digest = media.sha(path)
            try:
                validate_observation(media.read(path), audio, self.tier)
            except (ValueError, KeyError, TypeError):
                # One invalid entry is retained as evidence; the ordinary
                # recognition below runs once, rather than failing forever on
                # every subsequent continuation or inventing an approval.
                destination = media.job_file(self.root / 'jobs',
                    self.cache / 'invalid' / (signature + '_' + digest + '.json'), False)
                destination.parent.mkdir(parents=True, exist_ok=True)
                _check(media.sha(path) == digest, 'ASR cache changed while preserving invalid evidence')
                _check(not destination.exists() or media.sha(destination) == digest, 'Invalid-cache history changed')
                os.replace(path, destination)
                self.invalid_cache_records.append({'path': str(destination), 'sha256': digest})
        return super().observe(audio)

    def release(self):
        self.model = None


def validate_observation(observation, audio, tier, root=None, check_assets=False):
    profile = observation.get('profile', {})
    _check(profile.get('device') == 'cpu' and profile.get('compute_type') == 'int8'
        and profile.get('local_files_only') is True and profile.get('target_conditioning') is False
        and profile.get('transcribe_options') == alignment.OPTIONS,
        'Recognition must be local CPU/int8 without target, prompt, prefix or hotwords')
    _check(any(re.search(r'(^|[-_])' + tier + r'($|[-_])', part, re.I)
        for part in Path(profile.get('model_path', '')).parts), 'Incorrect recognizer tier')
    alignment.validate_observation(observation, audio, profile)
    assets = profile.get('model_assets', [])
    _check(any(asset.get('name') == 'model.bin' and media.SHA.fullmatch(str(asset.get('sha256', '')))
        for asset in assets), 'Model asset identity missing')
    if check_assets:
        _check(root is not None and profile == profile_for_model(root, profile['model_path'], tier),
               'Recognition profile/assets no longer match the local model')
    previous = 0.
    for segment in observation['segments']:
        _check(all(isinstance(segment.get(name), (int, float)) and math.isfinite(segment[name])
            for name in ('start_s', 'end_s', 'avg_logprob', 'no_speech_prob')),
            'Missing finite recognition confidence/time evidence')
        _check(0 <= segment['start_s'] <= segment['end_s'] <= audio['duration_s'] + .1
            and 0 <= segment['no_speech_prob'] <= 1, 'Recognition times/probabilities outside the utterance')
        for word in segment['words']:
            _check(0 <= word['start_s'] <= word['end_s'] <= audio['duration_s'] + .1
                and 0 <= word['probability'] <= 1 and word['start_s'] >= previous - .02,
                'Recognition word geometry invalid')
            previous = word['start_s']


_CLOCK_POLICY = 'target_anchored_clock_equivalence_v1'
_CLOCK_LITERAL = re.compile(r'(?<![0-9:])([0-9]{2}):([0-9]{2})(?![0-9:])')
_CLOCK_NUMBER_EDGE = '0-9零〇一二两兩三四五六七八九十.负+\-'
_CLOCK_NON_TIME_UNIT = re.compile(
    r'^(?:元|欧元|美元|英镑|日元|万元|亿元|人|名|个|次|岁|年|月|日|'
    r'公斤|千克|克|公里|千米|米|升|毫升|吨|平方米|立方米|度|小时|分钟|%|％)')
_CLOCK_NON_TIME_LABEL = re.compile(
    r'(?:金额|价格|票价|售价|收入|费用|成本|预算|支付|花费|罚款|价值|总价|人数|数量|'
    r'型号|编号|代码|比例|比值|比分|赔率|概率|经度|纬度|章节)(?:约|共|为|是)*$')
_CLOCK_CURRENCY_WORD = re.compile(
    r'(?:人民币|美元|欧元|英镑|日元|瑞郎|港元|港币|新台币|台币|韩元|卢布|'
    r'澳元|加元|新西兰元|新加坡元|印度卢比|格里夫纳)(?:约|约为|共|为|是|约合)*$')
_CLOCK_CURRENCY_CODES = r'(?:USD|EUR|GBP|JPY|CNY|RMB|CHF|HKD|TWD|KRW|RUB|AUD|CAD|NZD|SGD|INR|UAH)'


def _clock_currency_context(before, after, normalize):
    """Explicit money immediately around a number cannot establish a clock."""
    before, after = before.rstrip(), after.lstrip()
    if before[-1:] in ('$', '€', '£', '¥', '￥') or after[:1] in ('$', '€', '£', '¥', '￥'):
        return True
    # A punctuation boundary ends the currency prefix: a sentence about an
    # exchange rate may still legitimately contain a later broadcast time.
    chinese = re.search(r'[\u3400-\u9fff]+$', before)
    return bool(chinese and _CLOCK_CURRENCY_WORD.search(normalize(chinese.group()))
        or re.search(r'(?<![A-Za-z])' + _CLOCK_CURRENCY_CODES + r'\s*$', before, re.I)
        or re.match(_CLOCK_CURRENCY_CODES + r'(?![A-Za-z])', after, re.I))


def _clock_chinese(value):
    digits = '零一二三四五六七八九'
    if value < 10:
        return digits[value]
    tens, ones = divmod(value, 10)
    return ('' if tens == 1 else digits[tens]) + '十' + (digits[ones] if ones else '')


def _clock_context_matches(target, target_span, recognized, actual_span, normalize):
    """Use the same utterance location, never an unrelated matching amount."""
    a, b = target_span; c, d = actual_span
    left, other_left = normalize(target[:a]), normalize(recognized[:c])
    right, other_right = normalize(target[b:]), normalize(recognized[d:])
    if not (left or other_left or right or other_right):
        return True  # The complete target is explicitly one clock time.
    return (len(left) >= 2 and len(other_left) >= 2 and left[-2:] == other_left[-2:]
        or len(right) >= 2 and len(other_right) >= 2 and right[:2] == other_right[:2])


def _clock_equivalent_texts(target, recognized, normalize):
    """Pair only legal, explicit target HH:MM with equal clocks in context.

    This derives comparison text after target-free ASR. It never alters the
    recognizer input, raw words, original normalized evidence or other numbers.
    """
    target = unicodedata.normalize('NFKC', target)
    recognized = unicodedata.normalize('NFKC', recognized)
    targets = list(_CLOCK_LITERAL.finditer(target))
    if not targets:
        return target, recognized, None
    if [match.group() for match in _CLOCK_LITERAL.finditer(recognized)] == [match.group() for match in targets]:
        # No clock-format equivalence is needed when both texts already keep
        # the same explicit literals; preserve the existing comparison policy.
        return target, recognized, None
    values = [(int(match[1]), int(match[2])) for match in targets]
    evidence = {'policy': _CLOCK_POLICY, 'applied': False,
        'target_values': [list(value) for value in values], 'recognized_values': [],
        'mismatch': True, 'matched_occurrences': []}
    if any(hour > 23 or minute > 59 for hour, minute in values):
        evidence['reason'] = 'invalid_explicit_target_clock'
        return target, recognized, evidence
    found = {}
    for target_match, (hour, minute) in zip(targets, values):
        target_before = target[:target_match.start()]
        target_after = target[target_match.end():]
        if (_clock_currency_context(target_before, target_after, normalize)
                or re.fullmatch(r'[A-Za-z]', target_before[-1:])
                or re.fullmatch(r'[A-Za-z]', target_after[:1])
                or _CLOCK_NON_TIME_LABEL.search(normalize(target_before))
                or _CLOCK_NON_TIME_UNIT.match(target[target_match.end():].lstrip())):
            evidence['reason'] = 'explicit_target_number_has_non_clock_context'
            return target, recognized, evidence
        minute_words = '零零' if minute == 0 else '零' + _clock_chinese(minute) if minute < 10 else _clock_chinese(minute)
        spoken = _clock_chinese(hour) + minute_words
        aliases = {f'{hour:02d}:{minute:02d}', f'{hour:02d}-{minute:02d}', f'{hour:02d}{minute:02d}', spoken}
        # A concatenation which is also an ordinary short cardinal, such as
        # 二十一 for 02:11, does not establish a spoken hour/minute boundary.
        if re.fullmatch(r'[一二两三四五六七八九]?十[一二两三四五六七八九]?', spoken):
            aliases.discard(spoken)
        pattern = re.compile(r'(?<![' + _CLOCK_NUMBER_EDGE + r'])(?:'
            + '|'.join(re.escape(alias) for alias in sorted(aliases, key=len, reverse=True))
            + r')(?![' + _CLOCK_NUMBER_EDGE + r'])')
        for actual in pattern.finditer(recognized):
            before = recognized[:actual.start()].rstrip()
            after = recognized[actual.end():].lstrip()
            if (_clock_currency_context(before, after, normalize)
                    or re.fullmatch(r'[A-Za-z]', recognized[actual.start()-1:actual.start()])
                    or re.fullmatch(r'[A-Za-z]', recognized[actual.end():actual.end()+1])
                    or _CLOCK_NON_TIME_LABEL.search(normalize(before))
                    or _CLOCK_NON_TIME_UNIT.match(after)):
                continue
            if not _clock_context_matches(target, target_match.span(), recognized, actual.span(), normalize):
                continue
            found[actual.span()] = (hour, minute)
    occurrences = sorted(found.items())
    evidence['recognized_values'] = [list(value) for _, value in occurrences]
    if [value for _, value in occurrences] != values:
        evidence['reason'] = 'clock_values_or_occurrence_counts_differ'
        return target, recognized, evidence
    evidence.update(applied=True, mismatch=False,
        matched_occurrences=[{'target': match.group(), 'recognized': recognized[left:right],
            'value': list(value)} for match, ((left, right), value) in zip(targets, occurrences)])
    for match, (hour, minute) in reversed(list(zip(targets, values))):
        target = target[:match.start()] + f'{hour:02d}{minute:02d}' + target[match.end():]
    for (left, right), (hour, minute) in reversed(occurrences):
        recognized = recognized[:left] + f'{hour:02d}{minute:02d}' + recognized[right:]
    return target, recognized, evidence


def text_evidence(target, observation, normalize):
    recognized = ''.join(segment['text'] for segment in observation['segments'])
    legacy_expected, legacy_actual = normalize(target), normalize(recognized)
    comparison_target, comparison_recognized, clock = _clock_equivalent_texts(target, recognized, normalize)
    expected, actual = normalize(comparison_target), normalize(comparison_recognized)
    _check(bool(expected), 'Empty normalized target')
    matcher = difflib.SequenceMatcher(None, expected, actual, autojunk=False)
    matches = sum(block.size for block in matcher.get_matching_blocks())
    edits = [{'operation': op, 'target_start': a, 'target_end': b,
        'target': expected[a:b], 'recognized': actual[c:d]}
        for op, a, b, c, d in matcher.get_opcodes() if op != 'equal']
    missing = sum(len(edit['target']) for edit in edits if edit['operation'] == 'delete')
    extra = sum(len(edit['recognized']) for edit in edits if edit['operation'] == 'insert')
    words = [word for segment in observation['segments'] for word in segment['words']]
    weights = [max(1, len(normalize(word['word']))) for word in words]
    confidence = (sum(word['probability'] * weight for word, weight in zip(words, weights)) /
                  sum(weights)) if weights else 0.
    isolated_substitutions = bool(edits) and all(edit['operation'] == 'replace'
        and max(len(edit['target']), len(edit['recognized'])) <= 2 for edit in edits) and len(edits) <= 2
    numeric_mismatch = (bool(clock and clock['mismatch']) or
        re.findall(r'-?\d+(?:\.\d+)?', expected) != re.findall(r'-?\d+(?:\.\d+)?', actual))
    negation_mismatch = any(('不' in edit['target'] or '没' in edit['target'] or '无' in edit['target']
        or '不' in edit['recognized'] or '没' in edit['recognized'] or '无' in edit['recognized']) for edit in edits)
    repeated = [edit['recognized'] for edit in edits if edit['operation'] == 'insert'
        and len(edit['recognized']) >= THRESHOLDS['repeated_extra_characters']
        and edit['recognized'] in expected]
    evidence = {'recognized': recognized, 'normalized_target': expected, 'normalized_recognized': actual,
        'similarity': matcher.ratio(), 'target_coverage': matches / len(expected),
        'recognized_precision': matches / len(actual) if actual else 0.,
        'missing_characters': missing, 'extra_characters': extra, 'edits': edits,
        'word_probability': confidence, 'isolated_substitutions': isolated_substitutions,
        'numeric_mismatch': numeric_mismatch, 'negation_mismatch': negation_mismatch,
        'repeated_target_spans': repeated, 'word_evidence_present': bool(words)}
    if clock is not None:
        evidence.update(clock_equivalence=clock, legacy_normalized_target=legacy_expected,
            legacy_normalized_recognized=legacy_actual)
    return evidence


def objective_findings(metrics, duration):
    faults, advisories = [], []
    if metrics.get('rms_dbfs', 0.) <= THRESHOLDS['near_silence_dbfs'] and metrics.get('silent_fraction', 0.) >= THRESHOLDS['silent_fraction']:
        faults.append('near_silent_complete_pcm')
    if metrics.get('clipped_fraction', 0.) >= THRESHOLDS['clipped_fraction'] or metrics.get('longest_clipped_run_s', 0.) >= THRESHOLDS['continuous_clip_s']:
        faults.append('severe_full_scale_clipping')
    if metrics.get('longest_internal_silence_s', 0.) >= max(THRESHOLDS['internal_silence_min_s'], THRESHOLDS['internal_silence_fraction'] * duration):
        advisories.append('abnormally_long_internal_silence')
    if metrics.get('acoustically_suspicious'):
        advisories.append('low_energy_dynamic_range')
    return faults, advisories


def evaluate_group(target, audio, metrics, small, medium=None, normalize=None):
    """Deterministic decision, replayed against current PCM and saved observations."""
    _check(normalize is not None, 'A versioned script/number normalizer is required')
    first = text_evidence(target, small, normalize)
    second = text_evidence(target, medium, normalize) if medium is not None else None
    faults, advisories = objective_findings(metrics, audio['duration_s'])
    pass_text = lambda evidence: (evidence['similarity'] >= THRESHOLDS['pass_similarity']
        and evidence['target_coverage'] >= THRESHOLDS['pass_target_coverage']
        and evidence['missing_characters'] <= 2 and evidence['extra_characters'] <= 2
        and not evidence['repeated_target_spans']
        and not evidence['numeric_mismatch'] and not evidence['negation_mismatch'])
    benign = lambda evidence: (evidence['isolated_substitutions'] and evidence['similarity'] >= .7
        and not evidence['numeric_mismatch'] and not evidence['negation_mismatch'])
    if benign(first):
        advisories.append('isolated_asr_spelling_or_name_difference')
    requires_medium = bool(not faults and not pass_text(first) and not benign(first))
    reasons, status = list(faults), 'machine_content_screen_passed'
    if faults:
        status = 'needs_repair'
    elif pass_text(first) or benign(first):
        pass
    elif second is None:
        status = 'needs_review'
        reasons.append('second_local_recognizer_required')
    elif pass_text(second) or benign(second):
        advisories.append('small_asr_disagreed_medium_recovered_target')
    else:
        agreement = difflib.SequenceMatcher(None, first['normalized_recognized'],
            second['normalized_recognized'], autojunk=False).ratio()
        required_missing = max(4, min(THRESHOLDS['major_missing_characters'],
            math.ceil(len(first['normalized_target']) * THRESHOLDS['major_missing_fraction'])))
        first_missing = {position for edit in first['edits'] if edit['operation'] == 'delete'
            for position in range(edit['target_start'], edit['target_end'])}
        second_missing = {position for edit in second['edits'] if edit['operation'] == 'delete'
            for position in range(edit['target_start'], edit['target_end'])}
        confident = first['word_probability'] >= THRESHOLDS['confident_word_probability'] and second['word_probability'] >= THRESHOLDS['confident_word_probability']
        if metrics.get('acoustically_suspicious') and first['similarity'] < .4 and second['similarity'] < .4:
            reasons.append('low_dynamic_and_two_target_free_recognizers_cannot_recover_content')
        elif agreement >= THRESHOLDS['cross_model_similarity'] and confident:
            if len(first_missing.intersection(second_missing)) >= required_missing:
                reasons.append('two_recognizers_confirm_major_missing_span')
            if set(first['repeated_target_spans']).intersection(second['repeated_target_spans']):
                reasons.append('two_recognizers_confirm_repeated_target_span')
            if (len(first['normalized_target']) >= 12 and first['similarity'] < .4
                    and second['similarity'] < .4 and agreement >= .85):
                reasons.append('two_confident_recognizers_confirm_incompatible_content')
        if reasons:
            status = 'needs_repair'
        else:
            status = 'needs_review'
            reasons.append('recognizer_uncertainty_without_sufficient_repair_evidence')
    return {'schema': EVALUATION_SCHEMA, 'policy': POLICY, 'thresholds': copy.deepcopy(THRESHOLDS),
        'status': status, 'needs_repair': status == 'needs_repair',
        'needs_review': status == 'needs_review', 'requires_medium': requires_medium,
        'small': first, 'medium': second, 'reasons': reasons, 'advisories': advisories,
        'human_listened': False, 'voice_identity_approved': False, 'emotion_approved': False,
        'pronunciation_approved': False, 'waveform_modified': False}


def validate_quality_row(root, folder, group, native, audio, row, check_assets=True):
    """Trust boundary for bounded same-text recovery; never trusts a row's flags."""
    root, folder = Path(root).resolve(), Path(folder).resolve()
    provenance = row.get('generation_provenance')
    if provenance is not None:
        _check(isinstance(provenance, dict), 'Missing current group generation provenance')
        plan_path = media.checked_record(folder, provenance.get('plan'))
        manifest_path = media.checked_record(folder, provenance.get('native_manifest'))
        _, _, plan, _ = media.plan_binding(root, folder, plan_path)
        expected_group = next((item for item in plan['tts_groups'] if item['group_id'] == group['group_id']), None)
        _check(expected_group == group, 'Quality decision is bound to another source group')
        manifest = validated_manifest(root, folder, plan, plan_path, manifest_path)
        expected_native = next((item for item in manifest['utterances'] if item['segment_id'] == group['group_id']), None)
        _check(expected_native == native, 'Quality row belongs to another generated manifest')
        checked_audio = bound_audio(root, folder, plan, plan_path, manifest_path, group, native)
    else:
        # Old in-job test/legacy receipts remain confined to their own job.
        media.job_file(folder, native['audio'])
        media.job_file(folder, Path(native['audio']).with_suffix('.json'))
        checked_audio = alignment.audio_binding(group, native, folder / 'legacy_quality_manifest.json')
    _check(all(audio.get(name) == value for name, value in checked_audio.items()),
        'Quality bound audio no longer matches the validated current source/cache')
    small = media.read(media.checked_record(folder, row.get('observation')))
    validate_observation(small, audio, 'small', root, check_assets)
    medium = None
    if row.get('medium_observation') is not None:
        medium = media.read(media.checked_record(folder, row['medium_observation']))
        validate_observation(medium, audio, 'medium', root, check_assets)
    metrics = media.wav_metrics(audio['path'], native)
    _check(row.get('group_id') == group['group_id'] and row.get('member_ids') == group['member_ids']
        and row.get('audio_sha256') == audio['sha256'] and row.get('human_listened') is False,
        'Saved utterance membership/audio identity changed')
    decision = evaluate_group(group['text_zh'], audio, metrics, small, medium, normalizer(root))
    _check(row.get('content_evaluation') == decision and row.get('needs_repair') == decision['needs_repair']
        and row.get('needs_review') == decision['needs_review'] and row.get('target_free_asr_used') is True
        and row.get('target_free_recognized') == decision['small']['recognized']
        and row.get('normalized_text_similarity') == decision['small']['similarity'],
        'Saved decision differs from current full text/PCM/ASR evidence')
    _check(all(row.get(name) == value for name, value in metrics.items()), 'Saved PCM metrics changed')
    return decision


def audio_quality(root, plan_path, manifest_path, output, observer=None, medium_observer=None, normalize=None):
    root, plan_path = Path(root).resolve(), Path(plan_path).absolute()
    folder = root / 'jobs' / media.read(plan_path)['job']
    root, folder, plan, plan_path = media.plan_binding(root, folder, plan_path)
    output, manifest_path = media.job_file(folder, output, False), media.job_file(folder, manifest_path)
    manifest = validated_manifest(root, folder, plan, plan_path, manifest_path)
    groups, native = plan['tts_groups'], manifest.get('utterances', [])
    _check([row['segment_id'] for row in native] == [group['group_id'] for group in groups], 'Incomplete ordered group coverage')
    normalize = normalize or normalizer(root)
    observer = observer or Observer(root, folder / 'tts/media_recovery/target_free_quality_asr')
    binding = {'plan': media.record(folder, plan_path), 'native_manifest': media.record(folder, manifest_path),
        'source_segments_sha256': plan['source_segments']['sha256']}
    started, results, medium_pending = time.monotonic(), [], []
    code_sha = media.sha(__file__)

    def publish(complete=False):
        repair = [row for row in results if row['needs_repair']]
        uncertain = [row for row in results if row['needs_review']]
        report = {'schema': 'website_audio_quality_v1', 'policy': POLICY, 'binding': binding,
            'complete': complete, 'rows': results, 'checked_utterances': len(results),
            'target_free_checked_utterances': len(results), 'all_groups_target_free_checked': complete,
            'needs_repair_ids': [sid for row in repair for sid in row['member_ids']],
            'needs_repair_groups': [row['group_id'] for row in repair],
            'flagged_groups': [{'group_id': row['group_id'], 'member_ids': row['member_ids']} for row in repair],
            'needs_review_ids': [sid for row in uncertain for sid in row['member_ids']],
            'needs_review_groups': [row['group_id'] for row in uncertain],
            'status': 'needs_repair' if repair else 'needs_content_review' if uncertain else 'machine_content_screen_passed',
            'helper_code_sha256': code_sha, 'thresholds': copy.deepcopy(THRESHOLDS),
            'wall_seconds': time.monotonic() - started, 'waveform_modified': False, 'human_listened': False,
            'voice_identity_approved': False, 'emotion_approved': False,
            'method': 'Every complete generated WAV has target-free local small CPU/int8 ASR; uncertain groups receive at most one local medium pass. Current text/PCM/model assets bind all evidence. This is content/engineering screening, not human listening or perceived voice/emotion approval.'}
        media.save(output, report)
        return report

    for group, entry in zip(groups, native):
        audio = bound_audio(root, folder, plan, plan_path, manifest_path, group, entry)
        metrics = media.wav_metrics(audio['path'], entry)
        observation, path, hit = observer.observe(audio)
        validate_observation(observation, audio, 'small')
        decision = evaluate_group(group['text_zh'], audio, metrics, observation, normalize=normalize)
        row = {'group_id': group['group_id'], 'member_ids': group['member_ids'],
            'audio_sha256': audio['sha256'], **metrics, 'target_free_asr_used': True,
            'target_free_recognized': decision['small']['recognized'],
            'normalized_text_similarity': decision['small']['similarity'],
            'observation': media.record(folder, path), 'asr_cache_hit': hit,
            'generation_provenance': {'plan': binding['plan'], 'native_manifest': binding['native_manifest']},
            'content_evaluation': decision, 'needs_repair': decision['needs_repair'],
            'needs_review': decision['needs_review'], 'human_listened': False}
        results.append(row)
        if decision['requires_medium']:
            medium_pending.append((group, audio, metrics, observation, row))
        publish()
    # Batch the second tier so only one recognizer is resident at a time.
    if hasattr(observer, 'release'):
        observer.release()
    if medium_pending and medium_observer is None:
        model = local_model(root, 'medium')
        if model is not None:
            medium_observer = Observer(root, folder / 'tts/media_recovery/target_free_medium_quality_asr', 'medium', model)
    if medium_observer is not None:
        for group, audio, metrics, first, row in medium_pending:
            second, path, hit = medium_observer.observe(audio)
            validate_observation(second, audio, 'medium')
            decision = evaluate_group(group['text_zh'], audio, metrics, first, second, normalize)
            row.update(medium_observation=media.record(folder, path), medium_asr_cache_hit=hit,
                content_evaluation=decision, needs_repair=decision['needs_repair'], needs_review=decision['needs_review'])
            publish()
        if hasattr(medium_observer, 'release'):
            medium_observer.release()
    # Rebind source, manifest and every audio after recognition, before publication.
    media.plan_binding(root, folder, plan_path)
    _check(binding['native_manifest'] == media.record(folder, manifest_path), 'Manifest changed during recognition')
    validated_manifest(root, folder, plan, plan_path, manifest_path)
    for group, entry, row in zip(groups, native, results):
        audio = bound_audio(root, folder, plan, plan_path, manifest_path, group, entry)
        _check(audio['sha256'] == row['audio_sha256'], 'Generated audio changed during recognition')
    return publish(True)
