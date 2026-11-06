"""Outages cool down without new retry rights; cancellation stays responsive."""
from pathlib import Path
import sys,unittest
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'web'))
import pipeline

class RetryBackoffTests(unittest.TestCase):
    def flow(self,check=lambda:None):
        events=[]
        return SimpleNamespace(options={'autonomy_policy':'evidence_first_v1'},
            progress=58,check=check,emit=lambda *x:events.append(x),events=events)

    def test_two_existing_attempts_wait_ten_then_thirty_and_check_while_waiting(self):
        for attempt,expected in ((1,10),(2,30)):
            clock=[0.];checks=[];sleeps=[]
            def sleep(seconds):sleeps.append(seconds);clock[0]+=seconds
            flow=self.flow(lambda:checks.append(clock[0]))
            with patch.object(pipeline.time,'perf_counter',side_effect=lambda:clock[0]),patch.object(pipeline.time,'sleep',side_effect=sleep):
                pipeline.proxy_retry_backoff(flow,attempt)
            self.assertAlmostEqual(sum(sleeps),expected)
            self.assertLessEqual(max(sleeps),.2)
            self.assertGreater(len(checks),expected)
            self.assertFalse(flow.events[0][2]['new_retry_budget_granted'])

    def test_cancellation_or_deadline_stops_wait_before_retry(self):
        for error in (RuntimeError('任务已取消'),TimeoutError('deadline')):
            checks=[0];clock=[0.]
            def check():
                checks[0]+=1
                if checks[0]>=4:raise error
            flow=self.flow(check)
            with patch.object(pipeline.time,'perf_counter',side_effect=lambda:clock[0]),patch.object(pipeline.time,'sleep',side_effect=lambda x:clock.__setitem__(0,clock[0]+x)):
                with self.assertRaises(type(error)):pipeline.proxy_retry_backoff(flow,1)
            self.assertLess(clock[0],1.)

    def test_no_third_attempt_or_boolean_can_acquire_waiting_authority(self):
        for attempt in (0,3,True,None):
            with self.assertRaises(ValueError):pipeline.proxy_retry_backoff(self.flow(),attempt)

    def test_legacy_jobs_keep_existing_policy(self):
        flow=self.flow();flow.options={}
        with patch.object(pipeline.time,'sleep',side_effect=AssertionError('legacy must not wait')):
            pipeline.proxy_retry_backoff(flow,1)
        self.assertFalse(flow.events)

if __name__=='__main__':unittest.main()
