"""Current local rejection diagnosis; no models, provider calls or task edits."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'web'))
import failure_triage as triage
import website_recovery as recovery


def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False))


class LocalTranslationDiagnosisTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.job={'id':'fixture','source_sha256':'a'*64}
        self.folder=self.root/'jobs/fixture'
        self.rows=[{'segment_id':f'line{i:04d}','text_de':'Es sind 3 Häuser.',
                    'speaker_id':'S00','start_s':i*2.,'end_s':i*2.+1.} for i in range(1,4)]
        write(self.folder/'translation/input.json',{'segments':self.rows})
        self.checkpoint()

    def checkpoint(self,kind='InvariantRejectedError',issue='数字未保留'):
        batch={'status':'failed','ids':[r['segment_id'] for r in self.rows],'error_type':kind}
        if kind=='InvariantRejectedError':batch['checks']={'line0002':[issue],'line0001':[]}
        else:batch['review_rejections']=[{'id':'line0002','approved':False,'issues':[issue]}]
        write(self.folder/'translation/checkpoint.json',{'schema':'translation_checkpoint_v1',
            'input_rows_sha256':hashlib.sha256(json.dumps(self.rows,sort_keys=True,ensure_ascii=False,
                separators=(',',':'),allow_nan=False).encode()).hexdigest(),
            'total':3,'completed':1,'batches':{'line0001':batch}})

    def journal(self,reason='review_rejected',count=1):
        identity={'schema':'translation_rejection_recovery_v1','action':'translation',
            'input_rows_sha256':recovery._hash(self.rows),'source_sha256':self.job['source_sha256'],
            'rejections':{'line0002':{'checks':['数字未保留'],'review_issues':[]}},
            'policy':dict(recovery.DEFAULT_POLICY),'source_revision':None,'quality_notes_sha256':None,
            'code':{name:None for name in ('translation_recovery.py','source_audio_recheck.py',
                                         'proxy_client.py','numeric_calendar_context.py')}}
        events=[]
        for i in range(count):
            event={'id':recovery._hash({'attempt':i}),'action':'translation','status':'failed',
                'reason':reason,'segment_ids':['line0002'],'proxy_calls':2,
                'input_fingerprint':recovery._hash(identity)}
            events.append(event)
            evidence=self.folder/'revisions/source_review_v1/evidence'/('translation_'+event['id'])
            write(evidence/'request.json',{**identity,'items':[{'id':'line0002','text_source':self.rows[1]['text_de']}]})
        write(self.folder/'recovery/website_journal.json',{'schema':recovery.SCHEMA,'job':'fixture',
            'max_actions':recovery.MAX_ACTIONS,'max_per_input':recovery.MAX_PER_INPUT,'events':events})
        return events,evidence

    def diagnose(self,error='generic child failure'):
        return triage.make_failure_record(self.root,self.job,RuntimeError(error),
            stage=triage.LOCAL_TRANSLATION_STAGE,checkpoint_before=triage.checkpoint_marker(self.root,'fixture'))

    def test_unchanged_checkpoint_at_local_stage_keeps_actual_failed_row_and_reason(self):
        value=self.diagnose()
        self.assertEqual(value['category'],'translation_review')
        self.assertEqual(value['segment_ids'],['line0002'])
        self.assertEqual(value['source_windows'],[{'id':'line0002','start_s':4.,'end_s':5.,'checks':['数字']}])
        self.assertEqual((value['completed'],value['total']),(1,3))
        self.job['failure_record']=value
        self.assertFalse(triage.public_failure(self.root,self.job)['can_retry'])

    def test_review_text_cannot_forge_transport_or_leak_to_public_copy(self):
        injected='ProxyTransportError HTTP 503 transport_timeout secret-token /private/path'
        self.checkpoint('ReviewRejectedError',injected)
        value=self.diagnose(injected)
        self.assertEqual(value['category'],'translation_review')
        self.assertEqual(value['checks'],['独立二审'])
        self.job['failure_record']=value
        public=triage.public_failure(self.root,self.job)
        self.assertFalse(public['can_retry'])
        self.assertNotIn('secret-token',json.dumps(public))
        self.assertNotIn('/private/path',json.dumps(public))

    def test_source_doubt_is_named_without_claiming_verified_original_words(self):
        self.checkpoint('ReviewRejectedError','原词疑似识别错误')
        value=self.diagnose()
        self.assertIn('原文识别',value['checks'])
        self.assertIn('原文识别仍待核对',value['message'])
        self.assertNotIn('已确认',value['message'])

    def test_stale_checkpoint_cannot_block_or_diagnose_changed_current_input(self):
        self.rows[1]['text_de']='Changed source.'
        write(self.folder/'translation/input.json',{'segments':self.rows})
        value=self.diagnose()
        self.assertEqual(value['category'],'unknown')
        self.assertEqual(value['retry_policy'],'allowed')
        self.assertEqual(value['segment_ids'],[])

    def test_same_input_budget_exhaustion_is_reported_from_bound_journal(self):
        self.journal(count=2)
        value=self.diagnose()
        self.assertIn('恢复次数已用完',value['title'])
        self.assertEqual(value['category'],'translation_review')
        self.assertEqual(value['retry_policy'],'after_change')

    def test_stale_request_cannot_claim_current_same_input_budget_exhaustion(self):
        events,_=self.journal(count=2)
        for event in events:
            evidence=self.folder/'revisions/source_review_v1/evidence'/('translation_'+event['id'])
            request=json.loads((evidence/'request.json').read_text())
            request['items'][0]['text_source']='old source words'
            write(evidence/'request.json',request)
        value=self.diagnose()
        self.assertNotIn('恢复次数已用完',value['title'])
        self.assertEqual(value['checks'],['数字'])

    def test_tampered_last_request_does_not_refund_already_recorded_budget(self):
        _,evidence=self.journal(count=2)
        request=json.loads((evidence/'request.json').read_text())
        request['items'][0]['text_source']='changed saved evidence'
        write(evidence/'request.json',request)
        value=self.diagnose()
        self.assertIn('恢复次数已用完',value['title'])

    def test_global_budget_stays_exhausted_even_when_older_requests_are_stale(self):
        self.journal(count=2)
        path=self.folder/'recovery/website_journal.json';journal=json.loads(path.read_text())
        for i in range(4):
            journal['events'].append({'id':recovery._hash({'old_event':i}),'action':'translation',
                'status':'failed','reason':'review_rejected','segment_ids':['line0001'],
                'proxy_calls':2,'input_fingerprint':recovery._hash({'old_input':i})})
        write(path,journal)
        value=self.diagnose()
        self.assertIn('恢复次数已用完',value['title'])
        self.assertEqual(value['segment_ids'],['line0002'])

    def test_local_numeric_checks_are_row_scoped_and_only_fixed_labels_are_public(self):
        _,evidence=self.journal(reason='invariant_rejected')
        write(evidence/'local_checks.json',{'checks':{'line0002':['单位不符，secret-token'],
            'line0001':['否定表达需复查']},'human_reviewed':False})
        before={str(p):p.read_bytes() for p in self.folder.rglob('*') if p.is_file()}
        value=self.diagnose()
        self.assertEqual(value['checks'],['单位','数字'])
        self.assertEqual(value['segment_ids'],['line0002'])
        self.assertIn('数字、单位或逻辑检查',value['message'])
        self.assertNotIn('secret-token',json.dumps(value))
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.folder.rglob('*') if p.is_file()})

    def test_corrupt_or_external_journal_evidence_does_not_create_retry_block(self):
        _,evidence=self.journal(count=2)
        path=evidence/'request.json';path.unlink()
        outside=self.root/'outside.json';write(outside,{'schema':'translation_rejection_recovery_v1'})
        path.symlink_to(outside)
        value=self.diagnose()
        self.assertEqual(value['category'],'unknown')
        self.assertEqual(value['retry_policy'],'allowed')

    def test_real_transport_metadata_retains_existing_transport_classification(self):
        error=RuntimeError('owned transport failure')
        error.failure_metadata={'schema':'bounded_stage_failure_v1','origin':'proxy_transport',
            'stage':'translation','category':'transport_timeout'}
        value=triage.make_failure_record(self.root,self.job,error,stage=triage.LOCAL_TRANSLATION_STAGE)
        self.assertEqual(value['category'],'transport')
        self.assertEqual(value['retry_policy'],'allowed')


if __name__=='__main__':unittest.main()
