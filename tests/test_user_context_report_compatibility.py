"""Mixed user/Proxy reports must pass the real shared pipeline validator."""
import copy
import hashlib
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'web'))
import failure_triage
import pipeline
import translation_reuse
import test_user_context_adoption_integration as integration

write = integration.cache_tests.write


class UserContextReportCompatibilityTests(unittest.TestCase):
    def setUp(self):
        owner = integration.UserContextAdoptionIntegrationTests(); owner.setUp()
        self.addCleanup(owner.doCleanups); self.fixture = owner.fixture
        self.fixture.root = self.fixture.root.resolve()
        self.fixture.folder = self.fixture.folder.resolve()
        self.fixture.diagnostics = self.fixture.diagnostics.resolve()
        for row in self.fixture.rows:
            row['text_source'] = row['text_de']
        self.updated, self.report = self.fixture.run_translation()

    def publish(self, updated):
        f = self.fixture
        speakers = f.folder/'speakers.json'; revision = f.folder/'source_plan_revision.json'
        write(speakers, {'fixture': 'speakers'}); write(revision, {'fixture': 'revision'})
        segments = f.folder/'segments.json'; report_path = f.folder/'translation/report.json'
        write(segments, {'segments': updated})
        report = copy.deepcopy(self.report)
        sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
        report.update(source_segments_sha256=sha(segments), source_speakers_sha256=sha(speakers),
                      source_revision_receipt_sha256=sha(revision))
        write(report_path, report)
        write(f.folder/'pipeline_receipts.json', {'translation': {'outputs': [
            {'path': str(path), 'sha256': sha(path)} for path in (segments, report_path)]}})

    def test_triage_accepts_mixed_receipt_only_after_actual_validation(self):
        f = self.fixture; self.publish(self.updated)
        with patch.object(pipeline, 'validate_translations', wraps=pipeline.validate_translations) as gate:
            self.assertTrue(failure_triage._translation_receipt_valid(f.folder, f.checkpoint(), f.rows))
            self.assertEqual(gate.call_args.kwargs['folder'], f.folder)
        changed = copy.deepcopy(self.updated); changed[1]['text_zh'] += '未获准的附加事实。'
        self.publish(changed)  # All file SHAs are current; target proof must stop it.
        self.assertFalse(failure_triage._translation_receipt_valid(f.folder, f.checkpoint(), f.rows))

    def test_reuse_noop_still_validates_a_mixed_report(self):
        f = self.fixture
        with patch.object(pipeline, 'validate_translations', wraps=pipeline.validate_translations) as gate:
            actual, report = translation_reuse.apply_prior_reviews(f.root, f.folder, f.rows, self.updated, self.report)
            self.assertIs(actual, self.updated); self.assertIs(report, self.report)
            gate.assert_called_once()
        changed = copy.deepcopy(self.updated); changed[1]['text_zh'] += '未获准的附加事实。'
        with self.assertRaises(RuntimeError):
            translation_reuse.apply_prior_reviews(f.root, f.folder, f.rows, changed, self.report)

    def test_prior_review_configuration_must_never_cover_a_user_adopted_id(self):
        f = self.fixture
        write(f.folder/translation_reuse.CONFIG, {'schema': translation_reuse.SCHEMA,
            'input_source_sha256': 'a'*64, 'rows': {'line0108': {
                'source_binding': self.report['user_context_adoptions']['line0108']['source_binding'],
                'diagnostic': {'path': 'jobs/web_fixture/prior.json', 'sha256': 'b'*64}}}})
        with self.assertRaisesRegex(ValueError, '不能覆盖用户明确采纳'):
            translation_reuse.apply_prior_reviews(f.root, f.folder, f.rows, self.updated, self.report)
        self.assertEqual(len(f.client.calls), 2)


if __name__ == '__main__':
    unittest.main()
