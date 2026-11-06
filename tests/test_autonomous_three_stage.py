"""Run the real three controller phases against bounded in-memory Proxy fixtures."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'web'))
import autonomous_content_review as whole
import pipeline
import proxy_client as proxy


def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False))


class ThreeStageTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve();self.folder=self.root/'jobs/web_three_stage'
        self.folder.mkdir(parents=True)
        self.source=self.root/'fixture.mp4';self.source.write_bytes(b'local immutable source identity fixture')
        source_sha=hashlib.sha256(self.source.read_bytes()).hexdigest()
        write(self.folder/'source.json',{'input':str(self.source)})
        write(self.root/'web/state/jobs/web_three_stage.json',{'source_sha256':source_sha})
        self.rows=[{'segment_id':'line0001','speaker_id':'S01','start_s':0.,'end_s':2.,
                    'source_language':'de','text_de':'Es sind 3 Menschen.','text_source':'Es sind 3 Menschen.'}]
        write(self.folder/'segments.plan.json',{'segments':self.rows})
        write(self.folder/'translation/input.json',{'segments':self.rows})
        self.calls=[];self.lock=threading.Lock();self.reject=False;self.rewrite=False
        self.adoptions={}
        load=patch('user_context_adoption.load_adoptions',side_effect=lambda *args:copy.deepcopy(self.adoptions))
        load.start();self.addCleanup(load.stop)
        owner=self
        class Client:
            model='offline-three-stage-proxy'
            config={'base_url':'http://127.0.0.1:0/v1'}
            def __init__(self):self.records=[]
            def json(self,system,data):
                with owner.lock:owner.calls.append((system,copy.deepcopy(data)))
                self.records.append({'model':self.model,'text_only':True,'usage':{},'elapsed_s':.001})
                if data.get('purpose')==whole.VERSION:
                    answer={'rows':[{'id':item['id'],'approved':not owner.reject,
                        'issues':[{'type':'translation_mismatch','detail':'有实际依据的数量主体误译。',
                          'source_quote':'3 Menschen','translation_quote':'3人','related_ids':[]}]
                          if owner.reject else []} for item in data['items']]}
                    if owner.rewrite:answer['rows'][0]['text_zh']='试图覆盖用户采纳。'
                    return answer
                return {'translations':[{'id':item['id'],'text_zh':'有3人。','approved':True,'issues':[]}
                                         for item in data['items']]}
        self.factory=lambda root:Client()

    def translate(self):
        with patch.object(proxy,'ProxyClient',side_effect=self.factory):
            after,report=proxy.translate_segments(self.rows,self.root,
                diagnostic_dir=self.folder/'translation/diagnostics')
        write(self.folder/'segments.json',{'segments':after})
        write(self.folder/'translation/report.json',report)
        pipeline.validate_translations(self.rows,after,report,folder=self.folder)
        return after,report

    def test_real_editor_audit_and_fresh_whole_script_are_three_distinct_text_requests(self):
        after,normal=self.translate()
        self.assertEqual(len(self.calls),2)
        old_files={path:path.read_bytes() for path in (self.folder/'translation/diagnostics').glob('*.json')}
        result=whole.ensure_review(self.root,self.folder,after,client_factory=self.factory)
        self.assertEqual(len(self.calls),3)
        self.assertEqual(result['status'],'passed')
        self.assertNotIn('purpose',self.calls[0][1]);self.assertNotIn('purpose',self.calls[1][1])
        self.assertEqual(self.calls[2][1]['purpose'],whole.VERSION)
        self.assertIn('whole_script',self.calls[2][1])
        self.assertNotEqual(self.calls[0][0],self.calls[1][0])
        self.assertNotEqual(self.calls[1][0],self.calls[2][0])
        self.assertFalse(result['human_listened']);self.assertFalse(result['old_rejections_closed'])
        self.assertEqual(len(result['diagnostics']),1)
        for path,before in old_files.items():self.assertEqual(path.read_bytes(),before)
        whole.verify_report(self.root,self.folder,after,result,client_factory=self.factory)

    def test_current_helper_code_change_invalidates_whole_review_cache_without_retranslating(self):
        after,_=self.translate()
        first=whole.ensure_review(self.root,self.folder,after,client_factory=self.factory)
        again=whole.ensure_review(self.root,self.folder,after,client_factory=self.factory)
        self.assertEqual(first,again);self.assertEqual(len(self.calls),3)
        changed=whole._code_identity();changed['autonomous_content_review.py']='f'*64
        with patch.object(whole,'_code_identity',return_value=changed):
            with self.assertRaises(whole.ContentReviewError):
                whole.verify_report(self.root,self.folder,after,first,client_factory=self.factory)
            revised=whole.ensure_review(self.root,self.folder,after,client_factory=self.factory)
        self.assertEqual(len(self.calls),4)
        self.assertEqual(revised['status'],'passed')
        self.assertNotEqual(first['snapshot_sha256'],revised['snapshot_sha256'])

    def test_real_whole_script_rejection_is_separate_from_the_old_proxy_approval(self):
        after,normal=self.translate();self.reject=True
        result=whole.ensure_review(self.root,self.folder,after,client_factory=self.factory)
        self.assertEqual(result['status'],'rejected')
        self.assertEqual(result['findings']['line0001']['target_text_sha256'],
            hashlib.sha256(after[0]['text_zh'].encode()).hexdigest())
        self.assertEqual(json.loads((self.folder/'translation/report.json').read_text()),normal)
        self.assertEqual(normal['review_method'],'two_pass_machine_review_and_numeric_invariants')
        self.assertEqual(len(self.calls),3)

    def test_user_adoption_never_enters_editor_or_retranslation_and_whole_review_cannot_rewrite_it(self):
        self.adoptions={'line0001':{'text_zh':'有3人。','source_binding':proxy.source_binding(self.rows[0]),
            'decision_ref':{'path':'jobs/web_three_stage/actual_user_decision.json','sha256':'a'*64},
            'review_method':'user_contextual_translation_adoption','human_reviewed':False,
            'source_words_verified':False,'proxy_approved':False}}
        after,normal=self.translate()
        self.assertEqual(self.calls,[])
        before=(self.folder/'segments.json').read_bytes()
        self.rewrite=True
        with self.assertRaises(whole.ContentReviewIncomplete):
            whole.ensure_review(self.root,self.folder,after,client_factory=self.factory)
        self.assertEqual(len(self.calls),1)
        request=self.calls[0][1]
        self.assertEqual(request['items'][0]['authority'],'user_context_adoption')
        self.assertEqual(request['items'][0]['user_decision']['text_zh'],'有3人。')
        self.assertEqual((self.folder/'segments.json').read_bytes(),before)
        self.assertFalse((self.folder/whole.REPORT).exists())


if __name__=='__main__':unittest.main()
