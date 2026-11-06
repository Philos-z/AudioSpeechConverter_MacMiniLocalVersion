"""Real fixed controller integration with a virtual clock and no child/API."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'web'))
import pipeline
import recovery


class ProxyBackoffIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve()
        self.job={'id':'web_backoff_fixture','options':{'autonomy_policy':'evidence_first_v1'}}
        self.folder=self.root/'jobs'/self.job['id'];self.folder.mkdir(parents=True)
        self.clock=0.;self.sleeps=[];self.stage_calls=0;self.decision_calls=0
        self.cancel_at=None;self.failures=3

    def cancelled(self):
        return self.cancel_at is not None and self.clock>=self.cancel_at

    def sleep(self,seconds):
        self.sleeps.append(seconds);self.clock+=seconds

    def stages(self,flow):
        self.stage_calls+=1
        flow.check()
        if self.stage_calls>self.failures:return {'metrics':flow.metrics}
        metadata={'schema':'bounded_stage_failure_v1','origin':'proxy_transport','stage':'translation',
                  'category':'http_response','http_status':(500,503,504)[self.stage_calls-1]}
        raise pipeline.StageFailure('整稿独立内容终审','Owned offline stage failure',metadata)

    def command(self,flow,stage,start,end,argv,**kwargs):
        self.decision_calls+=1
        words=[str(word) for word in argv]
        self.assertEqual(words[3],'decide')
        packet=json.loads(Path(words[words.index('--packet')+1]).read_text())
        decision=recovery.RecoveryController(self.root,self.job['id']).deterministic_decision(
            packet['stage'],RuntimeError('Owned offline stage failure'),packet['failure_metadata'])
        destination=Path(words[words.index('--out')+1])
        destination.write_text(json.dumps(decision))
        self.clock+=.125
        return {'returncode':0}

    def run_flow(self,deadline=14400.):
        owner=self
        with patch.object(pipeline.time,'perf_counter',side_effect=lambda:owner.clock),\
             patch.object(pipeline.time,'sleep',side_effect=self.sleep),\
             patch.object(pipeline.Flow,'resource_low',return_value=False),\
             patch.object(pipeline.Flow,'stages',lambda flow:owner.stages(flow)),\
             patch.object(pipeline.Flow,'command',lambda flow,*args,**kw:owner.command(flow,*args,**kw)),\
             patch.object(recovery.RecoveryController,'_client',side_effect=AssertionError('No Proxy permitted')):
            return pipeline._run_with_recovery(self.root,self.job,lambda *args:None,
                lambda *args,**kw: (_ for _ in ()).throw(AssertionError('No child permitted')),
                self.cancelled,0.,0.,deadline=deadline)

    def saved(self):
        return json.loads((self.folder/'web_pipeline_metrics.json').read_text())

    def journal(self):
        return json.loads(recovery.RecoveryController(self.root,self.job['id']).path.read_text())

    def test_initial_plus_two_network_retries_never_launches_fourth_stage(self):
        with self.assertRaises(pipeline.StageFailure):self.run_flow()
        self.assertEqual(self.stage_calls,3)
        self.assertEqual(self.decision_calls,3)
        self.assertAlmostEqual(sum(self.sleeps),40.)
        journal=self.journal()
        self.assertEqual(journal['attempts_consumed'],2)
        self.assertEqual([item['outcome'] for item in journal['attempts']],['failed','failed'])
        saved=self.saved()
        self.assertAlmostEqual(saved['recovery']['advice_wall_seconds'],40.375)
        self.assertAlmostEqual(saved['stage_wall_s']['固定恢复策略'],40.375)
        self.assertEqual(saved['status'],'failed')

    def test_cancel_during_wait_spends_reservation_and_never_launches_retry(self):
        self.cancel_at=1.
        with self.assertRaisesRegex(RuntimeError,'已取消'):self.run_flow()
        self.assertEqual(self.stage_calls,1)
        self.assertEqual(self.decision_calls,1)
        self.assertEqual(self.journal()['attempts'][0]['outcome'],'cancelled')
        saved=self.saved()
        self.assertEqual(saved['status'],'cancelled')
        self.assertEqual(saved['recovery']['records'][0]['status'],'cancelled')
        self.assertAlmostEqual(saved['recovery']['advice_wall_seconds'],self.clock)
        self.assertGreater(saved['recovery']['advice_wall_seconds'],1.)

    def test_deadline_during_wait_spends_reservation_and_counts_wait(self):
        with self.assertRaisesRegex(RuntimeError,'4小时'):self.run_flow(deadline=1.)
        self.assertEqual(self.stage_calls,1)
        self.assertEqual(self.journal()['attempts'][0]['outcome'],'stopped')
        saved=self.saved()
        self.assertEqual(saved['status'],'failed')
        self.assertEqual(saved['recovery']['records'][0]['status'],'backoff_stopped')
        self.assertAlmostEqual(saved['recovery']['advice_wall_seconds'],self.clock)
        self.assertGreater(saved['recovery']['advice_wall_seconds'],1.)

    def test_success_after_first_retry_preserves_complete_wait_metrics(self):
        self.failures=1
        result=self.run_flow()
        self.assertEqual(self.stage_calls,2)
        self.assertAlmostEqual(sum(self.sleeps),10.)
        self.assertEqual(self.journal()['attempts'][0]['outcome'],'completed')
        self.assertEqual(result['metrics']['status'],'complete')
        self.assertAlmostEqual(result['metrics']['recovery']['advice_wall_seconds'],10.125)


if __name__=='__main__':unittest.main()
