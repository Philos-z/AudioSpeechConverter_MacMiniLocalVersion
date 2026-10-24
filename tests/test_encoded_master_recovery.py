"""Real tiny FFmpeg loudnorm/AAC/H264 remux, no TTS, model or network."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'web'))
import encoded_master_recovery as repair
import media_recovery as media


def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value))


@unittest.skipUnless(Path(repair.FFMPEG).is_file(),'Local FFmpeg required')
class EncodedMasterTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.folder=self.root/'jobs/fixture';self.output=self.folder/'outputs/candidate'
        self.output.mkdir(parents=True)
        self.space=patch.object(repair,'space');self.space.start();self.addCleanup(self.space.stop)
        self.video=self.output/'group_zh_candidate.mp4'
        self.ff(['-f','lavfi','-i','sine=frequency=440:duration=8','-ar','48000','-ac','2','-c:a','pcm_f32le',self.output/'premix.wav'])
        self.ff(['-i',self.output/'premix.wav','-af','volume=0.25','-c:a','pcm_s24le',self.output/'zh_mix.wav'])
        self.ff(['-i',self.output/'zh_mix.wav','-c:a','aac','-b:a','128k',self.output/'zh_mix.m4a'])
        self.ff(['-f','lavfi','-i','color=c=black:s=96x54:r=25:d=8','-i',self.output/'zh_mix.m4a',
            '-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p','-c:a','copy',
            '-metadata:s:a:0','language=zho','-disposition:a:0','default',self.video])
        self.source=self.root/'original.mp4';self.source.write_bytes(b'fixture original source identity')
        self.reference=self.folder/'reference.wav';shutil.copyfile(self.output/'zh_mix.wav',self.reference)
        self.group_audio=self.folder/'accepted_group.wav';shutil.copyfile(self.output/'zh_mix.wav',self.group_audio)
        self.side=self.group_audio.with_suffix('.json')
        save(self.side,{'schema':'continuous_utterance_v1','sha256':media.sha(self.group_audio),
            'content':{'text_zh':'完整中文。','effective_tts_text':'完整中文。','reference_sha256':media.sha(self.reference)}})
        row={'segment_id':'line0001','speaker_id':'S1','text_zh':'完整中文。','text_de':'Ganzer Satz.',
             'start_s':0.,'end_s':8.,'source_ids':['seg0001']}
        self.group={'group_id':'group1','speaker_id':'S1','member_ids':['line0001'],'text_zh':row['text_zh'],
            'text_de':row['text_de'],'reference':{'path':str(self.reference),'sha256':media.sha(self.reference)}}
        save(self.folder/'segments.json',{'segments':[row]});save(self.folder/'speakers.json',{'S1':{'reference':str(self.reference)}})
        save(self.folder/'source.json',{'input':str(self.source),'duration_s':8.,'audio_track':0})
        self.plan=self.folder/'tts/plan.json'
        save(self.plan,{'schema':'pretts_semantic_group_plan_v1','job':'fixture','source_rows':[row],
            'source_segments':media.record(self.folder,self.folder/'segments.json'),
            'source_speakers':media.record(self.folder,self.folder/'speakers.json'),
            'source_binding':{'source_receipt_path':str(self.folder/'source.json'),'source_receipt_sha256':media.sha(self.folder/'source.json'),
                'source_video_path':str(self.source),'source_video_sha256':media.sha(self.source),'source_video_bytes':self.source.stat().st_size},
            'tts_groups':[self.group]})
        self.edl={'frames':200,'duration_s':8.,'issues':[],'partial_preview':False,'source_input':str(self.source),
            'utterances':[{**self.group,'audio':str(self.group_audio),'audio_sha256':media.sha(self.group_audio),
                'generation_sidecar':str(self.side),'generation_sidecar_sha256':media.sha(self.side)}]}
        save(self.output/'edit_decisions.json',self.edl)
        save(self.output/'group_adapter_report.json',{'plan':str(self.plan),'plan_sha256':media.sha(self.plan),
            'source_video':{'sha256':media.sha(self.source)},'partial_preview':False,'audio_truncated':False})
        save(self.output/'group_mix_report.json',{'edl_sha256':media.sha(self.output/'edit_decisions.json'),
            'master_sha256':media.sha(self.output/'zh_mix.wav'),'placements':[{'accepted':'unchanged'}]})
        save(self.output/'mastering_receipt.json',{'premix_sha256':media.sha(self.output/'premix.wav'),
            'requested_strategy':'constant_test','identity':{'fixture':True},'outputs':{n:{'sha256':media.sha(self.output/n)} for n in ['zh_mix.wav','zh_mix.m4a']}})
        save(self.output/'group_export.json',{'output':str(self.video),'frames':200,'duration_s':8.,
            'size_limit':{'bytes':self.video.stat().st_size,'output_sha256':media.sha(self.video)}})

    def ff(self,args):
        subprocess.run([repair.FFMPEG,'-v','error','-threads','1','-y',*[str(a) for a in args]],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)

    def recover(self):return repair.recover(self.root,self.folder,self.output)

    def marker(self,result):
        directory=self.folder/'tts/encoded_master_recovery'/result['token'];binding=result['original_input'];targets=[]
        for name in repair.TARGETS:
            proof=binding['files'].get(name) if name!='audio_master_recovery.json' else binding.get('prior_report')
            targets.append({'name':name,'before_sha256':proof['sha256'] if proof else None,
                'after_sha256':media.sha(directory/'stage'/name),
                'backup':media.record(self.folder,directory/'history'/name) if proof else None})
        document={'schema':repair.SCHEMA,'candidate':str(self.output),'token':result['token'],'targets':targets}
        save(self.output/'audio_master_recovery.pending.json',document);return document

    def test_real_two_pass_aac_remux_and_fast_idempotent_reuse(self):
        originals={n:media.sha(self.output/n) for n in repair.FILES}
        result=self.recover();self.assertTrue(result['changed']);self.assertFalse(result['reused'])
        self.assertFalse(repair.levels_ok(result['levels_before']));self.assertTrue(repair.levels_ok(result['levels_new_aac']))
        self.assertTrue(repair.levels_ok(result['levels_final']));self.assertTrue(result['video_bitstream_unchanged'])
        self.assertEqual(result['pcm_frames'],384000);self.assertLess(self.video.stat().st_size,50_000_000)
        for name,digest in originals.items():self.assertEqual(media.sha(Path(result['history'])/name),digest)
        self.assertEqual(media.sha(self.output/'premix.wav'),originals['premix.wav'])
        self.assertEqual(media.read(self.output/'group_mix_report.json')['placements'],[{'accepted':'unchanged'}])
        accepted={p['path']:media.sha(Path(p['path'])) for p in result['original_input']['accepted_group_audio']}
        with patch.object(repair,'scan',side_effect=AssertionError('reuse must not rescan')):
            self.assertTrue(self.recover()['reused'])
            self.assertTrue(self.recover()['reused'])
        self.assertEqual({p:media.sha(Path(p)) for p in accepted},accepted)
        self.assertEqual(len(media.journal(self.folder)['entries']),1)

    def test_good_encoded_levels_are_noop_and_persist_current_report(self):
        original=media.sha(self.video)
        with patch.object(repair,'scan',return_value={'input_i':'-16','input_tp':'-2','input_lra':'4'}):result=self.recover()
        self.assertFalse(result['changed']);self.assertEqual(media.sha(self.video),original)
        self.assertFalse(media.read(self.output/'audio_master_recovery.json')['changed'])
        self.assertEqual(media.journal(self.folder)['entries'],[])

    def test_invalid_or_nonfinite_levels_cannot_authorize_repair(self):
        for value in ('nan','inf','-inf'):
            with self.assertRaises(ValueError):repair.levels_ok({'input_i':value,'input_tp':'-2','input_lra':'4'})
        self.assertTrue(repair.levels_ok({'input_i':'-17','input_tp':'-1.5','input_lra':'11'}))
        self.assertFalse(repair.levels_ok({'input_i':'-17.02','input_tp':'-2.08','input_lra':'3.2'}))

    def test_stale_master_export_or_original_source_is_rejected(self):
        self.source.write_bytes(b'changed source')
        with self.assertRaises(ValueError):self.recover()
        self.assertEqual(media.journal(self.folder)['entries'],[])

    def test_completed_journal_pending_marker_is_finalized_without_rollback_or_rescan(self):
        result=self.recover();self.marker(result);after=media.sha(self.video)
        with patch.object(repair,'scan',side_effect=AssertionError('no repeated processing')):again=self.recover()
        self.assertTrue(again['reused']);self.assertEqual(media.sha(self.video),after)
        self.assertFalse((self.output/'audio_master_recovery.pending.json').exists())

    def test_pending_uncommitted_transaction_restores_exact_originals(self):
        result=self.recover();self.marker(result)
        path=self.folder/'tts/media_recovery_journal.json';journal=media.read(path)
        journal['entries'][0]['status']='pending';journal['entries'][0].pop('result');save(path,journal)
        repair.restore_pending(self.folder,self.output)
        for name,proof in result['original_input']['files'].items():self.assertEqual(media.sha(self.output/name),proof['sha256'])
        self.assertFalse((self.output/'audio_master_recovery.json').exists())
        self.assertEqual(media.journal(self.folder)['entries'][0]['status'],'pending')

    def test_pending_target_whitelist_and_token_cannot_modify_foreign_files(self):
        result=self.recover();marker=self.marker(result);path=self.output/'audio_master_recovery.pending.json'
        for mutate in [lambda d:d['targets'].append(d['targets'][0]),lambda d:d['targets'][0].update(name='../../segments.json'),
                       lambda d:d.update(token='0'*64),lambda d:d['targets'][0]['backup'].update(path='segments.json')]:
            bad=copy.deepcopy(marker);mutate(bad);save(path,bad)
            before=media.sha(self.video)
            with self.assertRaises(ValueError):repair.restore_pending(self.folder,self.output)
            self.assertEqual(media.sha(self.video),before)

    def test_completed_report_cannot_forge_action_binding_or_output_coverage(self):
        self.recover();path=self.output/'audio_master_recovery.json';original=media.read(path)
        for mutate in [lambda d:d.update(token='0'*64),lambda d:d['original_input'].update(source_video_sha256='0'*64),
                       lambda d:d.update(outputs=[]),lambda d:d.update(video_bitstream_unchanged=False)]:
            bad=copy.deepcopy(original);mutate(bad);save(path,bad)
            with self.assertRaises(ValueError):self.recover()
        save(path,original)
        journal_path=self.folder/'tts/media_recovery_journal.json';journal=media.read(journal_path)
        journal['entries'][0]['action']='picture_holds';save(journal_path,journal)
        with self.assertRaises(ValueError):self.recover()

    def test_actual_failure_before_commit_preserves_every_original_delivery_file(self):
        originals={n:media.sha(self.output/n) for n in repair.FILES};real=repair.command
        def fail(argv,log,out):
            if log.name=='remux.stderr.log':raise RuntimeError('synthetic remux failure')
            return real(argv,log,out)
        with patch.object(repair,'command',side_effect=fail):
            with self.assertRaises(RuntimeError):self.recover()
        self.assertEqual({n:media.sha(self.output/n) for n in repair.FILES},originals)
        self.assertFalse((self.output/'audio_master_recovery.pending.json').exists())


if __name__=='__main__':unittest.main()
