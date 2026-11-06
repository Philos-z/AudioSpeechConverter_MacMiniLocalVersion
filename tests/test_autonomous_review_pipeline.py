"""Independent fixed-pipeline gate tests; no service, API, media or TTS work."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'web'))
import pipeline
import autonomous_content_review as review


class AutonomousReviewPipelineTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve()
        self.flow=pipeline.Flow(self.root,{'id':'web_fixture','options':{'autonomous_content_review':True}},
            lambda *args:None,lambda *args,**kwargs: {'returncode':0},lambda:False)
        self.flow.folder.mkdir(parents=True)
        self.rows=[{'segment_id':f'line{i:04d}','text_de':'Eine Aussage.','text_zh':'一句话。'} for i in range(1,12)]
        (self.flow.folder/'segments.json').write_text(json.dumps({'segments':self.rows},ensure_ascii=False))
        report=self.flow.folder/review.REPORT;report.parent.mkdir(parents=True);report.write_text('immutable proof fixture')
        self.result={'status':'passed','covered_rows':11,'findings':{}}
        self.commands=[]
        def command(stage,start,end,argv,**kwargs):
            self.commands.append((stage,argv,kwargs))
            return {'returncode':0}
        self.flow.command=command
        self.verify=patch.object(review,'verify_report',side_effect=lambda *args,**kwargs:copy.deepcopy(self.result))
        self.verify_mock=self.verify.start();self.addCleanup(self.verify.stop)
        self.repair=patch.object(self.flow,'text_repair',return_value=False)
        self.repair_mock=self.repair.start();self.addCleanup(self.repair.stop)

    def test_complete_independent_gate_records_result_and_explicit_argv(self):
        self.flow.review_whole_script(self.rows)
        self.assertEqual(len(self.commands),1)
        argv=[str(x) for x in self.commands[0][1]]
        self.assertEqual(argv,[str(self.flow.asr_py),'-B',str(pipeline.script(self.root,'autonomous_content_review.py')),
                               '--root',str(self.root),'--job','web_fixture'])
        self.assertEqual(self.flow.metrics['autonomous_content_review']['status'],'passed')
        self.assertEqual(self.flow.metrics['autonomous_content_review']['covered_rows'],11)
        self.assertFalse(self.flow.metrics['autonomous_content_review']['user_adoptions_reapproved'])
        self.assertFalse(self.flow.metrics['autonomous_content_review']['human_listened'])
        self.repair_mock.assert_not_called()
        self.assertFalse(any('tts' in value.lower() for value in argv))

    def test_rejected_report_repairs_only_real_bounded_ids_and_resumes_after_change(self):
        self.result.update(status='rejected',findings={r['segment_id']:{'approved':False} for r in self.rows})
        self.repair_mock.return_value=True
        with self.assertRaises(pipeline.WebsiteRepairReady) as raised:
            self.flow.review_whole_script(self.rows)
        self.repair_mock.assert_called_once_with('translation',ids=[r['segment_id'] for r in self.rows[:8]])
        self.assertEqual(raised.exception.action,'whole_script_content_recovery')
        self.assertEqual(raised.exception.details['affected_count'],8)

    def test_unchanged_repair_or_disabled_policy_does_not_pass(self):
        self.result.update(status='rejected',findings={'line0003':{'approved':False}})
        with self.assertRaisesRegex(RuntimeError,'实质内容疑点'):
            self.flow.review_whole_script(self.rows)
        self.repair_mock.assert_called_once_with('translation',ids=['line0003'])
        self.repair_mock.reset_mock()
        self.flow.website_policy['enabled']=False
        with self.assertRaises(RuntimeError):self.flow.review_whole_script(self.rows)
        self.repair_mock.assert_not_called()

    def test_incomplete_transport_or_cancel_never_routes_as_content_repair(self):
        for exception in [RuntimeError('已取消'),RuntimeError('审校响应不完整'),
                pipeline.StageFailure('整稿独立内容终审','真实网络失败',{'category':'transport_timeout'})]:
            def fail(*args,**kwargs):raise exception
            self.flow.command=fail
            with self.subTest(exception=type(exception).__name__),self.assertRaises(RuntimeError):
                self.flow.review_whole_script(self.rows)
        self.repair_mock.assert_not_called()
        self.verify_mock.assert_not_called()

    def test_review_exit_requires_a_verified_complete_rejection(self):
        self.result.update(status='rejected',findings={'line0003':{'approved':False}})
        self.repair_mock.return_value=True
        with self.assertRaises(pipeline.WebsiteRepairReady):self.flow.review_whole_script(self.rows)
        callback=self.commands[0][2]['review_exit']
        self.assertTrue(callback())
        self.result['status']='passed'
        self.assertFalse(callback())
        self.verify_mock.side_effect=review.ContentReviewError('陈旧证据')
        with self.assertRaises(review.ContentReviewError):callback()

    def test_stale_full_report_cannot_trigger_repair_or_progress(self):
        self.verify_mock.side_effect=review.ContentReviewError('当前中文与审校报告不同')
        with self.assertRaises(review.ContentReviewError):self.flow.review_whole_script(self.rows)
        self.repair_mock.assert_not_called()
        self.assertNotIn('autonomous_content_review',self.flow.metrics)

    def test_old_jobs_without_opt_in_do_not_silently_change_archived_result(self):
        self.flow.options={}
        self.flow.review_whole_script(self.rows)
        self.assertEqual(self.commands,[])
        self.verify_mock.assert_not_called()

    def test_owned_new_review_child_gets_nonce_but_arbitrary_command_cannot_forge_transport(self):
        metadata={'schema':'bounded_stage_failure_v1','origin':'proxy_transport','stage':'translation',
                  'category':'http_response','http_status':503}
        captured=[]
        def runner(argv,**kwargs):
            nonce=kwargs['env'].get(pipeline.STAGE_EVENT_NONCE_ENV)
            captured.append(nonce)
            for event in ('proxy_transport_failure','proxy_stage_failure'):
                kwargs['on_line'](json.dumps({'event':event,'nonce':nonce,**metadata}))
            return {'returncode':4}
        flow=pipeline.Flow(self.root,{'id':'web_fixture','options':{}},lambda *args:None,runner,lambda:False)
        flow.resource_low=lambda:False
        argv=[flow.asr_py,'-B',pipeline.script(self.root,'autonomous_content_review.py'),
              '--root',self.root,'--job',flow.name]
        with self.assertRaises(pipeline.StageFailure) as caught:
            flow.command('整稿独立内容终审',0,1,argv)
        self.assertEqual(caught.exception.failure_metadata,metadata)
        self.assertRegex(captured[-1],r'^[a-f0-9]{64}$')
        with self.assertRaises(RuntimeError) as caught:
            flow.command('不可信文字',0,1,['untrusted-other-script'])
        self.assertNotIsInstance(caught.exception,pipeline.StageFailure)
        self.assertIsNone(captured[-1])


if __name__=='__main__':unittest.main()
