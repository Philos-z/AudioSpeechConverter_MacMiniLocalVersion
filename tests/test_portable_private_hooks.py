"""Synthetic public-build boundaries; no task, model, service or network use."""
import importlib
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'web'))
from portable_private_hooks import check_context, PrivateScopeUnavailable
import user_context_adoption
import committed_review_targets
import user_voice_adoption_20261103 as voice
import tagesschau_quality_tolerance_20261105 as tolerance


class PublicScopeBoundaries(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.folder=self.root/'jobs/synthetic_case'
        self.folder.mkdir(parents=True)

    def test_empty_scope_keeps_new_task_on_ordinary_path(self):
        check_context(self.root,self.folder)
        self.assertEqual(user_context_adoption.load_adoptions(self.root,self.folder,[]),{})
        self.assertEqual(committed_review_targets.load_targets(self.root,self.folder,[]),{})
        manifest=self.folder/'tts/ordinary/manifest.json'
        self.assertEqual(voice.resolve(self.root,self.folder,None,manifest),manifest)
        self.assertFalse(manifest.exists())

    def test_grouped_pipeline_keeps_ordinary_manifest_readable(self):
        import pipeline
        manifest=self.folder/'tts/ordinary/manifest.json'
        manifest.parent.mkdir(parents=True)
        value={'schema':'synthetic_complete_manifest','utterances':[]}
        manifest.write_text(json.dumps(value))
        selected=voice.resolve(self.root,self.folder,self.folder/'tts/plan.json',manifest)
        self.assertEqual(selected,manifest)
        self.assertEqual(pipeline.read(selected),value)

    def test_present_private_credentials_never_silently_disappear(self):
        for relative in ('translation/user_context_adoption.json',
                         'translation/frozen_saved_reviews_synthetic.json',
                         'recovery/bounded_adjudication/receipt.json',
                         'tts/native_generation_failure/request.json'):
            path=self.folder/relative;path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text('{}')
            with self.assertRaises(PrivateScopeUnavailable):
                user_context_adoption.load_adoptions(self.root,self.folder,[])
            path.unlink()
            # Empty private scope directories also remain explicit evidence.
            while path.parent!=self.folder and not any(path.parent.iterdir()):
                path.parent.rmdir();path=path.parent

    def test_symlink_private_credential_is_rejected(self):
        path=self.folder/'translation/user_context_adoption.json'
        path.parent.mkdir();path.symlink_to(self.root/'absent.json')
        with self.assertRaises(PrivateScopeUnavailable):check_context(self.folder)

    def test_unregistered_local_configuration_is_rejected(self):
        path=self.root/'config/source_scopes.local.json';path.parent.mkdir()
        path.write_text(json.dumps({'schema':'local_source_scopes_v1','new_exception':{}}))
        with self.assertRaises(PrivateScopeUnavailable):check_context(self.folder)

    def test_root_voice_reply_is_scoped_to_its_actual_job(self):
        path=self.root/'reports/tagesschau_daily/synthetic/three_voice_decision_synthetic/keep_current_user_reply.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'job_id':'another_synthetic_case'}))
        check_context(self.folder)
        path.write_text(json.dumps({'job_id':self.folder.name}))
        with self.assertRaises(PrivateScopeUnavailable):voice.accept_quality(self.root,self.folder)

    def test_existing_portable_registry_names_do_not_enable_new_exceptions(self):
        path=self.root/'config/source_scopes.local.json';path.parent.mkdir()
        path.write_text(json.dumps({'schema':'local_source_scopes_v1','picture_holds':{}}))
        check_context(self.folder)
        with self.assertRaises(PrivateScopeUnavailable):
            user_context_adoption._load_legacy_adoptions(self.root,self.folder)

    def test_exact_pronunciation_remains_required_for_ordinary_audio(self):
        group={'text_zh':'这是合成测试。','tts_pronunciation_text':'这是合成测试。'}
        content={'text_zh':group['text_zh'],'effective_tts_text':group['tts_pronunciation_text']}
        self.assertTrue(voice.pronunciation_matches(group,content,'a'*64))
        content['effective_tts_text']='另一段测试。'
        with self.assertRaises(PrivateScopeUnavailable):voice.pronunciation_matches(group,content,'a'*64)

    def test_quality_hook_preserves_every_unknown_finding(self):
        findings={'line0001':{'approved':False,'issues':['synthetic uncertainty']}}
        result,adoptions=tolerance.apply({},findings,{},[],self.folder)
        self.assertIs(result,findings);self.assertEqual(adoptions,{})

    def test_explicit_private_operations_and_cli_refuse_without_registration(self):
        for name in ('explicit_continuation','bounded_adjudication',
                     'native_generation_recovery_20261105','explicit_voice_attempt'):
            module=importlib.import_module(name)
            operation=next(getattr(module,method) for method in ('prepare','preflight','register','validate','decision')
                           if hasattr(module,method))
            with self.assertRaises(PrivateScopeUnavailable):operation(self.root,self.folder)
        result=subprocess.run([sys.executable,'-B',str(ROOT/'web/explicit_continuation.py')],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('PrivateScopeUnavailable',result.stderr)

    def test_historical_rejections_cannot_be_closed_by_adapter(self):
        from bounded_adjudication import autonomous_diagnostic_resolved
        self.assertFalse(autonomous_diagnostic_resolved(self.folder,'line0001',{}, {},0))


if __name__=='__main__':unittest.main()
