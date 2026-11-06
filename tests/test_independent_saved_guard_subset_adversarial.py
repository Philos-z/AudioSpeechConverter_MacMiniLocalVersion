"""Independent adversarial checks for source-bound safe-subset replay."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'web'), str(ROOT / 'tests')]
import pipeline
import test_translation_recovery as fixtures
import translation_recovery as repair
import website_recovery as recovery


class IndependentSavedGuardSubsetTests(unittest.TestCase):
    def fixture(self):
        f = fixtures.TranslationRecoveryTests('test_fresh_job_without_revision_recovers_only_actual_rejected_row')
        f.setUp()
        self.addCleanup(f.doCleanups)
        eid = f.saved_partial_fixture(whole=True, bad_target=True)
        path = f.root / 'web/state/jobs' / (f.jid + '.json')
        state = recovery._read(path)
        state['options'] = {'autonomy_policy': 'evidence_first_v1'}
        fixtures.write(path, state)
        return f, eid

    def current_rows(self, f):
        rows, _ = pipeline.reviewed_source_plan(f.folder, f.folder / 'segments.plan.json', f.source_sha)
        return rows

    def test_repeated_resume_after_pipeline_rebind_does_not_spend_or_call(self):
        f, eid = self.fixture()
        repair.resume_partial_review(f.root, f.jid, eid)
        fixtures.write(f.folder / 'translation/input.json', {'segments': self.current_rows(f), 'source_language': 'de'})
        before = (f.folder / 'recovery/website_journal.json').read_bytes()
        calls = copy.deepcopy(f.calls)
        with patch('proxy_client.ProxyClient', side_effect=AssertionError('no API')), \
             patch('source_audio_recheck.recheck', side_effect=AssertionError('no ASR')):
            for _ in range(3):
                result = repair.resume_partial_review(f.root, f.jid, eid)
                self.assertEqual(result['status'], 'reused')
                self.assertEqual(result['new_proxy_calls'], 0)
                self.assertEqual(result['accepted_ids'], ['line0002', 'line0003'])
                self.assertIn('line0001', repair.source_pending(f.folder, self.current_rows(f)))
        self.assertEqual((f.folder / 'recovery/website_journal.json').read_bytes(), before)
        self.assertEqual(f.calls, calls)

    def test_newer_same_binding_diagnostic_rejection_vetoes_completed_subset_replay(self):
        f, eid = self.fixture()
        repair.resume_partial_review(f.root, f.jid, eid)
        f.rows = self.current_rows(f)
        fixtures.write(f.folder / 'translation/input.json', {'segments': f.rows, 'source_language': 'de'})
        f.later_rejection_fixture(diagnostic=True)
        before = (f.folder / 'recovery/website_journal.json').read_bytes()
        with self.assertRaises(recovery.RecoveryReviewRequired):
            repair.resume_partial_review(f.root, f.jid, eid)
        self.assertEqual((f.folder / 'recovery/website_journal.json').read_bytes(), before)
        self.assertIn('line0002', repair.source_pending(f.folder, f.rows))

    def test_interrupted_subset_resumes_and_keeps_bad_row_without_budget_change(self):
        f, eid = self.fixture()
        before = recovery._journal(f.folder)
        atomic = recovery._atomic

        def interrupt(path, data):
            if path == f.folder / 'revisions/source_review_v1/segments.plan.json':
                raise KeyboardInterrupt('independent subset interruption')
            return atomic(path, data)

        with patch.object(recovery, '_atomic', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                repair.resume_partial_review(f.root, f.jid, eid)
        applying = recovery._journal(f.folder)
        self.assertEqual(applying['events'][-1]['partial_review']['status'], 'applying')
        result = repair.resume_partial_review(f.root, f.jid, eid)
        after = recovery._journal(f.folder)
        self.assertEqual(result['status'], 'reused')
        self.assertEqual(len(after['events']), len(before['events']))
        self.assertEqual([e['proxy_calls'] for e in after['events']], [e['proxy_calls'] for e in before['events']])
        self.assertEqual(after['events'][-1]['status'], 'failed')
        self.assertIn('line0001', repair.source_pending(f.folder, self.current_rows(f)))

    def test_phase_cannot_promote_locally_failed_row_to_safe_subset(self):
        f, eid = self.fixture()
        repair.resume_partial_review(f.root, f.jid, eid)
        event = recovery._journal(f.folder)['events'][-1]
        phase = event['partial_review']
        phase.update(segment_ids=['line0001', 'line0002', 'line0003'], rejected_ids=[],
                     whole_saved_review_replay=True, local_guard_pending_ids=[])
        phase['id'] = recovery._hash({'schema': repair.PARTIAL_SCHEMA, 'event_id': event['id'],
            'request_sha256': phase['request_sha256'], 'audit_sha256': phase['audit_sha256'],
            'approved_ids': phase['segment_ids'], 'rejected_ids': phase['rejected_ids']})
        # The original guard partition must still bind the failed row even
        # when the mutable phase's list and identity are consistently changed.
        with self.assertRaises(recovery.RecoveryReviewRequired):
            repair._partial_phase_valid(f.folder, event, phase)

    def test_final_allowed_action_can_still_save_zero_call_safe_subset(self):
        f, _ = self.fixture()
        journal = recovery._journal(f.folder)
        template = copy.deepcopy(journal['events'][-1])
        journal['events'] = []
        for number in range(recovery.MAX_ACTIONS - 1):
            event = copy.deepcopy(template)
            event.update(id=hashlib.sha256(('independent old event ' + str(number)).encode()).hexdigest(),
                         input_fingerprint=hashlib.sha256(('independent old input ' + str(number)).encode()).hexdigest(),
                         reason='review_rejected')
            event.pop('saved_review', None)
            journal['events'].append(event)
        fixtures.write(f.folder / 'recovery/website_journal.json', journal)
        calls_before = len(f.calls)
        result = repair.run_automatic(f.root, f.jid, client_factory=f.partial_client_factory,
                                     recheck_factory=f.partial_recheck_factory)
        self.assertEqual(result['accepted_ids'], ['line0002', 'line0003'])
        self.assertEqual(result['rejected_ids'], ['line0001'])
        self.assertEqual(len(f.calls) - calls_before, 2)
        self.assertEqual(len(recovery._journal(f.folder)['events']), recovery.MAX_ACTIONS)


if __name__ == '__main__':
    unittest.main()
