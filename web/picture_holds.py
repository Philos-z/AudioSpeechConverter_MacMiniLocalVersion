"""Explicit locally registered picture holds; no default timing relaxation."""
import copy,hashlib,json,math,re,wave
from pathlib import Path
SCHEMA='scoped_picture_hold_review_v1'
DEFAULT=.35
LIMITS={'max_speech_excess_s':1.25,'max_last_frame_hold_s':1.65}
CHOICE='preserve_complete_audio_extend_last_picture_frame'
def read(p):return json.loads(Path(p).read_text())
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def require(v,msg):
 if not v:raise ValueError('PICTURE_HOLD: '+msg)
def registration(folder):
 """Only operator-owned local configuration may register a historical exception."""
 folder=Path(folder).resolve();root=folder.parent.parent
 require(folder.parent.name=='jobs','Only a registered local job is supported')
 path=root/'config/source_scopes.local.json'
 require(path.is_file() and not path.is_symlink() and path.resolve()==path,'No local picture-hold scope is registered')
 value=read(path);require(isinstance(value,dict) and value.get('schema')=='local_source_scopes_v1','Invalid local source-scope schema')
 item=value.get('picture_holds')
 require(isinstance(item,dict) and set(item)=={'job','ids','source_row_count'},'No valid local picture-hold scope is registered')
 require(item['job']==folder.name and re.fullmatch(r'[A-Za-z0-9_-]+',item['job']),'Only the registered local job is supported')
 ids=item['ids'];count=item['source_row_count']
 require(isinstance(ids,list) and ids and all(isinstance(s,str) and re.fullmatch(r'line\d{4}',s) for s in ids) and len(set(ids))==len(ids),'Invalid registered picture-hold IDs')
 require(type(count) is int and count>0 and all(1<=int(s[4:])<=count for s in ids),'Invalid registered source coverage')
 return item
def file_receipt(p):return {'path':str(Path(p).resolve()),'sha256':sha(p)}
def binding(folder):
 return {'segments':file_receipt(folder/'segments.json'),'speakers':file_receipt(folder/'speakers.json'),'source_video':file_receipt(read(folder/'source.json')['input'])}
def auth_guard(a):
 require(isinstance(a,dict) and a.get('choice')==CHOICE and a.get('human_reviewed') is False,'Explicit scoped choice is required; it never means listening approval')
 require(a.get('origin') in ('actual_user_answer','operator_machine_draft'),'Choice provenance must be explicit')
 if a['origin']=='actual_user_answer':require(a.get('user_authorized') is True and isinstance(a.get('answer_text'),str) and bool(a['answer_text'].strip()),'An actual user answer is missing')
 else:require(a.get('user_authorized') is False and a.get('answer_text') is None,'A machine draft cannot claim a user answer')
def scope(folder):
 folder=Path(folder).resolve();registration(folder);return folder

def actual_holds(folder,edl):
 """Recompute source boundaries and actual WAV geometry, never trust supplied durations."""
 registered=registration(folder);ids_registered=registered['ids']
 rows=read(folder/'segments.json')['segments'];lookup={r['segment_id']:r for r in rows};positions={r['segment_id']:i for i,r in enumerate(rows)}
 require(len(rows)==registered['source_row_count'] and len(lookup)==registered['source_row_count'],'Registered original source coverage required')
 utterances=edl['utterances'];require([r['segment_id'] for r in utterances]==[r['segment_id'] for r in rows],'EDL source coverage/order changed')
 source=read(folder/'source.json');require(Path(edl['source_input']).resolve()==Path(source['input']).resolve() and edl['fps']==25,'Actual source picture/FPS changed')
 result={}
 for index,clip in enumerate(edl['clips']):
  if clip['kind']!='speech':continue
  members=[r for r in utterances if r['clip_index']==index];ids=[r['segment_id'] for r in members]
  require(ids==clip['segment_ids'],'Clip member coverage changed')
  p=positions[ids[0]];after=p+len(ids);require(ids==[r['segment_id'] for r in rows[p:after]],'Source members are not adjacent')
  start=rows[p]['start_s'];boundary=rows[after]['start_s'] if after<len(rows) else source['duration_s'];available=max(0.,boundary-start)
  speech=sum(r['speech_s'] for r in members)
  require(abs(clip['speech_s']-speech)<1e-7 and abs(clip['source_start_s']-start)<1e-7,'Clip source/speech changed')
  excess=speech-available
  if excess<=DEFAULT+1e-6:continue
  require(len(ids)==1 and ids[0] in ids_registered,'A new/unregistered overrun or grouped overrun remains blocked')
  sid=ids[0];row=members[0];original=lookup[sid];audio=Path(row['audio']).resolve()
  require(all(row[k]==original[k] for k in ('speaker_id','start_s','end_s','text_de','text_zh')),'Source text/voice/window changed: '+sid)
  with wave.open(str(audio),'rb') as w:frames,rate,channels,width=w.getnframes(),w.getframerate(),w.getnchannels(),w.getsampwidth()
  require(frames==row['audio_frames'] and rate==row['audio_sample_rate'] and channels==1 and width==3 and frames>0,'Actual PCM24 geometry changed: '+sid)
  require(abs(frames/rate-speech)<1e-7 and sha(audio)==row['audio_sha256'],'Actual complete audio SHA/duration changed: '+sid)
  output_frames=math.ceil((speech+DEFAULT)*25-1e-8);hold=output_frames/25-available
  require(excess<=LIMITS['max_speech_excess_s']+1e-6 and hold<=LIMITS['max_last_frame_hold_s']+1e-6,'Explicit picture hold bound exceeded: '+sid)
  require(clip['frames']==output_frames and abs(clip['source_duration_s']-available)<1e-7 and abs(clip['source_end_s']-boundary)<1e-7 and abs(clip['freeze_s']-hold)<1e-7 and clip['pause_s']>=DEFAULT-1e-7,'Existing last-frame hold geometry changed: '+sid)
  result[sid]={'segment_id':sid,'speaker_id':original['speaker_id'],'audio':{'path':str(audio),'sha256':row['audio_sha256'],'frames':frames,'sample_rate':rate},'source_start_s':start,'source_row_end_s':original['end_s'],'next_source_boundary_s':boundary,'available_source_s':available,'speech_s':speech,'actual_excess_s':excess,'last_frame_hold_s':hold,'output_frames':output_frames}
 require(set(result)==set(ids_registered),'All and only the registered actual overruns must be present')
 return [result[sid] for sid in ids_registered]

def make_receipt(folder,edl,authorization):
 """Return a candidate in memory. Only the operator may explicitly save/enable it."""
 folder=scope(folder);auth_guard(authorization)
 return {'schema':SCHEMA,'job':folder.name,'scope':'last_source_frame_only_no_audio_changes','human_reviewed':False,'authorization':copy.deepcopy(authorization),'limits':copy.deepcopy(LIMITS),'default_other_windows_tolerance_s':DEFAULT,'inputs':binding(folder),'holds':actual_holds(folder,edl),'audio_cut':False,'audio_speed_or_pitch_changed':False,'translation_changed':False}

def checked_receipt(folder,edl,path):
 folder=scope(folder);path=Path(path)
 require(path==folder/'picture_hold_review.json' and not path.is_symlink() and path.resolve()==path,'Only the fixed job receipt is accepted')
 r=read(path);auth_guard(r.get('authorization'))
 require(r.get('schema')==SCHEMA and r.get('job')==folder.name and r.get('scope')=='last_source_frame_only_no_audio_changes' and r.get('human_reviewed') is False,'Wrong receipt scope or subjective claim')
 require(r.get('limits')==LIMITS and r.get('default_other_windows_tolerance_s')==DEFAULT and r.get('inputs')==binding(folder),'Actual source/speaker/segments SHA or limits changed')
 require(all(r.get(k) is False for k in ('audio_cut','audio_speed_or_pitch_changed','translation_changed')),'Receipt cannot authorize audio/text changes')
 require(r.get('holds')==actual_holds(folder,edl),'Actual WAV/window/overrun no longer equals the reviewed nine')
 return r

def apply(folder,edl):
 folder=Path(folder).resolve();path=folder/'picture_hold_review.json'
 if not path.exists():return edl
 registered=registration(folder);ids_registered=registered['ids']
 r=checked_receipt(folder,edl,path);old=copy.deepcopy(edl['issues'])
 require(len(old)==len(ids_registered) and all(x.get('reason')=='complete_audio_exceeds_source_window' and len(x.get('ids',[]))==1 for x in old) and {x['ids'][0] for x in old}==set(ids_registered),'Only the registered original timing issues may be explicitly resolved')
 for issue in old:
  hold=next(x for x in r['holds'] if x['segment_id']==issue['ids'][0])
  require(all(abs(issue[k]-hold[{'speech_s':'speech_s','available_source_s':'available_source_s','excess_s':'actual_excess_s'}[k]])<1e-7 for k in ('speech_s','available_source_s','excess_s')),'Original default-gate diagnostic changed')
 receipt=file_receipt(path);edl['picture_hold_review']={'schema':SCHEMA,'receipt':receipt,'human_reviewed':False,'default_other_windows_tolerance_s':DEFAULT,'limits':copy.deepcopy(LIMITS),'resolved_ids':list(ids_registered),'original_issue_count':len(old)}
 edl['resolved_timing_issues']=[{**x,'resolution':'explicit_last_frame_hold_preserve_all_audio','receipt_sha256':receipt['sha256']} for x in old]
 for clip in edl['clips']:
  if clip.get('segment_ids') and clip['segment_ids'][0] in ids_registered:clip['picture_hold_approval']={'segment_id':clip['segment_ids'][0],'receipt_sha256':receipt['sha256']}
 edl['issues']=[]
 validate_edl(edl)
 return edl

def validate_edl(edl):
 proof=edl.get('picture_hold_review')
 if proof is None:
  require(not edl.get('resolved_timing_issues') and not any('picture_hold_approval' in c for c in edl['clips']),'Picture hold approval metadata is missing')
  return {}
 receipt=proof.get('receipt',{});path=Path(receipt.get('path',''));folder=path.parent
 ids_registered=registration(folder)['ids']
 require(proof.get('schema')==SCHEMA and proof.get('human_reviewed') is False and proof.get('resolved_ids')==list(ids_registered) and proof.get('original_issue_count')==len(ids_registered) and proof.get('limits')==LIMITS and proof.get('default_other_windows_tolerance_s')==DEFAULT,'EDL hold provenance changed')
 require(path.is_file() and sha(path)==receipt.get('sha256'),'Picture hold receipt SHA differs')
 r=checked_receipt(folder,edl,path);resolved=edl.get('resolved_timing_issues',[])
 require(len(resolved)==len(ids_registered) and {x.get('ids',[None])[0] for x in resolved}==set(ids_registered),'Original overrun diagnostics were removed or forged')
 holds={x['segment_id']:x for x in r['holds']}
 for issue in resolved:
  h=holds[issue['ids'][0]]
  require(issue.get('reason')=='complete_audio_exceeds_source_window' and issue.get('resolution')=='explicit_last_frame_hold_preserve_all_audio' and issue.get('receipt_sha256')==receipt['sha256'] and all(abs(issue[k]-h[v])<1e-7 for k,v in [('speech_s','speech_s'),('available_source_s','available_source_s'),('excess_s','actual_excess_s')]),'Resolved diagnostic differs from actual audio/window')
 for clip in edl['clips']:
  approval=clip.get('picture_hold_approval');ids=clip.get('segment_ids',[])
  if ids and ids[0] in holds:require(approval=={'segment_id':ids[0],'receipt_sha256':receipt['sha256']},'Clip approval provenance changed')
  else:require(approval is None,'An unregistered clip claims hold approval')
 return holds
