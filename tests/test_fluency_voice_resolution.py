"""Full synthetic PCM/report replay; no speech models or real media are used."""
import copy
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_speech_content_quality as fixture
import editorial_policy as policy
import fluency_voice_resolution as resolution

media, speech, alignment = fixture.media, fixture.speech, fixture.alignment


class FluencyVoiceResolutionTests(fixture.Fixture):
    def setup_manifest(self):
        groups, native = fixture.WholeManifest.setup_manifest(self)
        for index, group in enumerate(groups):
            group['source_ids'] = ['source_' + str(index)]
        media.save(self.plan_path, self.plan)
        return groups, native

    run_quality = fixture.WholeManifest.run_quality

    def setUp(self):
        # These plain synthetic strings need only a deterministic punctuation
        # normalizer; production OpenCC setup is covered by its own tests.
        normalizer = patch.object(speech, 'normalizer', return_value=
            lambda text: ''.join(char.lower() for char in text if char.isalnum()))
        normalizer.start(); self.addCleanup(normalizer.stop)
        super().setUp()
        helper = self.root / 'web/speech_content_quality.py'; helper.parent.mkdir()
        shutil.copyfile(fixture.ROOT / 'web/speech_content_quality.py', helper)
        self.checked, _, _ = self.run_quality(
            first_text='今天公布的完整结果。', second_text='明天送交议会讨论。')
        self.quality_path = self.folder / 'tts/full_quality.json'
        self.state_path = self.root / ('web/state/jobs/' + self.folder.name + '.json')
        self.state = {'id': self.folder.name, 'source_sha256': self.plan['source_binding']['source_video_sha256'],
                      'options': {'editorial_policy': policy.FLUENCY}}
        media.save(self.state_path, self.state)

    def accept(self, checked=None):
        with patch.object(alignment, 'environment_versions', return_value={}):
            return resolution.accept_quality(self.root, self.folder, self.plan_path,
                self.manifest_path, self.quality_path, self.checked if checked is None else checked)

    def test_complete_evidence_accepts_uncertainty_separately_and_reuses_receipt(self):
        original = self.quality_path.read_bytes()
        report = self.accept()
        self.assertEqual(report['controller_accepted_risk_ids'], ['line1'])
        self.assertEqual(report['machine_status_preserved'], 'needs_content_review')
        for field in ('machine_approved', 'human_listened', 'whole_video_human_reviewed',
                      'waveform_modified', 'machine_findings_rewritten', 'pronunciation_approved'):
            self.assertFalse(report[field], field)
        self.assertEqual(report['new_model_calls'], 0)
        self.assertEqual(self.quality_path.read_bytes(), original)
        self.state['progress'] = 92; media.save(self.state_path, self.state)
        self.assertEqual(self.accept(), report)
        self.assertEqual(len(list((self.folder / 'tts/policy_resolution').rglob('*.json'))), 1)

    def test_legacy_does_not_inherit_default_and_hold_never_creates_receipt(self):
        self.state['options'].clear(); media.save(self.state_path, self.state)
        self.assertIsNone(self.accept())
        self.state['options']['editorial_policy'] = policy.FLUENCY
        self.state['cancel_requested_at'] = 'synthetic cancellation'
        media.save(self.state_path, self.state)
        with self.assertRaises(ValueError):
            self.accept()
        self.assertFalse((self.folder / 'tts/policy_resolution').exists())

    def test_hard_fault_missing_medium_stale_summary_and_changed_wav_are_blocked(self):
        for mutate in (lambda d: d.update(needs_repair_ids=['line1']),
                       lambda d: d.update(checked_utterances=1),
                       lambda d: d['rows'][1].update(medium_observation=None)):
            changed = copy.deepcopy(self.checked); mutate(changed)
            media.save(self.quality_path, changed)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                self.accept(changed)
        media.save(self.quality_path, self.checked)
        wav = Path(media.read(self.manifest_path)['utterances'][0]['audio'])
        wav.write_bytes(wav.read_bytes()[:-2])
        with self.assertRaises(ValueError):
            self.accept()
        self.assertFalse((self.folder / 'tts/policy_resolution').exists())

    def test_cancellation_during_pcm_scan_stops_before_receipt(self):
        original = media.wav_metrics
        def cancel(*args, **kwargs):
            self.state['cancel_requested_at'] = 'synthetic midscan cancellation'
            media.save(self.state_path, self.state)
            return original(*args, **kwargs)
        with patch.object(media, 'wav_metrics', side_effect=cancel), self.assertRaises(ValueError):
            self.accept()
        self.assertFalse((self.folder / 'tts/policy_resolution').exists())

    def install_guard(self, location):
        state = copy.deepcopy(self.state)
        queue = {'watched': {}, 'daily_intents': {}, 'episodes': {}}
        identity = self.folder.name
        if location == 'state':
            state['status'] = 'paused'
        elif location == 'projection':
            queue['projection'] = {'active_decisions': [
                {'job_id': identity, 'status': 'asked_once_waiting_actual_reply'}]}
        elif location == 'nested_state':
            state['pending_voice_decision'] = {'status': 'paused'}
        elif location == 'nested_watch':
            queue['watched'][identity] = {'pending_voice_decision': {'status': 'paused'}}
        else:
            field = {'watch': 'watched', 'intent': 'daily_intents', 'episode': 'episodes'}[location]
            queue[field][identity] = {'job_id': identity, 'status': 'paused'}
        media.save(self.state_path, state)
        media.save(self.root / 'web/state/tagesschau_daily_automation.json', queue)

    def test_pause_and_pending_decision_at_every_current_authority_block_receipts(self):
        original = self.quality_path.read_bytes()
        for location in ('state', 'watch', 'intent', 'episode', 'projection',
                         'nested_state', 'nested_watch'):
            self.install_guard(location)
            with self.subTest(location=location), self.assertRaises(ValueError):
                self.accept()
            self.assertFalse((self.folder / 'tts/policy_resolution').exists())
            self.assertEqual(self.quality_path.read_bytes(), original)
            self.assertEqual(media.read(self.quality_path)['needs_review_ids'], ['line1'])

    def test_midscan_pause_or_pending_decision_blocks_receipt_without_rewriting_quality(self):
        original_metrics = media.wav_metrics
        original_quality = self.quality_path.read_bytes()
        queue_path = self.root / 'web/state/tagesschau_daily_automation.json'
        for location in ('state', 'watch', 'projection', 'nested_state'):
            media.save(self.state_path, self.state)
            media.save(queue_path, {'watched': {}, 'daily_intents': {}, 'episodes': {}})
            installed = []
            def pause(*args, **kwargs):
                if not installed:
                    installed.append(True)
                    self.install_guard(location)
                return original_metrics(*args, **kwargs)
            with self.subTest(location=location), patch.object(media, 'wav_metrics', side_effect=pause), \
                    self.assertRaises(ValueError):
                self.accept()
            self.assertTrue(installed, 'The hold must arrive after PCM verification starts')
            self.assertFalse((self.folder / 'tts/policy_resolution').exists())
            self.assertEqual(self.quality_path.read_bytes(), original_quality)
            self.assertEqual(media.read(self.quality_path)['needs_review_ids'], ['line1'])


if __name__ == '__main__':
    unittest.main()
