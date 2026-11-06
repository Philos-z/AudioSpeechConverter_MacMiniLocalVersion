"""Current reference evidence yields a useful diagnosis without recovery rights."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'web'))
import failure_triage as triage
import server


class ReferenceDiagnosisTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name).resolve();self.folder=self.root/'jobs/fixture'
        self.folder.mkdir(parents=True)
        self.video=self.root/'source.mp4';self.video.write_bytes(b'owned source')
        self.write('source.json',{'input':str(self.video)})
        self.write('segments.asr.json',{'segments':[{'segment_id':'seg0001','start_s':2.,'end_s':2.66}]})
        self.write('speakers.json',{'S21':{'reference':'missing.wav'}})
        self.job={'id':'fixture','source_sha256':self.sha(self.video)}
        self.report={'schema':'speaker_reference_review_v2','complete':True,
            'bindings':{str(path.relative_to(self.root)):self.sha(path)
                for path in (self.video,self.folder/'source.json',self.folder/'segments.asr.json',self.folder/'speakers.json')},
            'speakers':[{'speaker_id':'S21','source_ids':['seg0001'],
                         'finite_duration_energy_passed':False,'reason_codes':['invalid_duration']}],
            'manifest_issues':[]}
        self.write('speaker_machine_review.json',self.report)

    def sha(self,path):return hashlib.sha256(path.read_bytes()).hexdigest()
    def write(self,name,value):
        (self.folder/name).write_text(json.dumps(value,ensure_ascii=False))
    def diagnose(self):
        return triage.make_failure_record(self.root,self.job,RuntimeError('owned child failed'),stage='检查参考声音')

    def test_specific_speaker_and_source_window_are_bound_and_repeat_is_blocked(self):
        record=self.diagnose()
        self.assertEqual(record['category'],'reference')
        self.assertEqual(record['speaker_ids'],['S21'])
        self.assertEqual(record['source_windows'],[{'id':'S21','speaker_id':'S21',
            'start_s':2.,'end_s':2.66,'checks':['参考声音']}])
        self.assertIsNone(record['completed'])
        self.job['failure_record']=record
        self.assertFalse(triage.public_failure(self.root,self.job)['can_retry'])
        self.write('speakers.json',{'S21':{'reference':'new_valid.wav'}})
        self.assertTrue(triage.public_failure(self.root,self.job)['can_retry'])

    def test_stale_source_media_or_reference_manifest_does_not_invent_a_diagnosis(self):
        self.video.write_bytes(b'different source')
        self.assertEqual(self.diagnose()['category'],'unknown')

    def test_external_or_incomplete_report_cannot_name_a_current_failure(self):
        self.report['bindings']['../external.wav']='a'*64
        self.write('speaker_machine_review.json',self.report)
        self.assertEqual(self.diagnose()['category'],'unknown')
        self.report['bindings'].pop('../external.wav');self.report['complete']=False
        self.write('speaker_machine_review.json',self.report)
        self.assertEqual(self.diagnose()['category'],'unknown')

    def test_empty_manifest_is_not_translation_progress_or_an_automatic_budget_permission(self):
        self.report['speakers']=[];self.report['manifest_issues']=['empty_or_invalid_manifest']
        self.write('speaker_machine_review.json',self.report)
        record=self.diagnose()
        self.assertEqual(record['category'],'reference')
        self.assertIn('清单不完整',record['message'])
        self.assertIsNone(record['total'])
        self.assertTrue(server.recovery_needs_text_budget('reference'))

    def test_report_text_cannot_become_a_public_reason_or_transport_failure(self):
        self.report['speakers'][0]['reason_codes']=['secret-token /example-user HTTP503']
        self.write('speaker_machine_review.json',self.report)
        record=self.diagnose();self.job['failure_record']=record
        public=triage.public_failure(self.root,self.job)
        self.assertEqual(public['category'],'reference')
        self.assertNotIn('secret-token',json.dumps(public))

    def test_missing_reference_repaired_at_original_path_unlocks_retry(self):
        self.job['failure_record']=self.diagnose()
        self.assertFalse(triage.public_failure(self.root,self.job)['can_retry'])
        (self.root/'missing.wav').write_bytes(b'restored reference')
        self.assertTrue(triage.public_failure(self.root,self.job)['can_retry'])

    def test_omitted_manifest_binding_and_malformed_asr_do_not_break_diagnosis(self):
        self.report['bindings'].pop(str((self.folder/'speakers.json').relative_to(self.root)))
        self.write('speaker_machine_review.json',self.report)
        self.assertEqual(self.diagnose()['category'],'unknown')
        self.report['bindings'][str((self.folder/'speakers.json').relative_to(self.root))]=self.sha(self.folder/'speakers.json')
        self.write('segments.asr.json',[])
        self.report['bindings'][str((self.folder/'segments.asr.json').relative_to(self.root))]=self.sha(self.folder/'segments.asr.json')
        self.write('speaker_machine_review.json',self.report)
        self.assertEqual(self.diagnose()['category'],'unknown')


if __name__=='__main__':unittest.main()
