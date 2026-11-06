"""Offline fixtures and local HTTP counterexamples; no production mutations."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import email.message
import io
import json
from pathlib import Path
import sys
import tempfile
import os
import urllib.request
import urllib.response
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout

STAGED = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGED / 'web'))
import tagesschau_daily_state as tick
import tagesschau_daily_tick as cli
from task_takeover import InstanceLock, write_json

PRIVATE = STAGED.parent / 'private' / 'queue_test_runtime'
NOW = datetime(2026, 11, 6, 22, 50, tzinfo=timezone.utc)
JID = 'web_20260101_120000_11111111'
OLD = 'web_20260102_120000_22222222'
DONE = 'web_20260103_120000_33333333'


class TickCase(unittest.TestCase):
    def setUp(self):
        PRIVATE.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=PRIVATE)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.calls = []
        self.config = {'schema': 'tagesschau_daily_automation_config_v1', 'root': str(self.root),
            'api_base': 'http://127.0.0.1:8766', 'state_path': 'web/state/tagesschau_daily_automation.json',
            'timezone': 'Europe/Berlin', 'notification': {'to': 'owner@example.invalid'},
            'new_job_reserve_gib': 8}
        self.queue = {'schema': tick.STATE_SCHEMA, 'generation': 12, 'watched': {},
            'daily_intents': {}, 'episodes': {}, 'notifications': {},
            'next_action': 'OLD stale text', 'monitor': {'status': 'ACTIVE', 'daily_status': 'ACTIVE'}}
        self.save(self.root / tick.CONFIG, self.config)
        self.add_job(JID, 'running')
        self.write_queue()

    def save(self, path, doc):
        write_json(path, doc)

    def ref(self, relative, doc):
        path = self.root / relative
        self.save(path, doc)
        return {'path': relative, 'sha256': tick.digest(path.read_bytes())}

    def add_job(self, job_id, status, episode=None):
        local = {'id': job_id, 'status': status, 'stage': status, 'progress': 55.0 if status == 'running' else 100.0,
            'updated_at': NOW.isoformat(), 'source_url': 'https://official.invalid/' + job_id + '.mp4',
            'source_sha256': 'a' * 64, 'artifacts': [], 'options': {'website_recovery': {'reserve_gib': 8}},
            'cancel_requested_at': None}
        self.save(self.root / 'web/state/jobs' / (job_id + '.json'), local)
        self.queue['watched'][job_id] = {'job_id': job_id, 'status': status,
            'source_url': local['source_url'], 'source_sha256': local['source_sha256'],
            'completion_verification': None, 'email': {'status': 'pending', 'attempts': 0, 'to': 'owner@example.invalid'},
            'new_generation_authorized': False, 'historical_budget': {'used': 6, 'max': 6}}
        if episode:
            self.queue['watched'][job_id]['episode_key'] = episode
        return local

    def write_queue(self):
        self.save(self.root / self.config['state_path'], self.queue)

    def local(self, job_id):
        return tick.read_file(self.root, 'web/state/jobs/' + job_id + '.json')[0]

    def get(self, route):
        self.calls.append(route)
        if route == '/api/liveness':
            return {'version': 'philos_lan_web_v1', 'project_identity': tick.digest(str(self.root).encode()),
                'worker_alive': True, 'stopping': False}
        self.assertRegex(route, '^/api/jobs/web_')
        return self.local(route.rsplit('/', 1)[1])

    def observed(self, get=None, now=NOW):
        state, binding = tick.snapshot(self.root)
        return state, tick.observe(self.root, state, binding, get=get or self.get, now=now)

    def planned(self):
        state, observation = self.observed()
        return tick.plan(state, observation)

    def complete(self, job_id=DONE, sent=False):
        local = self.add_job(job_id, 'completed')
        local['artifacts'] = [{'kind': 'video', 'sha256': 'b' * 64}]
        self.save(self.root / 'web/state/jobs' / (job_id + '.json'), local)
        archive = self.ref('reports/proof/' + job_id + '/archive.json', {'schema': 'archive_fixture'})
        proof = {'schema': 'actual_complete_media_archive_verification_v1', 'job_id': job_id,
            'api_and_local_completed': True, 'full_decode_current_same_sha_passed': True,
            'bilingual_cues_complete': True, 'bytes': 48000000, 'video_sha256': 'b' * 64,
            'artifacts': [{'kind': kind} for kind in ['video', 'audio', 'subtitle', 'subtitle', 'report']],
            'archive_manifest': archive}
        self.queue['watched'][job_id]['completion_verification'] = self.ref('reports/proof/' + job_id + '/completion.json', proof)
        if sent:
            tool = self.ref('reports/proof/' + job_id + '/tool.json',
                {'isError': False, 'content': [{'type': 'text', 'text': 'Action completed.'}]})
            body = 'Fixture completed ' + job_id
            email = {'status': 'sent', 'attempts': 1, 'to': 'owner@example.invalid',
                'subject': job_id, 'body_sha256': tick.digest(body.encode()), 'final_video_sha256': 'b' * 64}
            intent = self.ref('reports/proof/' + job_id + '/intent.json',
                {'schema': 'tagesschau_completion_email_intent_v1', 'job_id': job_id, 'to': email['to'],
                    'subject': email['subject'], 'body': body, 'body_sha256': email['body_sha256'],
                    'final_video_sha256': 'b' * 64, 'save_to_sent_items': True})
            receipt = {'schema': 'tagesschau_completion_email_receipt_v1', 'status': 'sent', 'job_id': job_id,
                'to': email['to'], 'subject': email['subject'], 'body_sha256': email['body_sha256'],
                'final_video_sha256': 'b' * 64, 'send_attempts': 1,
                'confirmation': 'Outlook Email tool success', 'tool_result': tool, 'intent': intent}
            email['receipt'] = self.ref('reports/proof/' + job_id + '/email.json', receipt)
            self.queue['watched'][job_id]['email'] = email
        self.write_queue()

    def test_projection_preserves_all_control_data(self):
        self.add_job(OLD, 'failed', 'frozen:old')
        self.queue['watched'][OLD]['user_hold'] = {'status': 'deferred_by_user',
            'automatic_resumption_allowed': False, 'response_ref': {'path': 'keep original', 'sha256': 'x'}}
        self.queue['daily_intents']['2026-11-04'] = {'status': 'waiting_previous',
            'source_url': 'https://frozen.invalid/original.mp4', 'post_attempts': 0,
            'blocked_by_job_ids': [OLD], 'episode_key': 'frozen:nov4'}
        self.queue['episodes']['frozen:nov4'] = {'status': 'waiting_previous'}
        self.queue['notifications']['historical_without_job'] = {'status': 'asked_once'}
        self.write_queue()
        before = copy.deepcopy(self.queue)
        result = tick.apply_projection(self.root, self.planned(), now=NOW)
        self.assertEqual(result['status'], 'applied')
        after, _ = tick.snapshot(self.root)
        for field in before:
            if field not in {'generation', 'next_action'}:
                self.assertEqual(before[field], after[field], field)
        projection = after['projection']
        self.assertEqual(projection['jobs'][OLD]['next_action'], 'preserve_user_hold')
        self.assertEqual(projection['intents']['2026-11-04']['next_action'], 'preserve_waiting_previous')
        self.assertEqual(projection['notification_resolution']['historical_without_job']['effective_status'], 'unresolved_mapping')
        self.assertEqual(projection['conflicts'], [])
        self.assertNotIn('OLD stale text', after['next_action'])
        self.assertEqual(self.calls, ['/api/liveness'] + ['/api/jobs/' + job_id for job_id in sorted([OLD, JID])])

    def test_repeated_tick_is_semantically_idempotent(self):
        self.assertEqual(tick.apply_projection(self.root, self.planned(), now=NOW)['status'], 'applied')
        before = (self.root / self.config['state_path']).read_bytes()
        state, obs = self.observed(now=NOW + timedelta(seconds=20))
        result = tick.apply_projection(self.root, tick.plan(state, obs), now=NOW + timedelta(seconds=20))
        self.assertEqual(result['status'], 'unchanged')
        self.assertEqual(before, (self.root / self.config['state_path']).read_bytes())

    def test_generation_cas_rejects_competing_tick(self):
        plan = self.planned()
        self.queue['generation'] += 1
        self.queue['new_user_hold'] = {'automatic_resumption_allowed': False}
        self.write_queue()
        self.assertEqual(tick.apply_projection(self.root, plan, now=NOW)['status'], 'conflict')
        self.assertNotIn('projection', tick.snapshot(self.root)[0])

    def test_sha_cas_rejects_change_without_generation_increment(self):
        plan = self.planned()
        self.queue['watched'][JID]['user_hold'] = {'automatic_resumption_allowed': False}
        self.write_queue()
        self.assertEqual(tick.apply_projection(self.root, plan, now=NOW)['status'], 'conflict')

    def test_busy_queue_lock(self):
        plan = self.planned()
        with InstanceLock((self.root / self.config['state_path']).with_suffix('.lock')):
            self.assertEqual(tick.apply_projection(self.root, plan, now=NOW)['status'], 'busy')

    def test_job_change_after_observation_rejected(self):
        plan = self.planned()
        local = self.local(JID)
        local['status'] = 'failed'
        self.save(self.root / 'web/state/jobs' / (JID + '.json'), local)
        with self.assertRaises(tick.StateError):
            tick.apply_projection(self.root, plan, now=NOW)

    def test_observation_is_outside_queue_lock(self):
        def unlocked_get(route):
            with InstanceLock((self.root / self.config['state_path']).with_suffix('.lock')):
                pass
            return self.get(route)
        state, obs = self.observed(get=unlocked_get)
        self.assertTrue(obs['jobs'][JID]['valid'])
        self.assertEqual(tick.plan(state, obs)['proposed_external_actions'], [])

    def test_missing_completion_proof_never_sends(self):
        local = self.local(JID)
        local['status'] = 'completed'
        self.save(self.root / 'web/state/jobs' / (JID + '.json'), local)
        projection = self.planned()['allowed_patch']['projection']
        self.assertEqual(projection['jobs'][JID]['next_action'], 'verify_completion_evidence')

    def test_bad_completion_digest_or_wrong_job_never_sends(self):
        self.complete()
        for mutation in ['digest', 'job', 'flag', 'video', 'size']:
            with self.subTest(mutation=mutation):
                watch = self.queue['watched'][DONE]
                original = copy.deepcopy(watch['completion_verification'])
                doc, _ = tick.read_file(self.root, original['path'])
                backup = copy.deepcopy(doc)
                if mutation == 'digest':
                    watch['completion_verification']['sha256'] = 'e' * 64
                else:
                    if mutation == 'job': doc['job_id'] = JID
                    if mutation == 'flag': doc['full_decode_current_same_sha_passed'] = False
                    if mutation == 'video': doc['video_sha256'] = 'e' * 64
                    if mutation == 'size': doc['bytes'] = 50000000
                    watch['completion_verification'] = self.ref(original['path'], doc)
                self.write_queue()
                projection = self.planned()['allowed_patch']['projection']
                self.assertEqual(projection['jobs'][DONE]['next_action'], 'verify_completion_evidence')
                self.save(self.root / original['path'], backup)
                watch['completion_verification'] = original
        self.write_queue()

    def test_completed_sent_has_no_duplicate_candidate(self):
        self.complete(sent=True)
        projection = self.planned()['allowed_patch']['projection']
        self.assertEqual(projection['jobs'][DONE]['next_action'], 'completed_notified')
        self.assertFalse(any(a['kind'] == 'connector_send_candidate' and a.get('job_id') == DONE
            for a in projection['next_actions']))

    def test_legacy_sent_requires_actual_success_and_exact_intent_body(self):
        self.complete(sent=True)
        ref = self.queue['watched'][DONE]['email']['receipt']
        receipt, _ = tick.read_file(self.root, ref['path'])
        tool_ref = receipt['tool_result']
        self.save(self.root / tool_ref['path'], {'isError': True,
            'content': [{'type': 'text', 'text': 'Action completed.'}]})
        self.assertEqual(self.planned()['allowed_patch']['projection']['jobs'][DONE]['next_action'],
            'connector_verify_sent_items')
        self.complete(sent=True)
        ref = self.queue['watched'][DONE]['email']['receipt']
        receipt, _ = tick.read_file(self.root, ref['path'])
        intent_ref = receipt['intent']
        intent, _ = tick.read_file(self.root, intent_ref['path'])
        intent['body'] = 'changed body'
        receipt['intent'] = self.ref(intent_ref['path'], intent)
        self.queue['watched'][DONE]['email']['receipt'] = self.ref(ref['path'], receipt)
        self.write_queue()
        self.assertEqual(self.planned()['allowed_patch']['projection']['jobs'][DONE]['next_action'],
            'connector_verify_sent_items')

    def test_sending_unknown_or_attempted_pending_only_checks_sent_items(self):
        self.complete()
        for status, attempts in [('sending', 1), ('email_unknown', 1), ('unknown', 0), ('pending', 1), ('sent', 1)]:
            with self.subTest(status=status):
                self.queue['watched'][DONE]['email'].update(status=status, attempts=attempts)
                self.write_queue()
                self.assertEqual(self.planned()['allowed_patch']['projection']['jobs'][DONE]['next_action'],
                    'connector_verify_sent_items')

    def test_completed_pending_is_only_an_inert_connector_candidate(self):
        self.complete()
        plan = self.planned()
        self.assertEqual(plan['allowed_patch']['projection']['jobs'][DONE]['next_action'], 'connector_send_candidate')
        self.assertEqual(plan['proposed_external_actions'], [])
        self.assertTrue(all(a['execution_allowed'] is False for a in plan['allowed_patch']['projection']['next_actions']))

    def test_resolved_notifications_use_real_bound_decision(self):
        self.complete(sent=True)
        ref = self.ref('reports/reply.json', {'answer_verbatim': 'keep exact voices'})
        decision = {'status': 'adopted_exact_current_voices_completed', 'user_answer': 'keep exact voices',
            'authorization_consumed': True, 'response_ref': ref, 'scope': ['line0130'], 'question_asked_once': True}
        self.queue['watched'][DONE]['pending_voice_decision'] = decision
        self.queue['notifications']['old_asked_once'] = {'job_id': DONE, 'status': 'asked_once', 'scope': ['line0130']}
        self.write_queue()
        projection = self.planned()['allowed_patch']['projection']
        self.assertEqual(projection['active_decisions'], [])
        self.assertEqual(projection['notification_resolution']['old_asked_once']['effective_status'], 'resolved')
        self.assertEqual(self.queue['notifications']['old_asked_once']['status'], 'asked_once')
        (self.root / ref['path']).write_text('{}')
        broken = self.planned()['allowed_patch']['projection']
        self.assertEqual(broken['notification_resolution']['old_asked_once']['effective_status'], 'evidence_invalid')
        self.assertEqual(broken['jobs'][DONE]['next_action'], 'inspect_decision_evidence')

    def test_pending_question_not_reasked_or_executed(self):
        self.queue['watched'][JID]['pending_editorial_decision'] = {'status': 'asked_once',
            'scope': ['line0001'], 'question_asked_once': True, 'new_generation_authorized': False}
        self.write_queue()
        projection = self.planned()['allowed_patch']['projection']
        self.assertEqual(projection['active_decisions'][0]['effective_status'], 'awaiting_actual_reply')
        self.assertTrue(projection['active_decisions'][0]['do_not_reask'])

    def test_forged_offline_valid_flags_and_empty_files_rejected(self):
        state, observation = self.observed()
        forged = copy.deepcopy(observation)
        forged['files'] = {}
        row = forged['jobs'][JID]
        row['local']['status'] = row['api']['status'] = 'completed'
        row['completion'] = {'valid': True, 'video_sha256': 'b' * 64}
        row['email'] = {'valid': True}
        with self.assertRaises(tick.StateError):
            tick.apply_projection(self.root, tick.plan(state, forged), now=NOW)
        self.assertNotIn('projection', tick.snapshot(self.root)[0])

    def test_forged_plan_cannot_write_hold_or_fake_summary(self):
        for key, value in [('next_action', 'pretend sent'), ('user_hold', {})]:
            plan = self.planned()
            plan['allowed_patch'][key] = value
            with self.assertRaises(tick.StateError):
                tick.apply_projection(self.root, plan, now=NOW)

    def test_changed_config_stale_future_observation_rejected(self):
        plan = self.planned()
        for now in [NOW + timedelta(seconds=301), NOW - timedelta(seconds=31)]:
            with self.subTest(now=now), self.assertRaises(tick.StateError):
                tick.apply_projection(self.root, plan, now=now)
        self.config['notification']['to'] = 'other@example.invalid'
        self.save(self.root / tick.CONFIG, self.config)
        with self.assertRaises(tick.StateError):
            tick.apply_projection(self.root, plan, now=NOW)

    def test_api_mismatch_or_unavailable_never_retries(self):
        def mismatch(route):
            value = self.get(route)
            if route != '/api/liveness': value['updated_at'] = 'different'
            return value
        state, observation = self.observed(get=mismatch)
        self.assertFalse(observation['jobs'][JID]['valid'])
        self.assertEqual(tick.plan(state, observation)['allowed_patch']['projection']['jobs'][JID]['next_action'],
            'inspect_observation_conflict')

    def test_wrong_liveness_identity_does_not_query_jobs(self):
        routes = []
        def wrong(route):
            routes.append(route)
            return {'version': 'philos_lan_web_v1', 'project_identity': 'wrong', 'worker_alive': True, 'stopping': False}
        _, observation = self.observed(get=wrong)
        self.assertEqual(routes, ['/api/liveness'])
        self.assertFalse(observation['liveness']['valid'])

    def test_dead_worker_or_stopping_is_not_claimed_healthy(self):
        for dead, stopping in [(False, False), (True, True)]:
            def unready(route):
                response = self.get(route)
                if route == '/api/liveness': response.update(worker_alive=dead, stopping=stopping)
                return response
            state, observation = self.observed(get=unready)
            projection = tick.plan(state, observation)['allowed_patch']['projection']
            self.assertEqual(projection['jobs'][JID]['next_action'], 'inspect_observation_conflict')
            self.assertIn('service_worker_not_operational', projection['conflicts'])

    def test_submission_unknown_and_canceled_are_preserved(self):
        local = self.local(JID)
        local.update(status='canceled', cancel_requested_at='keep exact')
        self.save(self.root / 'web/state/jobs' / (JID + '.json'), local)
        self.queue['daily_intents']['2026-11-06'] = {'status': 'submission_unknown', 'post_attempts': 1,
            'source_url': 'https://frozen.invalid/file.mp4'}
        self.write_queue()
        plan = self.planned()
        self.assertEqual(plan['allowed_patch']['projection']['jobs'][JID]['next_action'], 'preserve_canceled_job')
        self.assertEqual(plan['allowed_patch']['projection']['intents']['2026-11-06']['next_action'],
            'reconcile_submission_without_repost')
        tick.apply_projection(self.root, plan, now=NOW)
        self.assertEqual(self.local(JID)['cancel_requested_at'], 'keep exact')

    def test_cli_offline_reconcile_and_forged_offline_plan_rejected(self):
        _, observation = self.observed()
        path = self.root / 'reports/offline.json'
        self.save(path, observation)
        before = (self.root / self.config['state_path']).read_bytes()
        with redirect_stdout(io.StringIO()) as output:
            code = cli.main(['--root', str(self.root), '--mode', 'reconcile', '--observation', str(path)])
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(output.getvalue())['changed'])
        observation['files'] = {}
        self.save(path, observation)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['--root', str(self.root), '--observation', str(path)]), 2)
        self.assertEqual(before, (self.root / self.config['state_path']).read_bytes())

    def test_cli_evidence_directory_preflight_before_commit(self):
        directory = self.root / 'reports/already_exists'
        directory.mkdir(parents=True)
        before = (self.root / self.config['state_path']).read_bytes()
        with patch.object(tick, 'LocalGet', return_value=self.get), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['--root', str(self.root), '--mode', 'reconcile',
                '--evidence-dir', str(directory)]), 2)
        self.assertEqual(before, (self.root / self.config['state_path']).read_bytes())

    def test_cli_post_commit_receipt_error_reports_real_application(self):
        directory = self.root / 'reports/new_receipts'
        real_write = cli.write_json
        def fail_result(path, doc):
            if Path(path).name == 'result.json': raise OSError('simulated receipt failure')
            real_write(path, doc)
        with patch.object(tick, 'LocalGet', return_value=self.get), patch.object(tick, 'utc_now', return_value=NOW), \
                patch.object(cli, 'write_json', side_effect=fail_result), redirect_stdout(io.StringIO()) as output:
            code = cli.main(['--root', str(self.root), '--mode', 'reconcile', '--evidence-dir', str(directory)])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 3)
        self.assertEqual(result['status'], 'applied')
        self.assertTrue(result['changed'])
        self.assertIn('projection', tick.snapshot(self.root)[0])

    def test_symlink_and_outside_config_state_rejected(self):
        (self.root / 'reports').mkdir(exist_ok=True)
        (self.root / 'reports/link.json').symlink_to(self.root / self.config['state_path'])
        with self.assertRaises(tick.StateError): tick.read_file(self.root, 'reports/link.json')
        self.config['state_path'] = '../outside.json'
        self.save(self.root / tick.CONFIG, self.config)
        with self.assertRaises(tick.StateError): tick.snapshot(self.root)

    def test_relative_config_root_is_independent_of_process_cwd(self):
        self.config['root'] = '.'
        self.save(self.root / tick.CONFIG, self.config)
        previous = Path.cwd()
        try:
            os.chdir(self.root.parent)
            self.assertEqual(tick.load_config(self.root)[0]['root'], '.')
            with patch.object(tick, 'LocalGet', return_value=self.get), redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['--root', str(self.root)]), 0)
        finally:
            os.chdir(previous)
        self.config['root'] = ''
        self.save(self.root / tick.CONFIG, self.config)
        with self.assertRaises(tick.StateError): tick.load_config(self.root)

    def test_intent_hold_also_preserves_failed_watch_quietly(self):
        self.add_job(OLD, 'failed', 'frozen:old')
        self.queue['daily_intents']['2026-11-03'] = {'job_id': OLD, 'episode_key': 'frozen:old',
            'status': 'job_bound', 'user_hold': {'automatic_resumption_allowed': False}}
        self.write_queue()
        projection = self.planned()['allowed_patch']['projection']
        self.assertEqual(projection['jobs'][OLD]['next_action'], 'preserve_user_hold')
        self.assertFalse(next(a for a in projection['next_actions'] if a.get('job_id') == OLD)['notification_needed'])

    def legacy_adoption(self):
        self.complete(sent=True)
        scope = self.ref('reports/legacy_scope.json', {'job': DONE, 'typed_current_failure_metadata':
            {'job_id': DONE, 'source_sha256': 'a' * 64, 'segment_ids': ['line0001', 'line0002']}})
        delivery = self.ref('reports/legacy_delivery.json', {'candidate': True})
        reply = self.ref('reports/legacy_reply.json', {'schema': 'current_job_user_voice_adoption_v1',
            'job': DONE, 'source_video_sha256': 'a' * 64, 'decision_origin': 'actual_human_user_reply',
            'question': 'adopt?', 'answer_verbatim': 'yes', 'new_regeneration_authorized': False,
            'seed43_authorized': False, 'original_scope': scope, 'corrected_delivery': delivery,
            'rows': [{'id': 'line0001'}, {'id': 'line0002'}]})
        watch = self.queue['watched'][DONE]
        watch['pending_voice_decision'] = {'status': 'superseded_by_final_adoption_reply',
            'response_ref': 'legacy unbound old answer', 'scope_ref': scope['path'], 'scope_sha256': scope['sha256']}
        watch['pending_final_voice_choice'] = {'status': 'answered_adopted',
            'decision_ref': reply['path'], 'decision_sha256': reply['sha256'],
            'original_scope_ref': scope['path'], 'original_scope_sha256': scope['sha256'],
            'corrected_delivery_ref': delivery['path'], 'human_listened_scope': ['line0001', 'line0002'],
            'corrected_candidate_ids': ['line0001'], 'other_original_ids': ['line0002'],
            'adoption_is_not_new_regeneration_authorization': True, 'line0195_seed43_authorized': False,
            'question': 'adopt?', 'answer_verbatim': 'yes'}
        self.write_queue()

    def test_bound_legacy_final_adoption_resolves_old_unhashed_response(self):
        self.legacy_adoption()
        projection = self.planned()['allowed_patch']['projection']
        self.assertEqual(projection['jobs'][DONE]['next_action'], 'completed_notified')
        self.assertEqual(projection['active_decisions'], [])
        self.assertEqual(projection['jobs'][DONE]['decisions']['pending_voice_decision']['effective_status'], 'resolved')

    def test_legacy_supersession_does_not_hide_wrong_scope_job_or_unsigned_reply(self):
        self.legacy_adoption()
        for mutation in ['wrong_scope', 'no_reply_sha', 'wrong_job', 'source_changed']:
            with self.subTest(mutation=mutation):
                backup = copy.deepcopy(self.queue['watched'][DONE])
                final = self.queue['watched'][DONE]['pending_final_voice_choice']
                if mutation == 'wrong_scope': final['original_scope_sha256'] = 'e' * 64
                elif mutation == 'no_reply_sha': final.pop('decision_sha256')
                else:
                    doc, _ = tick.read_file(self.root, final['decision_ref'])
                    if mutation == 'wrong_job': doc['job'] = JID
                    else: doc['source_video_sha256'] = 'e' * 64
                    new_ref = self.ref('reports/wrong_reply.json', doc)
                    final['decision_ref'], final['decision_sha256'] = new_ref['path'], new_ref['sha256']
                self.write_queue()
                projection = self.planned()['allowed_patch']['projection']
                self.assertEqual(projection['jobs'][DONE]['next_action'], 'inspect_decision_evidence')
                self.queue['watched'][DONE] = backup


class HttpBoundary(unittest.TestCase):
    def test_rejects_nonloopback_or_unsafe_bases_and_routes(self):
        for url in ['http://example.com:8766', 'http://192.168.1.2:8766', 'http://127.0.0.1',
                'http://user@127.0.0.1:8766', 'http://127.0.0.1:8766/redirect',
                'http://127.0.0.1:8766?url=x', 'file:///etc/passwd', 'http://127.0.0.1:8766/#x']:
            with self.subTest(url=url), self.assertRaises(tick.StateError): tick.LocalGet(url)
        get = tick.LocalGet('http://localhost:8766')
        self.assertEqual(get.base, 'http://127.0.0.1:8766')
        for route in ['/api/health', '/api/jobs', '/api/jobs/' + JID + '/retry', 'https://example.com']:
            with self.assertRaises(tick.StateError): get(route)

    def test_urllib_redirect_response_is_never_followed(self):
        received = []
        class FakeHTTP(urllib.request.HTTPHandler):
            def http_open(self, request):
                received.append((request.method, request.full_url))
                headers = email.message.Message()
                headers['Location'] = 'http://example.invalid:9999/steal'
                response = urllib.response.addinfourl(io.BytesIO(b''), headers, request.full_url, 302)
                response.msg = 'Found'
                return response
        get = tick.LocalGet('http://127.0.0.1:8766')
        get.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), FakeHTTP(), tick._NoRedirect())
        with self.assertRaises(tick.StateError): get('/api/liveness')
        self.assertEqual(received, [('GET', 'http://127.0.0.1:8766/api/liveness')])

    def test_duplicate_and_nonfinite_json_rejected(self):
        for raw in [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}']:
            with self.assertRaises(tick.StateError): tick.parse_json(raw)


if __name__ == '__main__':
    unittest.main()
