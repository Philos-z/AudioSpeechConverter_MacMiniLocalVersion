"""User adoption has its own provenance and never fabricates a Proxy approval."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'web'))
import pipeline
import proxy_client as proxy
import test_translation_cache_revalidation as cache_tests
from translation_constraints import approved_target_errors


class UserContextAdoptionIntegrationTests(unittest.TestCase):
    def setUp(self):
        f = cache_tests.TranslationCacheRevalidationTests(); f.setUp()
        self.addCleanup(f.doCleanups); self.fixture = f
        f.rows = [{**f.rows[0], 'segment_id': 'line%04d' % sid, 'start_s': 3.*i,
                   'end_s': 3.*i+2.} for i, sid in enumerate((107, 108, 109))]
        self.adopted = {'line0108': {'text_zh': '有3人。', 'source_binding': proxy.source_binding(f.rows[1]),
            'decision_ref': {'path': 'jobs/web_fixture/adoption.json', 'sha256': 'a'*64},
            'review_method': 'user_contextual_translation_adoption',
            'human_reviewed': False, 'source_words_verified': False, 'proxy_approved': False}}
        module = types.ModuleType('user_context_adoption')
        module.load_adoptions = lambda root, folder, rows: copy.deepcopy(self.adopted)
        self.loader = patch.dict(sys.modules, {'user_context_adoption': module})
        self.loader.start(); self.addCleanup(self.loader.stop)

    def test_single_adoption_is_excluded_from_proxy_and_cache_without_losing_neighbor_context(self):
        f = self.fixture; updated, report = f.run_translation()
        self.assertEqual(len(f.client.calls), 2)
        for data in f.client.calls:
            self.assertEqual([item['id'] for item in data['items']], ['line0107', 'line0109'])
            self.assertEqual(data['items'][0]['next_context'], f.rows[1]['text_de'])
            self.assertEqual(data['items'][1]['previous_context'], f.rows[1]['text_de'])
        actual = updated[1]
        self.assertEqual(actual['translation_review_method'], 'user_contextual_translation_adoption')
        self.assertIsNone(actual['translation_model']); self.assertIs(actual['proxy_approved'], False)
        self.assertIs(actual['source_words_verified'], False); self.assertIs(actual['human_listened'], False)
        self.assertIs(actual['join_previous'], False)
        self.assertEqual(report['proxy_reviewed_rows'], 2); self.assertEqual(report['user_context_adopted_rows'], 1)
        self.assertEqual(report['numeric_check_coverage'],'per_row_language_invariants_with_separate_user_context_adoptions')
        self.assertEqual(f.checkpoint()['completed'], 3)
        for path in (f.root/'cache'/proxy.CACHE_NAMESPACE).glob('*.json'):
            value = json.loads(path.read_text())
            self.assertNotIn('line0108', [item['id'] for item in value['input']['items']])
        pipeline.validate_translations(f.rows, updated, report, folder=f.folder)

    def test_only_adopted_row_needs_no_proxy_request_or_cache(self):
        f = self.fixture; f.rows = [f.rows[1]]
        updated, report = f.run_translation()
        self.assertEqual(f.client.calls, []); self.assertEqual(updated[0]['text_zh'], '有3人。')
        self.assertFalse(list((f.root/'cache').rglob('*.json')))
        self.assertEqual(report['proxy_reviewed_rows'], 0)
        pipeline.validate_translations(f.rows, updated, report, folder=f.folder)

    def test_new_numeric_conflict_still_stops_before_proxy(self):
        f = self.fixture; self.adopted['line0108']['text_zh'] = '有4人。'
        with self.assertRaises(proxy.InvariantRejectedError): f.run_translation()
        self.assertEqual(f.client.calls, []); self.assertEqual(f.checkpoint()['completed'], 0)

    def test_actual_target_or_provenance_tampering_cannot_pass_pipeline(self):
        f = self.fixture; updated, report = f.run_translation()
        for mutation in ('target', 'ref', 'proxy_method', 'pure_report'):
            changed = copy.deepcopy(updated); result = copy.deepcopy(report)
            if mutation == 'target': changed[1]['text_zh'] += '还发生了别的事。'
            elif mutation == 'ref': changed[1]['decision_ref']['sha256'] = 'b'*64
            elif mutation == 'proxy_method': changed[1]['translation_review_method'] = 'proxy_second_pass_and_numeric_checks'
            else:
                result['review_method'] = 'two_pass_machine_review_and_numeric_invariants'
                result.pop('user_context_adoptions')
            with self.subTest(mutation=mutation), self.assertRaises(RuntimeError):
                pipeline.validate_translations(f.rows, changed, result, folder=f.folder)

    def test_user_hint_is_an_exact_lock_with_a_distinct_source_flag(self):
        row = self.fixture.rows[1]; target = self.adopted['line0108']['text_zh']
        hint = {'user_contextual_adoption': True, 'source_text_sha256': hashlib.sha256(row['text_de'].encode()).hexdigest(),
                'approved_text_zh': target, 'approved_text_zh_sha256': hashlib.sha256(target.encode()).hexdigest(),
                'required_strings': [target]}
        self.assertEqual(approved_target_errors(row, target, hint), [])
        self.assertTrue(approved_target_errors(row, target+'多加一句。', hint))
        hint['machine_explicit_continuation'] = True
        self.assertTrue(approved_target_errors(row, target, hint))


if __name__ == '__main__':
    unittest.main()
