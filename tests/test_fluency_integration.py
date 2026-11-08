"""Offline fluent/strict translation, whole-review and recovery integration.

All source bytes, Chinese text, jobs, authorities and responses are synthetic.
The fake Proxy never contacts a service and no ASR/TTS is loaded.
"""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'web'))
import autonomous_content_review as whole
import editorial_policy as policy
import pipeline
import proxy_client as proxy
import translation_recovery as repair
import website_recovery as recovery


def write(path, value):
    recovery._save(path, value)


def read(path):
    return json.loads(path.read_text())


class FluencyIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.folder = self.root / 'jobs/web_fluency_fixture'; self.folder.mkdir(parents=True)
        self.state_path = self.root / ('web/state/jobs/' + self.folder.name + '.json')
        for source in (ROOT / 'web').glob('*.py'):
            target = self.root / 'web' / source.name; target.parent.mkdir(exist_ok=True)
            shutil.copyfile(source, target)
        self.video = self.root / 'inputs/source.mp4'; self.video.parent.mkdir()
        self.video.write_bytes(b'SYNTHETIC SOURCE IDENTITY, NEVER DECODED')
        self.source_sha = hashlib.sha256(self.video.read_bytes()).hexdigest()
        self.rows = [
            {'segment_id': 'line0001', 'speaker_id': 'S01', 'start_s': 0., 'end_s': 2.,
             'text_de': 'Es sind 3 Menschen.', 'text_source': 'Es sind 3 Menschen.',
             'source_language': 'de', 'source_ids': ['seg0001']},
            {'segment_id': 'line0002', 'speaker_id': 'S01', 'start_s': 3., 'end_s': 5.,
             'text_de': 'Sie kommen morgen nicht.', 'text_source': 'Sie kommen morgen nicht.',
             'source_language': 'de', 'source_ids': ['seg0002']}]
        write(self.folder / 'source.json', {'input': str(self.video), 'duration_s': 10., 'audio_track': 0})
        write(self.folder / 'speakers.json', {'S01': {'label': 'synthetic speaker'}})
        write(self.folder / 'segments.plan.json', {'segments': self.rows})
        write(self.folder / 'translation/input.json', {'segments': self.rows, 'source_language': 'de'})
        self.state = {'id': self.folder.name, 'status': 'failed', 'source_sha256': self.source_sha,
                      'options': {'editorial_policy': policy.FLUENCY,
                                  'website_recovery': {'enabled': True, 'source_mode': 'contextual', 'reserve_gib': 8}}}
        write(self.state_path, self.state)
        self.calls = []; self.lock = threading.Lock(); self.correct = False
        self.reject = False; self.mutate = None; self.backend = 'one'

    def set_mode(self, mode):
        self.state['options']['editorial_policy'] = mode
        write(self.state_path, self.state)

    def target(self, sid):
        values = (['有3人。', '他们明天不会来。'] if self.correct
                  else ['有5位居民。', '他们明天会来。'])
        return values[int(sid[-4:]) - 1]

    def factory(self, root):
        outer = self
        class FakeProxy:
            model = 'offline-review'
            reasoning_effort = 'high'
            config = {'base_url': 'http://offline.invalid/' + outer.backend,
                      'model': model, 'reasoning_effort': reasoning_effort}
            def __init__(self):
                self.records = []
            def json(self, system, data):
                with outer.lock:
                    outer.calls.append({'system': system, 'data': copy.deepcopy(data)})
                self.records.append({'model': self.model, 'reasoning_effort': 'high',
                                     'text_only': True, 'elapsed_s': .001})
                if outer.mutate:
                    outer.mutate(system, data)
                if 'whole_script' in data:
                    return {'rows': [{'id': item['id'], 'approved': True, 'issues': []}
                                     for item in data['items']]}
                recovering = any('source_alternatives' in item for item in data['items'])
                audit = 'draft' in data or any('draft_zh' in item for item in data['items'])
                answer = []
                for item in data['items']:
                    row = {'id': item['id'], 'text_zh': outer.target(item['id'])}
                    if recovering:
                        row['source_choice'] = 'original'
                    if audit:
                        row.update(approved=not outer.reject,
                                   issues=['中文不通顺'] if outer.reject else [])
                    answer.append(row)
                return {'translations': answer}
        return FakeProxy()

    def translate(self):
        with patch.object(proxy, 'ProxyClient', side_effect=self.factory):
            pipeline.translate_job(self.root, self.folder)
        return read(self.folder / 'segments.json')['segments'], read(self.folder / 'translation/report.json')

    def checkpoint(self):
        rowhash = hashlib.sha256(json.dumps(self.rows, sort_keys=True, ensure_ascii=False,
                                          separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        write(self.folder / 'translation/checkpoint.json', {
            'schema': 'translation_checkpoint_v1', 'input_rows_sha256': rowhash,
            'total': 2, 'completed': 0, 'batches': {'line0001': {'status': 'failed',
                'ids': ['line0001', 'line0002'], 'error_type': 'ReviewRejectedError',
                'review_rejections': [{'id': 'line0001', 'approved': False, 'issues': ['中文不通顺']}]}}})

    def test_real_two_pass_pipeline_and_whole_review_preserve_boundaries_without_claiming_fidelity(self):
        rows, report = self.translate()
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(report['source_facts_complete'])
        self.assertFalse(report['semantic_equivalence_required'])
        self.assertEqual(report['review_method'], 'two_pass_machine_fluency_review')
        self.assertEqual([row['source_ids'] for row in rows], [row['source_ids'] for row in self.rows])
        self.assertTrue(proxy.invariants(self.rows[0]['text_de'], rows[0]['text_zh']))
        self.assertEqual(proxy.invariants(self.rows[0]['text_de'], rows[0]['text_zh'], editorial_policy=policy.FLUENCY), [])
        pipeline.validate_translations(self.rows, rows, report, folder=self.folder)
        reviewed = whole.ensure_review(self.root, self.folder, rows, client_factory=self.factory)
        self.assertEqual(reviewed['status'], 'passed')
        self.assertFalse(reviewed['strict_semantic_review_passed'])
        count = len(self.calls)
        whole.ensure_review(self.root, self.folder, rows, client_factory=self.factory)
        self.translate()
        self.assertEqual(len(self.calls), count)
        self.assertTrue(all(call['data'].get('editorial_policy') == policy.FLUENCY
                            or call['data'].get('identity', {}).get('editorial_policy') == policy.FLUENCY
                            for call in self.calls))

    def test_strict_mode_cannot_reuse_fluent_report_or_cache(self):
        rows, report = self.translate(); count = len(self.calls)
        self.set_mode(policy.STRICT)
        with self.assertRaises((ValueError, RuntimeError)):
            pipeline.validate_translations(self.rows, rows, report, folder=self.folder)
        self.correct = True
        rows, strict = self.translate()
        self.assertEqual(len(self.calls), count + 2)
        self.assertNotIn('editorial_policy', strict)
        pipeline.validate_translations(self.rows, rows, strict, folder=self.folder)

    def test_bad_text_wrong_policy_and_changed_source_binding_are_rejected(self):
        with self.assertRaises(ValueError):
            proxy.translate_segments(self.rows, self.root, diagnostic_dir=self.folder / 'translation/diagnostics',
                                     editorial_policy=policy.STRICT)
        for bad in ('', '\ufffd坏掉', '\x00坏掉'):
            self.assertTrue(proxy.invariants('完整源句', bad, editorial_policy=policy.FLUENCY))
        rows, report = self.translate()
        rows[0]['source_ids'] = ['unrelated']
        with self.assertRaises((ValueError, RuntimeError)):
            pipeline.validate_translations(self.rows, rows, report, folder=self.folder)

    def test_recovery_keeps_source_reference_no_recognition_and_spends_real_two_calls(self):
        self.checkpoint()
        with patch('source_audio_recheck.recheck', side_effect=AssertionError('no ASR needed')):
            result = repair.run(self.root, self.folder.name, client_factory=self.factory)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(len(self.calls), 2)
        events = read(self.folder / 'recovery/website_journal.json')['events']
        self.assertEqual(events[0]['proxy_calls'], 2)
        evidence = self.folder / ('revisions/source_review_v1/evidence/translation_' + events[0]['id'])
        request = read(evidence / 'request.json')
        self.assertEqual(request['editorial_policy'], policy.FLUENCY)
        self.assertEqual(request['items'][0]['source_alternatives'], {'original': self.rows[0]['text_de']})
        self.assertFalse(read(evidence / 'line0001_source_recheck.json')['asr_performed'])
        self.assertEqual(read(self.folder / 'segments.plan.json')['segments'], self.rows)

    def test_mode_change_never_grants_third_recovery_for_same_source(self):
        self.set_mode(policy.STRICT); self.checkpoint(); self.reject = True; self.correct = True
        def recheck(root, folder, rows, directory, source_sha):
            return {row['segment_id']: {'alternatives': {'original': row['text_de']},
                    'evidence': [], 'human_reviewed': False, 'source_words_verified': False} for row in rows}
        for _ in range(2):
            with self.assertRaises(recovery.RecoveryReviewRequired):
                repair.run(self.root, self.folder.name, client_factory=self.factory, recheck_factory=recheck)
        journal = self.folder / 'recovery/website_journal.json'
        original, count = journal.read_bytes(), len(self.calls)
        self.set_mode(policy.FLUENCY)
        with self.assertRaises(recovery.RecoveryBudgetExhausted):
            repair.run(self.root, self.folder.name, client_factory=self.factory)
        self.assertEqual(journal.read_bytes(), original)
        self.assertEqual(len(self.calls), count)

    def test_whole_budget_keeps_old_attempts_across_policy_and_backend_changes(self):
        self.correct = True; self.set_mode(policy.STRICT)
        rows, _ = self.translate(); self.calls.clear()
        whole.ensure_review(self.root, self.folder, rows, client_factory=self.factory)
        self.backend = 'two'; whole.ensure_review(self.root, self.folder, rows, client_factory=self.factory)
        journal = self.folder / whole.JOURNAL; original = journal.read_bytes()
        self.set_mode(policy.FLUENCY); self.backend = 'three'
        with self.assertRaises(whole.ContentReviewBudgetExhausted):
            whole.ensure_review(self.root, self.folder, rows, client_factory=self.factory)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(journal.read_bytes(), original)

    def test_current_hold_blocks_calls_and_cancel_during_audit_preserves_raw_only(self):
        self.state['cancel_requested_at'] = 'synthetic cancellation'; write(self.state_path, self.state)
        with self.assertRaises(ValueError): self.translate()
        self.assertEqual(self.calls, [])
        self.state.pop('cancel_requested_at'); write(self.state_path, self.state)
        def cancel(system, data):
            if any('draft_zh' in item for item in data.get('items', [])):
                self.state['cancel_requested_at'] = 'synthetic inflight cancellation'
                write(self.state_path, self.state)
        self.mutate = cancel
        with self.assertRaises(ValueError): self.translate()
        self.assertEqual(len(self.calls), 2)
        self.assertFalse((self.folder / 'segments.json').exists())
        self.assertGreaterEqual(len(list((self.folder / 'translation/diagnostics').glob('*.json'))), 2)


if __name__ == '__main__':
    unittest.main()
