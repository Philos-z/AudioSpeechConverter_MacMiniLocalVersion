"""Independent shared-cache audio boundary; real PCM24, no synthesis or ASR."""
import copy
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
import wave

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'web'));sys.path.insert(0,str(ROOT/'tests'))
import test_speech_content_quality as fixture
import speech_content_quality as speech
import media_recovery as media


class SharedNativeAudioBoundaryTests(unittest.TestCase):
    def setUp(self):
        f=fixture.WholeManifest();f.setUp();self.addCleanup(f.doCleanups)
        groups,native=f.setup_manifest()
        self.f=f;self.group=groups[0];self.entry=copy.deepcopy(native[0])
        old=Path(self.entry['audio'])
        with wave.open(str(old),'rb') as handle:
            raw=handle.readframes(handle.getnframes());rate=handle.getframerate()
        content={'schema':'native_mlx_whole_utterance_v1','text_zh':self.group['text_zh'],
            'effective_tts_text':self.group['text_zh'],'reference_sha256':self.group['reference']['sha256'],
            'model_identity':{'fixture':'native-cache-validator-no-model-run'},'parameters':{}}
        key=media.key(content)
        self.path=f.root/'cache/native_mlx_semantic_groups_v2/audio'/(key+'.wav')
        self.path.parent.mkdir(parents=True)
        with wave.open(str(self.path),'wb') as handle:
            handle.setnchannels(1);handle.setsampwidth(3);handle.setframerate(rate)
            handle.writeframes(b''.join(b'\x00'+raw[i:i+2] for i in range(0,len(raw),2)))
        self.entry.update(audio=str(self.path),path=str(self.path),cache_key=key,content=content,
                          sha256=media.sha(self.path))
        self.sidecar=self.path.with_suffix('.json')
        media.save(self.sidecar,{'schema':'native_mlx_whole_utterance_v1','cache_key':key,
            'content':content,'sha256':self.entry['sha256'],'frames':self.entry['frames'],
            'sample_rate':self.entry['sample_rate'],'duration_s':self.entry['duration_s'],'channels':1})

    def bound(self, entry=None):
        return speech.bound_audio(self.f.root,self.f.folder,self.f.plan,self.f.plan_path,
            self.f.manifest_path,self.group,entry or self.entry)

    def test_real_registered_shared_native_pcm24_passes_original_cache_validator(self):
        before=self.path.read_bytes();sidecar=self.sidecar.read_bytes()
        audio=self.bound()
        self.assertEqual(audio['path'],str(self.path))
        self.assertEqual(audio['sha256'],self.entry['sha256'])
        self.assertFalse(self.path.is_relative_to(self.f.folder))
        self.assertEqual(self.path.read_bytes(),before);self.assertEqual(self.sidecar.read_bytes(),sidecar)

    def test_identical_project_audio_outside_exact_shared_namespace_is_rejected(self):
        alternate=self.f.root/'other_audio'/self.path.name;alternate.parent.mkdir()
        shutil.copyfile(self.path,alternate);shutil.copyfile(self.sidecar,alternate.with_suffix('.json'))
        entry=copy.deepcopy(self.entry);entry['audio']=str(alternate)
        with self.assertRaises(ValueError):self.bound(entry)

    def test_outside_project_or_symlink_cannot_use_a_good_shared_fingerprint(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        alternate=Path(temporary.name).resolve()/self.path.name
        shutil.copyfile(self.path,alternate);shutil.copyfile(self.sidecar,alternate.with_suffix('.json'))
        entry=copy.deepcopy(self.entry);entry['audio']=str(alternate)
        with self.assertRaises(ValueError):self.bound(entry)
        self.path.unlink();self.path.symlink_to(alternate)
        with self.assertRaises(ValueError):self.bound()

    def test_wrong_sha_content_key_or_sidecar_cannot_reuse_an_existing_wave(self):
        for mutation in ('sha','key','target','sidecar'):
            entry=copy.deepcopy(self.entry);before=self.sidecar.read_bytes()
            if mutation=='sha':entry['sha256']='0'*64
            elif mutation=='key':entry['cache_key']='0'*64
            elif mutation=='target':entry['content']['text_zh']='没有批准的新正文。'
            else:
                value=media.read(self.sidecar);value['frames']-=1;media.save(self.sidecar,value)
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):self.bound(entry)
            self.sidecar.write_bytes(before)

    def test_shared_cache_identity_alone_never_grants_parent_job_or_source_permission(self):
        value=media.read(self.f.manifest_path);value['utterances'][0]=self.entry
        media.save(self.f.manifest_path,value)
        # The content-addressed sound is valid, but this plan has no registered
        # child/permission. It must fail before a recognizer is even considered.
        with self.assertRaises(ValueError):
            speech.validated_manifest(self.f.root,self.f.folder,self.f.plan,self.f.plan_path,self.f.manifest_path)


class SharedNativeWholeSourceBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.SharedNativeCache();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.groups,self.native,self.child,self.manifest=self.f.setup_shared_manifest()

    def validated(self):
        return speech.validated_manifest(self.f.root,self.f.folder,self.f.plan,self.f.plan_path,self.f.manifest_path)

    def test_complete_real_child_and_shared_pcm24_are_accepted_without_source_edits(self):
        before={path:path.read_bytes() for path in self.f.folder.rglob('*.json')}
        self.assertEqual(self.validated(),self.manifest)
        for group,native in zip(self.groups,self.native):
            sound=speech.bound_audio(self.f.root,self.f.folder,self.f.plan,self.f.plan_path,
                self.f.manifest_path,group,native)
            self.assertEqual(sound['sha256'],native['sha256'])
        for path,data in before.items():self.assertEqual(path.read_bytes(),data)

    def test_complete_cache_does_not_authorize_another_source_child_or_changed_text(self):
        for name in ('source.json','segments.json'):
            path=self.child/name;before=path.read_bytes();value=media.read(path)
            if name=='source.json':value['input']=str(self.f.root/'different_source.mp4')
            else:value['segments'][0]['text_zh']='不属于已绑定计划的台词。'
            media.save(path,value)
            with self.subTest(name=name),self.assertRaises(ValueError):self.validated()
            path.write_bytes(before)

    def test_refreshed_plan_and_child_hashes_cannot_grant_missing_processing_permission(self):
        permission_path=self.f.folder/'processing_request.json'
        permission=media.read(permission_path);permission['processing_authorized']=False
        media.save(permission_path,permission)
        self.f.plan['tts_job']['processing_permission_sha256']=media.sha(permission_path)
        media.save(self.f.plan_path,self.f.plan)
        binding=media.read(self.child/'group_plan_binding.json')
        binding['group_plan_sha256']=media.sha(self.f.plan_path)
        media.save(self.child/'group_plan_binding.json',binding)
        permission.update(parent_job=self.f.folder.name,child_role='semantic_groups_tts_only',
            group_plan_path=str(self.f.plan_path),group_plan_sha256=media.sha(self.f.plan_path))
        media.save(self.child/'processing_request.json',permission)
        with self.assertRaises(ValueError):self.validated()

    def test_symlinked_shared_namespace_is_rejected_even_with_identical_registered_pcm(self):
        namespace=self.f.root/'cache/native_mlx_semantic_groups_v2'
        moved=namespace.with_name('moved_native_cache');namespace.rename(moved)
        namespace.symlink_to(moved,target_is_directory=True)
        with self.assertRaises(ValueError):
            speech.bound_audio(self.f.root,self.f.folder,self.f.plan,self.f.plan_path,
                self.f.manifest_path,self.groups[0],self.native[0])


if __name__=='__main__':unittest.main()
