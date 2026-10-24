"""Offline tests for bounded source-aware translation recovery; no real API/model."""
import copy
import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'web'))
import pipeline
import translation_recovery as repair
import website_recovery as recovery


def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False))


class TranslationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.jid='web_translation_fixture'
        self.folder=self.root/'jobs'/self.jid;self.folder.mkdir(parents=True)
        self.video=self.root/'input.mp4';self.video.write_bytes(b'fixture original media')
        self.source_sha=hashlib.sha256(self.video.read_bytes()).hexdigest()
        self.rows=[{'segment_id':f'line{i:04d}','text_de':'Es sind 3 Hauser.',
            'text_source':'Es sind 3 Hauser.','speaker_id':'S00','start_s':float(i),
            'end_s':float(i)+.8,'source_language':'de'} for i in range(1,4)]
        write(self.folder/'segments.plan.json',{'segments':self.rows})
        write(self.folder/'translation/input.json',{'segments':self.rows,'source_language':'de'})
        write(self.folder/'source.json',{'input':str(self.video),'duration_s':10,'audio_track':0})
        write(self.root/'web/state/jobs'/f'{self.jid}.json',{'id':self.jid,'source_sha256':self.source_sha})
        self.checkpoint()
        self.calls=[];self.rechecks=[];self.reject=False;self.change_source=False

    def checkpoint(self,sid='line0002'):
        write(self.folder/'translation/checkpoint.json',{'schema':'translation_checkpoint_v1',
            'input_rows_sha256':hashlib.sha256(json.dumps(self.rows,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest(),
            'total':3,'completed':0,'batches':{'line0001':{'status':'failed','ids':[r['segment_id'] for r in self.rows],
            'error_type':'ReviewRejectedError','review_rejections':[{'id':sid,'approved':False,'issues':['原词疑似识别错误']} ]}}})

    def recheck(self,root,folder,rows,evidence_dir,source_sha):
        self.rechecks.append([row['segment_id'] for row in rows])
        self.assertEqual(source_sha,self.source_sha)
        return {row['segment_id']:{'alternatives':{'original':row['text_de'],'exact':'Es sind 3 Häuser.'},
            'human_reviewed':False,'source_words_verified':False,'evidence':[]} for row in rows}

    def client(self,root):
        owner=self
        class FakeProxy:
            def json(self,system,data):
                owner.calls.append(copy.deepcopy(data));audit='draft' in data
                if audit and owner.change_source:owner.video.write_bytes(b'concurrent video edit')
                rows=[]
                for item in data['items']:
                    row={'id':item['id'],'text_zh':'有3座房屋。',
                         'source_choice':'exact' if 'exact' in item['source_alternatives'] else 'original'}
                    if audit:row.update(approved=not owner.reject,issues=['不能确认'] if owner.reject else [])
                    rows.append(row)
                return {'translations':rows}
        return FakeProxy()

    def run_repair(self,**kwargs):
        return repair.run(self.root,self.jid,client_factory=self.client,recheck_factory=self.recheck,**kwargs)

    def test_fresh_job_without_revision_recovers_only_actual_rejected_row(self):
        before=(self.folder/'segments.plan.json').read_bytes()
        result=self.run_repair()
        self.assertEqual(result['status'],'succeeded');self.assertEqual(self.rechecks,[['line0002']])
        self.assertEqual(len(self.calls),2)
        self.assertEqual((self.folder/'segments.plan.json').read_bytes(),before)
        revised,receipt=pipeline.reviewed_source_plan(self.folder,self.folder/'segments.plan.json',self.source_sha)
        self.assertEqual(revised[1]['text_de'],'Es sind 3 Häuser.')
        self.assertEqual(revised[0],self.rows[0]);self.assertEqual(revised[2],self.rows[2])
        self.assertEqual(receipt['unresolved'],['line0002'])
        hints=json.loads((self.folder/'translation/source_review_hints.json').read_text())
        self.assertEqual(set(hints['rows']),{'line0002'})
        journal=json.loads((self.folder/'recovery/website_journal.json').read_text())
        self.assertEqual(journal['events'][0]['proxy_calls'],2)
        self.assertFalse(journal['events'][0]['source_words_verified'])

    def test_rejection_is_not_committed_and_same_input_budget_persists(self):
        self.reject=True
        for _ in range(2):
            with self.assertRaises(recovery.RecoveryReviewRequired):self.run_repair()
        with self.assertRaises(recovery.RecoveryBudgetExhausted):self.run_repair()
        self.assertEqual(len(self.calls),4)
        self.assertFalse((self.folder/'source_plan_revision.json').exists())
        self.assertFalse((self.folder/'translation/source_review_hints.json').exists())

    def test_stale_checkpoint_never_qualifies_for_model_or_proxy_call(self):
        self.rows[0]['text_de']='A changed current input.'
        self.assertEqual(repair.pending(self.folder,self.rows),{})

    def test_explicit_historical_scope_never_expands_to_other_rejections(self):
        self.checkpoint(sid='line0001')
        value=json.loads((self.folder/'translation/checkpoint.json').read_text())
        write(self.folder/'translation/checkpoint_history/old.json',value)
        self.checkpoint(sid='line0002')
        self.assertEqual(list(repair.pending(self.folder,self.rows)),['line0002'])
        self.assertEqual(set(repair.pending(self.folder,self.rows,include_history=True)),{'line0001','line0002'})
        self.run_repair(ids=['line0001'])
        self.assertEqual(self.rechecks,[['line0001']])
        self.assertEqual([[row['id'] for row in data['items']] for data in self.calls],[['line0001'],['line0001']])

    def test_original_asr_rejection_cannot_be_erased_by_later_cache_approval(self):
        from proxy_client import source_binding,VERSION
        row=self.rows[1]
        write(self.folder/'translation/diagnostics/rejected.json',{
            'schema':'proxy_translation_diagnostic_v1','version':VERSION,'stage':'audit','human_reviewed':False,
            'input':{'items':[{'id':row['segment_id'],'text_de':row['text_de'],'source_binding':source_binding(row)}]},
            'response':{'translations':[{'id':row['segment_id'],'approved':False,'issues':['原文识别有疑点']} ]}})
        write(self.folder/'translation/checkpoint.json',{'schema':'translation_checkpoint_v1',
            'input_rows_sha256':hashlib.sha256(json.dumps(self.rows,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest(),
            'total':3,'completed':3,'batches':{}})
        self.assertEqual(repair.source_pending(self.folder,self.rows),['line0002'])
        changed=copy.deepcopy(self.rows);changed[0].update(text_de='Another row changed.',text_source='Another row changed.')
        self.assertEqual(repair.source_pending(self.folder,changed),['line0002'])
        changed[1].update(text_de='The rejected source itself changed.',text_source='The rejected source itself changed.')
        self.assertEqual(repair.source_pending(self.folder,changed),[])

    def test_rechecked_source_hint_closes_only_its_own_pending_rejection(self):
        self.test_original_asr_rejection_cannot_be_erased_by_later_cache_approval()
        text=self.rows[1]['text_de']
        write(self.folder/'translation/source_review_hints.json',{'schema':'source_review_hints_v1','rows':{'line0002':{
            'source_text_sha256':hashlib.sha256(text.encode()).hexdigest(),'note':'已依据本机复核及独立二审修订',
            'required_strings':['有3座房屋。'],'machine_translation_recovery':True}}})
        self.assertEqual(repair.source_pending(self.folder,self.rows),[])

    def test_strict_policy_never_invokes_model_or_proxy(self):
        with self.assertRaises(recovery.RecoveryReviewRequired):self.run_repair(policy={'source_mode':'strict'})
        self.assertFalse(self.calls);self.assertFalse(self.rechecks)

    def test_unknown_source_choice_is_rejected(self):
        value={'translations':[{'id':'line0002','text_zh':'有3座房屋。','source_choice':'invented'}]}
        with self.assertRaises(recovery.RecoveryReviewRequired):repair._response(value,[{'id':'line0002','source_alternatives':{'original':'x'}}])

    def test_concurrent_original_media_change_prevents_commit(self):
        self.change_source=True
        with self.assertRaises(recovery.RecoveryReviewRequired):self.run_repair()
        self.assertFalse((self.folder/'source_plan_revision.json').exists())

    def test_existing_user_text_constraints_are_not_replaced(self):
        text=self.rows[1]['text_de']
        write(self.folder/'translation/source_review_hints.json',{'schema':'source_review_hints_v1','rows':{'line0002':{
            'source_text_sha256':hashlib.sha256(text.encode()).hexdigest(),'note':'用户已核对','required_strings':['必须保留这段文字']}}})
        with self.assertRaises(recovery.RecoveryReviewRequired):self.run_repair()
        self.assertFalse((self.folder/'source_plan_revision.json').exists())
        self.assertEqual(self.calls[0]['items'][0]['source_alternatives'],{'original':text})

    def test_failed_invariant_batch_lists_only_nonempty_checks(self):
        path=self.folder/'translation/checkpoint.json';value=json.loads(path.read_text())
        value['batches']['line0001'].update(error_type='InvariantRejectedError',
            checks={'line0001':[],'line0002':['数字不符'],'line0003':[]},review_rejections=[])
        write(path,value)
        self.assertEqual(list(repair.pending(self.folder,self.rows)),['line0002'])

    def test_source_candidate_cannot_drop_quantities_negation_or_condition(self):
        for original,candidate in [('Es sind 3 Häuser.','Es sind Häuser.'),
            ('Es sind drei Häuser.','Es sind Häuser.'),
            ('Es gibt keine 3 Häuser.','Es gibt 3 Häuser.'),
            ('Wenn es regnet, bleiben wir.','Es regnet, wir bleiben.'),
            ('Es sind mehr als 3 Häuser.','Es sind 3 Häuser.')]:
            with self.subTest(original=original):
                self.assertTrue(repair.source_alternative_errors(original,candidate))
        self.assertEqual(repair.source_alternative_errors('um 22 über 15','um 22.15 Uhr'),[])

    def test_success_never_downgrades_existing_user_constraints(self):
        text=self.rows[1]['text_de']
        hint={'source_text_sha256':hashlib.sha256(text.encode()).hexdigest(),'note':'用户已核对','required_strings':['有3座房屋。']}
        write(self.folder/'translation/source_review_hints.json',{'schema':'source_review_hints_v1','rows':{'line0002':hint}})
        self.run_repair()
        saved=json.loads((self.folder/'translation/source_review_hints.json').read_text())['rows']['line0002']
        self.assertIsNot(saved.get('machine_translation_recovery'),True)
        self.assertEqual(saved['required_strings'],hint['required_strings'])
        self.assertTrue(saved['note'].startswith(hint['note']))
        self.assertFalse((self.folder/'source_plan_revision.json').exists())

    def test_interrupted_apply_revalidates_original_media(self):
        original_atomic=recovery._atomic
        def interrupt(path,data):
            if path==self.folder/'revisions/source_review_v1/segments.plan.json':
                raise KeyboardInterrupt('fixture interruption')
            return original_atomic(path,data)
        with patch.object(recovery,'_atomic',side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):self.run_repair()
        self.video.write_bytes(b'replaced media while process was stopped')
        journal=json.loads((self.folder/'recovery/website_journal.json').read_text())
        with self.assertRaises(recovery.RecoveryReviewRequired):recovery._recover_interrupted(self.folder,journal)
        self.assertEqual(journal['events'][0]['status'],'applying')
        self.assertFalse((self.folder/'source_plan_revision.json').exists())

    def test_recovery_code_fix_changes_retry_fingerprint(self):
        import failure_triage
        job={'id':self.jid,'source_sha256':self.source_sha,'options':{}}
        record=failure_triage.make_failure_record(self.root,job,RuntimeError('content rejection'),
            stage=failure_triage.TRANSLATION_STAGE,checkpoint_before=None)
        job['failure_record']=record
        self.assertFalse(failure_triage.public_failure(self.root,job)['can_retry'])
        path=self.root/'web/translation_recovery.py';path.parent.mkdir(parents=True,exist_ok=True);path.write_text('fixed recovery')
        self.assertTrue(failure_triage.public_failure(self.root,job)['can_retry'])

    def test_pipeline_uses_fixed_repair_then_existing_bounded_outer_loop(self):
        flow=pipeline.Flow(self.root,{'id':self.jid,'options':{'website_recovery':{'enabled':True,'source_mode':'contextual'}}},
            lambda *a:None,lambda *a,**kw:None,lambda:False)
        calls=[]
        def command(*args,**kwargs):
            self.checkpoint();raise RuntimeError('content stopped')
        with patch.object(flow,'command',side_effect=command),patch.object(flow,'text_repair',side_effect=lambda phase:calls.append(phase) or True):
            with self.assertRaises(pipeline.WebsiteRepairReady):flow.translate_with_recovery(self.rows,lambda *a:None)
        self.assertEqual(calls,['translation'])

    def test_pipeline_never_repairs_an_unchanged_stale_failure(self):
        flow=pipeline.Flow(self.root,{'id':self.jid,'options':{'website_recovery':{}}},lambda *a:None,lambda *a,**kw:None,lambda:False)
        with patch.object(flow,'command',side_effect=RuntimeError('unrelated error')),patch.object(flow,'text_repair') as mocked:
            with self.assertRaises(RuntimeError):flow.translate_with_recovery(self.rows,lambda *a:None)
        mocked.assert_not_called()

    def saved_partial_fixture(self,old_stop=True,bad_target=False,whole=False):
        value=json.loads((self.folder/'translation/checkpoint.json').read_text())
        value['batches']['line0001']['review_rejections']=[{'id':r['segment_id'],'approved':False,
            'issues':['原词疑似识别错误']} for r in self.rows]
        write(self.folder/'translation/checkpoint.json',value)
        def recheck(root,folder,rows,evidence_dir,source_sha):
            result=self.recheck(root,folder,rows,evidence_dir,source_sha)
            for row in rows:
                sid=row['segment_id'];clip=evidence_dir/(sid+'_exact.wav');clip.write_bytes(b'fixture audio '+sid.encode())
                item=result[sid]
                item.update(original_text_sha256=hashlib.sha256(row['text_de'].encode()).hexdigest(),
                    input_source_sha256=source_sha,evidence=[{'kind':'exact','clip':str(clip.relative_to(folder)),
                    'clip_sha256':hashlib.sha256(clip.read_bytes()).hexdigest(),'window_start_s':row['start_s'],
                    'window_end_s':row['end_s'],'full_text':item['alternatives']['exact']}])
                write(evidence_dir/(sid+'_source_recheck.json'),item)
            return result
        owner=self
        class Client:
            def json(self,system,data):
                owner.calls.append(copy.deepcopy(data));audit='draft' in data;result=[]
                for item in data['items']:
                    row={'id':item['id'],'text_zh':'有2座房屋。' if bad_target and item['id']=='line0001' else '有3座房屋。',
                         'source_choice':'exact'}
                    if audit:row.update(approved=whole or item['id']!='line0002',issues=['不能确认'] if not whole and item['id']=='line0002' else [])
                    result.append(row)
                return {'translations':result}
        if whole:
            with patch('proxy_client.invariants',return_value=['fixture original local-rule rejection']):
                with self.assertRaises(recovery.RecoveryReviewRequired):
                    repair.run(self.root,self.jid,client_factory=lambda _:Client(),recheck_factory=recheck)
        elif old_stop:
            with patch.object(repair,'_resume_partial_review_locked',side_effect=RuntimeError('old whole-batch stop')):
                with self.assertRaises(RuntimeError):repair.run(self.root,self.jid,client_factory=lambda _:Client(),recheck_factory=recheck)
        else:
            self.partial_result=repair.run(self.root,self.jid,client_factory=lambda _:Client(),recheck_factory=recheck)
        self.partial_client_factory=lambda _:Client();self.partial_recheck_factory=recheck
        path=self.folder/'recovery/website_journal.json';journal=json.loads(path.read_text());event=journal['events'][0]
        # Match the real task's three already-consumed actions; no new action
        # may be appended when the last one's accepted subset is replayed.
        previous=[]
        for n in (1,2):
            prior=copy.deepcopy(event);prior.pop('partial_review',None)
            prior.update(id=hashlib.sha256(('prior'+str(n)).encode()).hexdigest(),
                input_fingerprint=hashlib.sha256(('prior input'+str(n)).encode()).hexdigest())
            previous.append(prior)
        journal['events']=previous+[event];write(path,journal)
        self.assertEqual(len(self.calls),2)
        return event['id']

    def test_saved_approved_subset_replays_without_calls_or_budget_changes(self):
        eid=self.saved_partial_fixture();original=(self.folder/'segments.plan.json').read_bytes()
        evidence=self.folder/'revisions/source_review_v1/evidence'/('translation_'+eid)
        failed=(evidence/'failure.json').read_bytes();audit=(evidence/'audit.json').read_bytes()
        with patch('proxy_client.ProxyClient',side_effect=AssertionError('no API')), patch('source_audio_recheck.recheck',side_effect=AssertionError('no ASR')):
            result=repair.resume_partial_review(self.root,self.jid,eid)
        self.assertEqual(result['accepted_ids'],['line0001','line0003']);self.assertEqual(result['rejected_ids'],['line0002'])
        journal=json.loads((self.folder/'recovery/website_journal.json').read_text());event=journal['events'][-1]
        self.assertEqual(len(journal['events']),3);self.assertEqual(sum(e['proxy_calls'] for e in journal['events']),6)
        self.assertEqual(event['status'],'failed');self.assertEqual(event['proxy_calls'],2)
        self.assertEqual(event['partial_review']['new_proxy_calls'],0)
        self.assertEqual(event['partial_review']['status'],'succeeded')
        revised,_=pipeline.reviewed_source_plan(self.folder,self.folder/'segments.plan.json',self.source_sha)
        self.assertEqual(revised[1],self.rows[1]);self.assertEqual(revised[0]['text_de'],'Es sind 3 Häuser.')
        self.assertEqual((self.folder/'segments.plan.json').read_bytes(),original)
        self.assertEqual((evidence/'failure.json').read_bytes(),failed);self.assertEqual((evidence/'audit.json').read_bytes(),audit)
        hints=json.loads((self.folder/'translation/source_review_hints.json').read_text())['rows']
        self.assertEqual(set(hints),{'line0001','line0003'})
        self.assertEqual(repair.resume_partial_review(self.root,self.jid,eid)['status'],'reused')

    def test_new_mixed_audit_saves_subset_and_preserves_failed_action(self):
        eid=self.saved_partial_fixture(old_stop=False)
        event=json.loads((self.folder/'recovery/website_journal.json').read_text())['events'][-1]
        self.assertEqual(event['status'],'failed');self.assertEqual(event['partial_review']['status'],'succeeded')
        self.assertEqual(event['partial_review']['rejected_ids'],['line0002'])
        self.assertEqual(len(self.calls),2);self.assertEqual(len(self.rechecks),1)
        self.assertEqual(self.partial_result['status'],'partial')
        evidence=self.folder/'revisions/source_review_v1/evidence'/('translation_'+eid)
        self.assertTrue(json.loads((evidence/'failure.json').read_text())['partial_review_saved'])

    def test_new_partial_rejection_blocks_old_hint_until_newer_approved_commit(self):
        text=self.rows[1]['text_de']
        write(self.folder/'translation/source_review_hints.json',{'schema':'source_review_hints_v1','rows':{'line0002':{
            'source_text_sha256':hashlib.sha256(text.encode()).hexdigest(),'note':'旧机器审校',
            'required_strings':['有3座房屋。'],'machine_translation_recovery':True}}})
        self.saved_partial_fixture(old_stop=False)
        revised,_=pipeline.reviewed_source_plan(self.folder,self.folder/'segments.plan.json',self.source_sha)
        write(self.folder/'translation/input.json',{'segments':revised,'source_language':'de'})
        self.assertEqual(repair.source_pending(self.folder,revised),['line0002'])
        # A later committed independent approval can close that same binding.
        self.rows=revised;self.checkpoint(sid='line0002')
        result=repair.run(self.root,self.jid,client_factory=self.client,ids=['line0002'],recheck_factory=lambda root,folder,rows,evidence,sha:{
            row['segment_id']:{'alternatives':{'original':row['text_de']},'evidence':[]} for row in rows})
        self.assertEqual(result['status'],'succeeded')
        self.assertEqual(repair.source_pending(self.folder,revised),[])
        # A newer partial rejection must in turn override that approval.
        self.rows=revised;value=json.loads((self.folder/'translation/checkpoint.json').read_text())
        value['input_rows_sha256']=hashlib.sha256(json.dumps(revised,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()
        value['batches']['line0001']['review_rejections']=[{'id':r['segment_id'],'approved':False,'issues':['原文仍有歧义']} for r in revised]
        write(self.folder/'translation/checkpoint.json',value)
        again=repair.run(self.root,self.jid,ids=[r['segment_id'] for r in revised],
            client_factory=self.partial_client_factory,recheck_factory=self.partial_recheck_factory)
        self.assertEqual(again['status'],'partial')
        self.assertEqual(repair.source_pending(self.folder,revised),['line0002'])

    def test_partial_rejections_remain_scoped_to_the_same_source_binding(self):
        eid=self.saved_partial_fixture(old_stop=False)
        hints=json.loads((self.folder/'translation/source_review_hints.json').read_text())['rows']
        self.assertEqual(repair._partial_rejections_pending(self.folder,self.rows,hints),{'line0002'})
        for key,value in [('text_de','A newly corrected source.'),('speaker_id','S99'),('start_s',99.)]:
            changed=copy.deepcopy(self.rows);changed[1][key]=value
            self.assertEqual(repair._partial_rejections_pending(self.folder,changed,hints),set())

    def test_partial_replay_rejects_changed_rows_media_and_user_constraints(self):
        eid=self.saved_partial_fixture();path=self.folder/'translation/input.json';before=path.read_bytes()
        changed=json.loads(path.read_text());changed['segments'][0]['speaker_id']='S99';write(path,changed)
        with self.assertRaises(recovery.RecoveryReviewRequired):repair.resume_partial_review(self.root,self.jid,eid)
        path.write_bytes(before);self.video.write_bytes(b'other video')
        with self.assertRaises(recovery.RecoveryReviewRequired):repair.resume_partial_review(self.root,self.jid,eid)
        self.video.write_bytes(b'fixture original media')
        write(self.folder/'translation/source_review_hints.json',{'schema':'source_review_hints_v1','rows':{'line0001':{
            'source_text_sha256':hashlib.sha256(self.rows[0]['text_de'].encode()).hexdigest(),
            'note':'new user lock','required_strings':['用户新确认']}}})
        with self.assertRaises(recovery.RecoveryReviewRequired):repair.resume_partial_review(self.root,self.jid,eid)
        self.assertFalse((self.folder/'source_plan_revision.json').exists())

    def test_partial_replay_rejects_tampered_response_and_candidate(self):
        eid=self.saved_partial_fixture();evidence=self.folder/'revisions/source_review_v1/evidence'/('translation_'+eid)
        path=evidence/'audit.json';before=path.read_bytes();data=json.loads(path.read_text())
        data['translations'][1].update(approved=True,issues=[]);write(path,data)
        with self.assertRaises(recovery.RecoveryReviewRequired):repair.resume_partial_review(self.root,self.jid,eid)
        path.write_bytes(before);path=evidence/'line0001_source_recheck.json';data=json.loads(path.read_text())
        data['alternatives']['exact']='Es sind 2 Häuser.';write(path,data)
        with self.assertRaises(recovery.RecoveryReviewRequired):repair.resume_partial_review(self.root,self.jid,eid)
        self.assertFalse((self.folder/'source_plan_revision.json').exists())

    def test_partial_replay_rechecks_local_invariants(self):
        eid=self.saved_partial_fixture(bad_target=True)
        with self.assertRaises(recovery.RecoveryReviewRequired):repair.resume_partial_review(self.root,self.jid,eid)
        self.assertFalse((self.folder/'source_plan_revision.json').exists())
        self.assertFalse((self.folder/'translation/source_review_hints.json').exists())

    def test_partial_apply_is_crash_resumable_without_new_budget(self):
        eid=self.saved_partial_fixture();original_atomic=recovery._atomic
        def crash(path,data):
            if path==self.folder/'revisions/source_review_v1/segments.plan.json':raise KeyboardInterrupt('partial apply interruption')
            return original_atomic(path,data)
        with patch.object(recovery,'_atomic',side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):repair.resume_partial_review(self.root,self.jid,eid)
        journal=json.loads((self.folder/'recovery/website_journal.json').read_text())
        self.assertEqual(journal['events'][-1]['status'],'failed')
        self.assertEqual(journal['events'][-1]['partial_review']['status'],'applying')
        result=repair.resume_partial_review(self.root,self.jid,eid)
        self.assertEqual(result['status'],'reused');self.assertEqual(len(self.calls),2)
        self.assertEqual(len(json.loads((self.folder/'recovery/website_journal.json').read_text())['events']),3)

    def test_interrupted_partial_apply_still_rejects_changed_media(self):
        eid=self.saved_partial_fixture();original_atomic=recovery._atomic
        def crash(path,data):
            if path==self.folder/'revisions/source_review_v1/segments.plan.json':raise KeyboardInterrupt('partial apply interruption')
            return original_atomic(path,data)
        with patch.object(recovery,'_atomic',side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):repair.resume_partial_review(self.root,self.jid,eid)
        self.video.write_bytes(b'changed during interruption')
        with self.assertRaises(recovery.RecoveryReviewRequired):repair.resume_partial_review(self.root,self.jid,eid)
        self.assertFalse((self.folder/'source_plan_revision.json').exists())

    def test_legacy_saved_audit_uses_exact_full_input_binding(self):
        eid=self.saved_partial_fixture();path=self.folder/'recovery/website_journal.json';journal=json.loads(path.read_text())
        journal['events'][-1].pop('saved_review');write(path,journal)
        request=self.folder/'revisions/source_review_v1/evidence'/('translation_'+eid)/'request.json'
        data=json.loads(request.read_text())
        for item in data['items']:item.pop('source_binding')
        write(request,data)
        result=repair.resume_partial_review(self.root,self.jid,eid)
        self.assertEqual(result['accepted_ids'],['line0001','line0003'])

    def independent_report(self,sid='line0002'):
        from proxy_client import source_binding
        row=next(row for row in self.rows if row['segment_id']==sid);binding=source_binding(row)
        report={'schema':'independent_content_review_v1','input_source_sha256':self.source_sha,
            'review_method':'agent_source_semantics','human_listened':False,'source_words_verified':False,
            'rows':{sid:{'source_binding':binding,'source_text_sha256':binding['text_de_sha256'],
                        'approved':False,'issues':['原文语义存在独立复核指出的歧义']}}}
        write(self.folder/repair.INDEPENDENT_REPORT,report)
        return report

    def test_independent_rejection_only_qualifies_as_explicit_current_history(self):
        self.independent_report('line0001')
        self.assertNotIn('line0001',repair.pending(self.folder,self.rows))
        self.assertTrue(repair.pending(self.folder,self.rows,include_history=True)['line0001']['independent_content_rejection'])
        self.assertIn('line0001',repair.source_pending(self.folder,self.rows))

    def test_independent_rejection_rejects_bad_schema_and_ignores_stale_bindings(self):
        report=self.independent_report();path=self.folder/repair.INDEPENDENT_REPORT
        for key,value in [('speaker_id','S99'),('start_s',99.),('text_de_sha256','0'*64)]:
            changed=copy.deepcopy(report);changed['rows']['line0002']['source_binding'][key]=value;write(path,changed)
            self.assertEqual(repair._independent_content_rejections(self.folder,self.rows),{})
        changed=copy.deepcopy(report);changed['input_source_sha256']='0'*64;write(path,changed)
        self.assertEqual(repair._independent_content_rejections(self.folder,self.rows),{})
        for mutate in (lambda d:d.update(review_method='proxy_audit'),
                       lambda d:d.update(human_listened=True),
                       lambda d:d['rows']['line0002'].update(approved=True),
                       lambda d:d['rows']['line0002'].update(issues=[])):
            changed=copy.deepcopy(report);mutate(changed);write(path,changed)
            with self.assertRaises(recovery.RecoveryReviewRequired):repair._independent_content_rejections(self.folder,self.rows)

    def test_independent_rejection_survives_old_hint_cache_and_old_approved_event(self):
        original_only=lambda root,folder,rows,evidence,sha:{row['segment_id']:{
            'alternatives':{'original':row['text_de']},'evidence':[]} for row in rows}
        repair.run(self.root,self.jid,client_factory=self.client,recheck_factory=original_only)
        old_hint=(self.folder/'translation/source_review_hints.json').read_bytes()
        self.independent_report();write(self.folder/'translation/report.json',{'review_status':'machine_second_pass_approved'})
        self.assertEqual(repair.source_pending(self.folder,self.rows),['line0002'])
        self.assertEqual((self.folder/'translation/source_review_hints.json').read_bytes(),old_hint)
        # The next real committed review sees this report and keeps the same
        # source/Chinese. Its bound receipt, rather than mere cache approval,
        # closes the finding and makes durable progress visible to Flow.
        result=repair.run(self.root,self.jid,client_factory=self.client,recheck_factory=original_only)
        self.assertEqual(result['status'],'succeeded')
        self.assertEqual(repair.source_pending(self.folder,self.rows),[])
        self.assertNotEqual((self.folder/'translation/source_review_hints.json').read_bytes(),old_hint)
        hint=json.loads((self.folder/'translation/source_review_hints.json').read_text())['rows']['line0002']
        self.assertEqual(hint['independent_content_review_sha256'],recovery._sha(self.folder/repair.INDEPENDENT_REPORT))

    def test_modified_independent_report_cannot_reuse_prior_approval(self):
        self.independent_report();original_only=lambda root,folder,rows,evidence,sha:{row['segment_id']:{
            'alternatives':{'original':row['text_de']},'evidence':[]} for row in rows}
        repair.run(self.root,self.jid,client_factory=self.client,recheck_factory=original_only)
        report=json.loads((self.folder/repair.INDEPENDENT_REPORT).read_text());report['rows']['line0002']['issues']=['新的独立语义问题']
        write(self.folder/repair.INDEPENDENT_REPORT,report)
        self.assertEqual(repair.source_pending(self.folder,self.rows),['line0002'])

    def test_changed_independent_report_blocks_saved_partial_replay(self):
        self.independent_report();eid=self.saved_partial_fixture()
        report=json.loads((self.folder/repair.INDEPENDENT_REPORT).read_text());report['rows']['line0002']['issues']=['复核约束发生变化']
        write(self.folder/repair.INDEPENDENT_REPORT,report)
        with self.assertRaises(recovery.RecoveryReviewRequired):repair.resume_partial_review(self.root,self.jid,eid)
        self.assertFalse((self.folder/'source_plan_revision.json').exists())

    def test_default_run_includes_selected_independent_issues_without_expanding_scope(self):
        from proxy_client import source_binding
        report=self.independent_report();binding=source_binding(self.rows[0])
        report['rows']['line0001']={'source_binding':binding,'source_text_sha256':binding['text_de_sha256'],
            'approved':False,'issues':['另一行的独立源文问题，不属于本次默认选择范围']}
        write(self.folder/repair.INDEPENDENT_REPORT,report)
        result=self.run_repair()
        self.assertEqual(result['status'],'succeeded');self.assertEqual(self.rechecks,[['line0002']])
        for request in self.calls:
            self.assertEqual([item['id'] for item in request['items']],['line0002'])
            self.assertIn(report['rows']['line0002']['issues'][0],request['items'][0]['review_issues'])
            self.assertTrue(request['items'][0]['independent_content_rejection'])
        journal=json.loads((self.folder/'recovery/website_journal.json').read_text())
        self.assertEqual(len(journal['events']),1);self.assertEqual(journal['events'][0]['proxy_calls'],2)

    def test_whole_saved_approval_replays_only_after_current_invariants_pass(self):
        eid=self.saved_partial_fixture(whole=True)
        evidence=self.folder/'revisions/source_review_v1/evidence'/('translation_'+eid)
        originals={name:(evidence/name).read_bytes() for name in ('request.json','editor.json','audit.json','failure.json','local_checks.json')}
        with patch('proxy_client.ProxyClient',side_effect=AssertionError('no API')),patch('source_audio_recheck.recheck',side_effect=AssertionError('no ASR')):
            result=repair.resume_partial_review(self.root,self.jid,eid)
        self.assertEqual(result['accepted_ids'],['line0001','line0002','line0003']);self.assertEqual(result['rejected_ids'],[])
        self.assertTrue(result['whole_saved_review_replay']);self.assertEqual(result['new_proxy_calls'],0)
        self.assertEqual(result['action'],'translation_saved_review_replay')
        for name,data in originals.items():self.assertEqual((evidence/name).read_bytes(),data)
        self.assertTrue((evidence/'whole_review_local_checks.json').is_file())
        journal=json.loads((self.folder/'recovery/website_journal.json').read_text());event=journal['events'][-1]
        self.assertEqual(len(journal['events']),3);self.assertEqual(sum(x['proxy_calls'] for x in journal['events']),6)
        self.assertEqual(event['status'],'failed');self.assertEqual(event['reason'],'invariant_rejected')
        self.assertEqual(event['partial_review']['status'],'succeeded');self.assertEqual(len(self.calls),2)
        self.assertEqual(repair.resume_partial_review(self.root,self.jid,eid)['status'],'reused')

    def test_whole_saved_approval_does_not_bypass_a_real_wrong_number(self):
        eid=self.saved_partial_fixture(whole=True,bad_target=True)
        with self.assertRaises(recovery.RecoveryReviewRequired):repair.resume_partial_review(self.root,self.jid,eid)
        self.assertFalse((self.folder/'source_plan_revision.json').exists())
        self.assertFalse((self.folder/'translation/source_review_hints.json').exists())
        journal=json.loads((self.folder/'recovery/website_journal.json').read_text())
        self.assertEqual(len(journal['events']),3);self.assertEqual(journal['events'][-1]['proxy_calls'],2)

    def test_whole_saved_approval_requires_original_invariant_rejected_reason(self):
        eid=self.saved_partial_fixture(whole=True);path=self.folder/'recovery/website_journal.json';journal=json.loads(path.read_text())
        journal['events'][-1]['reason']='review_rejected';write(path,journal)
        with self.assertRaises(recovery.RecoveryReviewRequired):repair.resume_partial_review(self.root,self.jid,eid)
        self.assertFalse((self.folder/'source_plan_revision.json').exists())

    def exhaust_saved_fixture_budget(self):
        path=self.folder/'recovery/website_journal.json';journal=json.loads(path.read_text())
        extras=[]
        for number in range(3):
            old=copy.deepcopy(journal['events'][0]);old.update(
                id=hashlib.sha256(('extra event '+str(number)).encode()).hexdigest(),
                input_fingerprint=hashlib.sha256(('extra input '+str(number)).encode()).hexdigest())
            extras.append(old)
        journal['events']=extras+journal['events'];write(path,journal)
        return journal

    def auto_saved(self):
        with patch('proxy_client.ProxyClient',side_effect=AssertionError('no API')),patch('source_audio_recheck.recheck',side_effect=AssertionError('no ASR')):
            return repair.run(self.root,self.jid)

    def test_automatic_whole_replay_precedes_full_budget_without_api_or_asr(self):
        eid=self.saved_partial_fixture(whole=True);self.exhaust_saved_fixture_budget()
        result=self.auto_saved()
        self.assertTrue(result['automatic_saved_review_replay'])
        self.assertTrue(result['whole_saved_review_replay']);self.assertEqual(result['new_proxy_calls'],0)
        self.assertEqual(result['event_id'],eid);self.assertEqual(result['status'],'succeeded')
        journal=json.loads((self.folder/'recovery/website_journal.json').read_text())
        self.assertEqual(len(journal['events']),6);self.assertEqual(sum(e['proxy_calls'] for e in journal['events']),12)
        self.assertEqual(journal['events'][-1]['status'],'failed')
        self.assertEqual(journal['events'][-1]['reason'],'invariant_rejected')

    def test_current_guard_skip_is_readonly_and_later_guard_fix_can_replay(self):
        eid=self.saved_partial_fixture(whole=True);self.exhaust_saved_fixture_budget()
        originals={p:p.read_bytes() for p in self.folder.rglob('*') if p.is_file()}
        with patch('proxy_client.invariants',return_value=['current guard still rejects']):
            with self.assertRaises(recovery.RecoveryBudgetExhausted):self.auto_saved()
        self.assertEqual({p:p.read_bytes() for p in self.folder.rglob('*') if p.is_file()},originals)
        event=json.loads((self.folder/'recovery/website_journal.json').read_text())['events'][-1]
        self.assertNotIn('partial_review',event)
        result=self.auto_saved();self.assertEqual(result['event_id'],eid);self.assertEqual(result['new_proxy_calls'],0)

    def test_automatic_whole_replay_can_reuse_batch_with_only_one_pending_guard_row(self):
        self.saved_partial_fixture(whole=True);self.exhaust_saved_fixture_budget()
        checkpoint=json.loads((self.folder/'translation/checkpoint.json').read_text())
        batch=checkpoint['batches']['line0001'];batch.update(error_type='InvariantRejectedError',
            checks={'line0001':['historical rule rejection']},review_rejections=[])
        write(self.folder/'translation/checkpoint.json',checkpoint)
        self.assertEqual(list(repair.pending(self.folder,self.rows)),['line0001'])
        result=self.auto_saved()
        self.assertEqual(result['accepted_ids'],['line0001','line0002','line0003'])
        self.assertEqual(result['new_proxy_calls'],0)

    def test_automatic_replay_does_not_bypass_true_wrong_number(self):
        self.saved_partial_fixture(whole=True,bad_target=True);self.exhaust_saved_fixture_budget()
        before=(self.folder/'recovery/website_journal.json').read_bytes()
        with self.assertRaises(recovery.RecoveryBudgetExhausted):self.auto_saved()
        self.assertEqual((self.folder/'recovery/website_journal.json').read_bytes(),before)
        self.assertFalse((self.folder/'translation/source_review_hints.json').exists())

    def test_saved_replay_preflight_never_writes_before_rejecting(self):
        eid=self.saved_partial_fixture(whole=True,bad_target=True)
        before=(self.folder/'recovery/website_journal.json').read_bytes()
        with patch.object(recovery,'_save',side_effect=AssertionError('preflight must not write')):
            with self.assertRaises(recovery.RecoveryReviewRequired):repair.resume_partial_review(self.root,self.jid,eid)
        self.assertEqual((self.folder/'recovery/website_journal.json').read_bytes(),before)

    def test_automatic_replay_cannot_widen_explicit_requested_ids(self):
        self.saved_partial_fixture(whole=True)
        journal=recovery._journal(self.folder);before=(self.folder/'recovery/website_journal.json').read_bytes()
        self.assertIsNone(repair._try_latest_whole_saved_review(self.root,self.folder,journal,self.rows,['line0002']))
        self.assertEqual((self.folder/'recovery/website_journal.json').read_bytes(),before)

    def later_rejection_fixture(self,same_binding=True,diagnostic=False):
        from proxy_client import source_binding,VERSION
        journal=recovery._journal(self.folder);event=journal['events'][-1]
        row=self.rows[1];binding=source_binding(row)
        if not same_binding:binding['speaker_id']='S99'
        item={'id':row['segment_id'],'source_binding':binding,'text_source':row['text_de'],'text_de':row['text_de']}
        audit={'translations':[{'id':row['segment_id'],'text_zh':'有3座房屋。','approved':False,'issues':['后续原词拒绝']} ]}
        if diagnostic:
            write(self.folder/'translation/diagnostics/later_audit.json',{
                'schema':'proxy_translation_diagnostic_v1','version':VERSION,'stage':'audit','human_reviewed':False,
                'created_at_unix':datetime.fromisoformat(event['finished_at']).timestamp()+10,
                'input':{'items':[item]},'response':audit})
        else:
            later=copy.deepcopy(event);later.update(id=hashlib.sha256(b'later explicit rejection').hexdigest(),
                input_fingerprint=hashlib.sha256(b'later rejection input').hexdigest(),
                reason='review_rejected',segment_ids=['line0002']);later.pop('saved_review')
            journal['events'].append(later);write(self.folder/'recovery/website_journal.json',journal)
            evidence=self.folder/'revisions/source_review_v1/evidence'/('translation_'+later['id'])
            write(evidence/'request.json',{'items':[item]});write(evidence/'audit.json',audit)
        return recovery._journal(self.folder)

    def test_later_same_binding_journal_rejection_blocks_old_automatic_approval(self):
        self.saved_partial_fixture(whole=True);journal=self.later_rejection_fixture()
        before=(self.folder/'recovery/website_journal.json').read_bytes()
        self.assertIsNone(repair._try_latest_whole_saved_review(self.root,self.folder,journal,self.rows,[r['segment_id'] for r in self.rows]))
        self.assertEqual((self.folder/'recovery/website_journal.json').read_bytes(),before)
        self.assertFalse((self.folder/'source_plan_revision.json').exists())

    def test_later_same_binding_normal_audit_rejection_also_blocks_replay(self):
        self.saved_partial_fixture(whole=True);journal=self.later_rejection_fixture(diagnostic=True)
        self.assertIsNone(repair._try_latest_whole_saved_review(self.root,self.folder,journal,self.rows,[r['segment_id'] for r in self.rows]))
        self.assertFalse((self.folder/'source_plan_revision.json').exists())

    def test_later_rejection_for_different_source_binding_does_not_block(self):
        self.saved_partial_fixture(whole=True);journal=self.later_rejection_fixture(same_binding=False)
        result=repair._try_latest_whole_saved_review(self.root,self.folder,journal,self.rows,[r['segment_id'] for r in self.rows])
        self.assertTrue(result['automatic_saved_review_replay']);self.assertEqual(result['new_proxy_calls'],0)

    def test_latest_stale_saved_approval_never_falls_back_to_older_approval(self):
        eid=self.saved_partial_fixture(whole=True);journal=recovery._journal(self.folder)
        latest=copy.deepcopy(journal['events'][-1]);latest['id']=hashlib.sha256(b'latest stale approval').hexdigest()
        evidence=self.folder/'revisions/source_review_v1/evidence'
        shutil.copytree(evidence/('translation_'+eid),evidence/('translation_'+latest['id']))
        latest['saved_review']['audit_sha256']='0'*64
        journal['events'].append(latest);write(self.folder/'recovery/website_journal.json',journal)
        before=(self.folder/'recovery/website_journal.json').read_bytes()
        self.assertIsNone(repair._try_latest_whole_saved_review(self.root,self.folder,journal,self.rows,[r['segment_id'] for r in self.rows]))
        self.assertEqual((self.folder/'recovery/website_journal.json').read_bytes(),before)
        self.assertFalse((self.folder/'source_plan_revision.json').exists())

    def test_automatic_saved_approval_respects_new_user_lock_and_original_media(self):
        self.saved_partial_fixture(whole=True);journal=recovery._journal(self.folder)
        before=(self.folder/'recovery/website_journal.json').read_bytes()
        write(self.folder/'translation/source_review_hints.json',{'schema':'source_review_hints_v1','rows':{'line0001':{
            'source_text_sha256':hashlib.sha256(self.rows[0]['text_de'].encode()).hexdigest(),
            'note':'用户新确认','required_strings':['用户指定文字']}}})
        self.assertIsNone(repair._try_latest_whole_saved_review(self.root,self.folder,journal,self.rows,[r['segment_id'] for r in self.rows]))
        (self.folder/'translation/source_review_hints.json').unlink();self.video.write_bytes(b'other original media')
        self.assertIsNone(repair._try_latest_whole_saved_review(self.root,self.folder,journal,self.rows,[r['segment_id'] for r in self.rows]))
        self.assertEqual((self.folder/'recovery/website_journal.json').read_bytes(),before)
        self.assertFalse((self.folder/'source_plan_revision.json').exists())

    def test_automatic_preflight_preserves_permanent_user_confirmed_source_lock(self):
        self.saved_partial_fixture(whole=True);journal=recovery._journal(self.folder)
        before=(self.folder/'recovery/website_journal.json').read_bytes()
        revision={'website_manual_corrections':[{'source_word_confirmed_by_user_ids':['line0001']}]}
        with patch.object(recovery,'_source_revision',return_value=revision):
            result=repair._try_latest_whole_saved_review(self.root,self.folder,journal,self.rows,[r['segment_id'] for r in self.rows])
        self.assertIsNone(result)
        self.assertEqual((self.folder/'recovery/website_journal.json').read_bytes(),before)

    def test_source_candidate_cannot_drop_knapp_comparison(self):
        self.assertTrue(repair.source_alternative_errors('Es sind knapp 998 Milliarden Euro.','Es sind 998 Milliarden Euro.'))
        self.assertEqual(repair.source_alternative_errors('Es sind knapp 998 Milliarden Euro.','Es sind knapp 998 Milliarden Euro.'),[])


if __name__=='__main__':unittest.main()
