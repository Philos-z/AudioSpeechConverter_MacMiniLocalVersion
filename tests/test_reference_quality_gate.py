"""Actual tiny audio: reference failures persist complete, input-bound evidence."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

import numpy as np
import soundfile as sf

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'web'))
import pipeline


class ReferenceQualityGateTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name).resolve();self.folder=self.root/'jobs/fixture'
        (self.folder/'references').mkdir(parents=True)
        video=self.root/'source.mp4';video.write_bytes(b'source')
        self.write('source.json',{'input':str(video)})
        self.rows=[{'segment_id':'seg0000','speaker_id':'S00','start_s':0.,'end_s':1.}]
        self.write('segments.asr.json',{'segments':self.rows})
        self.write('segments.plan.json',{'segments':[{'speaker_id':'S00','source_ids':['seg0000']}]})

    def write(self,name,value):
        (self.folder/name).write_text(json.dumps(value))
    def audio(self,sid,seconds=1.,silent=False,nonfinite=False):
        path=self.folder/'references'/(sid+'.wav');sr=8000
        samples=np.zeros(round(sr*seconds),dtype='float32') if silent else (
            .2*np.sin(np.arange(round(sr*seconds))*2*np.pi*180/sr)).astype('float32')
        if nonfinite:samples[100]=np.nan
        sf.write(path,samples,sr,subtype='FLOAT' if nonfinite else 'PCM_24')
        return {'reference':str(path.relative_to(self.root)),'source_segments':['seg'+sid[1:].zfill(4)]}
    def report(self):return json.loads((self.folder/'speaker_machine_review.json').read_text())

    def test_empty_manifest_is_a_failure_with_complete_evidence(self):
        self.write('speakers.json',{})
        with self.assertRaises(RuntimeError):pipeline.check_speakers(self.root,self.folder)
        report=self.report()
        self.assertTrue(report['complete'])
        self.assertIn('empty_or_invalid_manifest',report['manifest_issues'])

    def test_missing_silent_short_and_nonfinite_references_are_all_reported(self):
        speakers={'S00':{'reference':'missing.wav','source_segments':['seg0000']},
            'S01':self.audio('S01',silent=True),'S02':self.audio('S02',seconds=.7),
            'S03':self.audio('S03',nonfinite=True),'S04':self.audio('S04')}
        self.write('speakers.json',speakers)
        self.write('speaker_assignments.json',{'segments':[{'segment_id':'seg'+str(i).zfill(4),
            'speaker_id':sid,'start_s':i*2.,'end_s':i*2.+1.} for i,sid in enumerate(speakers)]})
        self.write('segments.plan.json',{'segments':[{'speaker_id':sid} for sid in speakers]})
        with self.assertRaisesRegex(RuntimeError,'S00.*S01.*S02.*S03'):
            pipeline.check_speakers(self.root,self.folder)
        rows=self.report()['speakers']
        self.assertEqual(len(rows),5)
        self.assertEqual([r['finite_duration_energy_passed'] for r in rows],[False]*4+[True])
        self.assertEqual(rows[3]['reason_codes'],['nonfinite_audio'])
        self.assertIsNone(rows[3]['peak'])
        self.assertEqual(json.loads((self.folder/'speakers.json').read_text())['S04']['reviewed'],True)

    def test_manifest_must_cover_every_planned_speaker(self):
        self.write('speakers.json',{'S00':self.audio('S00')})
        self.write('segments.plan.json',{'segments':[{'speaker_id':'S00'},{'speaker_id':'S01'}]})
        with self.assertRaises(RuntimeError):pipeline.check_speakers(self.root,self.folder)
        self.assertIn('speaker_coverage_mismatch',self.report()['manifest_issues'])

    def test_valid_audio_is_bound_to_the_source_and_checked_manifest(self):
        self.write('speakers.json',{'S00':self.audio('S00')})
        pipeline.check_speakers(self.root,self.folder)
        report=self.report()
        self.assertTrue(report['speakers'][0]['finite_duration_energy_passed'])
        for relative,expected in report['bindings'].items():
            self.assertEqual(pipeline.digest(self.root/relative),expected)

    def test_empty_plan_or_untraceable_reference_cannot_pass(self):
        self.write('speakers.json',{'S00':self.audio('S00')})
        self.write('segments.plan.json',{'segments':[]})
        self.write('segments.asr.json',{'segments':[]})
        with self.assertRaises(RuntimeError):pipeline.check_speakers(self.root,self.folder)
        self.assertIn('empty_or_invalid_plan',self.report()['manifest_issues'])

    def test_another_speakers_source_ids_cannot_be_borrowed(self):
        item=self.audio('S00')
        self.write('speakers.json',{'S00':item,'S01':dict(item)})
        self.write('speaker_assignments.json',{'segments':self.rows+[
            {'segment_id':'seg0001','speaker_id':'S01','start_s':2.,'end_s':3.}]})
        self.write('segments.plan.json',{'segments':[{'speaker_id':'S00'},{'speaker_id':'S01'}]})
        with self.assertRaises(RuntimeError):pipeline.check_speakers(self.root,self.folder)
        self.assertIn('source_assignment_mismatch',self.report()['speakers'][1]['reason_codes'])

    def single_fixture(self):
        (self.root/'scripts').mkdir()
        shutil.copy2(ROOT/'scripts/speaker_references.py',self.root/'scripts/speaker_references.py')
        source=self.folder/'separated/htdemucs/original/vocals.wav';source.parent.mkdir(parents=True)
        rate=8000;samples=(.2*np.sin(np.arange(3*rate)*2*np.pi*180/rate)).astype('float32')
        sf.write(source,samples,rate,subtype='PCM_24')
        self.write('segments.asr.json',{'segments':[{'segment_id':'seg0000','speaker_id':'S00',
            'start_s':.9,'end_s':1.56,'text_de':'Ein Satz.'}]})
        pipeline.single_speaker(self.root,self.folder)
        return json.loads((self.folder/'speakers.json').read_text())['S00']

    def test_single_speaker_stage_uses_shared_context_and_provenance_checks(self):
        item=self.single_fixture()
        self.assertAlmostEqual(item['reference_window_s'][1]-item['reference_window_s'][0],1.2)
        pipeline.check_speakers(self.root,self.folder)
        self.assertTrue(self.report()['speakers'][0]['source_provenance_checked'])

    def test_selected_finite_audio_replacement_cannot_be_reapproved(self):
        item=self.single_fixture();path=self.root/item['reference']
        samples,rate=sf.read(path,dtype='float32')
        sf.write(path,-samples,rate,subtype='PCM_24')
        with self.assertRaises(RuntimeError):pipeline.check_speakers(self.root,self.folder)
        self.assertIn('reference_identity_mismatch',self.report()['speakers'][0]['reason_codes'])


if __name__=='__main__':unittest.main()
