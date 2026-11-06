"""Independent accounting identity checks, using only temporary local evidence."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'web'))
import autonomous_content_review as review
from proxy_client import ProxyTransportError


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False))


class BudgetIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.folder = self.root / 'jobs/web_budget_identity'
        self.folder.mkdir(parents=True)
        self.video = self.root / 'source.mp4'
        self.video.write_bytes(b'immutable source fixture, no decoding needed by text review')
        write(self.folder / 'source.json', {'input': str(self.video)})
        write(self.root / 'web/state/jobs/web_budget_identity.json',
              {'source_sha256': hashlib.sha256(self.video.read_bytes()).hexdigest()})
        self.rows = [{'segment_id': 'line0001', 'speaker_id': 'S01',
                      'start_s': 0., 'end_s': 2., 'source_language': 'de',
                      'text_source': 'Es sind 3 Menschen.', 'text_de': 'Es sind 3 Menschen.',
                      'text_zh': '有3人。'}]
        write(self.folder / 'segments.plan.json', {'segments': self.rows})
        write(self.folder / 'translation/input.json', {'segments': self.rows})
        self.calls = 0
        owner = self

        class Client:
            model = 'offline-budget-identity'
            config = {'base_url': 'http://127.0.0.1:0/v1'}
            def __init__(self): self.records = []
            def json(self, system, request):
                owner.calls += 1
                raise ProxyTransportError('temporary local typed fixture', {
                    'schema': 'bounded_stage_failure_v1', 'origin': 'proxy_transport',
                    'stage': 'translation', 'category': 'http_response', 'http_status': 503})

        self.factory = lambda root: Client()
        identity = review._client_identity(self.factory(self.root))
        self.snapshot = review._snapshot(self.root, self.folder, self.rows, identity)

    def run_review(self):
        return review.ensure_review(self.root, self.folder, self.rows, client_factory=self.factory)

    def request(self):
        return review._requests(self.snapshot)[0]

    def two_failed_calls(self):
        for _ in range(2):
            with self.assertRaises(review.ContentReviewIncomplete): self.run_review()
        return review._journal(self.folder)

    def test_only_code_and_per_request_scheduler_cap_are_removed_from_budget_key(self):
        request = self.request()
        changed = copy.deepcopy(self.snapshot)
        changed['code_sha256'] = {key: 'f' * 64 for key in changed['code_sha256']}
        changed['max_per_request'] = 2
        upgraded = review._requests(changed)[0]
        self.assertNotEqual(review._fp(request), review._fp(upgraded))
        self.assertEqual(review._attempt_identity(request), review._attempt_identity(upgraded))

    def test_complete_source_target_model_prompt_evidence_and_context_stay_bound(self):
        original = self.request()
        variants = []
        for key in ('input_source_sha256', 'prompt_sha256'):
            changed = copy.deepcopy(self.snapshot); changed[key] = 'b' * 64; variants.append((key, changed))
        changed = copy.deepcopy(self.snapshot)
        changed['bindings']['translation/source_quality_notes.json'] = 'c' * 64
        variants.append(('evidence binding', changed))
        for key in ('model', 'endpoint_sha256', 'reasoning_effort'):
            changed = copy.deepcopy(self.snapshot); changed['model_configuration'][key] = 'changed'
            variants.append(('model ' + key, changed))
        for key in ('text_source', 'text_zh', 'source_language', 'authority'):
            changed = copy.deepcopy(self.snapshot); changed['whole_script'][0][key] = 'changed'
            variants.append(('row ' + key, changed))
        changed = copy.deepcopy(self.snapshot)
        changed['whole_script'][0]['source_binding']['speaker_id'] = 'S02'
        variants.append(('speaker binding', changed))
        changed = copy.deepcopy(self.snapshot)
        changed['whole_script'][0]['source_binding']['end_s'] = 3.
        variants.append(('time binding', changed))
        for name, snapshot in variants:
            with self.subTest(name=name):
                self.assertNotEqual(review._attempt_identity(original),
                                    review._attempt_identity(review._requests(snapshot)[0]))
        # A far row outside both this batch and its two-row adjacent context is
        # still part of the budget's complete whole-script input identity.
        extended = copy.deepcopy(self.snapshot)
        extended['whole_script'] = [copy.deepcopy(extended['whole_script'][0]) for _ in range(20)]
        for i, row in enumerate(extended['whole_script']): row['id'] = 'line%04d' % (i + 1)
        before = review._requests(extended)[0]
        after = copy.deepcopy(extended); after['whole_script'][-1]['text_zh'] += '有新事实。'
        self.assertEqual(before['items'], review._requests(after)[0]['items'])
        self.assertEqual(before['next_context'], review._requests(after)[0]['next_context'])
        self.assertNotEqual(review._attempt_identity(before),
                            review._attempt_identity(review._requests(after)[0]))

    def test_saved_budget_key_must_match_the_actual_full_diagnostic(self):
        journal = self.two_failed_calls()
        journal['events'][0]['attempt_identity_sha256'] = 'e' * 64
        write(self.folder / review.JOURNAL, journal)
        with self.assertRaisesRegex(review.ContentReviewError, '预算身份与诊断不符'): self.run_review()
        self.assertEqual(self.calls, 2)

    def test_rehashed_diagnostic_cannot_change_the_original_request_identity(self):
        journal = self.two_failed_calls()
        event = journal['events'][0]
        path = self.folder / event['diagnostic']['path']
        record = json.loads(path.read_text())
        record['request']['identity']['input_source_sha256'] = 'e' * 64
        write(path, record)
        event['diagnostic']['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        write(self.folder / review.JOURNAL, journal)
        with self.assertRaisesRegex(review.ContentReviewError, '真实完整请求'): self.run_review()
        self.assertEqual(self.calls, 2)

    def test_legacy_unverifiable_interruption_is_spent_and_fails_closed(self):
        request = self.request()
        journal = review._journal(self.folder)
        journal['events'] = [{'id': 'b' * 32, 'request_sha256': review._fp(request),
                              'status': 'reserved', 'started_at_unix': 1.}]
        write(self.folder / review.JOURNAL, journal)
        changed = {key: 'f' * 64 for key in review._code_identity()}
        with patch.object(review, '_code_identity', return_value=changed):
            with self.assertRaisesRegex(review.ContentReviewError, '缺少可验证输入'): self.run_review()
        self.assertEqual(self.calls, 0)
        self.assertEqual(review._journal(self.folder)['events'][0]['status'], 'interrupted')


if __name__ == '__main__': unittest.main()
