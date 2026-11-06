"""Independent hostile restart states; temporary tasks, no model/API work."""
import copy
import hashlib
import importlib.util
import json
import shutil
import signal
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'web'))
import task_takeover as takeover
import service
import pipeline
spec=importlib.util.spec_from_file_location('adversarial_takeover_server',ROOT/'web/server.py')
server=importlib.util.module_from_spec(spec);spec.loader.exec_module(server)
JID='web_20990101_120000_00000001'


class TakeoverAdversarialTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve()
        self.source=self.root/'inputs/web'/JID/'source.mp4'
        self.source.parent.mkdir(parents=True);self.source.write_bytes(b'immutable uploaded fixture')
        self.manager=server.JobManager(self.root,start_worker=False)
        self.manager.add(input_path=self.source,job_id=JID,
            source_sha256=hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.job=self.manager.jobs[JID]
        self.job.update(status='running',progress=42.,stage='真实中断阶段',error=None)

    def queued(self):
        self.assertTrue(takeover.prepare_takeover(self.root,self.job,timestamp=0))
        self.assertEqual(self.job['status'],'queued')
        self.manager.save(self.job)

    def observe_worker(self, *, blocked=None):
        called=threading.Event()
        def forbidden(*args,**kwargs):
            called.set()
            raise RuntimeError('fixture pipeline execution must have been blocked')
        manager=None
        with patch.object(pipeline,'run_job',side_effect=forbidden),\
                patch.object(server,'retry_message',return_value=blocked),\
                patch('failure_triage.retry_message',return_value=blocked):
            manager=server.JobManager(self.root,start_worker=True)
            try:
                called.wait(.3)
            finally:
                manager.stopping.set()
                with manager.condition:manager.condition.notify_all()
                manager.thread.join(timeout=2)
        self.assertFalse(manager.thread.is_alive())
        return called.is_set(),manager

    def test_queued_takeover_cannot_drop_its_binding_to_run_replaced_source(self):
        self.queued()
        self.job['takeover'].pop('source_binding')
        self.source.write_bytes(b'completely replaced upload')
        self.manager.save(self.job)
        called,manager=self.observe_worker()
        self.assertFalse(called)
        self.assertEqual(manager.jobs[JID]['status'],'failed')

    def test_queued_takeover_rechecks_revoked_restart_and_recovery_policies(self):
        self.queued()
        before=copy.deepcopy(self.job)
        for field in ('restart_policy','website_recovery'):
            self.job=copy.deepcopy(before)
            if field=='restart_policy':self.job['options'].pop('restart_policy')
            else:self.job['options']['website_recovery']['enabled']=False
            self.manager.save(self.job)
            called,manager=self.observe_worker()
            with self.subTest(field=field):
                self.assertFalse(called)
                self.assertEqual(manager.jobs[JID]['status'],'failed')

    def test_queued_takeover_cannot_bypass_new_current_content_refusal(self):
        self.queued()
        called,manager=self.observe_worker(blocked='当前绑定的真实内容拒绝尚未解消。')
        self.assertFalse(called)
        self.assertEqual(manager.jobs[JID]['status'],'failed')

    def test_explicit_queued_cancel_always_wins_over_ready_takeover(self):
        self.queued()
        self.job['cancel_requested_at']='2026-10-26T10:00:00+00:00'
        self.manager.save(self.job)
        called,manager=self.observe_worker()
        self.assertFalse(called)
        self.assertEqual(manager.jobs[JID]['status'],'canceled')
        self.assertFalse(manager.pending)

    def test_same_id_in_two_different_state_files_does_not_duplicate_queue(self):
        self.queued()
        forged=self.root/'web/state/jobs/web_aaa_wrong_filename.json'
        takeover.write_json(forged,self.job)
        loaded=server.JobManager(self.root,start_worker=False)
        self.assertEqual(list(loaded.pending).count(JID),1)
        self.assertEqual(set(loaded.jobs),{JID})

    def test_source_path_cannot_be_redirected_to_another_tasks_identical_bytes(self):
        other=self.root/'inputs/web/web_another_task/source.mp4'
        other.parent.mkdir(parents=True);other.write_bytes(self.source.read_bytes())
        self.job['input_path']=str(other)
        self.assertFalse(takeover.prepare_takeover(self.root,self.job))
        self.assertEqual(self.job['status'],'failed')

    def test_same_progress_cannot_be_reset_using_nonfinite_or_out_of_range_numbers(self):
        for progress in (float('nan'),float('inf'),-1.,101.):
            job=copy.deepcopy(self.job);job['progress']=progress
            with self.subTest(progress=progress):
                self.assertFalse(takeover.prepare_takeover(self.root,job,timestamp=0))
                self.assertEqual(job['status'],'failed')
            evidence=self.root/'reports/web_app/takeovers'/JID
            if evidence.exists():shutil.rmtree(evidence)

    def test_supervisor_rejects_wrong_generation_or_schema_journal(self):
        generation='a'*32
        directory=takeover.runtime(self.root)
        takeover.write_json(directory/'control.json',{'desired':'stopped','generation':generation})
        path=directory/('restarts_'+generation+'.json')
        for schema,saved_generation in [('wrong-schema',generation),('service_supervision_v1','b'*32)]:
            takeover.write_json(path,{'schema':schema,'generation':saved_generation,'events':[]})
            handlers={sig:signal.getsignal(sig) for sig in (signal.SIGTERM,signal.SIGINT)}
            with self.subTest(schema=schema,generation=saved_generation),self.assertRaises(ValueError):
                service.supervise(self.root,generation,command=[sys.executable,'-c','raise SystemExit(8)'],poll=.01)
            for sig,handler in handlers.items():self.assertEqual(signal.getsignal(sig),handler)

    def test_held_supervisor_lease_prevents_a_second_launcher(self):
        generation='c'*32
        directory=takeover.runtime(self.root)
        takeover.write_json(directory/'control.json',{'desired':'running','generation':generation})
        with takeover.InstanceLock(directory/'supervisor.lock'):
            with patch.object(service.subprocess,'Popen',side_effect=AssertionError('must not launch')):
                with self.assertRaises(BlockingIOError):
                    service.supervise(self.root,generation,command=['never-spawn'],poll=.01)

    def url_job(self):
        jid='web_20990101_120000_00000002'
        self.manager.add(source_url='https://example.invalid/approved-source',job_id=jid)
        job=self.manager.jobs[jid]
        job.update(status='running',progress=1.,stage='下载视频到本机',error=None)
        return job

    def test_initial_authorized_url_download_can_resume_and_is_bound_to_original_request(self):
        job=self.url_job()
        self.assertTrue(takeover.prepare_takeover(self.root,job,timestamp=0))
        self.assertEqual(job['takeover']['events'][-1]['phase'],'acquiring_source')
        self.assertTrue(takeover.validate_queued_takeover(self.root,job))
        self.assertNotIn('source_binding',job['takeover'])
        original=copy.deepcopy(job)
        job['source_url']='https://example.invalid/different-source'
        with self.assertRaises(ValueError):takeover.validate_queued_takeover(self.root,job)
        job=copy.deepcopy(original);job['options'].pop('restart_policy')
        with self.assertRaises(ValueError):takeover.validate_queued_takeover(self.root,job)
        job=copy.deepcopy(original);job['options']['website_recovery']['enabled']=False
        with self.assertRaises(ValueError):takeover.validate_queued_takeover(self.root,job)

    def test_url_download_resume_requires_immutable_request_receipt_and_not_legacy_guessing(self):
        job=self.url_job();original=copy.deepcopy(job)
        request_path=takeover._request_path(self.root,job)
        request_path.unlink()
        self.assertFalse(takeover.prepare_takeover(self.root,job,timestamp=0))
        job=copy.deepcopy(original)
        job['takeover'].pop('source_request_binding')
        self.assertFalse(takeover.prepare_takeover(self.root,job,timestamp=0))

    def test_bound_upload_cannot_be_downgraded_to_url_by_deleting_original_binding(self):
        # Model an immutable bound-source marker left by a completed first
        # acquisition, independent of the mutable job state being corrupted.
        job=self.url_job()
        bound=takeover._request_path(self.root,job).with_suffix('.bound.json')
        takeover.write_json(bound,{'schema':'bound_source_identity_v1','source_binding':{'sha256':'a'*64}})
        job['takeover'].pop('source_binding',None)
        job['takeover'].update(phase='acquiring_source',source_ever_bound=False)
        self.assertFalse(takeover.prepare_takeover(self.root,job,timestamp=0))

    def test_authorized_url_queued_cancel_still_wins_before_download(self):
        job=self.url_job()
        self.assertTrue(takeover.prepare_takeover(self.root,job,timestamp=0))
        job['cancel_requested_at']='2026-10-26T10:00:00+00:00'
        with self.assertRaises(ValueError):takeover.validate_queued_takeover(self.root,job)


if __name__=='__main__':unittest.main()
