"""Bounded orchestration, cache dependencies and resource cancellation."""
import ast
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'web'))
import pipeline


class PipelineWebsiteTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.events=[]

    def test_orchestration_only_changes_keep_speaker_and_background_identity(self):
        before=ROOT/'web/pipeline.py';after=self.root/'pipeline.py'
        tree=ast.parse(before.read_text())
        flow=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Flow')
        flow.body.append(ast.parse('def new_ui_hook(self): return "new orchestration"').body[0])
        after.write_text(ast.unparse(tree))
        self.assertEqual(pipeline.speaker_implementation_signature(before),pipeline.speaker_implementation_signature(after))
        self.assertEqual(pipeline.implementation_signature(('background_job',),before),
                         pipeline.implementation_signature(('background_job',),after))

    def test_real_background_change_invalidates_only_background_identity(self):
        before=ROOT/'web/pipeline.py';after=self.root/'pipeline.py'
        tree=ast.parse(before.read_text())
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='background_job')
        fn.body.append(ast.parse('changed_recipe = True').body[0]);after.write_text(ast.unparse(tree))
        self.assertEqual(pipeline.speaker_implementation_signature(before),pipeline.speaker_implementation_signature(after))
        self.assertNotEqual(pipeline.implementation_signature(('background_job',),before),
                            pipeline.implementation_signature(('background_job',),after))

    def fake_flow(self,failures):
        owner=self
        class FakeFlow:
            calls=0
            def __init__(self,*args):self.metrics={'stage_wall_s':{},'stage_cache_hits':['translation']}
            def stages(self):
                type(self).calls+=1
                if type(self).calls<=failures:
                    raise pipeline.WebsiteRepairReady('scoped_fixture',{'private_text':'must not enter public events'})
                return {'metrics':self.metrics,'artifacts':[]}
        return FakeFlow

    def run_fake(self,failures,cancelled=False):
        fake=self.fake_flow(failures)
        with patch.object(pipeline,'Flow',fake):
            result=pipeline._run_with_recovery(self.root,{'id':'web_bound_test'},
                lambda *args:self.events.append(args),lambda *a,**kw:None,lambda:cancelled,
                pipeline.time.perf_counter(),0)
        return fake,result

    def test_fixed_local_data_actions_continue_through_same_runner(self):
        fake,result=self.run_fake(2)
        self.assertEqual(fake.calls,3)
        self.assertEqual(len(result['metrics']['recovery']['website_data_repairs']),2)
        self.assertTrue(all('private_text' not in json.dumps(e) for e in self.events))
        proof=json.loads((self.root/'jobs/web_bound_test/web_pipeline_metrics.json').read_text())
        self.assertEqual(proof['status'],'complete')

    def test_data_action_loop_is_bounded(self):
        with self.assertRaisesRegex(RuntimeError,'次数预算'):
            self.run_fake(100)
        proof=json.loads((self.root/'jobs/web_bound_test/web_pipeline_metrics.json').read_text())
        self.assertEqual(proof['status'],'failed')
        self.assertEqual(len(proof['recovery']['website_data_repairs']),6)

    def test_cancelled_action_never_resumes(self):
        fake=self.fake_flow(100)
        with patch.object(pipeline,'Flow',fake),self.assertRaises(pipeline.WebsiteRepairReady):
            pipeline._run_with_recovery(self.root,{'id':'web_cancel_test'},lambda *a:None,
                lambda *a,**kw:None,lambda:True,pipeline.time.perf_counter(),0)
        self.assertEqual(fake.calls,1)

    def flow(self,runner=lambda *a,**kw:None):
        return pipeline.Flow(self.root,{'id':'web_resource_test','options':{'website_recovery':{
            'enabled':True,'source_mode':'contextual','picture_holds':True,'reserve_gib':2}}},
            lambda *a:None,runner,lambda:False)

    def test_single_task_reserve_does_not_modify_global_configuration(self):
        config={'minimum_free_gib':8};flow=self.flow()
        self.assertEqual(flow.space_reserve(config),2)
        self.assertEqual(config,{'minimum_free_gib':8})
        self.assertFalse((self.root/'config').exists())

    def test_runtime_disk_floor_stops_before_starting_child(self):
        calls=[];flow=self.flow(lambda *a,**kw:calls.append(a))
        with patch.object(pipeline.shutil,'disk_usage',return_value=types.SimpleNamespace(free=1024**3)):
            with self.assertRaisesRegex(RuntimeError,'空间不足'):flow.command('fixture',0,1,['never-run'])
        self.assertFalse(calls)

    def test_command_receives_shared_time_budget_and_live_resource_predicate(self):
        calls=[]
        def runner(argv,**kwargs):
            calls.append(kwargs);self.assertFalse(kwargs['should_abort']())
            return {'returncode':0,'stdout':''}
        flow=self.flow(runner);flow.deadline=pipeline.time.perf_counter()+30
        flow.command('fixture',0,1,['not-a-model'],timeout=600)
        self.assertLessEqual(calls[0]['timeout'],30)

    def test_only_owned_text_repair_can_authorize_transport_recovery(self):
        def runner(argv,**kwargs):
            nonce=kwargs['env'][pipeline.STAGE_EVENT_NONCE_ENV]
            meta={'schema':'bounded_stage_failure_v1','origin':'proxy_transport',
                'stage':'translation','category':'http_response','http_status':503}
            for event in ('proxy_transport_failure','proxy_stage_failure'):
                kwargs['on_line'](json.dumps({**meta,'event':event,'nonce':nonce}))
            return {'returncode':1,'stdout':''}
        flow=self.flow(runner)
        # script lookup requires the owned helper, not a user-selected program.
        path=self.root/'web/website_recovery.py';path.parent.mkdir();path.write_text('fixture')
        with self.assertRaises(pipeline.StageFailure) as caught:flow.text_repair('source')
        import recovery
        self.assertTrue(recovery.classify_error(caught.exception.stage,caught.exception,
            caught.exception.failure_metadata)['retryable'])
        self.assertIsNone(flow._owned_website_text_argv)

    def test_unowned_child_cannot_claim_proxy_recovery(self):
        def runner(argv,**kwargs):
            self.assertNotIn(pipeline.STAGE_EVENT_NONCE_ENV,kwargs['env'])
            kwargs['on_line'](json.dumps({'event':'proxy_stage_failure','nonce':'a'*64,
                'schema':'bounded_stage_failure_v1','origin':'proxy_transport','stage':'translation',
                'category':'http_response','http_status':503}))
            return {'returncode':1,'stdout':''}
        flow=self.flow(runner)
        with self.assertRaises(RuntimeError) as caught:flow.command('Proxy处理局部译稿并独立复核',0,1,['unowned'])
        self.assertNotIsInstance(caught.exception,pipeline.StageFailure)


if __name__=='__main__':unittest.main()
