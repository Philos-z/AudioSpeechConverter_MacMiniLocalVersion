"""Actual tiny PCM/sidecars, no model imports, GPU, network or subprocesses."""
import copy
import json
import math
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'web'))
import acoustic_fallback as fallback
import media_recovery as media
import generic_group_alignment as alignment


def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False))


class AcousticFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.folder=self.root/'jobs'/'fixture'
        self.folder.mkdir(parents=True)
        self.normalizer=patch.object(alignment,'normalize_factory',return_value=lambda s:''.join(c.lower() for c in s if c.isalnum()))
        self.normalizer.start();self.addCleanup(self.normalizer.stop)
        self.video=self.root/'video.mp4';self.video.write_bytes(b'original media')
        self.ref=self.folder/'references/S1.wav';self.ref.parent.mkdir();self.ref.write_bytes(b'original reference')
        self.speakers={'S1':{'reference':str(self.ref.relative_to(self.root)),'reviewed':True,'note':'original reference'}}
        save(self.folder/'speakers.json',self.speakers)
        save(self.folder/'source.json',{'input':str(self.video),'duration_s':20,'audio_track':0})
        self.permission={'processing_authorized':True,'scope':'private_local_evaluation','requested_by':'fixture'}
        save(self.folder/'processing_request.json',self.permission)
        self.source_rows=[{'segment_id':f'line{i:04d}','speaker_id':'S1','text_zh':f'完整句子{i}。',
            'text_de':f'Satz {i}.','start_s':float(i*3),'end_s':float(i*3+2),
            'source_ids':[f'seg{i:04d}'],'translation_reviewed':True,'speaker_reviewed':True} for i in range(1,4)]
        save(self.folder/'segments.json',{'segments':self.source_rows})
        self.groups=[]
        for i,row in enumerate(self.source_rows):
            group={'group_id':f'group{i+1}','member_ids':[row['segment_id']],'speaker_id':'S1',
                'source_ids':row['source_ids'],'text_zh':row['text_zh'],'text_de':row['text_de'],
                'reference':{'path':str(self.ref),'sha256':media.sha(self.ref)},
                'tts_row':{**row,'segment_id':f'group{i+1}','member_ids':[row['segment_id']]}}
            self.groups.append(group)
        self.plan_path=self.folder/'tts/plan.json'
        self.plan={'schema':'pretts_semantic_group_plan_v1','job':'fixture','source_rows':self.source_rows,
            'source_segments':{'path':str(self.folder/'segments.json'),'sha256':media.sha(self.folder/'segments.json')},
            'source_speakers':{'path':str(self.folder/'speakers.json'),'sha256':media.sha(self.folder/'speakers.json')},
            'source_binding':{'source_receipt_path':str(self.folder/'source.json'),
                'source_receipt_sha256':media.sha(self.folder/'source.json'),'source_video_path':str(self.video),
                'source_video_bytes':self.video.stat().st_size,'source_video_sha256':media.sha(self.video)},
            'tts_groups':self.groups,'tts_job':{'id':'native_fixture'}}
        save(self.plan_path,self.plan)
        self.base_path=self.folder/'tts/base/manifest.json'
        self.base={'schema':'native_mlx_whole_utterance_v1','job':'native_fixture','phase':'complete',
            'pending_unique_keys':0,'parameters':{'native':'original'},'generated_unique_keys':3,
            'selected_utterances':3,'utterances':[self.audio(g,'native_mlx_whole_utterance_v1',i==0) for i,g in enumerate(self.groups)]}
        save(self.base_path,self.base)
        self.quality_path=self.folder/'tts/quality.json';self.make_quality(self.base_path)
        runner=self.root/'scripts/continuous_tts.py';runner.parent.mkdir();runner.write_text('fixture pinned runner')
        python=self.root/'vendor/index-tts/.venv/bin/python';python.parent.mkdir(parents=True);python.write_text('fixture python')

    def audio(self,group,schema,suspicious=False,parameters=None):
        parameters=parameters or {'native':'original'}
        content={'schema':schema,'text_zh':group['text_zh'],'effective_tts_text':group['text_zh'],
            'reference_sha256':media.sha(self.ref),'model_identity':{'fixture':schema},'parameters':parameters}
        key=alignment.key(content);path=self.root/'cache'/schema/(key+'.wav');path.parent.mkdir(parents=True,exist_ok=True)
        rate=8000;samples=[int(10000*(.3 if suspicious or i>=rate//2 else .01)*math.sin(2*math.pi*80*i/rate)) for i in range(rate)]
        with wave.open(str(path),'wb') as out:
            out.setnchannels(1);out.setsampwidth(2);out.setframerate(rate);out.writeframes(struct.pack('<'+'h'*rate,*samples))
        entry={'schema':schema,'cache_key':key,'content':content,'sha256':media.sha(path),
            'frames':rate,'sample_rate':rate,'channels':1,'duration_s':1.}
        save(path.with_suffix('.json'),entry)
        return {'segment_id':group['group_id'],'speaker_id':'S1','reference':str(self.ref),
            'audio':str(path),'status':'generated',**{k:v for k,v in entry.items() if k!='schema'}}

    def make_quality(self,manifest_path,flagged=None):
        manifest=media.read(manifest_path);rows=[];flagged=flagged if flagged is not None else ['group1']
        for group,audio in zip(self.groups,manifest['utterances']):
            metrics=media.wav_metrics(audio['audio'],audio)
            row={'group_id':group['group_id'],'member_ids':group['member_ids'],'audio_sha256':audio['sha256'],
                 **metrics,'target_free_asr_used':False,'needs_repair':group['group_id'] in flagged,'human_listened':False}
            if row['needs_repair']:
                profile={'device':'cpu','compute_type':'int8','local_files_only':True,'target_conditioning':False,
                    'transcribe_options':alignment.OPTIONS}
                observation={'schema':alignment.ASR_SCHEMA,'phase':'complete','profile':profile,
                    'audio_sha256':audio['sha256'],'signature':alignment.key({'audio_sha256':audio['sha256'],'profile':profile}),
                    'segments':[{'text':'音乐','words':[]}]}
                path=self.folder/'tts'/('observation_'+group['group_id']+'.json');save(path,observation)
                row.update(target_free_asr_used=True,acoustically_suspicious=True,target_free_recognized='音乐',
                    normalized_text_similarity=0.,observation=media.record(self.folder,path))
            rows.append(row)
        selected=[g for g in self.groups if g['group_id'] in flagged]
        result={'schema':'website_audio_quality_v1','binding':{'plan':media.record(self.folder,self.plan_path),
            'native_manifest':media.record(self.folder,manifest_path),'source_segments_sha256':self.plan['source_segments']['sha256']},
            'complete':True,'rows':rows,'checked_utterances':len(rows),'needs_repair_ids':[sid for g in selected for sid in g['member_ids']],
            'needs_repair_groups':[g['group_id'] for g in selected],
            'flagged_groups':[{'group_id':g['group_id'],'member_ids':g['member_ids']} for g in selected],
            'human_listened':False,'waveform_modified':False,'status':'needs_repair'}
        save(self.quality_path,result);return result

    def prepare(self):
        return fallback.prepare(self.root,self.folder,self.plan_path,self.base_path,self.quality_path)

    def torch_manifest(self,request,change=None):
        parameters={**fallback.POLICY,'device':'mps','use_qwen_emo':False}
        selected=[g for g in self.groups if g['group_id'] in request['group_ids']]
        result={'schema':'continuous_utterance_v1','job':request['child_job'],'phase':'complete',
            'pending_unique_keys':0,'selected_utterances':len(selected),'parameters':parameters,
            'experimental_preview':False,'utterances':[self.audio(g,'continuous_utterance_v1',False,parameters) for g in selected]}
        if change:change(result)
        path=Path(request['fallback_manifest']);save(path,result);return path

    def finish(self,manifest):
        return fallback.finish(self.root,self.folder,self.plan_path,self.base_path,self.quality_path,manifest)

    def test_prepare_exact_child_and_fixed_torch_arguments_preserve_originals(self):
        originals={p:p.read_bytes() for p in [self.folder/'segments.json',self.folder/'speakers.json',self.base_path,self.ref,self.video]}
        request=self.prepare();child=self.root/'jobs'/request['child_job']
        self.assertEqual(media.read(child/'segments.json')['segments'],[self.groups[0]['tts_row']])
        self.assertEqual(media.read(child/'speakers.json'),self.speakers)
        self.assertEqual(media.read(child/'processing_request.json'),self.permission)
        self.assertIn('--cfm-steps',request['argv']);self.assertEqual(request['argv'][-4:],['--cfm-steps','25','--emotion-mode','reference'])
        self.assertEqual(media.journal(self.folder)['entries'][0]['action'],'audio_regenerate')
        self.assertEqual(self.prepare(),request);self.assertEqual(len(media.journal(self.folder)['entries']),1)
        self.assertEqual({p:p.read_bytes() for p in originals},originals)

    def test_flat_high_zcr_audio_is_not_exempt_from_content_verification(self):
        path=self.root/'flat_noise.wav';rate=8000
        samples=[int(10000*math.sin(2*math.pi*1200*i/rate)) for i in range(rate)]
        with wave.open(str(path),'wb') as out:
            out.setnchannels(1);out.setsampwidth(2);out.setframerate(rate)
            out.writeframes(struct.pack('<'+'h'*rate,*samples))
        metrics=media.wav_metrics(path,{'sha256':media.sha(path),'frames':rate,
            'sample_rate':rate,'duration_s':1.,'channels':1})
        self.assertLess(metrics['energy_dynamic_range_db'],12.)
        self.assertGreater(metrics['zero_crossing_rate'],.06)
        self.assertTrue(metrics['acoustically_suspicious'])

    def test_finish_and_resolve_replace_only_selected_audio_and_keep_statistics(self):
        request=self.prepare();manifest=self.torch_manifest(request);result=self.finish(manifest)
        composite=media.read(result['manifest'])
        self.assertEqual(composite['utterances'][1:],self.base['utterances'][1:])
        self.assertEqual(composite['base_generation_statistics']['generated_unique_keys'],3)
        self.assertEqual(composite['utterances'][0]['content']['schema'],'continuous_utterance_v1')
        self.assertTrue(composite['composition']);self.assertFalse(composite['single_native_generation_claimed'])
        self.assertTrue(result['requires_new_alignment_and_quality_checks'])
        self.assertEqual(fallback.resolve(self.root,self.folder,self.plan_path,self.base_path),Path(result['manifest']))
        self.assertEqual(self.finish(manifest),result)

    def test_second_native_group_repair_extends_prior_composition(self):
        self.base['utterances'][1]=self.audio(self.groups[1],'native_mlx_whole_utterance_v1',True)
        save(self.base_path,self.base);self.make_quality(self.base_path)
        first_request=self.prepare();first=self.finish(self.torch_manifest(first_request))
        prior=Path(first['manifest']);self.make_quality(prior,flagged=['group2'])
        second_request=fallback.prepare(self.root,self.folder,self.plan_path,prior,self.quality_path)
        second=fallback.finish(self.root,self.folder,self.plan_path,prior,self.quality_path,
            self.torch_manifest(second_request))
        resolved=fallback.resolve(self.root,self.folder,self.plan_path,self.base_path)
        self.assertEqual(resolved,Path(second['manifest']))
        result=media.read(resolved);first_rows=media.read(prior)['utterances']
        self.assertEqual(result['utterances'][0],first_rows[0])
        self.assertEqual(result['utterances'][2],first_rows[2])
        self.assertEqual(result['replaced_group_ids'],['group2'])
        self.assertEqual(len(media.journal(self.folder)['entries']),2)

    def test_resolve_without_selection_returns_valid_base(self):
        self.assertEqual(fallback.resolve(self.root,self.folder,self.plan_path,self.base_path),self.base_path)

    def test_stale_quality_or_missing_asr_evidence_does_not_reserve(self):
        original=media.read(self.quality_path)
        for mutate in [lambda d:d.update(complete=False),lambda d:d['binding']['plan'].update(sha256='0'*64),
                       lambda d:d['rows'][0].update(target_free_asr_used=False),lambda d:d['rows'][0].update(normalized_text_similarity=.9),
                       lambda d:d.update(needs_repair_groups=['group2'])]:
            with self.subTest(mutate=mutate):
                value=copy.deepcopy(original);mutate(value);save(self.quality_path,value)
                with self.assertRaises(ValueError):self.prepare()
                self.assertEqual(media.journal(self.folder)['entries'],[])

    def test_observation_content_and_signature_are_checked(self):
        quality=media.read(self.quality_path);path=media.checked_record(self.folder,quality['rows'][0]['observation'])
        value=media.read(path);value['profile']['target_conditioning']=True;save(path,value)
        quality['rows'][0]['observation']=media.record(self.folder,path);save(self.quality_path,quality)
        with self.assertRaises(ValueError):self.prepare()
        self.assertEqual(media.journal(self.folder)['entries'],[])

    def test_same_source_group_cannot_get_second_reservation_from_new_quality_path(self):
        self.prepare();another=self.folder/'tts/changed_quality_provenance.json'
        changed=media.read(self.quality_path);changed['helper_code_sha256']='0'*64;save(another,changed)
        with self.assertRaisesRegex(ValueError,'only once'):
            fallback.prepare(self.root,self.folder,self.plan_path,self.base_path,another)
        self.assertEqual(len(media.journal(self.folder)['entries']),1)

    def test_interrupted_pending_resumes_when_quality_asr_cache_hit_changes(self):
        quality=media.read(self.quality_path);quality['rows'][0]['asr_cache_hit']=False;save(self.quality_path,quality)
        request=self.prepare();original=media.sha(self.quality_path)
        quality['rows'][0]['asr_cache_hit']=True;save(self.quality_path,quality)
        self.assertNotEqual(media.sha(self.quality_path),original)
        self.assertEqual(self.prepare(),request)
        self.assertEqual(len(media.journal(self.folder)['entries']),1)
        result=self.finish(self.torch_manifest(request))
        self.assertEqual(fallback.resolve(self.root,self.folder,self.plan_path,self.base_path),Path(result['manifest']))

    def test_lifetime_media_budget_is_not_reset(self):
        for i in range(6):media.reserve_recovery(self.folder,'alignment_split',{'fixture':i})
        with self.assertRaisesRegex(ValueError,'budget exhausted'):self.prepare()
        self.assertEqual(len(media.journal(self.folder)['entries']),6)

    def test_changed_base_wav_or_sidecar_is_rejected_before_reservation(self):
        path=Path(self.base['utterances'][1]['audio']);path.write_bytes(b'changed')
        with self.assertRaises((ValueError,wave.Error,EOFError)):self.prepare()
        self.assertEqual(media.journal(self.folder)['entries'],[])

    def test_wrong_torch_parameters_or_incomplete_output_never_selects(self):
        request=self.prepare()
        for mutate in [lambda d:d['parameters'].update(seed=7),lambda d:d['parameters'].update(cfm_steps=8),
                       lambda d:d['parameters'].update(dtype='float16'),lambda d:d['parameters'].update(emo_vector=[.3]),
                       lambda d:d.update(phase='failed'),lambda d:d['utterances'].append(d['utterances'][0])]:
            with self.subTest(mutate=mutate):
                path=self.torch_manifest(request,mutate)
                with self.assertRaises(ValueError):self.finish(path)
                self.assertEqual(media.journal(self.folder)['entries'][0]['status'],'pending')
                self.assertFalse((Path(request['fallback_manifest']).parent.parent/'selection.json').exists())

    def test_changed_child_text_or_speaker_reference_is_rejected(self):
        request=self.prepare();path=self.torch_manifest(request)
        child=self.root/'jobs'/request['child_job'];data=media.read(child/'segments.json');data['segments'][0]['text_zh']='改变文字';save(child/'segments.json',data)
        with self.assertRaises(ValueError):self.finish(path)

    def test_reused_composition_revalidates_actual_torch_audio_and_source(self):
        request=self.prepare();path=self.torch_manifest(request);self.finish(path)
        self.video.write_bytes(b'new original')
        with self.assertRaises(ValueError):fallback.resolve(self.root,self.folder,self.plan_path,self.base_path)
        self.video.write_bytes(b'original media')
        Path(media.read(path)['utterances'][0]['audio']).write_bytes(b'bad selected audio')
        with self.assertRaises((ValueError,wave.Error,EOFError)):fallback.resolve(self.root,self.folder,self.plan_path,self.base_path)

    def test_torch_failure_again_stops_without_proxy_or_second_regeneration(self):
        request=self.prepare();path=self.torch_manifest(request);result=self.finish(path)
        self.make_quality(Path(result['manifest']))
        with self.assertRaisesRegex(ValueError,'Torch acoustic fallback already failed'):
            fallback.prepare(self.root,self.folder,self.plan_path,Path(result['manifest']),self.quality_path)
        self.assertEqual(len(media.journal(self.folder)['entries']),1)

    def test_composition_or_selection_tampering_cannot_be_reused(self):
        request=self.prepare();result=self.finish(self.torch_manifest(request))
        path=Path(result['manifest']);data=media.read(path);data['utterances'][1]['duration_s']=999;save(path,data)
        with self.assertRaises(ValueError):fallback.resolve(self.root,self.folder,self.plan_path,self.base_path)

    def test_base_sidecar_only_change_is_rejected(self):
        path=Path(self.base['utterances'][1]['audio']).with_suffix('.json')
        value=media.read(path);value['content']['parameters']['native']='changed';save(path,value)
        with self.assertRaisesRegex(ValueError,'content/sidecar'):self.prepare()
        self.assertEqual(media.journal(self.folder)['entries'],[])

    def test_symlink_quality_or_audio_is_rejected(self):
        original=self.quality_path.read_bytes();other=self.root/'external_quality.json';other.write_bytes(original)
        self.quality_path.unlink();self.quality_path.symlink_to(other)
        with self.assertRaisesRegex(ValueError,'Symlink'):self.prepare()
        self.quality_path.unlink();self.quality_path.write_bytes(original)
        audio=Path(self.base['utterances'][0]['audio']);other=self.root/'external.wav';audio.rename(other);audio.symlink_to(other)
        with self.assertRaisesRegex(ValueError,'Symlink'):self.prepare()
        self.assertEqual(media.journal(self.folder)['entries'],[])

    def test_finish_cannot_accept_new_chinese_or_reference_even_with_consistent_sidecar(self):
        request=self.prepare()
        for changed_field,changed_value in [('text_zh','不同的中文。'),('reference_sha256','0'*64)]:
            path=self.torch_manifest(request);document=media.read(path);row=document['utterances'][0]
            row['content'][changed_field]=changed_value;row['cache_key']=alignment.key(row['content'])
            sidecar=Path(row['audio']).with_suffix('.json');entry=media.read(sidecar)
            entry.update(content=row['content'],cache_key=row['cache_key']);save(sidecar,entry);save(path,document)
            with self.assertRaises(ValueError):self.finish(path)
            self.assertEqual(media.journal(self.folder)['entries'][0]['status'],'pending')

    def test_truncated_torch_audio_never_creates_selection(self):
        request=self.prepare();path=self.torch_manifest(request)
        audio=Path(media.read(path)['utterances'][0]['audio']);audio.write_bytes(audio.read_bytes()[:-2])
        with self.assertRaises(ValueError):self.finish(path)
        self.assertFalse((Path(request['fallback_manifest']).parent.parent/'selection.json').exists())

    def test_finished_selection_rejects_changed_base_receipt_even_if_audio_unchanged(self):
        request=self.prepare();self.finish(self.torch_manifest(request))
        changed=media.read(self.base_path);changed['generated_unique_keys']=999;save(self.base_path,changed)
        with self.assertRaises(ValueError):fallback.resolve(self.root,self.folder,self.plan_path,self.base_path)

    def test_finish_revalidates_inherited_permission(self):
        request=self.prepare();path=self.torch_manifest(request)
        child=self.root/'jobs'/request['child_job'];value=media.read(child/'processing_request.json')
        value['processing_authorized']=False;save(child/'processing_request.json',value)
        with self.assertRaisesRegex(ValueError,'child data changed'):self.finish(path)


if __name__=='__main__':unittest.main()
