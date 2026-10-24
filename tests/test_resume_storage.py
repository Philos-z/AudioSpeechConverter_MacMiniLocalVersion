"""Pure remaining-space accounting and exact background-scratch eligibility."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'web'))
import resume_storage as storage


class ResumeStorageTests(unittest.TestCase):
    def setUp(self):
        self.job='/project/jobs/web_fixture';self.source='a'*64
        self.stages={}
        for i,(stage,names) in enumerate(storage.REQUIRED_OUTPUTS.items(),1):
            self.stages[stage]={'source_sha256':self.source,'fingerprint':str(i)*64,
                'expected_fingerprint':str(i)*64,'reusable':True,'will_rebuild':False,
                'outputs':[self.file(name) for name in names]}

    def file(self,name,size=1024):
        return {'path':self.job+'/'+name,'sha256':'b'*64,'actual_sha256':'b'*64,
                'bytes':size,'regular_file':True,'is_symlink':False}

    def estimate(self,free=20*storage.GIB,**kwargs):
        options={'job_path':self.job,'source_sha256':self.source,'stages':self.stages,
            'speech_duration_s':834.2465306122447,'source_rows':175}
        options.update(kwargs)
        return storage.preflight(924.,free,**options)

    def test_no_verified_stage_preserves_original_rounded_cold_peak(self):
        value=self.estimate(stages={})
        self.assertEqual(value['estimated_new_peak_bytes'],924*4*1024**2+256*1024**2)
        self.assertEqual(value['existing_outputs_credit_bytes'],0)
        self.assertEqual(value['required_free_bytes']/storage.GIB,11.859375)

    def test_current_completed_upstream_stages_keep_every_future_cost(self):
        value=self.estimate()
        self.assertEqual(value['storage_duration_bound_s'],924.)
        self.assertEqual(value['estimated_new_peak_bytes'],924*(1248000+320000+88000)+256*1024**2)
        self.assertEqual(value['existing_outputs_credit_bytes'],924*(320000+705600+1152000))
        self.assertEqual(set(value['verified_reusable_stages']),{'prepare','separation','background'})
        self.assertEqual(value['protected_reserve_bytes'],8*storage.GIB)
        self.assertEqual(value['runtime_hard_floor_bytes'],2*storage.GIB)
        self.assertEqual(value['cleanup_bytes_credited'],0)
        self.assertFalse(value['old_assets_deleted'])

    def test_921gib_is_insufficient_until_actual_free_space_increases(self):
        free=int(9.21*storage.GIB);before=self.estimate(free)
        self.assertFalse(before['sufficient'])
        candidates=storage.cleanup_candidates(job_path=self.job,source_sha256=self.source,
            stages=self.stages,temporary_files=[self.file(storage.BACKGROUND_TEMPORARIES[0],354808412),
                                              self.file(storage.BACKGROUND_TEMPORARIES[1],354816088)])
        self.assertEqual(sum(x['bytes'] for x in candidates),709624500)
        self.assertFalse(self.estimate(free)['sufficient'])
        # This input represents newly measured space, not an automatic credit.
        self.assertTrue(self.estimate(free+709624500)['sufficient'])
        with self.assertRaisesRegex(RuntimeError,'断点续跑预计新增峰值'):
            storage.require_sufficient(924,free,job_path=self.job,source_sha256=self.source,stages=self.stages)

    def test_reuse_credit_does_not_depend_on_how_many_bytes_already_exist(self):
        before=self.estimate()
        evidence=copy.deepcopy(self.stages)
        for stage in evidence.values():
            for item in stage['outputs']:item['bytes']=1000*storage.GIB
        after=self.estimate(stages=evidence)
        self.assertEqual(before['existing_outputs_credit_bytes'],after['existing_outputs_credit_bytes'])
        self.assertEqual(before['estimated_new_peak_bytes'],after['estimated_new_peak_bytes'])

    def test_invalid_source_signature_hash_or_regeneration_never_earns_credit(self):
        mutations=[lambda e:e.update(source_sha256='c'*64),
            lambda e:e.update(expected_fingerprint='c'*64),lambda e:e.update(will_rebuild=True),
            lambda e:e.update(reusable=False),lambda e:e['outputs'][0].update(actual_sha256='c'*64),
            lambda e:e['outputs'][0].update(is_symlink=True),
            lambda e:e['outputs'][0].update(regular_file=False),lambda e:e['outputs'].pop(),
            lambda e:e['outputs'].append(copy.deepcopy(e['outputs'][0])),
            lambda e:e['outputs'][0].update(path=self.job+'/../other/source.json')]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                changed=copy.deepcopy(self.stages);mutate(changed['prepare'])
                result=self.estimate(stages=changed)
                self.assertNotIn('prepare',result['verified_reusable_stages'])
                self.assertEqual(result['remaining_upstream_components_bytes']['prepare'],924*320000)
                self.assertEqual(result['reused_stage_budget_bytes']['prepare'],0)

    def test_unfinished_stage_and_unrecognized_native_cache_keep_future_costs(self):
        evidence=copy.deepcopy(self.stages);evidence['background']['will_rebuild']=True
        evidence['native_tts']={'reusable':True,'bytes':999*storage.GIB}
        result=self.estimate(stages=evidence)
        self.assertEqual(result['remaining_upstream_components_bytes']['background'],924*1152000)
        self.assertEqual(result['future_components_bytes']['native_pcm_and_audio_container'],924*88000)
        self.assertEqual(result['future_components_bytes']['mix_four_wav_intermediates'],924*1248000)
        self.assertEqual(result['future_components_bytes']['three_encoded_video_copies'],924*320000)

    def test_short_audio_never_reduces_source_bound_and_long_audio_increases_it(self):
        self.assertEqual(storage.output_duration_bound(924,speech_duration_s=10,source_rows=175),924)
        expected=4+1000+175*.39
        self.assertAlmostEqual(storage.output_duration_bound(924,speech_duration_s=1000,source_rows=175,intro_s=4),expected)
        value=self.estimate(speech_duration_s=1000,intro_s=4)
        self.assertAlmostEqual(value['storage_duration_bound_s'],expected)
        self.assertGreater(value['estimated_new_peak_bytes'],self.estimate()['estimated_new_peak_bytes'])

    def test_longer_actual_edl_requires_more_space_without_adding_reuse_credit(self):
        before=self.estimate();after=self.estimate(planned_output_duration_s=1800)
        self.assertEqual(after['storage_duration_bound_s'],1800)
        self.assertEqual(after['existing_outputs_credit_bytes'],before['existing_outputs_credit_bytes'])
        self.assertEqual(after['estimated_new_peak_bytes'],1800*sum(storage.FUTURE_RATES.values())+storage.FIXED_MARGIN_BYTES)
        self.assertTrue(after['recheck_on_changed_source_stage_signature_audio_or_longer_edl'])

    def test_cleanup_is_exact_allowlist_and_preserves_source_stems_and_master(self):
        files=[self.file(name,123) for name in storage.BACKGROUND_TEMPORARIES]
        files += [self.file(name,123) for name in ('audio/original.wav','audio/background_independent.wav',
            'separated/htdemucs/original/vocals.wav','outputs/old_video.mp4','audio/../source.mp4')]
        files += [copy.deepcopy(files[0])]
        result=storage.cleanup_candidates(job_path=self.job,source_sha256=self.source,
            stages=self.stages,temporary_files=files)
        self.assertEqual([x['path'] for x in result],[self.job+'/'+name for name in storage.BACKGROUND_TEMPORARIES])
        self.assertEqual(sum(x['bytes'] for x in result),246)
        self.assertTrue(all(x['requires_immediate_stage_and_sha_recheck'] for x in result))

    def test_cleanup_requires_current_prepare_and_independent_background_evidence(self):
        for stage in ('prepare','background'):
            bad=copy.deepcopy(self.stages);bad[stage]['expected_fingerprint']='d'*64
            result=storage.cleanup_candidates(job_path=self.job,source_sha256=self.source,
                stages=bad,temporary_files=[self.file(storage.BACKGROUND_TEMPORARIES[0])])
            self.assertEqual(result,[])
        for change in ({'actual_sha256':'e'*64},{'is_symlink':True},{'regular_file':False},{'bytes':-1}):
            item=self.file(storage.BACKGROUND_TEMPORARIES[0]);item.update(change)
            self.assertEqual(storage.cleanup_candidates(job_path=self.job,source_sha256=self.source,
                stages=self.stages,temporary_files=[item]),[])

    def test_pure_functions_do_not_mutate_evidence_or_touch_disk(self):
        original=copy.deepcopy(self.stages)
        with patch('builtins.open',side_effect=AssertionError('No disk I/O is permitted')):
            self.estimate()
            storage.cleanup_candidates(job_path=self.job,source_sha256=self.source,
                stages=self.stages,temporary_files=[self.file(storage.BACKGROUND_TEMPORARIES[0])])
        self.assertEqual(self.stages,original)

    def test_reserve_and_runtime_floor_are_not_relaxed_by_existing_files(self):
        value=self.estimate(2*storage.GIB-1)
        self.assertFalse(value['sufficient']);self.assertFalse(value['runtime_hard_floor_satisfied'])
        self.assertEqual(value['protected_reserve_bytes'],8*storage.GIB)
        self.assertEqual(self.estimate()['schema'],storage.SCHEMA)

    def test_invalid_measurements_and_unbound_paths_fail_closed(self):
        for duration in (0,-1,float('nan'),float('inf'),True):
            with self.subTest(duration=duration),self.assertRaises(ValueError):
                storage.preflight(duration,20*storage.GIB,job_path=self.job,source_sha256=self.source)
        for free in (-1,True,3.2):
            with self.subTest(free=free),self.assertRaises(ValueError):self.estimate(free)
        for path in ('relative/path','/job/../escape',None):
            with self.subTest(path=path),self.assertRaises(ValueError):self.estimate(job_path=path)
        with self.assertRaises(ValueError):self.estimate(source_sha256='unbound')
        with self.assertRaises(ValueError):self.estimate(source_rows=True)


if __name__=='__main__':unittest.main()
