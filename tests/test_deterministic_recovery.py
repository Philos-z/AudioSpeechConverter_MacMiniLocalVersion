"""Offline fixed transport recovery checks; no model, network or media work."""
from concurrent.futures import ThreadPoolExecutor
import copy
import errno
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'web'))
import recovery


def metadata(status=503, **changes):
    value = {'schema': recovery.FAILURE_SCHEMA, 'origin': 'proxy_transport',
             'stage': 'translation', 'category': 'http_response', 'http_status': status}
    value.update(changes)
    return value


class DeterministicRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.job = 'web_fixed_recovery_fixture'
        self.error = RuntimeError('Stage failed; private details retained locally')
        self.controller = self.new_controller()
        self.no_proxy = patch.object(recovery.RecoveryController, '_client',
                                     side_effect=AssertionError('Proxy must not be called'))
        self.no_proxy.start()
        self.addCleanup(self.no_proxy.stop)

    def new_controller(self):
        return recovery.RecoveryController(self.root, self.job)

    def decide(self, evidence=None, error=None, stage='translation'):
        return self.controller.deterministic_decision(
            stage, self.error if error is None else error,
            metadata() if evidence is None else evidence)

    def journal(self):
        return json.loads(self.controller.path.read_text())

    def write_journal(self, value):
        self.controller.path.write_text(json.dumps(value))

    def test_verified_transport_gets_persisted_local_decision_without_proxy(self):
        decision = self.decide()
        self.assertEqual(decision['action'], 'retry_same_stage')
        self.assertFalse(decision['will_execute'])
        self.assertEqual(decision['attempts_consumed'], 1)
        self.assertTrue(self.controller.validate_retry_decision(
            decision, 'translation', self.error, metadata()))
        attempt = self.journal()['attempts'][0]
        self.assertEqual(attempt['before'], recovery.classify_error('translation', self.error, metadata()))
        self.assertEqual(attempt['mode'], 'deterministic_transport_policy_v1')
        self.assertEqual(attempt['decision'], decision)
        self.assertIsNone(attempt['verified_cache'])

    def test_reservation_is_saved_before_decision(self):
        saved = []
        original = self.controller._save

        def capture(value):
            saved.append(copy.deepcopy(value))
            original(value)

        with patch.object(self.controller, '_save', side_effect=capture):
            decision = self.decide()
        self.assertEqual(len(saved), 2)
        reserved = saved[0]['attempts'][0]
        self.assertEqual(reserved['state'], 'deciding')
        self.assertIsNone(reserved['decision'])
        self.assertEqual(saved[0]['attempts_consumed'], 1)
        self.assertEqual(saved[1]['attempts'][0]['decision'], decision)

    def test_supported_timeout_and_reset_evidence(self):
        cases = [
            metadata(category='transport_timeout', exception_type='TimeoutError'),
            metadata(category='transport_timeout', exception_type='socket.timeout'),
            metadata(category='connection_reset', exception_type='ConnectionResetError'),
            metadata(category='connection_reset', errno=errno.ECONNABORTED),
        ]
        cases += [metadata(status) for status in (408, 425, 429, 500, 502, 503, 504)]
        for number, evidence in enumerate(cases):
            with self.subTest(evidence=evidence):
                control = recovery.RecoveryController(self.root, 'web_transport_'+str(number))
                result = control.deterministic_decision('translation', self.error, evidence)
                self.assertEqual(result['action'], 'retry_same_stage')
                self.assertTrue(control.validate_retry_decision(result, 'translation', self.error, evidence))

    def test_hard_denials_do_not_consume_budget_or_create_journal(self):
        cases = [
            ('translation', self.error, {}),
            ('translation', self.error, metadata(401)),
            ('translation', self.error, metadata(True)),
            ('translation', self.error, metadata(origin='untrusted')),
            ('translation', self.error, metadata(stage='tts')),
            ('tts', self.error, metadata()),
            ('translation', self.error, metadata(cancelled=True)),
            ('translation', RuntimeError('任务已取消'), metadata()),
            ('translation', PermissionError('denied'), metadata()),
            ('translation', OSError(errno.ENOSPC, 'No space left'), metadata()),
            ('translation', RuntimeError('non-finite logits'), metadata()),
            ('translation', RuntimeError('译文未通过'), metadata()),
            ('translation', self.error, metadata(category='transport_timeout', exception_type='RuntimeError')),
            ('translation', self.error, metadata(category='connection_reset', exception_type='OSError', errno=5)),
        ]
        for stage, error, evidence in cases:
            with self.subTest(stage=stage, evidence=evidence, error=type(error).__name__):
                decision = self.decide(evidence, error, stage)
                self.assertEqual(decision['action'], 'stop_for_review')
                self.assertEqual(decision['attempts_consumed'], 0)
                self.assertFalse(self.controller.path.exists())

    def test_same_failure_is_once_and_lifetime_budget_is_two_across_restart(self):
        first = self.decide()
        self.controller.record_outcome(first['attempt_id'], 'failed')
        self.controller = self.new_controller()
        same = self.decide()
        self.assertEqual(same['policy_code'], 'same_failure_already_attempted')
        self.assertEqual(self.journal()['attempts_consumed'], 1)
        second = self.decide(metadata(502))
        self.assertEqual(second['attempt_id'], 2)
        self.controller.record_outcome(second['attempt_id'], 'failed')
        self.controller = self.new_controller()
        third = self.decide(metadata(504))
        self.assertEqual(third['policy_code'], 'job_recovery_budget_exhausted')
        self.assertEqual(self.journal()['attempts_consumed'], 2)

    def test_outcomes_and_cancellation_revoke_decisions_without_refund(self):
        for outcome in ('completed', 'failed', 'stopped', 'cancelled'):
            with self.subTest(outcome=outcome):
                control = recovery.RecoveryController(self.root, 'web_outcome_'+outcome)
                decision = control.deterministic_decision('translation', self.error, metadata())
                self.assertFalse(control.validate_retry_decision(
                    decision, 'translation', self.error, metadata(cancelled=True)))
                control.record_outcome(1, outcome)
                self.assertFalse(control.validate_retry_decision(decision, 'translation', self.error, metadata()))
                self.assertEqual(control.deterministic_decision(
                    'translation', self.error, metadata())['policy_code'], 'same_failure_already_attempted')
                with self.assertRaises(ValueError):
                    control.record_outcome(1, outcome)

    def test_unfinished_decision_blocks_other_failure(self):
        self.decide()
        result = self.decide(metadata(502))
        self.assertEqual(result['policy_code'], 'recovery_decision_in_flight')
        self.assertEqual(self.journal()['attempts_consumed'], 1)

    def test_interruption_after_reservation_is_spent_and_can_be_closed(self):
        original = self.controller._save
        calls = []

        def fail_decision(value):
            calls.append(True)
            if len(calls) == 2:
                raise OSError('fixture interrupted persistence')
            original(value)

        with patch.object(self.controller, '_save', side_effect=fail_decision):
            result = self.decide()
        self.assertEqual(result['policy_code'], 'journal_decision_not_persisted')
        self.assertEqual(self.journal()['attempts_consumed'], 1)
        self.assertEqual(self.journal()['attempts'][0]['state'], 'deciding')
        self.assertFalse(self.controller.validate_retry_decision(result, 'translation', self.error, metadata()))
        self.controller.record_outcome(1, 'cancelled')
        self.assertEqual(self.journal()['attempts'][0]['decision']['policy_code'], 'decision_interrupted')
        self.assertEqual(self.decide(metadata(502))['attempt_id'], 2)

    def test_returned_output_and_current_failure_must_match_exactly(self):
        decision = self.decide()
        for key, value in (('attempt_id', 2), ('will_execute', True),
                           ('policy_code', 'different'), ('action', 'reuse_verified_outputs'),
                           ('local_policy_allowed', False), ('extra', True)):
            with self.subTest(key=key):
                changed = {**decision, key: value}
                self.assertFalse(self.controller.validate_retry_decision(
                    changed, 'translation', self.error, metadata()))
        self.assertFalse(self.controller.validate_retry_decision(decision, 'translation', self.error, metadata(502)))
        self.assertFalse(self.controller.validate_retry_decision(decision, 'tts', self.error, metadata()))
        self.assertTrue(self.controller.validate_retry_decision(
            {**decision, 'local_policy_allowed': True}, 'translation', self.error, metadata()))

    def test_complete_policy_is_bound_even_fields_outside_fingerprint(self):
        decision = self.decide()
        original = self.journal()
        for key, value in (('allowed_actions', ['retry_same_stage']),
                           ('retryable_reasons', []), ('retryable', False),
                           ('safe_error', 'tampered summary')):
            with self.subTest(key=key):
                changed = copy.deepcopy(original)
                changed['attempts'][0]['before'][key] = value
                self.write_journal(changed)
                self.assertFalse(self.controller.validate_retry_decision(decision, 'translation', self.error, metadata()))
        self.write_journal(original)
        self.assertTrue(self.controller.validate_retry_decision(decision, 'translation', self.error, metadata()))

    def test_changed_fingerprint_identity_or_journal_is_rejected(self):
        decision = self.decide()
        original = self.journal()
        changes = []
        changed = copy.deepcopy(original)
        changed['attempts'][0]['before']['evidence']['http_status'] = 502
        changes.append(changed)
        changed = copy.deepcopy(original); changed['job_id'] = 'other_job'; changes.append(changed)
        changed = copy.deepcopy(original); changed['attempts_consumed'] = 0; changes.append(changed)
        changed = copy.deepcopy(original); changed['attempts'][0]['state'] = 'unknown'; changes.append(changed)
        for value in changes:
            self.write_journal(value)
            self.assertFalse(self.controller.validate_retry_decision(decision, 'translation', self.error, metadata()))
            result = self.decide(metadata(502))
            self.assertEqual(result['action'], 'stop_for_review')
            self.assertEqual(result['policy_code'], 'journal_unavailable_or_invalid')

    def test_concurrent_controllers_authorize_only_one_pending_retry(self):
        gate = threading.Barrier(8)

        def decide(number):
            control = self.new_controller()
            gate.wait(timeout=10)
            return control.deterministic_decision('translation', self.error, metadata(502 if number % 2 else 503))

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(decide, range(8)))
        self.assertEqual(sum(value['action'] == 'retry_same_stage' for value in results), 1)
        self.assertEqual(self.journal()['attempts_consumed'], 1)

    def test_cli_decide_and_cross_process_lock_share_existing_budget(self):
        folder = self.root / 'jobs' / self.job / 'recovery'
        folder.mkdir(parents=True)
        packet = folder / 'packet.json'
        packet.write_text(json.dumps({'schema': 'bounded_recovery_packet_v1',
            'stage': 'translation', 'error_class': 'RuntimeError', 'failure_metadata': metadata()}))
        processes = []
        for number in range(4):
            output = folder / ('decision_'+str(number)+'.json')
            processes.append((output, subprocess.Popen([sys.executable, '-B', str(ROOT / 'web/recovery.py'),
                'decide', '--root', str(self.root), '--job', self.job,
                '--packet', str(packet), '--out', str(output)], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True)))
        results = []
        for output, process in processes:
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, stdout+stderr)
            result = json.loads(output.read_text()); results.append(result)
            if result['action'] == 'retry_same_stage':
                self.assertTrue(self.controller.validate_retry_decision(result, 'translation', self.error, metadata()))
        self.assertEqual(sum(value['action'] == 'retry_same_stage' for value in results), 1)
        self.assertEqual(self.journal()['attempts_consumed'], 1)

    def test_advise_interface_remains_compatible(self):
        class FakeClient:
            def json(self, system, request):
                return {'action': 'retry_same_stage', 'reason_code': 'transient_verified'}

        with patch.object(self.controller, '_client', return_value=FakeClient()):
            decision = self.controller.advice('translation', self.error, metadata())
        self.assertTrue(self.controller.validate_retry_decision(decision, 'translation', self.error, metadata()))
        self.controller.record_outcome(1, 'failed')
        self.assertEqual(self.decide()['policy_code'], 'same_failure_already_attempted')

    def test_cached_advice_still_validates_and_shares_fixed_decision_budget(self):
        class FakeClient:
            def json(self, system, request):
                return {'action': 'retry_same_stage', 'reason_code': 'transient_verified'}

        verified = {'verified': True, 'receipt_sha256': 'a'*64, 'stages': ['asr'],
                    'outputs': [{'sha256': 'b'*64, 'bytes': 123}]}
        with patch.object(self.controller, '_client', return_value=FakeClient()):
            decision = self.controller.advice('translation', self.error, metadata(),
                                              cache_verifier=lambda: verified)
        self.assertTrue(self.controller.validate_retry_decision(decision, 'translation', self.error, metadata()))
        self.assertEqual(self.decide(metadata(502))['policy_code'], 'recovery_decision_in_flight')
        self.controller.record_outcome(1, 'failed')
        self.assertEqual(self.decide(metadata(502))['attempt_id'], 2)

    def test_pending_fixed_decision_cannot_be_upgraded_by_legacy_advice(self):
        self.decide()
        decision = self.controller.advice('translation', self.error, metadata(502))
        self.assertEqual(decision['policy_code'], 'recovery_advice_in_flight')
        self.assertEqual(self.journal()['attempts_consumed'], 1)


if __name__ == '__main__':
    unittest.main()
