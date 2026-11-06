"""A bad approved row stays pending while independently safe rows can commit."""
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
import pipeline


class SavedGuardSubsetTests(unittest.TestCase):
    def fixture(self):
        f=fixtures.TranslationRecoveryTests('test_fresh_job_without_revision_recovers_only_actual_rejected_row')
        f.setUp();self.addCleanup(f.doCleanups)
        eid=f.saved_partial_fixture(whole=True,bad_target=True)
        path=f.root/'web/state/jobs'/f'{f.jid}.json'
        state=json.loads(path.read_text());state['options']={'autonomy_policy':'evidence_first_v1'}
        fixtures.write(path,state)
        return f,eid

    def test_valid_subset_commits_without_new_calls_and_bad_number_stays_pending(self):
        f,eid=self.fixture();journal_before=copy.deepcopy(recovery._journal(f.folder))
        result=repair.resume_partial_review(f.root,f.jid,eid)
        self.assertEqual(result['accepted_ids'],['line0002','line0003'])
        self.assertEqual(result['rejected_ids'],['line0001'])
        self.assertEqual(result['new_proxy_calls'],0)
        journal=recovery._journal(f.folder)
        self.assertEqual(len(journal['events']),len(journal_before['events']))
        self.assertEqual(journal['events'][-1]['status'],'failed')
        phase=journal['events'][-1]['partial_review']
        self.assertEqual(phase['local_guard_pending_ids'],['line0001'])
        repair._partial_phase_valid(f.folder,journal['events'][-1],phase)
        hints=recovery._read(f.folder/'translation/source_review_hints.json')['rows']
        self.assertEqual(set(hints),{'line0002','line0003'})
        rows,_=pipeline.reviewed_source_plan(f.folder,f.folder/'segments.plan.json',f.source_sha)
        self.assertIn('line0001',repair.source_pending(f.folder,rows))

    def test_guard_partition_tamper_is_rejected(self):
        f,eid=self.fixture();repair.resume_partial_review(f.root,f.jid,eid)
        event=recovery._journal(f.folder)['events'][-1]
        partition=f.folder/event['partial_review']['guard_partition']['path']
        data=recovery._read(partition);data['checks']['line0001']=[]
        fixtures.write(partition,data)
        with self.assertRaises(recovery.RecoveryReviewRequired):
            repair._partial_phase_valid(f.folder,event,event['partial_review'])

    def test_no_valid_rows_does_not_create_a_phase(self):
        f,eid=self.fixture();before=(f.folder/'recovery/website_journal.json').read_bytes()
        with patch('proxy_client.invariants',return_value=['actual failure']):
            with self.assertRaises(recovery.RecoveryReviewRequired):
                repair.resume_partial_review(f.root,f.jid,eid)
        self.assertEqual((f.folder/'recovery/website_journal.json').read_bytes(),before)


if __name__=='__main__':unittest.main()
