"""Independent bounded-reader checks; fixtures only, no models or real jobs."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'web'))
sys.path.insert(0, str(ROOT / 'tests'))
import failure_triage as triage
import website_recovery as recovery
import pipeline
import test_current_audio_review_failure as fixture


class LargeAudioEvidenceIndependentTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.CurrentAudioReviewFailureTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def padded_composition(self, size=9 * 1024**2):
        f = self.f
        value = json.loads(f.current_path.read_text())
        value['independent_padding'] = ''
        base = json.dumps(value, ensure_ascii=False).encode()
        value['independent_padding'] = ' ' * (size - len(base))
        fixture.write(f.current_path, value)
        self.assertEqual(f.current_path.stat().st_size, size)
        entry = f.entries[-1]
        selection_path = Path(entry['result']['selection'])
        selection = json.loads(selection_path.read_text())
        selection['composition'] = fixture.ref(f.folder, f.current_path)
        fixture.write(selection_path, selection)
        entry['result']['outputs'] = [fixture.ref(f.folder, selection_path),
                                    fixture.ref(f.folder, f.current_path)]
        f.save_journal()
        f.quality = f.quality_for(f.current_path, f.pending)
        fixture.write(f.quality_path, f.quality)

    def test_nine_mib_full_chain_returns_current_negative_not_old_failure(self):
        self.padded_composition()
        f = self.f
        with self.assertRaisesRegex(ValueError, '读取上限'):
            recovery._read(f.current_path)
        self.assertIsNone(triage._read(f.current_path))
        error = f.error()
        record = f.diagnose(error)
        self.assertEqual(record['segment_ids'], f.pending)
        self.assertEqual((record['category'], record['retry_policy']),
                         ('audio', 'after_change'))
        self.assertNotIn('line0154', record['segment_ids'])
        f.job['failure_record'] = record
        self.assertFalse(triage.public_failure(f.root, f.job)['can_retry'])

    def test_exact_sixteen_mib_is_bounded_and_readable(self):
        self.padded_composition(16 * 1024**2)
        self.assertEqual(self.f.error().failure_metadata['segment_ids'], self.f.pending)

    def test_over_sixteen_mib_is_rejected_even_with_rebound_real_hashes(self):
        self.padded_composition(16 * 1024**2 + 1)
        with self.assertRaisesRegex(ValueError, 'Oversized'):
            triage._audio_evidence_document(self.f.current_path)
        with self.assertRaisesRegex(ValueError, '绑定不完整'):
            self.f.error()

    def test_small_truncated_and_invalid_encoding_never_become_evidence(self):
        for raw in (b'{"schema":', b'\xff', b'not JSON'):
            with self.subTest(raw=raw):
                p = self.f.root / 'broken.json'
                p.write_bytes(raw)
                with self.assertRaises((ValueError, UnicodeError)):
                    triage._audio_evidence_document(p)

    def test_within_bound_changed_sha_and_selection_ref_are_rejected(self):
        self.padded_composition()
        f = self.f
        metadata = f.error().failure_metadata
        raw = f.current_path.read_bytes()
        f.current_path.write_bytes(raw + b' ')
        self.assertIsNone(triage._current_audio_review(f.root, f.folder, metadata))
        f.current_path.write_bytes(raw)
        entry = f.entries[-1]
        selection_path = Path(entry['result']['selection'])
        selection = json.loads(selection_path.read_text())
        selection['composition']['sha256'] = '0' * 64
        fixture.write(selection_path, selection)
        entry['result']['outputs'][0] = fixture.ref(f.folder, selection_path)
        f.save_journal()
        self.assertIsNone(triage._current_audio_review(f.root, f.folder, metadata))

    def test_large_current_composition_does_not_accept_earlier_chain_terminal(self):
        self.padded_composition()
        f = self.f
        metadata = f.error().failure_metadata
        old = f.quality_for(f.old_path, [sid for sid in f.ids if sid != 'line0154'])
        fixture.write(f.old_quality_path, old)
        metadata = copy.deepcopy(metadata)
        metadata['bindings']['quality'] = fixture.ref(f.folder, f.old_quality_path)
        metadata['bindings']['manifest'] = fixture.ref(f.folder, f.old_path)
        metadata['segment_ids'] = old['needs_review_ids']
        metadata['group_ids'] = old['needs_review_groups']
        self.assertIsNone(triage._current_audio_review(f.root, f.folder, metadata))

    def test_large_pending_formal_alternate_still_stops_without_generation(self):
        self.padded_composition()
        f = self.f
        flow = f.flow()
        manifest = json.loads(f.current_path.read_text())
        with patch('acoustic_fallback.prepare', side_effect=AssertionError('no new attempt')) as prepare, \
                patch('acoustic_fallback.finish', side_effect=AssertionError('no commit')) as finish:
            with self.assertRaises(pipeline.AudioContentReviewRequired) as caught:
                flow.handle_unconfirmed_audio_quality(f.quality, f.plan_path,
                                                      f.current_path, f.quality_path, manifest)
        self.assertEqual(caught.exception.failure_metadata['segment_ids'], f.pending)
        prepare.assert_not_called()
        finish.assert_not_called()
        flow.command.assert_not_called()


if __name__ == '__main__':
    unittest.main()
