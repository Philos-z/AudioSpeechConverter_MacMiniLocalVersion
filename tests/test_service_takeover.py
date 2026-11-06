"""Crash/EOF/lease tests use only temporary tasks and tiny Python processes."""
import copy
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'web'))
import task_takeover as takeover
import service
spec = importlib.util.spec_from_file_location('takeover_test_server', ROOT / 'web/server.py')
server = importlib.util.module_from_spec(spec); spec.loader.exec_module(server)
JID = 'web_20990101_120000_00000001'


def eventually(predicate, seconds=6):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate(): return True
        time.sleep(.02)
    return False


class TakeoverTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source = self.root / 'inputs/web' / JID / 'source.mp4'
        self.source.parent.mkdir(parents=True); self.source.write_bytes(b'original fixture video')
        self.manager = server.JobManager(self.root, start_worker=False)
        self.manager.add(input_path=self.source, job_id=JID)
        self.job = self.manager.jobs[JID]
        self.job.update(status='running', progress=42, stage='真实旧阶段', error='真实旧错误')

    def test_new_policy_and_public_summary(self):
        self.assertEqual(self.job['options']['restart_policy'], takeover.POLICY)
        self.assertEqual(self.job['options']['autonomy_policy'], 'evidence_first_v1')
        self.assertTrue(self.job['options']['autonomous_content_review'])
        self.assertEqual(self.job['options']['speech_quality_policy'], 'layered_full_content_v1')
        self.assertTrue(self.manager.public(self.job)['automatic_takeover']['enabled'])
        self.assertNotIn('source_binding', self.manager.public(self.job)['automatic_takeover'])

    def test_valid_restart_preserves_attempt_and_original_error(self):
        before = copy.deepcopy(self.job)
        self.assertTrue(takeover.prepare_takeover(self.root, self.job, timestamp=100))
        self.assertEqual(self.job['status'], 'queued')
        self.assertEqual(self.job['takeover']['retry_at_unix'], 102)
        evidence = takeover.read_json(self.root / 'reports/web_app/takeovers' / JID / '01.json')
        self.assertEqual(evidence['job'], before)
        self.assertEqual(self.job['attempts'][-1]['error'], '真实旧错误')
        self.assertEqual(self.job['attempts'][-1]['status'], 'interrupted')

    def test_interruption_between_evidence_and_job_save_reconciles_once(self):
        before = copy.deepcopy(self.job)
        self.assertTrue(takeover.prepare_takeover(self.root, self.job, timestamp=100))
        first_event = copy.deepcopy(self.job['takeover']['events'][0])
        evidence = self.root / 'reports/web_app/takeovers' / JID / '01.json'
        evidence_before = evidence.read_bytes()
        # Restore the actual disk state that existed before the queued commit.
        self.job = before
        self.assertTrue(takeover.prepare_takeover(self.root, self.job, timestamp=200))
        self.assertEqual(self.job['takeover']['events'], [first_event])
        self.assertEqual(evidence.read_bytes(), evidence_before)

    def test_unchanged_progress_cannot_crash_loop(self):
        for _ in range(2):
            self.job['status'] = 'running'
            self.assertTrue(takeover.prepare_takeover(self.root, self.job, timestamp=100))
        self.job['status'] = 'running'
        self.assertFalse(takeover.prepare_takeover(self.root, self.job, timestamp=100))
        self.assertEqual(len(self.job['takeover']['events']), 2)
        self.assertIn('未取得新进展', self.job['error'])

    def test_progress_does_not_reset_lifetime_budget(self):
        for number in range(6):
            self.job.update(status='running', progress=number + 1)
            self.job['takeover']['progress_floor'] = number
            self.assertTrue(takeover.prepare_takeover(self.root, self.job, timestamp=100))
        self.job.update(status='running', progress=90)
        self.assertFalse(takeover.prepare_takeover(self.root, self.job, timestamp=100))
        self.assertEqual(len(self.job['takeover']['events']), 6)

    def test_cancel_persists_before_signal_and_survives_reload(self):
        self.manager.save(self.job)
        self.manager.cancel(JID)
        persisted = json.loads((self.root / 'web/state/jobs' / (JID + '.json')).read_text())
        self.assertTrue(persisted['cancel_requested_at'])
        loaded = server.JobManager(self.root, start_worker=False)
        self.assertEqual(loaded.jobs[JID]['status'], 'canceled')
        self.assertEqual(list(loaded.pending), [])

    def test_legacy_running_task_requires_manual_start(self):
        self.job['options'].pop('restart_policy')
        self.assertFalse(takeover.prepare_takeover(self.root, self.job))
        self.assertIn('旧任务', self.job['error'])

    def test_completed_delivery_is_never_automatically_requeued(self):
        self.job.update(status='completed', artifacts=[{'label': 'old', 'path': 'old.mp4'}])
        before = copy.deepcopy(self.job)
        self.assertFalse(takeover.prepare_takeover(self.root, self.job))
        self.assertEqual(before, self.job)

    def test_source_changed_is_rejected(self):
        self.source.write_bytes(b'changed fixture video!')
        self.assertFalse(takeover.prepare_takeover(self.root, self.job))
        self.assertEqual(self.job['status'], 'failed')
        self.assertNotIn('events', self.job['takeover'])

    def test_source_symlink_is_rejected(self):
        original = self.source.with_name('original.mp4'); self.source.rename(original)
        self.source.symlink_to(original)
        self.assertFalse(takeover.prepare_takeover(self.root, self.job))

    def test_bad_budget_is_not_reset(self):
        path = self.root / 'jobs' / JID / 'recovery/website_journal.json'
        takeover.write_json(path, {'schema': 'wrong', 'events': []})
        before = path.read_bytes()
        self.assertFalse(takeover.prepare_takeover(self.root, self.job))
        self.assertEqual(path.read_bytes(), before)

    def test_no_progress_uses_content_not_timestamps(self):
        marker = takeover.progress_marker(self.root, self.job)
        self.job['updated_at'] = '2099-01-01T00:00:00Z'
        self.job['stage'] = '再次准备素材'
        self.assertEqual(marker, takeover.progress_marker(self.root, self.job))

    def test_kernel_lease_excludes_other_owner(self):
        path = takeover.runtime(self.root) / 'server.lock'
        with takeover.InstanceLock(path):
            with self.assertRaises(BlockingIOError): takeover.InstanceLock(path)
        with takeover.InstanceLock(path): pass

    def test_shared_child_lease_blocks_new_worker_barrier(self):
        with takeover.InstanceLock(takeover.runtime(self.root) / 'children.lock', shared=True):
            self.assertFalse(takeover.children_quiet(self.root))
        self.assertTrue(takeover.children_quiet(self.root))

    def test_real_command_success_receipt_and_failure_code(self):
        ok = self.manager.command(JID, [sys.executable, '-c', "print('fixture success')"])
        self.assertEqual(ok['returncode'], 0)
        self.assertIn('fixture success', ok['stdout'])
        bad = self.manager.command(JID, [sys.executable, '-c', 'raise SystemExit(7)'])
        self.assertEqual(bad['returncode'], 7)
        self.assertTrue(takeover.children_quiet(self.root))

    def test_actual_parent_crash_cleans_own_guardian_and_children(self):
        ready = self.root / 'ready'
        guard_ready = self.root / 'guard_ready'
        script = self.root / 'parent.py'
        child_code = f'from pathlib import Path;import time;Path({str(ready)!r}).write_text("ready");time.sleep(60)'
        # The production JobManager.command itself runs in a fixture parent.
        # We kill that directly-owned parent, not any production service.
        script.write_text("import sys\nfrom pathlib import Path\n"
            f"sys.path.insert(0, {str(ROOT / 'web')!r})\nimport server\n"
            f"m=server.JobManager({str(self.root)!r},start_worker=False)\n"
            f"m.command({JID!r}, [sys.executable, '-c', "
            f"{child_code!r}])\n")
        process = subprocess.Popen([sys.executable, '-B', str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertTrue(eventually(ready.exists))
            self.assertFalse(takeover.children_quiet(self.root))
            process.kill(); process.wait(timeout=3)
            self.assertTrue(eventually(lambda: takeover.children_quiet(self.root)))
        finally:
            if process.poll() is None: process.kill(); process.wait()
            process.stdout.close(); process.stderr.close()

    def test_guardian_kills_term_ignoring_descendant_before_lease_release(self):
        lock_path = self.root / 'descendant.lock'
        ready = self.root / 'descendant_ready'
        child_code = ('import fcntl,signal,time;from pathlib import Path;'
            'signal.signal(signal.SIGTERM,signal.SIG_IGN);'
            f'f=open({str(lock_path)!r},"a+");fcntl.flock(f,fcntl.LOCK_EX);'
            f'Path({str(ready)!r}).write_text("ready");time.sleep(60)')
        main_code = ('import subprocess,sys,time;'
            f'p=subprocess.Popen([sys.executable,"-c",{child_code!r}]);time.sleep(.3)')
        result = self.manager.command(JID, [sys.executable, '-c', main_code])
        self.assertTrue(ready.exists())
        self.assertEqual(result['returncode'], 0)
        self.assertTrue(takeover.children_quiet(self.root))
        with takeover.InstanceLock(lock_path): pass

    def test_supervisor_stops_after_two_same_progress_crashes(self):
        generation = 'a' * 32
        takeover.write_json(takeover.runtime(self.root) / 'control.json', {'desired': 'running', 'generation': generation})
        count = self.root / 'launches'
        command = [sys.executable, '-c', f'from pathlib import Path;p=Path({str(count)!r});p.write_text(p.read_text()+"x" if p.exists() else "x");raise SystemExit(8)']
        journal = service.supervise(self.root, generation, command=command, poll=.01, backoff_scale=.001)
        self.assertEqual(count.read_text(), 'xx')
        self.assertEqual(journal['state'], 'needs_attention')
        self.assertEqual([e['returncode'] for e in journal['events']], [8, 8])

    def test_explicit_stop_is_durable_and_does_not_respawn(self):
        generation = 'b' * 32
        takeover.write_json(takeover.runtime(self.root) / 'control.json', {'desired': 'stopped', 'generation': generation})
        journal = service.supervise(self.root, generation,
            command=[sys.executable, '-c', "raise RuntimeError('must not launch')"], poll=.01)
        self.assertEqual(journal['events'], [])
        self.assertEqual(journal['state'], 'stopped')
        self.assertEqual(takeover.read_json(takeover.runtime(self.root) / 'control.json')['desired'], 'stopped')

    def test_live_supervisor_stops_own_child_without_a_pid_file_signal(self):
        generation = 'c' * 32
        directory = takeover.runtime(self.root)
        takeover.write_json(directory / 'control.json', {'desired': 'running', 'generation': generation})
        ready = self.root / 'service_ready'
        lock = self.root / 'owned_service.lock'
        command = [sys.executable, '-c', f'import fcntl,time;from pathlib import Path;f=open({str(lock)!r},"a+");fcntl.flock(f,fcntl.LOCK_EX);Path({str(ready)!r}).write_text("ready");time.sleep(60)']
        script = self.root / 'supervisor_fixture.py'
        script.write_text(f'import sys;sys.path.insert(0,{str(ROOT / "web")!r});import service;service.supervise({str(self.root)!r},{generation!r},command={command!r},poll=.01,backoff_scale=.001)')
        process = subprocess.Popen([sys.executable, '-B', str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertTrue(eventually(ready.exists))
            # An unrelated bogus PID file cannot influence shutdown.
            (self.root / 'reports/web_app/server.pid').write_text(str(os.getpid()))
            with patch.object(service, 'status', return_value=None): service.stop(self.root, timeout=5)
            process.wait(timeout=5)
            self.assertEqual(process.returncode, 0)
            with takeover.InstanceLock(lock): pass
            self.assertEqual(takeover.read_json(directory / ('restarts_' + generation + '.json'))['state'], 'stopped')
        finally:
            if process.poll() is None: process.kill(); process.wait()
            process.stdout.close(); process.stderr.close()

    def test_reloaded_authorized_running_task_is_queued_once(self):
        self.manager.save(self.job)
        manager = server.JobManager(self.root, start_worker=False)
        self.assertEqual(list(manager.pending), [JID])
        self.assertEqual(manager.jobs[JID]['status'], 'queued')
        # Another service boot while still queued consumes no second attempt.
        again = server.JobManager(self.root, start_worker=False)
        self.assertEqual(list(again.pending), [JID])
        self.assertEqual(len(again.jobs[JID]['takeover']['events']), 1)

    def test_worker_waits_for_old_child_lease_and_rechecks_source(self):
        self.manager.save(self.job)
        called = threading.Event()
        import pipeline
        def forbidden(*args, **kwargs):
            called.set()
            raise AssertionError('must not run a changed source')
        with takeover.InstanceLock(takeover.runtime(self.root) / 'children.lock', shared=True):
            with patch.object(pipeline, 'run_job', side_effect=forbidden):
                manager = server.JobManager(self.root, start_worker=True)
                try:
                    time.sleep(.2)
                    self.assertFalse(called.is_set())
                    self.assertEqual(manager.jobs[JID]['status'], 'queued')
                    self.source.write_bytes(b'changed after queued')
                finally:
                    # Release the fixture lease before waiting for the worker.
                    manager.jobs[JID]['takeover']['retry_at_unix'] = 0
        try:
            self.assertTrue(eventually(lambda: manager.jobs[JID]['status'] == 'failed'))
            self.assertFalse(called.is_set())
        finally:
            manager.stopping.set()
            with manager.condition: manager.condition.notify_all()
            manager.thread.join(timeout=3)

    def test_service_stop_is_not_a_per_task_cancel(self):
        self.job.update(status='queued', error=None)
        self.manager.save(self.job)
        started = threading.Event()
        import pipeline
        def fake_flow(root, job, update, command, canceled):
            started.set()
            while not canceled(): time.sleep(.01)
            raise RuntimeError('fixture service stopping')
        with patch.object(pipeline, 'run_job', side_effect=fake_flow):
            manager = server.JobManager(self.root, start_worker=True)
            self.assertTrue(started.wait(3))
            manager.stopping.set()
            with manager.condition: manager.condition.notify_all()
            manager.thread.join(timeout=3)
        self.assertFalse(manager.thread.is_alive())
        self.assertEqual(manager.jobs[JID]['status'], 'running')
        self.assertEqual(manager.jobs[JID]['takeover']['state'], 'service_stopped')
        self.assertNotIn('cancel_requested_at', manager.jobs[JID])
        # Only a subsequent explicit service start creates this new manager.
        resumed = server.JobManager(self.root, start_worker=False)
        self.assertEqual(resumed.jobs[JID]['status'], 'queued')

    def test_liveness_http_is_local_and_single_instance(self):
        httpd = server.make_server(self.root, host='127.0.0.1', port=0, start_worker=False)
        thread = threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        try:
            with self.assertRaises(BlockingIOError):
                server.make_server(self.root, host='127.0.0.1', port=0, start_worker=False)
            with patch.object(httpd.manager, 'check_proxy', side_effect=AssertionError('no Proxy')):
                connection = http.client.HTTPConnection('127.0.0.1', httpd.server_address[1], timeout=3)
                connection.request('GET', '/api/liveness')
                response = connection.getresponse(); value = json.loads(response.read()); connection.close()
                self.assertEqual(response.status, 200)
                self.assertIs(value['worker_alive'], False)
                self.assertEqual(value['project_identity'], hashlib.sha256(str(self.root).encode()).hexdigest())
        finally:
            httpd.shutdown(); httpd.server_close(); thread.join(timeout=2)

    def test_url_source_binds_only_complete_task_local_video(self):
        import source_acquisition
        jid = 'web_20990101_120000_00000002'
        self.manager.add(source_url='https://example.com/video', job_id=jid)
        job = self.manager.jobs[jid]
        job['status'] = 'running'
        self.assertEqual(job['takeover']['phase'], 'acquiring_source')
        self.assertNotIn('source_binding', job['takeover'])
        source = source_acquisition.directory(self.root, jid, job['source_url']) / 'source.mp4'
        source.parent.mkdir(parents=True)
        ffmpeg = '/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg'
        if not Path(ffmpeg).is_file(): ffmpeg = shutil.which('ffmpeg')
        self.assertTrue(ffmpeg, 'installed project media runtime required')
        subprocess.run([ffmpeg, '-v', 'error', '-nostdin', '-f', 'lavfi', '-i',
            'color=c=black:s=16x16:r=2:d=0.5', '-f', 'lavfi', '-i',
            'sine=frequency=440:sample_rate=8000:duration=0.5', '-c:v', 'libx264', '-c:a', 'aac',
            '-shortest', '-y', str(source)], check=True, capture_output=True, timeout=20)
        source_acquisition.finalize(self.root, jid, job['source_url'])
        self.manager.update(jid, '下载完成', 5)
        self.assertTrue(takeover.prepare_takeover(self.root, job))
        self.assertEqual(job['takeover']['source_binding']['sha256'], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertTrue(job['takeover']['source_ever_bound'])

    def test_first_url_download_can_resume_same_frozen_authorized_request(self):
        jid = 'web_20990101_120000_00000002'
        self.manager.add(source_url='https://example.com/video', job_id=jid)
        job = self.manager.jobs[jid]; job.update(status='running', stage='下载视频到本机')
        self.assertTrue(takeover.prepare_takeover(self.root, job, timestamp=0))
        self.assertTrue(takeover.validate_queued_takeover(self.root, job))
        self.assertEqual(job['takeover']['events'][0]['phase'], 'acquiring_source')
        self.assertNotIn('source_binding', job['takeover'])

    def test_first_url_download_changed_request_or_policy_is_not_resumed(self):
        jid = 'web_20990101_120000_00000002'
        self.manager.add(source_url='https://example.com/video', job_id=jid)
        job = self.manager.jobs[jid]; job.update(status='running')
        self.assertTrue(takeover.prepare_takeover(self.root, job, timestamp=0))
        job['source_url'] = 'https://example.com/different-video'
        with self.assertRaises(ValueError): takeover.validate_queued_takeover(self.root, job)

    def test_manual_retry_can_resume_its_original_unbound_url_after_cancel(self):
        jid = 'web_20990101_120000_00000002'
        self.manager.add(source_url='https://example.com/video', job_id=jid)
        job = self.manager.jobs[jid]; job.update(status='running')
        self.assertTrue(takeover.prepare_takeover(self.root, job, timestamp=0))
        self.manager.cancel(jid)
        self.assertEqual(job['status'], 'canceled')
        self.manager.retry(jid)
        self.assertFalse(takeover.validate_queued_takeover(self.root, job))
        self.assertEqual(job['queue_reason'], 'manual_retry')
        self.assertNotIn('cancel_requested_at', job)

    def test_bound_url_cannot_delete_binding_and_downgrade_to_download(self):
        self.test_url_source_binds_only_complete_task_local_video()
        jid = 'web_20990101_120000_00000002'; job = self.manager.jobs[jid]
        job['takeover'].pop('source_binding')
        job['takeover'].update(phase='acquiring_source', source_ever_bound=False)
        with self.assertRaises(ValueError): takeover.validate_queued_takeover(self.root, job)

    def test_actual_empty_http_service_start_stop_is_supervised(self):
        # Use a separate empty root: there is no task/model/API to execute.
        empty = self.root / 'empty_http_service'
        empty.mkdir()
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        try:
            service.start(empty, port=port, host='127.0.0.1')
            current = service.status(empty, port)
            self.assertIsNotNone(current)
            self.assertTrue(current['automatic_takeover']['supervised'])
            self.assertTrue(current['worker']['ready'])
            self.assertFalse(current['automatic_takeover']['login_autostart'])
            self.assertTrue(service._alive(empty, port))
            service.stop(empty, port=port, timeout=8)
            self.assertIsNone(service.status(empty, port))
            self.assertFalse(service._held(takeover.runtime(empty) / 'supervisor.lock'))
            self.assertEqual(takeover.read_json(takeover.runtime(empty) / 'control.json')['desired'], 'stopped')
        finally:
            # Idempotent durable stop leaves no fixture service behind.
            service.stop(empty, port=port, timeout=8)


if __name__ == '__main__': unittest.main()
