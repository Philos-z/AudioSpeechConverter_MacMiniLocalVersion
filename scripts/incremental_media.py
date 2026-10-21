"""Read-only project audit and incremental dubbing dependency planner.

No model inference, downloads, deletion, or media regeneration. Writes only the
explicit report/manifest/timing destinations. Planning hashes are separate from
vendor TTS cache keys; a runner must consume the returned regenerate IDs.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

SCHEMA='dubbing_incremental_v1'
FFPROBE='/opt/homebrew/opt/ffmpeg-full/bin/ffprobe'


def read(path):return json.loads(Path(path).read_text())
def write(path,value):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    tmp=p.with_suffix(p.suffix+'.partial');tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');tmp.replace(p)
def fingerprint(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def hash_file(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda:handle.read(1024*1024),b''):h.update(block)
    return h.hexdigest()
def file_record(path):
    p=Path(path).resolve();s=p.stat()
    return {'path':str(p),'bytes':s.st_size,'mtime_ns':s.st_mtime_ns,'sha256':hash_file(p)}
def compare_file(record,strict=False):
    p=Path(record['path'])
    if not p.is_file():return False,'missing'
    st=p.stat()
    if not strict and st.st_size==record['bytes'] and st.st_mtime_ns==record['mtime_ns']:
        return True,'unchanged_stat (local fast mode)'
    return hash_file(p)==record['sha256'],'sha256'
def profile_for(root,job,override=None):
    c=read(root/'config/local.json');p=read(job/'progress.json').get('profile',{})
    result={'engine':p.get('engine','IndexTTS-2.5'),'revision':p.get('revision'),
        'calm_strength':c.get('tts_calm_strength',.35),'emotion_alpha':c.get('tts_emotion_alpha',1.),
        'num_beams':c.get('tts_num_beams',1),'duration_factor':1.,'seed':p.get('seed',42),
        'punctuation_policy':'verified quiet boundaries: comma/colon .18s, sentence .35s',
        'speaker_overrides':{}}
    if override:result.update(override)
    return result

def desired_inputs(root,job,segments_path=None,profile_override=None):
    rows=read(segments_path or job/'segments.json')['segments']
    speakers=read(job/'speakers.json');profile=profile_for(root,job,profile_override)
    references={key:file_record(root/value['reference']) for key,value in speakers.items() if value.get('reference')}
    entries={}
    for row in rows:
        speaker=row['speaker_id'];effective={k:v for k,v in profile.items() if k!='speaker_overrides'}
        effective.update(profile.get('speaker_overrides',{}).get(speaker,{}))
        audio_input={'text_zh':row['text_zh'],'tts_pronunciation_text':row.get('tts_pronunciation_text'),
            'speaker_id':speaker,'reference_sha256':references[speaker]['sha256'],'profile':effective}
        entries[row['segment_id']]={'row':dict(row),'speaker_id':speaker,'audio_input':audio_input,
            'audio_input_fingerprint':fingerprint(audio_input),'text_de':row.get('text_de',''),
            'source_start_s':row['start_s'],'source_end_s':row['end_s']}
    return entries,profile,references

def picture_signature(source,clip):
    return hashlib.sha256(json.dumps([str(Path(source)),clip['source_start_s'],clip['frames'],
        clip['source_duration_s'],clip['freeze_s'],960,540],sort_keys=True).encode()).hexdigest()[:12]

def picture_cache(edl,cache,probe=False):
    rows=[];used=set();started=time.perf_counter()
    for clip in edl['clips']:
        sig=picture_signature(edl['source_input'],clip);candidates=[]
        for file in cache.glob(f'clip_*_{sig}.mp4'):
            if file.with_suffix('.done').is_file():candidates.append(file)
        entry={'signature':sig,'frames_expected':clip['frames'],'hit':bool(candidates)}
        if candidates:
            file=candidates[0];used.add(file.resolve());entry.update(path=str(file.resolve()),bytes=file.stat().st_size)
            if probe:
                r=subprocess.run([FFPROBE,'-v','error','-select_streams','v:0','-show_entries','stream=width,height,nb_frames,avg_frame_rate,duration','-of','json',str(file)],capture_output=True,text=True,check=True)
                stream=json.loads(r.stdout)['streams'][0]
                valid=(int(stream['nb_frames'])==clip['frames'] and stream['width']==960 and stream['height']==540
                       and stream['avg_frame_rate']=='25/1' and abs(float(stream['duration'])-clip['frames']/25)<.001)
                entry.update(probe_valid=valid,hit=valid)
        rows.append(entry)
    unused=[p for p in cache.glob('clip_*.mp4') if p.resolve() not in used]
    return {'total':len(rows),'hits':sum(r['hit'] for r in rows),'rows':rows,'actual_ffprobe':probe,
        'audit_elapsed_s':time.perf_counter()-started,'unused_candidate_files':len(unused),
        'unused_candidate_bytes':sum(p.stat().st_size for p in unused),'deletion_performed':False}

def snapshot(root,job,output,profile_override=None):
    began=time.perf_counter();out=job/'outputs/landscape_compact'
    entries,profile,refs=desired_inputs(root,job,profile_override=profile_override)
    timeline=read(job/'timeline.punctuated.json');final={r['segment_id']:r for r in timeline['segments']}
    mix=read(out/'mix_report.json');verification=read(out/'verification.json')
    if not verification['status'].startswith('engineering_checks_passed'):raise ValueError('Verified final baseline required')
    gains={p['id']:p for p in mix['placements']};measurements={}
    for identifier,entry in entries.items():
        row=final[identifier]
        if row['text_zh']!=entry['audio_input']['text_zh']:raise ValueError('Final audio text differs from current segments: '+identifier)
        record=file_record(row['audio'])
        if row.get('audio_sha256') and row['audio_sha256']!=record['sha256']:raise ValueError('Final audio SHA mismatch')
        entry['audio_artifact']=record;entry['duration_s']=row['duration_s']
        p=gains[identifier]
        if Path(p['raw_audio']).resolve()!=Path(row['audio']).resolve():raise ValueError('Gain measurement source differs')
        measurements[record['sha256']]={'source_sha256':record['sha256'],'gain_db':p['gain_db'],
            'measurement_policy':{'target_integrated_lufs':-19,'max_true_peak_dbtp':-3,'lra_lu':11,
                'algorithm':'ffmpeg_loudnorm_input_measurement_then_min_fixed_gain_v1'},
            'origin':'existing verified mix_report.json; not remeasured','target_sample_rate':48000,
            'target_channels':2,'fade_s':.002}
    edl=read(out/'edit_decisions.json')
    export=read(out/'compact_export.json')
    club=export.get('logo',{}).get('brand')=='Philos.club'
    serif=export.get('logo',{}).get('style')=='serif_box_v2'
    assets={key:file_record(path) for key,path in {'source_video':edl['source_input'],
        'independent_background':mix['background_source'],
        'logo':root/('assets/philos_club_serif_wordmark.png' if serif else
                    'assets/philos_club_wordmark.png' if club else 'assets/philos_haircut_logo.png')}.items()}
    visual={'width':960,'height':540,'fps':25,'zh_font_size':34,'de_font_size':24,
        'logo_width':144 if club else 84,'logo_opacity':.55,'video_bitrate':850000,'aac_bitrate':128000,
        'join_groups':edl['join_groups'],'pause_target_s':edl['pause_target_s']}
    result={'schema':SCHEMA,'manifest_kind':'snapshot_of_verified_delivery_not_backend_content_cache',
        'verification_record':file_record(out/'verification.json'),'root':str(root),'job':str(job),'created_at_unix':time.time(),
        'entries':entries,'profile':profile,'references':refs,'assets':assets,'visual':visual,
        'edl':edl,'verified_output_video_sha256':verification.get('video_sha256'),
        'gain_measurements':measurements,'picture_cache':picture_cache(edl,out/'render_cache'),
        'timing':{'snapshot_elapsed_s':time.perf_counter()-began},
        'limits':['Planning does not generate speech or assert subjective quality.',
                  'A changed Chinese duration requires rebuilding EDL before exact picture-cache selection.',
                  'Stat fast mode is for local nonadversarial editing; strict audit hashes bytes.']}
    write(output,result)
    write(Path(output).with_name('verified_gain_cache.json'),{'schema':'verified_loudness_gain_v1','entries':measurements})
    return result

def plan(manifest,segments_path=None,profile_override=None,strict=False):
    began=time.perf_counter();old=read(manifest);root=Path(old['root']);job=Path(old['job'])
    current,profile,refs=desired_inputs(root,job,segments_path,profile_override)
    source_bytes_ok,_=compare_file(old['assets']['source_video'],strict)
    current_source=Path(read(job/'source.json')['input']).expanduser().resolve()
    snapshot_source=Path(old['assets']['source_video']['path']).resolve()
    source_pointer_ok=current_source==snapshot_source
    source_ok=source_bytes_ok and source_pointer_ok
    background_ok,_=compare_file(old['assets']['independent_background'],strict)
    logo_ok,_=compare_file(old['assets']['logo'],strict)
    regenerate=[];reuse=[];reasons={};gains_reused=0
    for identifier,entry in current.items():
        previous=old['entries'].get(identifier);reason=[]
        if not previous:reason.append('new_segment')
        elif previous['audio_input_fingerprint']!=entry['audio_input_fingerprint']:reason.append('text_voice_reference_or_profile_changed')
        if previous:
            valid,_=compare_file(previous['audio_artifact'],strict)
            if not valid:reason.append('audio_cache_missing_or_changed')
        if not source_ok:reason.append('source_video_changed_requires_upstream_revalidation')
        if reason:regenerate.append(identifier);reasons[identifier]=reason
        else:
            reuse.append(identifier)
            gains_reused+=previous['audio_artifact']['sha256'] in old['gain_measurements']
    removed=sorted(set(old['entries'])-set(current))
    de_changed=[i for i,r in current.items() if i not in old['entries'] or old['entries'][i]['text_de']!=r['text_de']]
    boundaries_changed=any(i not in old['entries'] or any(current[i][k]!=old['entries'][i][k] for k in ['source_start_s','source_end_s']) for i in current)
    audio_changed=bool(regenerate or removed);timing_changed=audio_changed or boundaries_changed
    captions_changed=bool(audio_changed or removed or de_changed or boundaries_changed)
    video_burn=timing_changed or captions_changed or not logo_ok
    audio_mix=audio_changed or not background_ok or boundaries_changed
    stages={'source_download_asr_separation':'reuse' if source_ok else 'revalidate source before automatic reuse',
        'tts':{'action':'generate_changed_only' if regenerate else 'skip','ids':regenerate},
        'loudness_measurement':{'reuse_verified':gains_reused,'measure_only_regenerated':len(regenerate)},
        'edl':'rebuild' if timing_changed else 'reuse','background_asset':'reuse' if background_ok else 'changed',
        'background_retiming':'rebuild' if timing_changed or not background_ok else 'reuse',
        'ass_subtitles':'rebuild' if captions_changed else 'reuse',
        'mix_and_master':'rebuild' if audio_mix else 'reuse',
        'source_picture_clips':'resolve exact signatures after new EDL' if timing_changed else 'reuse all verified cache hits',
        'final_video':'burn subtitles/logo' if video_burn else ('copy existing H264 video and replace audio' if audio_mix else 'reuse verified final MP4'),
        'verification':('reuse exact bound verification receipt; no rewritten output' if not video_burn and not audio_mix else
            ('verify new audio/levels/mux; compare copied H264 bitstream against prior verified output' if not video_burn else
             'verify newly encoded video, changed audio and final assembled deliverable'))}
    return {'schema':SCHEMA,'regenerate_audio_ids':regenerate,'reuse_audio_ids':reuse,'removed_ids':removed,
        'regeneration_reasons':reasons,'segments':len(current),'regenerate_count':len(regenerate),'reuse_count':len(reuse),
        'audio_cache_reuse_fraction':len(reuse)/max(1,len(current)),
        'source_binding':{'current_input':str(current_source),'snapshot_input':str(snapshot_source),
                          'pointer_matches':source_pointer_ok,'bytes_match_snapshot':source_bytes_ok},
        'regenerate_by_speaker':dict(Counter(current[i]['speaker_id'] for i in regenerate)),
        'stages':stages,'elapsed_s':time.perf_counter()-began,
        'execution_blocked':not source_ok,
        'blockers':[] if source_ok else ['Source video changed: revalidate transcript, separation, references and rights before generation.'],
        'pending_rows':[{**current[i]['row'],'audio_input_fingerprint':current[i]['audio_input_fingerprint'],
            'effective_tts_inputs':current[i]['audio_input'],'regeneration_reasons':reasons[i],
            'cache_state':'pending_generation'} for i in regenerate],
        'reuse_rows':[{**current[i]['row'],'audio':old['entries'][i]['audio_artifact']['path'],
            'audio_sha256':old['entries'][i]['audio_artifact']['sha256'],'duration_s':old['entries'][i]['duration_s'],
            'audio_input_fingerprint':current[i]['audio_input_fingerprint'],
            'cache_state':'verified_existing_artifact','verification_receipt':old.get('verification_record')} for i in reuse],
        'runtime_claim':'Counts and dependencies only. No end-to-end 80% time reduction has been measured by this planner.'}

def audit(manifest,probe=False):
    old=read(manifest);began=time.perf_counter();bad=[]
    for identifier,row in old['entries'].items():
        valid,method=compare_file(row['audio_artifact'],True)
        if not valid:bad.append(identifier)
    assets={k:compare_file(v,True)[0] for k,v in old['assets'].items()}
    cache=picture_cache(old['edl'],Path(old['job'])/'outputs/landscape_compact/render_cache',probe)
    pipeline=read(Path(old['job'])/'compact_pipeline_progress.json')
    render=read(Path(old['job'])/'outputs/landscape_compact/render_cache/render_progress.json')
    tts_runs=[]
    for name in ['comma_chunk_progress.initial73.json','comma_chunk_progress.json']:
        path=Path(old['job'])/name
        if path.exists():
            ledger=read(path)
            if isinstance(ledger,dict) and ledger.get('finished_at_unix') and ledger.get('started_at_unix'):
                tts_runs.append({'ledger':name,'rows':len(ledger.get('results',{})),
                    'wall_s':ledger['finished_at_unix']-ledger['started_at_unix']})
    return {'audio_checked':len(old['entries']),'bad_audio_ids':bad,'assets_sha256_match':assets,
        'picture_cache':cache,'speaker_counts':dict(Counter(r['speaker_id'] for r in old['entries'].values())),
        'historical_timings':{'final_pipeline_wall_s':pipeline['finished_at_unix']-pipeline['started_at_unix'],
            'final_render_wall_s':render['updated_at_unix']-render['started_at_unix'],
            'individual_stage_timings_available':False,'observed_legacy_tts_runs':tts_runs,
            'baseline_caution':'The historical TTS workload is 73 fallback sentences plus 5 revisions, not a new single-pass 135-sentence benchmark.',
            'note':'Only explicit historical start/end timestamps are used; missing stage timings are not invented.'},
        'gain_cache_entries':len(old['gain_measurements']),'audit_elapsed_s':time.perf_counter()-began,
        'deletion_performed':False}

def timed(stage,log,argv):
    if argv and argv[0]=='--':argv=argv[1:]
    if not argv:raise ValueError('Timed stage needs a command after --')
    path=Path(log);path.parent.mkdir(parents=True,exist_ok=True)
    start=time.time();clock=time.perf_counter()
    # Never log arguments: a future provider command may contain a credential.
    with path.open('a') as f:f.write(json.dumps({'event':'start','stage':stage,'at_unix':start,'executable':Path(argv[0]).name})+'\n')
    result=subprocess.run(argv)
    row={'event':'end','stage':stage,'started_at_unix':start,'finished_at_unix':time.time(),
         'elapsed_s':time.perf_counter()-clock,'returncode':result.returncode}
    with path.open('a') as f:f.write(json.dumps(row)+'\n')
    return result.returncode

def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='action',required=True)
    s=sub.add_parser('snapshot');s.add_argument('--root',type=Path,required=True);s.add_argument('--job',required=True);s.add_argument('--output',type=Path,required=True)
    a=sub.add_parser('plan');a.add_argument('--manifest',type=Path,required=True);a.add_argument('--segments',type=Path);a.add_argument('--profile',type=Path);a.add_argument('--strict',action='store_true');a.add_argument('--output',type=Path,required=True)
    a=sub.add_parser('audit');a.add_argument('--manifest',type=Path,required=True);a.add_argument('--probe',action='store_true');a.add_argument('--output',type=Path,required=True)
    a=sub.add_parser('timed');a.add_argument('--stage',required=True);a.add_argument('--log',type=Path,required=True);a.add_argument('command',nargs=argparse.REMAINDER)
    args=p.parse_args()
    if args.action=='timed':raise SystemExit(timed(args.stage,args.log,args.command))
    if args.action=='snapshot':r=snapshot(args.root.resolve(),args.root.resolve()/'jobs'/args.job,args.output)
    elif args.action=='plan':r=plan(args.manifest,args.segments,read(args.profile) if args.profile else None,args.strict);write(args.output,r)
    else:r=audit(args.manifest,args.probe);write(args.output,r)
    print(json.dumps({'action':args.action,'output':str(args.output.resolve()),'summary':{k:r[k] for k in ['regenerate_count','reuse_count','audio_checked','bad_audio_ids','gain_cache_entries','audit_elapsed_s'] if k in r}},ensure_ascii=False),flush=True)

if __name__=='__main__':main()
