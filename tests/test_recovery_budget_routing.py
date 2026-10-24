"""Local HTTP/UI budget routing; no production data, Proxy or model work."""
import copy
import hashlib
import http.client
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'web'))
spec = importlib.util.spec_from_file_location('budget_routing_server', ROOT / 'web/server.py')
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)
import media_recovery
import failure_triage
import website_recovery

POLICY = {'enabled': True, 'source_mode': 'contextual', 'picture_holds': True, 'reserve_gib': 8}
JID = 'web_20261023_000001_deadbeef'
EXHAUSTED = {'used': 6, 'max': 6, 'remaining': 0, 'exhausted': True}


class RecoveryBudgetRoutingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.http = server.make_server(self.root, host='127.0.0.1', port=0, start_worker=False,
                                       static_root=ROOT / 'web/static', lan_host='localhost')
        self.thread = threading.Thread(target=self.http.serve_forever,
                                       kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start(); self.addCleanup(self.close_server)
        self.manager = self.http.manager
        self.port = self.http.server_address[1]
        self.origin = 'http://127.0.0.1:'+str(self.port)
        # The worker is disabled; no processing or remote check may be started.
        self.command = patch.object(server.JobManager, 'command', side_effect=AssertionError('No production subprocess'))
        self.command.start(); self.addCleanup(self.command.stop)
        proxy = patch.object(server.JobManager, 'check_proxy', side_effect=AssertionError('No Proxy'))
        proxy.start(); self.addCleanup(proxy.stop)
        module = SimpleNamespace(resume_pending=lambda root, jid: None)
        review = patch.dict(sys.modules, {'website_review': module})
        review.start(); self.addCleanup(review.stop)
        source = self.root/'inputs/web'/JID/'source.mp4'
        source.parent.mkdir(parents=True); source.write_bytes(b'fixture original source')
        self.folder = self.root/'jobs'/JID; self.folder.mkdir(parents=True)
        self.manager.add(input_path=source, job_id=JID, website_recovery=POLICY)
        self.job = self.manager.jobs[JID]
        self.reset_job()
        self.journal_path = self.folder/'recovery/website_journal.json'
        self.write_journal(6)
        self.journal_before = self.journal_path.read_bytes()
        self.source = source

    def close_server(self):
        self.http.shutdown(); self.http.server_close(); self.thread.join(timeout=2)

    def reset_job(self, policy=None):
        self.job.update(status='failed', stage='failed', error='待恢复')
        self.job['options']['website_recovery'] = copy.deepcopy(POLICY if policy is None else policy)
        self.job.pop('website_recovery_request', None)
        self.job.pop('website_recovery_policy_overrides', None)
        self.manager.pending.clear(); self.manager.save(self.job)

    def write_journal(self, count):
        server.atomic_json(self.journal_path, {'schema': website_recovery.SCHEMA, 'job': JID,
            'max_actions': 6, 'max_per_input': 2, 'events': [
                {'id': f'{i+1:064x}', 'input_fingerprint': f'{i+20:064x}', 'action': 'translation',
                 'status': 'failed', 'proxy_calls': 2, 'segment_ids': ['line0001']}
                for i in range(count)]})

    def request(self, policy):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        try:
            connection.request('POST', '/api/jobs/'+JID+'/recover',
                body=json.dumps({'policy': policy}).encode(),
                headers={'Origin': self.origin, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def failure(self, category, blocked=False):
        failure = {'category': category, 'can_retry': not blocked,
                   'segment_ids': ['line0001'] if blocked else []}
        return patch.object(server, 'public_failure', return_value=failure), patch.object(
            server, 'retry_message', return_value='当前没有新的修订' if blocked else None)

    def test_exhausted_text_budget_allows_only_known_local_resume_categories(self):
        categories = ('storage', 'tts', 'alignment', 'timing', 'picture',
                      'source_review', 'translation_review', 'audio', 'unknown', 'transport', None)
        for category in categories:
            with self.subTest(category=category):
                self.reset_job(); before = copy.deepcopy(self.job)
                diagnosis, guard = self.failure(category)
                with diagnosis, guard:
                    status, result = self.request(POLICY)
                allowed = category in {'storage', 'tts', 'audio', 'alignment', 'timing', 'picture'}
                self.assertEqual(status, 200 if allowed else 400, result)
                if allowed:
                    self.assertEqual(result['status'], 'queued')
                    self.assertEqual(list(self.manager.pending), [JID])
                    self.assertEqual(self.job['options']['website_recovery']['reserve_gib'], 8)
                else:
                    self.assertEqual(self.job, before)
                    self.assertEqual(list(self.manager.pending), [])
                self.assertEqual(self.journal_path.read_bytes(), self.journal_before)
                self.assertEqual(self.source.read_bytes(), b'fixture original source')

    def test_storage_lower_reserve_is_still_explicit_and_storage_only(self):
        low = {**POLICY, 'reserve_gib': 2}
        for category in ('storage', 'alignment', 'picture', 'timing', 'tts', 'audio', 'unknown'):
            with self.subTest(category=category):
                self.reset_job(); diagnosis, guard = self.failure(category)
                with diagnosis, guard:
                    status, result = self.request(low)
                self.assertEqual(status, 200 if category == 'storage' else 400, result)
                self.assertEqual(self.job['options']['website_recovery']['reserve_gib'], 2 if category == 'storage' else 8)
                self.assertEqual(self.journal_path.read_bytes(), self.journal_before)

    def test_media_fingerprint_guard_and_single_override_are_unchanged(self):
        changed = {**POLICY, 'picture_holds': False}
        for category in ('alignment', 'picture', 'timing', 'tts', 'audio'):
            with self.subTest(category=category):
                self.reset_job(); diagnosis, guard = self.failure(category, blocked=True)
                with diagnosis, guard:
                    self.assertEqual(self.request(POLICY)[0], 400)
                    self.assertEqual(self.request(changed)[0], 200)
                    self.assertEqual(len(self.job['website_recovery_policy_overrides']), 1)
                    self.job.update(status='failed', stage='failed'); self.manager.pending.clear()
                    self.job['options']['website_recovery'] = copy.deepcopy(POLICY)
                    self.assertEqual(self.request(changed)[0], 400)
                self.assertEqual(self.journal_path.read_bytes(), self.journal_before)

    def test_content_or_unknown_cannot_gain_rights_from_media_option_changes(self):
        for category in ('source_review', 'translation_review', 'unknown', None):
            with self.subTest(category=category):
                self.reset_job(); before = copy.deepcopy(self.job)
                diagnosis, guard = self.failure(category, blocked=True)
                with diagnosis, guard:
                    self.assertEqual(self.request({**POLICY, 'picture_holds': False})[0], 400)
                self.assertEqual(self.job, before)
                self.assertEqual(self.journal_path.read_bytes(), self.journal_before)

    def test_invalid_budget_still_rejects_storage_and_media_without_mutation(self):
        invalid = ({}, {'available': False, 'budget': EXHAUSTED},
            {'budget': {'used': 6, 'max': 6, 'remaining': True, 'exhausted': True}},
            {'budget': {'used': 6, 'max': 6, 'remaining': 0, 'exhausted': False}},
            {'budget': {'used': 5, 'max': 6, 'remaining': 0, 'exhausted': True}})
        for category in ('storage', 'alignment', 'picture', 'audio'):
            for value in invalid:
                with self.subTest(category=category, budget=value):
                    self.reset_job(); before = copy.deepcopy(self.job)
                    diagnosis, guard = self.failure(category)
                    with diagnosis, guard, patch.object(server, 'public_recovery', return_value=value):
                        self.assertEqual(self.request(POLICY)[0], 400)
                    self.assertEqual(self.job, before)
                    self.assertEqual(list(self.manager.pending), [])

    def test_storage_cannot_override_an_unchanged_content_fingerprint(self):
        diagnosis, guard = self.failure('storage', blocked=True)
        with diagnosis, guard:
            self.assertEqual(self.request({**POLICY, 'reserve_gib': 2})[0], 400)
        self.assertEqual(self.journal_path.read_bytes(), self.journal_before)

    def test_repeated_request_does_not_duplicate_queue_or_clear_active_cancel(self):
        diagnosis, guard = self.failure('storage')
        with diagnosis, guard:
            self.assertEqual(self.request(POLICY)[0], 200)
            self.assertEqual(self.request(POLICY)[0], 200)
            self.assertEqual(list(self.manager.pending), [JID])
            self.assertEqual(len(self.job['attempts']), 1)
            self.job.update(status='running', stage='处理中')
            self.manager.pending.clear(); self.manager.cancel_events[JID].set()
            self.assertEqual(self.request(POLICY)[0], 200)
        self.assertTrue(self.manager.cancel_events[JID].is_set())
        self.assertEqual(list(self.manager.pending), [])
        self.assertEqual(self.journal_path.read_bytes(), self.journal_before)

    def test_media_lifetime_budget_is_not_reset_by_local_resume(self):
        for i in range(6):
            media_recovery.reserve_recovery(self.folder, 'alignment_split', {'fixture': i})
        path = self.folder/'tts/media_recovery_journal.json'; before = path.read_bytes()
        diagnosis, guard = self.failure('alignment')
        with diagnosis, guard:
            self.assertEqual(self.request(POLICY)[0], 200)
        self.assertEqual(path.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, 'budget exhausted'):
            media_recovery.reserve_recovery(self.folder, 'alignment_split', {'fixture': 7})
        self.assertEqual(path.read_bytes(), before)

    def compact_fixture(self):
        row = {'segment_id': 'line0001', 'speaker_id': 'S00', 'start_s': 0., 'end_s': 2.,
               'text_de': 'Es sind 3 Häuser.', 'text_source': 'Es sind 3 Häuser.', 'source_language': 'de'}
        server.atomic_json(self.folder/'translation/input.json', {'segments': [row]})
        server.atomic_json(self.folder/'segments.json', {'segments': [{**row, 'text_zh': '这里有3座房屋。',
            'translation_reviewed': True, 'human_reviewed': False}]})

    def test_later_compact_still_denies_six_actions_before_any_proxy_call(self):
        diagnosis, guard = self.failure('timing')
        with diagnosis, guard:
            self.assertEqual(self.request(POLICY)[0], 200)
        self.compact_fixture()
        with self.assertRaises(website_recovery.RecoveryBudgetExhausted):
            website_recovery.run_compact(self.root, JID, ['line0001'], 'timing', policy=POLICY,
                client_factory=lambda root: self.fail('Proxy must not be created after exhausted budget'))
        self.assertEqual(self.journal_path.read_bytes(), self.journal_before)

    def test_later_compact_keeps_same_input_two_attempt_limit(self):
        self.write_journal(0); self.compact_fixture(); calls = []

        class RejectingProxy:
            def json(self, system, payload):
                calls.append(True)
                row = {'id': 'line0001', 'text_zh': '有3座房屋。'}
                if 'draft' in payload: row.update(approved=False, issues=['fixture rejected'])
                return {'translations': [row]}

        for _ in range(2):
            with self.assertRaises(website_recovery.RecoveryReviewRequired):
                website_recovery.run_compact(self.root, JID, ['line0001'], 'timing', policy=POLICY,
                                            client_factory=lambda root: RejectingProxy())
        with self.assertRaises(website_recovery.RecoveryBudgetExhausted):
            website_recovery.run_compact(self.root, JID, ['line0001'], 'timing', policy=POLICY,
                                        client_factory=lambda root: self.fail('No third attempt'))
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(json.loads(self.journal_path.read_text())['events']), 2)

    def test_actual_bound_audio_failure_tracks_fallback_helper_and_can_resume(self):
        self.compact_fixture()
        server.atomic_json(self.folder/'tts/audio_quality_fixture.json', {
            'complete': True, 'binding': {'source_segments_sha256': failure_triage._sha(self.folder/'segments.json')},
            'needs_repair_ids': ['line0001']})
        record = failure_triage.make_failure_record(self.root, self.job,
            RuntimeError('局部声学检查未通过'), stage='检查配音声学完整性')
        self.assertEqual(record['category'], 'audio')
        self.assertEqual(record['segment_ids'], ['line0001'])
        self.assertEqual(record['retry_policy'], 'after_change')
        self.job['failure_record'] = record; self.manager.save(self.job)
        self.assertFalse(failure_triage.public_failure(self.root, self.job)['can_retry'])
        self.assertEqual(self.request(POLICY)[0], 400)
        helper = self.root/'web/acoustic_fallback.py'
        helper.write_text('# fixture local same-text fallback installed\n')
        self.assertTrue(failure_triage.public_failure(self.root, self.job)['can_retry'])
        status, result = self.request(POLICY)
        self.assertEqual(status, 200, result)
        self.assertEqual(result['status'], 'queued')
        self.assertEqual(self.journal_path.read_bytes(), self.journal_before)

    def test_audio_helper_change_does_not_unlock_actual_translation_or_unknown_failure(self):
        self.compact_fixture()
        document = json.loads((self.folder/'translation/input.json').read_text())
        rows = document['segments']
        server.atomic_json(self.folder/'translation/checkpoint.json', {
            'schema': 'translation_checkpoint_v1', 'input_rows_sha256': hashlib.sha256(json.dumps(
                rows, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest(),
            'total': 1, 'completed': 0, 'batches': {'line0001': {
                'status': 'failed', 'ids': ['line0001'], 'error_type': 'InvariantRejectedError',
                'checks': {'line0001': ['数字未保留']}}}})
        record = failure_triage.make_failure_record(self.root, self.job,
            RuntimeError('数字未保留'), stage=failure_triage.TRANSLATION_STAGE, checkpoint_before=None)
        self.assertEqual(record['category'], 'translation_review')
        self.assertEqual(record['retry_policy'], 'after_change')
        self.job['failure_record'] = record; self.manager.save(self.job)
        before = failure_triage._revision_fingerprint(self.root, self.job, failure_category='unknown')
        helper = self.root/'web/acoustic_fallback.py'
        helper.write_text('# fixture fallback version one\n')
        self.assertFalse(failure_triage.public_failure(self.root, self.job)['can_retry'])
        self.assertEqual(before, failure_triage._revision_fingerprint(
            self.root, self.job, failure_category='unknown'))
        self.assertEqual(self.request({**POLICY, 'picture_holds': False})[0], 400)
        helper.write_text('# fixture fallback version two with a changed SHA\n')
        self.assertFalse(failure_triage.public_failure(self.root, self.job)['can_retry'])
        self.assertEqual(self.journal_path.read_bytes(), self.journal_before)

    def test_ui_uses_same_category_budget_and_fingerprint_guards_as_api(self):
        node = shutil.which('node')
        if node is None: self.skipTest('Node runtime unavailable for actual UI function evaluation')
        source = (ROOT/'web/static/app.js').read_text()

        def function(name):
            match = re.search(r'  function '+name+r'\([^\n]*\) \{.*?\n  \}', source, re.S)
            self.assertIsNotNone(match, name)
            return match.group(0)

        cases = []
        for category in ('storage', 'tts', 'alignment', 'timing', 'picture', 'audio',
                         'source_review', 'translation_review', 'unknown', 'transport', None):
            for blocked in (False, True):
                for changed in (False, True):
                    cases.append({'category': category, 'blocked': blocked, 'changed': changed})
        code = '\n'.join([function('recoveryPolicy'), function('recoveryNeedsTextBudget'), function('updateRecoveryAction')])
        script = '''
const cases=JSON.parse(process.argv[1]); let state, elements, draft;
const $=(id)=>elements[id]||(elements[id]={});
const recoveryDraft=()=>draft;
'''+code+'''
const results=cases.map(c=>{
  const policy={enabled:true,source_mode:'contextual',picture_holds:true,reserve_gib:8};
  draft={...policy,picture_holds:c.changed?false:true}; elements={};
  state={selectedJob:{failure:{category:c.category,can_retry:!c.blocked},
    recovery:{policy,available:true,budget:{used:6,max:6,remaining:0,exhausted:true}}},
    recoverPending:false,reviewSaving:false};
  updateRecoveryAction(); return elements['recover-button'].disabled;
}); process.stdout.write(JSON.stringify(results));
'''
        result = subprocess.run([node, '-e', script, json.dumps(cases)], capture_output=True, text=True, check=True)
        disabled = json.loads(result.stdout)
        for case, ui_disabled in zip(cases, disabled):
            category = case['category']
            text_blocked = server.recovery_needs_text_budget(category)
            permitted_change = case['changed'] and category in {'tts', 'audio', 'alignment', 'timing', 'picture'}
            expected = text_blocked or (case['blocked'] and not permitted_change)
            self.assertEqual(ui_disabled, expected, case)


if __name__ == '__main__':
    unittest.main()
