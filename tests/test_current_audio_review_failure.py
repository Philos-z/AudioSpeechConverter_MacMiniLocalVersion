"""Current complete audio diagnosis and real pipeline stop branch; no ASR/TTS."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'web'))
import failure_triage as triage
import media_recovery as media
import pipeline


def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False))


def ref(folder,path):
    return {'path':str(path.relative_to(folder)),
        'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}


class CurrentAudioReviewFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.folder=self.root/'jobs/fixture'
        self.ids=['line0014','line0020','line0090','line0103','line0154','line0155','line0163']
        self.pending=['line0014','line0103','line0155']
        self.rows=[{'segment_id':sid,'speaker_id':'S01','start_s':i*2.,'end_s':i*2.+1.,
            'text_de':'Ganze Quelle.','text_zh':'完整译文。'} for i,sid in enumerate(self.ids)]
        write(self.folder/'segments.json',{'segments':self.rows})
        write(self.folder/'translation/input.json',{'segments':self.rows})
        source=self.root/'inputs/source.mp4';source.parent.mkdir();source.write_bytes(b'original')
        self.source_sha=hashlib.sha256(source.read_bytes()).hexdigest()
        write(self.folder/'source.json',{'input':str(source)})
        self.job={'id':'fixture','source_sha256':self.source_sha}
        helper=self.root/'web/speech_content_quality.py';helper.parent.mkdir();helper.write_text('frozen checker')
        self.plan_path=self.folder/'tts/semantic_group_plan.json'
        self.groups=[{'group_id':'group_'+sid,'member_ids':[sid]} for sid in self.ids]
        write(self.plan_path,{'job':'fixture','source_segments':{
            'path':str(self.folder/'segments.json'),
            'sha256':ref(self.folder,self.folder/'segments.json')['sha256']},'tts_groups':self.groups})
        self.plan_ref=ref(self.folder,self.plan_path)
        native=[{'segment_id':group['group_id'],'sha256':hashlib.sha256(sid.encode()).hexdigest(),
            'content':{'schema':'native_mlx_whole_utterance_v1'}} for group,sid in zip(self.groups,self.ids)]
        self.native_path=self.folder/'tts/snapshot/manifest.json'
        write(self.native_path,self.manifest(native))
        self.entries=[]
        self.old_path=self.compose(self.native_path,['line0154'])
        self.current_path=self.compose(self.old_path,[sid for sid in self.ids if sid!='line0154'])
        self.save_journal()
        self.quality_path=self.folder/'tts/audio_quality_aaaaaaaaaaaaaaaa.json'
        self.quality=self.quality_for(self.current_path,self.pending)
        write(self.quality_path,self.quality)
        self.old_quality_path=self.folder/'tts/audio_quality_bbbbbbbbbbbbbbbb.json'
        write(self.old_quality_path,self.quality_for(self.old_path,[sid for sid in self.ids if sid!='line0154']))
        # The historical acoustic failure is real evidence, but no longer current.
        write(self.folder/'tts/audio_quality_0000000000000000.json',{
            'complete':True,'binding':{'source_segments_sha256':ref(self.folder,self.folder/'segments.json')['sha256']},
            'needs_repair_ids':['line0154']})

    def manifest(self,rows,base=None):
        value={'schema':'local_acoustic_fallback_composition_v1','job':'fixture','phase':'complete',
            'pending_unique_keys':0,'selected_utterances':len(rows),'utterances':rows}
        if base is not None:value['source_manifests']={'base':base}
        return value

    def compose(self,base_path,selected):
        groups=['group_'+sid for sid in selected]
        binding={'plan':self.plan_ref,'base_manifest':ref(self.folder,base_path),
            'source_video_sha256':self.source_sha,'group_ids':groups}
        token=media.key({'action':'audio_regenerate','binding':binding})
        directory=self.folder/'tts/acoustic_fallback'/token
        rows=copy.deepcopy(json.loads(base_path.read_text())['utterances'])
        for row in rows:
            if row['segment_id'] in groups:
                row['content']={'schema':'continuous_utterance_v1'}
                row['sha256']=hashlib.sha256((token+row['segment_id']).encode()).hexdigest()
        composition=directory/'composition_manifest.json'
        write(composition,self.manifest(rows,binding['base_manifest']))
        request=directory/'request.json'
        write(request,{'schema':'local_acoustic_fallback_v1','token':token,'binding':binding,'group_ids':groups})
        selection=directory/'selection.json'
        write(selection,{'schema':'local_acoustic_fallback_v1','token':token,'binding':binding,
            'composition':ref(self.folder,composition),'request':ref(self.folder,request),'selected_group_ids':groups})
        result={'schema':'local_acoustic_fallback_v1','selection':str(selection),'manifest':str(composition),
            'group_ids':groups,'outputs':[ref(self.folder,selection),ref(self.folder,composition)]}
        self.entries.append({'token':token,'action':'audio_regenerate','binding':binding,
            'status':'complete','result':result})
        return composition

    def save_journal(self):
        write(self.folder/'tts/media_recovery_journal.json',{'schema':media.JOURNAL_SCHEMA,
            'job':'fixture','max_recoveries':6,'entries':self.entries})

    def quality_for(self,manifest_path,pending):
        binding={'plan':self.plan_ref,'native_manifest':ref(self.folder,manifest_path),
            'source_segments_sha256':ref(self.folder,self.folder/'segments.json')['sha256']}
        native=json.loads(manifest_path.read_text())['utterances'];rows=[]
        for sid,group,audio in zip(self.ids,self.groups,native):
            review=sid in pending
            rows.append({'group_id':group['group_id'],'member_ids':[sid],'audio_sha256':audio['sha256'],
                'target_free_asr_used':True,'human_listened':False,
                'generation_provenance':{'plan':binding['plan'],'native_manifest':binding['native_manifest']},
                'needs_repair':False,'needs_review':review,'content_evaluation':{
                    'needs_repair':False,'needs_review':review,
                    'status':'needs_review' if review else 'machine_content_screen_passed'}})
        return {'schema':'website_audio_quality_v1','complete':True,'policy':'layered_full_content_v1',
            'all_groups_target_free_checked':True,'helper_code_sha256':hashlib.sha256(
                (self.root/'web/speech_content_quality.py').read_bytes()).hexdigest(),
            'binding':binding,'rows':rows,'checked_utterances':len(rows),'target_free_checked_utterances':len(rows),
            'needs_repair_ids':[],'needs_repair_groups':[],'needs_review_ids':pending,
            'needs_review_groups':['group_'+sid for sid in pending]}

    def error(self):
        return pipeline.current_audio_content_review_error(self.root,self.folder,self.plan_path,
            self.current_path,self.quality_path)

    def diagnose(self,error):
        return triage.make_failure_record(self.root,self.job,error,stage='检查配音声学异常')

    def flow(self):
        flow=pipeline.Flow.__new__(pipeline.Flow)
        flow.root=self.root;flow.folder=self.folder;flow.progress=81;flow.command=Mock()
        return flow

    def test_current_three_replace_stale_154_and_require_change(self):
        record=self.diagnose(self.error())
        self.assertEqual((record['category'],record['retry_policy']),('audio','after_change'))
        self.assertEqual(record['segment_ids'],self.pending)
        self.assertEqual([row['id'] for row in record['source_windows']],self.pending)
        self.assertEqual(record['diagnostic_source'],'current_complete_audio_quality')
        self.assertIn('两版',record['message']);self.assertIn('集中试听',record['next_action'])
        self.job['failure_record']=record
        self.assertFalse(triage.public_failure(self.root,self.job)['can_retry'])

    def test_production_fallback_review_branch_stops_without_generating(self):
        flow=self.flow();manifest=json.loads(self.current_path.read_text())
        with patch('acoustic_fallback.prepare') as prepare,patch('acoustic_fallback.finish') as finish:
            with self.assertRaises(pipeline.AudioContentReviewRequired) as caught:
                flow.handle_unconfirmed_audio_quality(self.quality,self.plan_path,self.current_path,
                    self.quality_path,manifest)
            prepare.assert_not_called();finish.assert_not_called();flow.command.assert_not_called()
        self.assertEqual(self.diagnose(caught.exception)['segment_ids'],self.pending)

    def test_native_first_try_keeps_same_text_fallback_route(self):
        flow=self.flow();native=json.loads(self.native_path.read_text())
        with patch('acoustic_fallback.prepare',return_value={'argv':['fixed-runner'],
                'fallback_manifest':'fallback.json'}) as prepare,patch('acoustic_fallback.finish',
                return_value={'changed':True}) as finish:
            with self.assertRaises(pipeline.WebsiteRepairReady):
                flow.handle_unconfirmed_audio_quality(self.quality,self.plan_path,self.native_path,
                    self.quality_path,native)
            self.assertEqual(prepare.call_args.kwargs,{'mode':'unconfirmed_content'})
            self.assertEqual(finish.call_args.kwargs,{'mode':'unconfirmed_content'})
            flow.command.assert_called_once()

    def test_no_pending_is_noop(self):
        flow=self.flow()
        flow.handle_unconfirmed_audio_quality({'needs_review_ids':[]},None,None,None,{})
        flow.command.assert_not_called()

    def test_acoustic_hard_failure_does_not_enter_review_recovery(self):
        flow=self.flow();quality={**self.quality,'needs_repair_ids':['line0154']}
        with patch('acoustic_fallback.prepare') as prepare:
            with self.assertRaisesRegex(RuntimeError,'声学硬检查'):
                flow.handle_unconfirmed_audio_quality(quality,None,None,None,{})
            prepare.assert_not_called();flow.command.assert_not_called()

    def test_changed_quality_between_read_and_stop_is_rejected(self):
        changed={**self.quality,'complete':False};write(self.quality_path,changed)
        flow=self.flow()
        with self.assertRaisesRegex(RuntimeError,'证据已改变'):
            flow.handle_unconfirmed_audio_quality(self.quality,self.plan_path,self.current_path,
                self.quality_path,json.loads(self.current_path.read_text()))
        flow.command.assert_not_called()

    def test_corrupt_or_forged_metadata_never_scans_old_quality(self):
        valid=self.error().failure_metadata
        for mutate in (lambda m:m.update(source_sha256='0'*64),
                lambda m:m.update(segment_ids=['line0154']),lambda m:m.update(group_ids=['invented']),
                lambda m:m['segment_ids'].append('line0014'),
                lambda m:m.update(origin='forged'),lambda m:m['bindings']['manifest'].update(sha256='0'*64),
                lambda m:m['bindings']['quality'].update(sha256='f'*64),
                lambda m:m['bindings']['segments'].update(path='../segments.json'),
                lambda m:m['bindings']['plan'].update(path='tts/other_plan.json')):
            with self.subTest(mutate=mutate):
                metadata=copy.deepcopy(valid);mutate(metadata)
                record=self.diagnose(pipeline.AudioContentReviewRequired('配音检查失败',metadata))
                self.assertEqual(record['category'],'unknown');self.assertEqual(record['segment_ids'],[])
                self.assertEqual(record['retry_policy'],'allowed')

    def test_claimed_typed_runtimeerror_with_bad_binding_is_safe_unknown(self):
        error=RuntimeError('配音检查失败');error.failure_metadata={'schema':triage.AUDIO_REVIEW_FAILURE_SCHEMA}
        self.assertEqual(self.diagnose(error)['segment_ids'],[])

    def test_missing_current_input_rows_cannot_invent_source_windows(self):
        error=self.error();write(self.folder/'translation/input.json',{'segments':[]})
        record=self.diagnose(error)
        self.assertEqual(record['category'],'unknown');self.assertEqual(record['source_windows'],[])

    def test_incomplete_quality_cannot_construct_current_error(self):
        self.quality['complete']=False;write(self.quality_path,self.quality)
        with self.assertRaises(ValueError):self.error()

    def test_forged_manifest_self_consistency_is_insufficient(self):
        other=self.folder/'tts/forged/manifest.json'
        write(other,json.loads(self.current_path.read_text()))
        write(self.quality_path,self.quality_for(other,self.pending))
        with self.assertRaises(ValueError):
            pipeline.current_audio_content_review_error(self.root,self.folder,self.plan_path,other,self.quality_path)

    def test_old_complete_six_review_manifest_is_not_current_leaf(self):
        self.assertFalse(triage._audio_manifest_is_current(self.folder,self.plan_ref,
            ref(self.folder,self.old_path),self.source_sha))
        with self.assertRaises(ValueError):
            pipeline.current_audio_content_review_error(self.root,self.folder,self.plan_path,
                self.old_path,self.old_quality_path)
        metadata=copy.deepcopy(self.error().failure_metadata)
        metadata['bindings']['quality']=ref(self.folder,self.old_quality_path)
        metadata['bindings']['manifest']=ref(self.folder,self.old_path)
        old=json.loads(self.old_quality_path.read_text())
        metadata.update(segment_ids=old['needs_review_ids'],group_ids=old['needs_review_groups'])
        self.assertEqual(self.diagnose(pipeline.AudioContentReviewRequired('旧检查',metadata))['category'],'unknown')

    def test_journal_order_and_unrelated_quality_mtime_do_not_select_audio(self):
        self.entries.reverse();self.save_journal()
        for path in self.folder.glob('tts/audio_quality_*.json'):os.utime(path,(2_000_000_000,2_000_000_000))
        self.assertEqual(self.diagnose(self.error())['segment_ids'],self.pending)

    def test_other_completed_media_actions_do_not_change_audio_leaf(self):
        for action in ('picture_holds','encoded_master_repair'):
            self.entries.append({'token':hashlib.sha256(action.encode()).hexdigest(),'action':action,
                'binding':{'other':'later'},'status':'complete','result':{}})
        self.save_journal()
        self.assertEqual(self.diagnose(self.error())['segment_ids'],self.pending)

    def test_broken_selection_chain_cannot_approve_a_current_failure(self):
        selection=Path(self.entries[-1]['result']['selection'])
        value=json.loads(selection.read_text());value['token']='0'*64;write(selection,value)
        with self.assertRaises(ValueError):self.error()

    def test_unknown_legacy_error_keeps_existing_154_diagnosis(self):
        record=self.diagnose(RuntimeError('备用配音仍未通过声学检查'))
        self.assertEqual(record['category'],'audio');self.assertEqual(record['segment_ids'],['line0154'])
        self.assertNotIn('diagnostic_source',record)

    def test_audio_journal_identity_does_not_unlock_text_or_unknown(self):
        before={kind:triage._revision_fingerprint(self.root,self.job,failure_category=kind)
            for kind in ('audio','translation_review','unknown')}
        self.entries.append({'token':'a'*64,'action':'audio_regenerate','status':'pending','binding':{'new':'voice'}})
        self.save_journal()
        after={kind:triage._revision_fingerprint(self.root,self.job,failure_category=kind) for kind in before}
        self.assertNotEqual(before['audio'],after['audio'])
        self.assertEqual(before['translation_review'],after['translation_review'])
        self.assertEqual(before['unknown'],after['unknown'])

    def test_unrelated_old_quality_does_not_unlock_current_audio_diagnosis(self):
        record=self.diagnose(self.error());self.job['failure_record']=record
        write(self.folder/'tts/audio_quality_cccccccccccccccc.json',{'complete':True,'extra':'historical'})
        self.assertFalse(triage.public_failure(self.root,self.job)['can_retry'])


if __name__=='__main__':unittest.main()
