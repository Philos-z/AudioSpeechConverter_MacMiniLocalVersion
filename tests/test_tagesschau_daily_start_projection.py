"""Read-only queue projection boundaries for the autonomous daily entrypoint."""
import copy
from datetime import datetime
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'web'))
import tagesschau_daily_state as tick
import test_tagesschau_daily_tick as legacy


class DailyProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='daily_projection_fixture_')
        self.addCleanup(self.temporary.cleanup)
        private = Path(self.temporary.name).resolve()
        override = patch.object(legacy, 'PRIVATE', private)
        override.start()
        self.addCleanup(override.stop)
        self.fixture = legacy.TickCase(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        fixture = self.fixture
        fixture.config.update(daily_submission_policy='current_date_independent_v1',
            daily_local_time='21:00')
        fixture.save(fixture.root / tick.CONFIG, fixture.config)
        fixture.queue['daily_intents'] = {
            day: {'status': 'waiting_previous', 'episode_key': 'tagesschau:' + day}
            for day in ['2026-11-03', '2026-11-04', '2026-11-07', '2026-11-08']}
        fixture.write_queue()

    def run_at(self, at):
        state, observation = self.fixture.observed(now=datetime.fromisoformat(at))
        plan = tick.plan(state, observation)
        self.assertEqual(plan['proposed_external_actions'], [])
        projection = plan['allowed_patch']['projection']
        self.assertTrue(all(action['execution_allowed'] is False
            for action in projection['next_actions']))
        return projection

    def selected(self, at):
        return [day for day, row in self.run_at(at)['intents'].items()
            if row['next_action'] == 'daily_start_candidate']

    def test_midnight_and_daily_boundary(self):
        for at, expected in [
                ('2026-11-07T19:59:59+00:00', []),
                ('2026-11-07T20:00:00+00:00', ['2026-11-07']),
                ('2026-11-07T23:59:00+00:00', ['2026-11-07']),
                ('2026-11-08T19:59:59+00:00', ['2026-11-07']),
                ('2026-11-08T20:00:00+00:00', ['2026-11-08'])]:
            with self.subTest(at=at):
                self.assertEqual(self.selected(at), expected)

    def test_dst_uses_local_21_hour(self):
        for days, utc in [(['2026-03-28', '2026-03-29'], '2026-03-29T19:00:00+00:00'),
                (['2026-10-24', '2026-10-25'], '2026-10-25T20:00:00+00:00')]:
            self.fixture.queue['daily_intents'] = {
                day: {'status': 'waiting_previous'} for day in days}
            self.fixture.write_queue()
            self.assertEqual(self.selected(utc), [days[-1]])
            if days[0] == '2026-10-24':
                for fold in ['2026-10-25T00:30:00+00:00', '2026-10-25T01:30:00+00:00']:
                    self.assertEqual(self.selected(fold), [days[0]])

    def test_old_hold_does_not_block_new_day_but_current_hold_does(self):
        fixture = self.fixture
        fixture.queue['watched'][legacy.JID]['user_hold'] = {
            'automatic_resumption_allowed': False, 'status': 'deferred_by_user'}
        fixture.queue['daily_intents']['2026-11-03']['user_hold'] = {
            'automatic_resumption_allowed': False}
        fixture.write_queue()
        self.assertEqual(self.selected('2026-11-07T21:00:00+00:00'), ['2026-11-07'])
        for hold in [{'automatic_resumption_allowed': False},
                {'status': 'deferred_by_user'}, {'status': 'paused'}]:
            fixture.queue['daily_intents']['2026-11-07']['user_hold'] = hold
            fixture.write_queue()
            self.assertEqual(self.selected('2026-11-07T21:00:00+00:00'), [])

    def test_completed_latest_does_not_backfill(self):
        self.fixture.queue['daily_intents']['2026-11-07'].update(
            status='completed', job_id=legacy.JID)
        self.fixture.write_queue()
        self.assertEqual(self.selected('2026-11-07T21:00:00+00:00'), [])

    def test_missing_latest_due_intent_preserves_old_backlog(self):
        self.fixture.queue['daily_intents'] = {
            '2026-11-04': {'status': 'waiting_previous', 'local_date': '2026-11-04'}}
        self.fixture.write_queue()
        projection = self.run_at('2026-11-08T10:00:00+00:00')
        self.assertEqual(projection['intents']['2026-11-04']['next_action'],
            'preserve_waiting_previous')
        self.assertEqual([action for action in projection['next_actions']
            if action['kind'] == 'daily_start_candidate'], [])

    def test_unknown_submission_requires_reconcile(self):
        for status in ['submitting', 'submission_unknown']:
            self.fixture.queue['daily_intents']['2026-11-07']['status'] = status
            self.fixture.write_queue()
            projection = self.run_at('2026-11-07T21:00:00+00:00')
            self.assertEqual(projection['intents']['2026-11-07']['next_action'],
                'reconcile_submission_without_repost')
            self.assertEqual(self.selected('2026-11-07T21:00:00+00:00'), [])

    def test_unavailable_worker_and_invalid_schedule(self):
        fixture = self.fixture
        state, binding = tick.snapshot(fixture.root)
        def get(route):
            if route == '/api/liveness':
                return {'version': 'philos_lan_web_v1',
                    'project_identity': tick.digest(str(fixture.root).encode()),
                    'worker_alive': False, 'stopping': False}
            return fixture.local(legacy.JID)
        observation = tick.observe(fixture.root, state, binding, get=get,
            now=datetime.fromisoformat('2026-11-07T21:00:00+00:00'))
        projection = tick.plan(state, observation)['allowed_patch']['projection']
        self.assertEqual(projection['intents']['2026-11-07']['next_action'],
            'preserve_frozen_wait')
        fixture.config['daily_local_time'] = '20:30'
        fixture.save(fixture.root / tick.CONFIG, fixture.config)
        with self.assertRaises(tick.StateError):
            fixture.observed()

    def test_reconcile_changes_only_projection_and_config_cas(self):
        fixture = self.fixture
        state, observation = fixture.observed(now=legacy.NOW)
        plan = tick.plan(state, observation)
        before = copy.deepcopy(state)
        result = tick.apply_projection(fixture.root, plan, now=legacy.NOW)
        self.assertEqual(result['status'], 'applied')
        after, _ = tick.snapshot(fixture.root)
        for key in before:
            if key not in {'generation', 'next_action', 'projection'}:
                self.assertEqual(after[key], before[key])
        state, observation = fixture.observed(now=legacy.NOW)
        plan = tick.plan(state, observation)
        fixture.config['daily_local_time'] = '20:30'
        fixture.save(fixture.root / tick.CONFIG, fixture.config)
        with self.assertRaises(tick.StateError):
            tick.apply_projection(fixture.root, plan, now=legacy.NOW)


if __name__ == '__main__':
    unittest.main()
