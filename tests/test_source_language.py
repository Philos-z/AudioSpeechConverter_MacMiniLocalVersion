"""Offline language, ASR metadata, review and cache boundary checks (no models/API)."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'web'))
import proxy_client
from source_language import SUPPORTED_LANGUAGES, language_label, normalize_language


def source_row(text='Hello.', code=None):
    row = {'segment_id': 's1', 'speaker_id': 'S01', 'start_s': 0., 'end_s': 2.,
           'text_source': text, 'overlap': False}
    if code:
        row['source_language'] = code
    return row


class FakeClient:
    model = 'offline-test-model'

    def __init__(self, approved=True, translation='你好。'):
        self.calls = []
        self.records = []
        self.approved = approved
        self.translation = translation

    def json(self, system, data):
        self.calls.append((system, data))
        return {'translations': [{'id': item['id'], 'text_zh': self.translation,
                'approved': self.approved, 'issues': [] if self.approved else ['归属无法核实'],
                'join_previous': False, 'join_previous_id': None, 'join_reason': '首句',
                'topic': '问候', 'speech_mode': 'narration'} for item in data['items']]}


class SourceLanguageTests(unittest.TestCase):
    def test_bounded_codes_names_and_locale_hints(self):
        self.assertEqual(len(SUPPORTED_LANGUAGES), 100)
        for raw, expected in [('auto', 'auto'), (None, 'auto'), ('德语', 'de'),
                              ('English', 'en'), ('fr-FR', 'fr'), ('JA', 'ja'),
                              ('zh-Hans-CN', 'zh'), ('jv', 'jw')]:
            self.assertEqual(normalize_language(raw), expected)
        self.assertEqual(language_label('es'), '西班牙语')
        for value in ('xx', '', 'en; rm -rf /', {'language': 'en'}, 'auto-anything'):
            with self.assertRaises(ValueError):
                normalize_language(value)
        with self.assertRaises(ValueError):
            normalize_language('auto', allow_auto=False)

    def test_asr_auto_preserves_words_alias_and_detected_language(self):
        spec = importlib.util.spec_from_file_location('offline_asr', ROOT / 'scripts/asr.py')
        asr = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(asr)
        calls = []

        class Model:
            def __init__(self, *args, **kwargs):
                self.kwargs = kwargs

            def transcribe(self, source, **kwargs):
                calls.append(kwargs)
                segment = types.SimpleNamespace(start=1., end=2., text=' Hello. ', words=[
                    types.SimpleNamespace(start=1.1, end=1.8, word='Hello')])
                return [segment], types.SimpleNamespace(language='en', language_probability=.97)

        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'asr.json'
            args = ['asr.py', '--input', folder + '/input.wav', '--model', folder,
                    '--output', str(output), '--language', 'auto']
            with patch.dict(sys.modules, {'faster_whisper': types.SimpleNamespace(WhisperModel=Model)}), patch.object(sys, 'argv', args):
                asr.main()
            result = json.loads(output.read_text())
            self.assertIsNone(calls[-1]['language'])
            self.assertEqual(result['source_language'], 'en')
            self.assertEqual(result['language_probability'], .97)
            row = result['segments'][0]
            self.assertEqual(row['text_source'], row['text_de'])
            self.assertEqual(row['words'], [{'start_s': 1.1, 'end_s': 1.8, 'word': 'Hello'}])
            with patch.dict(sys.modules, {'faster_whisper': types.SimpleNamespace(WhisperModel=Model)}), patch.object(sys, 'argv', args[:-2]):
                asr.main()
            self.assertEqual(calls[-1]['language'], 'de')

    def test_non_german_local_checks_do_not_apply_german_lexicon(self):
        self.assertEqual(proxy_client.invariants('nicht wenn drei', '测试译文', source_language='ja'), [])
        self.assertEqual(proxy_client.invariants('2,5% à 09:30', '09:30时为2.5%。', source_language='fr'), [])
        self.assertTrue(proxy_client.invariants('20 km at 09:30', '09:30时为21公里。', source_language='en'))
        self.assertTrue(proxy_client.invariants('USD 50', '51美元', source_language='en'))
        self.assertEqual(proxy_client.invariants('$50', '50墨西哥比索', source_language='es'), [])
        self.assertEqual(proxy_client.invariants('50$', '50澳元', source_language='en'), [])
        self.assertEqual(proxy_client.invariants('1,234 kg', '1234公斤', source_language='en'), [])
        self.assertTrue(proxy_client.invariants('keine 3 Menschen', '3人', source_language='de'))

    def test_multilingual_two_pass_cache_separation_and_source_alias(self):
        fake = FakeClient()
        rows = [source_row()]
        with tempfile.TemporaryDirectory() as folder, patch.object(proxy_client, 'ProxyClient', return_value=fake):
            updated, report = proxy_client.translate_segments(rows, folder, source_language='en')
            self.assertEqual(len(fake.calls), 2)
            self.assertIn('英语', fake.calls[0][0])
            self.assertIn('逐条完整核对', fake.calls[1][0])
            self.assertEqual(updated[0]['text_de'], rows[0]['text_source'])
            self.assertEqual(updated[0]['source_language'], 'en')
            self.assertTrue(updated[0]['translation_reviewed'])
            self.assertEqual(report['source_language'], 'en')
            self.assertEqual(report['source_input_rows_sha256'], hashlib.sha256(json.dumps(
                rows, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest())
            proxy_client.translate_segments(rows, folder, source_language='en')
            self.assertEqual(len(fake.calls), 2)
            proxy_client.translate_segments(rows, folder, source_language='fr')
            self.assertEqual(len(fake.calls), 4)
            saved = [json.loads(path.read_text()) for path in (Path(folder) / 'cache' / proxy_client.CACHE_NAMESPACE).glob('*.json')]
            self.assertEqual({item['input']['source_language'] for item in saved}, {'en', 'fr'})
        self.assertNotIn('text_de', rows[0])

    def test_language_mismatch_and_alias_drift_fail_before_any_request(self):
        for rows, code in [([source_row(code='fr')], 'en'),
                           ([{**source_row(), 'text_de': 'different'}], 'en'),
                           ([source_row()], 'auto')]:
            with patch.object(proxy_client, 'ProxyClient') as client:
                with self.assertRaises(ValueError):
                    proxy_client.translate_segments(rows, ROOT, source_language=code)
                client.assert_not_called()

    def test_unapproved_non_german_audit_is_not_silently_disabled(self):
        fake = FakeClient(approved=False)
        with tempfile.TemporaryDirectory() as folder, patch.object(proxy_client, 'ProxyClient', return_value=fake):
            with self.assertRaises(proxy_client.ReviewRejectedError):
                proxy_client.translate_segments([source_row()], folder, source_language='ja')
            self.assertEqual(len(fake.calls), 3)
            self.assertFalse(list((Path(folder) / 'cache').rglob('*.json')))

    def test_existing_german_v4_cache_remains_usable_only_for_german(self):
        fake = FakeClient()
        row = {**source_row('Hallo.'), 'text_de': 'Hallo.'}
        row.pop('text_source')
        item = {'id': 's1', 'text_de': 'Hallo.', 'target_characters': 8,
                'previous_context': '', 'next_context': '', 'timing_feedback': None,
                'source_binding': proxy_client.source_binding(row), 'previous_source_binding': None}
        payload = {'version': proxy_client.VERSION, 'model': fake.model, 'items': [item]}
        key = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with tempfile.TemporaryDirectory() as folder, patch.object(proxy_client, 'ProxyClient', return_value=fake):
            cache = Path(folder) / 'cache' / proxy_client.CACHE_NAMESPACE
            cache.mkdir(parents=True)
            (cache / (key + '.json')).write_text(json.dumps({
                'schema': proxy_client.CACHE_SCHEMA, 'input': payload, 'translations': {'s1': '你好。'},
                'continuity_candidates': {}, 'human_reviewed': False,
                'review_method': 'proxy_second_pass_and_numeric_checks',
                'continuity_review_source': 'same_second_pass_as_final_translation'}))
            updated, report = proxy_client.translate_segments([row], folder)
            self.assertEqual(fake.calls, [])
            self.assertEqual(report['cache_batches'], 1)
            self.assertEqual(updated[0]['source_language'], 'de')
            proxy_client.translate_segments([row], folder, source_language='en')
            self.assertEqual(len(fake.calls), 2)


if __name__ == '__main__':
    unittest.main()
