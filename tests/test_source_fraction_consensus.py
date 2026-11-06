"""Adversarial fraction evidence tests; no real recognizer or remote request."""
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
sys.path.insert(0, str(ROOT / 'web'))
import source_fraction_consensus as fraction
from translation_recovery import source_alternative_errors


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=True))


class FakeRecognizer:
    def __init__(self, text):
        self.text = text
        self.calls = []
        self.segments = None

    def transcribe(self, clip, **options):
        self.calls.append((clip, copy.deepcopy(options)))
        segments = self.segments or [SimpleNamespace(text=self.text, start=0., end=3.9)]
        return iter(segments), SimpleNamespace(language='de', language_probability=.99)


class FractionMaskTests(unittest.TestCase):
    def test_masks_only_fraction_spans_and_keeps_all_other_characters(self):
        original = 'Falls nötig, nutzen etwa 2,3 der 24 Länder nicht mehr als 5 Euro.'
        candidate = 'Falls nötig, nutzen etwa zwei Drittel der 24 Länder nicht mehr als 5 Euro.'
        masked = fraction.mask_fraction_pair(original, candidate)
        self.assertEqual(masked, (original.replace('2,3', 'ASRFRACTIONPAIR'),
                                  candidate.replace('zwei Drittel', 'ASRFRACTIONPAIR')))
        self.assertEqual(masked[0], masked[1])
        # The generic guard remains strict without this bound evidence path.
        self.assertTrue(source_alternative_errors(original, candidate, 'de'))

    def test_ratios_supported_with_single_digit_words_and_inflection(self):
        for original, candidate in [('1,2 von ihnen', 'ein Halb von ihnen'),
                                     ('3,4 der Länder', 'drei Viertel der Länder'),
                                     ('2,3 der Länder', 'zwei Dritteln der Länder')]:
            with self.subTest(original=original):
                self.assertIsNotNone(fraction.mask_fraction_pair(original, candidate))

    def test_money_units_wrong_digits_multiple_and_nonratio_are_ineligible(self):
        pairs = [('2,3 Euro', 'zwei Drittel Euro'), ('2,3 Liter', 'zwei Drittel Liter'),
                 ('2,3 Prozent', 'zwei Drittel Prozent'), ('2,3', 'zwei Drittel'),
                 ('2.3 der Länder', 'zwei Drittel der Länder'),
                 ('12,3 der Länder', 'zwei Drittel der Länder'),
                 ('2,30 der Länder', 'zwei Drittel der Länder'),
                 ('2,3 der Länder', 'drei Viertel der Länder'),
                 ('2,3 der Länder', 'zwei Drittel Euro'),
                 ('2,3 der A und 2,3 der B', 'zwei Drittel der A und zwei Drittel der B'),
                 ('2,3 der A', 'zwei Drittel der A und ein Viertel der B'),
                 ('2,3 der A', 'zweidrittel der A')]
        for original, candidate in pairs:
            with self.subTest(original=original, candidate=candidate):
                self.assertIsNone(fraction.mask_fraction_pair(original, candidate))

    def test_mask_does_not_hide_other_number_negation_condition_or_comparison_changes(self):
        original = 'Falls nötig, haben etwa 2,3 der 24 Länder nicht mehr als 5 Euro.'
        candidate = original.replace('2,3', 'zwei Drittel')
        self.assertTrue(fraction._candidate_safe(original, candidate))
        for changed in [candidate.replace('24', '25'), candidate.replace('5 Euro', ''),
                        candidate.replace('nicht ', ''), candidate.replace('Falls nötig, ', ''),
                        candidate.replace('mehr als ', ''), candidate.replace('etwa ', ''),
                        candidate.replace('Euro.', 'Euro und 5 Stimmen.')]:
            with self.subTest(candidate=changed):
                self.assertFalse(fraction._candidate_safe(original, changed))


class SourceFractionConsensusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.folder = self.root / 'jobs/fixture'
        self.evidence = self.folder / 'revisions/source_review_v1/evidence/automatic'
        self.evidence.mkdir(parents=True)
        self.home = self.root / 'home'
        hub = self.home / '.cache/huggingface/hub'
        self.medium = hub / 'models--Systran--faster-whisper-medium/snapshots/medium'
        self.small = hub / 'models--Systran--faster-whisper-small/snapshots/small'
        for path, data in ((self.medium, b'installed-medium'), (self.small, b'installed-small')):
            path.mkdir(parents=True)
            (path / 'model.bin').write_bytes(data)
            (path / 'config.json').write_bytes(b'{"fixture":true}')
        write(self.root / 'config/local.json', {'asr_model': str(self.small)})
        self.video = self.root / 'source.mp4'
        # Real PCM in a decodable container, with distinctive 10--14s samples.
        with wave.open(str(self.video), 'wb') as stream:
            stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
            stream.writeframes(b'\x00\x00' * 160000 + b'\x01\x00' * 64000
                               + b'\x00\x00' * 256000)
        self.source_sha = sha(self.video)
        write(self.folder / 'source.json', {'input': str(self.video), 'duration_s': 30., 'audio_track': 0})
        write(self.root / 'web/state/jobs/fixture.json', {'source_sha256': self.source_sha})
        self.original = 'Etwa 2,3 der 24 Länder haben nicht mehr als 5 Euro.'
        self.candidate = 'Etwa zwei Drittel der 24 Länder haben nicht mehr als 5 Euro.'
        self.row = {'segment_id': 'line0006', 'speaker_id': 'S01', 'start_s': 10., 'end_s': 14.,
                    'slot_end_s': 14.05, 'overlap': False, 'text_de': self.original,
                    'text_source': self.original, 'source_language': 'de'}
        self.rows = [{**self.row, 'segment_id': 'line0005', 'start_s': 8., 'end_s': 9.8, 'slot_end_s': 9.9},
                     self.row,
                     {**self.row, 'segment_id': 'line0007', 'start_s': 14.3, 'end_s': 17., 'slot_end_s': 17.05}]
        write(self.folder / 'segments.plan.json', {'segments': self.rows})
        write(self.folder / 'segments.json', {'segments': self.rows})
        self.clip = self.evidence / 'line0006_exact.wav'
        with wave.open(str(self.clip), 'wb') as stream:
            stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
            stream.writeframes(b'\x01\x00' * 64000)
        self.exact = {'kind': 'exact', 'clip': str(self.clip.relative_to(self.folder)),
                      'clip_sha256': sha(self.clip), 'window_start_s': 10., 'window_end_s': 14.,
                      'full_text': self.candidate, 'bounded_text': self.candidate}
        self.record = {'alternatives': {'original': self.original, 'exact': self.candidate},
            'evidence': [self.exact], 'decoding': {'policy': 'bounded_same_window_lexical_hint_v1',
                'hotwords': [], 'hinted_candidate_used': False, 'human_verified': False},
            'model_path': str(self.medium), 'model_sha256': sha(self.medium / 'model.bin'),
            'original_text_sha256': hashlib.sha256(self.original.encode()).hexdigest(),
            'input_source_sha256': self.source_sha, 'source_words_verified': False,
            'human_reviewed': False, 'method': 'local_cpu_asr_exact_and_contextual_windows_v1'}
        self.recognizer = FakeRecognizer(self.candidate)
        self.model_constructors = []
        def factory(path, **options):
            self.model_constructors.append((path, copy.deepcopy(options)))
            return self.recognizer
        patches = [patch.object(Path, 'home', return_value=self.home),
                   patch.dict(sys.modules, {'faster_whisper': SimpleNamespace(WhisperModel=factory)})]
        for item in patches:
            item.start(); self.addCleanup(item.stop)

    def attach(self, record=None):
        return fraction.attach(self.root, self.folder, self.row,
                               self.record if record is None else record, self.evidence, self.source_sha)

    def verify(self, record):
        return fraction.verify(self.root, self.folder, self.row, record, self.candidate, self.source_sha)

    def proof(self, record):
        path = self.folder / record['fraction_proposal']['fraction_evidence']['path']
        return path, json.loads(path.read_text())

    def publish(self, record, proof):
        path, _ = self.proof(record)
        write(path, proof)
        record['fraction_proposal']['fraction_evidence']['sha256'] = sha(path)

    def test_complete_real_pcm_two_installed_models_and_immutable_original(self):
        old_plan = (self.folder / 'segments.plan.json').read_bytes()
        old_record = copy.deepcopy(self.record)
        attached = self.attach()
        self.assertTrue(self.verify(attached))
        self.assertNotIn('fraction_proposal', self.record)
        self.assertEqual(self.record, old_record)
        self.assertEqual((self.folder / 'segments.plan.json').read_bytes(), old_plan)
        _, proof = self.proof(attached)
        self.assertFalse(proof['human_reviewed']); self.assertFalse(proof['source_words_verified'])
        self.assertEqual(proof['base_record'], old_record)
        self.assertEqual(proof['precommit_source_rows'], self.rows)
        self.assertNotEqual(proof['medium']['model_sha256'], proof['small']['model_sha256'])
        self.assertEqual(self.recognizer.calls, [(str(self.clip), fraction.OPTIONS)])
        self.assertEqual(self.model_constructors[0][1], {'device': 'cpu', 'compute_type': 'int8',
            'cpu_threads': 2, 'num_workers': 1, 'local_files_only': True})
        self.assertNotIn('initial_prompt', self.recognizer.calls[0][1])
        self.assertNotIn('hotwords', self.recognizer.calls[0][1])
        self.assertEqual(self.attach(attached), attached)
        self.assertEqual(len(self.recognizer.calls), 1)

    def test_nonmatching_candidate_does_not_load_or_call_any_model(self):
        record = copy.deepcopy(self.record)
        record['alternatives']['exact'] = self.original
        self.assertEqual(self.attach(record), record)
        self.assertFalse(self.model_constructors)
        self.assertFalse(self.recognizer.calls)
        self.assertFalse(self.verify(record))

    def test_derived_choice_exclusions_do_not_change_raw_evidence_identity(self):
        attached = self.attach()
        attached['excluded_source_choices'] = {'contextual': ['not selectable']}
        self.assertTrue(self.verify(attached))
        attached['alternatives']['contextual'] = 'An unrecorded alternative.'
        with self.assertRaises(ValueError): self.verify(attached)

    def test_other_row_bound_revision_allows_unchanged_original_fraction_row(self):
        evidence = self.evidence / 'other_row_machine_evidence.json'
        write(evidence, {'fixture': 'bound machine source evidence'})
        derived = copy.deepcopy(self.rows)
        derived[0]['text_de'] = derived[0]['text_source'] = 'Die andere vollständige Zeile.'
        derived_path = self.folder / 'revisions/source_review_v1/segments.plan.json'
        write(derived_path, {'segments': derived})
        write(self.folder / 'source_plan_revision.json', {
            'schema': 'source_plan_revision_v1', 'input_source_sha256': self.source_sha,
            'original_plan_sha256': sha(self.folder / 'segments.plan.json'),
            'revised_plan_sha256': sha(derived_path), 'changes': [{
                'segment_id': 'line0005', 'action': 'replace_source_text',
                'original_text_sha256': hashlib.sha256(self.original.encode()).hexdigest(),
                'replacement_text_sha256': hashlib.sha256(derived[0]['text_de'].encode()).hexdigest(),
                'evidence': [{'path': str(evidence.relative_to(self.folder)), 'sha256': sha(evidence)}]}]})
        write(self.folder / 'translation/input.json', {'segments': derived})
        attached = self.attach()
        self.assertTrue(self.verify(attached))
        _, proof = self.proof(attached)
        self.assertEqual(proof['precommit_source_rows'], derived)
        # Reusing the original input after a revision cannot validate evidence.
        write(self.folder / 'translation/input.json', {'segments': self.rows})
        with self.assertRaises(ValueError): self.verify(attached)

    def test_disagreement_preserves_observation_without_proposal(self):
        self.recognizer.text = self.candidate.replace('zwei Drittel', 'drei Viertel')
        result = self.attach()
        self.assertNotIn('fraction_proposal', result)
        paths = list(self.evidence.glob('*fraction_observations_*.json'))
        self.assertEqual(len(paths), 1)
        self.assertEqual(json.loads(paths[0].read_text())['small']['text'], self.recognizer.text)

    def test_secondary_cannot_remove_facts_or_qualifiers(self):
        for text in [self.candidate.replace('24', '25'), self.candidate.replace('nicht ', ''),
                     self.candidate.replace('Etwa ', ''), self.candidate.replace('mehr als ', '')]:
            self.recognizer.text = text
            with self.subTest(text=text):
                self.assertNotIn('fraction_proposal', self.attach())

    def test_hinted_or_wrong_original_medium_record_rejected_before_small(self):
        for mutation in ('hotword', 'hinted', 'proof_prompt', 'proof_human', 'original_sha',
                         'model_sha', 'candidate_fulltext', 'source_sha', 'method'):
            record = copy.deepcopy(self.record)
            if mutation == 'hotword': record['decoding']['hotwords'] = ['Drittel']
            elif mutation == 'hinted': record['decoding']['hinted_candidate_used'] = True
            elif mutation == 'proof_prompt': record['evidence'][0]['initial_prompt'] = self.candidate
            elif mutation == 'proof_human': record['human_reviewed'] = True
            elif mutation == 'original_sha': record['original_text_sha256'] = '0' * 64
            elif mutation == 'model_sha': record['model_sha256'] = '0' * 64
            elif mutation == 'candidate_fulltext': record['evidence'][0]['full_text'] = 'zwei Drittel'
            elif mutation == 'source_sha': record['input_source_sha256'] = '0' * 64
            else: record['method'] = 'untrusted'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): self.attach(record)
        self.assertFalse(self.recognizer.calls)

    def test_protected_row_revised_row_same_weights_and_missing_small_rejected(self):
        self.row['human_reviewed'] = True
        with self.assertRaises(ValueError): self.attach()
        self.row.pop('human_reviewed')
        # This mutation keeps the physical original but violates current plan identity.
        changed = copy.deepcopy(self.rows)
        changed[1]['text_de'] = changed[1]['text_source'] = self.candidate
        write(self.folder / 'segments.plan.json', {'segments': changed})
        with self.assertRaises(ValueError): self.attach()
        write(self.folder / 'segments.plan.json', {'segments': self.rows})
        original_weights = (self.small / 'model.bin').read_bytes()
        (self.small / 'model.bin').write_bytes((self.medium / 'model.bin').read_bytes())
        with self.assertRaises(ValueError): self.attach()
        (self.small / 'model.bin').write_bytes(original_weights)
        (self.small / 'model.bin').unlink()
        with self.assertRaises(ValueError): self.attach()
        self.assertFalse(self.recognizer.calls)

    def test_all_refreshed_hashes_cannot_substitute_neighbor_pcm_or_short_window(self):
        for mutation in ('neighbor_pcm', 'short_pcm', 'neighbor_window', 'cropped_window', 'overlap'):
            record = copy.deepcopy(self.record)
            if mutation in ('neighbor_pcm', 'short_pcm'):
                replacement = self.evidence / (mutation + '.wav')
                with wave.open(str(replacement), 'wb') as stream:
                    stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
                    stream.writeframes(b'\x00\x00' * 64000 if mutation == 'neighbor_pcm'
                                       else b'\x01\x00' * 48000)
                record['evidence'][0].update(clip=str(replacement.relative_to(self.folder)),
                                             clip_sha256=sha(replacement))
            elif mutation == 'neighbor_window': record['evidence'][0].update(window_start_s=14.3, window_end_s=18.3)
            elif mutation == 'cropped_window': record['evidence'][0].update(window_start_s=10.2, window_end_s=13.8)
            else: self.row['overlap'] = True
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): self.attach(record)
            self.row['overlap'] = False
        self.assertFalse(self.recognizer.calls)

    def test_claimed_proof_mutations_fail_even_after_refreshed_reference_hash(self):
        original = self.attach()
        _, baseline = self.proof(original)
        for mutation in ('schema', 'source_binding', 'input_binding', 'base_record', 'helper_sha',
                         'candidate', 'raw_human', 'small_prompted', 'small_hotwords',
                         'small_gpu', 'small_segments', 'small_text', 'medium_text', 'medium_assets'):
            record = copy.deepcopy(original); proof = copy.deepcopy(baseline)
            if mutation == 'schema': proof['schema'] = 'untrusted'
            elif mutation == 'source_binding': proof['source_binding']['start_s'] = 11.
            elif mutation == 'input_binding': proof['input_binding']['source_input_rows_sha256'] = '0' * 64
            elif mutation == 'base_record': proof['base_record']['method'] = 'untrusted'
            elif mutation == 'helper_sha': proof['helper_sha256'] = '0' * 64
            elif mutation == 'candidate': proof['candidate'] += ' Extra.'
            elif mutation == 'raw_human': proof['human_reviewed'] = True
            elif mutation == 'small_prompted': proof['small']['prompted'] = True
            elif mutation == 'small_hotwords': proof['small']['options']['hotwords'] = 'Drittel'
            elif mutation == 'small_gpu': proof['small']['device'] = 'cuda'
            elif mutation == 'small_segments': proof['small']['segments'][0]['end_s'] = 8.
            elif mutation == 'small_text':
                proof['small']['text'] = proof['small']['text'].replace('zwei Drittel', 'drei Viertel')
                proof['small']['segments'][0]['text'] = proof['small']['text']
            elif mutation == 'medium_text': proof['medium']['text'] = 'zwei Drittel'
            else: proof['medium']['model_assets'][0]['sha256'] = '0' * 64
            self.publish(record, proof)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): self.verify(record)
        # Restore the proof so later tests can use an unaltered reference.
        self.publish(original, baseline)
        self.assertTrue(self.verify(original))

    def test_source_model_audio_input_and_selected_text_changes_invalidate(self):
        attached = self.attach()
        for path in (self.video, self.clip, self.medium / 'model.bin', self.small / 'model.bin',
                     self.small / 'config.json'):
            old = path.read_bytes()
            path.write_bytes(old + b'mutation')
            with self.subTest(path=path), self.assertRaises((ValueError, OSError)):
                self.verify(attached)
            path.write_bytes(old)
        with self.assertRaises(ValueError):
            fraction.verify(self.root, self.folder, self.row, attached, 'zwei Drittel', self.source_sha)
        changed = copy.deepcopy(self.rows)
        changed[0]['speaker_id'] = 'S02'
        write(self.folder / 'segments.plan.json', {'segments': changed})
        with self.assertRaises(ValueError): self.verify(attached)

    def test_bad_secondary_segment_times_and_hinted_evidence_cannot_gain_proposal(self):
        for start, end in [(0., 8.), (float('nan'), 3.9), (0., 0.)]:
            self.recognizer.segments = [SimpleNamespace(text=self.candidate, start=start, end=end)]
            with self.subTest(start=start, end=end), self.assertRaises(ValueError): self.attach()


if __name__ == '__main__':
    unittest.main()
