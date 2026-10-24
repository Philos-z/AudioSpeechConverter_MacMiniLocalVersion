"""Offline tests of explicitly pinned earlier approvals; no API or model calls."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'web'))
import translation_reuse as reuse
from proxy_client import VERSION, source_binding


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False))


class PriorReviewReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.folder = self.root / 'jobs' / 'fixture'
        self.folder.mkdir(parents=True)
        self.video = self.root / 'input.mp4'
        self.video.write_bytes(b'original media')
        self.rows = [{'segment_id': f'line{i:04d}', 'speaker_id': 'S1', 'start_s': float(i),
            'end_s': i + .8, 'text_de': text, 'source_language': 'de'} for i, text in
            enumerate(['Der Beklagte bleibt hier.', 'Es sind 3 Häuser.', 'Sie stehen dort.'], 1)]
        self.updated = [{**row, 'text_zh': '原机器译文。', 'translation_reviewed': True,
            'human_reviewed': False} for row in self.rows]
        self.report = {'translation_version': VERSION, 'model': 'test-model', 'human_reviewed': False,
            'review_method': 'two_pass_machine_review_and_numeric_invariants', 'requests': [{'actual': True}]}
        save(self.folder / 'source.json', {'input': str(self.video)})
        item = {'id': 'line0002', 'source_binding': source_binding(self.rows[1]),
            'text_de': self.rows[1]['text_de'], 'previous_context': self.rows[0]['text_de'],
            'next_context': self.rows[2]['text_de'], 'previous_source_binding': source_binding(self.rows[0])}
        self.diagnostic = {'schema': 'proxy_translation_diagnostic_v1', 'version': VERSION,
            'model': 'test-model', 'stage': 'audit', 'status': 'response_received', 'human_reviewed': False,
            'input': {'source_language': 'de', 'items': [item]},
            'response': {'translations': [{'id': 'line0002', 'approved': True, 'issues': [], 'text_zh': '有3座房屋。'}]}}
        self.path = self.folder / 'translation/diagnostics/saved.json'
        self.config = {'schema': reuse.SCHEMA, 'input_source_sha256': hashlib.sha256(self.video.read_bytes()).hexdigest(),
            'rows': {'line0002': {'source_binding': source_binding(self.rows[1]),
                'diagnostic': {'path': 'translation/diagnostics/saved.json', 'sha256': ''}}}}
        self.write_evidence()

    def write_evidence(self):
        save(self.path, self.diagnostic)
        self.config['rows']['line0002']['diagnostic']['sha256'] = hashlib.sha256(self.path.read_bytes()).hexdigest()
        save(self.folder / reuse.CONFIG, self.config)

    def apply(self):
        with patch('proxy_client.ProxyClient', side_effect=AssertionError('No API is allowed')):
            return reuse.apply_prior_reviews(self.root, self.folder, self.rows, self.updated, self.report)

    def test_missing_configuration_is_noop(self):
        (self.folder / reuse.CONFIG).unlink()
        after, report = self.apply()
        self.assertIs(after, self.updated)
        self.assertIs(report, self.report)

    def test_exact_saved_approval_only_replaces_named_target_and_preserves_history(self):
        original = copy.deepcopy((self.updated, self.report))
        diagnostic_bytes = self.path.read_bytes()
        after, report = self.apply()
        self.assertEqual(after[1]['text_zh'], '有3座房屋。')
        self.assertEqual(after[0], self.updated[0])
        self.assertEqual(after[2], self.updated[2])
        self.assertEqual((self.updated, self.report), original)
        self.assertEqual(self.path.read_bytes(), diagnostic_bytes)
        self.assertEqual(report['requests'], self.report['requests'])
        self.assertEqual(report['prior_review_reuse']['new_api_calls'], 0)
        self.assertFalse(report['prior_review_reuse']['human_reviewed'])
        again, again_report = reuse.apply_prior_reviews(self.root, self.folder, self.rows, after, report)
        self.assertEqual((again, again_report), (after, report))

    def test_context_only_casefold_and_whitespace_are_equivalent(self):
        item = self.diagnostic['input']['items'][0]
        item['previous_context'] = 'DER   beklagte\tbleibt hier.'
        item['previous_source_binding']['text_de_sha256'] = hashlib.sha256(item['previous_context'].encode()).hexdigest()
        item['next_context'] = '  SIE stehen dort. '
        self.write_evidence()
        self.assertEqual(self.apply()[0][1]['text_zh'], '有3座房屋。')

    def test_material_context_change_and_neighbor_identity_are_rejected(self):
        for field, value in [('previous_context', 'Der Beklagte geht weg.'), ('next_context', 'Sie stehen nicht dort.')]:
            with self.subTest(field=field):
                original = copy.deepcopy(self.diagnostic)
                self.diagnostic['input']['items'][0][field] = value
                self.write_evidence()
                with self.assertRaises(ValueError): self.apply()
                self.diagnostic = original
        self.diagnostic['input']['items'][0]['previous_source_binding']['speaker_id'] = 'Other'
        self.write_evidence()
        with self.assertRaises(ValueError): self.apply()

    def test_changed_media_or_current_source_binding_is_rejected(self):
        self.video.write_bytes(b'different source')
        with self.assertRaises(ValueError): self.apply()
        self.video.write_bytes(b'original media')
        self.rows[1]['text_de'] = 'Es sind 4 Häuser.'
        self.updated[1]['text_de'] = self.rows[1]['text_de']
        with self.assertRaises(ValueError): self.apply()

    def test_changed_evidence_hash_is_rejected(self):
        self.path.write_text(self.path.read_text() + '\n')
        with self.assertRaises(ValueError): self.apply()

    def test_only_compatible_actual_audit_or_repair_approval_is_accepted(self):
        for field, value in [('version', 'older'), ('stage', 'draft'), ('status', 'failed'), ('human_reviewed', True)]:
            with self.subTest(field=field):
                original = copy.deepcopy(self.diagnostic)
                self.diagnostic[field] = value
                self.write_evidence()
                with self.assertRaises(ValueError): self.apply()
                self.diagnostic = original
        self.diagnostic['stage'] = 'repair'
        self.write_evidence()
        self.assertEqual(self.apply()[0][1]['text_zh'], '有3座房屋。')

    def test_rejected_or_nonempty_issues_never_reuse(self):
        response = self.diagnostic['response']['translations'][0]
        for approved, issues in [(False, []), (True, ['未确认']), (True, None)]:
            with self.subTest(approved=approved, issues=issues):
                response.update(approved=approved, issues=issues)
                self.write_evidence()
                with self.assertRaises(ValueError): self.apply()

    def test_local_fact_checks_reject_earlier_wrong_number(self):
        self.diagnostic['response']['translations'][0]['text_zh'] = '有4座房屋。'
        self.write_evidence()
        with self.assertRaisesRegex(ValueError, '事实检查'): self.apply()

    def test_cannot_inject_target_via_config_or_escape_diagnostics(self):
        setting = self.config['rows']['line0002']
        setting['text_zh'] = '自选中文'
        save(self.folder / reuse.CONFIG, self.config)
        with self.assertRaises(ValueError): self.apply()
        del setting['text_zh']
        setting['diagnostic']['path'] = 'translation/diagnostics/../../../outside.json'
        save(self.folder / reuse.CONFIG, self.config)
        with self.assertRaises(ValueError): self.apply()

    def test_symlink_evidence_and_duplicate_response_are_rejected(self):
        original = self.path.read_bytes()
        self.path.unlink()
        external = self.root / 'saved.json'
        external.write_bytes(original)
        self.path.symlink_to(external)
        with self.assertRaises(ValueError): self.apply()
        self.path.unlink()
        self.diagnostic['response']['translations'] *= 2
        self.write_evidence()
        with self.assertRaises(ValueError): self.apply()

    def test_continuity_hashes_use_final_chinese_and_changed_row_cannot_join(self):
        for index, row in enumerate(self.updated):
            row['continuity_suggestion'] = {'join_previous': bool(index), 'machine_reviewed': True,
                'source_binding': source_binding(row, row['text_zh']), 'join_reason': '旧建议'}
        self.report['continuity_suggestions'] = {}
        after, report = self.apply()
        self.assertFalse(after[1]['continuity_suggestion']['join_previous'])
        self.assertFalse(after[1]['continuity_suggestion']['machine_reviewed'])
        self.assertEqual(after[2]['continuity_suggestion']['previous_source_binding'], source_binding(after[1], '有3座房屋。'))
        self.assertEqual(report['continuity_suggestions']['machine_reviewed_rows'], 2)

    def test_planner_receipt_keeps_unrelated_join_advice_usable(self):
        from generic_group_plan import suggestion_receipt
        for index, row in enumerate(self.updated):
            row['continuity_suggestion'] = {'join_previous': bool(index), 'machine_reviewed': True,
                'source_binding': source_binding(row, row['text_zh']), 'join_reason': '旧建议'}
        self.report['continuity_suggestions'] = {}
        after, report = self.apply()
        report.update(job='fixture', source_segments_sha256='segments-sha', source_speakers_sha256='speakers-sha')
        save(self.folder / 'translation/report.json', report)
        proposals, receipt = suggestion_receipt(self.folder, 'fixture', 'segments-sha', 'speakers-sha')
        self.assertTrue(receipt['advice_usable'])
        self.assertTrue(after[2]['continuity_suggestion']['join_previous'])
        self.assertEqual(proposals[0]['member_ids'], ['line0002', 'line0003'])


if __name__ == '__main__':
    unittest.main()
