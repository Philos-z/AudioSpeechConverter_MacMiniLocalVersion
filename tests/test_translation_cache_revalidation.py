"""Cache/diagnostic restart behavior; no real Proxy, media or model calls."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'web'))
import proxy_client as proxy
import translation_recovery
import failure_triage


def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False))


class Client:
    model='offline-cache-review'
    def __init__(self,approved=True):self.records=[];self.calls=[];self.approved=approved
    def json(self,system,data):
        self.calls.append(copy.deepcopy(data))
        return {'translations':[{'id':item['id'],'text_zh':'有3人。',
            'approved':self.approved,'issues':[] if self.approved else ['原文词义无法确认']}
            for item in data['items']]}


class TranslationCacheRevalidationTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.rows=[{'segment_id':'line0001','text_de':'Es sind 3 Menschen.',
            'speaker_id':'S01','start_s':0.,'end_s':2.,'source_language':'de'}]
        self.folder=self.root/'jobs/web_fixture';self.diagnostics=self.folder/'translation/diagnostics'
        self.client=Client()

    def run_translation(self):
        with patch.object(proxy,'ProxyClient',return_value=self.client):
            return proxy.translate_segments(self.rows,self.root,diagnostic_dir=self.diagnostics)

    def seed(self,kind='language_bound'):
        self.run_translation();self.assertIn(len(self.client.calls),(0,2))
        path=next((self.root/'cache'/proxy.CACHE_NAMESPACE).glob('*.json'))
        value=json.loads(path.read_text())
        if kind!='language_bound':
            path.unlink();value['input'].pop('source_language');value['input'].pop('source_language_policy')
            for item in value['input']['items']:
                item.pop('text_source');item.pop('source_language')
                if kind=='legacy_german':item.pop('source_binding');item.pop('previous_source_binding')
            if kind=='legacy_german':value['input']['version']=proxy.LEGACY_VERSION
            key=hashlib.sha256(json.dumps(value['input'],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            namespace=proxy.LEGACY_CACHE_NAMESPACE if kind=='legacy_german' else proxy.CACHE_NAMESPACE
            path=self.root/'cache'/namespace/(key+'.json')
            write(path,value)
        self.client.calls=[]
        return path,value

    def checkpoint(self):return json.loads((self.folder/'translation/checkpoint.json').read_text())

    def test_all_cache_generations_recheck_then_count_and_keep_original_bytes(self):
        for kind in ('language_bound','prior_german','legacy_german'):
            with self.subTest(kind=kind):
                path,value=self.seed(kind);before=path.read_bytes()
                updated,report=self.run_translation()
                self.assertEqual(self.client.calls,[]);self.assertEqual(report['cache_batches'],1)
                self.assertEqual(updated[0]['text_zh'],'有3人。');self.assertEqual(path.read_bytes(),before)
                self.assertEqual(report['cache_revalidation'][0]['kind'],kind)
                self.assertEqual(report['cache_revalidation'][0]['status'],'verified')
                batch=self.checkpoint()['batches']['line0001']
                self.assertEqual(batch['cache_file'],str(path.relative_to(self.root)))
                self.assertEqual(batch['cache_file_sha256'],hashlib.sha256(before).hexdigest())
                path.unlink()

    def test_invalid_cache_exposes_only_failed_rows_without_calls_or_file_edits(self):
        path,value=self.seed();value['translations']['line0001']='有4人。'
        value.pop('invariant_policy',None);write(path,value);before=path.read_bytes()
        with self.assertRaises(proxy.InvariantRejectedError) as caught:self.run_translation()
        self.assertEqual(set(caught.exception.failures),{'line0001'})
        checkpoint=self.checkpoint();batch=checkpoint['batches']['line0001']
        self.assertEqual(checkpoint['completed'],0);self.assertEqual(checkpoint['cache_revalidation'],[])
        self.assertEqual(batch['error_type'],'InvariantRejectedError')
        self.assertTrue(batch['checks']['line0001'])
        self.assertEqual(batch['cache_revalidation']['checked_invariant_policy'],proxy.INVARIANT_POLICY)
        self.assertEqual(batch['cache_revalidation']['sha256'],hashlib.sha256(before).hexdigest())
        self.assertEqual(list(translation_recovery.pending(self.folder,self.rows)),['line0001'])
        self.assertEqual(self.client.calls,[]);self.assertEqual(path.read_bytes(),before)

    def test_cache_policy_label_cannot_bypass_current_checks(self):
        path,value=self.seed();value['translations']['line0001']='有4人。'
        value['invariant_policy']=proxy.INVARIANT_POLICY;write(path,value)
        with self.assertRaises(proxy.InvariantRejectedError):self.run_translation()
        self.assertEqual(self.client.calls,[])

    def test_structurally_invalid_cache_never_qualifies_as_content_recovery(self):
        path,value=self.seed()
        for invalid in (None,{}, {'line0001':''},{'line0001':3},{'line0001':'有3人。','other':'多余'}):
            with self.subTest(invalid=invalid):
                value['translations']=invalid;write(path,value);before=path.read_bytes()
                with self.assertRaises(RuntimeError) as caught:self.run_translation()
                self.assertNotIsInstance(caught.exception,proxy.InvariantRejectedError)
                self.assertEqual(translation_recovery.pending(self.folder,self.rows),{})
                self.assertEqual(self.checkpoint()['completed'],0)
                self.assertEqual(self.client.calls,[]);self.assertEqual(path.read_bytes(),before)

    def test_old_generation_content_failure_keeps_its_actual_fingerprint_path(self):
        path,value=self.seed('legacy_german');value['translations']['line0001']='有4人。';write(path,value)
        with self.assertRaises(proxy.InvariantRejectedError):self.run_translation()
        write(self.folder/'translation/input.json',{'segments':self.rows})
        job={'id':'web_fixture','source_sha256':'a'*64}
        before=failure_triage._revision_fingerprint(self.root,job)
        value['translations']['line0001']='有3人。';write(path,value)
        self.assertNotEqual(failure_triage._revision_fingerprint(self.root,job),before)

    def test_rejected_exact_requests_reuse_evidence_and_preserve_failure(self):
        self.client.approved=False
        with self.assertRaises(proxy.ReviewRejectedError):self.run_translation()
        self.assertEqual(len(self.client.calls),3)
        evidence={p:p.read_bytes() for p in self.diagnostics.glob('*.json')}
        self.client.calls=[]
        with self.assertRaises(proxy.ReviewRejectedError):self.run_translation()
        self.assertEqual(self.client.calls,[])
        self.assertEqual(self.checkpoint()['completed'],0)
        self.assertEqual([entry['review_status'] for entry in self.checkpoint()['diagnostic_reuse']],
                         ['draft','review_rejected','review_rejected'])
        self.assertTrue(all(p.read_bytes()==before for p,before in evidence.items()))
        self.assertFalse(list((self.root/'cache').rglob('*.json')))

    def test_latest_rejection_is_not_skipped_for_older_same_input_approval(self):
        self.client.approved=False
        with self.assertRaises(proxy.ReviewRejectedError):self.run_translation()
        records=[(p,json.loads(p.read_text())) for p in self.diagnostics.glob('*_repair_*.json')]
        path,record=records[0];old=copy.deepcopy(record)
        old['response']['translations'][0].update(approved=True,issues=[])
        write(path.with_name(path.name.split('_repair_')[0]+'_repair_0000000000.json'),old)
        self.client.calls=[]
        with self.assertRaises(proxy.ReviewRejectedError):self.run_translation()
        self.assertEqual(self.client.calls,[])
        self.assertEqual(self.checkpoint()['diagnostic_reuse'][-1]['review_status'],'review_rejected')

    def test_changed_source_request_does_not_reuse_old_diagnostic(self):
        self.client.approved=False
        with self.assertRaises(proxy.ReviewRejectedError):self.run_translation()
        self.client.calls=[];self.rows[0]['speaker_id']='S02'
        with self.assertRaises(proxy.ReviewRejectedError):self.run_translation()
        self.assertEqual(len(self.client.calls),3)
        self.assertEqual(self.checkpoint()['diagnostic_reuse'],[])

    def test_different_draft_cannot_reuse_same_key_audit_response(self):
        self.client.approved=False
        with self.assertRaises(proxy.ReviewRejectedError):self.run_translation()
        path=next(self.diagnostics.glob('*_audit_*.json'));record=json.loads(path.read_text())
        record['input']['items'][0]['draft_zh']='不同的旧草稿。';write(path,record)
        self.client.calls=[]
        with self.assertRaises(proxy.ReviewRejectedError):self.run_translation()
        self.assertEqual(len(self.client.calls),1)
        self.assertEqual(self.client.calls[0]['items'][0]['draft_zh'],'有3人。')

    def test_bad_cached_batch_does_not_cancel_verified_sibling(self):
        self.rows=[{**self.rows[0],'segment_id':f'line{i:04d}','start_s':i*3.,'end_s':i*3.+2.}
                   for i in range(1,10)]
        self.run_translation();self.client.calls=[]
        path=next(path for path in (self.root/'cache'/proxy.CACHE_NAMESPACE).glob('*.json')
                  if 'line0001' in json.loads(path.read_text())['translations'])
        value=json.loads(path.read_text());value['translations']['line0003']='有4人。';write(path,value)
        before=path.read_bytes()
        with self.assertRaises(proxy.InvariantRejectedError):self.run_translation()
        checkpoint=self.checkpoint();self.assertEqual(checkpoint['completed'],1)
        self.assertEqual(checkpoint['batches']['line0009']['status'],'verified')
        self.assertEqual(list(translation_recovery.pending(self.folder,self.rows)),['line0003'])
        self.assertEqual(self.client.calls,[]);self.assertEqual(path.read_bytes(),before)

    def test_semantic_refusal_cannot_hide_numeric_failure_of_an_approved_sibling(self):
        self.rows.append({**self.rows[0],'segment_id':'line0002','start_s':3.,'end_s':5.})
        calls=[]
        def respond(system,data):
            calls.append(copy.deepcopy(data))
            return {'translations':[
                {'id':'line0001','text_zh':'有3人。','approved':False,'issues':['原文词义无法确认']},
                {'id':'line0002','text_zh':'有4人。','approved':True,'issues':[]}]}
        self.client.json=respond
        with self.assertRaises(proxy.ReviewRejectedError) as caught:self.run_translation()
        self.assertEqual(len(calls),3)
        self.assertEqual(set(caught.exception.failures),{'line0002'})
        self.assertEqual([row['id'] for row in caught.exception.rejections],['line0001'])
        pending=translation_recovery.pending(self.folder,self.rows)
        self.assertEqual(set(pending),{'line0001','line0002'})
        self.assertTrue(pending['line0001']['review_issues']);self.assertFalse(pending['line0001']['checks'])
        self.assertTrue(pending['line0002']['checks']);self.assertFalse(pending['line0002']['review_issues'])
        self.assertEqual(self.checkpoint()['completed'],0)
        self.assertFalse(list((self.root/'cache').rglob('*.json')))

    def test_same_row_keeps_both_semantic_and_numeric_failure(self):
        def respond(system,data):
            return {'translations':[{'id':'line0001','text_zh':'有4人。',
                'approved':False,'issues':['原文词义无法确认']}]}
        self.client.json=respond
        with self.assertRaises(proxy.ReviewRejectedError):self.run_translation()
        issue=translation_recovery.pending(self.folder,self.rows)['line0001']
        self.assertTrue(issue['checks']);self.assertTrue(issue['review_issues'])

    def test_approval_flag_with_unresolved_issues_remains_pending(self):
        def respond(system,data):
            return {'translations':[{'id':'line0001','text_zh':'有3人。',
                'approved':True,'issues':['原文词义仍待核对']}]}
        self.client.json=respond
        with self.assertRaises(proxy.ReviewRejectedError):self.run_translation()
        self.assertEqual(list(translation_recovery.pending(self.folder,self.rows)),['line0001'])

    def test_flag_with_source_issues_survives_only_another_row_changing(self):
        self.rows.append({**self.rows[0],'segment_id':'line0002','start_s':3.,'end_s':5.})
        def respond(system,data):
            return {'translations':[{'id':item['id'],'text_zh':'有3人。','approved':True,
                'issues':['原文词义无法确认'] if item['id']=='line0001' else []} for item in data['items']]}
        self.client.json=respond
        with self.assertRaises(proxy.ReviewRejectedError):self.run_translation()
        self.rows[1]['text_de']='Es sind 4 Menschen.'
        self.assertIn('line0001',translation_recovery.pending(self.folder,self.rows,include_history=True))
        self.assertIn('line0001',translation_recovery.source_pending(self.folder,self.rows))

    def test_changed_local_checks_can_retain_a_refusal_without_repeating_request(self):
        self.client.approved=False
        with self.assertRaises(proxy.ReviewRejectedError):self.run_translation()
        path=next(self.diagnostics.glob('*_repair_*.json'));record=json.loads(path.read_text())
        record['input']['items'][0]['checks']=['以前的本地规则误报'];write(path,record)
        before=path.read_bytes();self.client.calls=[]
        with self.assertRaises(proxy.ReviewRejectedError):self.run_translation()
        self.assertEqual(self.client.calls,[]);self.assertEqual(path.read_bytes(),before)
        self.assertEqual(self.checkpoint()['diagnostic_reuse'][-1]['request_match'],'same_content_local_checks_changed')
        self.assertEqual(self.checkpoint()['completed'],0)

    def test_changed_checks_cannot_reuse_an_approval_or_changed_source_content(self):
        self.client.approved=False
        with self.assertRaises(proxy.ReviewRejectedError):self.run_translation()
        path=next(self.diagnostics.glob('*_repair_*.json'));record=json.loads(path.read_text())
        record['input']['items'][0]['checks']=['以前的本地规则误报']
        record['response']['translations'][0].update(approved=True,issues=[]);write(path,record)
        self.client.calls=[]
        with self.assertRaises(proxy.ReviewRejectedError):self.run_translation()
        self.assertEqual(len(self.client.calls),1)
        for candidate in self.diagnostics.glob('*_repair_*.json'):candidate.unlink()
        record['response']['translations'][0].update(approved=False,issues=['原文仍有疑点'])
        record['input']['items'][0]['draft_zh']='不同的旧草稿。';write(path,record)
        self.client.calls=[]
        with self.assertRaises(proxy.ReviewRejectedError):self.run_translation()
        self.assertEqual(len(self.client.calls),1)


if __name__=='__main__':unittest.main()
