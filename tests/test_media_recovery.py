"""Offline safety boundaries and CPU synthetic media probes; no AI/GPU/network."""
import copy
import importlib.util
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

WEB = Path(__file__).resolve().parents[1] / 'web'
sys.path.insert(0, str(WEB))
import media_recovery as media


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.folder = self.root / 'jobs' / 'sample'
        self.folder.mkdir(parents=True)
        self.rows = [{'segment_id': 'row%02d' % i, 'speaker_id': 'voice', 'start_s': (i - 1) * 2.,
            'end_s': (i - 1) * 2. + 1.8, 'source_ids': ['raw%02d' % i], 'text_de': 'source %d' % i,
            'text_zh': '测试句子%d' % i, 'translation_reviewed': True, 'human_reviewed': False}
            for i in range(1, 4)]
        media.save(self.folder / 'segments.json', {'segments': self.rows})
        media.save(self.folder / 'speakers.json', {'voice': {'reviewed': True}})
        self.video = self.root / 'source.mp4'
        self.video.write_bytes(b'immutable-source-video')
        self.source = {'input': str(self.video), 'duration_s': 8., 'audio_track': 0}
        media.save(self.folder / 'source.json', self.source)
        self.plan_path = self.folder / 'tts/semantic_group_plan.json'
        self.plan = {'schema': 'pretts_semantic_group_plan_v1', 'job': self.folder.name,
            'root': str(self.root), 'source_segments': {'path': str(self.folder / 'segments.json'), 'sha256': media.sha(self.folder / 'segments.json')},
            'source_speakers': {'path': str(self.folder / 'speakers.json'), 'sha256': media.sha(self.folder / 'speakers.json')},
            'source_binding': {'source_receipt_path': str(self.folder / 'source.json'), 'source_receipt_sha256': media.sha(self.folder / 'source.json'),
                'source_video_path': str(self.video), 'source_video_sha256': media.sha(self.video), 'source_video_bytes': self.video.stat().st_size,
                'source_duration_s': 8., 'source_audio_track': 0},
            'source_rows': copy.deepcopy(self.rows), 'tts_groups': [
                {'group_id': 'groupA', 'member_ids': ['row01', 'row02'], 'source_start_s': 0., 'speaker_id': 'voice', 'text_zh': '测试句子1测试句子2'},
                {'group_id': 'groupB', 'member_ids': ['row03'], 'source_start_s': 4., 'speaker_id': 'voice', 'text_zh': '测试句子3'}]}
        for group in self.plan['tts_groups']:
            group['members'] = [copy.deepcopy(r) for r in self.rows if r['segment_id'] in group['member_ids']]
            reference = self.folder / 'reference.wav'
            if not reference.exists():
                self.make_wave('reference.wav', .2)
            group['reference'] = {'path': str(reference), 'sha256': media.sha(reference)}
        media.save(self.plan_path, self.plan)
        self.manifest_path = self.folder / 'tts/native/manifest.json'
        self.manifest = {'phase': 'complete', 'utterances': []}
        for group in self.plan['tts_groups']:
            audio, geometry = self.make_wave('tts/' + group['group_id'] + '.wav', .4)
            content = {'text_zh': group['text_zh'], 'effective_tts_text': group['text_zh'], 'reference_sha256': group['reference']['sha256']}
            media.save(audio.with_suffix('.json'), {'schema': 'native_mlx_whole_utterance_v1', 'sha256': geometry['sha256'],
                'content': content, 'cache_key': media.key(content)})
            self.manifest['utterances'].append({'segment_id': group['group_id'], 'speaker_id': group['speaker_id'],
                'audio': str(audio), 'content': content, **geometry})
        media.save(self.manifest_path, self.manifest)
        self.index_path = self.folder / 'tts/alignment/alignment_manifest.json'
        self.index = {'schema': 'generic_group_subtitle_alignment_manifest_v1', 'phase': 'needs_alignment_review',
            'all_groups_ready': False, 'partial_preview': False, 'source_row_count': 3, 'source_plan_row_count': 3, 'tts_group_count': 2,
            'plan': {'path': str(self.plan_path), 'sha256': media.sha(self.plan_path)},
            'native_manifest': {'path': str(self.manifest_path), 'sha256': media.sha(self.manifest_path)},
            'selected_group_ids': ['groupA', 'groupB'], 'groups': []}
        for group in self.plan['tts_groups']:
            status = 'needs_alignment_review' if group['group_id'] == 'groupA' else 'aligned_for_machine_preview'
            path = self.index_path.parent / (group['group_id'] + '.json')
            receipt = {'schema': 'generic_group_subtitle_alignment_v1', 'group_id': group['group_id'], 'member_ids': group['member_ids'],
                'status': status, 'plan': self.index['plan'], 'native_manifest': self.index['native_manifest'], 'source_members': group['members']}
            media.save(path, receipt)
            self.index['groups'].append({**receipt, 'receipt': media.record(self.folder, path)})
        media.save(self.index_path, self.index)

    def make_wave(self, name='audio.wav', duration=1., modulated=False):
        import numpy as np
        rate = 24000
        t = np.arange(round(rate * duration)) / rate
        signal = np.sin(2 * np.pi * 180 * t) * .15
        if modulated:
            signal *= np.maximum(.001, np.sin(2 * np.pi * 2.5 * t) ** 4)
        path = self.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), 'wb') as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes((signal * 32767).astype('<i2').tobytes())
        return path, {'sha256': media.sha(path), 'frames': len(t), 'sample_rate': rate,
            'channels': 1, 'duration_s': len(t) / rate}

    def make_policy(self):
        proof = self.folder / 'tts/failure.json'
        media.save(proof, {'failure': 'actual-machine-evidence'})
        value = {'schema': 'scoped_no_join_recovery_v1', 'job': 'sample',
            'source_segments_sha256': self.plan['source_segments']['sha256'],
            'source_speakers_sha256': self.plan['source_speakers']['sha256'],
            'groups': [{'member_ids': ['row01', 'row02']}], 'evidence': [media.record(self.folder, proof)]}
        media.save(self.folder / 'tts/no_join_recovery.json', value)
        return value

    def picture_fixture(self, duration=5.):
        path, pcm = self.make_wave('tts/groupA.wav', duration)
        path2, pcm2 = self.make_wave('tts/groupB.wav', 1.)
        clips, utterances, issues = [], [], []
        for group, wav, values, boundary in [(self.plan['tts_groups'][0], path, pcm, 4.),
                                           (self.plan['tts_groups'][1], path2, pcm2, 8.)]:
            start = group['source_start_s']
            frames = math.ceil((values['duration_s'] + .35) * 25 - 1e-8)
            taken = min(frames / 25, boundary - start)
            clips.append({'kind': 'speech', 'segment_ids': [group['group_id']], 'source_member_ids': group['member_ids'],
                'source_start_s': start, 'source_end_s': start + taken, 'frames': frames, 'speech_s': values['duration_s']})
            utterances.append({'group_id': group['group_id'], 'member_ids': group['member_ids'], 'audio': str(wav),
                'audio_sha256': values['sha256'], **{k: v for k, v in values.items() if k != 'sha256'}})
            if values['duration_s'] - (boundary - start) > .35 + 1e-6:
                issues.append({'group_id': group['group_id'], 'reason': 'whole_group_audio_exceeds_source_window',
                    'speech_s': values['duration_s'], 'available_source_s': boundary - start})
        edl = {'fps': 25, 'source_input': str(self.video), 'partial_preview': False, 'clips': clips,
            'utterances': utterances, 'issues': issues}
        audit = {'plan': str(self.plan_path), 'plan_sha256': media.sha(self.plan_path),
            'source_json_sha256': media.sha(self.folder / 'source.json'), 'issues': issues}
        ep, ap = self.folder / 'outputs/current/edit_decisions.json', self.folder / 'outputs/current/group_adapter_report.json'
        media.save(ep, edl)
        media.save(ap, audit)
        return ep, ap

    def fake_frame(self, _ffmpeg, _source, timestamp, output):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b'JPEG evidence at ' + str(timestamp).encode())
        return {'usable': True, 'black': False, 'white': False, 'flat': False}


class AlignmentTests(Fixture):
    def test_only_failed_multirow_group_is_split_and_second_click_is_idempotent(self):
        result = media.repair_alignment(self.root, self.folder, self.plan_path, self.index_path)
        self.assertEqual(result['split_groups'], [{'member_ids': ['row01', 'row02']}])
        self.assertEqual(result['preserved_other_groups'], 1)
        self.assertTrue(result['changed'])
        again = media.repair_alignment(self.root, self.folder, self.plan_path, self.index_path)
        self.assertFalse(again['changed'])
        self.assertEqual(len(media.journal(self.folder)['entries']), 1)
        policy = media.read(self.folder / 'tts/no_join_recovery.json')
        media.checked_record(self.folder, policy['evidence'][0])

    def test_full_success_does_not_open_recovery(self):
        self.index.update(phase='complete', all_groups_ready=True)
        media.save(self.index_path, self.index)
        with self.assertRaisesRegex(ValueError, 'actual failed'):
            media.repair_alignment(self.root, self.folder, self.plan_path, self.index_path)

    def test_missing_or_wrong_failure_proof_is_rejected(self):
        self.index['groups'][0]['receipt']['sha256'] = '0' * 64
        media.save(self.index_path, self.index)
        with self.assertRaisesRegex(ValueError, 'evidence SHA changed'):
            media.repair_alignment(self.root, self.folder, self.plan_path, self.index_path)
        self.assertFalse((self.folder / 'tts/no_join_recovery.json').exists())

    def test_singleton_failure_is_reported_without_splitting_it(self):
        self.index['groups'][1]['status'] = 'needs_alignment_review'
        path = self.folder / self.index['groups'][1]['receipt']['path']
        receipt = media.read(path)
        receipt['status'] = 'needs_alignment_review'
        media.save(path, receipt)
        self.index['groups'][1]['receipt'] = media.record(self.folder, path)
        media.save(self.index_path, self.index)
        result = media.repair_alignment(self.root, self.folder, self.plan_path, self.index_path)
        self.assertEqual(result['unresolved_ids'], ['row03'])
        self.assertEqual(result['split_groups'], [{'member_ids': ['row01', 'row02']}])

    def test_actual_failed_wav_tamper_does_not_authorize_split(self):
        audio = Path(self.manifest['utterances'][0]['audio'])
        audio.write_bytes(audio.read_bytes() + b'changed')
        with self.assertRaisesRegex(ValueError, 'audio SHA mismatch'):
            media.repair_alignment(self.root, self.folder, self.plan_path, self.index_path)
        self.assertFalse((self.folder / 'tts/no_join_recovery.json').exists())

    def test_stale_manifest_does_not_authorize_split(self):
        media.save(self.manifest_path, {'phase': 'complete', 'utterances': [], 'different': True})
        with self.assertRaisesRegex(ValueError, 'manifest changed'):
            media.repair_alignment(self.root, self.folder, self.plan_path, self.index_path)


class JournalTests(Fixture):
    def test_lifetime_budget_is_shared_and_never_resets_on_duplicate(self):
        for i in range(6):
            item = media.reserve_recovery(self.folder, 'audio_text_repair', {'input_sha256': str(i)})
            media.finish_recovery(self.folder, item['token'], {'changed': True})
        for _ in range(3):
            self.assertTrue(media.reserve_recovery(self.folder, 'audio_text_repair', {'input_sha256': '0'})['reused'])
        with self.assertRaisesRegex(ValueError, 'exhausted'):
            media.reserve_recovery(self.folder, 'picture_holds', {'input_sha256': 'new'})
        self.assertEqual(len(media.journal(self.folder)['entries']), 6)

    def test_pending_action_can_resume_without_new_budget(self):
        first = media.reserve_recovery(self.folder, 'alignment_split', {'input': 'same'})
        second = media.reserve_recovery(self.folder, 'alignment_split', {'input': 'same'})
        self.assertEqual(first['token'], second['token'])
        self.assertEqual(len(media.journal(self.folder)['entries']), 1)

    def test_evidence_tamper_is_not_an_idempotent_cache_hit(self):
        path = self.folder / 'evidence.json'
        media.save(path, {'value': 1})
        reservation = media.reserve_recovery(self.folder, 'audio_text_repair', {'input': 'fixed'})
        media.finish_recovery(self.folder, reservation['token'], {'outputs': [media.record(self.folder, path)]})
        media.save(path, {'value': 2})
        with self.assertRaisesRegex(ValueError, 'evidence SHA changed'):
            media.reused_result(self.folder, media.reserve_recovery(self.folder, 'audio_text_repair', {'input': 'fixed'}))

    def test_symlink_and_sibling_job_paths_rejected(self):
        outside = self.root / 'outside.json'
        outside.write_text('{}')
        (self.folder / 'link.json').symlink_to(outside)
        for path in ('link.json', '../outside.json', outside):
            with self.assertRaises(ValueError):
                media.job_file(self.folder, path)


class RebindTests(Fixture):
    def test_chinese_only_change_rebinds_and_archives_policy(self):
        old = self.make_policy()
        self.rows[0]['text_zh'] = '精简译稿'
        self.rows[0]['translation_model'] = 'existing-model'
        media.save(self.folder / 'segments.json', {'segments': self.rows})
        result = media.rebind_no_join(self.root, self.folder, self.plan_path)
        self.assertTrue(result['source_identity_preserved'])
        current = media.read(self.folder / 'tts/no_join_recovery.json')
        self.assertEqual(current['source_segments_sha256'], media.sha(self.folder / 'segments.json'))
        backup = self.folder / current['source_rebindings'][0]['previous_policy']['path']
        self.assertEqual(media.read(backup), old)
        self.assertFalse(media.rebind_no_join(self.root, self.folder, self.plan_path)['changed'])

    def test_source_word_time_or_speaker_change_cannot_rebind(self):
        for field, value in [('text_de', 'changed source'), ('start_s', .1), ('speaker_id', 'other')]:
            with self.subTest(field=field):
                self.make_policy()
                changed = copy.deepcopy(self.rows)
                changed[0][field] = value
                media.save(self.folder / 'segments.json', {'segments': changed})
                with self.assertRaisesRegex(ValueError, 'Source identity changed'):
                    media.rebind_no_join(self.root, self.folder, self.plan_path)
                self.assertEqual(media.read(self.folder / 'tts/no_join_recovery.json')['source_segments_sha256'], self.plan['source_segments']['sha256'])

    def test_picture_permission_is_archived_not_reauthorized_after_text_change(self):
        old = {'schema': 'scoped_group_picture_holds_v1', 'job': 'sample',
            'source_segments_sha256': self.plan['source_segments']['sha256'],
            'source_video_sha256': self.plan['source_binding']['source_video_sha256'], 'groups': [{'old': True}]}
        media.save(self.folder / 'tts/picture_holds.json', old)
        self.rows[1]['text_zh'] = '新的中文句子'
        media.save(self.folder / 'segments.json', {'segments': self.rows})
        result = media.rebind_no_join(self.root, self.folder, self.plan_path)
        self.assertFalse((self.folder / 'tts/picture_holds.json').exists())
        self.assertEqual(media.read(self.folder / result['archived_picture_policy']['path']), old)

    def test_no_policies_is_noop_and_costs_no_recovery(self):
        result = media.rebind_no_join(self.root, self.folder, self.folder / 'missing_old_plan.json')
        self.assertFalse(result['changed'])
        self.assertFalse((self.folder / 'tts/media_recovery_journal.json').exists())


class AcousticTests(Fixture):
    def test_synthetic_drone_and_enveloped_speechlike_probe(self):
        drone, info = self.make_wave('drone.wav')
        varied, other = self.make_wave('varied.wav', modulated=True)
        before = media.sha(drone)
        flat = media.wav_metrics(drone, info)
        dynamic = media.wav_metrics(varied, other)
        self.assertTrue(flat['acoustically_suspicious'])
        self.assertFalse(dynamic['acoustically_suspicious'])
        self.assertTrue(flat['finite_samples'])
        self.assertEqual(media.sha(drone), before)

    def test_native_pcm24_geometry_and_drone_probe(self):
        import numpy as np
        rate = 24000
        samples = (np.sin(2 * np.pi * 180 * np.arange(rate) / rate) * 1000000).astype(np.int32)
        unsigned = samples & 0xffffff
        packed = np.column_stack((unsigned & 255, unsigned >> 8 & 255, unsigned >> 16 & 255)).astype(np.uint8)
        path = self.folder / 'pcm24.wav'
        with wave.open(str(path), 'wb') as handle:
            handle.setnchannels(1); handle.setsampwidth(3); handle.setframerate(rate); handle.writeframes(packed.tobytes())
        result = media.wav_metrics(path, {'sha256': media.sha(path), 'frames': rate, 'sample_rate': rate, 'duration_s': 1.})
        self.assertEqual(result['sample_width'], 3)
        self.assertTrue(result['acoustically_suspicious'])
        self.assertTrue(result['finite_samples'])

    def test_sha_geometry_and_duration_mismatch_fail_closed(self):
        path, values = self.make_wave()
        for field, value in [('sha256', '0' * 64), ('frames', 1), ('duration_s', 2.)]:
            altered = {**values, field: value}
            with self.subTest(field=field), self.assertRaises(ValueError):
                media.wav_metrics(path, altered)

    def test_quality_uses_target_free_asr_only_for_suspects(self):
        drone, flat = self.make_wave('tts/drone.wav')
        varied, dynamic = self.make_wave('tts/varied.wav', modulated=True)
        self.manifest['utterances'] = [{'segment_id': g['group_id'], 'audio': str(path),
            'speaker_id': 'voice', **values} for g, path, values in
            zip(self.plan['tts_groups'], [drone, varied], [flat, dynamic])]
        media.save(self.manifest_path, self.manifest)
        observations = []
        class Observer:
            def observe(inner, audio):
                observations.append(audio)
                path = self.folder / 'tts/asr/actual.json'
                media.save(path, {'segments': [{'text': '无关内容'}]})
                return media.read(path), path, False
        def audio_binding(_group, audio, _manifest):
            return {'path': audio['audio'], 'sha256': audio['sha256'], 'duration_s': audio['duration_s']}
        import generic_group_alignment as ga
        with patch.object(ga, 'audio_binding', side_effect=audio_binding):
            result = media.audio_quality(self.root, self.plan_path, self.manifest_path,
                self.folder / 'tts/quality.json', observer=Observer(), normalize=lambda s: ''.join(c for c in s if c.isalnum()))
        self.assertEqual(len(observations), 1)
        self.assertEqual(result['needs_repair_ids'], ['row01', 'row02'])
        self.assertEqual(result['flagged_groups'], [{'group_id': 'groupA', 'member_ids': ['row01', 'row02']}])
        self.assertTrue(result['complete'])
        self.assertFalse(result['human_listened'])


class PictureTests(Fixture):
    POLICY = {'enabled': True, 'picture_holds': True}
    def run_picture(self, ep, ap, policy=None):
        return media.prepare_picture_holds(self.root, self.folder, self.plan_path, self.folder / 'source.json', ep, ap,
            self.POLICY if policy is None else policy)

    def test_per_group_audio_bound_holds_and_idempotence(self):
        ep, ap = self.picture_fixture()
        with patch.object(media, 'extract_frame', side_effect=self.fake_frame):
            result = self.run_picture(ep, ap)
            again = self.run_picture(ep, ap)
        self.assertTrue(result['changed'])
        self.assertFalse(again['changed'])
        policy = media.read(self.folder / 'tts/picture_holds.json')
        self.assertEqual(policy['groups'][0]['member_ids'], ['row01', 'row02'])
        self.assertLessEqual(policy['groups'][0]['max_freeze_s'], 4.)
        self.assertFalse(policy['human_reviewed'])
        self.assertEqual(len(media.journal(self.folder)['entries']), 1)

    def test_actual_audio_change_after_edl_stops_picture_authorization(self):
        ep, ap = self.picture_fixture()
        audio = Path(media.read(ep)['utterances'][0]['audio'])
        audio.write_bytes(audio.read_bytes() + b'changed')
        with patch.object(media, 'extract_frame') as extract:
            with self.assertRaisesRegex(ValueError, 'WAV SHA changed'):
                self.run_picture(ep, ap)
        self.assertFalse(extract.called)
        self.assertFalse((self.folder / 'tts/picture_holds.json').exists())

    def test_existing_bound_picture_holds_are_preserved_when_another_group_needs_repair(self):
        ep, ap = self.picture_fixture()
        with patch.object(media, 'extract_frame', side_effect=self.fake_frame):
            self.run_picture(ep, ap)
        first = media.read(self.folder / 'tts/picture_holds.json')['groups'][0]
        audio, values = self.make_wave('tts/groupB.wav', 5.)
        edl, audit = media.read(ep), media.read(ap)
        edl['utterances'][1].update(audio=str(audio), audio_sha256=values['sha256'],
            **{k: v for k, v in values.items() if k != 'sha256'})
        edl['clips'][1].update(source_end_s=8., frames=134, speech_s=5.)
        issue = {'group_id': 'groupB', 'reason': 'whole_group_audio_exceeds_source_window', 'speech_s': 5., 'available_source_s': 4.}
        edl['issues'], audit['issues'] = [issue], [issue]
        media.save(ep, edl); media.save(ap, audit)
        with patch.object(media, 'extract_frame', side_effect=self.fake_frame):
            result = self.run_picture(ep, ap)
        entries = media.read(self.folder / 'tts/picture_holds.json')['groups']
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]['member_ids'], first['member_ids'])
        self.assertEqual(entries[0]['audio_sha256'], first['audio_sha256'])
        self.assertEqual(result['authorized_groups'], 2)

    def test_four_second_limit_does_not_get_raised(self):
        ep, ap = self.picture_fixture(duration=8.2)
        with patch.object(media, 'extract_frame') as extract:
            result = self.run_picture(ep, ap)
        self.assertFalse(result['changed'])
        self.assertEqual(result['overlong_ids'], ['row01', 'row02'])
        self.assertFalse(extract.called)
        self.assertFalse((self.folder / 'tts/picture_holds.json').exists())

    def test_blank_last_frame_retreat_is_bounded_and_increases_same_hold(self):
        ep, ap = self.picture_fixture()
        times = []
        def frame(ffmpeg, source, timestamp, output):
            result = self.fake_frame(ffmpeg, source, timestamp, output)
            times.append(timestamp)
            result['usable'] = len(times) > 2
            return result
        with patch.object(media, 'extract_frame', side_effect=frame):
            self.run_picture(ep, ap)
        entry = media.read(self.folder / 'tts/picture_holds.json')['groups'][0]
        self.assertAlmostEqual(entry['picture_source_end_s'], 3.88)
        self.assertAlmostEqual(entry['frame_time_s'], 3.84)
        self.assertLess(4 - entry['picture_source_end_s'], .5)
        self.assertGreater(entry['max_freeze_s'], 1.36)

    def test_all_blank_frames_stop_without_text_change_or_permission(self):
        ep, ap = self.picture_fixture()
        def blank(ffmpeg, source, timestamp, output):
            result = self.fake_frame(ffmpeg, source, timestamp, output)
            return {**result, 'usable': False}
        with patch.object(media, 'extract_frame', side_effect=blank):
            result = self.run_picture(ep, ap)
        self.assertEqual(result['blank_frame_ids'], ['row01', 'row02'])
        self.assertEqual(result['overlong_ids'], [])
        self.assertFalse(result['changed'])
        self.assertFalse((self.folder / 'tts/picture_holds.json').exists())
        self.assertEqual(media.read(self.folder / 'segments.json')['segments'], self.rows)

    def test_policy_denial_and_wrong_audit_fail_before_any_frame(self):
        ep, ap = self.picture_fixture()
        with self.assertRaisesRegex(ValueError, 'does not allow'):
            self.run_picture(ep, ap, {'enabled': True, 'picture_holds': False})
        audit = media.read(ap)
        audit['plan_sha256'] = '0' * 64
        media.save(ap, audit)
        with self.assertRaisesRegex(ValueError, 'another source/plan'):
            self.run_picture(ep, ap)
        self.assertFalse((self.folder / 'tts/picture_holds.json').exists())

    def test_blank_gray_frame_detector_cpu_probe(self):
        import numpy as np
        self.assertFalse(media.frame_is_usable(bytes(160 * 90))['usable'])
        self.assertFalse(media.frame_is_usable(bytes([255]) * (160 * 90))['usable'])
        pattern = np.tile(np.arange(160, dtype=np.uint8), (90, 1))
        self.assertTrue(media.frame_is_usable(pattern.tobytes())['usable'])

    @unittest.skipUnless(Path('/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg').exists(), 'Local ffmpeg unavailable')
    def test_actual_ffmpeg_cpu_blank_frame_probe(self):
        ffmpeg = '/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg'
        video = self.folder / 'probe.mp4'
        subprocess.run([ffmpeg, '-v', 'error', '-nostdin', '-f', 'lavfi', '-i', 'color=c=black:s=160x90:d=1:r=25',
            '-c:v', 'libx264', '-threads', '1', '-y', str(video)], check=True, capture_output=True)
        frame = self.folder / 'probe.jpg'
        result = media.extract_frame(ffmpeg, video, .5, frame)
        self.assertTrue(frame.is_file())
        self.assertFalse(result['usable'])
        self.assertTrue(result['black'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
