"""Offline fixtures for machine-bound per-row languages; no ASR/API calls."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'web'))
import pipeline
import proxy_client as pc


def sha(data):return hashlib.sha256(data).hexdigest()


class EvidencedMixedLanguagesTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='mixed-language-fixture-')
        self.addCleanup(temporary.cleanup);self.root=Path(temporary.name).resolve()
        self.folder=self.root/'jobs/fixture';self.evidence=self.folder/'revisions/source_review_v1/evidence/language'
        self.evidence.mkdir(parents=True)
        self.video=self.root/'source.mp4';self.video.write_bytes(b'offline source identity fixture')
        self.source_sha=sha(self.video.read_bytes())
        self.original=[self.row('line0107','Es sind nicht 3 Menschen hier.',10.,11.),
                       self.row('line0108','Unzuverlaessige alte Erkennung.',11.2,13.2)]
        self.revised=copy.deepcopy(self.original)
        self.revised[1].update(text_de='Відстань 3 km.',text_source='Відстань 3 km.',source_language='uk')
        self.plan=self.folder/'segments.plan.json';self.write(self.plan,{'segments':self.original})
        self.clip=self.evidence/'line0108_exact.wav';self.clip.write_bytes(b'offline audio hash fixture, not an ASR result')
        self.proof_path=self.evidence/'line0108_language.json'
        self.proof={'schema':'machine_row_language_evidence_v1','segment_id':'line0108',
            'input_source_sha256':self.source_sha,'source_binding':pc.source_binding(self.original[1]),
            'original_source_language':'de','detected_source_language':'uk',
            'replacement_text_sha256':sha(self.revised[1]['text_de'].encode()),
            'human_reviewed':False,'source_words_verified':False,
            'detection':{'mode':'auto','language':'uk','language_probability':.96,'model':'offline fixture model',
                'audio':{'path':str(self.clip.relative_to(self.folder)),'sha256':sha(self.clip.read_bytes()),
                         'start_s':11.2,'end_s':13.2}}}
        self.publish()

    def row(self,sid,text,start,end):
        return {'segment_id':sid,'speaker_id':'S00','start_s':start,'end_s':end,
                'text_de':text,'text_source':text,'source_language':'de','target_characters':20}

    def write(self,path,value):
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(value,ensure_ascii=False))

    def publish(self,change_update=None):
        self.write(self.proof_path,self.proof)
        ref={'path':str(self.proof_path.relative_to(self.folder)),'sha256':sha(self.proof_path.read_bytes())}
        derived=self.folder/'revisions/source_review_v1/segments.plan.json'
        self.write(derived,{'segments':self.revised})
        change={'segment_id':'line0108','action':'replace_source_text',
            'original_text_sha256':sha(self.original[1]['text_de'].encode()),
            'replacement_text_sha256':sha(self.revised[1]['text_de'].encode()),'evidence':[ref],
            'original_source_language':'de','replacement_source_language':'uk','language_evidence':ref}
        if change_update:change_update(change)
        self.write(self.folder/'source_plan_revision.json',{'schema':'source_plan_revision_v1',
            'original_plan_sha256':sha(self.plan.read_bytes()),'input_source_sha256':self.source_sha,
            'revised_plan_sha256':sha(derived.read_bytes()),'changes':[change],
            'unresolved':[],'blocking_unresolved':[]})

    def reviewed(self):return pipeline.reviewed_source_plan(self.folder,self.plan,self.source_sha)

    def client(self,targets=None):
        values={'line0107':'这里没有3人。','line0108':'距离3公里。'}
        if targets:values.update(targets)
        class Client:
            model='offline-mixed-fixture'
            def __init__(self):self.calls=[];self.records=[]
            def json(self,system,data):
                self.calls.append((system,copy.deepcopy(data)))
                return {'translations':[{'id':item['id'],'text_zh':values[item['id']],
                    'approved':True,'issues':[]} for item in data['items']]}
        return Client()

    def test_language_change_requires_current_machine_proof_and_preserves_original(self):
        original_bytes=self.plan.read_bytes();rows,receipt=self.reviewed()
        self.assertEqual(rows[1]['source_language'],'uk')
        self.assertEqual(self.plan.read_bytes(),original_bytes)
        exception=receipt['language_exceptions']['line0108']
        self.assertEqual(exception['source_binding'],pc.source_binding(self.revised[1]))
        self.assertFalse(exception['human_reviewed']);self.assertFalse(exception['source_words_verified'])
        self.publish(lambda change:change.pop('language_evidence'))
        with self.assertRaises(ValueError):self.reviewed()
        self.publish(lambda change:change.pop('original_source_language'))
        with self.assertRaises(ValueError):self.reviewed()

    def test_forced_stale_or_unbound_detection_does_not_authorize_language_changes(self):
        original=copy.deepcopy(self.proof)
        mutations=[lambda p:p['detection'].update(mode='forced'),
                   lambda p:p['detection'].update(forced_language='uk'),
                   lambda p:p.update(human_reviewed=True),
                   lambda p:p.update(source_words_verified=True),
                   lambda p:p.update(input_source_sha256='0'*64),
                   lambda p:p['source_binding'].update(speaker_id='S01'),
                   lambda p:p.update(replacement_text_sha256='0'*64),
                   lambda p:p['detection'].update(language='ru'),
                   lambda p:p['detection'].update(language_probability=float('nan')),
                   lambda p:p['detection']['audio'].update(sha256='0'*64),
                   lambda p:p['detection']['audio'].update(start_s=12.),
                   lambda p:p['detection']['audio'].update(end_s=15.)]
        for mutate in mutations:
            self.proof=copy.deepcopy(original);mutate(self.proof);self.publish()
            with self.subTest(proof=self.proof),self.assertRaises(ValueError):self.reviewed()
        self.proof=original;self.publish();self.proof_path.write_text('changed after receipt')
        with self.assertRaises(ValueError):self.reviewed()

    def test_optional_raw_reports_must_keep_their_real_hashes(self):
        raw=self.evidence/'auto.json';raw.write_bytes(b'offline raw detection fixture')
        self.proof['detection']['report']={'path':str(raw.relative_to(self.folder)),'sha256':sha(raw.read_bytes())}
        self.publish();self.reviewed()
        raw.write_bytes(b'changed raw detection')
        with self.assertRaises(ValueError):self.reviewed()

    def test_machine_language_proof_never_allows_speaker_time_or_other_metadata_edits(self):
        original=copy.deepcopy(self.revised)
        for key,value in [('speaker_id','S01'),('start_s',11.3),('end_s',13.1),('target_characters',21)]:
            self.revised=copy.deepcopy(original);self.revised[1][key]=value;self.publish()
            with self.subTest(key=key),self.assertRaises(ValueError):self.reviewed()

    def test_mixed_batch_uses_real_row_languages_in_prompt_guard_output_and_report(self):
        rows,receipt=self.reviewed();fake=self.client()
        with patch.object(pc,'ProxyClient',return_value=fake),patch.object(pc,'invariants',wraps=pc.invariants) as guard:
            updated,report=pc.translate_segments(rows,self.root,source_language='de',
                language_exceptions=receipt['language_exceptions'],review_scope=rows)
        self.assertEqual(len(fake.calls),2)
        for prompt,data in fake.calls:
            self.assertIn('乌克兰语(uk)',prompt);self.assertIn('不能把其他语言当成德语',prompt)
            self.assertEqual([item['source_language'] for item in data['items']],['de','uk'])
            self.assertEqual(data['items'][1]['text_source'],'Відстань 3 km.')
        self.assertEqual({call.kwargs['source_language'] for call in guard.call_args_list},{'de','uk'})
        self.assertEqual(updated[1]['text_de'],'Відстань 3 km.');self.assertEqual(updated[1]['source_language'],'uk')
        self.assertEqual(report['source_language'],'de');self.assertEqual(report['source_languages'],['de','uk'])
        self.assertEqual(report['source_language_by_segment'],{'line0107':'de','line0108':'uk'})
        pipeline.validate_translations(rows,updated,report)
        tampered=copy.deepcopy(report);tampered['source_language_by_segment']['line0108']='de'
        with self.assertRaises(RuntimeError):pipeline.validate_translations(rows,updated,tampered)
        tampered=copy.deepcopy(report);tampered.pop('source_languages');tampered.pop('source_language_by_segment')
        with self.assertRaises(RuntimeError):pipeline.validate_translations(rows,updated,tampered)
        changed=copy.deepcopy(updated);changed[1]['source_language']='de'
        with self.assertRaises(RuntimeError):pipeline.validate_translations(rows,changed,report)

    def test_default_mismatch_and_stale_exception_fail_before_any_request(self):
        rows,receipt=self.reviewed()
        for exceptions in [None,{}, {'line0108':{**receipt['language_exceptions']['line0108'],
                                                'source_binding':pc.source_binding(self.original[1])}}]:
            with patch.object(pc,'ProxyClient') as client:
                with self.assertRaises(ValueError):
                    pc.translate_segments(rows,self.root,source_language='de',language_exceptions=exceptions)
                client.assert_not_called()

    def test_german_payload_cache_key_is_unchanged(self):
        rows=[self.original[0]];fake=self.client();row=rows[0]
        item={'id':row['segment_id'],'text_de':row['text_de'],'target_characters':20,
              'previous_context':'','next_context':'','timing_feedback':None,
              'source_binding':pc.source_binding(row),'previous_source_binding':None,
              'text_source':row['text_de'],'source_language':'de'}
        payload={'version':pc.VERSION,'model':fake.model,'source_language':'de',
                 'source_language_policy':'whisper_multilingual_proxy_review_v1','items':[item]}
        key=sha(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode())
        with patch.object(pc,'ProxyClient',return_value=fake):
            pc.translate_segments(rows,self.root)
            cached=json.loads((self.root/'cache'/pc.CACHE_NAMESPACE/(key+'.json')).read_text())
            self.assertEqual(cached['input'],payload)
            pc.translate_segments(rows,self.root)
        self.assertEqual(len(fake.calls),2)

    def test_non_german_symbol_guard_and_german_negation_remain_required(self):
        rows,receipt=self.reviewed()
        for wrong in [{'line0108':'距离4公里。'},{'line0107':'这里有3人。'}]:
            fake=self.client(wrong)
            with patch.object(pc,'ProxyClient',return_value=fake):
                with self.assertRaises(pc.InvariantRejectedError):
                    pc.translate_segments(rows,self.root,source_language='de',
                        language_exceptions=receipt['language_exceptions'],review_scope=rows)


if __name__=='__main__':unittest.main()
