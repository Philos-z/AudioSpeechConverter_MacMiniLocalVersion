"""Resolve an explicitly registered local audio correction for export.

Private source bindings are loaded only from operator-owned local configuration.
No Proxy or automatic model output may select replacement audio.
"""
import argparse
import copy
import hashlib
import json
import math
import re
from pathlib import Path

SCHEMA='scoped_native_audio_selection_v1'
NATIVE='native_mlx_whole_utterance_v1'
REGISTRY_KEYS=frozenset(('base_key','base_wav','child_key','child_wav','child_manifest','provenance','reference','vocals','raw_speakers','source_video','small_proof','turbo_proof'))
FIELDS=('segment_id','speaker_id','start_s','end_s','text_zh','text_de')
ROW_FIELDS=('segment_id','speaker_id','source_index','source_start_s','source_end_s','reference','cache_key','content','audio','speaker_cache_key','speaker_cache_content','speaker_cache','duration_s','sha256','frames','sample_rate','channels','experimental_execution')

def check(value,message):
 if not value:raise ValueError(message)
def read(path):return json.loads(Path(path).read_text())
def registration(root,job):
 root=Path(root).resolve();path=root/'config/source_scopes.local.json'
 check(path.is_file() and not path.is_symlink() and path.resolve()==path,'No local audio-correction scope is registered')
 value=read(path);check(isinstance(value,dict) and value.get('schema')=='local_source_scopes_v1','Invalid local source-scope schema')
 item=value.get('native_audio_selection')
 required={'job','segment_id','child_job','registry','source_row_count','selected_source_index','speaker_id','reference_relative_path','source_window_s','raw_reference_segment_ids','reference_audio','expected_characters'}
 check(isinstance(item,dict) and set(item)==required,'No valid local audio-correction scope is registered')
 check(item['job']==job and isinstance(job,str) and re.fullmatch(r'[A-Za-z0-9_-]+',job),'No audio correction is registered for this job')
 check(isinstance(item['child_job'],str) and re.fullmatch(r'[A-Za-z0-9_-]+',item['child_job']) and item['child_job']!=job,'Invalid registered child job')
 count,index=item['source_row_count'],item['selected_source_index']
 check(type(count) is int and count>0 and type(index) is int and 0<=index<count and item['segment_id']=='line'+str(index+1).zfill(4),'Invalid registered source coverage or selected row')
 check(isinstance(item['speaker_id'],str) and re.fullmatch(r'S\d+',item['speaker_id']),'Invalid registered source speaker')
 registry=item['registry'];check(isinstance(registry,dict) and set(registry)==REGISTRY_KEYS and all(isinstance(v,str) and re.fullmatch(r'[a-f0-9]{64}',v) for v in registry.values()),'Incomplete registered evidence SHA bindings')
 relative=item['reference_relative_path'];check(isinstance(relative,str) and relative and not Path(relative).is_absolute() and '..' not in Path(relative).parts,'Invalid registered child reference path')
 window=item['source_window_s'];check(isinstance(window,list) and len(window)==2 and all(finite(v) for v in window) and 0<=window[0]<window[1],'Invalid registered source window')
 members=item['raw_reference_segment_ids'];check(isinstance(members,list) and members and all(isinstance(v,str) and re.fullmatch(r'seg\d+',v) for v in members) and len(set(members))==len(members),'Invalid registered acoustic members')
 audio=item['reference_audio'];check(isinstance(audio,dict) and set(audio)=={'frames','sample_rate','channels','subtype'} and type(audio['frames']) is int and audio['frames']>0 and type(audio['sample_rate']) is int and audio['sample_rate']>0 and audio['channels']==1 and audio['subtype']=='PCM_24' and abs(audio['frames']/audio['sample_rate']-(window[1]-window[0]))<1/audio['sample_rate'],'Invalid registered reference geometry')
 check(type(item['expected_characters']) is int and item['expected_characters']>0,'Invalid registered character coverage')
 return item
def sha(path):
 h=hashlib.sha256()
 with Path(path).open('rb') as handle:
  for block in iter(lambda:handle.read(1024*1024),b''):h.update(block)
 return h.hexdigest()
def key(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def binding(path):return {'path':str(Path(path).resolve()),'sha256':sha(path)}
def save(path,value):
 path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);temporary=path.with_suffix(path.suffix+'.partial');temporary.write_text(json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+'\n');temporary.replace(path)
def verified(record,expected=None):
 check(isinstance(record,dict) and sha(record['path'])==record['sha256'],'Bound evidence SHA changed')
 if expected:check(record['sha256']==expected,'Evidence is outside registered correction')
 return Path(record['path']).resolve()
def row_binding(row):return {name:row[name] for name in FIELDS}
def normalized(text):return ''.join(c.lower() for c in text if c.isalnum())
def finite(value):return type(value) in (int,float) and math.isfinite(value)

def raw_guard(row):
 execution=row['experimental_execution'];guard=execution['semantic_logits_guard'];signature=guard['signature'];transaction=guard['transaction']
 check(signature==row['content']['parameters']['experimental_profile']['semantic_logits_guard'],'Raw finite signature differs from generation parameters')
 check(signature['check_mode']=='all_steps' and 'BEFORE' in signature['point'] and signature['stop_after_first_sample'] is False and signature['sampling_math_or_rng_changed'] is False,'Raw finite guard must protect every original sampling call')
 counts=[transaction[name] for name in ('sampling_calls','checked_calls','original_sampler_calls')]
 check(all(type(n) is int and n>0 for n in counts) and len(set(counts))==1,'Incomplete actual raw finite checks')
 check(transaction['utterance_id']==row['segment_id'] and transaction['nonfinite_rejected'] is False and transaction['diagnostic_only'] is False,'Raw finite transaction failed or is diagnostic only')
 first=transaction['first_raw_logits'];n=first['values'];check(type(n) is int and n>0 and first['finite_count']==n and first['all_finite'] is True and all(first[name]==0 for name in ('nan_count','positive_inf_count','negative_inf_count')),'First raw semantic logits are not all finite')
 check(all(finite(first[name]) for name in ('finite_min','finite_max')),'Nonfinite raw logit summary')
 preclip=execution['preclip_guard'];check(preclip['mode']=='bigvgan_conv_post_static_gain_before_internal_clamp_v2' and 'BEFORE' in preclip['point'] and preclip['prevents_internal_clamp_for_this_tensor'] is True and preclip['time_or_pitch_changed'] is False and preclip['compressor_used'] is False and finite(preclip['protected_peak']) and 0<=preclip['protected_peak']<=.950001 and preclip['ceiling']==.95,'Actual preclip guard failed')
 return guard

def audio_record(row,guard=False):
 import numpy as np
 import soundfile as sf
 check(row['status'] in ('cached','generated') and row['content']['schema']==NATIVE and key(row['content'])==row['cache_key'],'Native cache content identity mismatch')
 audio=Path(row['audio']).resolve();sidecar=audio.with_suffix('.json');entry=read(sidecar)
 check(entry['schema']==NATIVE and entry['cache_key']==row['cache_key'] and entry['content']==row['content'],'Native sidecar content mismatch')
 check(entry['sha256']==row['sha256']==sha(audio),'Actual WAV SHA mismatch')
 check(entry['experimental_execution']==row['experimental_execution'],'Actual generation guard/manifest mismatch')
 with sf.SoundFile(audio) as handle:
  frames,rate,channels=handle.frames,handle.samplerate,handle.channels
  check(handle.subtype=='PCM_24','Native WAV must preserve original PCM24')
  for block in handle.blocks(blocksize=65536):check(bool(np.isfinite(block).all()),'Nonfinite actual audio samples')
 check(type(row['frames']) is int and frames==row['frames']==entry['frames'] and rate==row['sample_rate']==entry['sample_rate'] and channels==row['channels']==entry['channels']==1 and frames>0,'Native WAV geometry mismatch')
 check(finite(row['duration_s']) and abs(row['duration_s']-frames/rate)<1/rate and abs(entry['duration_s']-frames/rate)<1/rate,'Native duration mismatch')
 check(sha(row['reference'])==row['content']['reference_sha256'],'Native reference SHA changed')
 if guard:raw_guard(row)
 return {'audio':binding(audio),'sidecar':binding(sidecar),'cache_key':row['cache_key']}

def source_rows(root,job):
 registered=registration(root,job)
 folder=Path(root).resolve()/'jobs'/job;rows=read(folder/'segments.json')['segments'];ids=[r['segment_id'] for r in rows]
 check(len(rows)==registered['source_row_count'] and len(set(ids))==registered['source_row_count'] and ids==['line'+str(i).zfill(4) for i in range(1,registered['source_row_count']+1)],'Registered original source coverage/order changed')
 check(all(r.get('translation_reviewed') is True and r.get('speaker_reviewed') is True for r in rows),'Unreviewed source row')
 return folder,rows

def base_manifest(path,rows,full=True,*,registered):
 JOB=registered['job'];SID=registered['segment_id'];REG=registered['registry'];count=registered['source_row_count']
 base=read(path);check(base['schema']==NATIVE and base['job']==JOB and base['phase']=='complete' and base['pending_unique_keys']==0 and base['selected_utterances']==count,'Main native manifest is not complete')
 entries=base['utterances'];check([r['segment_id'] for r in entries]==[r['segment_id'] for r in rows] and len(entries)==count,'Main manifest has missing, extra, reordered or duplicate rows')
 original=entries[registered['selected_source_index']];check(original['segment_id']==SID and original['speaker_id']==registered['speaker_id'] and original['cache_key']==REG['base_key'] and original['sha256']==REG['base_wav'],'Original row is not the registered base audio')
 for index,(entry,row) in enumerate(zip(entries,rows)):
  check(type(entry['source_index']) is int and entry['source_index']==index,'Main native source index changed')
  if not full and row['segment_id']!=SID:continue
  check(entry['speaker_id']==row['speaker_id'] and entry['source_start_s']==row['start_s'] and entry['source_end_s']==row['end_s'] and entry['content']['text_zh']==row['text_zh'] and entry['content']['effective_tts_text']==(row.get('tts_pronunciation_text') or row['text_zh']),'Current native/source row mismatch: '+row['segment_id'])
  check(entry['content']['model_identity']==original['content']['model_identity'] and entry['content']['parameters']==base['parameters'],'Native model or parameters changed between rows')
  if full or row['segment_id']==SID:audio_record(entry,guard=True)
 return base,original

def child_evidence(root,folder,current,original,*,registered):
 JOB=registered['job'];SID=registered['segment_id'];CHILD=registered['child_job'];REG=registered['registry'];speaker=registered['speaker_id'];window=registered['source_window_s']
 child=Path(root).resolve()/'jobs'/CHILD;manifest=child/'tts/native25/manifest.json';check(sha(manifest)==REG['child_manifest'],'Unregistered child manifest')
 native=read(manifest);check(native['schema']==NATIVE and native['job']==CHILD and native['phase']=='complete' and native['pending_unique_keys']==0 and native['selected_utterances']==1 and len(native['utterances'])==1,'Child must contain exactly one complete actual row')
 chosen=native['utterances'][0];check(chosen['segment_id']==SID and chosen['speaker_id']==speaker and chosen['cache_key']==REG['child_key'] and chosen['sha256']==REG['child_wav'],'Child is outside registered audio choice')
 for field in ('source_start_s','source_end_s'):check(chosen[field]==original[field],'Child source window changed')
 before,after=copy.deepcopy(original['content']),copy.deepcopy(chosen['content']);before.pop('reference_sha256');after.pop('reference_sha256');check(before==after and native['parameters']==original['content']['parameters'],'Only the reference SHA may differ in child content')
 child_rows=read(child/'segments.json')['segments'];check(len(child_rows)==1 and row_binding(child_rows[0])==row_binding(current),'Child original bilingual text/voice/window differs from current source')
 check(Path(chosen['reference']).resolve()==child/registered['reference_relative_path'] and chosen['content']['reference_sha256']==REG['reference'],'Wrong scoped reference')
 actual=audio_record(chosen,guard=True);provenance_path=child/'probe_provenance.json';check(sha(provenance_path)==REG['provenance'],'Unregistered source reference provenance');provenance=read(provenance_path)
 check(provenance['parent_job']==JOB and provenance['probe_job']==CHILD and provenance['source_window_s']==window and provenance['new_reference_no_filter_or_gain_applied'] is True and provenance['production_speakers_modified'] is False and provenance['human_identity_verified'] is False and provenance['returncode']==0,'Wrong source-window reference transaction')
 records=provenance['source_binding'];expected_paths={'source_video':Path(read(folder/'source.json')['input']).resolve(),'source_json':folder/'source.json','vocals':folder/'separated/htdemucs/original/vocals.wav','raw_speaker_assignments':folder/'speaker_assignments.json','original_reference':folder/'references'/(speaker+'.wav'),'new_reference':Path(chosen['reference']).resolve()}
 for name,path in expected_paths.items():
  check(Path(records[name]['path']).resolve()==path,'Reference provenance points at different '+name);verified(records[name],{'source_video':REG['source_video'],'vocals':REG['vocals'],'raw_speaker_assignments':REG['raw_speakers'],'new_reference':REG['reference']}.get(name))
 check(records['original_reference']['sha256']==original['content']['reference_sha256'],'Original reference changed')
 raw=read(folder/'speaker_assignments.json')['segments'];selected=[r for r in raw if r['end_s']>window[0] and r['start_s']<window[1]]
 check([r['segment_id'] for r in selected]==registered['raw_reference_segment_ids'] and selected==provenance['raw_acoustic_members'] and all(r['speaker_id']==speaker and r.get('overlap') is False for r in selected),'Reference source includes unregistered or competing speaker segments')
 import soundfile as sf
 info=sf.info(chosen['reference']);check((info.frames,info.samplerate,info.channels,info.subtype)==tuple(registered['reference_audio'][name] for name in ('frames','sample_rate','channels','subtype')),'Scoped reference must preserve registered original PCM24 geometry')
 return native,chosen,{'manifest':binding(manifest),'segments':binding(child/'segments.json'),'provenance':binding(provenance_path),'audio':actual,'reference':binding(chosen['reference'])}

def verify_proof(record,kind,row,*,registered):
 CHILD=registered['child_job'];REG=registered['registry']
 path=verified(record,REG[kind+'_proof']);proof=read(path);check(proof['job']==CHILD and proof['target_conditioning'] is False and proof['cer']==0 and proof['expected_characters']==proof['recognized_characters']==registered['expected_characters'] and normalized(proof['expected_text'])==normalized(proof['recognized_text'])==normalized(row['text_zh']),'Actual target-free complete character proof failed')
 if kind=='small':
  actual_observation=record.get('observation',proof['observation'])
  observation=read(verified(actual_observation,proof['observation']['sha256']));check(observation['audio_sha256']==REG['child_wav'] and observation['profile']['target_conditioning'] is False and observation['phase']=='complete','Wrong small observation audio or mode');options=observation['profile']['transcribe_options']
 else:
  check(proof['audio']['sha256']==REG['child_wav'] and proof['model']['device']=='cpu','Wrong independent observation audio or device');options=proof['options']
 check(options.get('initial_prompt') is None and options.get('prefix') is None and options.get('hotwords') is None and options['condition_on_previous_text'] is False,'Target-conditioned observation is forbidden')
 return record

def portable_proofs(root,proofs):
 root=Path(root).resolve()
 check(isinstance(proofs['small'].get('observation'),dict),'Production receipt needs an explicit portable small observation record')
 for record in (proofs['small'],proofs['turbo'],proofs['small']['observation']):
  check(Path(record['path']).resolve().is_relative_to(root),'Production proof/observation must be stored inside deployment root')
  verified(record)

def safe_output(root,job,output,kind):
 root=Path(root).resolve();raw=Path(output).absolute();path=raw.resolve();folder=root/'jobs'/job
 allowed=folder/('native_audio_selections.json' if kind=='receipt' else 'tts/native25/resolved_export.json')
 check(path.suffix=='.json' and not raw.is_symlink(),'Output must be a regular JSON file')
 if raw.is_relative_to(root) or path.is_relative_to(root):check(raw==path==allowed,'Refuse to overwrite any production file except the fixed scoped output')
 if path.exists():
  previous=read(path);expected=SCHEMA if kind=='receipt' else NATIVE
  check(previous.get('schema')==expected and previous.get('job')==job,'Refuse to overwrite an unrelated existing JSON')
 return path

def prepare_receipt(root,job,manifest,small_proof,turbo_proof,output,small_observation=None):
 registered=registration(root,job);SID=registered['segment_id'];selected_index=registered['selected_source_index']
 output=safe_output(root,job,output,'receipt');folder,rows=source_rows(root,job);base,original=base_manifest(manifest,rows,full=False,registered=registered);current=rows[selected_index];_,chosen,evidence=child_evidence(root,folder,current,original,registered=registered)
 proofs={kind:binding(path) for kind,path in [('small',small_proof),('turbo',turbo_proof)]}
 if small_observation is not None:proofs['small']['observation']=binding(small_observation)
 if output.is_relative_to(Path(root).resolve()):portable_proofs(root,proofs)
 for kind in ('small','turbo'):verify_proof(proofs[kind],kind,current,registered=registered)
 result={'schema':SCHEMA,'job':job,'selected_segment_ids':[SID],'source_segments':binding(folder/'segments.json'),'current_row':row_binding(current),'production_speakers':binding(folder/'speakers.json'),'base_original':audio_record(original,guard=True),'child':evidence,'actual_character_observations':proofs,'selection_policy':'fixed registered same-source reference correction, not an AI selectable operation','source_and_old_audio_preserved':True,'human_listening_approved':False}
 save(output,result);return result

def compose(root,job,manifest,output,receipt):
 registered=registration(root,job);SID=registered['segment_id'];selected_index=registered['selected_source_index']
 output=safe_output(root,job,output,'resolved');folder,rows=source_rows(root,job);check(Path(manifest).resolve()==folder/'tts/native25/manifest.json','Only the parent native manifest may be resolved')
 check(receipt['schema']==SCHEMA and receipt['job']==job and receipt['selected_segment_ids']==[SID] and receipt['current_row']==row_binding(rows[selected_index]),'Receipt scope or current six source fields changed')
 check(verified(receipt['source_segments'])==folder/'segments.json' and verified(receipt['production_speakers'])==folder/'speakers.json','Receipt current source/speakers binding mismatch')
 base,original=base_manifest(manifest,rows,registered=registered);child,chosen,evidence=child_evidence(root,folder,rows[selected_index],original,registered=registered)
 check(receipt['base_original']==audio_record(original,guard=True) and receipt['child']==evidence,'Receipt does not select the registered exact original and replacement audio')
 if output.is_relative_to(Path(root).resolve()):portable_proofs(root,receipt['actual_character_observations'])
 for kind in ('small','turbo'):verify_proof(receipt['actual_character_observations'][kind],kind,rows[selected_index],registered=registered)
 entries=[]
 for index,entry in enumerate(base['utterances']):
  selected=chosen if entry['segment_id']==SID else entry;stable={name:copy.deepcopy(selected[name]) for name in ROW_FIELDS};stable['source_index']=entry['source_index'];stable.update(status='cached',cache_check='verified original generation WAV and sidecar; no synthesis in resolver');entries.append(stable)
 composition={'schema':'registered_reference_export_composition_v1','registered_correction':SID,'current_source':receipt['source_segments'],'current_rows':[row_binding(r) for r in rows],'selection_receipt_identity':key(receipt),'base_stable_audio_identity':key([(r['segment_id'],r['cache_key'],r['sha256']) for r in base['utterances']]),'child':evidence,'implementation_sha256':sha(__file__),'source_manifests':[str(Path(manifest).resolve()),evidence['manifest']['path']],'one_inference_run':False,'no_synthesis_performed':True,'original_native_manifest_and_cache_unchanged':True,'human_listening_approved':False}
 result={'schema':NATIVE,'phase':'complete','job':job,'parameters':copy.deepcopy(base['parameters']),'selected_utterances':registered['source_row_count'],'pending_unique_keys':0,'utterances':entries,'composition':composition,'quality_status':'engineering composition only; subjective listening remains required','timings_not_combined_or_inherited':True}
 result['composition_identity_sha256']=key(result);output=Path(output).resolve();check(output.suffix=='.json' and output not in (Path(manifest).resolve(),folder/'segments.json',folder/'speakers.json',folder/'native_audio_selections.json',Path(evidence['manifest']['path'])),'Refuse to overwrite an original input')
 save(output,result)
 audit={'schema':'actual_native_composition_source_runs_v1','resolved_manifest':binding(output),'base_actual_manifest':binding(manifest),'base_actual_run_metrics':{name:base.get(name) for name in ('timings','model_loaded','initial_cache_hit_unique_keys','generated_unique_keys','run_wall_seconds')},'child_actual_manifest':evidence['manifest'],'child_actual_run_metrics':{name:child.get(name) for name in ('timings','model_loaded','initial_cache_hit_unique_keys','generated_unique_keys','run_wall_seconds')},'interpretation':'Two source manifests contain reused/generated assets from their recorded runs. This resolver performed no inference; timings are not a single complete synthesis measurement.'}
 save(output.with_suffix('.composition_audit.json'),audit)
 return {'path':str(output),'resolved':True,'segments':registered['source_row_count'],'selected_segment_ids':[SID],'composition_identity_sha256':result['composition_identity_sha256'],'audit':str(output.with_suffix('.composition_audit.json'))}

def resolve(root,job,manifest,output):
 folder=Path(root).resolve()/'jobs'/job;receipt=folder/'native_audio_selections.json'
 if not receipt.exists():return {'path':str(Path(manifest).resolve()),'resolved':False,'reason':'No explicit registered audio receipt'}
 return compose(root,job,manifest,output,read(receipt))

def main():
 parser=argparse.ArgumentParser();commands=parser.add_subparsers(dest='command',required=True)
 for name in ('prepare-receipt','resolve'):
  sub=commands.add_parser(name);sub.add_argument('--root',type=Path,required=True);sub.add_argument('--job',required=True);sub.add_argument('--manifest',type=Path,required=True);sub.add_argument('--output',type=Path,required=True)
  if name=='prepare-receipt':
   sub.add_argument('--small-proof',type=Path,required=True);sub.add_argument('--turbo-proof',type=Path,required=True);sub.add_argument('--small-observation',type=Path,help='Original observation bytes relocated into R; report bytes/SHA stay unchanged')
 args=parser.parse_args()
 if args.command=='prepare-receipt':
  receipt=prepare_receipt(args.root,args.job,args.manifest,args.small_proof,args.turbo_proof,args.output,args.small_observation);result={'path':str(args.output.resolve()),'schema':receipt['schema'],'selected_segment_ids':receipt['selected_segment_ids']}
 else:result=resolve(args.root,args.job,args.manifest,args.output)
 print(json.dumps(result,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
