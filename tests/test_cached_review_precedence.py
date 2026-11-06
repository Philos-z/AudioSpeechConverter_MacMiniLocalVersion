"""Cache acceptance follows actual saved review timestamps, not file dates."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'web'))
sys.path.insert(0,str(ROOT/'tests'))
import proxy_client as proxy
import test_translation_cache_revalidation as fixtures


class CachedReviewPrecedenceTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.TranslationCacheRevalidationTests('test_cache_policy_label_cannot_bypass_current_checks')
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.path,self.value=self.fixture.seed()
        self.audit_path=next(self.fixture.diagnostics.glob('*_audit_*.json'))
        self.original=json.loads(self.audit_path.read_text())
        self.at=self.original['created_at_unix'];self.counter=0

    def save_cache(self):
        fixtures.write(self.path,self.value)

    def review(self,offset,approved,issues=None,target=None,mutate=None,name=None):
        record=copy.deepcopy(self.original);record['created_at_unix']=self.at+offset
        answer=record['response']['translations'][0]
        answer.update(approved=approved,issues=[] if approved else ['当前译文信息不符'])
        if issues is not None:answer['issues']=issues
        if target is not None:answer['text_zh']=target
        if mutate:mutate(record)
        self.counter+=1
        path=self.fixture.diagnostics/(record['batch_key'][:20]+'_audit_'+(name or 'fixture'+str(self.counter))+'.json')
        fixtures.write(path,record)
        return path,record

    def assert_veto(self):
        before=self.path.read_bytes();diagnostics={p:p.read_bytes() for p in self.fixture.diagnostics.glob('*.json')}
        with self.assertRaises(proxy.ReviewRejectedError) as caught:self.fixture.run_translation()
        self.assertEqual(self.fixture.client.calls,[])
        self.assertEqual(self.fixture.checkpoint()['completed'],0)
        self.assertEqual(self.path.read_bytes(),before)
        self.assertTrue(all(path.read_bytes()==data for path,data in diagnostics.items()))
        batch=self.fixture.checkpoint()['batches']['line0001']
        self.assertEqual(batch['error_type'],'ReviewRejectedError')
        self.assertEqual(batch['cache_revalidation']['status'],'rejected')
        evidence=batch['cache_revalidation']['review_evidence']
        for proof in evidence:
            path=self.fixture.root/proof['path']
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),proof['sha256'])
            self.assertEqual(json.loads(path.read_text())['created_at_unix'],proof['created_at_unix'])
        return caught.exception,evidence

    def assert_usable(self):
        before=self.path.read_bytes();updated,report=self.fixture.run_translation()
        self.assertEqual(self.fixture.client.calls,[])
        self.assertEqual(self.fixture.checkpoint()['completed'],1)
        self.assertEqual(report['cache_batches'],1)
        self.assertEqual(self.path.read_bytes(),before)
        return updated,report

    def test_later_same_request_and_same_target_refusal_vetoes_cache(self):
        self.review(10,False)
        _,proofs=self.assert_veto()
        self.assertEqual(proofs[0]['created_at_unix'],self.at+10)

    def test_later_true_approval_can_resolve_earlier_refusal_for_current_target(self):
        refused,_=self.review(10,False,name='zzzz_refusal')
        approved,_=self.review(20,True,name='0000_approval')
        # Deliberately reverse file timestamps too. Only persisted review times
        # establish which semantic decision happened later.
        os.utime(refused,(2000000000,2000000000));os.utime(approved,(1000000000,1000000000))
        _,report=self.assert_usable()
        proofs=report['cache_revalidation'][0]['review_evidence']
        self.assertEqual([proof['outcome'] for proof in proofs],['rejected','approved'])
        self.assertEqual(proofs[-1]['created_at_unix'],self.at+20)

    def test_old_cache_without_timestamp_needs_real_saved_later_approval(self):
        self.value.pop('final_review_provenance',None);self.save_cache()
        self.review(10,False);self.review(20,True)
        self.assert_usable()

    def test_old_cache_with_refusal_and_no_approval_proof_stops_conservatively(self):
        self.value.pop('final_review_provenance',None);self.save_cache()
        self.audit_path.unlink();self.review(10,False)
        os.utime(self.path,(2100000000,2100000000))
        self.assert_veto()

    def test_cache_metadata_cannot_invent_a_later_review_timestamp(self):
        self.value['final_review_provenance']['created_at_unix']=self.at+1000;self.save_cache()
        self.review(10,False)
        self.assert_veto()

    def test_other_source_binding_cannot_veto_the_current_cache(self):
        self.review(10,False,mutate=lambda record:record['input']['items'][0]['source_binding'].update(speaker_id='S99'))
        self.assert_usable()

    def test_wrong_key_model_or_context_is_not_current_review_authority(self):
        mutations=[lambda record:record.update(batch_key='0'*64),
                   lambda record:record.update(model='another-model'),
                   lambda record:record['input']['items'][0].update(previous_context='另一段原文。'),
                   lambda record:record['input']['items'][0].update(next_context='另一段原文。')]
        for mutate in mutations:self.review(10,False,mutate=mutate)
        self.assert_usable()

    def test_missing_or_extra_full_request_fields_are_not_authority(self):
        self.review(10,False,mutate=lambda record:record['input']['items'][0].pop('draft_zh'))
        self.review(11,False,mutate=lambda record:record['input']['items'][0].update(unrelated='extra'))
        self.assert_usable()

    def test_rejection_of_a_different_target_does_not_block_safe_cached_text(self):
        self.review(10,False,issues=['译文数字不符'],target='有4人。')
        self.assert_usable()

    def test_equal_time_rejection_is_not_erased_by_approval(self):
        self.review(10,False);self.review(10,True)
        self.assert_veto()

    def test_whitespace_cannot_hide_a_refused_cached_target(self):
        self.value['translations']['line0001']='  有3人。  ';self.save_cache()
        self.review(10,False,target='有3人。')
        self.assert_veto()

    def test_new_cache_records_final_actual_diagnostic_proof(self):
        proof=self.value.get('final_review_provenance')
        self.assertIsInstance(proof,dict)
        path=self.fixture.root/proof['path'];record=json.loads(path.read_text())
        self.assertEqual(proof['sha256'],hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(proof['created_at_unix'],record['created_at_unix'])
        self.assertEqual(record['stage'],'audit')
        self.assertTrue(record['response']['translations'][0]['approved'])

    def test_valid_approval_pointer_outside_current_job_can_prove_its_real_time(self):
        self.review(10,False)
        _,record=self.review(20,True)
        path=self.fixture.root/'jobs/earlier/translation/diagnostics/saved_approval.json'
        fixtures.write(path,record)
        proof=proxy._review_evidence(path,record,self.fixture.root)
        # Remove this job's later approval so only the bound cache provenance
        # can supply it; an unverified timestamp in the cache is insufficient.
        for candidate in self.fixture.diagnostics.glob('*fixture2.json'):candidate.unlink()
        self.value['final_review_provenance']=proof;self.save_cache()
        self.assert_usable()

    def test_no_cache_cannot_publish_an_old_response_over_a_later_refusal(self):
        self.path.unlink()
        # The existing audit filename sorts after this new filename, while its
        # recorded review happened earlier. call() can return that old response;
        # the common publication gate must still retain the actual later veto.
        self.review(10,False,name='0000_newer_refusal')
        with self.assertRaises(proxy.ReviewRejectedError):self.fixture.run_translation()
        self.assertEqual(self.fixture.client.calls,[])
        self.assertFalse(self.path.exists())
        self.assertEqual(self.fixture.checkpoint()['completed'],0)
        batch=self.fixture.checkpoint()['batches']['line0001']
        proof=batch['saved_review_revalidation']['review_evidence'][0]
        self.assertEqual(proof['created_at_unix'],self.at+10)
        self.assertEqual(batch['error_type'],'ReviewRejectedError')


if __name__=='__main__':unittest.main()
