"""Real tiny media and local download-controller fixtures; no network or models."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'web'))
import source_acquisition as acquisition
import pipeline
import task_takeover as takeover

JID='web_20990101_120000_00000001'
URL='https://example.invalid/authorised-episode?edition=one'


class SourceAcquisitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media_temp=tempfile.TemporaryDirectory()
        home=Path(cls.media_temp.name)
        cls.av=home/'audio_video.mp4';cls.video=home/'video_only.mp4'
        ffmpeg=shutil.which('ffmpeg')
        if not ffmpeg:raise unittest.SkipTest('local ffmpeg unavailable')
        common=[ffmpeg,'-v','error','-nostdin','-y','-f','lavfi','-i','color=c=navy:s=96x54:r=25']
        subprocess.run(common+['-f','lavfi','-i','sine=frequency=440:sample_rate=16000','-t','0.6',
            '-c:v','libx264','-threads','1','-pix_fmt','yuv420p','-c:a','aac','-b:a','32k',
            '-movflags','+faststart',str(cls.av)],check=True,capture_output=True,timeout=30)
        subprocess.run(common+['-t','0.6','-c:v','libx264','-threads','1','-pix_fmt','yuv420p',
            '-an','-movflags','+faststart',str(cls.video)],check=True,capture_output=True,timeout=30)

    @classmethod
    def tearDownClass(cls):cls.media_temp.cleanup()

    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve()
        self.directory=acquisition.directory(self.root,JID,URL)
        self.directory.mkdir(parents=True)
        self.final=self.directory/'source.mp4'
        self.receipt=self.root/'inputs'/JID/'source_acquisition.json'
        self.config=self.root/'config/local.json';self.config.parent.mkdir()
        self.config.write_text('{}')

    def publish_av(self):
        shutil.copyfile(self.av,self.final)
        return acquisition.finalize(self.root,JID,URL)

    def source_receipt(self):return json.loads(self.receipt.read_text())

    def write_receipt(self,value):self.receipt.write_text(json.dumps(value,ensure_ascii=False))

    def flow(self):
        return pipeline.Flow(self.root,{'id':JID,'source_url':URL,'options':{}},
            lambda *args:None,None,lambda:False)

    def install_fake_tool(self):
        tool=self.root/'tools/yt-dlp';tool.parent.mkdir();tool.write_text('never executed')
        return tool

    def test_actual_complete_double_stream_media_url_sha_receipt_and_idempotent_reuse(self):
        actual=self.publish_av()
        self.assertEqual(actual,self.final)
        receipt=self.source_receipt()
        self.assertEqual(receipt['job'],JID)
        self.assertEqual(receipt['request'],acquisition.request_binding(URL))
        self.assertEqual(receipt['source']['sha256'],acquisition._sha(self.final))
        self.assertEqual(receipt['source']['bytes'],self.final.stat().st_size)
        self.assertEqual(receipt['probe']['video_streams'],1)
        self.assertEqual(receipt['probe']['audio_streams'],1)
        self.assertFalse(receipt['partial_streams_used'])
        before=self.receipt.read_bytes()
        self.assertEqual(acquisition.read_final_source(self.root,JID,URL),self.final)
        self.assertEqual(acquisition.finalize(self.root,JID,URL),self.final)
        self.assertEqual(self.receipt.read_bytes(),before)

    def test_video_only_and_partial_names_are_never_final_sources_and_not_deleted(self):
        paths=[]
        for name in ('source.f123.mp4','source.mp4.part'):
            path=self.directory/name;shutil.copyfile(self.video,path);paths.append(path)
        before={path:path.read_bytes() for path in paths}
        with self.assertRaises(ValueError):acquisition.finalize(self.root,JID,URL)
        shutil.copyfile(self.video,self.final)
        with self.assertRaises(ValueError):acquisition.finalize(self.root,JID,URL)
        self.assertFalse(self.receipt.exists())
        for path,data in before.items():self.assertEqual(path.read_bytes(),data)

    def test_multiple_final_sources_are_rejected_before_publishing_receipt(self):
        shutil.copyfile(self.av,self.final)
        other=self.directory/'source.mkv';shutil.copyfile(self.av,other)
        with self.assertRaises(ValueError):acquisition.finalize(self.root,JID,URL)
        self.assertTrue(self.final.exists());self.assertTrue(other.exists())
        self.assertFalse(self.receipt.exists())

    def test_receipt_url_job_path_hash_probe_and_schema_forgery_rejected(self):
        self.publish_av();baseline=self.source_receipt()
        other=self.root/'inputs'/'web_20990101_120000_00000002'/'source.mp4'
        other.parent.mkdir(parents=True);shutil.copyfile(self.av,other)
        for mutation in ('url','job','cross_job','sha','bytes','probe','schema'):
            value=copy.deepcopy(baseline)
            if mutation=='url':value['request']=acquisition.request_binding(URL+'&changed=yes')
            elif mutation=='job':value['job']='web_20990101_120000_00000002'
            elif mutation=='cross_job':value['source']['path']=str(other.relative_to(self.root))
            elif mutation=='sha':value['source']['sha256']='0'*64
            elif mutation=='bytes':value['source']['bytes']+=1
            elif mutation=='probe':value['probe']['audio_streams']=2
            else:value['schema']='forged receipt'
            self.write_receipt(value)
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):
                acquisition.read_final_source(self.root,JID,URL)
        self.write_receipt(baseline)
        with self.assertRaises(ValueError):acquisition.read_final_source(self.root,JID,URL+'&edition=two')

    def test_final_file_and_receipt_symlinks_rejected(self):
        self.final.symlink_to(self.av)
        with self.assertRaises(ValueError):acquisition.finalize(self.root,JID,URL)
        self.final.unlink();self.publish_av()
        original=self.receipt.with_name('receipt.original.json');self.receipt.rename(original)
        self.receipt.symlink_to(original)
        with self.assertRaises(ValueError):acquisition.read_final_source(self.root,JID,URL)

    def test_successful_download_flow_ignores_legacy_intermediate_and_retains_partial(self):
        self.install_fake_tool()
        old=self.root/'inputs'/JID/'source.f123.mp4';shutil.copyfile(self.video,old)
        partial=self.directory/'source.mp4.part';partial.write_bytes(b'old partial fixture')
        calls=[]
        def command(*args,**kwargs):
            calls.append(args[3]);shutil.copyfile(self.av,self.final)
            return {'returncode':0}
        flow=self.flow();flow.command=command
        with patch.object(pipeline.shutil,'disk_usage',return_value=SimpleNamespace(free=20*1024**3)):
            result=flow.acquire_source()
        self.assertEqual(result,self.final)
        self.assertEqual(len(calls),1)
        strings=[str(x) for x in calls[0]]
        self.assertIn(str(self.directory/'source.%(ext)s'),strings)
        self.assertEqual(strings[-2:],['--',URL])
        self.assertTrue(old.exists());self.assertEqual(partial.read_bytes(),b'old partial fixture')

    def test_cached_verified_source_does_not_need_downloader_to_still_be_installed(self):
        self.publish_av()
        flow=self.flow();flow.command=lambda *args,**kwargs: self.fail('verified cache must not redownload')
        with patch.object(pipeline.shutil,'which',return_value=None):
            self.assertEqual(flow.acquire_source(),self.final)

    def test_flow_does_not_call_downloader_for_wrong_url_receipt(self):
        self.publish_av();self.install_fake_tool()
        flow=self.flow();flow.job['source_url']=URL+'&changed=yes'
        flow.command=lambda *args,**kwargs:self.fail('old receipt must not silently be replaced')
        with self.assertRaises(ValueError):flow.acquire_source()
        self.assertTrue(self.final.exists())

    def test_truncated_real_faststart_mp4_is_not_a_complete_download(self):
        self.final.write_bytes(self.av.read_bytes()[:-600])
        # This fixture retains both stream descriptions and positive duration;
        # complete-media acceptance must also reject the damaged packet tail.
        with self.assertRaises((ValueError,subprocess.CalledProcessError)):
            acquisition.finalize(self.root,JID,URL)
        self.assertFalse(self.receipt.exists())

    def test_real_completed_url_source_binds_once_and_cannot_be_downgraded_to_download(self):
        job={'id':JID,'source_url':URL,'options':{'restart_policy':takeover.POLICY,
            'website_recovery':{'enabled':True}}}
        takeover.authorize_source_request(self.root,job)
        self.publish_av()
        self.assertTrue(takeover.bind_source(self.root,job))
        self.assertEqual(takeover.validate_source(self.root,job)['sha256'],acquisition._sha(self.final))
        self.assertEqual(job['takeover']['phase'],'bound_source')
        source_before=self.final.read_bytes();receipt_before=self.receipt.read_bytes()
        job['takeover'].pop('source_binding')
        job['takeover'].update(phase='acquiring_source',source_ever_bound=False)
        job.update(status='running',progress=1.,stage='伪装首次下载')
        self.assertFalse(takeover.prepare_takeover(self.root,job,timestamp=0))
        self.assertEqual(self.final.read_bytes(),source_before)
        self.assertEqual(self.receipt.read_bytes(),receipt_before)


if __name__=='__main__':unittest.main()
