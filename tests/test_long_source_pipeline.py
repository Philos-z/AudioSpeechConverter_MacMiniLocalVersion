"""Parent orchestration contracts: serial children, resume, privacy, and limits."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parents[1]/'web'))
import pipeline

class ParentPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.jid='web_20261021_120000_aabbccdd'
        (self.root/'scripts').mkdir();(self.root/'scripts/asr.py').write_text('# fixture')
        self.source=self.root/'source.mp4';self.source.write_bytes(b'source')
        self.job={'id':self.jid,'input_path':str(self.source),'options':{'source_language':'auto','episode_max_seconds':3}}
        self.updates=[];self.calls=[]
        self.flow=pipeline.Flow(self.root,self.job,lambda *a:self.updates.append(a),None,lambda:False)
        for name in ('audio','outputs','episode_asr'): (self.flow.folder/name).mkdir(parents=True,exist_ok=True)
        pipeline.save(self.flow.folder/'source.json',{'input':str(self.source),'duration_s':6,'probe':{}})
        self.asr={'language':'en','source_language':'en','segments':[
            {'segment_id':'a','speaker_id':'S00','start_s':.2,'end_s':2.,'text_source':'Hello.','text_de':'Hello.','source_language':'en'},
            {'segment_id':'b','speaker_id':'S00','start_s':3.2,'end_s':5.,'text_source':'Goodbye.','text_de':'Goodbye.','source_language':'en'}]}
        def command(stage,start,end,argv,**kwargs):
            argv=list(map(str,argv));self.calls.append(argv)
            if '--language' in argv:
                pipeline.save(Path(argv[argv.index('--output')+1]),self.asr)
            elif 'export' in argv:
                Path(argv[argv.index('--output')+1]).write_bytes(b'clip'+argv[argv.index('--episode')+1].encode())
                pipeline.save(Path(argv[argv.index('--receipt')+1]),{'full_decode_passed':True})
            else:Path(argv[-1]).write_bytes(b'asr audio')
            return {'stdout':'{}','returncode':0}
        self.flow.command=command
        self.children=[]
        def child(root,job,update,runner,cancelled,started,waited):
            self.children.append(copy.deepcopy(job))
            self.assertEqual(job['options']['source_language'],'en')
            self.assertFalse(job['options']['split_long_source'])
            self.assertEqual(job['options']['voice_pilot_groups'],0)
            self.assertTrue(Path(job['_episode_asr']['path']).is_relative_to(self.flow.folder))
            self.assertEqual(pipeline.read(Path(job['_episode_asr']['path']))['language'],'en')
            folder=self.root/'jobs'/job['id']/'outputs';folder.mkdir(parents=True,exist_ok=True)
            video=folder/'candidate.mp4';video.write_bytes(b'video'+job['id'].encode())
            update('done',100)
            return {'duration_s':2.8,'metrics':{},'artifacts':[{'label':'video','kind':'video','path':str(video),'sha256':pipeline.digest(video)}]}
        self.child=child
    def run_parent(self,child=None):
        with patch.object(pipeline,'_run_with_recovery',child or self.child),patch.object(pipeline,'space_preflight',return_value={'sufficient':True}):
            return self.flow.process_episodes({'asr_model':'fixture','minimum_free_gib':8},self.source,pipeline.digest(self.source),{},6,0,'auto',3)
    def test_full_source_once_serial_children_private_publication_and_resume(self):
        result=self.run_parent()
        self.assertEqual(result['episode_count'],2)
        self.assertEqual(result['metrics']['full_source_asr_runs'],1)
        self.assertEqual(len(self.children),2)
        for artifact in result['artifacts']:
            path=Path(artifact['path']);self.assertTrue(path.is_relative_to(self.flow.folder/'outputs'))
            self.assertEqual(pipeline.digest(path),artifact['sha256'])
        self.assertEqual(result['episodes'][0]['source_end_s'],result['episodes'][1]['source_start_s'])
        self.assertEqual(result['episodes'][-1]['source_end_s'],6)
        old_count=len(self.calls);self.run_parent()
        self.assertEqual(len(self.calls),old_count)
        self.assertTrue(all(a[1]<=b[1] for a,b in zip(self.updates,self.updates[1:])))
    def test_replan_uses_actual_dubbed_duration_preserves_actual_asr_counter(self):
        self.asr['segments']=[{'segment_id':str(i),'speaker_id':'S00','start_s':i+.1,'end_s':i+.7,'text_source':'Hello.','text_de':'Hello.','source_language':'en'} for i in range(6)]
        def oversized(*args):
            result=self.child(*args)
            if len(self.children)==1:result['duration_s']=4
            return result
        result=self.run_parent(oversized)
        self.assertEqual(result['metrics']['duration_replan_attempts'],1)
        self.assertEqual(result['metrics']['full_source_asr_runs'],1)
        self.assertTrue(all(row['duration_s']<=3 for row in result['episodes']))
        self.assertTrue((self.flow.folder/'episode_replan_1.json').is_file())
    def test_cancellation_between_children_stops_remaining_work(self):
        def cancel_after_first(*args):
            result=self.child(*args);self.flow.cancelled=lambda:True;return result
        with self.assertRaisesRegex(RuntimeError,'取消'):self.run_parent(cancel_after_first)
        self.assertEqual(len(self.children),1)
    def test_source_alias_and_language_drift_are_rejected_before_grouping(self):
        for field,value in [('text_source','changed'),('source_language','fr')]:
            row=copy.deepcopy(self.asr['segments'][0]);row[field]=value
            with self.assertRaisesRegex(RuntimeError,'不一致'):
                pipeline.regroup_sentence_rows([row],6,source_language='en')
    def test_multilingual_sentence_ends(self):
        for language,text in [('ja','これは文です。'),('ja','「これは文です。」'),('ar','هل أنت هنا؟'),('hi','यह एक वाक्य है।')]:
            self.assertTrue(pipeline.german_sentence_end(text,language))

if __name__=='__main__':unittest.main()
