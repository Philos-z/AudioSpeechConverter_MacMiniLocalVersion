"""Independent daily-runner integration; network/model actions are fixture-only."""
import collections,copy,hashlib,html,importlib.util,json,sys,tempfile,threading,unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'web'))
import tagesschau_daily_state as queue
import tagesschau_daily_start as start
import server
NOW=datetime.fromisoformat('2026-11-08T20:05:00+00:00')
JID='web_20261108_210500_11111111';OLD='web_20261103_221748_22222222';OTHER='web_20261107_210000_33333333'
sha=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
def read(p):return json.loads(Path(p).read_text())
def write(p,x):queue.write_json(Path(p),x)
def media(date='2026-11-08',clock='20:00',asset='0000-0001'):
 dt=datetime.fromisoformat(date+'T'+clock+':00+01:00');name=date.replace('-','');url=f'https://{start.discovery.CDN}/video/{date[:4]}/{date[5:7]}{date[8:]}/TV-{name}-{asset}.webs.h264.mp4'
 return {'meta':{'seriesTitle':'tagesschau','title':f'tagesschau {clock} Uhr','broadcastedOnDateTime':dt.isoformat()},'live':False,'pluginData':{'download@web':{'isEnabled':True,'sources':[{'title':'SD 270p','url':url+'?download=true'}]}},'streams':[{'media':[{'url':url,'mimeType':'video/mp4','maxHResolutionPx':480,'maxVResolutionPx':270}]}]}
def listing(*items):return ''.join('<div data-v-type="MediaPlayer" data-v="'+html.escape(json.dumps({'mc':x}),quote=True)+'"></div>' for x in items)
class Fixture:
 def __init__(self,now=NOW):
  self.tmp=tempfile.TemporaryDirectory(prefix='daily_start_independent_');self.root=Path(self.tmp.name).resolve();self.now=now;self.calls=[];self.fetches=[];self.heads=[];self.count=0;self.html=listing(media());self.free=64*start.GIB;self.post_behavior=None;self.head_behavior=None;self.source_bytes=12_345_678
  auth={'schema':'daily_current_programme_independent_start_authorization_v1','effective_schedule':'21:00 Europe/Berlin daily','old_hold_blocks_only_its_own_job':True,'old_job_hold_or_budgets_revoked':False,'finish_and_email_existing_authority':True,'synthetic_fixture_not_user_authority':True}
  write(self.root/'reports/auth.json',auth)
  self.config={'schema':'tagesschau_daily_automation_config_v1','root':str(self.root),'state_path':'web/state/tagesschau_daily_automation.json','api_base':'http://127.0.0.1:8766','timezone':'Europe/Berlin','daily_local_time':'21:00','daily_submission_policy':'current_date_independent_v1','late_resume_policy':'latest_due_intent_only','old_holds_block_new_dates':False,'new_job_reserve_gib':8,'daily_current_date_authorization_ref':{'path':'reports/auth.json','sha256':sha(self.root/'reports/auth.json')},'notification':{'to':'test@example.invalid'}}
  write(self.root/queue.CONFIG,self.config)
  write(self.root/'web/state/tagesschau_daily_automation.json',{'schema':queue.STATE_SCHEMA,'generation':1,'daily_intents':{},'episodes':{},'watched':{},'notifications':{}})
  # Use the actual formal server creation method, without any worker/thread.
  editorial=self.root/'reports/editorial_fixture_authorization.json'
  write(editorial,{'synthetic_fixture_not_user_authority':True})
  write(self.root/'config/editorial_policy.json',{'schema':'editorial_policy_defaults_v1','default_policy':'fluency_first_v1','decision_maker':'proxy','ask_for_editorial_choices':False,'meaning_deviation_allowed':True,'authorization_ref':{'path':'reports/editorial_fixture_authorization.json','sha256':sha(editorial)}})
  m=server.JobManager.__new__(server.JobManager);m.root=self.root;m.state=self.root/'web/state/jobs';m.condition=threading.Condition();m.jobs={};m.pending=collections.deque();m.cancel_events={};m.new_id=lambda:JID;self.manager=m
 def close(self):self.tmp.cleanup()
 @property
 def state_path(self):return self.root/'web/state/tagesschau_daily_automation.json'
 def state(self):return read(self.state_path)
 def update(self,fn):
  s=self.state();fn(s);write(self.state_path,s)
 def fetch(self):self.fetches.append(True);return self.html
 def head(self,url):
  self.heads.append(url)
  if self.head_behavior:self.head_behavior()
  return {'status':200,'url':url,'final_url':url,'content_type':'video/mp4','content_length':str(self.source_bytes)}
 def api(self,method,route,body=None):
  with start.InstanceLock(self.state_path.with_suffix('.lock')):pass
  self.calls.append((method,route,copy.deepcopy(body)))
  if method=='GET' and route=='/api/liveness':return {'version':'philos_lan_web_v1','project_identity':queue.digest(str(self.root).encode()),'worker_alive':True,'stopping':False}
  if method=='GET' and route=='/api/jobs':return {'jobs':[self.public(x) for x in self.manager.jobs.values()]}
  if method=='POST' and route=='/api/jobs':
   s=self.state();intent=s['daily_intents'][start.due_date(self.now).isoformat()]
   assert intent['status']=='submitting' and intent['post_attempts']==1
   assert intent['submission']['request_body']==body and intent['submission']['request_body_sha256']==queue.digest(queue.canonical(body))
   if self.post_behavior:return self.post_behavior(body)
   return self.add(body)
  raise AssertionError('Unexpected network route '+method+route)
 def public(self,j):return {k:j[k] for k in ('id','title','status','stage','progress','created_at','updated_at') if k in j}
 def add(self,body,jid=JID):
  with patch.object(server,'now',return_value=self.now.isoformat()):
   return self.manager.add(source_url=body['source_url'],title=body['title'],source_language=body.get('source_language','auto'),job_id=jid)
 def putjob(self,jid,status='failed',url='https://example.invalid/old.mp4',title='old'):
  j={'id':jid,'status':status,'title':title,'source_url':url,'created_at':self.now.isoformat(),'updated_at':self.now.isoformat(),'stage':status,'progress':0,'options':{'website_recovery':{'reserve_gib':8}}};self.manager.jobs[jid]=j;write(self.manager.state/(jid+'.json'),j);return j
 def executor(self,now=None,api=None,head=None):
  self.count+=1
  return start.Executor(self.root,f'reports/try_{self.count}',now=now or self.now,api=api or self.api,fetch_listing=self.fetch,head=head or self.head,disk_free=lambda:self.free)
 def run(self,mode='daily',now=None,**kw):
  if now:self.now=now
  return self.executor(**kw).execute(mode)
 def frozen(self,date='2026-11-08',now=None):
  e=self.executor(now=now);e.mode='daily';i,status=e.claim(date,True);i=e.discover(i)
  self.update(lambda s:s['daily_intents'][date].update(starter_lease_until='2020-01-01T00:00:00+00:00'))
  return self.state()['daily_intents'][date]
 def posts(self):return [c for c in self.calls if c[0]=='POST']

class Tests(unittest.TestCase):
 def setUp(self):
  network=patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('Offline fixtures only'));network.start();self.addCleanup(network.stop)
  self.x=Fixture();self.addCleanup(self.x.close)
 def failclosed(self,fn):
  with self.assertRaises((ValueError,KeyError,TypeError,RuntimeError,BlockingIOError)):fn()
  self.assertEqual(self.x.posts(),[])
 def test_01_actual_server_add_freezes_policy_queue_and_durable_post_once(self):
  x=self.x;r=x.run();self.assertEqual(r['status'],'job_bound');self.assertEqual(r['new_post_attempts'],1);self.assertEqual(len(x.posts()),1)
  j=x.manager.jobs[JID];self.assertEqual(j['status'],'queued');self.assertEqual(list(x.manager.pending),[JID]);self.assertFalse(hasattr(x.manager,'thread'));self.assertEqual(j['options']['editorial_policy'],'fluency_first_v1');self.assertEqual(j['options']['website_recovery']['reserve_gib'],8)
  s=x.state();i=s['daily_intents']['2026-11-08'];self.assertEqual(s['episodes'][i['episode_key']]['job_id'],JID);self.assertEqual(s['watched'][JID]['email']['attempts'],0);self.assertEqual(s['watched'][JID]['source_url'],i['source_url']);self.assertEqual(x.run()['new_post_attempts'],0);self.assertEqual(len(x.posts()),1)
 def test_02_daily_before_time_and_resume_never_claim_new_day(self):
  x=self.x;self.assertEqual(x.run('resume',datetime.fromisoformat('2026-11-08T20:00:00+00:00'))['status'],'no_due_intent')
  self.assertEqual(x.state()['daily_intents'],{});self.assertEqual(x.calls+x.fetches+x.heads,[])
  for at in ['2026-11-08T19:59:59+00:00','2026-11-07T23:59:00+00:00']:
   y=Fixture();self.addCleanup(y.close);y.html=listing(media('2026-11-07'),media('2026-11-08'));r=y.run('daily',datetime.fromisoformat(at));self.assertEqual(r['status'],'job_bound');self.assertEqual(list(y.state()['daily_intents']),['2026-11-07']);self.assertIn('TV-20261107',y.posts()[0][2]['source_url'])
 def test_03_due_date_dst_and_midnight(self):
  for at,date in [('2026-03-29T18:59:59+00:00','2026-03-28'),('2026-03-29T19:00:00+00:00','2026-03-29'),('2026-10-25T19:59:59+00:00','2026-10-24'),('2026-10-25T20:00:00+00:00','2026-10-25'),('2026-10-25T00:30:00+00:00','2026-10-24'),('2026-10-25T01:30:00+00:00','2026-10-24'),('2026-11-08T00:00:00+01:00','2026-11-07')]:
   self.assertEqual(start.due_date(datetime.fromisoformat(at)).isoformat(),date)
  self.failclosed(lambda:start.due_date(datetime(2026,11,8)))
 def test_04_midnight_resume_frozen_previous_day_without_switch_or_fetch(self):
  x=self.x;x.now=datetime.fromisoformat('2026-11-07T20:05:00+00:00');x.html=listing(media('2026-11-07'));i=x.frozen('2026-11-07');n=len(x.fetches);x.html=listing(media('2026-11-08'));r=x.run('resume',datetime.fromisoformat('2026-11-07T23:30:00+00:00'))
  self.assertEqual(r['status'],'job_bound');self.assertEqual(len(x.fetches),n);self.assertEqual(x.posts()[0][2]['source_url'],i['source_url']);self.assertNotIn('2026-11-08',x.state()['daily_intents'])
 def test_05_missing_sd_freezes_episode_and_never_selects_newer(self):
  x=self.x;p=media();p['pluginData']['download@web']['sources']=[];x.html=listing(p);self.assertEqual(x.run()['status'],'waiting_source');i=x.state()['daily_intents']['2026-11-08'];key=i['episode_key'];self.assertEqual(x.posts(),[])
  x.now+=timedelta(minutes=4);x.html=listing(media(clock='20:30',asset='0000-0002'));self.assertEqual(x.run('resume')['status'],'waiting_source');self.assertEqual(x.state()['daily_intents']['2026-11-08']['episode_key'],key)
  x.now+=timedelta(minutes=4);x.html=listing(media(clock='20:30',asset='0000-0002'),media());self.assertEqual(x.run('resume')['status'],'job_bound');self.assertEqual(x.state()['daily_intents']['2026-11-08']['episode_key'],key);self.assertIn('0000-0001',x.posts()[0][2]['source_url'])
 def test_06_old_hold_and_waiting_do_not_block_new_day_active_queues(self):
  x=self.x;x.putjob(OLD);x.putjob(OTHER,'running')
  held={'status':'job_bound','local_date':'2026-11-03','job_id':OLD,'user_hold':{'automatic_resumption_allowed':False},'old_budget':{'used':6,'max':6}};waiting={'status':'waiting_previous','local_date':'2026-11-04','episode_key':'old4','source_url':'old frozen'}
  x.update(lambda s:(s['daily_intents'].update({'2026-11-03':held,'2026-11-04':waiting}),s['watched'].update({OLD:{'job_id':OLD,'user_hold':{'automatic_resumption_allowed':False}}})))
  before=copy.deepcopy(x.state());r=x.run();self.assertEqual(r['status'],'job_bound');self.assertEqual(x.manager.jobs[OTHER]['status'],'running');self.assertEqual(x.manager.jobs[JID]['status'],'queued');self.assertEqual(len(x.posts()),1)
  for d in ['2026-11-03','2026-11-04']:self.assertEqual(x.state()['daily_intents'][d],before['daily_intents'][d])
  self.assertEqual(x.state()['watched'][OLD],before['watched'][OLD])
 def test_07_current_intent_and_episode_holds_block_post(self):
  x=self.x;i=x.frozen();base=x.state()
  for scope,hold in [('intent',{'automatic_resumption_allowed':False}),('intent',{'status':'deferred_by_user'}),('episode',{'automatic_resumption_allowed':False}),('episode',{'status':'paused'})]:
   s=copy.deepcopy(base);target=s['daily_intents']['2026-11-08'] if scope=='intent' else s['episodes'][i['episode_key']];target['user_hold']=hold;write(x.state_path,s);r=x.run('resume');self.assertEqual(r['status'],'held');self.assertEqual(x.posts(),[])
 def test_08_storage_and_head_hard_guards(self):
  x=self.x;x.free=8*start.GIB+x.source_bytes+start.MARGIN-1;self.assertEqual(x.run()['status'],'waiting_storage');self.assertEqual(x.posts(),[])
  self.assertTrue(x.state()['daily_intents']['2026-11-08']['required_free_bytes']>x.free)
  x.config['new_job_reserve_gib']=2;write(x.root/queue.CONFIG,x.config);self.failclosed(lambda:x.run())
 def test_09_invalid_head_keeps_wait_without_post(self):
  x=self.x;i=x.frozen()
  for mutation in [{'status':404},{'content_type':'text/html'},{'content_length':'0'},{'content_length':'-3'},{'content_length':str(21*start.GIB)},{'final_url':i['source_url'].replace('0000-0001','0000-0002')}]:
   x.now+=timedelta(minutes=4)
   def head(url):return {**x.head(url),**mutation}
   self.assertEqual(x.run('resume',head=head)['status'],'waiting_api_or_source');self.assertEqual(x.posts(),[])
 def test_10_unique_existing_job_adopted_without_new_post(self):
  x=self.x;i=x.frozen();x.add({'source_url':i['source_url'],'title':i['title']});n=len(x.heads);r=x.run('resume');self.assertEqual(r['status'],'job_bound');self.assertEqual(r['new_post_attempts'],0);self.assertEqual(x.posts(),[]);self.assertEqual(len(x.heads),n)
 def test_11_duplicate_cross_source_title_and_identity_fail_closed(self):
  x=self.x;i=x.frozen();s=x.state();x.putjob(JID,url=i['source_url'],title=i['title']);x.putjob(OTHER,url=i['source_url'],title=i['title']);self.failclosed(lambda:x.run('resume'))
  del x.manager.jobs[OTHER];(x.manager.state/(OTHER+'.json')).unlink();write(x.state_path,s);j=read(x.manager.state/(JID+'.json'));j['source_url']=i['source_url'].replace('0000-0001','0000-0002');write(x.manager.state/(JID+'.json'),j);self.failclosed(lambda:x.run('resume'))
 def test_12_unknown_after_timeout_reconciles_without_repost(self):
  x=self.x
  def timeout(body):x.add(body);raise OSError('Synthetic timeout after persisted server add')
  x.post_behavior=timeout;r=x.run();self.assertEqual(r['status'],'job_bound');self.assertEqual(len(x.posts()),1);x.post_behavior=None;r=x.run('resume');self.assertEqual(r['status'],'job_bound');self.assertEqual(r['new_post_attempts'],0);self.assertEqual(len(x.posts()),1)
 def test_13_timeout_without_local_or_crash_before_socket_never_reposts(self):
  x=self.x;x.post_behavior=lambda body:(_ for _ in ()).throw(OSError('Synthetic timeout before proven response'));r=x.run();self.assertEqual(r['status'],'submission_unknown');self.assertEqual(len(x.posts()),1);x.post_behavior=None;n=len(x.heads);self.assertEqual(x.run('resume')['status'],'submission_unknown');self.assertEqual(len(x.posts()),1);self.assertEqual(len(x.heads),n)
  # A distinct fixture crashes after durable reservation and before successful POST.
  y=Fixture();self.addCleanup(y.close);y.post_behavior=lambda body:(_ for _ in ()).throw(SystemExit('synthetic crash'))
  with self.assertRaises(SystemExit):y.run()
  self.assertEqual(y.state()['daily_intents']['2026-11-08']['status'],'submitting');y.post_behavior=None;self.assertEqual(y.run('resume')['status'],'submission_unknown');self.assertEqual(len(y.posts()),1)
 def test_14_cas_hold_config_and_late_local_job_stop_before_post(self):
  x=self.x;i=x.frozen();base=x.state()
  x.head_behavior=lambda:x.update(lambda s:s['daily_intents']['2026-11-08'].update(user_hold={'automatic_resumption_allowed':False}))
  self.failclosed(lambda:x.run('resume'))
  write(x.state_path,base);x.head_behavior=lambda:x.putjob(JID,url=i['source_url'],title=i['title']);r=x.run('resume');self.assertEqual(r['status'],'job_bound');self.assertEqual(x.posts(),[])
 def test_15_three_active_wait_queue_and_resume_after_capacity(self):
  x=self.x
  for k,j in enumerate([OLD,OTHER,'web_20261108_200000_44444444']):x.putjob(j,'running' if k==0 else 'queued',url=f'https://example.invalid/{j}',title=j)
  self.assertEqual(x.run()['status'],'waiting_queue');self.assertEqual(x.posts(),[])
  j=x.manager.jobs[OTHER];j['status']='completed';write(x.manager.state/(OTHER+'.json'),j);x.now+=timedelta(minutes=4);self.assertEqual(x.run('resume')['status'],'job_bound');self.assertEqual(len(x.posts()),1)
 def test_16_lease_busy_then_expired_owner_and_cas(self):
  x=self.x;e=x.executor();e.mode='daily';i,s=e.claim('2026-11-08',True);self.assertEqual(s,'owned');self.assertEqual(x.run('resume')['status'],'busy');self.assertEqual(x.posts(),[]);x.now+=timedelta(seconds=181);self.assertEqual(x.run('resume')['status'],'job_bound');self.assertEqual(len(x.posts()),1)
  y=Fixture();self.addCleanup(y.close);e=y.executor();e.mode='daily';i,s=e.claim('2026-11-08',True);y.update(lambda s:s['daily_intents']['2026-11-08'].update(user_hold={'status':'paused'}));self.assertRaises(ValueError,e.change,'2026-11-08',start._hash(i),lambda *a:None)
 def test_17_orphan_source_authorization_and_wrong_live_project_stop(self):
  x=self.x;i=x.frozen();write(x.root/'web/state/source_requests'/f'{JID}.json',{'job':JID,'request':{'url_sha256':hashlib.sha256(i['source_url'].encode()).hexdigest()}});self.assertEqual(x.run('resume')['status'],'submission_unknown');self.assertEqual(x.posts(),[])
  y=Fixture();self.addCleanup(y.close)
  def wrong(method,route,body=None):
   r=y.api(method,route,body)
   if route=='/api/liveness':r['project_identity']='0'*64
   return r
  self.assertEqual(y.run(api=wrong)['status'],'waiting_api_or_source');self.assertEqual(y.posts(),[])
 def test_18_cross_bound_episode_watch_do_not_get_overwritten(self):
  x=self.x;i=x.frozen();x.add({'source_url':i['source_url'],'title':i['title']});base=x.state()
  for scope in ['episode','watch']:
   s=copy.deepcopy(base)
   if scope=='episode':s['episodes'][i['episode_key']]['source_url']=i['source_url'].replace('0000-0001','0000-0002')
   else:s['watched'][JID]={'job_id':JID,'episode_key':i['episode_key'],'source_url':i['source_url'].replace('0000-0001','0000-0002'),'title':i['title']}
   write(x.state_path,s);self.failclosed(lambda:x.run('resume'))
   self.assertEqual(x.state()['episodes'][i['episode_key']],s['episodes'][i['episode_key']])
   self.assertEqual(x.state()['watched'],s['watched'])
 def test_19_unbound_episode_source_drift_before_and_during_head_stops_post(self):
  x=self.x;i=x.frozen();base=x.state()
  x.update(lambda s:s['episodes'][i['episode_key']].update(source_url=i['source_url'].replace('0000-0001','0000-0002')))
  self.failclosed(lambda:x.run('resume'))
  write(x.state_path,base)
  x.head_behavior=lambda:x.update(lambda s:s['episodes'][i['episode_key']].update(source_url=i['source_url'].replace('0000-0001','0000-0002')))
  self.failclosed(lambda:x.run('resume'))
 def test_20_saved_frozen_discovery_and_original_request_are_authority(self):
  x=self.x;i=x.frozen();base=x.state()
  def drift(s):
   for obj in [s['daily_intents']['2026-11-08'],s['episodes'][i['episode_key']]]:
    obj['source_url']=obj['source_url'].replace('0000-0001','0000-0002');obj['canonical_media_url']=obj['canonical_media_url'].replace('0000-0001','0000-0002')
  x.update(drift);self.failclosed(lambda:x.run('resume'))
  write(x.state_path,base);proof=x.root/i['discovery']['path'];proof.write_bytes(proof.read_bytes()+b' ');self.failclosed(lambda:x.run('resume'))
  y=Fixture();self.addCleanup(y.close);y.post_behavior=lambda body:(_ for _ in ()).throw(OSError('Synthetic unknown'))
  self.assertEqual(y.run()['status'],'submission_unknown');before=len(y.posts());y.update(lambda s:s['daily_intents']['2026-11-08']['submission']['request_body'].update(source_url='https://example.invalid/different'))
  with self.assertRaises((ValueError,KeyError,TypeError)):y.run('resume')
  self.assertEqual(len(y.posts()),before)
 def test_21_terminal_unknown_states_not_reopened_and_wrapper_receipt(self):
  x=self.x;i=x.frozen();base=x.state()
  for status in ['completed','no_new_programme','terminal_no_new_programme','not_a_known_state']:
   s=copy.deepcopy(base);s['daily_intents']['2026-11-08']['status']=status;write(x.state_path,s)
   try:r=x.run('resume');self.assertIn(r['status'],{'terminal','preserved','preserved_terminal_intent','held','blocked'})
   except (ValueError,KeyError,TypeError):pass
   self.assertEqual(x.posts(),[])
  write(x.state_path,base);x.update(lambda s:s['episodes'][i['episode_key']].update(source_url='https://example.invalid/bad'))
  r=start.run(x.root,'resume','reports/wrapped_failure',now=x.now,api=x.api,fetch_listing=x.fetch,head=x.head,disk_free=lambda:x.free)
  self.assertEqual(r['status'],'blocked');self.assertEqual(r['new_post_attempts'],0);self.assertTrue((x.root/'reports/wrapped_failure/entry_error.json').is_file());self.assertTrue((x.root/'reports/wrapped_failure/result.json').is_file())

if __name__=='__main__':
 unittest.main(verbosity=2)
