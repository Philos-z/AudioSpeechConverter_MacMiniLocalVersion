"""Only a committed, current remux may reconcile the mix/render ledger."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'web'))
import pipeline


class EncodedMasterPipelineTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name).resolve()
        self.flow=pipeline.Flow(self.root,{'id':'web_master_fixture'},lambda *a:None,None,lambda:False)
        self.candidate=self.flow.folder/'outputs/groups_fixture'
        self.candidate.mkdir(parents=True)
        self.adapter=self.root/'web/group_compact_adapter.py'
        self.adapter.parent.mkdir();self.adapter.write_text('adapter')
        (self.root/'scripts').mkdir()
        (self.root/'scripts/encoded_master_recovery.py').write_text('fixture helper entry')
        (self.adapter.parent/'verify_natural_compact.py').write_text('verifier')
        for name in ('edit_decisions.json','group_mix_report.json','zh_mix.wav','zh_mix.m4a',
                     'zh.srt','de.srt','group_export.json','group_zh_candidate.mp4'):
            (self.candidate/name).write_text(name)
        self.report={'changed':True,'job':self.flow.name,'candidate':str(self.candidate),
            'edl_sha256':pipeline.digest(self.candidate/'edit_decisions.json'),
            'master_sha256':pipeline.digest(self.candidate/'zh_mix.wav'),
            'output_sha256':pipeline.digest(self.candidate/'group_zh_candidate.mp4'),
            'video_bitstream_unchanged':True}
        self.dependencies={'renderer':'fixed','mastering':'constant_requested'}

    def publish(self):
        pipeline.save(self.candidate/'audio_master_recovery.json',self.report)

    def repair(self,publish=True):
        def command(*args,**kwargs):
            if publish:self.publish()
        with patch.object(self.flow,'command',side_effect=command) as child:
            result=self.flow.recover_encoded_master(self.candidate,self.adapter,self.dependencies)
        self.assertIn('--job',list(map(str,child.call_args.args[3])))
        return result

    def test_committed_repair_reconciles_only_mix_and_render(self):
        result=self.repair()
        self.assertTrue(result['changed']);self.assertFalse(result['reused'])
        self.assertEqual(set(self.flow.ledger),{'export_mix','export_render'})
        for phase in ('mix','render'):
            signature=self.flow.export_phase_signature(self.candidate,self.adapter,self.dependencies,
                phase,'group_zh_candidate.mp4')
            self.assertTrue(self.flow.cached('export_'+phase,signature,[]))
        self.assertTrue(self.flow.metrics['encoded_master_recovery']['full_verifier_still_required'])

    def test_complete_receipt_reconciles_interrupted_ledger_without_new_repair(self):
        self.publish()
        self.assertTrue(self.repair(publish=False)['reused'])
        self.assertNotIn('export_verify',self.flow.ledger)

    def test_levels_already_valid_never_rewrites_receipts(self):
        self.report={'changed':False}
        self.assertFalse(self.repair())
        self.assertEqual(self.flow.ledger,{})

    def test_stale_or_other_candidate_receipt_rejected(self):
        for name in ('job','candidate','edl_sha256','master_sha256','output_sha256'):
            with self.subTest(field=name):
                original=self.report[name];self.report[name]='wrong'
                with self.assertRaisesRegex(RuntimeError,'未绑定'):self.repair()
                self.assertEqual(self.flow.ledger,{})
                self.report[name]=original

    def test_uncommitted_transaction_rejected(self):
        (self.candidate/'audio_master_recovery.pending.json').write_text('{}')
        with self.assertRaisesRegex(RuntimeError,'未绑定'):self.repair()
        self.assertEqual(self.flow.ledger,{})

    def test_changed_video_bitstream_rejected(self):
        self.report['video_bitstream_unchanged']=False
        with self.assertRaisesRegex(RuntimeError,'未绑定'):self.repair()
        self.assertEqual(self.flow.ledger,{})

    def test_final_signature_requires_actual_master_video_and_verifier(self):
        before=self.flow.export_phase_signature(self.candidate,self.adapter,self.dependencies,
            'verify','group_zh_candidate.mp4')
        self.assertEqual(before['master'],self.report['master_sha256'])
        self.assertEqual(before['video'],self.report['output_sha256'])
        (self.candidate/'zh_mix.wav').write_text('new master')
        after=self.flow.export_phase_signature(self.candidate,self.adapter,self.dependencies,
            'verify','group_zh_candidate.mp4')
        self.assertNotEqual(before,after)


if __name__=='__main__':unittest.main()
