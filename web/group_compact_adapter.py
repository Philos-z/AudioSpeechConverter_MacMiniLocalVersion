"""Whole-group audio once; original-row subtitles from auditable acoustic timing.

Independent from the historical per-row adapter. No model import/inference.
Every output/cache is isolated beneath the explicit output directory.
"""
import argparse
from collections import Counter
from fractions import Fraction
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time
import wave

SCHEMA='whole_group_audio_ctc_cues_edit_v1'
FFMPEG='/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg'
FFPROBE='/opt/homebrew/opt/ffmpeg-full/bin/ffprobe'
CTC_MODEL_SHA256='68c9c943840f7d9cf3e8a4970ba50f404feb5277f611fa82b7e72267786fa84a'

def read(path):return json.loads(Path(path).read_text())
def save(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.partial');tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');tmp.replace(path)
def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for b in iter(lambda:stream.read(1024*1024),b''):h.update(b)
    return h.hexdigest()
def key(data):return hashlib.sha256(json.dumps(data,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def module(path,name):
    previous=sys.modules.get(name)
    if previous is not None and Path(getattr(previous,'__file__','')).resolve()==Path(path).resolve():return previous
    spec=importlib.util.spec_from_file_location(name,path);value=importlib.util.module_from_spec(spec)
    sys.modules[name]=value;spec.loader.exec_module(value);return value
def check(value,message):
    if not value:raise ValueError(message)
def call(argv):
    result=subprocess.run([str(a) for a in argv],capture_output=True,text=True)
    if result.returncode:raise ValueError('MEDIA_COMMAND_FAILED: '+result.stderr[-2000:])
    return result

def character_count(text):
    expanded=re.sub(r'<([^<>|]+)\|[^<>]*>',lambda value:value.group(1),text)
    return sum(character.isalnum() for character in expanded)


def validate_generic_meaning_evidence(plan,group,rows):
    scope=group['reference'].get('speaker_scope',{})
    check(scope.get('job')==plan['job'] and scope.get('speaker_id')==group['speaker_id'] and
          scope.get('source_segments_sha256')==plan['source_segments']['sha256'] and
          scope.get('source_video_sha256')==plan['source_binding']['source_video_sha256'],
          'Generic reference is not bound to this job/source/speaker')
    evidence=group.get('join_evidence',{})
    if len(rows)==1:
        check(evidence.get('kind')=='single_source_sentence_no_join','Singleton falsely claims a semantic join')
        return
    check(evidence.get('boundary_review_method')=='proxy_second_pass_semantic_join' and
          evidence.get('same_topic') is True and evidence.get('topic_boundary_crossed') is False and
          evidence.get('quote_speaker_change') is False and bool(evidence.get('reason')),
          'Generic multi-row group lacks actual same-topic/no-speaker-change semantic review')
    check(character_count(group['text_zh'])<=80,'Generic semantic group exceeds 80 normalized characters')
    actual_gaps=[b['start_s']-a['end_s'] for a,b in zip(rows,rows[1:])]
    saved=evidence.get('source_gaps_s',[])
    check(len(saved)==len(actual_gaps) and all(abs(a-b)<1e-7 for a,b in zip(saved,actual_gaps)),'Semantic source gap receipt changed')
    provenance=evidence.get('suggestion_receipt',{});path=Path(provenance.get('path',''))
    check(path.is_file() and sha(path)==provenance.get('sha256'),'Proxy semantic suggestion receipt changed')
    proposals=read(path);name=provenance.get('proposals_key')
    check(name=='tts_join_suggestions','Unexpected semantic proposal collection')
    check(proposals.get('job')==plan['job'] and proposals.get('source_segments_sha256')==plan['source_segments']['sha256'] and
          proposals.get('source_speakers_sha256')==plan['source_speakers']['sha256'] and
          proposals.get('review_status') in ('machine_second_pass_approved','machine_second_pass_partial_continuity_review'),
          'Semantic suggestions lack exact job/source/speaker SHA or machine second-pass review')
    speakers=plan['source_speakers'];check(sha(speakers['path'])==speakers['sha256'],'Source speaker file changed')
    check(provenance.get('advice_usable') is True and provenance.get('review_status')==proposals['review_status'] and
          provenance.get('source_speakers_sha256')==speakers['sha256'],'Semantic review receipt context changed')
    index=evidence.get('proposal_index')
    check(isinstance(index,int) and not isinstance(index,bool) and 0<=index<len(proposals.get(name,[])),'Semantic proposal index invalid')
    proposal=proposals[name][index]
    check(proposal.get('member_ids')==group['member_ids'] and proposal.get('approved') is True and
          proposal.get('same_topic') is True and proposal.get('topic_boundary_crossed') is False and
          proposal.get('quote_speaker_change') is False and proposal.get('reason')==evidence['reason'],
          'Group differs from the actual approved Proxy semantic proposal')


def validate_plan(plan):
    check(plan.get('schema')=='pretts_semantic_group_plan_v1','Unexpected pre-TTS group schema')
    source=plan['source_rows'];groups=plan['tts_groups'];ids=[r['segment_id'] for r in source]
    if plan.get('planner_version')=='generic_pretts_semantic_groups_v1':
        speakers=plan['source_speakers'];check(sha(speakers['path'])==speakers['sha256'],'Job-local speaker file changed')
        values=read(speakers['path']);root=Path(plan['root']).resolve()
        for group in groups:
            info=values[group['speaker_id']];path=(root/info['reference']).resolve()
            check(info.get('reviewed') is True and path.is_relative_to(root) and
                  path==Path(group['reference']['path']).resolve() and sha(path)==group['reference']['sha256'],
                  'Group reference differs from the reviewed source speaker file')
    check(len(ids)==len(set(ids)) and bool(ids),'Invalid original source-row coverage')
    check(len({g['group_id'] for g in groups})==len(groups),'Duplicate synthesis group IDs')
    grouped_ids=[sid for g in groups for sid in g['member_ids']]
    if grouped_ids!=ids:
        check(plan.get('full_tts_group_plan_complete') is False,'Partial synthesis plan lacks an explicit incomplete-scope marker')
        check(grouped_ids==plan.get('selected_member_ids'),'Selected group source coverage differs from declared scope')
        check(len(grouped_ids)==len(set(grouped_ids)) and all(sid in ids for sid in grouped_ids),'Unknown or duplicated selected source row')
        check([sid for sid in ids if sid in set(grouped_ids)]==grouped_ids,'Selected source rows were reordered')
        check([sid for sid in ids if sid not in set(grouped_ids)]==plan.get('unselected_member_ids'),'Partial plan does not account for every original row')
    lookup={r['segment_id']:r for r in source}
    for group in groups:
        rows=[lookup[sid] for sid in group['member_ids']]
        positions=[ids.index(row['segment_id']) for row in rows]
        check(positions and positions==list(range(positions[0],positions[0]+len(positions))),'Group skips nonadjacent original source rows')
        check(all(r['speaker_id']==group['speaker_id'] for r in rows),'Synthesis group crosses speakers')
        check(''.join(r['text_zh'] for r in rows)==group['text_zh'],'Group synthesis changed original Chinese')
        check(''.join(group['text_de'].split())==''.join(''.join(r['text_de'] for r in rows).split()),'Group synthesis changed original German')
        check([m['segment_id'] for m in group['members']]==group['member_ids'],'Group member receipt changed')
        character=0
        for member,row in zip(group['members'],rows):
            check(all(member[k]==row[k] for k in ('segment_id','text_zh','text_de','source_ids')),'Group member source text/IDs changed')
            count=character_count(row['text_zh']) if 'alphanumeric' in plan.get('character_index_policy','') else len(row['text_zh'])
            check(member['character_start']==character and member['character_end']==character+count,'Group member character span changed')
            check(abs(member['source_start_s']-row['start_s'])<1e-6 and abs(member['source_end_s']-row['end_s'])<1e-6,'Group member source time changed')
            character=member['character_end']
        check(abs(group['source_start_s']-rows[0]['start_s'])<1e-6 and abs(group['source_end_s']-rows[-1]['end_s'])<1e-6,'Group source interval changed')
        evidence=group.get('join_evidence',{})
        generic=plan.get('planner_version')=='generic_pretts_semantic_groups_v1'
        if generic:
            validate_generic_meaning_evidence(plan,group,rows)
            maximum_span,maximum_gap=15.,1.5
        else:
            # Historical explicitly scoped plans remain readable. Their actual
            # reviewed limits, source/ref bindings and meaning evidence apply;
            # no episode, row ID, count or reference SHA is special-cased.
            maximum_span=min(24.,float(evidence.get('max_source_span_s',24.)))
            maximum_gap=min(1.5,float(evidence.get('max_source_gap_s',1.2)))
        if len(rows)>1:
            check(group['source_end_s']-group['source_start_s']<=maximum_span+1e-6,'Synthesis group exceeds its reviewed source span')
            check(all(-.04<=b['start_s']-a['end_s']<=maximum_gap+1e-9 for a,b in zip(rows,rows[1:])),'Synthesis group crosses a source discontinuity')
            check(evidence.get('kind')=='explicit_scoped_adjacent_same_speaker','Multi-row group lacks explicit reviewed join evidence')
            check(evidence.get('topic_boundary_crossed') is False or
                  (not generic and evidence.get('explicit_user_transition_accepted') is True and bool(evidence.get('semantic_reason'))),
                  'Multi-row group crosses an unreviewed topic boundary')
        ref=group['reference'];check(Path(ref['path']).is_file() and sha(ref['path'])==ref['sha256'],'Group original reference SHA mismatch')
    return lookup


def validated_alignment(group,alignment,rows):
    strict_ctc=alignment.get('schema')=='tts_group_ctc_subtitle_alignment_v1'
    whisper_words=alignment.get('schema')=='target_free_whisper_word_onset_v1'
    generic_alignment=alignment.get('schema')=='generic_group_subtitle_alignment_v1'
    check((strict_ctc and alignment.get('status')=='aligned') or
          (whisper_words and alignment.get('status')=='subtitle_timing_ready_for_preview') or
          (generic_alignment and alignment.get('status')=='aligned_for_machine_preview'),
          'Acoustic subtitle timing not approved for preview')
    if generic_alignment:
        generic_policy=module(Path(__file__).with_name('generic_group_alignment.py'),'_group_generic_word_policy')
        generic_policy.validate_receipt(alignment)
    if whisper_words:
        word_policy=module(Path(__file__).with_name('align_group_whisper_subtitles.py'),'_group_whisper_word_policy')
        word_policy.validate_receipt(alignment)
    check(alignment['group_id']==group['group_id'] and alignment['member_ids']==group['member_ids'],'CTC alignment belongs to another group')
    check(not alignment.get('proportional_fallback_used',False) and not alignment.get('proportional_timing_used',False),'Proportional subtitle fallback is forbidden')
    acoustic=alignment.get('alignment_receipt',{})
    if not generic_alignment:
        check(acoustic.get('ctc_model_sha256')==CTC_MODEL_SHA256,'Unexpected CTC acoustic model identity')
        check(acoustic.get('emissions_path') and sha(acoustic['emissions_path'])==acoustic.get('emissions_sha256'),'CTC emissions file changed')
    audio=alignment['audio'];path=Path(audio['path']);check(sha(path)==audio['sha256'],'Group audio SHA mismatch')
    with wave.open(str(path)) as stream:
        frames,rate=stream.getnframes(),stream.getframerate();channels=stream.getnchannels()
    check(frames==audio['frames'] and rate==audio['sample_rate'] and channels==1 and frames>0,'Invalid group WAV header')
    duration=frames/rate;check(abs(duration-audio['duration_s'])<=1/rate,'Stale group audio duration')
    sidecar=Path(alignment.get('generation_sidecar',audio.get('native_sidecar',path.with_suffix('.json'))))
    check(sidecar.is_file(),'Original generation sidecar required; no unbound group WAV')
    entry=read(sidecar);content=entry['content']
    if audio.get('native_sidecar_sha256'):check(sha(sidecar)==audio['native_sidecar_sha256'],'Declared generation sidecar SHA changed')
    check(entry.get('schema') in ('native_mlx_whole_utterance_v1','continuous_utterance_v1'),'Unexpected group generation provenance')
    check(entry['sha256']==audio['sha256'] and entry['cache_key']==key(content),'Generation content/audio receipt mismatch')
    check(content['text_zh']==group['text_zh'] and content['effective_tts_text']==(group.get('tts_pronunciation_text') or group['text_zh']),
          'Generated group text or pronunciation differs from source plan')
    check(content['reference_sha256']==group['reference']['sha256'],'Generated group used another voice reference')
    cues=alignment['subtitle_cues'];check([c['segment_id'] for c in cues]==group['member_ids'],'Missing or duplicate original subtitle rows')
    check(len(cues)==1 or len(alignment.get('boundaries',[]))==len(cues)-1,'Missing internal CTC boundary evidence')
    check(cues and abs(cues[0]['audio_start_s'])<1e-7 and abs(cues[-1]['audio_end_s']-duration)<1e-7,'Subtitle cues do not cover the complete group WAV')
    previous=0.
    for cue in cues:
        row=rows[cue['segment_id']]
        check(all(cue[k]==row[k] for k in ('speaker_id','text_zh','text_de','source_ids')),'Subtitle text/source identity changed')
        check(abs(cue['source_start_s']-row['start_s'])<1e-6 and abs(cue['source_end_s']-row['end_s'])<1e-6,'Subtitle original source time changed')
        a,b=cue['audio_start_s'],cue['audio_end_s']
        check(all(math.isfinite(v) for v in (a,b)) and a>=previous-1e-6 and 0<=a<b<=duration+1e-6,'Invalid/overlapping CTC cue times')
        previous=b
    for index,boundary in enumerate(alignment.get('boundaries',[])):
        previous_cue,next_cue=cues[index:index+2];switch=boundary['audio_switch_s']
        check(boundary['after_segment_id']==previous_cue['segment_id'],'CTC boundary attached to the wrong source cue')
        check(boundary['character_boundary']==group['members'][index]['character_end']==group['members'][index+1]['character_start'],
            'CTC boundary character index differs from original group members')
        check(abs(previous_cue['audio_end_s']-switch)<1e-7 and abs(next_cue['audio_start_s']-switch)<1e-7,'Subtitle switch differs from the CTC boundary receipt')
        if strict_ctc:
            protection=boundary['protection_ms_each_side']/1000
            check(protection>=.12-1e-9 and switch-boundary['forced_left_character_end_s']>=protection-1e-9
                and boundary['forced_right_character_start_s']-switch>=protection-1e-9,'CTC subtitle boundary violates the 120ms character guards')
            check(boundary['protected_switch_lower_s']-1e-9<=switch<=boundary['protected_switch_upper_s']+1e-9,'Subtitle switch lies outside the protected CTC intersection')
        else:
            check(switch==boundary['whisper_right_word_onset_s'] and boundary['whisper_left_word_end_s']<=switch,
                'Subtitle-only onset no longer matches complete target-free words')
    return {**audio,'duration_s':duration,'audio':str(path.resolve()),'audio_sha256':audio['sha256'],
        'audio_frames':frames,'audio_sample_rate':rate,'generation_sidecar':str(sidecar.resolve()),'generation_sidecar_sha256':sha(sidecar)}


def build_group_edl(plan,alignments,source,selected_groups=None,include_intro=False):
    rows=validate_plan(plan);groups=plan['tts_groups'];chosen=set(selected_groups or [g['group_id'] for g in groups])
    check(chosen and chosen.issubset({g['group_id'] for g in groups}),'Unknown or empty synthesis-group selection')
    by_alignment={a['group_id']:a for a in alignments};check(len(by_alignment)==len(alignments),'Duplicate group alignment files')
    check(chosen.issubset(by_alignment),'Missing CTC alignment for selected group')
    cursor=0;clips=[];utterances=[];cues=[];issues=[];used=[]
    if include_intro:
        check(len(chosen)==len(groups) and sum(len(g['member_ids']) for g in groups)==len(rows),'Intro retention is only allowed for the complete source-row plan')
        frames=math.floor(groups[0]['source_start_s']*25)
        if frames:clips.append({'kind':'intro','source_start_s':0.,'source_end_s':frames/25,'source_duration_s':frames/25,
            'freeze_s':0.,'output_start_s':0.,'output_end_s':frames/25,'frames':frames});cursor=frames
    for index,group in enumerate(groups):
        if group['group_id'] not in chosen:continue
        alignment=by_alignment[group['group_id']];audio=validated_alignment(group,alignment,rows)
        speech=audio['duration_s'];frames=math.ceil((speech+.35)*25-1e-8);length=frames/25;start=group['source_start_s']
        original=list(rows);after=original.index(group['member_ids'][-1])+1
        boundary=rows[original[after]]['start_s'] if after<len(original) else source['duration_s']
        available=boundary-start;check(available>0,'Invalid/overlapping group source intervals')
        taken=min(length,available);freeze=length-taken
        if speech-available>.35+1e-6:issues.append({'group_id':group['group_id'],'speech_s':speech,'available_source_s':available,
            'reason':'whole_group_audio_exceeds_source_window','audio_was_not_truncated':True})
        clip={'kind':'speech','segment_ids':[group['group_id']],'source_member_ids':group['member_ids'],
            'source_start_s':start,'source_end_s':start+taken,'source_duration_s':taken,'freeze_s':freeze,
            'output_start_s':cursor/25,'output_end_s':(cursor+frames)/25,'frames':frames,'speech_s':speech,
            'speech_audio_s':speech,'pause_s':length-speech,'joined':len(group['member_ids'])>1}
        clips.append(clip);offset=cursor/25
        utterances.append({'segment_id':group['group_id'],'group_id':group['group_id'],'speaker_id':group['speaker_id'],
            'member_ids':group['member_ids'],'text_zh':group['text_zh'],'text_de':group['text_de'],**audio,
            'speech_s':speech,'output_start_s':offset,'output_speech_end_s':offset+speech,'output_end_s':(cursor+frames)/25,
            'clip_index':len(clips)-1,'alignment_sha256':key(alignment),
            'subtitle_timing_schema':alignment['schema'],'strict_ctc_rejected':alignment.get('strict_ctc_rejected',False),
            'subtitle_uncertainty_note':alignment.get('uncertainty_note'),'content_risks':alignment.get('content_risks',[])})
        for local in alignment['subtitle_cues']:
            cues.append({**local,'group_id':group['group_id'],'clip_index':len(clips)-1,
                'output_start_s':offset+local['audio_start_s'],'output_speech_end_s':offset+local['audio_end_s'],
                'output_end_s':offset+local['audio_end_s']})
        # Only hold the final subtitle through the natural group tail; do not
        # change its CTC speech boundary or insert a gap into generated audio.
        cues[-1]['output_end_s']=(cursor+frames)/25
        used.extend(group['member_ids']);cursor+=frames
    check(len(used)==len(set(used)),'Original source subtitle row played twice')
    return {'schema':SCHEMA,'format':SCHEMA,'source_input':source['input'],'source_duration_s':source['duration_s'],
        'fps':25,'width':960,'height':540,'source_picture_box':[0,0,960,540],'duration_s':cursor/25,'frames':cursor,
        'speech_segments':len(utterances),'tts_group_count':len(utterances),'source_row_count':len(used),
        'source_plan_row_count':len(rows),'partial_preview':len(used)!=len(rows),'selected_source_ids':used,
        'clips':clips,'utterances':utterances,'subtitle_cues':cues,'issues':issues,'pause_target_s':.35,
        'new_internal_silence_s':0.,'group_audio_mixed_once':True,'proportional_fallback_used':False,
        'timing_method':'one intact WAV per synthesis group; bound original-row acoustic subtitle cues (singleton full duration or explicit real-audio word timing); natural group end + >=.35s',
        'subtitle_timing_schemas':[a['schema'] for a in alignments if a['group_id'] in chosen],
        'strict_ctc_rejected_groups':[a['group_id'] for a in alignments if a['group_id'] in chosen and a.get('strict_ctc_rejected')],
        'content_risks':[risk for a in alignments if a['group_id'] in chosen for risk in a.get('content_risks',[])],
        'human_reviewed':False,'full_delivery_approved':False}


def build_ass(root,edl,path):
    visual=module(Path(root)/'scripts/compact_edit.py','_group_adapter_subtitles')
    return visual.build_compact_ass({**edl,'utterances':edl['subtitle_cues']},path)


def prepare(root,plan_path,alignment_paths,source_path,receipt_path,output,selected_groups=None,include_intro=False):
    output=Path(output).resolve();check(not output.exists() or not any(output.iterdir()),'Use a new empty group output directory')
    source=read(source_path);receipt=read(receipt_path);assets=receipt['assets'];plan=read(plan_path)
    binding=plan.get('source_binding',{})
    check(Path(binding['source_receipt_path']).resolve()==Path(source_path).resolve(),'Group plan bound to a different source JSON path')
    check(binding.get('source_receipt_sha256')==sha(source_path),'Source JSON is not bound to group plan')
    check(binding.get('source_video_sha256')==assets['source_video']['sha256'] and binding.get('source_video_bytes')==assets['source_video']['bytes'],
          'Group plan belongs to another source video')
    check(Path(binding['source_video_path']).resolve()==Path(source['input']).resolve(),'Group plan video pointer changed')
    check(abs(binding['source_duration_s']-source['duration_s'])<1e-7 and binding['source_audio_track']==source['audio_track'],'Source duration/audio-track binding changed')
    check(sha(plan['source_segments']['path'])==plan['source_segments']['sha256'],'Original source-row file changed')
    check(read(plan['source_segments']['path'])['segments']==plan['source_rows'],'Group plan original rows are not unchanged')
    check(Path(source['input']).resolve()==Path(assets['source_video']['path']).resolve(),'Source receipt pointer mismatch')
    video=next(s for s in source['probe']['streams'] if s['codec_type']=='video')
    check(abs(video['width']/video['height']-16/9)<.01,'This scoped group adapter requires the original 16:9 source')
    for name in ('source_video','independent_background','logo'):
        asset=assets[name];check(Path(asset['path']).stat().st_size==asset['bytes'] and sha(asset['path'])==asset['sha256'],'Changed source/background/logo receipt')
    alignments=[read(p) for p in alignment_paths]
    for alignment in alignments:
        check(Path(alignment['plan']['path']).resolve()==Path(plan_path).resolve() and alignment['plan']['sha256']==sha(plan_path)
            and alignment['source_segments_sha256']==plan['source_segments']['sha256'],
            'CTC alignment source-plan binding changed')
        acoustic=alignment.get('alignment_receipt',{})
        if alignment.get('schema')!='generic_group_subtitle_alignment_v1':
            check(acoustic.get('emissions_path') and sha(acoustic['emissions_path'])==acoustic['emissions_sha256'],'CTC acoustic evidence changed')
    edl=build_group_edl(plan,alignments,source,selected_groups,include_intro)
    output.mkdir(parents=True,exist_ok=True);save(output/'edit_decisions.json',edl)
    save(output/'subtitle_layout.json',build_ass(root,edl,output/'compact_bilingual.ass'))
    report={'schema':SCHEMA,'plan':str(Path(plan_path).resolve()),'plan_sha256':sha(plan_path),
        'alignments':[{'path':str(Path(p).resolve()),'sha256':sha(p)} for p in alignment_paths],
        'source_json':str(Path(source_path).resolve()),'source_json_sha256':sha(source_path),
        'source_video':assets['source_video'],'background':assets['independent_background'],'logo':assets['logo'],
        'source_row_count':edl['source_row_count'],'tts_group_count':edl['tts_group_count'],'partial_preview':edl['partial_preview'],
        'duration_s':edl['duration_s'],'issues':edl['issues'],'audio_truncated':False,'models_started':False,
        'subtitle_timing_schemas':edl['subtitle_timing_schemas'],'strict_ctc_rejected_groups':edl['strict_ctc_rejected_groups'],
        'human_reviewed':False,'content_risks':edl['content_risks']}
    save(output/'group_adapter_report.json',report);return report


def mix(root,output,mastering_policy='legacy_loudnorm_v1'):
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly
    root=Path(root);output=Path(output);edl=read(output/'edit_decisions.json');audit=read(output/'group_adapter_report.json')
    check(mastering_policy in ('legacy_loudnorm_v1','constant_if_safe_v1'),'Unknown mastering policy')
    check(not edl['issues'],'Resolve whole-group source-window issues; no speech is cut')
    natural=module(Path(__file__).with_name('natural_compact_adapter.py'),'_group_natural_background')
    GainCache=module(root/'scripts/gain_cache.py','_group_gain').GainCache;cache=GainCache(output/'gain_cache.json')
    frames=edl['frames']*1920;dialogue=np.zeros((frames,2),dtype='float32');placements=[];began=time.perf_counter()
    for group in edl['utterances']:
        path=Path(group['audio']);check(sha(path)==group['audio_sha256'],'Group WAV changed after CTC alignment')
        samples,rate=sf.read(path,dtype='float32',always_2d=True);check(samples.shape[1]==1 and np.isfinite(samples).all(),'Invalid group samples')
        check(len(samples)==group['audio_frames'],'Group WAV frames changed')
        if rate!=48000:
            divisor=math.gcd(rate,48000);samples=resample_poly(samples,48000//divisor,rate//divisor,axis=0).astype('float32')
        start=round(group['output_start_s']*48000);end=start+len(samples)
        check(end<=frames and abs(len(samples)-round(group['speech_s']*48000))<=1,'Group speech would be truncated')
        gain,hit=cache.get(path);dialogue[start:end]+=np.repeat(samples,2,axis=1)*10**(gain/20)
        placements.append({'group_id':group['group_id'],'member_ids':group['member_ids'],'start_frame':start,'frames':len(samples),'gain_db':gain,**hit})
    check(len(placements)==edl['tts_group_count'],'A synthesis group was not mixed exactly once')
    check(sha(audit['background']['path'])==audit['background']['sha256'],'Background source changed')
    bed,background=natural.retime_background(edl,audit['background']['path'])
    sf.write(output/'zh_dialogue.wav',dialogue,48000,subtype='PCM_24');sf.write(output/'background_compact.wav',bed,48000,subtype='PCM_24')
    dialogue+=bed;sf.write(output/'premix.wav',dialogue,48000,subtype='FLOAT');del dialogue,bed
    video=module(root/'scripts/video.py','_group_video_utilities')
    def legacy_master():
        measure=call([FFMPEG,'-hide_banner','-threads','2','-i',output/'premix.wav','-af','loudnorm=I=-16:TP=-2:LRA=11:print_format=json','-f','null','-'])
        level=video.parse_loudness(measure.stderr)
        filt=f"loudnorm=I=-16:TP=-2:LRA=11:linear=true:measured_I={level['input_i']}:measured_TP={level['input_tp']}:measured_LRA={level['input_lra']}:measured_thresh={level['input_thresh']}:offset={level['target_offset']}"
        call([FFMPEG,'-v','error','-threads','2','-filter_threads','2','-y','-i',output/'premix.wav','-af',filt,'-ar','48000','-c:a','pcm_s24le',output/'zh_mix.wav'])
        call([FFMPEG,'-v','error','-threads','2','-y','-i',output/'zh_mix.wav','-c:a','aac','-b:a','128k',output/'zh_mix.m4a'])
        return {'requested_policy':mastering_policy,'actual_strategy':'legacy_loudnorm_v1',
                'measurement':level,'original_mastering_behavior_retained':True}
    if mastering_policy=='constant_if_safe_v1':
        mastering=module(Path(__file__).with_name('optional_constant_master.py'),'_group_constant_master')
        mastering_receipt=mastering.master_optional(output/'premix.wav',output,legacy_master,legacy_code_path=Path(__file__))
    else:mastering_receipt=legacy_master()
    for language in ('zh','de'):
        (output/f'{language}.srt').write_text('\n\n'.join(f"{i+1}\n{video.stamp(c['output_start_s'])} --> {video.stamp(c['output_end_s'])}\n{c['text_'+language]}" for i,c in enumerate(edl['subtitle_cues']))+'\n')
    cache.save(output/'gain_cache.json')
    result={'placements':placements,'source_row_count':edl['source_row_count'],'tts_group_count':edl['tts_group_count'],
        'group_audio_mixed_once':True,'group_wav_samples_cropped_or_faded':False,'background':background,'new_internal_silence_s':0.,
        'mastering':mastering_receipt,
        'edl_sha256':sha(output/'edit_decisions.json'),'master_sha256':sha(output/'zh_mix.wav'),'wall_s':time.perf_counter()-began}
    save(output/'group_mix_report.json',result);return result


def render(root,output):
    root=Path(root);output=Path(output);edl=read(output/'edit_decisions.json');audit=read(output/'group_adapter_report.json');mixed=read(output/'group_mix_report.json')
    check(mixed['edl_sha256']==sha(output/'edit_decisions.json') and mixed['master_sha256']==sha(output/'zh_mix.wav'),'Stale group mix')
    check(sha(audit['source_video']['path'])==audit['source_video']['sha256'] and sha(audit['logo']['path'])==audit['logo']['sha256'],'Source video/logo changed')
    visual=module(root/'scripts/compact_edit.py','_group_adapter_render')
    original_builder=visual.build_compact_ass
    # A private imported module only; renderer must rebuild from original-row
    # CTC cues, while audio/video clips remain whole synthesis groups.
    visual.build_portrait_ass=lambda value,path:original_builder({**value,'utterances':value['subtitle_cues']},path)
    result=visual.render(edl,output/'zh_mix.wav',output/'group_zh_candidate.mp4',output/'compact_bilingual.ass',
        Path(audit['logo']['path']),output/'render_cache')
    result.update(source_row_count=edl['source_row_count'],tts_group_count=edl['tts_group_count'],subtitle_timing='bound_acoustic_original_row_cues',subtitle_timing_schemas=edl['subtitle_timing_schemas'],
        strict_ctc_rejected_groups=edl['strict_ctc_rejected_groups'],content_risks=edl['content_risks'],
        group_audio_mixed_once=True,human_reviewed=False)
    save(output/'group_export.json',result);return result


def verify(root,output,skip_levels=False):
    import soundfile as sf
    output=Path(output);edl=read(output/'edit_decisions.json');audit=read(output/'group_adapter_report.json');mixed=read(output/'group_mix_report.json')
    check(not edl['issues'],'Unresolved group edit issues');groups=edl['utterances'];cues=edl['subtitle_cues']
    check(len(groups)==edl['tts_group_count'] and len(cues)==edl['source_row_count'],'Source/group counts conflated')
    check([sid for g in groups for sid in g['member_ids']]==[c['segment_id'] for c in cues]==edl['selected_source_ids'],'Incomplete source/group/cue coverage')
    check(len({g['group_id'] for g in groups})==len(groups) and len({c['segment_id'] for c in cues})==len(cues),'Duplicate group/audio/source row')
    check([p['group_id'] for p in mixed['placements']]==[g['group_id'] for g in groups],'Group audio mixed zero or multiple times')
    check(sha(audit['plan'])==audit['plan_sha256'] and all(sha(p['path'])==p['sha256'] for p in audit['alignments']),'Plan/alignment receipt changed')
    plan=read(audit['plan']);source_rows=validate_plan(plan)
    for receipt in audit['alignments']:
        alignment=read(receipt['path']);planned=next(g for g in plan['tts_groups'] if g['group_id']==alignment['group_id'])
        validated_alignment(planned,alignment,source_rows)
    for group in groups:
        check(sha(group['audio'])==group['audio_sha256'],'Original group WAV changed')
        check(sha(group['generation_sidecar'])==group['generation_sidecar_sha256'],'Generation sidecar changed')
        with wave.open(group['audio']) as stream:check(stream.getnframes()==group['audio_frames'],'Group raw frame count mismatch')
    for cue in cues:
        group=next(g for g in groups if g['group_id']==cue['group_id'])
        check(abs(cue['output_start_s']-group['output_start_s']-cue['audio_start_s'])<1e-7,'Subtitle CTC start changed')
        check(abs(cue['output_speech_end_s']-group['output_start_s']-cue['audio_end_s'])<1e-7,'Subtitle CTC end changed')
    ass=(output/'compact_bilingual.ass').read_text();events=[s.split(',',9) for s in ass.splitlines() if s.startswith('Dialogue:')]
    check(len(events)==2*len(cues),'Missing original-row bilingual subtitle events')
    check(Counter((e[4],e[3]) for e in events)==Counter((c['segment_id'],language) for c in cues for language in ('Chinese','German')),'Duplicate or missing cue language')
    legacy=module(Path(__file__).with_name('verify_natural_compact.py'),'_group_media_verify_utilities')
    for cue in cues:
        for language in ('Chinese','German'):
            event=next(e for e in events if e[4]==cue['segment_id'] and e[3]==language)
            check(abs(legacy._stamp(event[1])-cue['output_start_s'])<=.006 and abs(legacy._stamp(event[2])-cue['output_end_s'])<=.006,'ASS not timed to CTC cue')
    video=output/'group_zh_candidate.mp4';probe=json.loads(call([FFPROBE,'-v','error','-show_streams','-show_format','-of','json',video]).stdout)
    check(video.stat().st_size<50_000_000,'Video exceeds 50 decimal MB')
    visual=next(s for s in probe['streams'] if s['codec_type']=='video');audio=next(s for s in probe['streams'] if s['codec_type']=='audio')
    check((visual['width'],visual['height'],visual['codec_name'])==(960,540,'h264') and Fraction(visual['avg_frame_rate'])==25,'Video geometry/codec changed')
    check(int(visual['nb_frames'])==edl['frames'] and abs(float(probe['format']['duration'])-edl['duration_s'])<=.08,'Video frames/duration mismatch')
    check(audio['codec_name']=='aac' and audio.get('tags',{}).get('language')=='zho' and audio['disposition']['default']==1,'Chinese default track missing')
    check(mixed['edl_sha256']==sha(output/'edit_decisions.json') and mixed['master_sha256']==sha(output/'zh_mix.wav'),'Master/EDL receipt mismatch')
    master=sf.info(output/'zh_mix.wav');check(master.samplerate==48000 and master.channels==2 and master.frames==edl['frames']*1920,'Master audio clock/frame mismatch')
    export=read(output/'group_export.json');expected_width=144 if export['logo'].get('brand')=='Philos.club' else 84
    check(export['burned_bilingual_subtitles'] and export['logo']['width_px']==expected_width and export['logo']['opacity']==.55,'Subtitle/logo export settings mismatch')
    levels=None
    if not skip_levels:
        measured=call([FFMPEG,'-hide_banner','-threads','2','-i',video,'-map','0:a:0','-af','loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json','-f','null','-'])
        levels=json.JSONDecoder().raw_decode(measured.stderr[measured.stderr.rfind('{'):])[0]
        legacy.checked_encoded_levels(levels) # Same actual scan; include finite LRA≤11.
    decoded=call([FFMPEG,'-v','error','-threads','2','-i',video,'-map','0:v:0','-map','0:a','-f','null','-']);check(not decoded.stderr.strip(),'Full media decode failed')
    report={'status':'group_engineering_checks_passed_visual_and_listening_review_remaining','source_row_count':len(cues),
        'tts_group_count':len(groups),'partial_preview':edl['partial_preview'],'duration_s':edl['duration_s'],
        'frames':edl['frames'],'video_sha256':sha(video),'bytes':video.stat().st_size,
        'max_bytes_exclusive':50_000_000,'group_audio_mixed_once':True,'proportional_fallback_used':False,
        'full_decode_passed':True,'encoded_levels':levels,'human_reviewed':False,'listening_approved':False,
        'subtitle_timing_schemas':edl['subtitle_timing_schemas'],'strict_ctc_rejected_groups':edl['strict_ctc_rejected_groups'],
        'content_risks':edl['content_risks']}
    save(output/'group_verification.json',report);return report


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('phase',choices=('prepare','mix','render','verify'))
    p.add_argument('--root',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--mastering-policy',choices=('legacy_loudnorm_v1','constant_if_safe_v1'),default='legacy_loudnorm_v1')
    p.add_argument('--plan',type=Path);p.add_argument('--alignment',type=Path,action='append');p.add_argument('--source',type=Path)
    p.add_argument('--receipt',type=Path);p.add_argument('--alignment-index',type=Path);p.add_argument('--group',action='append');p.add_argument('--include-intro',action='store_true');p.add_argument('--skip-levels',action='store_true')
    args=p.parse_args()
    if args.phase=='prepare':
        paths=list(args.alignment or [])
        if args.alignment_index:
            collection=read(args.alignment_index)
            check(collection.get('schema')=='generic_group_subtitle_alignment_manifest_v1' and collection.get('all_groups_ready') is True,
                  'Generic alignment index has unresolved source-row timing; no subtitles may be dropped')
            check(Path(collection['plan']['path']).resolve()==args.plan.resolve() and collection['plan']['sha256']==sha(args.plan),
                  'Alignment index belongs to another source/group plan')
            for item in collection['groups']:
                check(item['status']=='aligned_for_machine_preview' and sha(item['receipt']['path'])==item['receipt']['sha256'],
                      'Generic alignment index receipt changed')
                paths.append(Path(item['receipt']['path']))
        result=prepare(args.root,args.plan,paths,args.source,args.receipt,args.output,args.group,args.include_intro)
    elif args.phase=='verify':result=verify(args.root,args.output,args.skip_levels)
    elif args.phase=='mix':result=mix(args.root,args.output,args.mastering_policy)
    else:result=render(args.root,args.output)
    print(json.dumps({'phase':args.phase,**result},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
