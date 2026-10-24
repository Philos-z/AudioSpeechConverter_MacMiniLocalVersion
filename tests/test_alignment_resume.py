"""Needs-review exit must reach bounded recovery, never mask a child crash."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'web'))
import media_recovery as media
import pipeline

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



class AlignmentResumeTests(Fixture):
    def flow(self,code=3,runner=None,cancelled=lambda:False):
        (self.root/'web').mkdir(exist_ok=True)
        (self.root/'web/generic_group_alignment.py').write_text('fixture entry')
        return pipeline.Flow(self.root,{'id':'web_fixture','options':{'website_recovery':{'enabled':True}}},
            lambda *a:None,runner or (lambda *a,**kw:{'returncode':code}),cancelled)

    def run_alignment(self,flow):
        # Fixture uses 'sample'; the production folder was established by Flow.
        flow.folder=self.folder
        return flow.align_group_subtitles(self.plan_path,self.manifest_path,self.index_path.parent)

    def test_exit_three_validated_review_reaches_only_scoped_recovery(self):
        flow=self.flow()
        value=self.run_alignment(flow)
        self.assertFalse(value['all_groups_ready'])
        self.assertFalse((self.folder/'tts/media_recovery_journal.json').exists())
        result=flow.media_recovery().repair_alignment(self.root,self.folder,self.plan_path,self.index_path)
        self.assertTrue(result['changed'])
        self.assertEqual(result['split_groups'],[{'member_ids':['row01','row02']}])
        self.assertEqual(len(media.journal(self.folder)['entries']),1)

    def test_other_nonzero_and_signal_exit_never_use_even_valid_old_review(self):
        for code in (1,2,-15,None):
            with self.subTest(code=code),patch.object(media,'alignment_failure_scope') as check:
                with self.assertRaises(RuntimeError):self.run_alignment(self.flow(code))
                check.assert_not_called()

    def test_missing_index_is_not_accepted(self):
        self.index_path.unlink()
        with self.assertRaises(ValueError):self.run_alignment(self.flow())

    def test_partial_or_success_index_cannot_mask_exit_three(self):
        original=copy.deepcopy(self.index)
        for changes in ({'phase':'aligning'},{'partial_preview':True},
                        {'phase':'complete','all_groups_ready':True}, {'source_row_count':2}):
            self.index={**original,**changes};media.save(self.index_path,self.index)
            with self.subTest(changes=changes),self.assertRaises(ValueError):self.run_alignment(self.flow())

    def test_stale_plan_or_audio_or_receipt_does_not_authorize_resume(self):
        self.plan['extra']='changed after alignment';media.save(self.plan_path,self.plan)
        with self.assertRaises(ValueError):self.run_alignment(self.flow())

    def test_other_valid_native_manifest_does_not_match_current_invocation(self):
        other=self.folder/'tts/another/manifest.json';media.save(other,self.manifest)
        flow=self.flow();flow.folder=self.folder
        with self.assertRaisesRegex(ValueError,'another audio manifest'):
            flow.align_group_subtitles(self.plan_path,other,self.index_path.parent)

    def test_receipt_tampering_is_not_accepted(self):
        self.index['groups'][0]['receipt']['sha256']='0'*64;media.save(self.index_path,self.index)
        with self.assertRaises(ValueError):self.run_alignment(self.flow())

    def test_child_timeout_and_cancellation_do_not_open_recovery(self):
        def timeout(*a,**kw):raise TimeoutError('actual timeout')
        with patch.object(media,'alignment_failure_scope') as check:
            with self.assertRaises(TimeoutError):self.run_alignment(self.flow(runner=timeout))
            with self.assertRaisesRegex(RuntimeError,'取消'):
                self.run_alignment(self.flow(cancelled=lambda:True))
            check.assert_not_called()

    def test_unowned_exit_three_has_no_special_meaning(self):
        with self.assertRaises(RuntimeError):self.flow().command('fixture',0,1,['unowned'])

if __name__=='__main__':unittest.main()
