"""Current cache identity, dependency chain and actual EDL storage guards."""
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'web'))
import pipeline
from resume_storage import REQUIRED_OUTPUTS

class ResumeSpacePipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        self.flow=pipeline.Flow(self.root,{'id':'web_space_fixture','options':{}},lambda *a:None,None,lambda:False)
        self.source_sha='a'*64;self.source={'duration_s':924.}
        self.prepare={'source_sha256':self.source_sha,'audio_track':0,'recipe':'48k_stereo_16k_asr_v1'}
        for stage,paths in REQUIRED_OUTPUTS.items():
            for name in paths:
                p=self.flow.folder/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(name)
        separation={'audio':pipeline.digest(self.flow.folder/'audio/original.wav'),
            'weight_sha256':pipeline.DEMUCS_SHA,'segment':5,'shifts':0,'overlap':.25,'device':'mps'}
        background={'no_vocals_sha':pipeline.digest(self.flow.folder/'separated/htdemucs/original/no_vocals.wav'),
            'duration':924.,'code':pipeline.implementation_signature(('background_job',)),
            'gain_body_db':10,'gain_titles_db':4}
        for stage,sig in (('prepare',self.prepare),('separation',separation),('background',background)):
            self.flow.ledger[stage]={'fingerprint':pipeline.key(sig),'outputs':[
                {'path':str(self.flow.folder/p),'sha256':pipeline.digest(self.flow.folder/p)} for p in REQUIRED_OUTPUTS[stage]]}

    def run_preflight(self,**kw):
        with patch.object(pipeline.shutil,'disk_usage',return_value=types.SimpleNamespace(free=10*1024**3)):
            return self.flow.current_space_preflight({},self.source,self.source_sha,self.prepare,**kw)

    def test_current_verified_producers_credit_only_completed_costs(self):
        v=self.run_preflight()
        self.assertEqual(v['verified_reusable_stages'],['prepare','separation','background'])
        self.assertEqual(v['protected_reserve_bytes'],8*1024**3)
        self.assertEqual(v['estimated_new_peak_bytes'],1798579456)
        self.assertEqual(v['cleanup_bytes_credited'],0)

    def test_changed_prepare_cannot_credit_any_downstream_stage(self):
        (self.flow.folder/'audio/original.wav').write_text('changed')
        with self.assertRaisesRegex(RuntimeError,'空间不足'):self.run_preflight()

    def test_stale_separation_signature_blocks_background_credit(self):
        self.flow.ledger['separation']['fingerprint']='b'*64
        with self.assertRaisesRegex(RuntimeError,'空间不足'):self.run_preflight()

    def test_changed_background_signature_is_not_credited(self):
        self.flow.ledger['background']['fingerprint']='b'*64
        with self.assertRaisesRegex(RuntimeError,'空间不足'):self.run_preflight()

    def test_other_path_cannot_substitute_for_expected_output(self):
        item=self.flow.ledger['prepare']['outputs'][1];outside=self.root/'other.wav'
        outside.write_bytes((self.flow.folder/'audio/original.wav').read_bytes());item['path']=str(outside)
        with self.assertRaisesRegex(RuntimeError,'空间不足'):self.run_preflight()

    def test_actual_longer_edl_rechecks_future_storage(self):
        with self.assertRaisesRegex(RuntimeError,'空间不足'):
            self.run_preflight(planned_output_duration_s=1600.)

    def test_symlink_with_identical_bytes_has_no_credit(self):
        p=self.flow.folder/'audio/original.wav';other=self.root/'copy.wav';other.write_bytes(p.read_bytes())
        p.unlink();p.symlink_to(other)
        with self.assertRaisesRegex(RuntimeError,'空间不足'):self.run_preflight()

    def scratch_fixture(self):
        source=self.root/'input.mp4';source.write_bytes(b'unchanged source')
        self.source['input']=str(source);self.source_sha=pipeline.digest(source)
        self.prepare['source_sha256']=self.source_sha
        self.flow.ledger['prepare']['fingerprint']=pipeline.key(self.prepare)
        paths=[self.flow.folder/'audio'/name for name in ('background48k.wav','background_boosted_float.wav')]
        for p in paths:p.write_bytes(b'scratch'*32)
        return paths

    def test_verified_scratch_cleanup_preserves_master_and_uses_measured_free_space(self):
        paths=self.scratch_fixture();master=self.flow.folder/'audio/background_independent.wav'
        original=master.read_bytes();required=8*1024**3+1798579456
        def measured(_):return types.SimpleNamespace(free=required-300+sum(224 for p in paths if not p.exists()))
        with patch.object(pipeline.shutil,'disk_usage',side_effect=measured):
            value=self.flow.current_space_preflight({},self.source,self.source_sha,self.prepare)
        self.assertTrue(value['sufficient']);self.assertTrue(all(not p.exists() for p in paths))
        self.assertEqual(master.read_bytes(),original)
        self.assertEqual(value['cleanup_bytes_credited'],0)
        receipt=json.loads(Path(value['scratch_cleanup_receipt']).read_text())
        self.assertEqual(len(receipt['removed']),2);self.assertEqual(receipt['after']['free_bytes'],required+148)
        self.assertEqual(value['protected_reserve_bytes'],8*1024**3)
        self.assertEqual(value['runtime_hard_floor_bytes'],2*1024**3)

    def test_deletion_without_measured_reclamation_does_not_pass(self):
        paths=self.scratch_fixture()
        with patch.object(pipeline.shutil,'disk_usage',return_value=types.SimpleNamespace(free=9*1024**3)):
            with self.assertRaisesRegex(RuntimeError,'空间不足'):
                self.flow.current_space_preflight({},self.source,self.source_sha,self.prepare)
        self.assertTrue(all(not p.exists() for p in paths))
        self.assertFalse(json.loads((self.flow.folder/'space_preflight.json').read_text())['sufficient'])

    def test_changed_source_or_master_never_deletes_scratch(self):
        paths=self.scratch_fixture();Path(self.source['input']).write_bytes(b'changed source')
        with patch.object(pipeline.shutil,'disk_usage',return_value=types.SimpleNamespace(free=9*1024**3)):
            with self.assertRaisesRegex(RuntimeError,'原片身份'):
                self.flow.current_space_preflight({},self.source,self.source_sha,self.prepare)
        self.assertTrue(all(p.exists() for p in paths))
        Path(self.source['input']).write_bytes(b'unchanged source')
        (self.flow.folder/'audio/background_independent.wav').write_bytes(b'changed master')
        with patch.object(pipeline.shutil,'disk_usage',return_value=types.SimpleNamespace(free=9*1024**3)):
            with self.assertRaisesRegex(RuntimeError,'空间不足'):
                self.flow.current_space_preflight({},self.source,self.source_sha,self.prepare)
        self.assertTrue(all(p.exists() for p in paths))

    def test_symlink_scratch_never_deletes_its_target(self):
        paths=self.scratch_fixture();target=self.root/'other.wav';target.write_bytes(paths[0].read_bytes())
        for p in paths:p.unlink();p.symlink_to(target)
        with patch.object(pipeline.shutil,'disk_usage',return_value=types.SimpleNamespace(free=9*1024**3)):
            with self.assertRaisesRegex(RuntimeError,'空间不足'):
                self.flow.current_space_preflight({},self.source,self.source_sha,self.prepare)
        self.assertTrue(target.exists());self.assertTrue(all(p.is_symlink() for p in paths))

    def test_failed_export_estimate_is_saved_for_current_duration(self):
        with self.assertRaisesRegex(RuntimeError,'空间不足'):
            self.run_preflight(planned_output_duration_s=1600.)
        saved=json.loads((self.flow.folder/'export_space_preflight.json').read_text())
        self.assertEqual(saved['storage_duration_bound_s'],1600.)
        self.assertFalse(saved['sufficient']);self.assertGreater(saved['deficit_bytes'],0)

if __name__=='__main__':unittest.main()
