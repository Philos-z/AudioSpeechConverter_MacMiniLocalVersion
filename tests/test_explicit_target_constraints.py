"""Current full-sentence locks through cached translation and the TTS gate."""
import copy
import hashlib
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'web'))
import pipeline
import proxy_client
from translation_constraints import validate_approved_targets
import test_translation_cache_revalidation as cache_tests

write = cache_tests.write


class ExplicitTargetConstraintTests(unittest.TestCase):
    def setUp(self):
        self.fixture = cache_tests.TranslationCacheRevalidationTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def hint(self, *, exact=True):
        f = self.fixture; target = '有3人。'
        value = {'source_text_sha256': hashlib.sha256(f.rows[0]['text_de'].encode()).hexdigest(),
                 'note': '已批准完整中文，保留原句。', 'required_strings': [target],
                 'approved_text_zh': target,
                 'approved_text_zh_sha256': hashlib.sha256(target.encode()).hexdigest()}
        if exact:
            value['machine_explicit_continuation'] = True
        write(f.folder/'translation/source_review_hints.json', {
            'schema': 'source_review_hints_v1', 'rows': {'line0001': value}})

    def test_cache_containing_approved_sentence_plus_added_sentence_is_rejected_without_calls(self):
        f = self.fixture; self.hint()
        path, value = f.seed(); value['translations']['line0001'] = '有3人。还发生了别的事。'
        write(path, value); before = path.read_bytes()
        with self.assertRaises(proxy_client.InvariantRejectedError) as caught:
            f.run_translation()
        self.assertTrue(any('整句完全一致' in issue for issue in caught.exception.failures['line0001']))
        self.assertEqual(f.client.calls, []); self.assertEqual(path.read_bytes(), before)
        self.assertEqual(f.checkpoint()['completed'], 0)

    def test_ordinary_name_hint_keeps_substring_behavior(self):
        f = self.fixture; self.hint(exact=False)
        original_json = f.client.json
        def respond(system, data):
            response = original_json(system, data)
            for answer in response['translations']:
                answer['text_zh'] = '有3人。还有补充说明。'
            return response
        f.client.json = respond
        updated, report = f.run_translation()
        self.assertEqual(updated[0]['text_zh'], '有3人。还有补充说明。')
        pipeline.validate_translations(f.rows, updated, report, folder=f.folder)

    def test_actual_published_and_pre_tts_rows_must_match_approval_despite_valid_metadata(self):
        f = self.fixture; self.hint()
        updated, report = f.run_translation()
        pipeline.validate_translations(f.rows, updated, report, folder=f.folder)
        changed = copy.deepcopy(updated); changed[0]['text_zh'] += '再加一个没有批准的句子。'
        with self.assertRaisesRegex(RuntimeError, 'line0001'):
            pipeline.validate_translations(f.rows, changed, report, folder=f.folder)
        with self.assertRaisesRegex(RuntimeError, 'line0001'):
            validate_approved_targets(changed, f.folder)


if __name__ == '__main__':
    unittest.main()
