"""Independent coverage for the one-field optional source-uncertainty form."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'web'), str(ROOT / 'tests')]
import autonomous_content_review as review
import test_autonomous_content_review as fixtures


class OptionalSourceLinksTests(unittest.TestCase):
    def fixture(self):
        f = fixtures.AutonomousContentReviewTests('test_full_whole_script_context_actual_calls_and_idempotent_resume')
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.single_batch()
        return f

    def missing_links(self, response, request):
        response['rows'][0] = {
            'id': request['items'][0]['id'], 'approved': False,
            'issues': [{'type': 'source_uncertain', 'detail': '原文词组不能可靠确认。',
                        'source_quote': '3 Menschen', 'translation_quote': ''}]}
        return response

    def request(self, f):
        snapshot = review._snapshot(f.root, f.folder, f.rows, review._client_identity(f.factory(f.root)))
        return review._requests(snapshot)[0]

    def create_old_invalid(self, f):
        f.mutate = self.missing_links
        # Recreate the genuine former strict-schema stop while keeping its
        # actual response, call receipt, timestamp and spent event unchanged.
        with patch.object(review, '_response', side_effect=review.ContentReviewError('former strict schema')):
            with self.assertRaises(review.ContentReviewIncomplete):
                f.run_review()
        event = review._journal(f.folder)['events'][-1]
        self.assertEqual(event['status'], 'invalid_response')
        self.assertEqual(event['failure_type'], 'ContentReviewError')
        return event

    def test_optional_field_normalizes_a_real_rejection_without_mutating_raw(self):
        f = self.fixture()
        request = self.request(f)
        response = self.missing_links({'rows': [{}]}, request)
        original = copy.deepcopy(response)
        parsed = review._response(response, request)
        self.assertFalse(parsed['line0001']['approved'])
        self.assertEqual(parsed['line0001']['issues'][0]['related_ids'], [])
        self.assertEqual(response, original)
        self.assertNotIn('related_ids', response['rows'][0]['issues'][0])

    def test_other_missing_fields_ungrounded_quotes_and_wrong_types_still_fail(self):
        f = self.fixture()
        request = self.request(f)
        for mutation in ('detail', 'source_quote', 'translation_quote', 'ungrounded_source',
                         'ungrounded_target', 'unknown_type', 'translation_mismatch', 'extra_field',
                         'null_links', 'approved_true'):
            with self.subTest(mutation=mutation):
                response = self.missing_links({'rows': [{}]}, request)
                issue = response['rows'][0]['issues'][0]
                if mutation in ('detail', 'source_quote', 'translation_quote'):
                    issue.pop(mutation)
                elif mutation == 'ungrounded_source':
                    issue['source_quote'] = 'not in current source'
                elif mutation == 'ungrounded_target':
                    issue['translation_quote'] = '不存在的译文'
                elif mutation in ('unknown_type', 'translation_mismatch'):
                    issue['type'] = 'unknown' if mutation == 'unknown_type' else mutation
                    issue['translation_quote'] = '3人'
                elif mutation == 'extra_field':
                    issue['untrusted'] = True
                elif mutation == 'null_links':
                    issue['related_ids'] = None
                else:
                    response['rows'][0]['approved'] = True
                with self.assertRaises(review.ContentReviewError):
                    review._response(response, request)

    def test_old_invalid_source_negative_survives_new_approval_and_target_edit(self):
        f = self.fixture()
        event = self.create_old_invalid(f)
        diagnostic = f.folder / event['diagnostic']['path']
        raw_before = diagnostic.read_bytes()
        record = review.recovery._read(diagnostic)
        parsed = review._diagnostic(f.folder, event, record['request'])
        self.assertFalse(parsed['line0001']['approved'])
        f.mutate = None
        f.rows[0]['text_zh'] = '这里有3人。'
        f.plan()
        report = f.run_review()
        self.assertEqual(report['status'], 'rejected')
        self.assertTrue(report['decisions']['line0001']['approved'])
        self.assertFalse(report['findings']['line0001']['approved'])
        self.assertIn(event['diagnostic'], report['historical_negative_diagnostics'])
        self.assertEqual(diagnostic.read_bytes(), raw_before)
        self.assertEqual(review._journal(f.folder)['events'][0], event)
        self.assertEqual(len(f.calls), 2)
        review.verify_report(f.root, f.folder, f.rows, report, client_factory=f.factory)

    def test_old_invalid_is_not_revalidated_for_other_schema_or_failure_types(self):
        f = self.fixture()
        event = self.create_old_invalid(f)
        diagnostic = f.folder / event['diagnostic']['path']
        record = review.recovery._read(diagnostic)
        for failure_type in ('ValueError', 'ProxyTransportError', None):
            changed = copy.deepcopy(event)
            changed['failure_type'] = failure_type
            with self.subTest(failure_type=failure_type), self.assertRaises(review.ContentReviewError):
                review._diagnostic(f.folder, changed, record['request'])
        # The same invalid status must not become reusable if there was no
        # actual optional-field omission in the saved raw response.
        record['response']['rows'][0]['issues'][0]['related_ids'] = []
        fixtures.write(diagnostic, record)
        changed = copy.deepcopy(event)
        changed['diagnostic'] = review._file_ref(f.folder, diagnostic)
        with self.assertRaises(review.ContentReviewError):
            review._diagnostic(f.folder, changed, record['request'])

    def test_code_upgrade_does_not_reset_spent_invalid_attempt_identity(self):
        f = self.fixture()
        event = self.create_old_invalid(f)
        diagnostic = f.folder / event['diagnostic']['path']
        prior = review.recovery._read(diagnostic)['request']
        upgraded = copy.deepcopy(prior)
        upgraded['identity']['code_sha256'] = {'upgraded_checker.py': 'f' * 64}
        upgraded['snapshot_sha256'] = 'e' * 64
        self.assertEqual(review._attempt_identity(prior), review._attempt_identity(upgraded))
        before = (f.folder / review.JOURNAL).read_bytes()
        attempts = review._budget_attempts(f.folder, review._journal(f.folder), upgraded)
        self.assertEqual([item['id'] for item in attempts], [event['id']])
        self.assertEqual((f.folder / review.JOURNAL).read_bytes(), before)
        self.assertFalse(review._transport_failed(event))


if __name__ == '__main__':
    unittest.main()
