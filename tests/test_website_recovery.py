"""Offline recovery contracts. No HTTP, Proxy credentials, models or GPU."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

STAGED = Path(__file__).resolve().parents[1] / 'web' / 'website_recovery.py'
PRODUCTION_WEB = STAGED.parent
sys.path.insert(0, str(PRODUCTION_WEB))
spec = importlib.util.spec_from_file_location('staged_website_recovery_test_module', STAGED)
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def read(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FakeProxy:
    def __init__(self, mode='good', wording='有3座房屋。'):
        self.calls = []
        self.mode = mode
        self.wording = wording

    def __call__(self, root):
        return self

    def json(self, system, data):
        self.calls.append((system, copy.deepcopy(data)))
        if self.mode == 'timeout':
            raise TimeoutError('/private/secret sk-secret endpoint.invalid')
        if self.mode == 'code':
            return {'translations': [], 'command': 'rm -rf /'}
        audit = 'draft' in data
        translations = [{'id': item['id'], 'text_zh': self.wording}
                        for item in data['items']]
        if audit:
            for item in translations:
                item.update(approved=self.mode != 'reject', issues=['不能确认'] if self.mode == 'reject' else [])
        return {'translations': translations}


class WebsiteRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.jid = 'web_fixture_01'
        self.folder = self.root / 'jobs' / self.jid
        self.folder.mkdir(parents=True)
        self.rows = [{'segment_id': 'line' + str(i).zfill(4), 'speaker_id': 'S01',
                      'start_s': float(i * 2), 'end_s': float(i * 2 + 1),
                      'text_de': 'Es sind 3 Häuser.', 'text_source': 'Es sind 3 Häuser.',
                      'source_language': 'de'} for i in range(1, 10)]
        original = copy.deepcopy(self.rows)
        original[0].update(text_de='Es sind 3 Hauser.', text_source='Es sind 3 Hauser.')
        write(self.folder / 'segments.plan.json', {'segments': original})
        write(self.folder / 'revisions/source_review_v1/segments.plan.json', {'segments': self.rows})
        evidence = self.folder / 'revisions/source_review_v1/evidence/asr.json'
        write(evidence, {'source_cross_check': 'fixture'})
        self.revision = {'schema': 'source_plan_revision_v1',
            'original_plan_sha256': sha(self.folder / 'segments.plan.json'),
            'revised_plan_sha256': sha(self.folder / 'revisions/source_review_v1/segments.plan.json'),
            'input_source_sha256': hashlib.sha256(b'original video').hexdigest(),
            'changes': [{'segment_id': 'line0001', 'action': 'replace_source_text',
                         'original_text_sha256': hashlib.sha256(original[0]['text_de'].encode()).hexdigest(),
                         'replacement_text_sha256': hashlib.sha256(self.rows[0]['text_de'].encode()).hexdigest(),
                         'evidence': [{'path': str(evidence.relative_to(self.folder)), 'sha256': sha(evidence)}]}],
            'unresolved': ['line0001'], 'blocking_unresolved': ['line0001']}
        write(self.folder / 'source_plan_revision.json', self.revision)
        write(self.folder / 'translation/input.json', {'segments': self.rows, 'source_language': 'de'})
        translated = [{**row, 'text_zh': '报道显示，目前这里一共有3座房屋。',
                       'translation_reviewed': True, 'human_reviewed': False,
                       'translation_review_method': 'proxy_second_pass_and_numeric_checks'} for row in self.rows]
        write(self.folder / 'segments.json', {'segments': translated})
        self.state = self.root / 'web/state/jobs' / (self.jid + '.json')
        write(self.state, {'id': self.jid, 'source_sha256': self.revision['input_source_sha256'],
                           'options': {'website_recovery': dict(recovery.DEFAULT_POLICY)}})
        self.proxy = FakeProxy()

    def tearDown(self):
        self.temporary.cleanup()

    def run_source(self, **kwargs):
        return recovery.run_source(self.root, self.jid, client_factory=self.proxy, **kwargs)

    def run_compact(self, ids=None, reason='timing', **kwargs):
        return recovery.run_compact(self.root, self.jid, ids or ['line0002'], reason,
                                    client_factory=self.proxy, **kwargs)

    def journal(self):
        return read(self.folder / 'recovery/website_journal.json')

    def test_policy_is_strict_and_defaults_are_copied(self):
        self.assertEqual(recovery.normalize_policy({}), recovery.DEFAULT_POLICY)
        self.assertEqual(recovery.normalize_policy({'reserve_gib': 2})['reserve_gib'], 2)
        for bad in (None, [], {'extra': True}, {'enabled': 1}, {'picture_holds': 0},
                    {'source_mode': 'guess'}, {'reserve_gib': True}, {'reserve_gib': 2.0}, {'reserve_gib': 1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                recovery.normalize_policy(bad)
        changed = recovery.normalize_policy({})
        changed['enabled'] = False
        self.assertTrue(recovery.DEFAULT_POLICY['enabled'])

    def test_source_keeps_uncertainty_and_originals_and_uses_two_independent_calls(self):
        protected = {name: (self.folder / name).read_bytes() for name in
                     ('segments.plan.json', 'segments.json', 'translation/input.json',
                      'revisions/source_review_v1/segments.plan.json')}
        result = self.run_source()
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(len(self.proxy.calls), 2)
        self.assertNotIn('draft', self.proxy.calls[0][1])
        self.assertIn('draft', self.proxy.calls[1][1])
        revision = read(self.folder / 'source_plan_revision.json')
        self.assertEqual(revision['blocking_unresolved'], [])
        self.assertEqual(revision['unresolved'], ['line0001'])
        self.assertFalse(revision['website_contextual_continuation']['source_words_verified'])
        for name, before in protected.items():
            self.assertEqual((self.folder / name).read_bytes(), before)
        hints = read(self.folder / 'translation/source_review_hints.json')
        self.assertEqual(set(hints['rows']), {'line0001'})
        self.assertEqual(hints['rows']['line0001']['source_text_sha256'],
                         hashlib.sha256(self.rows[0]['text_de'].encode()).hexdigest())
        event = self.journal()['events'][0]
        evidence = self.folder / 'recovery/website' / event['id']
        self.assertTrue((evidence / 'before/segments.json').is_file())
        self.assertTrue((evidence / 'editor.json').is_file())
        self.assertTrue((evidence / 'audit.json').is_file())
        self.assertEqual((self.folder / 'translation/source_review_hints.json').stat().st_mode & 0o777, 0o600)
        again = self.run_source()
        self.assertEqual(again['status'], 'noop')
        self.assertEqual(len(self.proxy.calls), 2)
        self.assertEqual(len(self.journal()['events']), 1)

    def test_source_only_eight_rows_per_action_preserving_remaining_gate(self):
        self.revision['unresolved'] = [r['segment_id'] for r in self.rows]
        self.revision['blocking_unresolved'] = list(self.revision['unresolved'])
        write(self.folder / 'source_plan_revision.json', self.revision)
        first = self.run_source()
        self.assertEqual(first['affected_count'], 8)
        self.assertEqual(first['remaining_blocking'], 1)
        self.assertEqual(len(self.proxy.calls[0][1]['items']), 8)
        self.assertEqual(read(self.folder / 'source_plan_revision.json')['blocking_unresolved'], ['line0009'])
        second = self.run_source()
        self.assertEqual(second['affected_count'], 1)
        self.assertEqual(second['remaining_blocking'], 0)

    def test_strict_and_disabled_never_call_proxy(self):
        result = self.run_source(policy={'source_mode': 'strict'})
        self.assertEqual(result['reason'], 'source_review_required')
        with self.assertRaises(recovery.RecoveryReviewRequired):
            self.run_source(policy={'enabled': False})
        self.assertFalse(self.proxy.calls)
        self.assertFalse((self.folder / 'recovery/website_journal.json').exists())

    def test_compact_only_writes_selected_hints_keeps_segments_and_reuses_success(self):
        original = (self.folder / 'segments.json').read_bytes()
        revision = (self.folder / 'source_plan_revision.json').read_bytes()
        self.assertEqual(self.run_compact()['status'], 'succeeded')
        self.assertEqual((self.folder / 'segments.json').read_bytes(), original)
        self.assertEqual((self.folder / 'source_plan_revision.json').read_bytes(), revision)
        hints = read(self.folder / 'translation/source_review_hints.json')
        self.assertEqual(set(hints['rows']), {'line0002'})
        self.assertEqual(hints['rows']['line0002']['required_strings'], ['有3座房屋。'])
        self.assertEqual(self.run_compact()['status'], 'reused')
        self.assertEqual(len(self.proxy.calls), 2)
        self.assertEqual(len(self.journal()['events']), 1)

    def test_new_queued_job_without_folder_has_unused_budget_and_saved_policy(self):
        jid = 'web_new_queued'
        write(self.root / 'web/state/jobs' / (jid + '.json'),
              {'id': jid, 'options': {'website_recovery': {'source_mode': 'strict', 'reserve_gib': 2}}})
        result = recovery.public_recovery(self.root, jid)
        self.assertTrue(result['available'])
        self.assertEqual(result['policy']['source_mode'], 'strict')
        self.assertEqual(result['budget']['used'], 0)
        self.assertEqual(result['budget']['remaining'], 6)
        self.assertFalse((self.root / 'jobs' / jid).exists())

    def test_other_line_hint_action_does_not_invalidate_successful_reuse(self):
        self.run_compact(ids=['line0002'])
        self.run_compact(ids=['line0003'])
        self.assertEqual(len(self.proxy.calls), 4)
        self.assertEqual(self.run_compact(ids=['line0002'])['status'], 'reused')
        self.assertEqual(len(self.proxy.calls), 4)
        self.assertEqual(len(self.journal()['events']), 2)

    def set_preview_rows(self, rows):
        write(self.folder / 'translation/input.json', {'segments': rows, 'source_language': 'de'})
        translated = [{**row, 'text_zh': '报道显示，目前这里一共有3座房屋。',
                       'translation_reviewed': True, 'human_reviewed': False,
                       'translation_review_method': 'proxy_second_pass_and_numeric_checks'} for row in rows]
        write(self.folder / 'segments.json', {'segments': translated})

    def test_preview_compact_accepts_only_exact_prefix_with_uncertainties_outside_preview(self):
        self.revision.update(unresolved=['line0001', 'line0009'], blocking_unresolved=[])
        write(self.folder / 'source_plan_revision.json', self.revision)
        self.set_preview_rows(self.rows[:2])
        before_segments = (self.folder / 'segments.json').read_bytes()
        before_revision = (self.folder / 'source_plan_revision.json').read_bytes()
        self.assertEqual(self.run_compact()['status'], 'succeeded')
        self.assertEqual(len(self.proxy.calls), 2)
        self.assertEqual((self.folder / 'segments.json').read_bytes(), before_segments)
        self.assertEqual((self.folder / 'source_plan_revision.json').read_bytes(), before_revision)
        self.assertEqual(set(read(self.folder / 'translation/source_review_hints.json')['rows']), {'line0002'})

    def test_preview_compact_rejects_subset_reordering_and_source_field_changes(self):
        changed = copy.deepcopy(self.rows[:2])
        changed[0]['speaker_id'] = 'S99'
        for rows in ([self.rows[0], self.rows[2]], self.rows[1:3], list(reversed(self.rows[:2])), changed):
            with self.subTest(ids=[r['segment_id'] for r in rows]):
                self.set_preview_rows(rows)
                with self.assertRaises(ValueError):
                    self.run_compact(ids=[rows[0]['segment_id']])
        self.assertFalse(self.proxy.calls)
        self.assertFalse((self.folder / 'recovery/website_journal.json').exists())

    def test_preview_compact_preserves_valid_complete_source_hints_outside_prefix(self):
        self.set_preview_rows(self.rows[:2])
        outside = {'source_text_sha256': hashlib.sha256(self.rows[8]['text_de'].encode()).hexdigest(),
                   'note': '完整源文先前经独立机器审校的第九句提示。', 'required_strings': ['3']}
        write(self.folder / 'translation/source_review_hints.json',
              {'schema': 'source_review_hints_v1', 'rows': {'line0009': outside}})
        self.assertEqual(self.run_compact()['status'], 'succeeded')
        hints = read(self.folder / 'translation/source_review_hints.json')
        self.assertEqual(hints['rows']['line0009'], outside)
        self.assertEqual(set(hints['rows']), {'line0002', 'line0009'})
        hints['rows']['line0009']['source_text_sha256'] = 'f' * 64
        write(self.folder / 'translation/source_review_hints.json', hints)
        with self.assertRaises(ValueError):
            self.run_compact()
        self.assertEqual(len(self.proxy.calls), 2)

    def test_source_action_still_requires_the_complete_derived_plan(self):
        self.set_preview_rows(self.rows[:2])
        with self.assertRaises(ValueError):
            self.run_source()
        self.assertFalse(self.proxy.calls)
        self.assertEqual(read(self.folder / 'source_plan_revision.json')['blocking_unresolved'], ['line0001'])

    def test_audit_or_numeric_failure_never_mutates_hints_or_revision(self):
        revision = (self.folder / 'source_plan_revision.json').read_bytes()
        self.proxy.mode = 'reject'
        with self.assertRaises(recovery.RecoveryReviewRequired):
            self.run_source()
        self.assertEqual(self.journal()['events'][0]['status'], 'failed')
        self.proxy.mode = 'good'
        self.proxy.wording = '有2座房屋。'
        with self.assertRaises(recovery.RecoveryReviewRequired):
            self.run_source()
        self.assertEqual(self.journal()['events'][1]['reason'], 'invariant_rejected')
        self.assertEqual((self.folder / 'source_plan_revision.json').read_bytes(), revision)
        self.assertFalse((self.folder / 'translation/source_review_hints.json').exists())

    def test_same_input_failure_budget_survives_restart_and_failure_keeps_reservation(self):
        self.proxy.mode = 'timeout'
        for _ in range(2):
            with self.assertRaises(TimeoutError):
                self.run_compact()
        self.assertEqual(len(self.proxy.calls), 2)
        with self.assertRaises(recovery.RecoveryBudgetExhausted):
            self.run_compact()
        self.assertEqual(len(self.journal()['events']), 2)
        self.assertEqual(recovery.public_recovery(self.root, self.jid)['budget']['remaining'], 4)
        # No input/output hints changed, so no false new input grants more calls.
        self.assertFalse((self.folder / 'translation/source_review_hints.json').exists())

    def test_six_action_budget_is_total_across_ids_and_reasons(self):
        self.proxy.mode = 'timeout'
        for number in range(2, 8):
            with self.assertRaises(TimeoutError):
                self.run_compact(ids=['line' + str(number).zfill(4)])
        with self.assertRaises(recovery.RecoveryBudgetExhausted):
            self.run_compact(ids=['line0008'], reason='acoustic')
        public = recovery.public_recovery(self.root, self.jid)
        self.assertEqual(public['budget']['used'], 6)
        self.assertEqual(public['budget']['remaining'], 0)
        self.assertTrue(public['budget']['exhausted'])
        self.assertEqual(public['proxy_calls'], 6)

    def test_successful_actions_still_have_twelve_request_lifetime_ceiling(self):
        for number in range(2, 8):
            self.assertEqual(self.run_compact(ids=['line' + str(number).zfill(4)])['status'], 'succeeded')
        self.assertEqual(len(self.proxy.calls), 12)
        self.assertEqual(recovery.public_recovery(self.root, self.jid)['proxy_calls'], 12)
        # Exact successful requests remain reusable even after the budget is spent.
        self.assertEqual(self.run_compact(ids=['line0002'])['status'], 'reused')
        with self.assertRaises(recovery.RecoveryBudgetExhausted):
            self.run_compact(ids=['line0008'])
        self.assertEqual(len(self.proxy.calls), 12)

    def test_code_or_path_response_is_data_and_rejected_not_executed(self):
        self.proxy.mode = 'code'
        with self.assertRaises(recovery.RecoveryReviewRequired):
            self.run_source()
        self.assertEqual(len(self.proxy.calls), 1)
        self.assertFalse((self.folder / 'translation/source_review_hints.json').exists())

    def test_bad_source_binding_and_evidence_symlink_are_rejected_before_proxy(self):
        self.revision['input_source_sha256'] = 'f' * 64
        write(self.folder / 'source_plan_revision.json', self.revision)
        with self.assertRaises(RuntimeError):
            self.run_source()
        self.revision['input_source_sha256'] = read(self.state)['source_sha256']
        write(self.folder / 'source_plan_revision.json', self.revision)
        evidence = self.folder / 'revisions/source_review_v1/evidence/asr.json'
        outside = self.root / 'outside.json'
        outside.write_bytes(evidence.read_bytes())
        evidence.unlink()
        evidence.symlink_to(outside)
        with self.assertRaises(ValueError):
            self.run_source()
        self.assertFalse(self.proxy.calls)

    def test_compact_rejects_longer_or_changed_source_without_mutation(self):
        self.proxy.wording = '报道显示，目前这里一共有3座房屋。'
        with self.assertRaises(recovery.RecoveryReviewRequired):
            self.run_compact()
        rows = read(self.folder / 'segments.json')
        rows['segments'][1]['speaker_id'] = 'S99'
        write(self.folder / 'segments.json', rows)
        with self.assertRaises(ValueError):
            self.run_compact()
        self.assertEqual(len(self.proxy.calls), 2)
        self.assertFalse((self.folder / 'translation/source_review_hints.json').exists())

    def test_public_summary_never_leaks_text_error_paths_or_credentials(self):
        self.proxy.mode = 'timeout'
        with self.assertRaises(TimeoutError):
            self.run_compact()
        journal = self.journal()
        event = journal['events'][0]
        event.update(raw_exception='/secret sk-secret', private_text='Es sind 3 Häuser.', endpoint='https://secret')
        write(self.folder / 'recovery/website_journal.json', journal)
        public = recovery.public_recovery(self.root, self.jid)
        encoded = json.dumps(public, ensure_ascii=False)
        for forbidden in ('secret', 'Häuser', str(self.root), 'endpoint', 'raw_exception', 'text_zh'):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(public['events'][0]['action'], 'compact')
        self.assertEqual(public['events'][0]['affected_count'], 1)
        self.assertTrue(public['events'][0]['proxy_called'])

    def test_apply_interruption_is_completed_from_verified_staged_data_without_new_calls(self):
        original_finish = recovery._finish_apply
        with patch.object(recovery, '_finish_apply', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_source()
        self.assertEqual(self.journal()['events'][0]['status'], 'applying')
        self.assertTrue(read(self.folder / 'source_plan_revision.json')['blocking_unresolved'])
        self.assertEqual(len(self.proxy.calls), 2)
        with patch.object(recovery, '_finish_apply', wraps=original_finish):
            self.assertEqual(self.run_source()['status'], 'noop')
        self.assertEqual(self.journal()['events'][0]['status'], 'succeeded')
        self.assertEqual(len(self.proxy.calls), 2)

    def test_pending_apply_never_overwrites_input_changed_during_interruption(self):
        with patch.object(recovery, '_finish_apply', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_source()
        current = read(self.folder / 'translation/input.json')
        current['segments'][0]['text_de'] = 'Es sind 4 Häuser.'
        write(self.folder / 'translation/input.json', current)
        with self.assertRaises(recovery.RecoveryReviewRequired):
            self.run_source()
        self.assertFalse((self.folder / 'translation/source_review_hints.json').exists())
        self.assertEqual(self.journal()['events'][0]['reason'], 'concurrent_update')

    def test_partially_written_multi_file_transaction_rolls_forward_without_proxy(self):
        def interrupt_after_hints(folder, journal, event):
            item = next(t for t in event['transaction'] if t['target'] == 'translation/source_review_hints.json')
            recovery._atomic(folder / item['target'], (folder / item['evidence']).read_bytes())
            raise KeyboardInterrupt()
        with patch.object(recovery, '_finish_apply', side_effect=interrupt_after_hints):
            with self.assertRaises(KeyboardInterrupt):
                self.run_source()
        self.assertTrue((self.folder / 'translation/source_review_hints.json').is_file())
        self.assertEqual(read(self.folder / 'source_plan_revision.json')['blocking_unresolved'], ['line0001'])
        self.assertEqual(self.run_source()['status'], 'noop')
        self.assertEqual(read(self.folder / 'source_plan_revision.json')['blocking_unresolved'], [])
        self.assertEqual(self.journal()['events'][0]['status'], 'succeeded')
        self.assertEqual(len(self.proxy.calls), 2)

    def test_abandoned_proxy_call_consumes_budget_without_sticking_pending(self):
        self.proxy.mode = 'timeout'
        with self.assertRaises(TimeoutError):
            self.run_compact()
        journal = self.journal()
        journal['events'][0].update(status='editing', proxy_calls=1)
        write(self.folder / 'recovery/website_journal.json', journal)
        with self.assertRaises(TimeoutError):
            self.run_compact()
        self.assertEqual(self.journal()['events'][0]['status'], 'interrupted')
        with self.assertRaises(recovery.RecoveryBudgetExhausted):
            self.run_compact()

    def test_cli_explicit_strict_policy_overrides_persisted_contextual_without_calls(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = recovery.main(['source', '--root', str(self.root), '--job', self.jid,
                                  '--policy-json', '{"source_mode":"strict"}'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())['reason'], 'source_review_required')
        self.assertFalse((self.folder / 'recovery/website_journal.json').exists())

    def test_episode_without_state_uses_actual_source_clip_hash_and_explicit_policy(self):
        self.state.unlink()
        source = self.root / 'inputs/episode.mp4'
        source.parent.mkdir()
        source.write_bytes(b'original video')
        write(self.folder / 'source.json', {'input': str(source)})
        self.assertEqual(self.run_source(policy={'source_mode': 'strict'})['reason'], 'source_review_required')
        self.assertEqual(self.run_source(policy=recovery.DEFAULT_POLICY)['status'], 'succeeded')
        # A new episode source cannot borrow the old source receipt.
        self.revision['blocking_unresolved'] = ['line0001']
        write(self.folder / 'source_plan_revision.json', self.revision)
        source.write_bytes(b'different original video')
        with self.assertRaises(RuntimeError):
            self.run_source(policy=recovery.DEFAULT_POLICY)

    def test_real_process_death_persists_budget_and_releases_lock(self):
        code = '''import importlib.util, pathlib, sys, time
sys.path.insert(0, sys.argv[2])
spec=importlib.util.spec_from_file_location('killed_website_recovery',sys.argv[1])
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
class WaitingProxy:
    def __init__(self,root): self.root=pathlib.Path(root)
    def json(self,system,data):
        (self.root/'jobs'/sys.argv[4]/'recovery/entered').write_text('entered')
        time.sleep(30)
module.run_compact(sys.argv[3],sys.argv[4],['line0002'],'timing',client_factory=WaitingProxy)
'''
        process = subprocess.Popen([sys.executable, '-B', '-c', code, str(STAGED), str(PRODUCTION_WEB),
                                    str(self.root), self.jid], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            marker = self.folder / 'recovery/entered'
            while not marker.exists() and time.monotonic() < deadline and process.poll() is None:
                time.sleep(.02)
            self.assertTrue(marker.exists(), 'Owned offline child did not reach its fake Proxy call')
            self.assertEqual(self.journal()['events'][0]['status'], 'editing')
            process.kill()
            process.communicate(timeout=5)
            self.proxy.mode = 'timeout'
            with self.assertRaises(TimeoutError):
                self.run_compact()
            journal = self.journal()
            self.assertEqual(len(journal['events']), 2)
            self.assertEqual(journal['events'][0]['status'], 'interrupted')
            self.assertEqual(journal['events'][0]['proxy_calls'], 1)
            with self.assertRaises(recovery.RecoveryBudgetExhausted):
                self.run_compact()
            self.assertFalse((self.folder / 'translation/source_review_hints.json').exists())
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

    def test_cli_only_actual_transport_class_emits_nonce_bound_terminal_event(self):
        import proxy_client
        metadata = {'schema': 'bounded_stage_failure_v1', 'origin': 'proxy_transport',
                    'stage': 'translation', 'category': 'transport_timeout', 'exception_type': 'TimeoutError'}
        class TransportProxy:
            def json(self, system, data):
                proxy_client._raise_transport('local sanitized transport failure', metadata)
        output = io.StringIO()
        with patch.object(proxy_client, 'ProxyClient', return_value=TransportProxy()), \
                patch.dict(os.environ, {proxy_client.STAGE_EVENT_NONCE_ENV: 'a' * 64}), \
                contextlib.redirect_stdout(output):
            code = recovery.main(['source', '--root', str(self.root), '--job', self.jid])
        self.assertEqual(code, 2)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([e.get('event') for e in events[:-1]], ['proxy_transport_failure', 'proxy_stage_failure'])
        self.assertTrue(all(e['nonce'] == 'a' * 64 for e in events[:-1]))
        self.assertEqual(self.journal()['events'][0]['proxy_calls'], 1)
        output = io.StringIO()
        bad = FakeProxy(wording='有2座房屋。')
        with patch.object(proxy_client, 'ProxyClient', return_value=bad), \
                patch.dict(os.environ, {proxy_client.STAGE_EVENT_NONCE_ENV: 'a' * 64}), \
                contextlib.redirect_stdout(output):
            code = recovery.main(['source', '--root', str(self.root), '--job', self.jid])
        self.assertEqual(code, 2)
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertNotIn('nonce', output.getvalue())
        self.assertEqual(self.journal()['events'][1]['reason'], 'invariant_rejected')


if __name__ == '__main__':
    unittest.main()
