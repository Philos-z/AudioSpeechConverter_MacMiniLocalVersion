"""Offline planner boundaries for intact complete sentences; no TTS or API."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'web'))
import generic_group_plan as planner


class LongCompleteSentenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.job='offline_complete_sentence'
        self.folder=self.root/'jobs'/self.job;self.folder.mkdir(parents=True)
        self.video=self.root/'source.mp4';self.video.write_bytes(b'offline source fixture')
        self.reference=self.folder/'reference.wav';self.reference.write_bytes(b'offline reference fixture')
        self.write('source.json',{'input':str(self.video),'duration_s':100.,'audio_track':0})
        self.write('speakers.json',{'S01':{'reviewed':True,'reference':str(self.reference.relative_to(self.root))}})
        self.write('processing_request.json',{'processing_authorized':True,'scope':'private_local_evaluation'})

    def write(self,name,data):
        path=self.folder/name;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(data,ensure_ascii=False))

    def row(self,id='line1',start=0.,end=16.28,characters=59,**updates):
        value={'segment_id':id,'speaker_id':'S01','start_s':start,'end_s':end,
            'text_de':'Ein vollständiger Satz.','text_zh':'字'*characters,
            'source_ids':[id+'_asr1',id+'_asr2'],'translation_reviewed':True,
            'speaker_reviewed':True,'overlap':False,
            'source_sentence_complete':True,'german_sentence_complete':True}
        value.update(updates);return value

    def build(self,rows):
        self.write('segments.json',{'schema_version':2,'segments':rows})
        before=(self.folder/'segments.json').read_bytes()
        result=planner.build_plan(self.root,self.job)
        self.assertEqual((self.folder/'segments.json').read_bytes(),before)
        self.assertFalse(Path(result['tts_job']['directory']).exists())
        self.assertEqual(result['source_rows'],rows)
        self.assertEqual([id for group in result['tts_groups'] for id in group['member_ids']],
                         [row['segment_id'] for row in rows])
        return result

    def test_long_complete_sentence_is_preserved_with_bound_evidence(self):
        row=self.row();plan=self.build([row]);self.assertTrue(plan['ready_for_tts'])
        self.assertEqual(plan['policy_issues'],[])
        group=plan['tts_groups'][0]
        self.assertEqual(group['member_ids'],[row['segment_id']])
        self.assertEqual(group['source_ids'],row['source_ids'])
        self.assertEqual(group['tts_row']['text_zh'],row['text_zh'])
        self.assertEqual(group['tts_row']['text_de'],row['text_de'])
        self.assertEqual(group['subtitle_alignment_method'],'single_member_full_audio_duration_v1')
        proof=group['join_evidence']['single_complete_source_row_exception']
        self.assertEqual(proof['source_row_sha256'],planner.fingerprint(row))
        self.assertEqual(proof['source_ids'],row['source_ids'])
        self.assertEqual(proof['source_span_s'],16.28)
        self.assertEqual(proof['max_source_span_s'],24.)
        self.assertTrue(proof['source_row_text_and_ids_unchanged'])
        self.assertFalse(proof['source_row_split_or_joined'])

    def test_singleton_duration_and_character_boundaries(self):
        for duration,characters,accepted in [(15.,80,True),(15.0001,80,True),
                (24.,80,True),(24.0001,80,False),(16.28,81,False)]:
            with self.subTest(duration=duration,characters=characters):
                plan=self.build([self.row(end=duration,characters=characters)])
                self.assertEqual(plan['ready_for_tts'],accepted)
                proof=plan['tts_groups'][0]['join_evidence'].get('single_complete_source_row_exception')
                self.assertEqual(proof is not None,accepted and duration>15.)

    def test_incomplete_unknown_or_overlapping_long_row_stays_blocked(self):
        for field,value in [('source_sentence_complete',False),('german_sentence_complete',False),
                ('source_sentence_complete',None),('german_sentence_complete','true'),('overlap',True)]:
            with self.subTest(field=field,value=value):
                row=self.row(**{field:value});plan=self.build([row])
                self.assertFalse(plan['ready_for_tts'])
                self.assertTrue(plan['policy_issues'])
                self.assertNotIn('single_complete_source_row_exception',plan['tts_groups'][0]['join_evidence'])
        row=self.row();del row['source_sentence_complete']
        self.assertFalse(self.build([row])['ready_for_tts'])

    def test_multiple_rows_still_cannot_join_beyond_fifteen_seconds(self):
        rows=[self.row('line1',0.,8.,20),self.row('line2',8.,16.,20)]
        self.write('segments.json',{'segments':rows})
        self.write('translation/report.json',{'job':self.job,
            'source_segments_sha256':planner.sha(self.folder/'segments.json'),
            'source_speakers_sha256':planner.sha(self.folder/'speakers.json'),
            'review_status':'machine_second_pass_approved',
            'tts_join_suggestions':[{'member_ids':['line1','line2'],'approved':True,
                'same_topic':True,'topic_boundary_crossed':False,'quote_speaker_change':False,
                'reason':'Same reviewed topic.'}]})
        # Keep the exact fixture SHA required by the proposal receipt.
        plan=planner.build_plan(self.root,self.job)
        self.assertTrue(plan['ready_for_tts'])
        self.assertEqual([g['member_ids'] for g in plan['tts_groups']],[['line1'],['line2']])
        self.assertEqual(plan['rejected_suggestions'][0]['reason'],'source_span_exceeds_15s')
        self.assertEqual(plan['policy']['max_source_span_s'],15.)
        self.assertTrue(all('single_complete_source_row_exception' not in g['join_evidence']
                            for g in plan['tts_groups']))

    def test_fifteen_second_multirow_join_retains_existing_limit(self):
        for end,joined in [(15.,True),(15.0001,False)]:
            with self.subTest(end=end):
                rows=[self.row('line1',0.,7.5,20),self.row('line2',7.5,end,20)]
                self.write('segments.json',{'segments':rows})
                self.write('translation/report.json',{'job':self.job,
                    'source_segments_sha256':planner.sha(self.folder/'segments.json'),
                    'source_speakers_sha256':planner.sha(self.folder/'speakers.json'),
                    'review_status':'machine_second_pass_approved',
                    'tts_join_suggestions':[{'member_ids':['line1','line2'],'approved':True,
                        'same_topic':True,'topic_boundary_crossed':False,'quote_speaker_change':False,
                        'reason':'Same reviewed topic.'}]})
                plan=planner.build_plan(self.root,self.job)
                self.assertTrue(plan['ready_for_tts'])
                self.assertEqual(len(plan['tts_groups']),1 if joined else 2)
                self.assertTrue(all('single_complete_source_row_exception' not in g['join_evidence']
                                    for g in plan['tts_groups']))

    def test_long_sentence_does_not_change_coverage_or_duplicate_ids(self):
        rows=[self.row('line1',0.,5.,10),self.row('line2',5.,21.28,59),
              self.row('line3',21.28,24.,10)]
        original=copy.deepcopy(rows);plan=self.build(rows)
        self.assertTrue(plan['ready_for_tts'])
        self.assertEqual(rows,original)
        self.assertEqual(plan['coverage']['original_source_rows'],3)
        self.assertTrue(plan['coverage']['every_source_row_exactly_once_in_original_order'])
        self.assertEqual([id for g in plan['tts_groups'] for id in g['source_ids']],
                         [id for row in rows for id in row['source_ids']])
        rows[2]['source_ids']=[rows[1]['source_ids'][0]]
        self.write('segments.json',{'segments':rows})
        with self.assertRaisesRegex(ValueError,'source coverage contains duplicates'):
            planner.build_plan(self.root,self.job)


if __name__=='__main__':unittest.main()
