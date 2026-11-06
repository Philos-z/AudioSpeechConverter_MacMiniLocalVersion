"""Offline trust-boundary tests; no recognizer, network or GPU is started."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'web'))
import generic_group_alignment as alignment
import media_recovery as media
import speech_content_quality as speech


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.folder = self.root / 'jobs/sample'
        self.folder.mkdir(parents=True)
        self.normalize = speech.normalizer(ROOT)
        self.audio_path, self.audio = self.wav('tts/audio.wav')
        self.metrics = media.wav_metrics(self.audio_path, self.audio)
        self.profiles = {}
        for tier in ('small', 'medium'):
            model = self.root / ('faster-whisper-' + tier)
            model.mkdir()
            (model / 'model.bin').write_bytes(('local fake asset ' + tier).encode())
            with patch.object(alignment, 'environment_versions', return_value={}):
                self.profiles[tier] = speech.profile_for_model(self.root, model, tier)

    def wav(self, name, silent=False, clipped=False, gap=False):
        rate, duration = 16000, 5.
        times = np.arange(round(duration * rate)) / rate
        signal = .18 * np.sin(2 * np.pi * 180 * times) * np.maximum(.005, np.sin(times * 7) ** 4)
        if silent:
            signal[:] = 0
        if clipped:
            signal[:rate // 10] = 1.
        if gap:
            signal[rate // 2:rate * 4] = 0
        path = self.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), 'wb') as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes((signal * 32767).astype('<i2').tobytes())
        bound = {'path': str(path), 'sha256': media.sha(path), 'duration_s': duration,
                 'frames': len(times), 'sample_rate': rate, 'channels': 1}
        return path, bound

    def observation(self, text, tier='small', probability=.9, audio=None):
        audio = audio or self.audio
        profile = copy.deepcopy(self.profiles[tier])
        words = [{'word': text, 'start_s': .1, 'end_s': audio['duration_s'] - .1, 'probability': probability}] if text else []
        segments = [{'text': text, 'start_s': .1, 'end_s': audio['duration_s'] - .1,
                     'avg_logprob': -.1, 'no_speech_prob': .01, 'words': words}] if text else []
        return {'schema': alignment.ASR_SCHEMA, 'phase': 'complete', 'profile': profile,
            'signature': alignment.key({'audio_sha256': audio['sha256'], 'profile': profile}),
            'audio_sha256': audio['sha256'], 'segments': segments, 'inference_s': .01}

    def evaluate(self, target, first, second=None, metrics=None):
        return speech.evaluate_group(target, self.audio, metrics or self.metrics,
            self.observation(first), self.observation(second, 'medium') if second is not None else None,
            self.normalize)


class ContentDecisions(Fixture):
    def test_script_punctuation_pronunciation_markup_and_common_numbers(self):
        equivalents = [('二十五度', '25度'), ('負二點五度', '-2.5度'),
            ('百分之二十五', '25%'), ('二〇二六年', '2026年'), ('两万五千人', '25000人'),
            ('12,500人', '一万二千五百人'), ('1.2万人', '一万二千人'),
            ('十一至零度', '11到0度'), ('零下五度', '-5度'),
            ('<重庆|chong2 qing4>天气。', '重慶天氣!')]
        for left, right in equivalents:
            with self.subTest(left=left):
                self.assertEqual(self.normalize(left), self.normalize(right))
        self.assertNotEqual(self.normalize('最多二十五人'), self.normalize('至少25人'))
        self.assertNotEqual(self.normalize('零下五度'), self.normalize('五度'))

    def test_normal_dynamic_waveform_gets_content_check_not_legacy_exemption(self):
        self.assertFalse(self.metrics['acoustically_suspicious'])
        decision = self.evaluate('今天公布的完整结果会在明天送交议会讨论。', '今天公布的完整结果会在明天送交议会讨论。')
        self.assertEqual(decision['status'], 'machine_content_screen_passed')
        self.assertFalse(decision['requires_medium'])
        self.assertFalse(decision['emotion_approved'])
        self.assertFalse(decision['voice_identity_approved'])

    def test_one_recognizer_cannot_authorize_normal_dynamic_missing_sentence_repair(self):
        result = self.evaluate('今天公布的完整结果会在明天送交议会讨论。', '今天公布的完整结果。')
        self.assertTrue(result['requires_medium'])
        self.assertTrue(result['needs_review'])
        self.assertFalse(result['needs_repair'])

    def test_two_confident_recognizers_confirm_long_missing_tail(self):
        result = self.evaluate('今天公布的完整结果会在明天送交议会讨论。',
            '今天公布的完整结果。', '今天公布的完整结果。')
        self.assertTrue(result['needs_repair'])
        self.assertIn('two_recognizers_confirm_major_missing_span', result['reasons'])

    def test_medium_recovers_complete_target_and_avoids_unnecessary_generation(self):
        target = '今天公布的完整结果会在明天送交议会讨论。'
        result = self.evaluate(target, '今天公布的完整结果。', target)
        self.assertFalse(result['needs_repair'])
        self.assertFalse(result['needs_review'])
        self.assertIn('small_asr_disagreed_medium_recovered_target', result['advisories'])

    def test_cross_model_disagreement_does_not_create_repair_permission(self):
        result = self.evaluate('今天公布的完整结果会在明天送交议会讨论。',
            '今天公布的完整结果。', '明天送交议会讨论。')
        self.assertFalse(result['needs_repair'])
        self.assertTrue(result['needs_review'])

    def test_two_low_confidence_outputs_without_acoustic_defect_stay_uncertain(self):
        target, text = '今天公布的完整结果会在明天送交议会讨论。', '今天公布的完整结果。'
        result = speech.evaluate_group(target, self.audio, self.metrics,
            self.observation(text, probability=.1), self.observation(text, 'medium', probability=.2), self.normalize)
        self.assertFalse(result['needs_repair'])
        self.assertTrue(result['needs_review'])

    def test_flat_noise_and_two_empty_observations_are_actual_repair_evidence(self):
        metrics = {**self.metrics, 'acoustically_suspicious': True, 'energy_dynamic_range_db': 3.}
        result = self.evaluate('今天公布的完整结果会在明天送交议会讨论。', '', '', metrics)
        self.assertTrue(result['needs_repair'])

    def test_target_span_repetition_needs_both_recognizers(self):
        target = '政府将在明天发布全部调查结果。'
        repeated = '政府将在明天发布全部调查结果发布全部调查结果。'
        self.assertFalse(self.evaluate(target, repeated)['needs_repair'])
        result = self.evaluate(target, repeated, repeated)
        self.assertTrue(result['needs_repair'])
        self.assertIn('two_recognizers_confirm_repeated_target_span', result['reasons'])

    def test_long_target_high_average_similarity_cannot_hide_missing_phrase(self):
        target = '政府将在明天发布全部调查结果并向议会提供完整材料，随后召开新闻发布会说明情况。'
        text = target.replace('提供完整材料', '')
        result = self.evaluate(target, text)
        self.assertGreater(result['small']['similarity'], .85)
        self.assertTrue(result['requires_medium'])
        self.assertTrue(self.evaluate(target, text, text)['needs_repair'])

    def test_different_missing_regions_do_not_count_as_shared_content_evidence(self):
        target = '政府将在明天发布全部调查结果并向议会提供完整材料，随后召开新闻发布会说明情况。'
        first = target.replace('发布全部调查结果', '')
        second = target.replace('召开新闻发布会', '')
        result = self.evaluate(target, first, second)
        self.assertFalse(result['needs_repair'])
        self.assertTrue(result['needs_review'])

    def test_two_confident_observations_of_incompatible_full_content_can_repair(self):
        result = self.evaluate('政府将在明天发布全部调查结果并向议会提供完整材料。',
            '球员在比赛中打进两球为球队赢得胜利。', '球员在比赛中打进两球为球队赢得胜利。')
        self.assertTrue(result['needs_repair'])
        self.assertIn('two_confident_recognizers_confirm_incompatible_content', result['reasons'])

    def test_isolated_name_spelling_and_homophone_noise_do_not_regenerate(self):
        result = self.evaluate('罗穆洛在下半场为球队打进了第二个进球。', '罗姆洛在下半场为球队打进了第2个进球。')
        self.assertFalse(result['needs_repair'])
        self.assertFalse(result['requires_medium'])

    def test_short_negation_or_numeric_conflict_is_not_dismissed_as_spelling(self):
        for target, text in [('政府明天不会发布结果', '政府明天会发布结果'), ('一共有二十五人参加会议', '一共有三十五人参加会议')]:
            with self.subTest(target=target):
                result = self.evaluate(target, text)
                self.assertTrue(result['needs_review'])
                self.assertTrue(result['requires_medium'])

    def test_pcm_near_silence_and_real_sustained_clipping_are_objective_faults(self):
        for kind in ('silent', 'clipped'):
            with self.subTest(kind=kind):
                path, audio = self.wav('tts/' + kind + '.wav', **{kind: True})
                metrics = media.wav_metrics(path, audio)
                result = speech.evaluate_group('完整台词', audio, metrics,
                    self.observation('完整台词', audio=audio), normalize=self.normalize)
                self.assertTrue(result['needs_repair'])

    def test_long_internal_silence_is_advisory_without_inventing_missing_content(self):
        path, audio = self.wav('tts/gap.wav', gap=True)
        metrics = media.wav_metrics(path, audio)
        self.assertGreater(metrics['longest_internal_silence_s'], 3.)
        result = speech.evaluate_group('完整台词', audio, metrics,
            self.observation('完整台词', audio=audio), normalize=self.normalize)
        self.assertIn('abnormally_long_internal_silence', result['advisories'])
        self.assertFalse(result['needs_repair'])


class EvidenceTrust(Fixture):
    def test_observation_bound_to_audio_model_and_target_free_parameters(self):
        value = self.observation('目标句子')
        speech.validate_observation(value, self.audio, 'small')
        for mutate in [lambda v: v.update(audio_sha256='0' * 64),
                       lambda v: v['profile']['transcribe_options'].update(initial_prompt='目标句子'),
                       lambda v: v['profile'].update(device='mps'),
                       lambda v: v['profile'].update(model_path='/model-medium')]:
            changed = copy.deepcopy(value)
            mutate(changed)
            with self.assertRaises(ValueError):
                speech.validate_observation(changed, self.audio, 'small')

    def test_real_model_asset_change_invalidates_saved_evidence(self):
        value = self.observation('目标句子')
        with patch.object(alignment, 'environment_versions', return_value={}):
            speech.validate_observation(value, self.audio, 'small', self.root, True)
            path = Path(self.profiles['small']['model_path']) / 'model.bin'
            path.write_bytes(b'changed asset')
            with self.assertRaisesRegex(ValueError, 'assets'):
                speech.validate_observation(value, self.audio, 'small', self.root, True)

    def test_cache_is_reused_without_starting_a_recognizer_and_key_includes_options(self):
        observer = speech.Observer(self.root, self.folder / 'cache')
        observer.profile = self.profiles['small']
        value = self.observation('目标句子')
        path = observer.cache / (value['signature'] + '.json')
        media.save(path, value)
        self.assertTrue(observer.observe(self.audio)[2])
        self.assertIsNone(observer.model)
        altered = copy.deepcopy(observer.profile)
        altered['transcribe_options']['beam_size'] += 1
        self.assertNotEqual(value['signature'], alignment.key({'audio_sha256': self.audio['sha256'], 'profile': altered}))
        changed_audio = {**self.audio, 'sha256': '1' * 64}
        self.assertNotEqual(value['signature'], alignment.key({'audio_sha256': changed_audio['sha256'], 'profile': observer.profile}))

    def test_invalid_observation_word_geometry_is_rejected(self):
        value = self.observation('目标句子')
        value['segments'][0]['words'][0]['end_s'] = 50.
        with self.assertRaisesRegex(ValueError, 'geometry'):
            speech.validate_observation(value, self.audio, 'small')

    def test_invalid_cache_is_preserved_before_one_fresh_recognition_attempt(self):
        observer = speech.Observer(self.root, self.folder / 'cache')
        observer.profile = self.profiles['small']
        value = self.observation('目标句子')
        path = observer.cache / (value['signature'] + '.json')
        path.parent.mkdir()
        bad = b'{interrupted incomplete cache'
        path.write_bytes(bad)
        with patch.object(alignment.Observer, 'observe', return_value=(value, path, False)) as fresh:
            observer.observe(self.audio)
        fresh.assert_called_once_with(self.audio)
        self.assertFalse(path.exists())
        self.assertEqual(Path(observer.invalid_cache_records[0]['path']).read_bytes(), bad)

    def test_symlink_cache_cannot_escape_job_scope(self):
        observer = speech.Observer(self.root, self.folder / 'cache')
        observer.profile = self.profiles['small']
        observer.cache.mkdir()
        outside = self.root / 'outside.json'
        outside.write_text('{}')
        path = observer.cache / (self.observation('目标句子')['signature'] + '.json')
        path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'Symlink'):
            observer.observe(self.audio)
        self.assertEqual(outside.read_text(), '{}')


class WholeManifest(Fixture):
    def setup_manifest(self):
        texts = ['这里是新闻节目。', '今天公布的完整结果会在明天送交议会讨论。']
        rows = [{'segment_id': 'line' + str(i), 'text_zh': text, 'text_de': 'source ' + str(i)}
                for i, text in enumerate(texts)]
        media.save(self.folder / 'segments.json', {'segments': rows})
        media.save(self.folder / 'speakers.json', {'S00': {'reviewed': True}})
        video = self.root / 'source.mp4'
        video.write_bytes(b'original video')
        media.save(self.folder / 'source.json', {'input': str(video)})
        groups, native = [], []
        ref, reference = self.wav('references/ref.wav')
        for index, row in enumerate(rows):
            gid = 'group' + str(index)
            path, audio = self.wav('tts/' + gid + '.wav')
            group = {'group_id': gid, 'member_ids': [row['segment_id']], 'text_zh': row['text_zh'],
                     'speaker_id': 'S00', 'reference': {'path': str(ref), 'sha256': reference['sha256']}}
            groups.append(group)
            content = {'text_zh': row['text_zh'], 'effective_tts_text': row['text_zh'], 'reference_sha256': reference['sha256']}
            media.save(path.with_suffix('.json'), {'schema': 'native_mlx_whole_utterance_v1',
                'sha256': audio['sha256'], 'content': content, 'cache_key': media.key(content)})
            native.append({'segment_id': gid, 'speaker_id': 'S00', 'audio': str(path), 'content': content, **audio})
        self.plan_path = self.folder / 'tts/plan.json'
        self.plan = {'schema': 'pretts_semantic_group_plan_v1', 'job': self.folder.name,
            'source_segments': media.record(self.folder, self.folder / 'segments.json'),
            'source_speakers': media.record(self.folder, self.folder / 'speakers.json'),
            'source_binding': {'source_receipt_path': str(self.folder / 'source.json'),
                'source_receipt_sha256': media.sha(self.folder / 'source.json'), 'source_video_path': str(video),
                'source_video_sha256': media.sha(video), 'source_video_bytes': video.stat().st_size},
            'source_rows': rows, 'tts_groups': groups}
        media.save(self.plan_path, self.plan)
        self.manifest_path = self.folder / 'tts/manifest.json'
        media.save(self.manifest_path, {'phase': 'complete', 'utterances': native})
        return groups, native

    def run_quality(self, first_text=None, second_text=None):
        groups, native = self.setup_manifest()
        events, outer = [], self

        class FakeObserver:
            def __init__(self, tier):
                self.tier, self.calls = tier, 0

            def observe(self, audio):
                events.append(self.tier)
                self.calls += 1
                if self.tier == 'small':
                    text = groups[self.calls - 1]['text_zh'] if self.calls == 1 else first_text or groups[1]['text_zh']
                else:
                    text = second_text or groups[1]['text_zh']
                value = outer.observation(text, self.tier, audio=audio)
                path = outer.folder / ('cache/' + self.tier + str(self.calls) + '.json')
                media.save(path, value)
                return value, path, False

            def release(self):
                events.append(self.tier + '_released')

        first, second = FakeObserver('small'), FakeObserver('medium')
        output = self.folder / 'tts/full_quality.json'
        result = media.audio_quality(self.root, self.plan_path, self.manifest_path, output,
            first, self.normalize, speech.POLICY, second)
        return result, events, native

    def test_every_normal_group_gets_small_once_medium_only_after_small_release(self):
        result, events, _ = self.run_quality(first_text='今天公布的完整结果。')
        self.assertEqual(events, ['small', 'small', 'small_released', 'medium', 'medium_released'])
        self.assertEqual(result['target_free_checked_utterances'], 2)
        self.assertTrue(result['all_groups_target_free_checked'])
        self.assertFalse(result['needs_repair_ids'])
        self.assertFalse(result['needs_review_ids'])
        self.assertTrue(all(row['target_free_asr_used'] for row in result['rows']))

    def test_receipt_replay_recomputes_current_pcm_full_target_and_both_observations(self):
        result, _, native = self.run_quality(first_text='今天公布的完整结果。', second_text='今天公布的完整结果。')
        group, entry, row = self.plan['tts_groups'][1], native[1], result['rows'][1]
        audio = alignment.audio_binding(group, entry, self.manifest_path)
        with patch.object(alignment, 'environment_versions', return_value={}):
            decision = speech.validate_quality_row(self.root, self.folder, group, entry, audio, row)
            self.assertTrue(decision['needs_repair'])
            changed = copy.deepcopy(row)
            changed['needs_repair'] = False
            with self.assertRaisesRegex(ValueError, 'decision'):
                speech.validate_quality_row(self.root, self.folder, group, entry, audio, changed)
            changed_group = {**group, 'text_zh': '今天公布的完整结果。'}
            with self.assertRaisesRegex(ValueError, 'decision'):
                speech.validate_quality_row(self.root, self.folder, changed_group, entry, audio, row)
            path = self.folder / row['medium_observation']['path']
            value = media.read(path)
            value['segments'][0]['text'] = '偷换识别'
            media.save(path, value)
            with self.assertRaisesRegex(ValueError, 'SHA'):
                speech.validate_quality_row(self.root, self.folder, group, entry, audio, row)

    def test_corrupted_wav_or_sidecar_stops_before_asr(self):
        self.setup_manifest()
        value = media.read(self.manifest_path)
        path = Path(value['utterances'][0]['audio'])
        path.write_bytes(path.read_bytes()[:-2])
        class NoASR:
            def observe(self, _):
                raise AssertionError('ASR must not start for corrupt audio')
        with self.assertRaises(ValueError):
            media.audio_quality(self.root, self.plan_path, self.manifest_path,
                self.folder / 'bad.json', NoASR(), self.normalize, speech.POLICY)


class SharedNativeCache(Fixture):
    setup_manifest = WholeManifest.setup_manifest

    def setup_shared_manifest(self):
        from generic_group_plan import CACHE_REUSE_POLICY
        groups, native = self.setup_manifest()
        child_id = 'web_ttsgroups_' + 'a' * 24
        child = self.root / 'jobs' / child_id
        cache = self.root / 'cache/native_mlx_semantic_groups_v2'
        permission_path = self.folder / 'processing_request.json'
        permission = {'processing_authorized': True, 'scope': 'private_local_evaluation'}
        media.save(permission_path, permission)
        for index, (group, row) in enumerate(zip(groups, native)):
            group.update(source_start_s=float(index), source_end_s=float(index + 1))
            group['tts_row'] = {'segment_id': group['group_id'], 'speaker_id': group['speaker_id'],
                'start_s': group['source_start_s'], 'end_s': group['source_end_s'], 'text_zh': group['text_zh']}
            row['content'].update(schema='native_mlx_whole_utterance_v1', parameters={}, model_identity={'fixture': True})
            key = media.key(row['content'])
            path = cache / 'audio' / (key + '.wav')
            path.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(row['audio'], 'rb') as source:
                samples = np.frombuffer(source.readframes(source.getnframes()), dtype='<i2').astype('<i4') << 8
            with wave.open(str(path), 'wb') as target:
                target.setnchannels(1); target.setsampwidth(3); target.setframerate(row['sample_rate'])
                target.writeframes(samples.view(np.uint8).reshape(-1, 4)[:, :3].tobytes())
            row.pop('path', None)
            row.update(audio=str(path), sha256=media.sha(path), cache_key=key, reference=group['reference']['path'],
                source_index=index, source_start_s=group['source_start_s'], source_end_s=group['source_end_s'])
            media.save(path.with_suffix('.json'), {'schema': 'native_mlx_whole_utterance_v1',
                'cache_key': key, 'content': row['content'], **{name: row[name] for name in
                    ('sha256', 'frames', 'sample_rate', 'duration_s', 'channels')}})
        tts = {'id': child_id, 'job': child_id, 'directory': str(child), 'parent_job': self.folder.name,
            'cache_scope_key': 'a' * 64, 'plan_provenance_key': 'a' * 64, 'cache_dir': str(cache),
            'profile': None, 'profile_options': {}, 'cache_reuse_policy': copy.deepcopy(CACHE_REUSE_POLICY),
            'processing_permission_path': str(permission_path), 'processing_permission_sha256': media.sha(permission_path)}
        self.plan['tts_job'] = tts
        media.save(self.plan_path, self.plan)
        binding = {'schema': 'generic_group_tts_child_binding_v1', 'parent_job': self.folder.name,
            'group_plan_path': str(self.plan_path), 'group_plan_sha256': media.sha(self.plan_path),
            **{name: tts[name] for name in ('cache_scope_key', 'profile', 'profile_options',
                'cache_dir', 'plan_provenance_key', 'cache_reuse_policy')}}
        media.save(child / 'group_plan_binding.json', binding)
        media.save(child / 'segments.json', {'schema_version': 2, 'segments': [g['tts_row'] for g in groups],
            'parent_job': self.folder.name, 'human_reviewed': False})
        media.save(child / 'source.json', media.read(self.folder / 'source.json'))
        media.save(child / 'speakers.json', media.read(self.folder / 'speakers.json'))
        permission.update(parent_job=self.folder.name, child_role='semantic_groups_tts_only',
            group_plan_path=str(self.plan_path), group_plan_sha256=media.sha(self.plan_path))
        media.save(child / 'processing_request.json', permission)
        manifest = {'schema': 'native_mlx_whole_utterance_v1', 'job': child_id, 'phase': 'complete',
            'cache_root': str(cache), 'parameters': {}, 'pending_unique_keys': 0,
            'selected_utterances': len(native), 'utterances': native}
        media.save(self.manifest_path, manifest)
        return groups, native, child, manifest

    def test_production_shape_shared_native_manifest_and_quality_revalidation(self):
        groups, native, child, manifest = self.setup_shared_manifest()
        outer = self
        class FakeObserver:
            def __init__(self): self.count = 0
            def observe(self, audio):
                group = groups[self.count]; self.count += 1
                value = outer.observation(group['text_zh'], audio=audio)
                path = outer.folder / ('asr/observation' + str(self.count) + '.json')
                media.save(path, value)
                return value, path, False
        report = media.audio_quality(self.root, self.plan_path, self.manifest_path,
            self.folder / 'tts/shared_quality.json', FakeObserver(), self.normalize, speech.POLICY)
        self.assertEqual(report['status'], 'machine_content_screen_passed')
        self.assertEqual(report['checked_utterances'], 2)
        for group, entry, row in zip(groups, native, report['rows']):
            audio = speech.bound_audio(self.root, self.folder, self.plan, self.plan_path,
                self.manifest_path, group, entry)
            with patch.object(alignment, 'environment_versions', return_value={}):
                self.assertFalse(speech.validate_quality_row(self.root, self.folder,
                    group, entry, audio, row)['needs_repair'])

    def test_shared_cache_rejects_wrong_sha_and_any_unregistered_path(self):
        groups, native, child, manifest = self.setup_shared_manifest()
        bad = copy.deepcopy(native[0]); bad['sha256'] = '0' * 64
        with self.assertRaises(ValueError):
            speech.bound_audio(self.root, self.folder, self.plan, self.plan_path, self.manifest_path, groups[0], bad)
        for path in (self.root.parent / 'outside.wav', self.root / 'cache/arbitrary.wav'):
            bad = copy.deepcopy(native[0]); bad['audio'] = str(path)
            with self.subTest(path=path), self.assertRaises(ValueError):
                speech.bound_audio(self.root, self.folder, self.plan, self.plan_path, self.manifest_path, groups[0], bad)

    def test_shared_manifest_rejects_other_child_or_modified_child_source_binding(self):
        groups, native, child, manifest = self.setup_shared_manifest()
        original = self.manifest_path.read_bytes()
        manifest['job'] = 'web_ttsgroups_' + 'b' * 24
        media.save(self.manifest_path, manifest)
        with self.assertRaises(ValueError):
            speech.validated_manifest(self.root, self.folder, self.plan, self.plan_path, self.manifest_path)
        self.manifest_path.write_bytes(original)
        binding = media.read(child / 'group_plan_binding.json'); binding['group_plan_sha256'] = '0' * 64
        media.save(child / 'group_plan_binding.json', binding)
        with self.assertRaises(ValueError):
            speech.validated_manifest(self.root, self.folder, self.plan, self.plan_path, self.manifest_path)


if __name__ == '__main__':
    unittest.main()
