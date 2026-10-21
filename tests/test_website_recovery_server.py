"""Website recovery HTTP/queue contracts, with no Proxy, subprocess or model work."""
import copy
import http.client
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'web'))
spec = importlib.util.spec_from_file_location('website_recovery_server_under_test', ROOT / 'web/server.py')
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)
POLICY = {'enabled': True, 'source_mode': 'contextual', 'picture_holds': True, 'reserve_gib': 8}
JID = 'web_20261022_000001_deadbeef'


class WebsiteRecoveryServerTests(unittest.TestCase):
    def test_upload_retains_eight_gib_without_reserving_model_work_twice(self):
        query=urlencode({'name':'synthetic.mp4','website_recovery':json.dumps(POLICY)})
        with patch.object(server.shutil,'disk_usage',return_value=SimpleNamespace(free=server.RESERVE+64*1024**2)):
            code,result=self.request('POST','/api/jobs/upload?'+query,raw=b'video')
        self.assertEqual(code,201)
        self.assertEqual(self.manager.jobs[result['id']]['options']['website_recovery']['reserve_gib'],8)

    def test_upload_still_rejects_before_crossing_eight_gib_floor(self):
        with patch.object(server.shutil,'disk_usage',return_value=SimpleNamespace(free=server.RESERVE+server.UPLOAD_MARGIN-1)):
            code,_=self.request('POST','/api/jobs/upload?name=synthetic.mp4',raw=b'video')
        self.assertEqual(code,507)
        self.assertEqual(self.manager.jobs,{})

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.http = server.make_server(self.root, host='127.0.0.1', port=0, start_worker=False,
                                       static_root=ROOT / 'web/static', lan_host='localhost')
        self.thread = threading.Thread(target=self.http.serve_forever,
                                       kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.manager = self.http.manager
        self.port = self.http.server_address[1]
        self.origin = 'http://127.0.0.1:' + str(self.port)
        self.addCleanup(patch.stopall)
        patch.object(server.subprocess, 'Popen', side_effect=AssertionError('No processes in HTTP tests')).start()
        patch.object(server.JobManager, 'check_proxy', side_effect=AssertionError('No Proxy in HTTP tests')).start()
        patch.object(server.shutil, 'disk_usage', return_value=SimpleNamespace(free=100 * 1024**3)).start()

    def close_server(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, payload=None, *, headers=None, raw=None, decode=True):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        self.addCleanup(connection.close)
        data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else b'')
        supplied = {'Origin': self.origin, 'Content-Type': 'application/json', **(headers or {})}
        connection.request(method, path, body=data, headers=supplied)
        response = connection.getresponse()
        body = response.read()
        return response.status, (json.loads(body) if body else None) if decode else body

    def review_module(self):
        calls = {'read': [], 'save': [], 'audio': []}
        row = {'id': 'line0001', 'start_s': 1.25, 'end_s': 3.5, 'text_source': '<script>source</script>',
               'text_zh': '原候选译稿', 'uncertain': True, 'private_path': '/Users/private/hidden'}
        audio = self.root / 'jobs' / JID / 'review_audio/line0001.mp3'
        audio.parent.mkdir(parents=True, exist_ok=True)
        audio.write_bytes(b'ID3-local-preview')
        def rows(root, jid, ids=None):
            calls['read'].append((root, jid))
            return [copy.deepcopy(row)]
        def save(root, jid, corrections):
            calls['save'].append((root, jid, corrections))
            return {'saved': True, 'rows': len(corrections), 'private_path': '/Users/private/hidden'}
        def preview(root, jid, sid):
            calls['audio'].append((root, jid, sid))
            return audio
        module = SimpleNamespace(review_rows=rows, save_corrections=save, audio_preview=preview,
                                 resume_pending=lambda root, jid: None)
        patch.dict(sys.modules, {'website_review': module}).start()
        return module, calls, audio

    def job(self, *, policy=POLICY, status='failed'):
        source = self.root / 'inputs/web' / JID / 'source.mp4'
        source.parent.mkdir(parents=True)
        source.write_bytes(b'original-media-must-stay-unchanged')
        folder = self.root / 'jobs' / JID
        folder.mkdir(parents=True)
        result = self.manager.add(input_path=source, job_id=JID, website_recovery=policy)
        job = self.manager.jobs[result['id']]
        job.update(status=status, stage=status, error='待恢复', artifacts=[])
        self.manager.pending.clear()
        self.manager.save(job)
        self.original = source
        return job

    def blocked(self, category='source_review'):
        failure = {'category': category, 'can_retry': False, 'title': '等待内容核对',
                   'segment_ids': ['line0001'], 'retry_policy': 'after_change'}
        patch.object(server, 'public_failure', return_value=failure).start()
        patch.object(server, 'retry_message', return_value='当前没有新的修订，不能重复相同内容。').start()

    def test_policy_values_rejected_before_queue_or_state_mutation(self):
        job = self.job()
        before = copy.deepcopy(job)
        for invalid in [None, [], {'reserve_gib': True}, {'reserve_gib': 0}, {'reserve_gib': 3},
                        {'enabled': 1}, {'picture_holds': 'yes'}, {'source_mode': 'anything'},
                        {'command': 'arbitrary'}, {'source_mode': None}]:
            with self.subTest(invalid=invalid):
                code, _ = self.request('POST', '/api/jobs/' + JID + '/recover', {'policy': invalid})
                self.assertEqual(code, 400)
                self.assertEqual(job, before)
                self.assertEqual(list(self.manager.pending), [])

    def test_same_job_recover_is_idempotent_and_cancel_then_resume_keeps_media(self):
        self.job()
        payload = {'policy': POLICY}
        for _ in range(2):
            code, data = self.request('POST', '/api/jobs/' + JID + '/recover', payload)
            self.assertEqual((code, data['id'], data['status']), (200, JID, 'queued'))
        self.assertEqual(list(self.manager.pending), [JID])
        self.assertEqual(len(self.manager.jobs[JID]['attempts']), 1)
        code, data = self.request('POST', '/api/jobs/' + JID + '/cancel')
        self.assertEqual((code, data['status']), (200, 'canceled'))
        code, data = self.request('POST', '/api/jobs/' + JID + '/recover', payload)
        self.assertEqual((code, data['id']), (200, JID))
        self.assertEqual(list(self.manager.pending), [JID])
        self.assertEqual(self.original.read_bytes(), b'original-media-must-stay-unchanged')

    def test_interrupted_manual_save_finishes_before_retry_guard_and_queue(self):
        job = self.job()
        order = []
        module = SimpleNamespace(resume_pending=lambda root, jid: order.append(('resume', jid)))
        with patch.dict(sys.modules, {'website_review': module}), patch.object(server, 'retry_message',
                side_effect=lambda root, value: order.append(('guard', value['id']))):
            self.manager.retry(JID)
        self.assertEqual(order, [('resume', JID), ('guard', JID)])
        self.assertEqual(job['status'], 'queued')
        self.assertEqual(list(self.manager.pending), [JID])

    def test_manual_save_conflict_prevents_both_retry_routes(self):
        job = self.job()
        before = copy.deepcopy(job)
        def conflict(root, jid):
            raise ValueError('核对期间资料已改变')
        module = SimpleNamespace(resume_pending=conflict)
        with patch.dict(sys.modules, {'website_review': module}):
            for endpoint, payload in [('retry', None), ('recover', {'policy': POLICY})]:
                code, _ = self.request('POST', '/api/jobs/' + JID + '/' + endpoint, payload)
                self.assertEqual(code, 400)
                self.assertEqual(job, before)
                self.assertEqual(list(self.manager.pending), [])

    def test_completed_or_unrelated_active_task_cannot_recover(self):
        job = self.job(status='completed')
        for status in ('completed', 'running', 'queued'):
            job['status'] = status
            code, _ = self.request('POST', '/api/jobs/' + JID + '/recover', {'policy': POLICY})
            self.assertEqual(code, 400)
            self.assertEqual(list(self.manager.pending), [])

    def test_cross_origin_cross_site_and_bad_job_id_are_rejected(self):
        self.job()
        path = '/api/jobs/' + JID + '/recover'
        for headers in ({'Origin': 'https://outside.invalid'}, {'Origin': 'null'},
                        {'Origin': 'http://127.0.0.1:1'}, {'Sec-Fetch-Site': 'cross-site'},
                        {'Host': 'outside.invalid'}):
            code, _ = self.request('POST', path, {'policy': POLICY}, headers=headers)
            self.assertEqual(code, 403)
        code, _ = self.request('POST', '/api/jobs/invalid/recover', {'policy': POLICY})
        self.assertEqual(code, 404)
        self.assertEqual(list(self.manager.pending), [])

    def test_post_body_shape_length_and_types_rejected_without_mutation(self):
        job = self.job()
        before = copy.deepcopy(job)
        for raw in (b'[]', b'null', b'"string"', b'{}', b'{"policy":{},"command":"x"}',
                    b'{bad json', b' ' * 4097):
            code, _ = self.request('POST', '/api/jobs/' + JID + '/recover', raw=raw)
            self.assertEqual(code, 400)
            self.assertEqual(job, before)
        self.assertEqual(list(self.manager.pending), [])

    def test_active_cancel_cannot_be_cleared_or_requeued_by_repeated_recover(self):
        job = self.job()
        self.manager.recover(JID, POLICY)
        self.manager.pending.clear()
        job.update(status='running', stage='处理中')
        self.manager.save(job)
        code, data = self.request('POST', '/api/jobs/' + JID + '/cancel')
        self.assertEqual(code, 200)
        self.assertTrue(self.manager.cancel_events[JID].is_set())
        code, data = self.request('POST', '/api/jobs/' + JID + '/recover', {'policy': POLICY})
        self.assertEqual((code, data['status']), (200, 'running'))
        self.assertTrue(self.manager.cancel_events[JID].is_set())
        self.assertEqual(list(self.manager.pending), [])
        self.assertEqual(len(job['attempts']), 1)

    def test_budget_exhaustion_does_not_reset_on_policy_change(self):
        job = self.job()
        before = copy.deepcopy(job)
        with patch.object(server, 'public_recovery', return_value={
                'policy': POLICY, 'events': [], 'budget': {'remaining': 0, 'exhausted': True}}):
            code, _ = self.request('POST', '/api/jobs/' + JID + '/recover', {
                'policy': {**POLICY, 'picture_holds': False}})
        self.assertEqual(code, 400)
        self.assertEqual(job, before)
        self.assertEqual(list(self.manager.pending), [])

    def test_unknown_or_malformed_budget_is_fail_closed(self):
        job = self.job()
        before = copy.deepcopy(job)
        for info in ({}, {'budget': {}}, {'budget': {'used': 0, 'max': 6, 'remaining': 6}},
                     {'budget': {'used': 0, 'max': 6, 'remaining': True, 'exhausted': False}},
                     {'budget': {'used': 0, 'max': 6, 'remaining': 6, 'exhausted': True}},
                     {'available': False, 'budget': {'used': 0, 'max': 6, 'remaining': 6, 'exhausted': False}}):
            with patch.object(server, 'public_recovery', return_value=info):
                code, _ = self.request('POST', '/api/jobs/' + JID + '/recover', {'policy': POLICY})
            self.assertEqual(code, 400)
            self.assertEqual(job, before)
        self.assertEqual(list(self.manager.pending), [])

    def test_same_policy_and_unrelated_policy_changes_cannot_bypass_content_guard(self):
        self.job()
        self.blocked('source_review')
        for policy in (POLICY, {**POLICY, 'picture_holds': False}):
            code, _ = self.request('POST', '/api/jobs/' + JID + '/recover', {'policy': policy})
            self.assertEqual(code, 400)
        self.assertEqual(list(self.manager.pending), [])

    def test_source_strict_to_contextual_can_unlock_once_but_numeric_rejection_cannot(self):
        job = self.job(policy={**POLICY, 'source_mode': 'strict'})
        self.blocked('source_review')
        code, data = self.request('POST', '/api/jobs/' + JID + '/recover', {'policy': POLICY})
        self.assertEqual((code, data['status']), (200, 'queued'))
        self.assertEqual(job['options']['website_recovery'], POLICY)
        job.update(status='failed', stage='failed')
        self.manager.pending.clear()
        job['options']['website_recovery']['source_mode'] = 'strict'
        code, _ = self.request('POST', '/api/jobs/' + JID + '/recover', {'policy': POLICY})
        self.assertEqual(code, 400)
        self.assertEqual(list(self.manager.pending), [])
        patch.object(server, 'public_failure', return_value={'category': 'translation_review',
            'can_retry': False, 'segment_ids': ['line0001']}).start()
        job.pop('website_recovery_policy_overrides')
        code, _ = self.request('POST', '/api/jobs/' + JID + '/recover', {'policy': POLICY})
        self.assertEqual(code, 400)

    def test_lower_reserve_only_for_storage_failure_and_preserved_per_job(self):
        job = self.job()
        low = {**POLICY, 'reserve_gib': 2}
        code, _ = self.request('POST', '/api/jobs/' + JID + '/recover', {'policy': low})
        self.assertEqual(code, 400)
        with patch.object(server, 'public_failure', return_value={'category': 'storage', 'can_retry': True}):
            code, data = self.request('POST', '/api/jobs/' + JID + '/recover', {'policy': low})
        self.assertEqual(code, 200)
        self.assertEqual(data['recovery']['policy']['reserve_gib'], 2)
        self.assertEqual(job['options']['website_recovery']['reserve_gib'], 2)

    def test_url_and_upload_creation_store_same_policy_and_default_eight_gib(self):
        policy = {**POLICY, 'source_mode': 'strict', 'picture_holds': False}
        with patch.object(server, 'validate_source_url', return_value='https://public.invalid/video'):
            code, linked = self.request('POST', '/api/jobs', {'source_url': 'https://public.invalid/video',
                'website_recovery': policy})
        self.assertEqual(code, 201)
        query = urlencode({'name': 'short.mp4', 'source_language': 'de', 'website_recovery': json.dumps(policy)})
        code, uploaded = self.request('POST', '/api/jobs/upload?' + query, raw=b'video')
        self.assertEqual(code, 201)
        for result in (linked, uploaded):
            self.assertEqual(self.manager.jobs[result['id']]['options']['website_recovery'], policy)
            self.assertEqual(result['recovery']['policy'], policy)
        with self.assertRaises(ValueError):
            self.manager.add(source_url='https://public.invalid/video', website_recovery={**POLICY, 'reserve_gib': 2})

    def test_public_recovery_has_only_safe_summary_not_private_prompts_paths_or_config(self):
        self.job()
        private = '/Users/private/secret endpoint https://private.invalid PRIVATE_SOURCE_TEXT api_key=PRIVATE_KEY'
        journal = {'schema': 'website_recovery_journal_v1', 'job': JID, 'max_actions': 6,
                   'max_per_input': 2, 'events': [{'id': 'a' * 64, 'input_fingerprint': 'b' * 64,
                   'action': 'source', 'status': 'succeeded', 'proxy_calls': 2,
                   'at': '2026-10-22T00:00:00+00:00', 'segment_ids': ['line0001'],
                   'prompt': private, 'path': private, 'response': private, 'error': private}]}
        path = self.root / 'jobs' / JID / 'recovery/website_journal.json'
        server.atomic_json(path, journal)
        code, result = self.request('GET', '/api/jobs/' + JID)
        self.assertEqual(code, 200)
        public = json.dumps(result, ensure_ascii=False)
        for forbidden in ('PRIVATE_SOURCE_TEXT', 'PRIVATE_KEY', '/Users/private', 'https://private.invalid'):
            self.assertNotIn(forbidden, public)
        self.assertEqual(result['recovery']['proxy_calls'], 2)
        self.assertEqual(result['recovery']['events'][0]['affected_count'], 1)

    def test_public_metrics_hide_nested_source_receipt_without_mutating_local_record(self):
        job = self.job()
        revision = {'receipt': '/Users/private/receipt.json', 'unresolved': 1, 'applied': True}
        job['metrics'] = {'source_plan_revision': revision,
            'children': [{'metrics': {'source_plan_revision': copy.deepcopy(revision)}}]}
        before = copy.deepcopy(job['metrics'])
        code, result = self.request('GET', '/api/jobs/' + JID)
        self.assertEqual(code, 200)
        self.assertNotIn('/Users/private', json.dumps(result))
        self.assertEqual(result['metrics']['source_plan_revision'], {'unresolved': 1, 'applied': True})
        self.assertNotIn('receipt', result['metrics']['children'][0]['metrics']['source_plan_revision'])
        self.assertEqual(job['metrics'], before)

    def test_server_restart_keeps_same_job_and_uses_resume_message(self):
        self.job(status='running')
        manager = server.JobManager(self.root, start_worker=False)
        job = manager.jobs[JID]
        self.assertEqual(job['status'], 'failed')
        self.assertIn('断点', job['error'])
        self.assertNotIn('重新提交', job['error'])
        self.assertEqual(self.original.read_bytes(), b'original-media-must-stay-unchanged')

    def test_manual_review_returns_only_scoped_rows_and_safe_save_summary(self):
        self.job()
        module, calls, _ = self.review_module()
        code, data = self.request('GET', '/api/jobs/' + JID + '/review')
        self.assertEqual(code, 200)
        self.assertEqual(len(data['rows']), 1)
        self.assertEqual(set(data['rows'][0]), {'id', 'start_s', 'end_s', 'text_source', 'text_zh', 'uncertain'})
        self.assertNotIn('/Users/private', json.dumps(data))
        correction = {'id': 'line0001', 'source_text': 'Corrected source', 'preferred_translation': '首选中文'}
        code, data = self.request('POST', '/api/jobs/' + JID + '/review', {'corrections': [correction]})
        self.assertEqual((code, data), (200, {'saved': True, 'rows': 1, 'requires_proxy_review': True}))
        self.assertEqual(calls['save'][0][2], [correction])
        self.assertEqual(list(self.manager.pending), [])
        self.assertEqual(self.original.read_bytes(), b'original-media-must-stay-unchanged')

    def test_manual_review_status_boundaries_cancelled_allowed_active_and_complete_denied(self):
        job = self.job(status='canceled')
        _, calls, _ = self.review_module()
        code, _ = self.request('GET', '/api/jobs/' + JID + '/review')
        self.assertEqual(code, 200)
        before = {key: len(value) for key, value in calls.items()}
        for status in ('queued', 'running', 'completed'):
            job['status'] = status
            for method, tail, payload in [
                    ('GET', '/review', None), ('GET', '/review-audio/line0001', None),
                    ('POST', '/review', {'corrections': [{'id': 'line0001', 'source_text': 'source'}]})]:
                code, _ = self.request(method, '/api/jobs/' + JID + tail, payload)
                self.assertEqual(code, 400)
        self.assertEqual({key: len(value) for key, value in calls.items()}, before)

    def test_manual_review_bad_body_or_cross_origin_never_reaches_module(self):
        self.job()
        _, calls, _ = self.review_module()
        for payload in (None, [], {}, {'corrections': []}, {'corrections': None},
                {'corrections': [{'id': 'line0001'}]},
                {'corrections': [{'id': 'line0001', 'source_text': ''}]},
                {'corrections': [{'id': 'line0001', 'source_text': 'x' * 4001}]},
                {'corrections': [{'id': '../bad', 'source_text': 'x'}]},
                {'corrections': [{'id': 'line0001', 'source_text': 'x', 'command': 'no'}]},
                {'corrections': [{'id': 'line0001', 'source_text': 'x'}] * 2},
                {'corrections': [{'id': 'line0001', 'source_text': 'x'}], 'path': 'outside'}):
            code, _ = self.request('POST', '/api/jobs/' + JID + '/review', payload)
            self.assertEqual(code, 400)
        code, _ = self.request('POST', '/api/jobs/' + JID + '/review',
            {'corrections': [{'id': 'line0001', 'source_text': 'x'}]}, headers={'Origin': 'https://outside.invalid'})
        self.assertEqual(code, 403)
        self.assertEqual(calls['save'], [])

    def test_manual_audio_is_only_regular_mp3_inside_current_job(self):
        self.job()
        module, _, audio = self.review_module()
        code, data = self.request('GET', '/api/jobs/' + JID + '/review-audio/line0001', decode=False)
        self.assertEqual((code, data), (200, audio.read_bytes()))
        outside = self.root / 'outside.mp3'
        outside.write_bytes(b'private-audio')
        module.audio_preview = lambda *args: outside
        code, _ = self.request('GET', '/api/jobs/' + JID + '/review-audio/line0001')
        self.assertEqual(code, 400)
        linked = audio.with_name('linked.mp3')
        linked.symlink_to(audio)
        module.audio_preview = lambda *args: linked
        code, _ = self.request('GET', '/api/jobs/' + JID + '/review-audio/line0001')
        self.assertEqual(code, 400)

    def test_manual_save_holds_condition_until_version_write_finishes(self):
        self.job()
        module, _, _ = self.review_module()
        saving, release, recovered = threading.Event(), threading.Event(), threading.Event()
        failures = []
        def blocking_save(*args):
            saving.set()
            if not release.wait(2):
                raise RuntimeError('fixture release timeout')
            return {'saved': True}
        module.save_corrections = blocking_save
        def save():
            try:
                self.manager.save_review(JID, [{'id': 'line0001', 'source_text': 'corrected'}])
            except Exception as error:
                failures.append(error)
        def resume():
            try:
                self.manager.recover(JID, POLICY)
                recovered.set()
            except Exception as error:
                failures.append(error)
        writer = threading.Thread(target=save)
        writer.start()
        self.assertTrue(saving.wait(1))
        runner = threading.Thread(target=resume)
        runner.start()
        self.assertFalse(recovered.wait(.05))
        self.assertEqual(list(self.manager.pending), [])
        release.set()
        writer.join(timeout=2)
        runner.join(timeout=2)
        self.assertEqual(failures, [])
        self.assertTrue(recovered.is_set())
        self.assertEqual(list(self.manager.pending), [JID])

    def test_manual_correction_then_plain_retry_does_not_reset_exhausted_recovery_budget(self):
        job = self.job()
        self.review_module()
        info = {'policy': POLICY, 'events': [], 'proxy_calls': 12,
                'budget': {'used': 6, 'max': 6, 'remaining': 0, 'exhausted': True}}
        with patch.object(server, 'public_recovery', return_value=info):
            code, _ = self.request('POST', '/api/jobs/' + JID + '/recover', {'policy': POLICY})
            self.assertEqual(code, 400)
            code, _ = self.request('POST', '/api/jobs/' + JID + '/review',
                {'corrections': [{'id': 'line0001', 'preferred_translation': '人工首选中文'}]})
            self.assertEqual(code, 200)
            with patch.object(server, 'retry_message', return_value=None):
                code, data = self.request('POST', '/api/jobs/' + JID + '/retry')
            self.assertEqual((code, data['id'], data['status']), (200, JID, 'queued'))
            self.assertEqual(data['recovery']['budget']['remaining'], 0)
        self.assertEqual(job['options']['website_recovery'], POLICY)


if __name__ == '__main__':
    unittest.main()
