"""Independent adversarial source-language proofs; no model/API/production calls."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'web'))
import source_language_consensus as language
import proxy_client


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=True))


class FakeRecognizer:
    def __init__(self, text='Я побачив річку та полетів далі.', detected='uk', probability=.94):
        self.text, self.language, self.probability = text, detected, probability
        self.calls = []

    def transcribe(self, clip, **options):
        self.calls.append((clip, copy.deepcopy(options)))
        return iter([SimpleNamespace(text=self.text, start=0., end=3.9)]), SimpleNamespace(
            language=self.language, language_probability=self.probability)


class SourceLanguageConsensusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.folder = self.root/'jobs/fixture'
        self.evidence = self.folder/'revisions/source_review_v1/evidence/automatic'
        self.evidence.mkdir(parents=True)
        self.home = self.root/'home'
        self.primary_path = self.home/'.cache/huggingface/hub/models--Systran--faster-whisper-medium/snapshots/one'
        self.secondary_path = self.root/'installed/small'
        for path, content in ((self.primary_path,b'independent-medium-weights'),(self.secondary_path,b'independent-small-weights')):
            path.mkdir(parents=True)
            (path/'model.bin').write_bytes(content)
        write(self.root/'config/local.json', {'asr_model': str(self.secondary_path)})
        self.video = self.root/'source.mp4'
        # A real locally decodable 30-second source. Only the assigned complete
        # 10--14 second window contains these samples; neighboring windows do
        # not match. No speech model or remote service is involved.
        pcm = b'\x00\x00' * 160000 + b'\x01\x00' * 64000 + b'\x00\x00' * 256000
        with wave.open(str(self.video), 'wb') as stream:
            stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(16000)
            stream.writeframes(pcm)
        self.source_sha = sha(self.video)
        write(self.folder/'source.json', {'input':str(self.video),'duration_s':30.,'audio_track':0})
        write(self.root/'web/state/jobs/fixture.json', {'source_sha256':self.source_sha})
        self.row = {'segment_id':'line0002','speaker_id':'S01','start_s':10.,'end_s':14.,
                    'slot_end_s':14.05,'overlap':False,'text_de':'Und dann sehe ich es.',
                    'text_source':'Und dann sehe ich es.','source_language':'de'}
        previous = {**self.row,'segment_id':'line0001','start_s':8.,'end_s':9.8,'slot_end_s':9.9}
        following = {**self.row,'segment_id':'line0003','start_s':14.3,'end_s':17.,'slot_end_s':17.05}
        self.rows = [previous,self.row,following]
        write(self.folder/'segments.plan.json', {'segments':self.rows})
        write(self.folder/'segments.json', {'segments':self.rows})
        self.clip = self.evidence/'line0002_exact.wav'
        with wave.open(str(self.clip),'wb') as stream:
            stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(16000)
            stream.writeframes(b'\x01\x00'*64000)
        self.exact = {'kind':'exact','clip':str(self.clip.relative_to(self.folder)),
                      'clip_sha256':sha(self.clip),'window_start_s':10.,'window_end_s':14.,
                      'full_text':'An unreliable forced German interpretation.'}
        self.primary = FakeRecognizer()
        self.secondary = FakeRecognizer()
        self.home_patch = patch.object(Path,'home',return_value=self.home)
        self.home_patch.start();self.addCleanup(self.home_patch.stop)

    def detect(self):
        return language.detect_on_window(self.root,self.folder,self.row,self.exact,self.primary,
            self.primary_path,self.evidence,self.source_sha,secondary_factory=lambda path:self.secondary)

    def verify(self, proposal):
        return language.verify_proposal(self.root,self.folder,self.row,proposal['candidate'],
                                         proposal,self.source_sha)

    def proof(self, proposal):
        path = self.folder/proposal['language_evidence']['path']
        return path,json.loads(path.read_text())

    def raw(self, proposal):
        path,proof = self.proof(proposal)
        raw_path = self.folder/proof['detection']['report']['path']
        return path,proof,raw_path,json.loads(raw_path.read_text())

    def publish(self, proposal, proof, raw=None):
        proof_path = self.folder/proposal['language_evidence']['path']
        if raw is not None:
            raw_path = self.folder/proof['detection']['report']['path']
            write(raw_path,raw)
            ref = {'path':str(raw_path.relative_to(self.folder)),'sha256':sha(raw_path)}
            proof['detection']['report']=ref
            proof['transcription']['report']=copy.deepcopy(ref)
        write(proof_path,proof)
        proposal['language_evidence']['sha256']=sha(proof_path)

    def test_real_shape_two_different_installed_weights_complete_unprompted_cpu_window(self):
        original=(self.folder/'segments.plan.json').read_bytes()
        proposal=self.detect()
        self.assertEqual(proposal['language'],'uk')
        self.assertEqual(self.verify(proposal),'uk')
        _,proof,_,raw=self.raw(proposal)
        self.assertEqual(len(raw['observations']),2)
        self.assertEqual(len({o['model_sha256'] for o in raw['observations']}),2)
        self.assertFalse(proof['human_reviewed']);self.assertFalse(proof['source_words_verified'])
        for recognizer in (self.primary,self.secondary):
            self.assertEqual(len(recognizer.calls),1)
            self.assertEqual(recognizer.calls[0][0],str(self.clip))
            self.assertEqual(recognizer.calls[0][1],language.OPTIONS)
            self.assertIsNone(recognizer.calls[0][1]['language'])
            self.assertNotIn('initial_prompt',recognizer.calls[0][1])
            self.assertNotIn('hotwords',recognizer.calls[0][1])
        self.assertEqual((self.folder/'segments.plan.json').read_bytes(),original)

    def test_low_probability_disagreement_identical_weights_forced_prompt_gpu_rejected(self):
        proposal=self.detect();_,_,_,raw=self.raw(proposal)
        baseline=raw['observations']
        mutations=['low','nan','infinite','bool','different_language','same_language','same_weight',
                   'forced','prompted','gpu','float16','different_text']
        for mutation in mutations:
            observations=copy.deepcopy(baseline)
            if mutation=='low':observations[0]['language_probability']=.54
            elif mutation=='nan':observations[0]['language_probability']=float('nan')
            elif mutation=='infinite':observations[0]['language_probability']=float('inf')
            elif mutation=='bool':observations[0]['language_probability']=True
            elif mutation=='different_language':observations[1]['language']='en'
            elif mutation=='same_language':
                for o in observations:o['language']='de'
            elif mutation=='same_weight':observations[1]['model_sha256']=observations[0]['model_sha256']
            elif mutation=='forced':observations[0]['options']['language']='uk'
            elif mutation=='prompted':observations[0]['prompted']=True
            elif mutation=='gpu':observations[0]['device']='cuda'
            elif mutation=='float16':observations[0]['compute_type']='float16'
            else:observations[1]['text']='Completely unrelated sentence with nothing in common.'
            with self.subTest(mutation=mutation):self.assertIsNone(language.consensus(observations,'de'))

    def test_digits_negation_conditions_units_cannot_be_discarded(self):
        for original,candidate in [('Es sind 3 Menschen.','Люди тут.'),
            ('Ich bin nicht dort.','Я там.'),('Wenn es regnet, bleibe ich.','Я залишаюсь.'),
            ('Es kostet 5 Euro.','Це коштує 5 доларів.')]:
            with self.subTest(original=original):
                self.assertTrue(language.candidate_errors(original,candidate,'de'))

    def test_other_original_languages_cannot_silently_drop_negation_or_condition(self):
        for source,candidate,code in [('I did not go.','Я поїхав.','en'),
                ('If it rains, I stay.','Я залишаюсь.','en'),
                ('Я не погоджуюсь.','I agree.','uk')]:
            with self.subTest(language=code,source=source):
                self.assertTrue(language.candidate_errors(source,candidate,code))

    def test_wrong_model_weights_or_mutated_audio_or_evidence_sha_rejected(self):
        proposal=self.detect()
        for mutation in ('proof_sha','model','audio','source_video'):
            current=copy.deepcopy(proposal)
            saved_model=(self.primary_path/'model.bin').read_bytes()
            saved_clip=self.clip.read_bytes()
            saved_source=self.video.read_bytes()
            if mutation=='proof_sha':current['language_evidence']['sha256']='0'*64
            elif mutation=='model':(self.primary_path/'model.bin').write_bytes(b'mutated weights')
            elif mutation=='audio':self.clip.write_bytes(b'mutated audio')
            else:self.video.write_bytes(b'mutated source video')
            with self.subTest(mutation=mutation),self.assertRaises((ValueError,OSError)):
                self.verify(current)
            (self.primary_path/'model.bin').write_bytes(saved_model)
            self.clip.write_bytes(saved_clip)
            self.video.write_bytes(saved_source)

    def test_fake_auto_flags_schema_binding_and_forced_transcription_rejected(self):
        original=self.detect();_,baseline,_,raw=self.raw(original)
        mutations=['schema','segment','original_language','detection_mode','forced_language',
                   'transcription_mode','transcription_forced','transcription_other_report',
                   'raw_human','raw_source_verified','helper_sha','agreement','probability']
        for mutation in mutations:
            proposal=copy.deepcopy(original);proof=copy.deepcopy(baseline);observation=copy.deepcopy(raw)
            if mutation=='schema':proof['schema']='untrusted_claim'
            elif mutation=='segment':proof['segment_id']='line0003'
            elif mutation=='original_language':proof['original_source_language']='en'
            elif mutation=='detection_mode':proof['detection']['mode']='forced'
            elif mutation=='forced_language':proof['detection']['forced_language']='uk'
            elif mutation=='transcription_mode':proof['transcription']['mode']='forced'
            elif mutation=='transcription_forced':proof['transcription']['forced_language']='uk'
            elif mutation=='transcription_other_report':proof['transcription']['report']={'path':'other.json','sha256':'0'*64}
            elif mutation=='raw_human':observation['human_reviewed']=True
            elif mutation=='raw_source_verified':observation['source_words_verified']=True
            elif mutation=='helper_sha':observation['helper_sha256']='0'*64
            elif mutation=='agreement':proof['normalized_agreement']=.1
            else:proof['detection']['language_probability']=.1
            self.publish(proposal,proof,observation)
            if mutation=='transcription_other_report':
                proof['transcription']['report']={'path':'other.json','sha256':'0'*64};self.publish(proposal,proof)
            with self.subTest(mutation=mutation),self.assertRaises((ValueError,OSError)):
                self.verify(proposal)

    def test_neighbor_window_cropped_window_and_segment_text_time_spoof_rejected(self):
        original=self.detect();_,baseline,_,raw=self.raw(original)
        for mutation in ['neighbor','cropped','nan_window','text_spoof','segment_outside','segment_incomplete']:
            proposal=copy.deepcopy(original);proof=copy.deepcopy(baseline);observation=copy.deepcopy(raw)
            if mutation=='neighbor':observation['audio'].update(start_s=14.3,end_s=18.3)
            elif mutation=='cropped':observation['audio'].update(start_s=10.2,end_s=13.8)
            elif mutation=='nan_window':observation['audio']['start_s']=float('nan')
            elif mutation=='text_spoof':observation['observations'][0]['segments'][0]['text']='Other whole transcript'
            elif mutation=='segment_outside':observation['observations'][0]['segments'][0]['end_s']=8.
            else:observation['observations'][0]['segments'][0].update(start_s=0.,end_s=0.)
            proof['detection']['audio']=copy.deepcopy(observation['audio'])
            self.publish(proposal,proof,observation)
            with self.subTest(mutation=mutation),self.assertRaises((ValueError,OSError)):
                self.verify(proposal)

    def test_detect_cannot_accept_neighbor_or_trimmed_window_before_decoding(self):
        for start,end in [(14.3,18.3),(10.2,13.8),(9.,15.)]:
            exact=copy.deepcopy(self.exact)
            exact.update(window_start_s=start,window_end_s=end)
            with self.subTest(start=start,end=end):
                try:
                    proposal=language.detect_on_window(self.root,self.folder,self.row,exact,self.primary,
                        self.primary_path,self.evidence,self.source_sha,secondary_factory=lambda path:self.secondary)
                except ValueError:
                    proposal=None
                self.assertIsNone(proposal)
        self.assertFalse(self.primary.calls)

    def test_candidate_must_equal_complete_first_transcript_not_cropped_or_context(self):
        proposal=self.detect()
        for candidate in ('Я побачив річку.',proposal['candidate']+' Сусіднє речення.'):
            current=copy.deepcopy(proposal);current['candidate']=candidate
            _,proof=self.proof(current)
            proof['replacement_text_sha256']=hashlib.sha256(candidate.encode()).hexdigest()
            self.publish(current,proof)
            with self.assertRaises((ValueError,OSError)):self.verify(current)

    def test_all_refreshed_hashes_cannot_substitute_neighbor_pcm_or_short_audio(self):
        original=self.detect();_,baseline,_,raw=self.raw(original)
        for mutation in ('neighbor_pcm','short_pcm'):
            proposal=copy.deepcopy(original);proof=copy.deepcopy(baseline);observation=copy.deepcopy(raw)
            replacement=self.evidence/('substituted_'+mutation+'.wav')
            with wave.open(str(replacement),'wb') as stream:
                stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(16000)
                stream.writeframes(b'\x00\x00'*64000 if mutation=='neighbor_pcm' else b'\x01\x00'*48000)
            observation['audio'].update(path=str(replacement.relative_to(self.folder)),sha256=sha(replacement))
            observation['window_proof'].update(clip=observation['audio']['path'],clip_sha256=sha(replacement))
            proof['detection']['audio']=copy.deepcopy(observation['audio'])
            self.publish(proposal,proof,observation)
            with self.subTest(mutation=mutation),self.assertRaises((ValueError,OSError)):
                self.verify(proposal)

    def test_state_source_receipt_and_canonical_row_changes_rejected(self):
        proposal=self.detect()
        state=self.root/'web/state/jobs/fixture.json'
        original_state=state.read_bytes()
        write(state,{'source_sha256':'0'*64})
        with self.assertRaises(ValueError):self.verify(proposal)
        state.write_bytes(original_state)
        receipt=self.folder/'source.json';original_receipt=receipt.read_bytes()
        changed=json.loads(original_receipt);changed['duration_s']=29.
        write(receipt,changed)
        with self.assertRaises(ValueError):self.verify(proposal)
        receipt.write_bytes(original_receipt)
        changed=copy.deepcopy(self.rows);changed[1]['speaker_id']='S02'
        write(self.folder/'segments.plan.json',{'segments':changed})
        with self.assertRaises(ValueError):self.verify(proposal)

    def test_non_weight_model_assets_are_bound_to_actual_installed_model(self):
        tokenizer=self.primary_path/'tokenizer.json';tokenizer.write_text('{"vocab":"old"}')
        proposal=self.detect()
        tokenizer.write_text('{"vocab":"new"}')
        with self.assertRaises(ValueError):self.verify(proposal)

    def test_raw_non_object_cannot_look_like_valid_language_observations(self):
        proposal=self.detect();_,proof=self.proof(proposal)
        self.publish(proposal,proof,[])
        with self.assertRaises(ValueError):self.verify(proposal)

    def test_callers_cannot_downgrade_user_flags_on_the_canonical_source_plan(self):
        for flag in ('source_word_confirmed_by_user','source_words_verified','human_reviewed','user_contextual_adoption'):
            changed=copy.deepcopy(self.rows);changed[1][flag]=True
            write(self.folder/'segments.plan.json',{'segments':changed})
            clean=copy.deepcopy(self.row);clean[flag]=False
            with self.subTest(flag=flag):
                try:
                    proposal=language.detect_on_window(self.root,self.folder,clean,self.exact,self.primary,
                        self.primary_path,self.evidence,self.source_sha,secondary_factory=lambda path:self.secondary)
                except ValueError:
                    proposal=None
                self.assertIsNone(proposal)
        self.assertFalse(self.primary.calls)

    def commit_proposal(self, proposal):
        replacement=copy.deepcopy(self.row)
        replacement.update(text_de=proposal['candidate'],text_source=proposal['candidate'],
                           source_language=proposal['language'])
        derived=copy.deepcopy(self.rows);derived[1]=replacement
        derived_path=self.folder/'revisions/source_review_v1/segments.plan.json'
        write(derived_path,{'segments':derived})
        change={'segment_id':self.row['segment_id'],'action':'replace_source_text',
                'original_text_sha256':hashlib.sha256(self.row['text_de'].encode()).hexdigest(),
                'replacement_text_sha256':hashlib.sha256(proposal['candidate'].encode()).hexdigest(),
                'evidence':[proposal['language_evidence']],
                'original_source_language':'de','replacement_source_language':proposal['language'],
                'language_evidence':proposal['language_evidence']}
        revision={'schema':'source_plan_revision_v1','input_source_sha256':self.source_sha,
            'original_plan_sha256':sha(self.folder/'segments.plan.json'),
            'revised_plan_sha256':sha(derived_path),'changes':[change],
            'unresolved':[],'blocking_unresolved':[]}
        write(self.folder/'source_plan_revision.json',revision)
        write(self.folder/'translation/input.json',{'segments':derived})
        return replacement,revision,derived

    def test_committed_language_replay_uses_exact_proof_without_pipeline_recursion(self):
        proposal=self.detect();replacement,revision,derived=self.commit_proposal(proposal)
        import pipeline
        with patch.object(pipeline,'reviewed_source_plan',side_effect=AssertionError('no recursive pipeline import')):
            for value in (replacement,replacement['text_de']):
                actual=language.verify_committed_language_evidence(self.root,self.folder,self.row,value,
                    proposal['language_evidence'],self.source_sha,self.rows)
                self.assertEqual(actual,'uk')
        self.assertEqual((self.folder/'segments.plan.json').read_text(),
            json.dumps({'segments':self.rows},ensure_ascii=False,allow_nan=True))

    def test_committed_language_replay_rejects_changed_before_derived_input_or_ref(self):
        proposal=self.detect();replacement,revision,derived=self.commit_proposal(proposal)
        receipt=self.folder/'source_plan_revision.json'
        for mutation in ('before','derived_language','input','change_ref','allocation'):
            before=copy.deepcopy(self.row);current=copy.deepcopy(replacement);allocation=copy.deepcopy(self.rows)
            changed=copy.deepcopy(revision);inputs=copy.deepcopy(derived)
            if mutation=='before':before['speaker_id']='S09'
            elif mutation=='derived_language':current['source_language']='en'
            elif mutation=='input':inputs[1]['text_de']='not the committed transcript'
            elif mutation=='change_ref':changed['changes'][0]['language_evidence']={'path':'missing.json','sha256':'0'*64}
            else:allocation[0]['end_s']=9.7
            write(receipt,changed);write(self.folder/'translation/input.json',{'segments':inputs})
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):
                language.verify_committed_language_evidence(self.root,self.folder,before,current,
                    proposal['language_evidence'],self.source_sha,allocation)
        write(receipt,revision);write(self.folder/'translation/input.json',{'segments':derived})

    def test_shared_exact_window_validator_checks_source_pcm_and_current_allocation(self):
        checked=language.validate_exact_source_window(self.root,self.folder,self.row,self.exact,
            self.source_sha,self.rows)
        self.assertTrue(checked['complete_source_pcm_verified'])
        self.assertFalse(checked['human_reviewed'])
        changed=copy.deepcopy(self.rows);changed[0]['end_s']=10.1
        with self.assertRaises(ValueError):
            language.validate_exact_source_window(self.root,self.folder,self.row,self.exact,
                self.source_sha,changed)

    def test_real_decoder_numpy_scalars_are_normalized_before_consensus_and_json(self):
        import numpy as np
        def transcribe(clip, **options):
            return iter([SimpleNamespace(text='Я побачив річку та полетів далі.',
                start=np.float64(0.),end=np.float64(3.9))]),SimpleNamespace(
                language='uk',language_probability=np.float64(.94))
        self.primary.transcribe=transcribe
        self.secondary.transcribe=transcribe
        proposal=self.detect()
        self.assertIsNotNone(proposal)
        self.assertEqual(self.verify(proposal),'uk')
        _,_,_,raw=self.raw(proposal)
        for observation in raw['observations']:
            self.assertIs(type(observation['language_probability']),float)
            self.assertIs(type(observation['segments'][0]['start_s']),float)
            self.assertIs(type(observation['segments'][0]['end_s']),float)

    def test_committed_replay_accepts_exact_old_input_until_next_pipeline_translation_commit(self):
        proposal=self.detect();replacement,revision,derived=self.commit_proposal(proposal)
        write(self.folder/'translation/input.json',{'segments':self.rows})
        actual=language.verify_committed_language_evidence(self.root,self.folder,self.row,replacement,
            proposal['language_evidence'],self.source_sha,self.rows)
        self.assertEqual(actual,'uk')
        for mutation in ('other_source','other_speaker','target_unbound_language'):
            changed=copy.deepcopy(self.rows)
            if mutation=='other_source':changed[0].update(text_de='Unbound changed source',text_source='Unbound changed source')
            elif mutation=='other_speaker':changed[0]['speaker_id']='S09'
            else:changed[1]['source_language']='en'
            write(self.folder/'translation/input.json',{'segments':changed})
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):
                language.verify_committed_language_evidence(self.root,self.folder,self.row,replacement,
                    proposal['language_evidence'],self.source_sha,self.rows)

    def prepare_short_window(self):
        self.row['end_s']=11.2;self.row['slot_end_s']=11.25
        self.rows[1]=self.row
        write(self.folder/'segments.plan.json',{'segments':self.rows})
        write(self.folder/'segments.json',{'segments':self.rows})
        with wave.open(str(self.clip),'wb') as stream:
            stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(16000)
            stream.writeframes(b'\x01\x00'*19200)
        self.exact.update(window_end_s=11.2,clip_sha256=sha(self.clip))

    def test_short_safe_window_skips_language_exception_but_bad_proof_still_blocks(self):
        self.prepare_short_window()
        self.assertIsNone(self.detect())
        self.assertEqual(self.primary.calls,[]);self.assertEqual(self.secondary.calls,[])
        self.exact['clip_sha256']='0'*64
        with self.assertRaises(ValueError):self.detect()

    def test_short_window_cannot_skip_neighbor_pcm_even_when_clip_sha_is_refreshed(self):
        self.prepare_short_window()
        with wave.open(str(self.clip),'wb') as stream:
            stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(16000)
            stream.writeframes(b'\x00\x00'*19200)
        self.exact['clip_sha256']=sha(self.clip)
        with self.assertRaises(ValueError):self.detect()
        self.assertEqual(self.primary.calls,[]);self.assertEqual(self.secondary.calls,[])

    def test_user_confirmed_or_adopted_source_cannot_take_automatic_language_path(self):
        hints=self.folder/'translation/source_review_hints.json'
        for flag in ('source_word_confirmed_by_user','user_contextual_adoption'):
            write(hints,{'schema':'source_review_hints_v1','rows':{'line0002':{
                flag:True,'source_text_sha256':hashlib.sha256(self.row['text_de'].encode()).hexdigest()}}})
            with self.subTest(flag=flag):
                try:
                    proposal=self.detect()
                except ValueError:
                    proposal=None
                self.assertIsNone(proposal)
        self.assertEqual(self.primary.calls,[])


if __name__=='__main__':unittest.main()
