"""Saved approvals constrain text; they never replace the new normal audit."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'web'))
import pipeline
import proxy_client as proxy
from translation_constraints import (
    approved_target_errors,validate_approved_targets,validate_saved_translation_lock)
import test_translation_cache_revalidation as fixtures


class SavedTranslationLockTests(unittest.TestCase):
    def setUp(self):
        f=fixtures.TranslationCacheRevalidationTests();f.setUp()
        self.addCleanup(f.doCleanups);self.fixture=f
        f.root=f.root.resolve();f.folder=f.root/'jobs/web_fixture'
        f.diagnostics=f.folder/'translation/diagnostics'
        self.video=f.root/'source/fixture.mp4';self.video.parent.mkdir()
        self.video.write_bytes(b'fixed source video fixture')
        source_sha=hashlib.sha256(self.video.read_bytes()).hexdigest()
        fixtures.write(f.folder/'source.json',{'input':str(self.video)})
        fixtures.write(f.root/'web/state/jobs/web_fixture.json',{'source_sha256':source_sha})
        fixtures.write(f.folder/'segments.plan.json',{'segments':f.rows})
        self.record={'schema':'proxy_translation_diagnostic_v1','version':proxy.VERSION,
            'model':'offline-actual-shape','stage':'audit','batch_key':'a'*64,
            'created_at_unix':1000.,'human_reviewed':False,'contains_credentials':False,
            'input':{'source_language':'de','items':[{
                'id':'line0001','source_binding':proxy.source_binding(f.rows[0]),
                'text_de':f.rows[0]['text_de'],'text_source':f.rows[0]['text_de'],
                'source_language':'de','draft_zh':'有3人。'}]},
            'response':{'translations':[{'id':'line0001','text_zh':'有3人。',
                                        'approved':True,'issues':[]}]}}
        self.path=f.diagnostics/'saved_audit.json'
        self.hint={'machine_saved_translation_lock':True,
            'source_text_sha256':hashlib.sha256(f.rows[0]['text_de'].encode()).hexdigest(),
            'note':'真实旧审校批准整句保持，本轮仍须正常独立二审。',
            'approved_text_zh':'有3人。',
            'approved_text_zh_sha256':hashlib.sha256('有3人。'.encode()).hexdigest(),
            'required_strings':['有3人。']}
        self.save_record(self.record)

    def save_record(self,record):
        fixtures.write(self.path,record)
        self.hint['saved_proxy_approval']={
            'path':str(self.path.relative_to(self.fixture.folder)),
            'sha256':hashlib.sha256(self.path.read_bytes()).hexdigest()}
        self.save_hint()

    def save_hint(self):
        fixtures.write(self.fixture.folder/'translation/source_review_hints.json',
            {'schema':'source_review_hints_v1','rows':{'line0001':self.hint}})

    def test_real_bound_approval_locks_text_but_still_requires_two_normal_requests(self):
        f=self.fixture;before=self.path.read_bytes()
        verified=validate_saved_translation_lock(f.rows[0],self.hint,f.folder)
        self.assertIs(verified['new_review_performed'],False)
        updated,report=f.run_translation()
        self.assertEqual(len(f.client.calls),2)
        self.assertEqual([item['id'] for item in f.client.calls[1]['items']],['line0001'])
        self.assertEqual(updated[0]['translation_review_method'],'proxy_second_pass_and_numeric_checks')
        self.assertIs(updated[0]['human_reviewed'],False)
        self.assertNotIn('machine_explicit_continuation',updated[0])
        self.assertEqual(report['review_method'],'two_pass_machine_review_and_numeric_invariants')
        self.assertEqual(self.path.read_bytes(),before)
        pipeline.validate_translations(f.rows,updated,report,folder=f.folder)
        changed=copy.deepcopy(updated);changed[0]['text_zh']+='增加一句。'
        with self.assertRaisesRegex(RuntimeError,'line0001'):
            pipeline.validate_translations(f.rows,changed,report,folder=f.folder)

    def test_saved_approval_cannot_override_new_normal_refusal(self):
        f=self.fixture;f.client.approved=False;before=self.path.read_bytes()
        with self.assertRaises(proxy.ReviewRejectedError):f.run_translation()
        self.assertEqual(len(f.client.calls),3)
        self.assertEqual(f.checkpoint()['completed'],0)
        self.assertEqual(self.path.read_bytes(),before)
        self.assertFalse(list((f.root/'cache').rglob('*.json')))

    def test_unique_full_same_source_approval_is_required(self):
        for kind in ('rejected','issues','target','binding','source','language','duplicate_input',
                     'duplicate_output','translate','wrong_schema','human','nan_time'):
            record=copy.deepcopy(self.record)
            item=record['input']['items'][0];answer=record['response']['translations'][0]
            if kind=='rejected':answer['approved']=False
            elif kind=='issues':answer['issues']=['原文仍有疑点']
            elif kind=='target':answer['text_zh']+='多了一句。'
            elif kind=='binding':item['source_binding']['speaker_id']='S02'
            elif kind=='source':item['text_source']='Es sind 4 Menschen.'
            elif kind=='language':item['source_language']='en'
            elif kind=='duplicate_input':record['input']['items'].append(copy.deepcopy(item))
            elif kind=='duplicate_output':record['response']['translations'].append(copy.deepcopy(answer))
            elif kind=='translate':record['stage']='translate'
            elif kind=='wrong_schema':record['schema']='approval_claim'
            elif kind=='human':record['human_reviewed']=True
            else:record['created_at_unix']=float('nan')
            self.save_record(record)
            with self.subTest(kind=kind),self.assertRaises(ValueError):
                validate_saved_translation_lock(self.fixture.rows[0],self.hint,self.fixture.folder)

    def test_sha_path_actual_media_and_plan_are_checked_before_requests(self):
        f=self.fixture;hint=copy.deepcopy(self.hint)
        for path in ('../outside.json','translation/other/saved_audit.json',str(self.path)):
            self.hint=copy.deepcopy(hint);self.hint['saved_proxy_approval']['path']=path
            with self.subTest(path=path),self.assertRaises(ValueError):
                validate_saved_translation_lock(f.rows[0],self.hint,f.folder)
        self.hint=copy.deepcopy(hint);self.hint['saved_proxy_approval']['sha256']='b'*64
        self.save_hint()
        with self.assertRaises(ValueError):f.run_translation()
        self.assertEqual(f.client.calls,[])
        self.hint=copy.deepcopy(hint);self.save_hint()
        self.video.write_bytes(b'changed source video')
        with self.assertRaises(ValueError):f.run_translation()
        self.video.write_bytes(b'fixed source video fixture')
        changed=copy.deepcopy(f.rows);changed[0]['text_de']='Es sind 4 Menschen.'
        fixtures.write(f.folder/'segments.plan.json',{'segments':changed})
        with self.assertRaises(ValueError):f.run_translation()
        self.assertEqual(f.client.calls,[])

    def test_exact_lock_cannot_be_substring_or_mislabeled_as_human_or_continuation(self):
        f=self.fixture
        self.assertTrue(approved_target_errors(f.rows[0],'有3人。多加一句。',self.hint))
        for kind in ('partial_required','wrong_hash','continuation','user','human'):
            hint=copy.deepcopy(self.hint)
            if kind=='partial_required':hint['required_strings']=['有3']
            elif kind=='wrong_hash':hint['approved_text_zh_sha256']='b'*64
            elif kind=='continuation':hint['machine_explicit_continuation']=True
            elif kind=='user':hint['user_contextual_adoption']=True
            else:hint['human_reviewed']=True
            with self.subTest(kind=kind),self.assertRaises(ValueError):
                validate_saved_translation_lock(f.rows[0],hint,f.folder)
        updated,report=f.run_translation()
        self.path.write_text(self.path.read_text()+' ')
        with self.assertRaisesRegex(RuntimeError,'line0001'):
            validate_approved_targets(updated,f.folder)


if __name__=='__main__':unittest.main()
