"""A preview uses a verified prefix without erasing full-source hints."""
import copy,hashlib,json
from pathlib import Path
import sys,tempfile,unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'web'))
import proxy_client as proxy

class PreviewHintTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.diagnostics=self.root/'job/translation/diagnostics'
        self.diagnostics.mkdir(parents=True);self.path=self.diagnostics.parent/'source_review_hints.json'
        self.rows=[{'segment_id':'line0001','speaker_id':'S00','start_s':0.,'end_s':2.,'text_de':'Hallo.'},
            {'segment_id':'line0002','speaker_id':'S00','start_s':2.,'end_s':4.,'text_de':'Tschüss.'}]
        self.notes={'schema':'source_review_hints_v1','rows':{r['segment_id']:{
            'source_text_sha256':hashlib.sha256(r['text_de'].encode()).hexdigest(),'note':'保留本句问候内容。'} for r in self.rows}}
        self.path.write_text(json.dumps(self.notes,ensure_ascii=False))
        self.payloads=[]
        owner=self
        class Client:
            model='offline';records=[]
            def json(self,system,data):
                owner.payloads.append(data)
                return {'translations':[{'id':r['id'],'text_zh':'你好。','approved':True,'issues':[]} for r in data['items']]}
        self.client=Client()
    def test_prefix_only_sends_its_rows_but_retains_all_verified_hints(self):
        before=self.path.read_bytes()
        with patch.object(proxy,'ProxyClient',return_value=self.client):
            translated,_=proxy.translate_segments(self.rows[:1],self.root,diagnostic_dir=self.diagnostics,review_scope=self.rows)
        self.assertEqual(len(translated),1);self.assertEqual(len(self.payloads),2)
        self.assertTrue(all([r['id'] for r in p['items']]==['line0001'] for p in self.payloads))
        self.assertEqual(self.path.read_bytes(),before)
    def test_outside_hint_still_requires_valid_source_sha(self):
        self.notes['rows']['line0002']['source_text_sha256']='0'*64;self.path.write_text(json.dumps(self.notes))
        with patch.object(proxy,'ProxyClient') as factory,self.assertRaisesRegex(ValueError,'原文不符'):
            proxy.translate_segments(self.rows[:1],self.root,diagnostic_dir=self.diagnostics,review_scope=self.rows)
        factory.assert_not_called()
    def test_subset_or_reordered_scope_cannot_enable_ignored_hints(self):
        with patch.object(proxy,'ProxyClient') as factory,self.assertRaisesRegex(ValueError,'完整前缀'):
            proxy.translate_segments(self.rows[1:],self.root,diagnostic_dir=self.diagnostics,review_scope=self.rows)
        factory.assert_not_called()

if __name__=='__main__':unittest.main()
