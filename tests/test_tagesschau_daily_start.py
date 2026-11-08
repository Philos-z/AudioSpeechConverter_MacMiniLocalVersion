"""Isolated official-metadata/queue/API fixtures; no production network or state writes."""
import copy,hashlib,html,json,pathlib,sys,tempfile,unittest
from datetime import datetime,timezone
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'web'))
import tagesschau_daily_start as start
from task_takeover import InstanceLock,write_json
from unittest.mock import patch
class Api:
 def __init__(self,owner):self.owner=owner;self.calls=[];self.crash=False;self.timeout=False
 def __call__(self,method,route,body=None):
  # A network callback must always be outside the queue lock.
  with InstanceLock(self.owner.state_path.with_suffix('.lock')):pass
  self.calls.append((method,route,copy.deepcopy(body)))
  if route=='/api/liveness':return {'version':'philos_lan_web_v1','project_identity':hashlib.sha256(str(self.owner.root).encode()).hexdigest(),'worker_alive':True,'stopping':False}
  if method=='GET':return {'jobs':start._jobs(self.owner.root)}
  if self.crash:raise SystemExit('crash after durable sending, before simulated socket')
  if self.timeout:raise TimeoutError('unknown outcome')
  jid='web_20261107_210100_'+str(len([x for x in self.calls if x[0]=='POST'])).zfill(8)
  job={'id':jid,'created_at':self.owner.now.isoformat(),'status':'queued','title':body['title'],'source_url':body['source_url'],'options':{'editorial_policy':'fluency_first_v1'}}
  write_json(self.owner.root/'web/state/jobs'/f'{jid}.json',job)
  return {k:v for k,v in job.items() if k!='source_url'}
def player(at,url):
 at=datetime.fromisoformat(at);local=at.astimezone(start.BERLIN)
 mc={'meta':{'seriesTitle':'tagesschau','title':local.strftime('tagesschau %H:%M Uhr'),'broadcastedOnDateTime':at.isoformat()},'pluginData':{'download@web':{'isEnabled':True,'sources':[{'title':'SD 270p','url':url}]}},'streams':[{'media':[{'url':url,'mimeType':'video/mp4','maxHResolutionPx':480,'maxVResolutionPx':270}]}]}
 return '<div data-v-type="MediaPlayer" data-v="'+html.escape(json.dumps({'mc':mc}),quote=True)+'"></div>'
class StartTests(unittest.TestCase):
 def setUp(self):
  network=patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('Offline fixtures only'));network.start();self.addCleanup(network.stop)
  self.temp=tempfile.TemporaryDirectory(prefix='daily_start_fixture_');self.addCleanup(self.temp.cleanup);self.root=pathlib.Path(self.temp.name).resolve();self.state_path=self.root/'web/state/tagesschau_daily_automation.json';self.n=0;self.now=datetime.fromisoformat('2026-11-07T20:00:00+00:00')
  auth={'schema':'daily_current_programme_independent_start_authorization_v1','effective_schedule':'21:00 Europe/Berlin daily','old_hold_blocks_only_its_own_job':True,'old_job_hold_or_budgets_revoked':False,'finish_and_email_existing_authority':True}
  p=self.root/'reports/auth.json';write_json(p,auth)
  config={'schema':'tagesschau_daily_automation_config_v1','root':str(self.root),'api_base':'http://127.0.0.1:8766','state_path':'web/state/tagesschau_daily_automation.json','timezone':'Europe/Berlin','daily_local_time':'21:00','daily_submission_policy':'current_date_independent_v1','late_resume_policy':'latest_due_intent_only','old_holds_block_new_dates':False,'new_job_reserve_gib':8,'daily_current_date_authorization_ref':{'path':'reports/auth.json','sha256':hashlib.sha256(p.read_bytes()).hexdigest()},'notification':{'to':'fixture@example.invalid'}}
  write_json(self.root/'config/tagesschau_daily_automation.json',config)
  write_json(self.state_path,{'schema':start.queue.STATE_SCHEMA,'generation':1,'watched':{},'episodes':{},'daily_intents':{},'notifications':{}})
  self.url='https://tagesschau-progressive.ard-mcdn.de/video/2099/0101/TV-20990101-0000-0001.webs.h264.mp4?download=true';self.listing=player('2026-11-07T19:00:00+00:00',self.url);self.api=Api(self);self.free=20*start.GIB;self.head=lambda url:{'status':200,'url':url,'final_url':url,'content_type':'video/mp4','content_length':'12345678'}
 def state(self):return json.loads(self.state_path.read_text())
 def save(self,q):write_json(self.state_path,q)
 def run_entry(self,mode='daily',**kw):
  self.n+=1
  return start.run(self.root,mode,self.root/'reports'/f'run{self.n}',now=self.now,api=self.api,fetch_listing=lambda:self.listing,head=kw.pop('head',self.head),disk_free=lambda:self.free,**kw)
 def posts(self):return [x for x in self.api.calls if x[0]=='POST']
 def test_01_due_daily_unique_post_binds_and_queues_email_network_outside_lock(self):
  result=self.run_entry();self.assertEqual(result['status'],'job_bound');self.assertEqual(len(self.posts()),1);self.assertEqual(result['new_post_attempts'],1);self.assertEqual(self.posts()[0][2]['source_url'],self.url)
  q=self.state();intent=q['daily_intents']['2026-11-07'];watch=q['watched'][intent['job_id']];self.assertEqual(watch['email']['status'],'pending');self.assertEqual(watch['email']['send_attempts'],0);self.assertEqual(q['generation'],5)
  before=copy.deepcopy(q);again=self.run_entry('resume');self.assertEqual(again['status'],'job_bound');self.assertEqual(again['new_post_attempts'],0);self.assertEqual(len(self.posts()),1);self.assertEqual(self.state(),before)
 def test_02_old_holds_failed_jobs_and_waiting_intents_do_not_block_new_date(self):
  q=self.state();old={'local_date':'2026-11-04','status':'waiting_previous','user_hold':{'automatic_resumption_allowed':False}};q['daily_intents']['2026-11-04']=old;oldid='web_20261103_210000_00000009';q['watched'][oldid]={'job_id':oldid,'user_hold':{'status':'deferred_by_user'}};self.save(q)
  write_json(self.root/'web/state/jobs'/f'{oldid}.json',{'id':oldid,'status':'failed','title':'previous','created_at':'2026-11-03T20:00:00+00:00'})
  self.assertEqual(self.run_entry()['status'],'job_bound');self.assertEqual(self.state()['daily_intents']['2026-11-04'],old);self.assertEqual(self.state()['watched'][oldid],q['watched'][oldid])
 def test_03_midnight_daily_claims_yesterday_latest_due_not_new_days_programme(self):
  self.now=datetime.fromisoformat('2026-11-08T00:30:00+01:00');nexturl=self.url.replace('0000-0001','0000-0002');self.listing+=player('2026-11-07T23:10:00+00:00',nexturl)
  self.assertEqual(self.run_entry()['status'],'job_bound');self.assertEqual(set(self.state()['daily_intents']),{'2026-11-07'});self.assertEqual(self.posts()[0][2]['source_url'],self.url)
 def test_04_resume_never_claims_or_backfills_old_backlog_and_dst_dates_correct(self):
  q=self.state();q['daily_intents']['2026-11-04']={'local_date':'2026-11-04','status':'waiting_previous'};self.save(q);before=self.state();self.now=datetime.fromisoformat('2026-11-08T11:00:00+01:00');self.assertEqual(self.run_entry('resume')['status'],'no_due_intent');self.assertEqual(self.state(),before)
  for at,date in [('2026-03-29T18:59:59+00:00','2026-03-28'),('2026-03-29T19:00:00+00:00','2026-03-29'),('2026-10-25T19:59:59+00:00','2026-10-24'),('2026-10-25T20:00:00+00:00','2026-10-25')]:self.assertEqual(start.due_date(datetime.fromisoformat(at)).isoformat(),date)
 def test_05_three_active_waits_but_two_active_uses_original_serial_queue(self):
  for i in range(3):
   jid=f'web_20261107_19000{i}_{i:08d}';write_json(self.root/'web/state/jobs'/f'{jid}.json',{'id':jid,'status':'running' if i==0 else 'queued','title':f'Other{i}','created_at':'2026-11-07T18:00:00+00:00'})
  self.assertEqual(self.run_entry()['status'],'waiting_queue');self.assertFalse(self.posts())
  last=self.root/'web/state/jobs/web_20261107_190002_00000002.json';last.unlink();self.now+=start.timedelta(minutes=5);self.assertEqual(self.run_entry('resume')['status'],'job_bound');self.assertEqual(len(self.posts()),1)
 def test_06_unknown_sent_or_crash_before_socket_never_blind_reposts(self):
  self.api.timeout=True;first=self.run_entry();self.assertEqual(first['status'],'submission_unknown');self.assertEqual(first['new_post_attempts'],1);self.assertEqual(len(self.posts()),1)
  self.now+=start.timedelta(minutes=5);self.api.timeout=False;second=self.run_entry('resume');self.assertEqual(second['status'],'submission_unknown');self.assertEqual(second['new_post_attempts'],0);self.assertEqual(len(self.posts()),1)
 def test_07_crash_before_post_is_still_spent_until_unique_local_job_proves_outcome(self):
  self.api.crash=True
  with self.assertRaises(SystemExit):self.run_entry()
  self.assertEqual(self.state()['daily_intents']['2026-11-07']['status'],'submitting');self.api.crash=False;self.now+=start.timedelta(minutes=5);self.assertEqual(self.run_entry('resume')['status'],'submission_unknown');self.assertEqual(len(self.posts()),1)
  intent=self.state()['daily_intents']['2026-11-07'];jid='web_20261107_210100_00000001';write_json(self.root/'web/state/jobs'/f'{jid}.json',{'id':jid,'source_url':self.url,'title':intent['title'],'created_at':intent['submission']['started_at'],'status':'running'})
  result=self.run_entry('resume');self.assertEqual(result['status'],'job_bound');self.assertEqual(result['new_post_attempts'],0);self.assertEqual(len(self.posts()),1)
 def test_08_disk_8gib_plus_source_margin_and_official_head_guards(self):
  self.free=8*start.GIB;self.assertEqual(self.run_entry()['status'],'waiting_storage');self.assertFalse(self.posts());self.now+=start.timedelta(minutes=5);self.free=20*start.GIB
  bad=lambda url:{**self.head(url),'final_url':'https://unofficial.invalid/file.mp4'};self.assertEqual(self.run_entry('resume',head=bad)['status'],'waiting_api_or_source');self.assertFalse(self.posts())
 def test_09_same_period_episode_hold_during_network_blocks_before_sending(self):
  def head(url):
   q=self.state();episode=next(iter(q['episodes'].values()));episode['user_hold']={'automatic_resumption_allowed':False};q['generation']+=1;self.save(q);return self.head(url)
  result=self.run_entry(head=head);self.assertEqual(result['status'],'blocked');self.assertFalse(self.posts());self.assertEqual(self.state()['daily_intents']['2026-11-07']['post_attempts'],0)
 def test_10_frozen_source_cannot_switch_and_cross_source_watch_binding_cannot_overwrite(self):
  self.free=8*start.GIB;self.assertEqual(self.run_entry()['status'],'waiting_storage');self.now+=start.timedelta(minutes=5);self.free=20*start.GIB;self.listing='this changed listing must not be fetched for frozen source';self.assertEqual(self.run_entry('resume')['status'],'job_bound');self.assertEqual(self.posts()[0][2]['source_url'],self.url)
 def test_11_liveness_wrong_project_never_posts(self):
  original=self.api
  def fake(method,route,body=None):
   value=original(method,route,body)
   if route=='/api/liveness':value['project_identity']='0'*64
   return value
  self.api=fake;result=self.run_entry();self.assertEqual(result['status'],'waiting_api_or_source');self.assertFalse(original.calls[-1][0]=='POST')
if __name__=='__main__':unittest.main(verbosity=2)
