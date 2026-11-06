"""Terminal public state presentation is read-only; no journal/budget changes."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'web'))
import task_takeover


class TakeoverPresentationTests(unittest.TestCase):
    def job(self, status, internal='running'):
        return {'status': status, 'options': {'restart_policy': task_takeover.POLICY},
            'takeover': {'state': internal, 'events': [
                {'number': 1, 'progress_marker': 'a' * 64, 'retry_at_unix': 100.},
                {'number': 2, 'progress_marker': 'b' * 64, 'retry_at_unix': 200.}]}}

    def test_terminal_status_controls_public_label_without_changing_record(self):
        for status, label in [('failed', 'needs_attention'), ('completed', 'completed'), ('canceled', 'canceled')]:
            for internal in ('running', 'waiting', 'ready'):
                value = self.job(status, internal); before = copy.deepcopy(value)
                with self.subTest(status=status, internal=internal):
                    result = task_takeover.public_state(value)
                    self.assertEqual(result['state'], label)
                    self.assertEqual(result['used'], 2); self.assertEqual(result['max'], 6)
                    self.assertEqual(value, before)

    def test_active_status_retains_actual_internal_recovery_stage(self):
        for status, internal in [('queued', 'waiting'), ('queued', 'ready'), ('running', 'running')]:
            value = self.job(status, internal)
            self.assertEqual(task_takeover.public_state(value)['state'], internal)

    def test_terminal_status_is_presentable_even_if_takeover_record_is_damaged(self):
        for status, label in [('failed', 'needs_attention'), ('completed', 'completed'), ('canceled', 'canceled')]:
            value = self.job(status); value['takeover'] = []
            before = copy.deepcopy(value); result = task_takeover.public_state(value)
            self.assertEqual(result['state'], label); self.assertEqual(result['used'], 6)
            self.assertEqual(value, before)

    def test_cancel_flag_and_lifetime_count_are_not_cleared_for_presentation(self):
        value = self.job('canceled'); value['cancel_requested_at'] = '2026-10-26T00:00:00Z'
        before = copy.deepcopy(value); result = task_takeover.public_state(value)
        self.assertTrue(result['cancel_requested']); self.assertEqual(result['used'], 2)
        self.assertEqual(value, before)


if __name__ == '__main__': unittest.main()
