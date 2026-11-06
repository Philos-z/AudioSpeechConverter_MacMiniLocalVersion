import io,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'web'))
import review_model_policy as policy
import website_recovery as recovery
from proxy_client import ProxyClient


class ReviewModelPolicyTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name).resolve();self.folder=self.root/'jobs'/'web_test';self.folder.mkdir(parents=True)
        self.video=self.root/'source.mp4';self.video.write_bytes(b'fixed source')
        recovery._save(self.folder/'source.json',{'input':str(self.video)})
        recovery._save(self.root/'config/web_proxy.json',{'base_url':'http://127.0.0.1:9999/v1','api_key':'offline-fixture','model':'gpt-6-luna'})
        self.record={'schema':policy.SCHEMA,'job_id':self.folder.name,'input_source_sha256':recovery._sha(self.video),
                     'model':'gpt-6-astra','reasoning_effort':'medium'}

    def save(self):recovery._save(self.folder/policy.CONFIG,self.record)
    def client(self):return policy.make_client(self.root,self.folder.name)

    def test_absent_policy_preserves_normal_translation_configuration(self):
        self.assertEqual(self.client().model,'gpt-6-luna')
        self.assertFalse(hasattr(self.client(),'reasoning_effort'))

    def test_task_policy_does_not_mutate_global_proxy(self):
        self.save();client=self.client();self.assertEqual((client.model,client.reasoning_effort),('gpt-6-astra','medium'))
        self.assertEqual(ProxyClient(self.root).model,'gpt-6-luna')

    def test_actual_payload_uses_selected_model_and_effort(self):
        self.save();captured=[]
        def fake(request,**kwargs):
            captured.append(json.loads(request.data));return io.BytesIO(json.dumps({'choices':[{'message':{'content':'{}'}}]}).encode())
        with patch('urllib.request.urlopen',side_effect=fake):self.client().json('data only',{'items':[]})
        self.assertEqual((captured[0]['model'],captured[0]['reasoning_effort']),('gpt-6-astra','medium'))

    def test_changed_source_cannot_reuse_policy(self):
        self.save();self.video.write_bytes(b'different source')
        with self.assertRaises(ValueError):self.client()

    def test_another_job_model_or_unknown_fields_are_rejected(self):
        for change in ({'job_id':'other'},{'model':'unknown'},{'reasoning_effort':'none'},{'extra':True}):
            old=dict(self.record);self.record.update(change);self.save()
            with self.assertRaises(ValueError):self.client()
            self.record=old

    def test_policy_identity_records_real_config_sha(self):
        self.save();client=self.client();self.assertEqual(client.content_review_policy['sha256'],recovery._sha(self.folder/policy.CONFIG))

    def test_explicit_sol_high_reaches_wire_without_altering_old_task_policy(self):
        config=recovery._read(self.root/'config/web_proxy.json')
        config.update(model='gpt-6-sol',reasoning_effort='high')
        recovery._save(self.root/'config/web_proxy.json',config)
        captured=[]
        def fake(request,**kwargs):
            captured.append(json.loads(request.data))
            return io.BytesIO(json.dumps({'model':'gpt-6-sol','choices':[{'message':{'content':'{}'}}]}).encode())
        client=ProxyClient(self.root)
        with patch('urllib.request.urlopen',side_effect=fake):client.json('data only',{'items':[]})
        self.assertEqual((captured[0]['model'],captured[0]['reasoning_effort']),('gpt-6-sol','high'))
        self.assertNotIn('temperature',captured[0])
        self.assertEqual(client.records[0]['response_model'],'gpt-6-sol')
        self.assertEqual(client.records[0]['reasoning_effort'],'high')
        self.save()
        self.assertEqual((self.client().model,self.client().reasoning_effort),('gpt-6-astra','medium'))
        self.record.update(model='gpt-6-sol',reasoning_effort='high');self.save()
        self.assertEqual((self.client().model,self.client().reasoning_effort),('gpt-6-sol','high'))

    def test_invalid_global_effort_stops_before_request(self):
        config=recovery._read(self.root/'config/web_proxy.json');config['reasoning_effort']='unknown'
        recovery._save(self.root/'config/web_proxy.json',config)
        with self.assertRaises(ValueError):ProxyClient(self.root)

    def test_other_purpose_is_not_silently_accepted(self):
        self.save()
        with self.assertRaises(ValueError):policy.make_client(self.root,self.folder.name,purpose='tts')


if __name__=='__main__':unittest.main()
