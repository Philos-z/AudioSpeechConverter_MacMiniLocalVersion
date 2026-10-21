"""Source exceptions require private local registration and unchanged evidence."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import wave


ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'web' / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


holds = module('picture_holds')
native = module('resolve_native_export')
reviews = module('review_compilation')


class LocalScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.folder = self.root / 'jobs' / 'synthetic_case'
        self.folder.mkdir(parents=True)

    def config(self, **sections):
        path = self.root / 'config/source_scopes.local.json'
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({'schema': 'local_source_scopes_v1', **sections}))
        return path

    def native_registration(self):
        return {
            'job': self.folder.name, 'segment_id': 'line0001',
            'child_job': 'synthetic_child', 'source_row_count': 1,
            'selected_source_index': 0, 'speaker_id': 'S00',
            'registry': {key: 'a' * 64 for key in native.REGISTRY_KEYS},
            'reference_relative_path': 'references/synthetic.wav',
            'source_window_s': [0.0, 1.0],
            'raw_reference_segment_ids': ['seg0001'],
            'reference_audio': {'frames': 24000, 'sample_rate': 24000,
                                'channels': 1, 'subtype': 'PCM_24'},
            'expected_characters': 1,
        }

    def test_ordinary_picture_path_preserves_pending_issues(self):
        edl = {'issues': [{'reason': 'pending'}], 'clips': []}
        self.assertIs(holds.apply(self.folder, edl), edl)
        self.assertEqual(edl['issues'], [{'reason': 'pending'}])
        self.assertEqual(holds.validate_edl(edl), {})

    def test_ordinary_audio_path_returns_original_manifest(self):
        manifest = self.folder / 'not_read.json'
        result = native.resolve(self.root, self.folder.name, manifest,
                                self.folder / 'unused.json')
        self.assertFalse(result['resolved'])
        self.assertEqual(result['path'], str(manifest))
        self.assertFalse((self.folder / 'unused.json').exists())

    def test_special_receipts_without_local_registration_are_rejected(self):
        (self.folder / 'picture_hold_review.json').write_text('{}')
        (self.folder / 'native_audio_selections.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'registered'):
            holds.apply(self.folder, {'issues': [], 'clips': []})
        with self.assertRaisesRegex(ValueError, 'registered'):
            native.resolve(self.root, self.folder.name,
                           self.folder / 'manifest.json', self.folder / 'output.json')
        with self.assertRaisesRegex(ValueError, 'registered'):
            reviews.compile_record(self.folder, {'job': self.folder.name})

    def test_empty_release_configuration_registers_no_exceptions(self):
        self.config()
        for call in (lambda: holds.registration(self.folder),
                     lambda: native.registration(self.root, self.folder.name),
                     lambda: reviews.registration(self.folder)):
            with self.assertRaisesRegex(ValueError, 'registered'):
                call()

    def test_registration_cannot_cross_jobs_or_follow_symlinks(self):
        path = self.config(picture_holds={'job': 'other_case',
                                        'ids': ['line0001'], 'source_row_count': 1})
        with self.assertRaises(ValueError):
            holds.registration(self.folder)
        path.unlink()
        target = self.root / 'external.json'
        target.write_text(json.dumps({'schema': 'local_source_scopes_v1'}))
        path.symlink_to(target)
        for call in (lambda: holds.registration(self.folder),
                     lambda: native.registration(self.root, self.folder.name),
                     lambda: reviews.registration(self.folder)):
            with self.assertRaises(ValueError):
                call()

    def test_native_registration_requires_complete_hash_bindings(self):
        value = self.native_registration()
        self.config(native_audio_selection=value)
        self.assertEqual(native.registration(self.root, self.folder.name), value)
        value['registry'].pop('source_video')
        self.config(native_audio_selection=value)
        with self.assertRaisesRegex(ValueError, 'SHA'):
            native.registration(self.root, self.folder.name)

    def test_native_source_coverage_and_review_flags_still_enforced(self):
        self.config(native_audio_selection=self.native_registration())
        path = self.folder / 'segments.json'
        row = {'segment_id': 'line0001', 'translation_reviewed': True,
               'speaker_reviewed': True}
        path.write_text(json.dumps({'segments': [row]}))
        self.assertEqual(native.source_rows(self.root, self.folder.name)[1], [row])
        row['speaker_reviewed'] = False
        path.write_text(json.dumps({'segments': [row]}))
        with self.assertRaisesRegex(ValueError, 'Unreviewed'):
            native.source_rows(self.root, self.folder.name)

    def test_local_registration_is_not_an_authorization_receipt(self):
        self.config(picture_holds={'job': self.folder.name,
                                  'ids': ['line0001'], 'source_row_count': 1})
        for authorization in ({}, {'choice': holds.CHOICE, 'human_reviewed': True},
                              {'choice': holds.CHOICE, 'human_reviewed': False,
                               'origin': 'actual_user_answer', 'user_authorized': False,
                               'answer_text': 'synthetic'}):
            with self.assertRaises(ValueError):
                holds.make_receipt(self.folder, {}, authorization)

    def test_registered_picture_hold_still_checks_actual_audio_and_receipt(self):
        self.config(picture_holds={'job': self.folder.name,
                                  'ids': ['line0001'], 'source_row_count': 1})
        source = self.folder / 'source.bin'
        source.write_bytes(b'synthetic source identity')
        audio = self.folder / 'synthetic.wav'
        with wave.open(str(audio), 'wb') as handle:
            handle.setnchannels(1)
            handle.setsampwidth(3)
            handle.setframerate(24000)
            handle.writeframes(b'\0' * (36000 * 3))
        row = {'segment_id': 'line0001', 'speaker_id': 'S00', 'start_s': 0.0,
               'end_s': 0.8, 'text_de': 'synthetic', 'text_zh': 'synthetic'}
        (self.folder / 'segments.json').write_text(json.dumps({'segments': [row]}))
        (self.folder / 'speakers.json').write_text('{}')
        (self.folder / 'source.json').write_text(json.dumps({'input': str(source),
                                                          'duration_s': 1.0}))
        issue = {'reason': 'complete_audio_exceeds_source_window',
                 'ids': ['line0001'], 'speech_s': 1.5,
                 'available_source_s': 1.0, 'excess_s': 0.5}
        edl = {'source_input': str(source), 'fps': 25, 'issues': [issue],
               'utterances': [{**row, 'clip_index': 0, 'speech_s': 1.5,
                               'audio': str(audio), 'audio_frames': 36000,
                               'audio_sample_rate': 24000,
                               'audio_sha256': holds.sha(audio)}],
               'clips': [{'kind': 'speech', 'segment_ids': ['line0001'],
                          'speech_s': 1.5, 'source_start_s': 0.0,
                          'source_duration_s': 1.0, 'source_end_s': 1.0,
                          'frames': 47, 'freeze_s': 0.88, 'pause_s': 0.38}]}
        authorization = {'choice': holds.CHOICE, 'human_reviewed': False,
                         'origin': 'operator_machine_draft',
                         'user_authorized': False, 'answer_text': None}
        receipt = holds.make_receipt(self.folder, edl, authorization)
        (self.folder / 'picture_hold_review.json').write_text(json.dumps(receipt))
        result = holds.apply(self.folder, copy.deepcopy(edl))
        self.assertEqual(result['issues'], [])
        self.assertEqual(set(holds.validate_edl(result)), {'line0001'})
        audio.write_bytes(b'changed evidence')
        with self.assertRaises((ValueError, wave.Error, EOFError)):
            holds.validate_edl(result)

    def test_review_compilation_cannot_skip_explicit_records(self):
        value = {'job': self.folder.name, 'narrow_ids': ['line0001'],
                 'cpu_source_id': 'line0001', 'grounded_translation_count': 1,
                 'grounded_group_count': 1}
        self.config(review_compilation=value)
        self.assertEqual(reviews.registration(self.folder), value)
        with self.assertRaisesRegex(ValueError, 'explicit local compilation'):
            reviews.derive(self.folder, {}, {'job': self.folder.name})


if __name__ == '__main__':
    unittest.main()
