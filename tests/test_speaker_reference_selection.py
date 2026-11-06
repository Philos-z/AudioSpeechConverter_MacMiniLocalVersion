"""Real WAV probes for same-label fallback, immutable assets and bound reports."""
import hashlib
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np
import soundfile as sf

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from speaker_references import (ReferenceSelectionError, prepare_reference,
                                reference_bindings, selection_provenance_errors,
                                write_selection_report)
import speaker_references
import speaker_plan


class ReferenceSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='reference-selection-test-')
        self.root=Path(self.temporary.name).resolve()
        self.folder=self.root/'jobs'/'probe';(self.folder/'references').mkdir(parents=True)
        self.source=self.folder/'vocals.wav';self.rate=16000
        self.audio=np.zeros(9*self.rate,dtype='float32')
        self.rows=[self.row('first','A',.1,4.1),self.row('middle','B',4.5,6.),
                   self.row('second','A',6.5,8.5)]
        self.voiced(6.5,8.5)
        self.write_audio()
        (self.root/'source.mp4').write_bytes(b'original source identity')
        self.save('source.json',{'input':str(self.root/'source.mp4'),'duration_s':9})
        self.save('segments.asr.json',{'segments':self.rows})

    def tearDown(self):
        self.temporary.cleanup()

    def row(self,sid,speaker,start,end,**extra):
        return {'segment_id':sid,'speaker_id':speaker,'start_s':start,'end_s':end,**extra}

    def save(self,name,data):
        (self.folder/name).write_text(json.dumps(data))

    def voiced(self,start,end):
        first,last=round(start*self.rate),round(end*self.rate)
        self.audio[first:last]=.2*np.sin(2*np.pi*220*np.arange(last-first)/self.rate)

    def write_audio(self,subtype='PCM_24'):
        sf.write(self.source,self.audio,self.rate,subtype=subtype)

    def prepare(self,rows=None,speaker='A'):
        return prepare_reference(self.root,self.folder,self.source,self.rows if rows is None else rows,speaker)

    def test_silent_longest_uses_valid_same_label_run_and_preserves_source(self):
        before=json.loads(json.dumps(self.rows));item=self.prepare()
        self.assertEqual(item['reference_selection']['selected_source_ids'],['second'])
        self.assertEqual(item['reference_selection']['candidate_failures'][0]['reason_codes'],['insufficient_energy'])
        self.assertEqual(self.rows,before)
        self.assertEqual(item['source_segments'],['first','second'])

    def test_nonfinite_longest_uses_valid_same_label_run(self):
        self.audio[2000:2100]=float('nan');self.write_audio('FLOAT')
        item=self.prepare()
        self.assertEqual(item['reference_selection']['selected_source_ids'],['second'])
        self.assertEqual(item['reference_selection']['candidate_failures'][0]['reason_codes'],['non_finite_audio'])

    def test_no_other_label_can_supply_an_invalid_speaker(self):
        with self.assertRaises(ReferenceSelectionError) as caught:self.prepare(speaker='B')
        self.assertEqual(caught.exception.speaker_id,'B')
        self.assertEqual(caught.exception.source_ids,['middle'])
        self.assertEqual(caught.exception.candidate_failures[0]['reason_codes'],['insufficient_energy'])

    def test_overlap_candidate_is_rejected_even_when_it_has_loud_audio(self):
        self.rows[2]['overlap']=True
        with self.assertRaises(ReferenceSelectionError) as caught:self.prepare()
        codes=[code for candidate in caught.exception.candidate_failures for code in candidate['reason_codes']]
        self.assertIn('overlapping_speech',codes)

    def test_subrow_fallback_can_avoid_a_bad_part_of_a_long_run(self):
        rows=[self.row('silence','A',.1,4.1),self.row('voice','A',4.6,8.5)]
        self.voiced(4.6,8.5);self.write_audio()
        # The first eight seconds are finite but this test forces an invalid
        # combined run through an explicit overlap marker on its first row.
        rows[0]['overlap']=True
        item=self.prepare(rows)
        self.assertEqual(item['reference_selection']['selected_source_ids'],['voice'])

    def test_single_short_row_uses_real_bounded_context(self):
        rows=[self.row('short','A',6.6,7.26)]
        item=self.prepare(rows)
        self.assertAlmostEqual(item['reference_window_s'][1]-item['reference_window_s'][0],1.2)
        self.assertEqual(item['reference_context']['synthetic_padding_samples'],0)
        self.assertEqual(item['reference_selection']['selected_source_ids'],['short'])

    def test_tiny_row_does_not_gain_duration_from_silence(self):
        with self.assertRaises(ReferenceSelectionError) as caught:
            self.prepare([self.row('tiny','A',6.6,6.9)])
        self.assertEqual(caught.exception.candidate_failures[0]['reason_codes'],['duration_too_short'])

    def test_existing_identical_long_reference_is_reused_byte_for_byte(self):
        rows=[self.row('voice','A',6.5,8.5)]
        path=self.folder/'references/A.wav'
        with sf.SoundFile(self.source) as handle:
            handle.seek(round(6.47*self.rate))
            samples=handle.read(round(2.06*self.rate),dtype='float32',always_2d=True).mean(axis=1)
        sf.write(path,samples,self.rate,subtype='PCM_24')
        before=path.read_bytes();mtime=path.stat().st_mtime_ns
        item=self.prepare(rows)
        self.assertEqual(item['reference'],str(path.relative_to(self.root)))
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(path.stat().st_mtime_ns,mtime)

    def test_changed_reference_gets_new_asset_and_repeat_is_stable(self):
        old=self.folder/'references/A.wav';old.write_bytes(b'preserve prior evidence')
        item=self.prepare();new=self.root/item['reference'];mtime=new.stat().st_mtime_ns
        self.assertNotEqual(new,old)
        self.assertEqual(old.read_bytes(),b'preserve prior evidence')
        self.assertEqual(self.prepare()['reference'],item['reference'])
        self.assertEqual(new.stat().st_mtime_ns,mtime)

    def test_interrupted_publish_exposes_no_partial_final_file(self):
        with mock.patch.object(speaker_references.os,'link',side_effect=OSError('publish interrupted')):
            with self.assertRaises(ReferenceSelectionError):self.prepare()
        self.assertEqual(list((self.folder/'references').iterdir()),[])
        item=self.prepare()
        self.assertTrue((self.root/item['reference']).is_file())

    def test_old_incomplete_digest_file_is_preserved_and_bypassed(self):
        old=self.folder/'references/A.wav';old.write_bytes(b'old reference')
        first=self.prepare();short_path=self.root/first['reference'];short_path.write_bytes(b'old partial file')
        second=self.prepare();long_path=self.root/second['reference']
        self.assertNotEqual(long_path,short_path)
        self.assertEqual(short_path.read_bytes(),b'old partial file')
        self.assertEqual(self.prepare()['reference'],second['reference'])

    def test_empty_invalid_and_nonfinite_inputs_fail_with_fixed_codes(self):
        for rows,code in (([],'empty_assignments'),([self.row('bad','A',float('nan'),1)],'non_finite_time'),
                          ([self.row('bad','A',2,1)],'invalid_source_window')):
            with self.assertRaises(ReferenceSelectionError) as caught:self.prepare(rows)
            self.assertEqual(caught.exception.reason_codes,[code])

    def test_report_binds_real_source_and_current_optional_manifests(self):
        item=self.prepare();self.save('speakers.json',{'A':item});self.save('segments.plan.json',{'segments':self.rows})
        self.save('speaker_assignments.json',{'segments':self.rows})
        report=write_selection_report(self.root,self.folder,self.source,{'A':item},[])
        self.assertEqual(report['schema'],'speaker_reference_selection_v1')
        self.assertTrue(report['complete'])
        for path in (self.source,self.root/'source.mp4',self.folder/'source.json',
                     self.folder/'segments.asr.json',self.folder/'speakers.json',
                     self.folder/'segments.plan.json',self.folder/'speaker_assignments.json'):
            self.assertEqual(report['bindings'][str(path.relative_to(self.root))],hashlib.sha256(path.read_bytes()).hexdigest())
        (self.root/'source.mp4').write_bytes(b'changed source')
        self.assertNotEqual(report['bindings'],reference_bindings(self.root,self.folder,self.source))

    def test_failure_replaces_stale_success_report_and_preserves_history(self):
        item=self.prepare();write_selection_report(self.root,self.folder,self.source,{'A':item},[])
        with self.assertRaises(ReferenceSelectionError) as caught:self.prepare(speaker='B')
        report=write_selection_report(self.root,self.folder,self.source,{},[caught.exception])
        self.assertFalse(report['speakers'][0]['selected'])
        self.assertEqual(report['speakers'][0]['speaker_id'],'B')
        self.assertEqual(len(list((self.folder/'reference_selection_history').glob('*.json'))),1)

    def provenance(self,item,speaker='A',**kwargs):
        samples,rate=sf.read(self.root/item['reference'],dtype='float32')
        return selection_provenance_errors(self.root,self.source,self.rows,speaker,item,
                                           kwargs.get('samples',samples),kwargs.get('rate',rate))

    def test_new_selection_provenance_and_legacy_compatibility(self):
        item=self.prepare()
        self.assertEqual(self.provenance(item),[])
        legacy={k:v for k,v in item.items() if k!='reference_selection'}
        self.assertEqual(self.provenance(legacy),[])

    def test_same_audio_cannot_be_borrowed_by_another_speaker(self):
        item=self.prepare()
        self.assertEqual(self.provenance(item,speaker='B'),['selection_speaker_mismatch'])

    def test_source_path_and_reference_hash_changes_are_rejected(self):
        item=self.prepare();changed=json.loads(json.dumps(item))
        changed['reference_source']='other_source.wav'
        self.assertIn('selection_source_mismatch',self.provenance(changed))
        changed=json.loads(json.dumps(item));changed['reference_selection']['reference_sha256']='0'*64
        self.assertIn('reference_identity_mismatch',self.provenance(changed))

    def test_missing_selected_ids_and_changed_window_or_format_are_rejected(self):
        item=self.prepare()
        for ids in ([],['absent'],['second','second']):
            changed=json.loads(json.dumps(item));changed['reference_selection']['selected_source_ids']=ids
            self.assertIn('invalid_selection_source_ids',self.provenance(changed))
        changed=json.loads(json.dumps(item));changed['reference_window_s'][0]+=.02
        self.assertIn('selection_window_mismatch',self.provenance(changed))
        self.assertIn('selection_audio_format_mismatch',self.provenance(item,rate=8000))

    def test_finite_changed_source_audio_cannot_reapprove_old_reference(self):
        item=self.prepare();self.audio[7*self.rate:7*self.rate+1000]*=.5;self.write_audio()
        self.assertIn('selection_source_audio_mismatch',self.provenance(item))

    def planner_fixture(self,rows):
        (self.folder/'audio').mkdir(exist_ok=True)
        sf.write(self.folder/'audio/original.wav',np.zeros_like(self.audio),self.rate,subtype='PCM_24')
        self.save('segments.asr.json',{'segments':rows})
        fingerprint=hashlib.sha256((self.folder/'segments.asr.json').read_bytes()).hexdigest()
        np.savez(self.folder/'speaker_embeddings.npz',vectors=np.array([[1.,0.],[0.,1.],[1.,0.]]),fingerprint=fingerprint)
        return ['speaker_plan','--root',str(self.root),'--job','probe']

    def test_planner_collects_every_failed_label_before_committing_failure_report(self):
        argv=self.planner_fixture(self.rows)
        with mock.patch.object(sys,'argv',argv),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError,'S00, S01'):speaker_plan.main()
        report=json.loads((self.folder/'speaker_reference_selection.json').read_text())
        self.assertTrue(report['complete'])
        self.assertEqual([row['speaker_id'] for row in report['failures']],['S00','S01'])
        self.assertTrue(all(row['candidate_failures'] for row in report['failures']))

    def test_empty_planner_source_fails_before_loading_any_model(self):
        argv=self.planner_fixture([])
        with mock.patch.object(sys,'argv',argv):
            with self.assertRaisesRegex(ValueError,'empty_assignments'):speaker_plan.main()


if __name__=='__main__':unittest.main()
