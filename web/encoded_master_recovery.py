"""One bounded, local post-AAC mastering fallback. No TTS or picture encoding."""
import argparse
import copy
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import time
import wave

import media_recovery as media

FFMPEG='/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg'
FFPROBE='/opt/homebrew/opt/ffmpeg-full/bin/ffprobe'
SCHEMA='encoded_master_recovery_v1'
GIB=1024**3
FILES=('premix.wav','zh_mix.wav','zh_mix.m4a','group_zh_candidate.mp4','edit_decisions.json',
       'group_mix_report.json','mastering_receipt.json','group_export.json','group_adapter_report.json')
TARGETS=('zh_mix.wav','zh_mix.m4a','group_zh_candidate.mp4','group_mix_report.json',
         'mastering_receipt.json','group_export.json','audio_master_recovery.json')


def space(path,minimum):
    media.require(shutil.disk_usage(path).free>=minimum,'Insufficient space for encoded master recovery')


def command(argv,log,output):
    """Same supervised process group; preserve stderr and enforce the 2 GiB floor."""
    space(output,2*GIB);log.parent.mkdir(parents=True,exist_ok=True)
    with log.open('w') as stderr:
        proc=subprocess.Popen([str(v) for v in argv],stdout=subprocess.PIPE,stderr=stderr,text=True)
        began=time.monotonic()
        try:
            while True:
                try:
                    stdout,_=proc.communicate(timeout=.25);break
                except subprocess.TimeoutExpired:
                    space(output,2*GIB)
                    media.require(time.monotonic()-began<1800,'Local mastering subprocess timed out')
        except BaseException:
            proc.kill();proc.communicate();raise
    media.require(proc.returncode==0,'Local media command failed; see '+str(log))
    return stdout


def levels_ok(value):
    numbers=[float(value[name]) for name in ('input_i','input_tp','input_lra')]
    media.require(all(math.isfinite(v) for v in numbers),'Nonfinite encoded levels; repair is not authorized')
    i,tp,lra=numbers
    media.require(lra>=0,'Invalid negative loudness range; repair is not authorized')
    return abs(i+16)<=1 and tp<=-1.5 and 0<=lra<=11


def scan(path,log,output,target_peak=-1.5):
    command([FFMPEG,'-hide_banner','-nostats','-threads','2','-i',path,'-map','0:a:0',
        '-af',f'loudnorm=I=-16:TP={target_peak}:LRA=11:print_format=json','-f','null','-'],log,output)
    text=log.read_text();value=json.JSONDecoder().raw_decode(text[text.rfind('{'):])[0]
    levels_ok(value)
    return value


def probe(path,log,output):
    return json.loads(command([FFPROBE,'-v','error','-show_streams','-show_format','-of','json',path],log,output))


def packet_hash(path,kind,log,output):
    return command([FFMPEG,'-v','error','-i',path,'-map','0:'+kind,'-c','copy',
                    '-f','streamhash','-hash','sha256','-'],log,output).strip()


def audio_frames(path,log,output):
    document=probe(path,log,output);streams=document['streams']
    media.require(len(streams)==1 and streams[0]['codec_type']=='audio','Expected one full PCM track')
    row=streams[0]
    media.require(row['sample_rate']=='48000' and row['channels']==2
        and row.get('time_base')=='1/48000' and str(row.get('duration_ts','')).isdigit(),
        'Master PCM clock/geometry mismatch')
    return int(row['duration_ts'])


def video_identity(document):
    video=[s for s in document['streams'] if s['codec_type']=='video']
    audio=[s for s in document['streams'] if s['codec_type']=='audio']
    media.require(len(video)==len(audio)==1 and video[0]['codec_name']=='h264'
        and audio[0]['codec_name']=='aac' and audio[0]['sample_rate']=='48000' and audio[0]['channels']==2,
        'Expected the current H264 and stereo48k AAC delivery')
    media.require(audio[0].get('tags',{}).get('language')=='zho'
        and audio[0].get('disposition',{}).get('default')==1,'Chinese default audio attribution changed')
    keys=('codec_name','width','height','sample_aspect_ratio','avg_frame_rate','nb_frames','time_base','duration_ts')
    result={k:video[0].get(k) for k in keys}
    media.require(str(result['nb_frames']).isdigit() and int(result['nb_frames'])>0,'Missing full video frame count')
    result['format_duration_s']=float(document['format']['duration'])
    return result


def bound_inputs(root,folder,output):
    root,folder=media.scope(root,folder);output=Path(output).absolute()
    paths={name:media.job_file(folder,output/name) for name in FILES}
    edl=media.read(paths['edit_decisions.json']);audit=media.read(paths['group_adapter_report.json'])
    mixed=media.read(paths['group_mix_report.json']);master=media.read(paths['mastering_receipt.json'])
    export=media.read(paths['group_export.json'])
    root,folder,plan,plan_path=media.plan_binding(root,folder,audit['plan'])
    media.require(audit['plan_sha256']==media.sha(plan_path) and not edl['issues']
        and not edl.get('partial_preview') and not audit.get('partial_preview') and not audit.get('audio_truncated'),
        'Incomplete/stale accepted group EDL')
    source=plan['source_binding'];video=media.job_file(root,source['source_video_path'])
    media.require(audit['source_video']['sha256']==source['source_video_sha256']
        and Path(edl['source_input']).resolve()==video.resolve(),'Source ownership mismatch')
    media.require(mixed['edl_sha256']==media.sha(paths['edit_decisions.json'])
        and mixed['master_sha256']==media.sha(paths['zh_mix.wav'])
        and master['premix_sha256']==media.sha(paths['premix.wav']),'Master/premix/EDL receipt mismatch')
    for name in ('zh_mix.wav','zh_mix.m4a'):
        media.require(master['outputs'][name]['sha256']==media.sha(paths[name]),'Current mastering output changed')
    size=export.get('size_limit',{})
    media.require(Path(export['output']).absolute()==paths['group_zh_candidate.mp4']
        and size.get('output_sha256')==media.sha(paths['group_zh_candidate.mp4'])
        and size.get('bytes')==paths['group_zh_candidate.mp4'].stat().st_size,'Current export/candidate mismatch')
    media.require(export['frames']==edl['frames'] and export['duration_s']==edl['duration_s'], 'Export EDL clock changed')
    groups=plan['tts_groups'];utterances=edl['utterances']
    media.require([u['group_id'] for u in utterances]==[g['group_id'] for g in groups],'EDL group order/coverage changed')
    proofs=[]
    for group,row in zip(groups,utterances):
        media.require(all(row.get(k)==group.get(k) for k in ('speaker_id','member_ids','text_zh','text_de')),
                      'Accepted group text/person changed')
        audio=media.job_file(root,row['audio']);side=media.job_file(root,row['generation_sidecar'])
        entry=media.read(side);content=entry['content'];ref=media.job_file(root,group['reference']['path'])
        media.require(media.sha(audio)==row['audio_sha256']==entry['sha256']
            and media.sha(side)==row['generation_sidecar_sha256']
            and content['text_zh']==group['text_zh']
            and content['effective_tts_text']==(group.get('tts_pronunciation_text') or group['text_zh'])
            and content['reference_sha256']==group['reference']['sha256']==media.sha(ref),
            'Accepted audio/reference identity changed')
        proofs.extend([{'path':str(audio),'sha256':entry['sha256']},{'path':str(side),'sha256':media.sha(side)}])
    protected={name:media.record(folder,path) for name,path in paths.items()}
    return {'schema':SCHEMA,'job':folder.name,'candidate':str(output),'files':protected,
        'plan':media.record(folder,plan_path),'source_segments_sha256':plan['source_segments']['sha256'],
        'source_video_sha256':source['source_video_sha256'],'accepted_group_audio':proofs,
        'prior_report':media.record(folder,output/'audio_master_recovery.json') if (output/'audio_master_recovery.json').is_file() else None},edl,mixed,master,export


def _copy_atomic(source,target):
    temporary=target.with_name('.'+target.name+'.master-recovery')
    if temporary.exists():temporary.unlink()
    try:os.link(source,temporary)
    except OSError:shutil.copyfile(source,temporary)
    os.replace(temporary,target)


def restore_pending(folder,output):
    marker=media.job_file(folder,output/'audio_master_recovery.pending.json',exists=False)
    if not marker.exists():return
    pending=media.read(marker)
    media.require(pending.get('schema')==SCHEMA and pending.get('candidate')==str(output),'Wrong pending master transaction')
    committed=next((e for e in media.journal(folder)['entries'] if e['token']==pending.get('token')),None)
    media.require(committed is not None and committed['action']=='encoded_master_repair'
        and committed['binding']['input']['candidate']==str(output),'Unbound pending master token')
    media.require(len(pending['targets'])==len(TARGETS)
        and {t['name'] for t in pending['targets']}==set(TARGETS),'Pending target whitelist/coverage changed')
    original=committed['binding']['input']
    directory=folder/'tts/encoded_master_recovery'/committed['token']
    for item in pending['targets']:
        name=item['name'];proof=original['files'].get(name) if name!='audio_master_recovery.json' else original.get('prior_report')
        media.require(item['before_sha256']==(proof['sha256'] if proof else None),'Pending original file binding changed')
        stage=media.job_file(folder,directory/'stage'/name)
        media.require(media.sha(stage)==item['after_sha256'],'Pending staged evidence changed')
        if proof:
            media.require(item['backup']=={'path':str((directory/'history'/name).relative_to(folder)),
                'sha256':proof['sha256']},'Pending backup identity/path changed')
            media.checked_record(folder,item['backup'])
        target=media.job_file(folder,output/name,exists=False)
        actual=media.sha(target) if target.is_file() else None
        media.require(actual in (item['before_sha256'],item['after_sha256']),'Concurrent delivery change before pending restore')
    if committed is not None and committed['status']=='complete':
        staged=media.read(directory/'stage/audio_master_recovery.json')
        media.require(staged==committed['result'],'Committed pending result changed')
        for item in pending['targets']:
            target=media.job_file(folder,output/item['name'])
            media.require(media.sha(target)==item['after_sha256'],'Committed master transaction was changed')
        marker.unlink()
        return
    for item in pending['targets']:
        target=media.job_file(folder,output/item['name'],exists=False)
        actual=media.sha(target) if target.is_file() else None
        media.require(actual in (item['before_sha256'],item['after_sha256']), 'Concurrent delivery change; cannot restore pending transaction')
        if item['before_sha256'] is not None:
            backup=media.checked_record(folder,item['backup']);_copy_atomic(backup,target)
        elif target.exists():target.unlink()
    marker.unlink()


def existing_result(root,folder,output):
    report_path=media.job_file(folder,output/'audio_master_recovery.json',exists=False)
    if not report_path.exists():return None
    result=media.read(report_path)
    if result.get('schema')!=SCHEMA or result.get('changed') is not True:return None
    media.require(result.get('job')==folder.name and result.get('candidate')==str(output),'Wrong master recovery receipt')
    for proof in result['outputs']:media.checked_record(folder,proof)
    plan_path=media.checked_record(folder,result['original_input']['plan']);media.plan_binding(root,folder,plan_path)
    for name in ('edit_decisions.json','premix.wav','group_adapter_report.json'):
        media.checked_record(folder,result['original_input']['files'][name])
    reservation=next((e for e in media.journal(folder)['entries'] if e['token']==result['token']),None)
    media.require(reservation is not None and reservation['action']=='encoded_master_repair'
        and reservation['binding']['input']==result['original_input'],'Missing/mismatched master recovery reservation')
    expected_paths={str((output/name).relative_to(folder)) for name in TARGETS[:-1]}
    media.require(len(result['outputs'])==len(expected_paths)
        and {p['path'] for p in result['outputs']}==expected_paths,'Incomplete master output proof coverage')
    core=lambda value:{k:v for k,v in value.items() if k!='reused'}
    if reservation['status']=='pending':
        staged=media.read(media.job_file(folder,'tts/encoded_master_recovery/'+result['token']+'/stage/audio_master_recovery.json'))
        media.require(core(staged)==core(result),'Uncommitted report differs from prepared result')
        media.finish_recovery(folder,result['token'],staged)
    else:media.require(core(reservation['result'])==core(result),'Completed master report was changed')
    for proof in result['original_input']['accepted_group_audio']:
        accepted_path=media.job_file(root,proof['path'])
        media.require(media.sha(accepted_path)==proof['sha256'],'Accepted source sound changed')
    result={**result,'reused':True};media.save(report_path,result)
    return result


def recover(root,folder,output):
    root,folder=media.scope(root,folder);output=Path(output).absolute()
    media.job_file(folder,output/'group_zh_candidate.mp4')
    restore_pending(folder,output)
    reused=existing_result(root,folder,output)
    if reused is not None:return reused
    binding,edl,mixed,master,export=bound_inputs(root,folder,output)
    video=output/'group_zh_candidate.mp4'
    diag=media.job_file(folder,'tts/encoded_master_diagnostics/'+binding['files']['group_zh_candidate.mp4']['sha256'],exists=False)
    before=scan(video,diag/'current_levels.stderr.log',output)
    diagnostic={'schema':'actual_encoded_levels_diagnostic_v1','video':binding['files']['group_zh_candidate.mp4'],
        'levels':before,'out_of_range':not levels_ok(before),'stderr':str(diag/'current_levels.stderr.log')}
    media.save(diag/'levels.json',diagnostic)
    if levels_ok(before):
        result={'schema':SCHEMA,'changed':False,'reused':False,'job':folder.name,'candidate':str(output),
            'output_sha256':media.sha(video),'master_sha256':mixed['master_sha256'],
            'edl_sha256':mixed['edl_sha256'],'levels_before':before,'reason':'actual_encoded_levels_already_in_range'}
        media.save(output/'audio_master_recovery.json',result)
        return result
    reserve_bytes=8*GIB+edl['frames']*1920*6+math.ceil(edl['duration_s']*48000)+video.stat().st_size+64*1024**2
    space(output,reserve_bytes)
    once={'source_video_sha256':binding['source_video_sha256'],'edl_sha256':mixed['edl_sha256'],
          'premix_sha256':master['premix_sha256'],'original_video_sha256':media.sha(video)}
    for entry in media.journal(folder)['entries']:
        if entry['action']=='encoded_master_repair':
            media.require(entry['binding'].get('once')==once,'Encoded master fallback already used for this delivery')
    reservation=media.reserve_recovery(folder,'encoded_master_repair',{'once':once,'input':binding})
    media.require(reservation['status']=='pending','Fixed encoded master fallback already completed/failed')
    directory=media.job_file(folder,'tts/encoded_master_recovery/'+reservation['token'],exists=False)
    stage=directory/'stage';history=directory/'history';stage.mkdir(parents=True,exist_ok=True);history.mkdir(parents=True,exist_ok=True)
    for name in FILES:
        original=output/name;backup=history/name
        if backup.exists():media.require(media.sha(backup)==binding['files'][name]['sha256'],'Original history changed')
        else:_copy_atomic(original,backup)
    media.save(directory/'request.json',{'schema':SCHEMA,'binding':binding,'levels_before':before,'once':once})
    old_probe=probe(video,stage/'old_probe.stderr.log',output);visual=video_identity(old_probe)
    media.require(int(visual['nb_frames'])==edl['frames'] and abs(visual['format_duration_s']-edl['duration_s'])<.08,
                  'Current video clock differs from accepted EDL')
    expected_frames=edl['frames']*1920
    media.require(audio_frames(output/'premix.wav',stage/'premix_probe.log',output)==expected_frames
        and audio_frames(output/'zh_mix.wav',stage/'master_probe.log',output)==expected_frames,'Premix/master PCM frames changed')
    measurement=scan(output/'premix.wav',stage/'premix_levels.stderr.log',output,-2.5)
    for name in ('input_i','input_tp','input_lra','input_thresh','target_offset'):
        media.require(math.isfinite(float(measurement[name])),'Invalid two-pass premix measurement')
    filt=('loudnorm=I=-16:TP=-2.5:LRA=11:linear=true:'+':'.join(
        target+'='+measurement[source] for target,source in [('measured_I','input_i'),('measured_TP','input_tp'),
        ('measured_LRA','input_lra'),('measured_thresh','input_thresh'),('offset','target_offset')]))
    command([FFMPEG,'-v','error','-threads','2','-filter_threads','2','-y','-i',output/'premix.wav',
        '-af',filt,'-ar','48000','-ac','2','-c:a','pcm_s24le',stage/'zh_mix.wav'],stage/'master.stderr.log',output)
    media.require(audio_frames(stage/'zh_mix.wav',stage/'new_pcm_probe.log',output)==expected_frames,'Two-pass mastering changed full PCM frames')
    command([FFMPEG,'-v','error','-threads','2','-y','-i',stage/'zh_mix.wav','-ar','48000','-ac','2',
        '-c:a','aac','-b:a','128k',stage/'zh_mix.m4a'],stage/'aac.stderr.log',output)
    aac_levels=scan(stage/'zh_mix.m4a',stage/'aac_levels.stderr.log',output)
    if not levels_ok(aac_levels):
        media.finish_recovery(folder,reservation['token'],{'schema':SCHEMA,'changed':False,
            'failure':'fixed_fallback_aac_still_out_of_range','levels':aac_levels})
        raise ValueError('Fixed fallback AAC is still outside encoded levels')
    video_hash=packet_hash(video,'v:0',stage/'video_packet_before.log',output)
    command([FFMPEG,'-v','error','-threads','2','-y','-i',video,'-i',stage/'zh_mix.m4a',
        '-map','0:v:0','-map','1:a:0','-map','0:s?','-map_metadata','0','-c','copy',
        '-metadata:s:a:0','language=zho','-disposition:a:0','default','-movflags','+faststart',stage/'group_zh_candidate.mp4'],stage/'remux.stderr.log',output)
    new_video=stage/'group_zh_candidate.mp4';new_visual=video_identity(probe(new_video,stage/'new_probe.log',output))
    media.require(visual==new_visual,'Remux changed video frames, timebase, duration or geometry')
    media.require(packet_hash(new_video,'v:0',stage/'video_packet_after.log',output)==video_hash,'Compressed H264 packet hash changed')
    new_audio_packet_hash=packet_hash(stage/'zh_mix.m4a','a:0',stage/'aac_packet.log',output)
    media.require(new_audio_packet_hash==
        packet_hash(new_video,'a:0',stage/'remux_audio_packet.log',output),'Remux changed the checked AAC payload')
    media.require(new_video.stat().st_size<50_000_000,'Remux exceeds strict50MB cap; no picture encoding authorized')
    final=scan(new_video,stage/'final_levels.stderr.log',output)
    media.require(levels_ok(final),'Final remux encoded levels still out of range')
    new_outputs={name:{'sha256':media.sha(stage/name),'bytes':(stage/name).stat().st_size} for name in ('zh_mix.wav','zh_mix.m4a')}
    new_master={'schema':'encoded_master_fallback_receipt_v1','requested_strategy':master.get('requested_strategy'),
        'actual_strategy':'legacy_loudnorm_encoded_headroom_v1','fallback_reason':'actual_postencode_levels_out_of_range',
        'premix_sha256':master['premix_sha256'],'levels_before':before,'levels_final':final,
        'two_pass_targets':{'I':-16,'TP':-2.5,'LRA':11,'aac_bitrate':'128k','sample_rate':48000,'channels':2},
        'measurement':measurement,'outputs':new_outputs,'original_receipt':media.record(folder,history/'mastering_receipt.json'),
        'pcm_frames':expected_frames,'subjective_quality_approved':False,'final_video_verification_pending':True,
        'actual_cache_identity':{'requested':master.get('identity'),'actual_strategy':'legacy_loudnorm_encoded_headroom_v1',
            'fallback_reason':'actual_postencode_levels_out_of_range','premix_sha256':master['premix_sha256']}}
    new_mix=copy.deepcopy(mixed);new_mix.update(master_sha256=new_outputs['zh_mix.wav']['sha256'],mastering=new_master,
        actual_strategy='legacy_loudnorm_encoded_headroom_v1',fallback_reason='actual_postencode_levels_out_of_range')
    new_export=copy.deepcopy(export);new_export['previous_export']=media.record(folder,history/'group_export.json')
    new_export['audio_master_recovery']={'mode':'video_packet_copy_audio_remux','source':str(video),
        'source_sha256':binding['files']['group_zh_candidate.mp4']['sha256'],'video_packet_sha256':video_hash,
        'video_reencoded':False,'new_render_claimed':False,'aac_sha256':new_outputs['zh_mix.m4a']['sha256']}
    new_export['size_limit']={'schema':'mp4_under_50_decimal_mb_v1',
        'previous_size_export':media.record(folder,history/'group_export.json'),
        'mode':'existing_compressed_h264_streamcopy_remux','source':str(history/'group_zh_candidate.mp4'),
        'source_sha256':binding['files']['group_zh_candidate.mp4']['sha256'],'source_bytes':video.stat().st_size,
        'output':str(video),'bytes':new_video.stat().st_size,'output_sha256':media.sha(new_video),
        'max_bytes_exclusive':50_000_000,'audio_mode':'new_checked_aac_packet_copy',
        'audio_packet_sha256':new_audio_packet_hash,'audio_packets_identical_to_new_aac':True,
        'video_packet_sha256':video_hash,'geometry_and_frame_count_preserved':True,'video_reencoded':False}
    for name,value in [('group_mix_report.json',new_mix),('mastering_receipt.json',new_master),('group_export.json',new_export)]:media.save(stage/name,value)
    result={'schema':SCHEMA,'changed':True,'reused':False,'job':folder.name,'candidate':str(output),'output':str(video),
        'token':reservation['token'],'original_input':binding,'levels_before':before,'levels_new_aac':aac_levels,
        'levels_final':final,'output_sha256':media.sha(new_video),'master_sha256':new_outputs['zh_mix.wav']['sha256'],
        'edl_sha256':mixed['edl_sha256'],'video_bitstream_unchanged':True,'video_packet_sha256':video_hash,
        'pcm_frames':expected_frames,'video_geometry_and_clock':visual,'history':str(history),
        'video_reencoded':False,'tts_or_translation_repeated':False,'full_verifier_required':True,'human_reviewed':False,
        'outputs':[{'path':str((output/name).relative_to(folder)),'sha256':media.sha(stage/name)} for name in TARGETS[:-1]]}
    media.save(stage/'audio_master_recovery.json',result)
    current,*_=bound_inputs(root,folder,output);media.require(current==binding,'Delivery/source changed before master commit')
    targets=[]
    for name in TARGETS:
        before_sha=media.sha(output/name) if (output/name).exists() else None
        if before_sha is not None and not (history/name).exists():_copy_atomic(output/name,history/name)
        targets.append({'name':name,'before_sha256':before_sha,'after_sha256':media.sha(stage/name),
            'backup':media.record(folder,history/name) if before_sha is not None else None})
    marker=output/'audio_master_recovery.pending.json'
    media.save(marker,{'schema':SCHEMA,'candidate':str(output),'token':reservation['token'],'targets':targets})
    try:
        for name in TARGETS:space(output,2*GIB);_copy_atomic(stage/name,output/name)
        media.finish_recovery(folder,reservation['token'],result)
        marker.unlink()
    except BaseException:
        restore_pending(folder,output);raise
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True);parser.add_argument('--job',required=True)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args(argv)
    result=recover(args.root,args.root/'jobs'/args.job,args.output)
    print(json.dumps(result,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
