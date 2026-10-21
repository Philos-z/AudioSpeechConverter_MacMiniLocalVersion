"""Explicit OPTIONAL constant-master candidate, not the default/old equivalent.

Call only from a new output identity. No TTS, Proxy, source-timing, per-utterance
or background policy is touched. Only mathematical input infeasibility invokes
the caller's unchanged legacy mastering callback; operational/QA failures stop.
The existing final video verifier must still run after this helper.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

VERSION='optional_constant_master_candidate_v1'
LEGACY='legacy_loudnorm_two_pass_v1'
FFMPEG='/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg'

class ConstantInfeasible(ValueError):pass


def digest(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):value.update(block)
    return value.hexdigest()

def save(path,value):
    path=Path(path);tmp=path.with_name(path.name+'.partial')
    tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');tmp.replace(path)

def identity(helper_path=None,legacy_code_path=None):
    """Bind both export output identity AND mix phase identity before lookup."""
    return {'strategy':VERSION,'helper_sha256':digest(helper_path or __file__),
      'legacy_code_sha256':digest(legacy_code_path) if legacy_code_path else None,
      'integrated_target_lufs':-16,'integrated_tolerance_lu':1,'master_peak_target_dbtp':-2,
      'master_extra_headroom_db':.05,'encoded_peak_max_dbtp':-1.5,'lra_max_lu':11,
      'sample_rate':48000,'channels':2,'pcm_subtype':'PCM_24','aac_bitrate':'128k',
      'gain_rule':'min(-16-I,-2.05-true_peak_rounding_upper_dbtp)',
      'candidate_is_behavior_change':True,'fallback_policy':'input_infeasible_only_then_unchanged_legacy',
      'original_final_video_verifier_required':True}

def parse_measurement(text):
    latest={};peak=0.
    for block in re.split(r'(?m)^frame:\d+.*\n',text):
        values=dict(re.findall(r'(?m)^(lavfi\.r128\.[^=\s]+)=([^\s]+)\s*$',block))
        if 'lavfi.r128.I' in values:latest=values
        for key,value in values.items():
            if re.fullmatch(r'lavfi\.r128\.true_peaks_ch\d+',key):
                number=float(value)
                if not math.isfinite(number) or number<0:raise ValueError('Invalid measured peak amplitude')
                peak=max(peak,number)
    if not latest or peak<=0:raise ValueError('Missing actual complete ebur128 measurement')
    i=float(latest['lavfi.r128.I']);lra=float(latest['lavfi.r128.LRA'])
    if not all(math.isfinite(v) for v in (i,lra)) or i<=-70 or lra<0:raise ValueError('Silent or invalid premix measurement')
    return {'integrated_lufs':i,'lra_lu':lra,'rounded_peak_amplitude':peak,
      'true_peak_rounding_upper_dbtp':20*math.log10(peak+.0005),'actual_ebur128_scan':True}

def gain_for(measured):
    i=measured['integrated_lufs'];tp=measured['true_peak_rounding_upper_dbtp'];lra=measured['lra_lu']
    if not all(math.isfinite(v) for v in (i,tp,lra)) or i<=-70 or lra<0:raise ValueError('Invalid actual input levels')
    if lra>11:raise ConstantInfeasible('source_lra_above_11')
    gain=min(-16-i,-2.05-tp)
    if abs(i+gain+16)>1:raise ConstantInfeasible('no_single_gain_satisfies_lufs_and_master_peak')
    return {'gain_db':gain,'factor':10**(gain/20),'predicted_lufs':i+gain,
       'predicted_master_true_peak_upper_dbtp':tp+gain,'single_constant':True,'dynamic_processing':False}

def pcm_check(source,output,factor):
    import numpy as np
    import soundfile as sf
    total=0;peak=0.;error_sq=0.
    with sf.SoundFile(source) as a,sf.SoundFile(output) as b:
        if (a.frames,a.samplerate,a.channels)!=(b.frames,b.samplerate,b.channels) or a.samplerate!=48000 or a.channels!=2:
            raise ValueError('Candidate changed full-track frames/rate/channels')
        if a.subtype!='FLOAT' or b.subtype!='PCM_24':raise ValueError('Expected float premix and PCM24 master')
        frames=a.frames
        while True:
            raw=a.read(65536,dtype='float64',always_2d=True)
            if not len(raw):break
            target=b.read(len(raw),dtype='float64',always_2d=True)
            if not np.isfinite(raw).all() or not np.isfinite(target).all():raise ValueError('Nonfinite whole-track samples')
            error=target-raw*factor;peak=max(peak,float(np.max(np.abs(error))));error_sq+=float(np.sum(error*error));total+=raw.size
    if total<1 or peak>2**-22:raise ValueError('Master violates one constant beyond PCM24 rounding')
    return {'frames':frames,'samples':total,'sample_rate':48000,'channels':2,'maximum_residual':peak,
      'rms_residual':math.sqrt(error_sq/total),'tolerance':2**-22,'single_constant_all_samples_verified':True,
      'relative_dialogue_background_gain_unchanged':True,'no_frame_change':True}

def _command(source,filtergraph,output=None,codec=None):
    cmd=[FFMPEG,'-hide_banner','-nostdin','-nostats','-threads','2','-filter_threads','2','-i',str(source),'-map','0:a:0']
    if filtergraph:cmd+=['-af',filtergraph]
    if output is None:return cmd+['-f','null','-']
    cmd+=['-ar','48000','-c:a',codec or 'pcm_s24le']
    if codec=='aac':cmd+=['-b:a','128k']
    return cmd+['-n',str(output)]

def _run(command,label,out):
    result=subprocess.run(command,cwd=out,capture_output=True,text=True,timeout=180)
    (out/(label+'.stderr.log')).write_text(result.stderr)
    if result.returncode:raise RuntimeError(label+' failed; inspect saved stderr')
    return result

def master_optional(premix,out,legacy_master,*,legacy_code_path,runner=None):
    """Trusted application hook, invoked ONLY for explicit optional selection.

    legacy_master is the application's existing unmodified three-command path.
    It must create zh_mix.wav/m4a. runner injection is for CPU contract tests,
    not a UI/API/AI-supplied command, policy, model or threshold.
    """
    premix=Path(premix).resolve();out=Path(out).resolve();runner=runner or _run
    if premix.parent!=out or not premix.is_file():raise ValueError('Premix must belong to exact new output folder')
    if any((out/name).exists() for name in ('verification.json','group_verification.json','compact_export.json','group_export.json')):
        raise ValueError('Refuse published/rendered output; require a new candidate directory')
    receipt_path=out/'mastering_receipt.json';source_sha=digest(premix);began=time.perf_counter();timing={}
    requested_identity=identity(legacy_code_path=legacy_code_path)
    receipt={'schema':'optional_mastering_receipt_v1','identity':requested_identity,'requested_strategy':VERSION,
      'actual_strategy':None,'status':'started','premix_sha256':source_sha,'constant_candidate_passed':False,'constant_candidate_pcm_passed':False,
      'final_video_verification_pending':True,'final_encoded_verification_pending':True,'encoded_loudness_scans_in_helper':0,'subjective_quality_approved':False,'full_workflow_80_verified':False}
    save(receipt_path,receipt)
    token=uuid.uuid4().hex[:12];wav=out/('.constant_'+token+'.wav');aac=out/('.constant_'+token+'.m4a')
    metadata_name='.constant_'+token+'_ebur128.txt'
    try:
        t=time.perf_counter()
        filt='ebur128=peak=true:metadata=1:framelog=quiet,ametadata=print:file='+metadata_name
        runner(_command(premix,filt),'mastering_actual_ebur128',out);timing['actual_input_scan_s']=time.perf_counter()-t
        measured=parse_measurement((out/metadata_name).read_text());receipt['input_measurement']=measured
        try:gain=gain_for(measured)
        except ConstantInfeasible as exc:
            receipt.update(actual_strategy=LEGACY,status='original_strategy_used_constant_infeasible',fallback_reason=str(exc),
                 constant_candidate_passed=False,legacy_encoded_levels_verified_here=False)
            t=time.perf_counter();legacy_master();timing['unchanged_legacy_mastering_s']=time.perf_counter()-t
        else:
            receipt['gain']=gain
            t=time.perf_counter();runner(_command(premix,f"volume={gain['gain_db']:.12f}dB:precision=double",wav),'mastering_constant_render',out);timing['constant_render_s']=time.perf_counter()-t
            t=time.perf_counter();runner(_command(wav,None,aac,'aac'),'mastering_same_aac',out);timing['same_aac_encode_s']=time.perf_counter()-t
            t=time.perf_counter();receipt['pcm_proof']=pcm_check(premix,wav,gain['factor']);timing['whole_pcm_constant_check_s']=time.perf_counter()-t
            # No partially checked candidate replaces the expected master names.
            if digest(premix)!=source_sha:raise ValueError('Premix changed during mastering')
            os.replace(wav,out/'zh_mix.wav');os.replace(aac,out/'zh_mix.m4a')
            receipt.update(actual_strategy=VERSION,status='constant_pcm_and_aac_generated_final_encoded_check_pending',constant_candidate_pcm_passed=True,constant_candidate_passed=False)
        if digest(premix)!=source_sha:raise ValueError('Premix changed during mastering')
        receipt['outputs']={name:{'sha256':digest(out/name),'bytes':(out/name).stat().st_size} for name in ('zh_mix.wav','zh_mix.m4a')}
        receipt['actual_cache_identity']={'requested':requested_identity,'actual_strategy':receipt['actual_strategy'],
          'fallback_reason':receipt.get('fallback_reason'),'premix_sha256':source_sha}
        receipt['timing_s']=timing;receipt['wall_s']=time.perf_counter()-began
        save(receipt_path,receipt);return receipt
    except Exception as exc:
        receipt.update(status='failed_safe_stop',constant_candidate_passed=False,constant_candidate_pcm_passed=False,error_type=type(exc).__name__,error=str(exc),timing_s=timing,wall_s=time.perf_counter()-began)
        save(receipt_path,receipt);raise
