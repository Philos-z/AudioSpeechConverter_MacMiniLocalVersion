"""Isolated native-manifest -> natural EDL -> existing 540p export adapter.

No model import or inference. Explicit prepare/mix/render/verify phases only.
Original jobs, cached WAVs, source media and delivered videos are read-only.
"""
import argparse
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace


def read(path):return json.loads(Path(path).read_text())
def save(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(path.suffix+'.partial')
    temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n');temporary.replace(path)
def digest(path):
    result=hashlib.sha256()
    with Path(path).open('rb') as source:
        for part in iter(lambda:source.read(1024*1024),b''):result.update(part)
    return result.hexdigest()
def fingerprint(data):return hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
def module(path,name):
    spec=importlib.util.spec_from_file_location(name,path);loaded=importlib.util.module_from_spec(spec)
    sys.modules[name]=loaded;spec.loader.exec_module(loaded);return loaded


def inspect_wav(path,expected_sha=None):
    import soundfile as sf
    path=Path(path).resolve();info=sf.info(path);actual=digest(path)
    if expected_sha is None or actual!=expected_sha:raise ValueError('Missing/mismatched WAV receipt: '+str(path))
    if info.frames<1 or info.channels!=1 or info.samplerate<8000:raise ValueError('Invalid mono dialogue WAV')
    return {'audio':str(path),'audio_sha256':actual,'duration_s':info.frames/info.samplerate,
            'audio_frames':info.frames,'audio_sample_rate':info.samplerate}


def verify_assets(job,receipt):
    source=read(job/'source.json');assets=receipt['assets']
    if Path(source['input']).resolve()!=Path(assets['source_video']['path']).resolve():
        raise ValueError('Current source.json input pointer differs from verified source receipt')
    for key in ('source_video','independent_background','logo'):
        item=assets[key];path=Path(item['path'])
        if path.stat().st_size!=item['bytes'] or digest(path)!=item['sha256']:
            raise ValueError('Verified source/background/logo changed: '+key)
    video=next(stream for stream in source['probe']['streams'] if stream['codec_type']=='video')
    if abs(video['width']/video['height']-16/9)>.01:
        raise ValueError('This compact adapter is limited to the current 16:9 source; preserve other source geometry separately')
    return source


def native_rows(manifests):
    result={}
    for path in manifests:
        manifest=read(path)
        if (manifest.get('schema')!='native_mlx_whole_utterance_v1' or manifest.get('phase')!='complete'
                or manifest.get('pending_unique_keys')!=0):raise ValueError('Native manifest is not complete: '+str(path))
        for row in manifest['utterances']:
            if row['status'] not in ('cached','generated') or fingerprint(row['content'])!=row['cache_key']:
                raise ValueError('Unverified native content key: '+row['segment_id'])
            payload=row['content'];sid=row['segment_id']
            item={**row,**inspect_wav(row['audio'],row.get('sha256')),
                  'manifest':str(Path(path).resolve()),'manifest_sha256':digest(path)}
            if sid in result and result[sid]['cache_key']!=row['cache_key']:
                raise ValueError('Conflicting native manifests for '+sid)
            if abs(item['duration_s']-row['duration_s'])>1/item['audio_sample_rate']:
                raise ValueError('Stale native WAV duration: '+sid)
            result[sid]=item
    return result


def build_natural_edl(rows,source,timeline_path,groups,output_path=None):
    """Retain every WAV sample, no inserted row/word gaps, >=.35s group tail."""
    by_id={row['segment_id']:i for i,row in enumerate(rows)};starts={};members=set()
    for ids in groups:
        if not all(sid in by_id for sid in ids):continue
        positions=[by_id[sid] for sid in ids]
        if positions!=list(range(positions[0],positions[0]+len(ids))) or members.intersection(ids):
            raise ValueError('Semantic groups must be adjacent, ordered, and disjoint')
        starts[ids[0]]=len(ids);members.update(ids)
    cursor=0;clips=[];utterances=[];issues=[];switches=[]
    first=math.floor(rows[0]['start_s']*25+1e-8)
    if first:
        clips.append({'kind':'intro','source_start_s':0.,'source_end_s':first/25,'source_duration_s':first/25,
            'freeze_s':0.,'output_start_s':0.,'output_end_s':first/25,'frames':first});cursor=first
    i=0
    while i<len(rows):
        group=rows[i:i+starts.get(rows[i]['segment_id'],1)];speech=sum(r['duration_s'] for r in group)
        frames=math.ceil((speech+.35)*25-1e-8);length=frames/25;start=group[0]['start_s']
        after=i+len(group);boundary=rows[after]['start_s'] if after<len(rows) else source['duration_s']
        available=max(0.,boundary-start);taken=min(length,available);freeze=max(0.,length-taken)
        if speech-available>.35+1e-6:
            issues.append({'ids':[r['segment_id'] for r in group],'reason':'complete_audio_exceeds_source_window',
                'speech_s':speech,'available_source_s':available,'excess_s':speech-available,
                'action':'review shorter faithful wording or a justified same-speaker meaning group; never cut audio'})
        clip={'kind':'speech','segment_ids':[r['segment_id'] for r in group],'source_start_s':start,
            'source_end_s':start+taken,'source_duration_s':taken,'freeze_s':freeze,
            'output_start_s':cursor/25,'output_end_s':(cursor+frames)/25,'frames':frames,'speech_s':speech,
            'speech_audio_s':speech,'internal_comma_gaps_s':0.,'internal_punctuation_gaps_s':0.,
            'pause_s':length-speech,'joined':len(group)>1}
        clips.append(clip);offset=cursor/25;previous=None
        for index,row in enumerate(group):
            import re
            match=re.search(r'''([，,：:；;。.!！？?])[”’"'）)\]】》」』\s]*$''',row['text_zh'].rstrip()) if index+1<len(group) else None
            punctuation=match.group(1) if match else ''
            updated={**row,'speech_s':row['duration_s'],'output_start_s':offset,
                'output_speech_end_s':offset+row['duration_s'],'output_end_s':offset+row['duration_s'],
                'punctuation_gap_after_s':0.,'boundary_pause_target_s':0.,'boundary_punctuation':punctuation,
                'comma_gap_after_s':0.,'clip_index':len(clips)-1}
            if previous and previous['speaker_id']!=row['speaker_id']:
                switches.append({'id':row['segment_id'],'speaker_id':row['speaker_id'],
                    'original_start_s':row['start_s'],'picture_time_at_audio_start_s':start+offset-cursor/25,
                    'drift_s':start+offset-cursor/25-row['start_s'],'requires_visual_review':True})
            utterances.append(updated);offset+=row['duration_s'];previous=row
        utterances[-1]['output_end_s']=(cursor+frames)/25
        cursor+=frames;i=after
    result={'format':'landscape_compact_edit_v1','source_input':source['input'],
        'source_duration_s':source['duration_s'],'source_timeline':str(Path(timeline_path).resolve()),
        'fps':25,'width':960,'height':540,'source_picture_box':[0,0,960,540],'pause_target_s':.35,
        'duration_s':cursor/25,'frames':cursor,'speech_segments':len(rows),'clips':clips,'utterances':utterances,
        'issues':issues,'join_groups':groups,'freeze_repairs':[c for c in clips if c['freeze_s']],
        'row_boundary_policy':'natural_wav_contiguous_no_inserted_silence','inserted_internal_silence_s':0.,
        'speaker_switch_drift':switches,
        'timing_method':'All full WAV samples preserved in original order; no new row/punctuation gap; >=.35s at complete-group end',
        'background_policy':'continuous full bed; short contextual crossfades at source cuts; natural tail loop on freeze, never zero-pad',
        'limitations':['Existing WAV head/tail silence and breaths are preserved; no subjective quality claim.',
            'Separate row WAVs do not prove one continuous performance across a comma; same-speaker meaning-group TTS remains a possible next step.']}
    if output_path:save(output_path,result)
    return result


def same_speaker_groups(rows,groups):
    """A quoted meaning group may include narration; keep visual speaker cuts."""
    lookup={row['segment_id']:row for row in rows};result=[]
    for ids in groups:
        if not all(sid in lookup for sid in ids):continue
        current=[];speaker=None
        for sid in ids:
            if current and lookup[sid]['speaker_id']!=speaker:
                if len(current)>1:result.append(current)
                current=[]
            current.append(sid);speaker=lookup[sid]['speaker_id']
        if len(current)>1:result.append(current)
    return result


def prepare(args):
    root=args.root.resolve();job=root/'jobs'/args.job;out=args.output.resolve()
    if out==job or (out.is_relative_to(job) and not getattr(args,'allow_job_output',False)):
        raise ValueError('Use an isolated output, or explicitly allow a new empty output inside the job')
    if out.exists() and any(out.iterdir()):raise FileExistsError('New adapter output must be empty')
    receipt=read(args.receipt);source=verify_assets(job,receipt)
    reviewed=read(job/'segments.json')['segments'];fresh=native_rows(args.manifest or [])
    baseline={r['segment_id']:r for r in read(args.baseline_timeline)['segments']} if args.baseline_timeline else {}
    rows=[];counts={'native':0,'legacy_receipt_reuse':0};pending=[]
    for row in reviewed:
        sid=row['segment_id']
        if not row.get('translation_reviewed') or not row.get('speaker_reviewed'):raise ValueError('Unreviewed row '+sid)
        chosen=fresh.get(sid)
        if chosen:
            if (chosen['speaker_id']!=row['speaker_id'] or chosen['content']['text_zh']!=row['text_zh']
                    or chosen['content']['effective_tts_text']!=(row.get('tts_pronunciation_text') or row['text_zh'])
                    or abs(chosen['source_start_s']-row['start_s'])>1e-7
                    or abs(chosen['source_end_s']-row['end_s'])>1e-7):raise ValueError('Native/current text, voice or source interval mismatch: '+sid)
            audio={k:chosen[k] for k in ('audio','audio_sha256','duration_s','audio_frames','audio_sample_rate')}
            provenance={'kind':'native_manifest','manifest':chosen['manifest'],'manifest_sha256':chosen['manifest_sha256'],
                'cache_key':chosen['cache_key'],'experimental_profile':chosen['content']['parameters'].get('experimental_profile'),
                'listening_approved':False}
            counts['native']+=1
        elif sid in baseline:
            old=baseline[sid]
            if any(old[k]!=row[k] for k in ('speaker_id','start_s','end_s','text_de','text_zh')):
                raise ValueError('Changed row cannot reuse baseline: '+sid)
            audio=inspect_wav(old['audio'],old.get('audio_sha256'))
            provenance={'kind':'legacy_verified_asset_receipt','timeline':str(args.baseline_timeline.resolve()),
                'legacy_method':old.get('punctuation_method'),'baked_legacy_pauses_may_remain':True,'listening_approved':False}
            counts['legacy_receipt_reuse']+=1
        else:pending.append(sid);continue
        rows.append({**row,**audio,'audio_provenance':provenance,'render_start_s':row['start_s'],
                     'render_end_s':row['start_s']+audio['duration_s']})
    if pending:raise ValueError('Missing complete audio rows; generate only these pending IDs: '+','.join(pending))
    if set(fresh)-{r['segment_id'] for r in reviewed}:raise ValueError('Native manifest contains unknown IDs')
    out.mkdir(parents=True,exist_ok=True);save(out/'source.json',source)
    timeline=out/'timeline.native.json';save(timeline,{'segments':rows,'issues':[],'method':'Verified full WAVs; no waveform/punctuation processing'})
    visual=module(root/'scripts/compact_edit.py','_natural_adapter_compact')
    requested_groups=read(args.join_groups) if args.join_groups else receipt['visual']['join_groups']
    groups=same_speaker_groups(rows,requested_groups)
    edl=build_natural_edl(rows,source,timeline,groups,out/'edit_decisions.json')
    holds=module(Path(__file__).with_name('picture_holds.py'),'_natural_picture_hold_guard')
    edl=holds.apply(job,edl);save(out/'edit_decisions.json',edl)
    page_path=getattr(args,'subtitle_pages',None) or job/'subtitle_pages.json'
    if Path(page_path).is_file():
        pager=module(Path(__file__).with_name('subtitle_pages.py'),'_natural_subtitle_pages')
        pager.attach_pages(edl,page_path);save(out/'edit_decisions.json',edl)
        layout=pager.build_ass(edl,out/'compact_bilingual.ass',visual)
    else:
        if getattr(args,'subtitle_pages',None):raise FileNotFoundError('Explicit subtitle page plan is missing')
        layout=visual.build_compact_ass(edl,out/'compact_bilingual.ass')
    save(out/'subtitle_layout.json',layout)
    old_signatures={r['signature'] for r in receipt['picture_cache']['rows'] if r.get('hit')}
    signatures=[hashlib.sha256(json.dumps([str(Path(edl['source_input'])),c['source_start_s'],c['frames'],c['source_duration_s'],c['freeze_s'],960,540],sort_keys=True).encode()).hexdigest()[:12] for c in edl['clips']]
    report={'status':'prepared_candidate_requires_timing_and_listening_review','counts':counts,'segments':len(rows),
        'duration_s':edl['duration_s'],'issues':edl['issues'],'speaker_switch_drift':edl['speaker_switch_drift'],
        'picture_hold_review':edl.get('picture_hold_review'),'resolved_timing_issues':edl.get('resolved_timing_issues',[]),
        'source_job':str(job),'asset_receipt':str(args.receipt.resolve()),'asset_receipt_sha256':digest(args.receipt),
        'source_video':receipt['assets']['source_video'],
        'background':receipt['assets']['independent_background'],'logo':receipt['assets']['logo'],
        'picture_cache_candidate_matches':sum(s in old_signatures for s in signatures),'picture_clips':len(signatures),
        'existing_job_or_deliveries_modified':False,'source_audio_modified':False,'models_started':False,
        'new_fixed_silence_inserted_s':0.,'full_native_backend_conversion':counts['native']==len(rows),
        'semantic_group_policy':'retain adjacent same-speaker groups; split at every speaker change',
        'requested_join_groups':requested_groups,'effective_join_groups':groups,
        'listening_approved':False,'legacy_pause_artifacts_removed':False}
    save(out/'adapter_report.json',report);return report


def _join_without_zero(left,right,width):
    """Fixed-duration contextual blend; preserve time, never fade both sides to zero."""
    import numpy as np
    n=min(width,len(left),len(right))
    if n<1:return
    tail=left[-n:].copy();head=right[:n].copy()
    # Reflected context makes both sides continuous at the central boundary.
    outgoing=np.concatenate((tail,tail[::-1]));incoming=np.concatenate((head[::-1],head))
    phase=np.linspace(0,1,2*n,dtype='float32')[:,None]
    bridge=outgoing*(1-phase)+incoming*phase
    left[-n:]=bridge[:n];right[:n]=bridge[n:]


def retime_background(edl,background):
    import numpy as np
    import soundfile as sf
    sr=48000;frames=edl['frames']*1920;bed=np.empty((frames,2),dtype='float32');cursor=0;repairs=[]
    with sf.SoundFile(background) as source:
        if source.samplerate!=sr or source.channels!=2:raise ValueError('Require original independent 48k stereo background')
        for clip_index,clip in enumerate(edl['clips']):
            count=clip['frames']*1920;start=round(clip['source_start_s']*sr);length=round(clip['source_duration_s']*sr)
            source.seek(start);block=source.read(length,dtype='float32',always_2d=True)
            if len(block)!=length or not len(block) or not np.isfinite(block).all():raise ValueError('Incomplete background source')
            if len(block)>count:
                if len(block)-count>1:raise ValueError('Background clip duration differs from EDL')
                block=block[:count] # Only background sample-clock rounding, never dialogue.
            if len(block)<count:
                missing=count-len(block);tail=block[-min(len(block),24000):]
                extension=np.tile(tail,(math.ceil(missing/len(tail)),1))[:missing].copy()
                for boundary in range(len(tail),len(extension),len(tail)):
                    _join_without_zero(extension[:boundary],extension[boundary:],480)
                _join_without_zero(block,extension,480)
                block=np.concatenate((block,extension));repairs.append({'clip':clip_index,'extension_frames':missing,'method':'natural_tail_context_loop_with_10ms_reflected_blend'})
            bed[cursor:cursor+count]=block
            if cursor:_join_without_zero(bed[:cursor],bed[cursor:cursor+count],480)
            cursor+=count
    if cursor!=frames:raise ValueError('Background timeline has a hole')
    return bed,{'frames':frames,'forced_zero_padding_frames':0,'crossfade_ms':10.,'freeze_context_extensions':repairs,
                'source_silence_preserved':True,'listening_review_required':True}


def mix(args):
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly
    GainCache=module(args.root/'scripts/gain_cache.py','_natural_adapter_gain_cache').GainCache
    out=args.output.resolve();edl=read(out/'edit_decisions.json');report=read(out/'adapter_report.json')
    policy=getattr(args,'mastering_policy','legacy_loudnorm_v1')
    if policy not in ('legacy_loudnorm_v1','constant_if_safe_v1'):raise ValueError('Unknown mastering policy')
    if edl['issues']:raise ValueError('Resolve source-window issues first; no speech will be cut')
    if digest(report['background']['path'])!=report['background']['sha256']:raise ValueError('Background changed after prepare')
    sr=48000;frames=edl['frames']*1920;dialogue=np.zeros((frames,2),dtype='float32');cache=GainCache(args.root/'cache/verified_gain_cache.json')
    placements=[];began=time.perf_counter()
    for row in edl['utterances']:
        raw=Path(row['audio']);inspect_wav(raw,row['audio_sha256'])
        samples,rate=sf.read(raw,dtype='float32',always_2d=True)
        if len(samples)!=row['audio_frames'] or not np.isfinite(samples).all():raise ValueError('Changed or invalid dialogue source')
        if rate!=sr:
            common=math.gcd(sr,rate);samples=resample_poly(samples,sr//common,rate//common,axis=0).astype('float32')
        samples=np.repeat(samples,2,axis=1);gain,hit=cache.get(raw);samples*=10**(gain/20)
        start=round(row['output_start_s']*sr);end=start+len(samples)
        if end>frames or abs(len(samples)-round(row['speech_s']*sr))>1:raise ValueError('Resampled speech would be truncated or EDL stale')
        dialogue[start:end]+=samples # Preserve the full resampler output, with no edge fades/crops.
        placements.append({'id':row['segment_id'],'start_frame':start,'frames':len(samples),'gain_db':gain,**hit})
    bed,bed_audit=retime_background(edl,report['background']['path'])
    sf.write(out/'zh_dialogue.wav',dialogue,sr,subtype='PCM_24');sf.write(out/'background_compact.wav',bed,sr,subtype='PCM_24')
    dialogue+=bed;sf.write(out/'premix.wav',dialogue,sr,subtype='FLOAT');del bed,dialogue
    video=module(args.root/'scripts/video.py','_natural_adapter_video')
    ffmpeg='/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg'
    def call(argv):return video.call([ffmpeg,'-threads','2','-filter_threads','2',*argv])
    def legacy_master():
        measured=call(['-hide_banner','-i',out/'premix.wav','-af','loudnorm=I=-16:TP=-2:LRA=11:print_format=json','-f','null','-'])
        m=video.parse_loudness(measured.stderr)
        filt=f"loudnorm=I=-16:TP=-2:LRA=11:linear=true:measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}"
        call(['-v','error','-y','-i',out/'premix.wav','-af',filt,'-ar','48000','-c:a','pcm_s24le',out/'zh_mix.wav'])
        call(['-v','error','-y','-i',out/'zh_mix.wav','-c:a','aac','-b:a','128k',out/'zh_mix.m4a'])
        return {'requested_policy':policy,'actual_strategy':'legacy_loudnorm_v1',
                'measurement':m,'original_mastering_behavior_retained':True}
    if policy=='constant_if_safe_v1':
        mastering=module(Path(__file__).with_name('optional_constant_master.py'),'_natural_constant_master')
        mastering_receipt=mastering.master_optional(out/'premix.wav',out,legacy_master,legacy_code_path=Path(__file__))
    else:mastering_receipt=legacy_master()
    pager=module(Path(__file__).with_name('subtitle_pages.py'),'_natural_srt_pages')
    pager.write_srt(edl,out,video.stamp)
    result={'placements':placements,'background':bed_audit,'gain_cache':cache.stats,'wall_s':time.perf_counter()-began,
            'dialogue_cropped_or_edge_faded':False,'new_internal_silence_inserted_s':0.,'speech_ducking':False,
            'edl_sha256':digest(out/'edit_decisions.json'),'master_sha256':digest(out/'zh_mix.wav'),
            'mastering':mastering_receipt,
            'quality_status':'candidate_requires_listening_review'}
    cache.save(args.root/'cache/verified_gain_cache.json')
    save(out/'natural_mix_report.json',result);return result


def seed_picture_cache(root,out):
    """Reuse only same-source, same-cut, byte-size and header-verified clips.

    Symlinks and new markers are written only inside this isolated output.
    Source job cached files/markers are never renamed or rewritten.
    """
    out=Path(out);audit=read(out/'adapter_report.json');edl=read(out/'edit_decisions.json')
    if digest(audit['asset_receipt'])!=audit['asset_receipt_sha256']:raise ValueError('Asset receipt changed after prepare')
    receipt=read(audit['asset_receipt']);source=receipt['assets']['source_video']
    if digest(source['path'])!=source['sha256']:raise ValueError('Source video changed after prepare')
    old={r['signature']:r for r in receipt['picture_cache']['rows'] if r.get('hit')}
    cache=out/'render_cache';cache.mkdir(exist_ok=True);reused=[]
    for index,clip in enumerate(edl['clips']):
        signature=hashlib.sha256(json.dumps([str(Path(edl['source_input'])),clip['source_start_s'],clip['frames'],clip['source_duration_s'],clip['freeze_s'],960,540],sort_keys=True).encode()).hexdigest()[:12]
        candidate=old.get(signature)
        if not candidate:continue
        path=Path(candidate['path'])
        if not path.is_file() or path.stat().st_size!=candidate['bytes'] or not path.with_suffix('.done').is_file():continue
        checked=subprocess.run(['/opt/homebrew/opt/ffmpeg-full/bin/ffprobe','-v','error','-show_streams','-of','json',str(path)],capture_output=True,text=True)
        if checked.returncode:continue
        stream=next((s for s in json.loads(checked.stdout)['streams'] if s['codec_type']=='video'),{})
        if (stream.get('width'),stream.get('height'),stream.get('avg_frame_rate'),str(stream.get('nb_frames'))) != (960,540,'25/1',str(clip['frames'])):continue
        target=cache/f'clip_{index:04}_{signature}.mp4'
        if not target.exists():target.symlink_to(path.resolve())
        target.with_suffix('.done').write_text('same-source receipt and stream geometry verified\n')
        reused.append({'index':index,'signature':signature,'source':str(path),'target':str(target)})
    result={'reused':len(reused),'clips':len(edl['clips']),'entries':reused,'source_cache_modified':False}
    save(out/'picture_cache_reuse.json',result);return result



def final_mux_frame_clock(argv):
    """Rebuild only final burn/mux picture timestamps from unchanged frame order."""
    command=list(argv)
    if '-filter_complex' not in command:return command
    if command.count('-filter_complex')!=1:raise ValueError('Unexpected final mux filter count')
    index=command.index('-filter_complex')+1;filters=command[index]
    if not isinstance(filters,str) or filters.count('[0:v:0]ass=')!=1:
        raise ValueError('Unexpected final mux picture filter; refuse timestamp rewrite')
    command[index]=filters.replace('[0:v:0]ass=','[0:v:0]setpts=N/(25*TB),ass=',1)
    # Source clip timestamp rounding must not drop the final complete picture frames.
    command[-1:-1]=['-r','25','-fps_mode','cfr']
    return command


def render(args):
    out=args.output.resolve();edl=read(out/'edit_decisions.json');audit=read(out/'adapter_report.json')
    mix_report=read(out/'natural_mix_report.json')
    if mix_report['edl_sha256']!=digest(out/'edit_decisions.json') or mix_report['master_sha256']!=digest(out/'zh_mix.wav'):
        raise ValueError('Stale natural mix: rebuild after audio/EDL changes')
    if digest(audit['logo']['path'])!=audit['logo']['sha256']:raise ValueError('Logo changed after prepare')
    seed_picture_cache(args.root,out)
    visual=module(args.root/'scripts/compact_edit.py','_natural_adapter_render')
    original_call=visual._call
    visual._call=lambda argv,*a,**k:original_call(final_mux_frame_clock(argv),*a,**k)
    if 'subtitle_pages' in edl:
        pager=module(Path(__file__).with_name('subtitle_pages.py'),'_natural_render_pages')
        # The isolated legacy renderer rebuilds ASS internally. Keep that rebuild
        # on the reviewed actual-word pages instead of restoring its old cue limit.
        visual.build_portrait_ass=lambda current,path:pager.build_ass(current,path,visual)
    result=visual.render(edl,out/'zh_mix.wav',out/'landscape_natural_candidate.mp4',out/'compact_bilingual.ass',
                         Path(audit['logo']['path']),out/'render_cache')
    save(out/'compact_export.json',result);return result


def prepare_candidate(root,job,output,manifests,receipt,baseline_timeline=None,join_groups=None,
                      update=None,allow_job_output=False):
    """Web-facing preparation API. Callback progress is 0..100."""
    if update:update('编排自然时间轴',0,{'human_reviewed':False})
    args=SimpleNamespace(root=Path(root),job=job,output=Path(output),manifest=[Path(p) for p in manifests],
        receipt=Path(receipt),baseline_timeline=Path(baseline_timeline) if baseline_timeline else None,
        join_groups=Path(join_groups) if join_groups else None,allow_job_output=allow_job_output)
    report=prepare(args)
    if update:update('编排自然时间轴',100,{'segments':report['segments'],'duration_s':report['duration_s'],'issues':report['issues']})
    return report


def export_candidate(root,job,output,update=None,verify=True,mastering_policy='legacy_loudnorm_v1'):
    """Explicit sequential mix/render/verification API; never invokes a model."""
    args=SimpleNamespace(root=Path(root).resolve(),job=job,output=Path(output).resolve(),mastering_policy=mastering_policy)
    if update:update('混合独立背景与配音',0)
    mixed=mix(args)
    if update:update('混合独立背景与配音',100)
    if update:update('导出双语视频',0)
    exported=render(args)
    if update:update('导出双语视频',100)
    checked=None
    if verify:
        if update:update('检查完整媒体',0)
        checker=module(Path(__file__).with_name('verify_natural_compact.py'),'_natural_api_verify')
        out=args.output
        checked=checker.verify(out/'edit_decisions.json',out/'landscape_natural_candidate.mp4',
            out/'compact_bilingual.ass',out/'zh_mix.wav',out/'compact_export.json',out/'verification.json')
        if update:update('检查完整媒体',100,{'human_reviewed':False})
    return {'mixed':mixed,'exported':exported,'verification':checked,'duration_s':exported['duration_s'],
            'human_reviewed':False,'listening_approved':False,'output':str(args.output)}


def preview_candidate(root,job,prepared_output,output,max_seconds=45,update=None,execute=True):
    """Prefix preview at complete group boundaries; never truncate a WAV."""
    import copy
    prepared=Path(prepared_output).resolve();target=Path(output).resolve()
    if target.exists() and any(target.iterdir()):raise FileExistsError('Preview output must be new and empty')
    if not math.isfinite(max_seconds) or max_seconds<=0:raise ValueError('Invalid preview duration')
    edl=copy.deepcopy(read(prepared/'edit_decisions.json'));kept=[]
    for clip in edl['clips']:
        kept.append(clip)
        if clip['kind']=='speech' and clip['output_end_s']>=max_seconds:break
    ids={sid for clip in kept for sid in clip.get('segment_ids',[])}
    rows=[r for r in edl['utterances'] if r['segment_id'] in ids]
    if not rows:raise ValueError('No complete speech group fits the preview')
    target.mkdir(parents=True,exist_ok=True)
    edl.update(clips=kept,utterances=rows,frames=sum(c['frames'] for c in kept),speech_segments=len(rows),
        duration_s=kept[-1]['output_end_s'],issues=[i for i in edl['issues'] if set(i['ids'])&ids],
        source_timeline=str(target/'timeline.native.json'),preview_only=True)
    if 'subtitle_pages' in edl:
        edl['subtitle_pages']=[page for page in edl['subtitle_pages'] if page['parent_id'] in ids]
    save(target/'timeline.native.json',{'segments':rows,'issues':[],'method':'Complete-group prefix preview'})
    save(target/'source.json',read(prepared/'source.json'));save(target/'edit_decisions.json',edl)
    counts={'native':sum(r['audio_provenance']['kind']=='native_manifest' for r in rows),
            'legacy_receipt_reuse':sum(r['audio_provenance']['kind']=='legacy_verified_asset_receipt' for r in rows)}
    audit=read(prepared/'adapter_report.json');audit.update(preview_only=True,segments=len(rows),duration_s=edl['duration_s'],
        counts=counts,full_native_backend_conversion=counts['native']==len(rows),picture_cache_candidate_matches=None)
    save(target/'adapter_report.json',audit)
    visual=module(Path(root)/'scripts/compact_edit.py','_natural_preview_layout')
    if 'subtitle_pages' in edl:
        pager=module(Path(__file__).with_name('subtitle_pages.py'),'_natural_preview_pages')
        layout=pager.build_ass(edl,target/'compact_bilingual.ass',visual)
    else:layout=visual.build_compact_ass(edl,target/'compact_bilingual.ass')
    save(target/'subtitle_layout.json',layout)
    return export_candidate(root,job,target,update=update) if execute else audit


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('phase',choices=('prepare','mix','render','verify'))
    p.add_argument('--root',type=Path,required=True);p.add_argument('--job',required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--receipt',type=Path);p.add_argument('--manifest',type=Path,action='append');p.add_argument('--baseline-timeline',type=Path)
    p.add_argument('--join-groups',type=Path);p.add_argument('--subtitle-pages',type=Path);p.add_argument('--allow-job-output',action='store_true')
    p.add_argument('--mastering-policy',choices=('legacy_loudnorm_v1','constant_if_safe_v1'),default='legacy_loudnorm_v1')
    p.add_argument('--progress-json',action='store_true',help='Emit supervised render progress lines')
    args=p.parse_args();args.root=args.root.resolve()
    if args.phase=='prepare':
        if not args.receipt:p.error('prepare requires --receipt')
        result=prepare(args)
    elif args.phase=='mix':result=mix(args)
    elif args.phase=='render':
        if args.progress_json:
            stopped=threading.Event();began=time.time()
            def watch():
                last=None;path=args.output/'render_cache/render_progress.json'
                while not stopped.wait(.5):
                    try:
                        current=path.read_text();state=json.loads(current)
                        if current!=last and state.get('started_at_unix',0)>=began-1:
                            print(json.dumps({'event':'render_progress',**state},ensure_ascii=False),flush=True);last=current
                    except (OSError,ValueError):pass
            watcher=threading.Thread(target=watch,daemon=True);watcher.start()
            try:result=render(args)
            finally:stopped.set();watcher.join(timeout=2)
        else:result=render(args)
    else:
        verify=module(Path(__file__).with_name('verify_natural_compact.py'),'_natural_adapter_verify')
        out=args.output.resolve();result=verify.verify(out/'edit_decisions.json',out/'landscape_natural_candidate.mp4',
            out/'compact_bilingual.ass',out/'zh_mix.wav',out/'compact_export.json',out/'verification.json')
    print(json.dumps({'phase':args.phase,'status':result.get('status','complete'),'issues':result.get('issues'),
                      'duration_s':result.get('duration_s'),'output':str(args.output.resolve())},ensure_ascii=False))


if __name__=='__main__':main()
