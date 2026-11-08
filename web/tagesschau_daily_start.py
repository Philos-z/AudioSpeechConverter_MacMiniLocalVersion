"""Start only the latest due daily programme through the original serial API.

No model, GPU, retry, cancellation, email or prior-period executor lives here.
Every possibly sent POST is durable and may only be reconciled, never repeated.
"""
from __future__ import annotations
import argparse
import copy
from datetime import datetime, time, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import urllib.request
import uuid
from zoneinfo import ZoneInfo

import tagesschau_daily_discovery as discovery
import tagesschau_daily_state as queue
from task_takeover import InstanceLock, write_json

BERLIN=ZoneInfo('Europe/Berlin')
START=time(21,0)
GIB=1024**3
MARGIN=512*1024**2
LEASE_SECONDS=180
SCHEMA='tagesschau_daily_start_receipt_v1'


def require(value,message):
    if not value:raise queue.StateError(message)


def due_date(now):
    require(now.tzinfo is not None,'Timezone-aware current time required')
    local=now.astimezone(BERLIN)
    return local.date() if local.timetz().replace(tzinfo=None)>=START else local.date()-timedelta(days=1)


def _stamp(now):return now.astimezone(timezone.utc).isoformat()
def _hash(value):return queue.digest(queue.canonical(value))
def _ref(root,path):return {'path':str(path.relative_to(root)),'sha256':queue.digest(path.read_bytes())}


def _config(root):
    config,sha=queue.load_config(root)
    require(config.get('timezone')=='Europe/Berlin' and config.get('daily_local_time')=='21:00'
        and config.get('daily_submission_policy')=='current_date_independent_v1'
        and config.get('late_resume_policy')=='latest_due_intent_only'
        and config.get('old_holds_block_new_dates') is False
        and config.get('new_job_reserve_gib')==8,'Current-date autonomous daily policy is not frozen')
    ref=config.get('daily_current_date_authorization_ref')
    require(isinstance(ref,dict) and set(ref)=={'path','sha256'},'Missing current-date start authorization')
    auth,actual=queue.read_file(root,ref['path'])
    require(actual==ref['sha256'] and auth.get('schema')=='daily_current_programme_independent_start_authorization_v1'
        and auth.get('effective_schedule')=='21:00 Europe/Berlin daily'
        and auth.get('old_hold_blocks_only_its_own_job') is True
        and auth.get('old_job_hold_or_budgets_revoked') is False
        and auth.get('finish_and_email_existing_authority') is True,'Current-date start authority changed')
    return config,sha


def _held(value):
    hold=value.get('user_hold')
    return (bool(value.get('cancel_requested_at')) or value.get('status') in
        {'canceled','cancelled','paused','user_hold','deferred_by_user','stop_after_rejected'}
        or value.get('automatic_resumption_allowed') is False
        or (isinstance(hold,dict) and (hold.get('automatic_resumption_allowed') is False
            or hold.get('status') in {'deferred_by_user','user_hold','paused'})))


TERMINAL={'completed','completed_notified','archived','finished','failed_closed','no_new_programme'}
READY_STATUSES={'claimed','preparing','frozen','waiting_previous','waiting_source','waiting_storage','waiting_queue','waiting_api_or_source','ready'}


def _own_episode(state,intent):
    key=intent.get('episode_key')
    if not key:return {}
    episode=state.get('episodes',{}).get(key,{})
    for job_id,watch in state.get('watched',{}).items():
        if watch.get('episode_key')!=key:continue
        require(watch.get('programme_date',intent['local_date'])==intent['local_date']
            and all(not watch.get(field) or watch[field]==intent.get(field) for field in ('source_url','title')),
            'Current programme watch source/title/date binding changed')
    if not episode:return {}
    require(isinstance(episode,dict) and episode.get('episode_key',key)==key
        and episode.get('local_date')==intent['local_date']
        and episode.get('daily_intent_date',intent['local_date'])==intent['local_date'],
        'Current episode key/date binding changed')
    for field in ('source_url','canonical_media_url','title','programme_title','broadcast_at_utc','broadcast_at_berlin','quality_label','expected_dimensions'):
        if field in episode and field in intent:
            require(episode[field]==intent[field],'Current episode '+field+' differs from the frozen intent')
    return episode


class Api:
    def __init__(self,base):
        self.base=queue.loopback_base(base)
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),queue._NoRedirect())
    def __call__(self,method,route,body=None):
        require((method=='GET' and route in {'/api/liveness','/api/jobs'}) or
            (method=='POST' and route=='/api/jobs'),'Unsupported daily API route')
        raw=None if body is None else queue.canonical(body)
        request=urllib.request.Request(self.base+route,data=raw,method=method,
            headers={'Accept':'application/json',**({'Content-Type':'application/json'} if raw is not None else {})})
        with self.opener.open(request,timeout=30) as response:
            require(response.url==self.base+route and response.status==(201 if method=='POST' else 200),'Unexpected API response')
            return queue.parse_json(response.read(queue.MAX_JSON+1))


def head_source(url):
    canonical=discovery.canonical_media(url)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),queue._NoRedirect())
    with opener.open(urllib.request.Request(url,method='HEAD'),timeout=20) as result:
        require(result.status==200 and discovery.canonical_media(result.url)==canonical,'Frozen official SD270 changed during HEAD')
        return {'status':result.status,'url':url,'final_url':result.url,
            'content_type':result.headers.get('Content-Type',''),'content_length':result.headers.get('Content-Length','')}


def _title(selected):
    local=queue.timestamp(selected['broadcast_at_utc']).astimezone(BERLIN)
    return 'Tagesschau '+local.strftime('%Y-%m-%d %H:%M')+' SD270 ['+local.astimezone(timezone.utc).strftime('%Y%m%dT%H%MZ')+']'


def _frozen(intent,date):
    at=queue.timestamp(intent['broadcast_at_utc']);local=at.astimezone(BERLIN)
    require(local.date().isoformat()==date and intent.get('episode_key')=='tagesschau:'+at.isoformat(), 'Frozen programme date/episode mismatch')
    require(intent.get('programme_title')==local.strftime('tagesschau %H:%M Uhr')
        or intent.get('programme_title')=='tagesschau '+str(local.hour)+local.strftime(':%M Uhr'),'Frozen programme clock mismatch')
    require(intent.get('quality_label')=='SD 270p' and intent.get('expected_dimensions')==[480,270]
        and intent.get('title')==_title(intent),'Frozen programme title/quality changed')
    canonical=discovery.canonical_media(intent['source_url'])
    require(intent.get('canonical_media_url',canonical)==canonical,'Frozen official source URL changed')
    return canonical


def _jobs(root):
    jobs=[]
    directory=queue.confined(root,'web/state/jobs')
    if not directory.exists():return jobs
    for path in sorted(directory.glob('web_*.json')):
        job,_=queue.read_file(root,path)
        require(isinstance(job,dict) and queue.JOB_ID.fullmatch(str(job.get('id','')))
            and path.name==job['id']+'.json','Malformed local job identity')
        jobs.append(job)
    return jobs


def _matches(jobs,intent,*,since=None):
    canonical=_frozen(intent,intent['local_date']);matches=[]
    for job in jobs:
        source=job.get('source_url');same_url=False
        try:same_url=discovery.canonical_media(source)==canonical
        except (TypeError,ValueError):pass
        same_title=job.get('title')==intent['title']
        if not (same_url or same_title):continue
        require(same_url and same_title,'A programme title/source conflicts with an existing local job')
        require(source==intent['source_url'],'Existing request differs from the exact frozen source URL')
        if since is not None:
            require(queue.timestamp(job['created_at'])>=queue.timestamp(since)-timedelta(seconds=1),
                'Unknown submission matched a job created before its reserved attempt')
        matches.append(job)
    require(len(matches)<=1,'Multiple jobs match the same frozen programme; do not POST')
    return matches


def _orphan_source(root,intent,jobs):
    expected=hashlib.sha256(intent['source_url'].encode()).hexdigest();known={j['id'] for j in jobs}
    directory=queue.confined(root,'web/state/source_requests')
    if not directory.exists():return False
    for path in directory.glob('web_*.json'):
        if path.name.endswith('.bound.json'):continue
        value,_=queue.read_file(root,path)
        if value.get('request',{}).get('url_sha256')==expected and value.get('job') not in known:return True
    return False


class Executor:
    def __init__(self,root,evidence_dir,*,now=None,api=None,fetch_listing=None,head=None,disk_free=None):
        self.root=Path(root).resolve();self.config,self.config_sha=_config(self.root)
        self.state_path=queue.confined(self.root,self.config['state_path'])
        self.evidence=queue.confined(self.root,evidence_dir)
        require(self.evidence.is_relative_to(self.root/'reports'),'Start evidence must be project-local reports')
        self.evidence.mkdir(parents=True,exist_ok=False)
        self.now=now or datetime.now(timezone.utc);require(self.now.tzinfo is not None,'Current time requires a timezone')
        self.api=api or Api(self.config['api_base']);self.fetch=fetch_listing or discovery.fetch_listing
        self.head=head or head_source;self.free=disk_free or (lambda:shutil.disk_usage(self.root).free)
        self.operation=uuid.uuid4().hex;self.new_posts=0
    def save(self,name,value):
        path=queue.confined(self.root,self.evidence/name);require(not path.exists(),'Evidence already exists')
        write_json(path,value);return _ref(self.root,path)
    def state(self):
        value,_=queue.read_file(self.root,self.state_path);queue.validate_state(value);return value
    def change(self,date,expected,mutate):
        with InstanceLock(self.state_path.with_suffix('.lock')):
            require(_config(self.root)[1]==self.config_sha,'Daily configuration changed during execution')
            state=self.state();current=state.get('daily_intents',{}).get(date)
            require((None if current is None else _hash(current))==expected,'Intent CAS changed; another owner won')
            mutate(state,current);state['generation']+=1
            write_json(self.state_path,state)
            return copy.deepcopy(state['daily_intents'][date])
    def mark(self,intent,status,**details):
        def mutate(state,current):current.update(status=status,last_checked_at=_stamp(self.now),**details)
        return self.change(intent['local_date'],_hash(intent),mutate)
    def receipt(self,status,**details):
        value={'schema':SCHEMA,'status':status,'mode':self.mode,'at_utc':_stamp(self.now),
            'operation':self.operation,'new_model_calls':0,'emails_sent':0,**details}
        self.save('result.json',value);return value
    def claim(self,date,create):
        with InstanceLock(self.state_path.with_suffix('.lock')):
            state=self.state();current=copy.deepcopy(state.get('daily_intents',{}).get(date))
        if current is not None:
            own_episode=_own_episode(state,current)
            if _held(own_episode):return current,'held'
            if current.get('status') in TERMINAL or own_episode.get('status') in TERMINAL:return current,'terminal'
            known_watch=any(watch.get('episode_key')==current.get('episode_key') for watch in state.get('watched',{}).values()) if current.get('episode_key') else False
            if (own_episode.get('job_id') or known_watch) and not current.get('job_id'):return current,'unknown'
            require(current.get('local_date')==date,'Intent date key mismatch')
            if current.get('job_id'):return current,'bound'
            if _held(current):return current,'held'
            if current.get('status') in {'submitting','submission_unknown','sending','job_bound'} or current.get('post_attempts',0):return current,'unknown'
            require(current.get('status') in READY_STATUSES,'Unknown intent state cannot be promoted to a new submission')
            lease=current.get('starter_lease_until')
            if current.get('starter_operation') and lease and queue.timestamp(lease)>self.now:return current,'busy'
        elif not create:return None,'absent'
        def mutate(state,old):
            if old is None:
                old={'schema':'tagesschau_daily_intent_v1','local_date':date,'timezone':'Europe/Berlin',
                    'claimed_at':_stamp(self.now),'origin':'official_daily_start_entrypoint','post_attempts':0,
                    'no_submission_yet':True,'new_job_reserve_gib':8,'listing_url':discovery.HOME_URL,
                    'not_before_local_time':'21:00','claimed_after_not_before':True}
                state.setdefault('daily_intents',{})[date]=old
            old.update(status='preparing',starter_operation=self.operation,
                starter_lease_until=_stamp(self.now+timedelta(seconds=LEASE_SECONDS)),
                automatic_start_authorization_ref=self.config['daily_current_date_authorization_ref'])
        result=self.change(date,None if current is None else _hash(current),mutate)
        return result,'owned'
    def validate_frozen(self,intent):
        canonical=_frozen(intent,intent['local_date'])
        ref=intent.get('discovery')
        require(isinstance(ref,dict) and set(ref)=={'path','sha256'},'Frozen source lacks original discovery proof')
        selected,actual=queue.read_file(self.root,ref['path'])
        require(actual==ref['sha256'] and selected.get('schema')=='official_tagesschau_sd270_v1'
            and selected.get('listing_url')==discovery.HOME_URL
            and selected.get('expected_width')==480 and selected.get('expected_height')==270,
            'Frozen original discovery proof or dimensions changed')
        require(all(selected.get(k)==intent.get(k) for k in ('programme_title','broadcast_at_utc','broadcast_at_berlin','episode_key','source_url','canonical_media_url','quality_label')),
            'Frozen source no longer equals its saved official discovery')
        if intent.get('listing_html'):
            listing,sha=None,None
            path=queue.confined(self.root,intent['listing_html']['path'])
            require(path.is_file() and queue.digest(path.read_bytes())==intent['listing_html']['sha256']==selected['listing_html_sha256'],
                'Frozen official listing proof changed')
        return canonical
    def validate_submission(self,intent):
        if not intent.get('post_attempts',0):return
        value=intent.get('submission',{});body=value.get('request_body')
        require(isinstance(body,dict) and body.get('source_url')==intent['source_url'] and body.get('title')==intent['title']
            and value.get('request_body_sha256')==_hash(body) and value.get('attempt')==1
            and queue.timestamp(value['started_at'])<=self.now,'Unknown sending body/time differs from frozen programme')
        if value.get('request'):
            actual,sha=queue.read_file(self.root,value['request']['path'])
            require(sha==value['request']['sha256'] and actual==body,'Unknown original request artifact changed')
    def discover(self,intent):
        date=intent['local_date']
        if intent.get('source_url'):
            self.validate_frozen(intent);return intent
        html=self.fetch();require(isinstance(html,str) and len(html.encode())<=4*1024**2,'Official listing too large')
        path=self.evidence/'listing.html';path.write_text(html);listing=_ref(self.root,path)
        local_end=datetime.combine(datetime.fromisoformat(date).date(),time.max,BERLIN)
        cutoff=min(self.now,local_end.astimezone(timezone.utc))
        try:selected=discovery.discover(html,now=cutoff,episode_key=intent.get('episode_key'))
        except discovery.NotReady as error:
            candidate=error.candidate or {}
            updates={'reason':str(error),'listing_html':listing}
            if candidate.get('broadcast_at_utc'):
                if queue.timestamp(candidate['broadcast_at_utc']).astimezone(BERLIN).date().isoformat()==date:
                    updates.update(candidate)
            return self.mark(intent,'waiting_source',**updates)
        if queue.timestamp(selected['broadcast_at_utc']).astimezone(BERLIN).date().isoformat()!=date:
            return self.mark(intent,'waiting_source',reason='Official listing has no regular programme for the due date',listing_html=listing)
        record=self.save('discovery.json',{**selected,'execution_checked_at_utc':_stamp(self.now),
            'discovery_cutoff_utc':_stamp(cutoff)})
        frozen={k:selected[k] for k in ('programme_title','broadcast_at_utc','broadcast_at_berlin','episode_key','source_url','canonical_media_url','quality_label')}
        frozen.update(title=_title(selected),expected_dimensions=[480,270],frozen_at=_stamp(self.now),
            discovery=record,listing_html=listing)
        if intent.get('episode_key'):require(intent['episode_key']==frozen['episode_key'],'Frozen candidate cannot change episode')
        def mutate(state,current):
            current.update(frozen,status='frozen')
            existing=state.setdefault('episodes',{}).get(current['episode_key'])
            if existing is not None:
                require(not _held(existing) and existing.get('status') not in TERMINAL and existing.get('local_date')==date and not existing.get('job_id'), 'Programme is held or already bound elsewhere')
                if existing.get('source_url'):require(existing['source_url']==frozen['source_url'],'Episode source conflict')
            episode=existing or {'schema':'tagesschau_daily_episode_v1','local_date':date,'daily_intent_date':date}
            episode.update(frozen,status='frozen',post_attempts=0,new_job_reserve_gib=8)
            state['episodes'][current['episode_key']]=episode
        return self.change(date,_hash(intent),mutate)
    def live(self):
        value=self.api('GET','/api/liveness')
        require(value.get('version')=='philos_lan_web_v1' and value.get('project_identity')==queue.digest(str(self.root).encode())
            and value.get('worker_alive') is True and value.get('stopping') is False,'Local production service is unavailable or has the wrong project identity')
        jobs=self.api('GET','/api/jobs');require(isinstance(jobs,dict) and isinstance(jobs.get('jobs'),list),'Malformed live serial queue')
        return jobs['jobs']
    def bind(self,intent,job,proof):
        date=intent['local_date'];job_id=job['id'];require(queue.JOB_ID.fullmatch(job_id),'Invalid returned job ID')
        def mutate(state,current):
            require(current.get('source_url')==intent['source_url'] and current.get('title')==intent['title']
                and current.get('episode_key')==intent['episode_key'] and not current.get('job_id'), 'Submission binding changed')
            if intent.get('submission'):
                require(current.get('submission',{}).get('token')==intent['submission'].get('token'), 'Submission token changed')
            current.update(status='job_bound',job_id=job_id,no_submission_yet=False,submitted_at=job['created_at'],submission_receipt=proof)
            current.setdefault('submission',{}).update(status='job_bound',job_id=job_id)
            episode=state.setdefault('episodes',{}).setdefault(current['episode_key'],{'schema':'tagesschau_daily_episode_v1','local_date':date})
            require(not episode.get('job_id') or episode['job_id']==job_id,'Frozen episode is already bound to another job')
            require(episode.get('local_date')==date and all(not episode.get(k) or episode[k]==current[k] for k in ('source_url','title')),
                'Existing episode date/source/title conflict')
            episode.update(episode_key=current['episode_key'],daily_intent_date=date,job_id=job_id,status='job_bound',
                source_url=current['source_url'],title=current['title'])
            watch=state.setdefault('watched',{}).setdefault(job_id,{'job_id':job_id,'programme_date':date,
                'episode_key':current['episode_key'],'title':current['title'],'source_url':current['source_url'],
                'status':job.get('status','queued'),'origin':'official_daily_start_entrypoint',
                'final_media_completed':False,'completion_verification':{'status':'pending'},
                'editorial_policy':job.get('options',{}).get('editorial_policy')})
            require(watch.get('episode_key')==current['episode_key'] and watch.get('job_id',job_id)==job_id
                and watch.get('programme_date',date)==date and all(not watch.get(k) or watch[k]==current[k] for k in ('source_url','title')),
                'Watch episode/date/source/title binding conflict')
            watch.setdefault('email',{'status':'pending','attempts':0,'send_attempts':0,'to':self.config['notification']['to']})
            watch.setdefault('submission_receipt',proof)
            if current.get('user_hold'):watch.setdefault('user_hold',copy.deepcopy(current['user_hold']))
        # A monitor may have changed unrelated projection/generation while POST was in flight.
        with InstanceLock(self.state_path.with_suffix('.lock')):
            state=self.state();current=state['daily_intents'][date]
            mutate(state,current);state['generation']+=1;write_json(self.state_path,state)
        return self.receipt('job_bound',job_id=job_id,local_date=date,new_post_attempts=self.new_posts)
    def unknown(self,intent):
        self.validate_frozen(intent);self.validate_submission(intent);jobs=_jobs(self.root)
        started=intent.get('submission',{}).get('started_at')
        matches=_matches(jobs,intent,since=started)
        if matches:return self.bind(intent,matches[0],self.save('reconciled_submission.json',{'job':matches[0],'new_post':False}))
        if intent.get('status')!='submission_unknown':self.mark(intent,'submission_unknown',reason='A POST may have been sent; local exact URL/title/time does not yet prove one job; never repost')
        return self.receipt('submission_unknown',local_date=intent['local_date'],new_post_attempts=self.new_posts)
    def execute(self,mode):
        require(mode in {'daily','resume'},'Unsupported daily start mode');self.mode=mode
        local=self.now.astimezone(BERLIN);date=due_date(self.now).isoformat()
        intent,state=self.claim(date,create=mode=='daily')
        if state=='absent':return self.receipt('no_due_intent',latest_due_date=date)
        if state=='bound':
            require(intent.get('episode_key') and intent.get('source_url'),'Bound intent lacks its frozen source')
            self.validate_frozen(intent);matches=_matches(_jobs(self.root),intent)
            require(len(matches)==1 and matches[0]['id']==intent['job_id'],'Bound job differs from exact local programme')
            return self.receipt('job_bound',job_id=intent['job_id'],local_date=date,new_post_attempts=0)
        if state in {'held','busy','terminal'}:return self.receipt('preserved_terminal_intent' if state=='terminal' else state,local_date=date)
        if state=='unknown':return self.unknown(intent)
        intent=self.discover(intent)
        if not intent.get('source_url'):return self.receipt('waiting_source',local_date=date)
        jobs=_jobs(self.root);matches=_matches(jobs,intent)
        if matches:return self.bind(intent,matches[0],self.save('existing_job.json',{'job':matches[0],'new_post':False}))
        if _orphan_source(self.root,intent,jobs):
            self.mark(intent,'submission_unknown',reason='An exact URL source authorization exists without a local job; do not repost')
            return self.receipt('submission_unknown',local_date=date,new_post_attempts=0)
        try:
            head=self.head(intent['source_url'])
            require(head.get('status')==200 and head.get('url')==intent['source_url']
                and discovery.canonical_media(head.get('final_url',''))==_frozen(intent,date)
                and head.get('content_type','').split(';')[0].strip().lower()=='video/mp4','HEAD did not preserve the official SD270 source')
            size=int(head['content_length']);require(0<size<=20*GIB,'Missing or invalid original source length')
            free=self.free();require(type(free) is int,'Disk free bytes must be an integer')
            required=8*GIB+size+MARGIN
            self.save('source_head.json',head);self.save('storage_preflight.json',{'actual_free_bytes':free,'required_bytes':required,'reserve_bytes':8*GIB,'source_bytes':size,'margin_bytes':MARGIN})
            if free<required:
                self.mark(intent,'waiting_storage',actual_free_bytes=free,required_free_bytes=required)
                return self.receipt('waiting_storage',local_date=date)
            live=self.live()
        except (OSError,ValueError,KeyError) as error:
            self.mark(intent,'waiting_api_or_source',reason=str(error)[:500])
            return self.receipt('waiting_api_or_source',local_date=date)
        active={j['id'] for j in [*jobs,*live] if j.get('status') in {'running','queued'}}
        if len(active)>=3:
            self.mark(intent,'waiting_queue',active_job_ids=sorted(active))
            return self.receipt('waiting_queue',local_date=date)
        # Last local dedupe follows all network waits and precedes the durable sending reservation.
        jobs=_jobs(self.root);matches=_matches(jobs,intent)
        if matches:return self.bind(intent,matches[0],self.save('late_existing_job.json',{'job':matches[0],'new_post':False}))
        body={'source_url':intent['source_url'],'title':intent['title'],'source_language':self.config.get('source_language','auto')}
        request_ref=self.save('submission_request.json',body)
        def reserve(state,current):
            own_episode=_own_episode(state,current)
            require(not _held(own_episode) and own_episode.get('status') not in TERMINAL and not own_episode.get('job_id'),'This episode is held, complete or already bound')
            self.validate_frozen(current)
            require(not any(watch.get('episode_key')==current.get('episode_key') for watch in state.get('watched',{}).values()),
                'Current programme already has a watch; reconcile without POST')
            require(not _matches(_jobs(self.root),current),'A local job appeared before sending; reconcile without POST')
            require(not _held(current) and current.get('starter_operation')==self.operation
                and not current.get('job_id') and not current.get('post_attempts',0),'Intent no longer authorizes one submission')
            current.update(status='submitting',post_attempts=1,no_submission_yet=False,
                submission={'status':'submitting','token':self.operation,'attempt':1,'started_at':_stamp(self.now),
                    'request_body':body,'request_body_sha256':_hash(body),'request':request_ref,
                    'unknown_requires_local_observation_before_repost':True})
        intent=self.change(date,_hash(intent),reserve)
        try:
            self.new_posts+=1
            response=self.api('POST','/api/jobs',body);proof=self.save('submission_response.json',response)
            require(isinstance(response,dict) and queue.JOB_ID.fullmatch(str(response.get('id',''))),'API returned no exact job ID')
            matches=_matches(_jobs(self.root),intent,since=intent['submission']['started_at'])
            require(len(matches)==1 and matches[0]['id']==response['id'] and response.get('title')==body['title'],'Response/local exact submission binding is incomplete')
            return self.bind(intent,matches[0],proof)
        except Exception as error:
            self.save('submission_error.json',{'error_type':type(error).__name__,'detail':str(error)[:500],'post_may_have_been_sent':True})
            # Do not issue another POST, including after a crash before the socket call.
            return self.unknown(intent)


def run(root,mode,evidence_dir,**kwargs):
    executor=Executor(root,evidence_dir,**kwargs)
    try:return executor.execute(mode)
    except Exception as error:
        executor.mode=mode
        executor.save('entry_error.json',{'error_type':type(error).__name__,'detail':str(error)[:800],
            'no_automatic_repost':True})
        return executor.receipt('blocked',error_type=type(error).__name__,reason=str(error)[:800],new_post_attempts=executor.new_posts)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--mode',choices=('daily','resume'),required=True);parser.add_argument('--evidence-dir',type=Path,required=True);args=parser.parse_args(argv)
    result=run(args.root,args.mode,args.evidence_dir)
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result['status'] in {'job_bound','not_due','no_due_intent','busy','held','preserved_terminal_intent'} else 2

if __name__=='__main__':raise SystemExit(main())
