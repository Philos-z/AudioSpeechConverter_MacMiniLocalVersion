"""Exercise durable content retries with the existing full recovery fixture."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_translation_recovery as fixtures

repair = fixtures.repair
recovery = fixtures.recovery


class AutomaticTranslationRetryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TranslationRecoveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def run_automatic(self, client=None):
        return repair.run_automatic(self.fixture.root, self.fixture.jid,
            client_factory=client or self.fixture.client, recheck_factory=self.fixture.recheck)

    def events(self):
        return json.loads((self.fixture.folder/'recovery/website_journal.json').read_text())['events']

    def test_completed_rejection_retries_once_and_commits_only_second_approval(self):
        fixture = self.fixture
        class Proxy:
            audits = 0
            def json(self, system, data):
                fixture.reject = 'draft' in data and self.audits == 0
                result = fixture.client(fixture.root).json(system, data)
                if 'draft' in data:
                    self.audits += 1
                return result
        proxy = Proxy()
        result = self.run_automatic(lambda root: proxy)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual([event['status'] for event in self.events()], ['failed', 'succeeded'])
        self.assertEqual(len(fixture.calls), 4)
        self.assertEqual(len(fixture.rechecks), 2)
        proof = fixture.folder/'revisions/source_review_v1/evidence'/('translation_'+self.events()[0]['id'])/'automatic_retry.json'
        self.assertFalse(json.loads(proof.read_text())['lifetime_budget_reset'])
        self.assertFalse(result['human_reviewed'])

    def test_second_rejection_stops_and_repeated_resume_does_not_reset_budget(self):
        self.fixture.reject = True
        with self.assertRaises(recovery.RecoveryReviewRequired):
            self.run_automatic()
        self.assertEqual(len(self.events()), 2)
        self.assertEqual(len(self.fixture.calls), 4)
        before = copy.deepcopy(self.events())
        with self.assertRaises(recovery.RecoveryBudgetExhausted):
            self.run_automatic()
        self.assertEqual(self.events(), before)
        self.assertFalse((self.fixture.folder/'translation/source_review_hints.json').exists())

    def test_malformed_audit_is_not_automatically_retried(self):
        fixture = self.fixture
        class Proxy:
            def json(self, system, data):
                if 'draft' in data:
                    fixture.calls.append(copy.deepcopy(data))
                    return {'translations': []}
                return fixture.client(fixture.root).json(system, data)
        with self.assertRaises(recovery.RecoveryReviewRequired):
            self.run_automatic(lambda root: Proxy())
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(len(fixture.calls), 2)

    def test_transport_or_concurrent_source_change_is_not_retried(self):
        self.fixture.change_source = True
        with self.assertRaises((recovery.RecoveryReviewRequired, ValueError)):
            self.run_automatic()
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(len(self.fixture.calls), 2)

    def test_only_exact_lexical_asr_repair_can_remove_stray_nie(self):
        original = 'Die Tür wurde wie nie später geöffnet.'
        candidate = 'Die Tür wurde wenig später geöffnet.'
        self.assertEqual(repair.source_alternative_errors(original, candidate), [])
        self.assertTrue(repair.source_alternative_errors(
            original.replace('wurde', 'wurde nicht'), candidate))
        self.assertTrue(repair.source_alternative_errors(
            'Die Tür wurde nie geöffnet.', 'Die Tür wurde wenig später geöffnet.'))
        self.assertTrue(repair.source_alternative_errors(
            original, candidate.replace('geöffnet', 'geschlossen')))


if __name__ == '__main__':
    unittest.main()
