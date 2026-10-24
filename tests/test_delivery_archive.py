import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'web'))
import delivery_archive as archive


class DeliveryArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.jid='web_20260115_102030_abcdef12'
        self.folder=self.root/'jobs'/self.jid;self.folder.mkdir(parents=True)
        self.job={'id':self.jid,'source_sha256':'a'*64,'title':'测试视频.webs.h264',
                  'created_at':'2026-01-15T10:20:30+00:00'}
        self.artifacts=[self.artifact('outputs/final.mp4','video',b'approved video'),
                        self.artifact('outputs/zh.srt','subtitle','完整字幕'.encode())]
        mock=patch.object(archive.shutil,'disk_usage',return_value=types.SimpleNamespace(free=20*1024**3))
        mock.start();self.addCleanup(mock.stop)

    def artifact(self, name, kind, data):
        p=self.folder/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
        return {'path':str(p),'kind':kind,'label':kind,'sha256':hashlib.sha256(data).hexdigest()}

    def run_archive(self):return archive.archive_delivery(self.root,self.job,self.artifacts)

    def test_copy_is_verified_independent_and_idempotent(self):
        result=self.run_archive();directory=Path(result['directory'])
        target=directory/'中文双语成片.mp4';self.assertEqual(target.read_bytes(),b'approved video')
        self.assertEqual((directory/'中文字幕.srt').read_text(),'完整字幕')
        self.assertNotEqual(target.stat().st_ino,Path(self.artifacts[0]['path']).stat().st_ino)
        self.assertEqual(self.run_archive(),result)
        self.assertFalse(result['human_reviewed']);self.assertTrue(result['originals_preserved'])
        Path(self.artifacts[0]['path']).write_bytes(b'working file changed')
        self.assertEqual(target.read_bytes(),b'approved video')
        self.assertEqual(len(list((self.root/'成片').iterdir())),1)

    def test_new_version_preserves_existing_archive(self):
        first=self.run_archive();self.artifacts[0]=self.artifact('outputs/final.mp4','video',b'new approved video')
        second=self.run_archive();self.assertNotEqual(first['directory'],second['directory'])
        self.assertEqual((Path(first['directory'])/'中文双语成片.mp4').read_bytes(),b'approved video')

    def test_episodes_with_repeated_names_remain_separate(self):
        self.artifacts=[]
        for n in [1,2]:
            self.artifacts.extend([self.artifact(f'outputs/episode_{n:03d}/final.mp4','video',f'video {n}'.encode()),
                self.artifact(f'outputs/episode_{n:03d}/zh.srt','subtitle',f'subtitle {n}'.encode())])
        result=self.run_archive();p=Path(result['directory'])
        self.assertEqual((p/'episode_002/中文字幕.srt').read_bytes(),b'subtitle 2')
        self.assertEqual((p/'episode_001/中文双语成片.mp4').read_bytes(),b'video 1')

    def test_changed_source_and_symlinks_are_rejected(self):
        p=Path(self.artifacts[0]['path']);p.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'校验'):self.run_archive()
        p.unlink();other=self.root/'other.mp4';other.write_bytes(b'approved video');p.symlink_to(other)
        with self.assertRaisesRegex(ValueError,'符号链接'):self.run_archive()
        self.assertEqual(other.read_bytes(),b'approved video')

    def test_exact_size_limit_and_insufficient_space_rejected(self):
        p=Path(self.artifacts[0]['path'])
        with p.open('wb') as f:f.truncate(50_000_000)
        self.artifacts[0]['sha256']=archive.sha(p)
        with self.assertRaisesRegex(ValueError,'50MB'):self.run_archive()
        self.artifacts[0]=self.artifact('outputs/final.mp4','video',b'approved video')
        with patch.object(archive.shutil,'disk_usage',return_value=types.SimpleNamespace(free=archive.RESERVE)):
            with self.assertRaisesRegex(RuntimeError,'空间不足'):self.run_archive()
        self.assertTrue(p.is_file())

    def test_bad_copy_never_publishes_partial_delivery(self):
        def corrupt(source, target):Path(target).write_bytes(b'incomplete')
        with patch.object(archive.shutil,'copy2',side_effect=corrupt):
            with self.assertRaisesRegex(RuntimeError,'校验失败'):self.run_archive()
        self.assertEqual(list((self.root/'成片').iterdir()),[])
        self.assertFalse((self.folder/'delivery_archive.json').exists())

    def test_renaming_job_does_not_duplicate_existing_delivery(self):
        first=self.run_archive();self.job['title']='新名称';self.job['created_at']='2026-10-24T01:00:00+00:00'
        self.assertEqual(self.run_archive(),first)

    def test_damaged_archive_metadata_is_preserved_and_new_version_is_saved(self):
        first=self.run_archive();manifest=Path(first['directory'])/'归档记录.json'
        damaged=copy.deepcopy(first);damaged['job_id']='another_job'
        manifest.write_text(json.dumps(damaged))
        second=self.run_archive()
        self.assertNotEqual(second['directory'],first['directory'])
        self.assertEqual(json.loads(manifest.read_text())['job_id'],'another_job')
        self.assertEqual(second['job_id'],self.jid)

    def test_worker_archives_url_delivery_before_marking_completed(self):
        import server
        import pipeline
        manager=server.JobManager(self.root,start_worker=False)
        job={k:v for k,v in self.job.items() if k!='source_sha256'}
        job.update(status='queued',stage='queued',progress=0,source_url='https://example.invalid/video')
        manager.jobs[self.jid]=job;manager.cancel_events[self.jid]=threading.Event()
        manager.pending.append(self.jid)
        def finished(*args,**kwargs):
            manager.stopping.set()
            return {'artifacts':self.artifacts,'source_sha256':'b'*64,'duration_s':12.}
        with patch.object(pipeline,'run_job',side_effect=finished),patch.object(server,'checkpoint_marker',return_value=None):
            manager.worker()
        self.assertEqual(job['status'],'completed')
        self.assertEqual(job['source_sha256'],'b'*64)
        self.assertTrue((Path(job['delivery_archive']['directory'])/'中文双语成片.mp4').is_file())
        self.assertEqual(job['artifacts'][0]['path'],self.artifacts[0]['path'])

    def test_explicit_completed_retry_preserves_old_delivery_and_attempt(self):
        import server
        manager=server.JobManager(self.root,start_worker=False)
        job={**self.job,'status':'completed','stage':'completed','progress':100,
             'artifacts':copy.deepcopy(self.artifacts),'updated_at':self.job['created_at']}
        manager.jobs[self.jid]=job
        with patch.object(manager,'_resume_manual_review',side_effect=AssertionError('Completed media is not a failed manual edit')),patch.object(manager,'public',return_value={}):
            manager.retry(self.jid)
        self.assertEqual(job['status'],'queued')
        self.assertEqual(job['attempts'][-1]['status'],'completed')
        self.assertEqual(job['artifacts'],self.artifacts)
        self.assertEqual(list(manager.pending),[self.jid])


if __name__=='__main__':unittest.main()
