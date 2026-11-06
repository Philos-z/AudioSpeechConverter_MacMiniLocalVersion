"""Offline pre-reservation and current-request contract boundaries."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'web'),str(ROOT/'tests')]
import translation_recovery as repair
import website_recovery as recovery
import test_translation_recovery as fixtures
import test_failure_triage_local_translation as diagnosis_fixtures
from proxy_client import source_binding


class SavedApprovalPreReservationTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.TranslationRecoveryTests()
        self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.eid=self.f.saved_partial_fixture(whole=True)
        state=self.f.root/'web/state/jobs'/f'{self.f.jid}.json'
        value=recovery._read(state);value['options']={'autonomy_policy':'evidence_first_v1'}
        fixtures.write(state,value)

    def snapshot(self):
        return {str(p.relative_to(self.f.root)):p.read_bytes()
                for p in self.f.root.rglob('*') if p.is_file()}

    def blocked(self,automatic=False):
        entry=repair.run_automatic if automatic else repair.run
        with patch('proxy_client.ProxyClient',side_effect=AssertionError('no Proxy construction')),\
                patch('source_audio_recheck.recheck',side_effect=AssertionError('no ASR')):
            with self.assertRaises(repair.LocalGuardPending) as result:
                entry(self.f.root,self.f.jid)
        return result.exception

    def autonomous_saved_request(self):
        journal=recovery._journal(self.f.folder);event=journal['events'][-1]
        evidence=self.f.folder/'revisions/source_review_v1/evidence'/('translation_'+self.eid)
        request=recovery._read(evidence/'request.json')
        snapshot={'job':self.f.jid,'input_source_sha256':self.f.source_sha,'synthetic':True}
        identity=recovery._hash(snapshot)
        report={'schema':'autonomous_content_review_v1','job':self.f.jid,
            'snapshot':snapshot,'snapshot_sha256':identity}
        history=self.f.folder/'translation/autonomous_content_review/history'/(identity+'.json')
        fixtures.write(history,report)
        fixtures.write(self.f.folder/'translation/autonomous_content_review/report.json',report)
        request['autonomous_content_review']={'path':str(history.relative_to(self.f.folder)),
            'sha256':recovery._sha(history)}
        request['source_recheck_records']={sid:{
            'path':str((evidence/(sid+'_source_recheck.json')).relative_to(self.f.folder)),
            'sha256':recovery._sha(evidence/(sid+'_source_recheck.json'))}
            for sid in event['segment_ids']}
        self.save_request(request,journal,event,evidence)
        return request,journal,event,evidence

    def save_request(self,request,journal,event,evidence):
        event['input_fingerprint']=repair.request_fingerprint(request)
        fixtures.write(evidence/'request.json',request)
        event['saved_review']['request_sha256']=recovery._sha(evidence/'request.json')
        fixtures.write(self.f.folder/'recovery/website_journal.json',journal)

    def assert_bad_auxiliary_replay(self,request,journal,event,evidence):
        self.save_request(request,journal,event,evidence)
        before=self.snapshot()
        with patch('proxy_client.ProxyClient',side_effect=AssertionError('no Proxy')),\
                patch('source_audio_recheck.recheck',side_effect=AssertionError('no ASR')):
            with self.assertRaises(recovery.RecoveryReviewRequired):
                repair.resume_partial_review(self.f.root,self.f.jid,self.eid)
        self.assertEqual(self.snapshot(),before)

    def test_real_auxiliary_refs_replay_after_current_report_changes(self):
        self.autonomous_saved_request()
        replacement={'snapshot_sha256':'e'*64,'synthetic_changed_report':True}
        fixtures.write(self.f.folder/'translation/autonomous_content_review/report.json',replacement)
        before=copy.deepcopy(recovery._journal(self.f.folder))
        with patch('proxy_client.ProxyClient',side_effect=AssertionError('no Proxy')),\
                patch('source_audio_recheck.recheck',side_effect=AssertionError('no ASR')):
            result=repair.resume_partial_review(self.f.root,self.f.jid,self.eid)
        self.assertEqual(result['new_proxy_calls'],0)
        self.assertEqual(result['accepted_ids'],event_ids:=before['events'][-1]['segment_ids'])
        self.assertEqual(len(event_ids),3)
        self.assertEqual(len(recovery._journal(self.f.folder)['events']),len(before['events']))

    def test_fake_auxiliary_path_is_not_ignored_when_real_candidate_exists(self):
        request,journal,event,evidence=self.autonomous_saved_request()
        request['source_recheck_records']['line0001']['path']='revisions/source_review_v1/evidence/another_event/line0001_source_recheck.json'
        self.assert_bad_auxiliary_replay(request,journal,event,evidence)

    def test_fake_auxiliary_sha_is_not_ignored_when_real_candidate_exists(self):
        request,journal,event,evidence=self.autonomous_saved_request()
        request['source_recheck_records']['line0001']['sha256']='0'*64
        self.assert_bad_auxiliary_replay(request,journal,event,evidence)

    def test_missing_auxiliary_file_refuses_without_new_phase(self):
        request,journal,event,evidence=self.autonomous_saved_request()
        (evidence/'line0001_source_recheck.json').unlink()
        self.assert_bad_auxiliary_replay(request,journal,event,evidence)

    def test_whole_ref_cannot_point_at_mutable_current_report(self):
        request,journal,event,evidence=self.autonomous_saved_request()
        path=self.f.folder/'translation/autonomous_content_review/report.json'
        request['autonomous_content_review']={'path':str(path.relative_to(self.f.folder)),
            'sha256':recovery._sha(path)}
        self.assert_bad_auxiliary_replay(request,journal,event,evidence)

    def test_original_whole_history_sha_remains_required(self):
        request,journal,event,evidence=self.autonomous_saved_request()
        request['autonomous_content_review']['sha256']='0'*64
        self.assert_bad_auxiliary_replay(request,journal,event,evidence)

    def test_existing_current_guard_failure_stops_with_unused_budget(self):
        before=self.snapshot()
        with patch('proxy_client.invariants',return_value=['current local guard still blocks']):
            error=self.blocked()
        self.assertTrue(error.checks)
        self.assertEqual(self.snapshot(),before)
        journal=recovery._journal(self.f.folder)
        self.assertEqual(len(journal['events']),3)
        self.assertEqual(sum(event['proxy_calls'] for event in journal['events']),6)

    def test_repeated_automatic_calls_do_not_reserve_or_recheck(self):
        before=self.snapshot()
        with patch('proxy_client.invariants',return_value=['current local guard still blocks']):
            self.blocked(automatic=True);self.blocked(automatic=True)
        self.assertEqual(self.snapshot(),before)

    def test_real_wrong_numbers_remain_blocked_even_if_audit_approved(self):
        journal=recovery._journal(self.f.folder);event=journal['events'][-1]
        evidence=self.f.folder/'revisions/source_review_v1/evidence'/('translation_'+self.eid)
        audit=recovery._read(evidence/'audit.json')
        for row in audit['translations']:row['text_zh']='有2座房屋。'
        fixtures.write(evidence/'audit.json',audit)
        event['saved_review']['audit_sha256']=recovery._sha(evidence/'audit.json')
        fixtures.write(self.f.folder/'recovery/website_journal.json',journal)
        before=self.snapshot();self.blocked()
        self.assertEqual(self.snapshot(),before)
        self.assertFalse((self.f.folder/'translation/source_review_hints.json').exists())

    def test_guard_fix_later_reuses_exact_saved_approval_with_no_new_calls(self):
        with patch('proxy_client.invariants',return_value=['unchanged local condition or negation check']):
            self.blocked()
        before=copy.deepcopy(recovery._journal(self.f.folder))
        with patch('proxy_client.ProxyClient',side_effect=AssertionError('no Proxy')),\
                patch('source_audio_recheck.recheck',side_effect=AssertionError('no ASR')):
            result=repair.run(self.f.root,self.f.jid)
        self.assertEqual(result['event_id'],self.eid)
        self.assertEqual(result['new_proxy_calls'],0)
        after=recovery._journal(self.f.folder)
        self.assertEqual(len(after['events']),len(before['events']))
        self.assertEqual(after['events'][-1]['status'],'failed')
        self.assertEqual(after['events'][-1]['proxy_calls'],2)

    def test_other_policy_keeps_existing_readonly_skip(self):
        path=self.f.root/'web/state/jobs'/f'{self.f.jid}.json'
        state=recovery._read(path);state['options']={};fixtures.write(path,state)
        before=self.snapshot()
        with patch('proxy_client.invariants',return_value=['still blocked']):
            result=repair._try_latest_whole_saved_review(self.f.root,self.f.folder,
                recovery._journal(self.f.folder),self.f.rows,[r['segment_id'] for r in self.f.rows])
        self.assertIsNone(result);self.assertEqual(self.snapshot(),before)

    def test_explicit_scope_is_not_widened_or_mislabeled_as_local_stop(self):
        before=self.snapshot()
        result=repair._try_latest_whole_saved_review(self.f.root,self.f.folder,
            recovery._journal(self.f.folder),self.f.rows,['line0002'])
        self.assertIsNone(result);self.assertEqual(self.snapshot(),before)

    def test_newer_real_refusal_is_not_hidden_by_local_guard_stop(self):
        self.f.later_rejection_fixture(diagnostic=True)
        before=self.snapshot()
        with patch('proxy_client.invariants',return_value=['current guard failure']):
            result=repair._try_latest_whole_saved_review(self.f.root,self.f.folder,
                recovery._journal(self.f.folder),self.f.rows,[r['segment_id'] for r in self.f.rows])
        self.assertIsNone(result);self.assertEqual(self.snapshot(),before)


class CurrentRecoveryRequestContractTests(unittest.TestCase):
    def setUp(self):
        self.f=diagnosis_fixtures.LocalTranslationDiagnosisTests()
        self.f.setUp();self.addCleanup(self.f.doCleanups)

    def current_requests(self,autonomous=False):
        events,_=self.f.journal(reason='invariant_rejected',count=2)
        whole=None
        if autonomous:
            snapshot={'job':self.f.folder.name,'input_source_sha256':self.f.job['source_sha256'],'synthetic':True}
            identity=recovery._hash(snapshot)
            report={'schema':'autonomous_content_review_v1','job':self.f.folder.name,
                'snapshot':snapshot,'snapshot_sha256':identity}
            history=self.f.folder/'translation/autonomous_content_review/history'/(identity+'.json')
            fixtures.write(history,report)
            fixtures.write(self.f.folder/'translation/autonomous_content_review/report.json',report)
            whole={'path':str(history.relative_to(self.f.folder)),'sha256':recovery._sha(history)}
        for event in events:
            evidence=self.f.folder/'revisions/source_review_v1/evidence'/('translation_'+event['id'])
            request=recovery._read(evidence/'request.json')
            request['code']=repair.request_code_identity(self.f.root)
            index=1;row=self.f.rows[index]
            request['items'][0].update(source_binding=source_binding(row),
                previous_context=[r['text_de'] for r in self.f.rows[:index]],
                next_context=[r['text_de'] for r in self.f.rows[index+1:]])
            if autonomous:
                candidate=evidence/'line0002_source_recheck.json'
                fixtures.write(candidate,{'synthetic_source_observation':True})
                request.update(autonomous_content_review=whole,source_recheck_records={'line0002':{
                    'path':str(candidate.relative_to(self.f.folder)),'sha256':recovery._sha(candidate)}})
            event['input_fingerprint']=repair.request_fingerprint(request)
            fixtures.write(evidence/'request.json',request)
            fixtures.write(evidence/'editor.json',{'synthetic_editor':True})
            fixtures.write(evidence/'audit.json',{'synthetic_audit':True})
            fixtures.write(evidence/'local_checks.json',{'checks':{'line0002':['单位未对应']}})
            event['saved_review']={name+'_sha256':recovery._sha(evidence/(name+'.json'))
                for name in ('request','editor','audit')}
        self.events=events;self.save_events()

    def save_events(self):
        path=self.f.folder/'recovery/website_journal.json'
        value=recovery._read(path);value['events']=self.events;fixtures.write(path,value)

    def mutate_requests(self,change,*,repin=True):
        for event in self.events:
            evidence=self.f.folder/'revisions/source_review_v1/evidence'/('translation_'+event['id'])
            request=recovery._read(evidence/'request.json');change(request,evidence)
            fixtures.write(evidence/'request.json',request)
            if repin:event['saved_review']['request_sha256']=recovery._sha(evidence/'request.json')
        self.save_events()

    def assert_not_bound(self):
        before=(self.f.folder/'recovery/website_journal.json').read_bytes()
        result=self.f.diagnose()
        self.assertNotIn('恢复次数已用完',result['title'])
        self.assertNotIn('数字、单位或逻辑检查',result['message'])
        self.assertEqual((self.f.folder/'recovery/website_journal.json').read_bytes(),before)
        self.assertEqual(len(recovery._journal(self.f.folder)['events']),2)

    def test_legacy_four_file_contract_remains_current(self):
        self.f.journal(reason='invariant_rejected',count=2)
        result=self.f.diagnose()
        self.assertIn('恢复次数已用完',result['title'])
        self.assertIn('数字、单位或逻辑检查',result['message'])

    def test_current_five_file_contract_reports_real_local_cause_and_spent_budget(self):
        self.current_requests();result=self.f.diagnose()
        self.assertIn('恢复次数已用完',result['title'])
        self.assertIn('数字、单位或逻辑检查',result['message'])
        self.assertEqual(result['checks'],['单位','数字'])

    def test_autonomous_source_refs_use_same_identity_without_refunding_calls(self):
        self.current_requests(autonomous=True)
        before=(self.f.folder/'recovery/website_journal.json').read_bytes()
        result=self.f.diagnose()
        self.assertIn('恢复次数已用完',result['title'])
        self.assertIn('数字、单位或逻辑检查',result['message'])
        self.assertEqual(before,(self.f.folder/'recovery/website_journal.json').read_bytes())
        event=self.events[0];evidence=self.f.folder/'revisions/source_review_v1/evidence'/('translation_'+event['id'])
        request=recovery._read(evidence/'request.json')
        identity={k:v for k,v in request.items() if k not in {'items','source_recheck_records'}}
        self.assertEqual(repair.request_fingerprint(request),recovery._hash(identity))

    def test_current_contract_missing_binding_is_not_treated_as_legacy(self):
        self.current_requests()
        self.mutate_requests(lambda request,evidence:request['items'][0].pop('source_binding'))
        self.assert_not_bound()

    def test_context_speaker_time_and_source_mismatches_are_rejected(self):
        self.current_requests()
        original=[recovery._read(self.f.folder/'revisions/source_review_v1/evidence'/('translation_'+e['id'])/'request.json')
                  for e in self.events]
        for change in (
                lambda request,evidence:request['items'][0]['source_binding'].update(speaker_id='other'),
                lambda request,evidence:request['items'][0]['source_binding'].update(start_s=99),
                lambda request,evidence:request['items'][0].update(previous_context=['different context']),
                lambda request,evidence:request['items'][0].update(next_context=['different context']),
                lambda request,evidence:request['items'][0].update(text_source='different source')):
            with self.subTest(change=change):
                for event,request in zip(self.events,original):
                    evidence=self.f.folder/'revisions/source_review_v1/evidence'/('translation_'+event['id'])
                    fixtures.write(evidence/'request.json',request)
                self.mutate_requests(change);self.assert_not_bound()

    def test_changed_saved_raw_sha_is_not_a_current_diagnostic(self):
        self.current_requests()
        self.mutate_requests(lambda request,evidence:request['items'][0].update(extra='changed raw'),repin=False)
        self.assert_not_bound()

    def test_code_contract_accepts_no_unknown_or_missing_file_sets(self):
        self.current_requests()
        self.mutate_requests(lambda request,evidence:request['code'].update(unknown_module=None))
        self.assert_not_bound()
        with self.assertRaises(ValueError):repair.request_code_identity(self.f.root,{'proxy_client.py':None})

    def test_wrong_code_hash_source_sha_or_unknown_identity_field_cannot_bind(self):
        for change in (
                lambda request,evidence:request['code'].update({'proxy_client.py':'0'*64}),
                lambda request,evidence:request.update(source_sha256='0'*64),
                lambda request,evidence:request.update(new_identity_field='not in recorded fingerprint')):
            with self.subTest(change=change):
                self.current_requests();self.mutate_requests(change);self.assert_not_bound()

    def test_source_ref_file_tampering_is_not_hidden_by_auxiliary_identity(self):
        self.current_requests(autonomous=True)
        for event in self.events:
            path=self.f.folder/'revisions/source_review_v1/evidence'/('translation_'+event['id'])/'line0002_source_recheck.json'
            path.write_text('{"changed":true}')
        self.assert_not_bound()

    def test_source_refs_cannot_escape_or_claim_missing_scope(self):
        self.current_requests(autonomous=True)
        self.mutate_requests(lambda request,evidence:request['source_recheck_records']['line0002'].update(path='../outside.json'))
        self.assert_not_bound()
        self.mutate_requests(lambda request,evidence:request.pop('autonomous_content_review'))
        self.assert_not_bound()

    def test_changed_whole_report_does_not_reuse_old_auxiliary_request(self):
        self.current_requests(autonomous=True)
        report={'snapshot_sha256':'e'*64}
        fixtures.write(self.f.folder/'translation/autonomous_content_review/report.json',report)
        fixtures.write(self.f.folder/'translation/autonomous_content_review/history'/('e'*64+'.json'),report)
        self.assert_not_bound()

    def test_non_hex_sha_in_code_or_source_refs_is_rejected(self):
        self.current_requests(autonomous=True)
        event=self.events[0];request=recovery._read(self.f.folder/'revisions/source_review_v1/evidence'/('translation_'+event['id'])/'request.json')
        for target in ('code','source_recheck_records'):
            malformed=copy.deepcopy(request)
            if target=='code':malformed['code']['proxy_client.py']='not a sha'
            else:malformed['source_recheck_records']['line0002']['sha256']='not a sha'
            with self.subTest(target=target),self.assertRaises(ValueError):repair.request_fingerprint(malformed)


if __name__=='__main__':unittest.main()
