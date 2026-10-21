"""Select verified pause audio without modifying the original natural TTS timeline."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

import soundfile as sf
from comma_chunk_tts import trim_generated_silence

def load(path):
    return json.loads(Path(path).read_text())

def write(path, value):
    path=Path(path); temporary=path.with_suffix('.partial.json')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');temporary.replace(path)

def main():
    p=argparse.ArgumentParser();p.add_argument('--job',type=Path,required=True);args=p.parse_args()
    job=args.job;raw=load(job/'timeline.json');base=load(job/'tts/punctuated/punctuation_report.json')
    again=load(job/'tts/punctuated_v3/punctuation_report.json')
    reviewed={row['segment_id']:row for row in load(job/'segments.json')['segments']}
    if base['pending_ids'] or again['pending_ids']:raise ValueError('Pause audit incomplete')
    reports={r['id']:r for r in base['utterances']}
    for r in again['utterances']:
        if r['status']=='repaired': reports[r['id']]=r
    rows=[];audit=[]
    for original in raw['segments']:
        identifier=original['segment_id'];report=reports[identifier]
        current=reviewed[identifier]
        for field in ['segment_id','speaker_id','start_s','end_s','text_de']:
            if current[field]!=original[field]:raise ValueError('Unexpected non-text revision: '+identifier)
        expected_text=current['text_zh']
        mode=report['status']
        if mode=='needs_tts_punctuation_regen':
            report=load(job/'tts/comma_chunks/final'/f'{identifier}.json')
            if report['status']!='comma_chunked':raise ValueError('Fallback not complete: '+identifier)
            if report['raw_source_text']!=expected_text:raise ValueError('Fallback text mismatch')
            mode='comma_chunked'
            for join in report['join_info']['joins']:
                if abs(join['total_quiet_gap_s']-.18) > 1/22050+1e-8:
                    raise ValueError('Unexpected comma pause: '+identifier)
        else:
            if report['text']!=expected_text or report['raw_cache_key']!=original['cache_key']:
                raise ValueError('Pause report does not match current text/cache: '+identifier)
        audio=Path(report['output_file']);digest=hashlib.sha256(audio.read_bytes()).hexdigest()
        expected=report.get('output_sha256') or report.get('sha256')
        if expected and expected!=digest:raise ValueError('Audio SHA mismatch: '+identifier)
        info=sf.info(audio);duration=info.frames/info.samplerate
        if abs(duration-report['duration_s'])>1/info.samplerate+1e-8:raise ValueError('Audio duration mismatch')
        if mode=='repaired' and report.get('voice_samples_deleted')!=0:raise ValueError('Unsafe DSP audio')
        # The edit's 350ms tail starts after the voice, rather than after long
        # generated edge silence. Keep 20ms protection and all audible samples.
        source_audio=str(audio);source_digest=digest
        samples,sr=sf.read(audio,dtype='float32')
        trimmed,edge_trim=trim_generated_silence(samples,sr)
        if edge_trim['trim_head_samples'] or edge_trim['trim_tail_samples']:
            folder=job/'tts/final_paused';folder.mkdir(exist_ok=True)
            audio=folder/f'{identifier}_{digest[:14]}.wav'
            sf.write(audio,trimmed,sr,subtype='PCM_24')
            digest=hashlib.sha256(audio.read_bytes()).hexdigest()
            duration=len(trimmed)/sr
        row={**original,'text_zh':expected_text,'audio':str(audio.resolve()),'duration_s':duration,
             'render_end_s':original['render_start_s']+duration,
             'punctuation_method':mode,'audio_sha256':digest,
             'edge_quiet_head_s':edge_trim['quiet_head_guard_samples']/sr,
             'edge_quiet_tail_s':edge_trim['quiet_tail_guard_samples']/sr}
        rows.append(row)
        audit.append({'id':identifier,'method':mode,'text':expected_text,
                      'duration_s':duration,'audio':str(audio),'sha256':digest,
                      'pause_source_audio':source_audio,'pause_source_sha256':source_digest,
                      'edge_silence_trim':edge_trim,
                      'commas':report.get('commas',[]),'join_info':report.get('join_info')})
    if not rows or len(rows)!=len(raw['segments']) or len({r['segment_id'] for r in rows})!=len(rows):
        raise ValueError('Missing/repeated rows')
    result={**raw,'segments':rows,'issues':[],
            'method':'Final pause audio; source starts retained solely for speech-led EDL construction',
            'original_timeline':str(job/'timeline.json')}
    write(job/'timeline.punctuated.json',result)
    write(job/'punctuation_audit.json',{'utterance_count':len(rows),'counts':dict(Counter(r['method'] for r in audit)),
          'comma_pause_target_s':.18,'original_audio_preserved':True,'tempo_or_pitch_changed':False,
          'subjective_full_listening_performed':False,'utterances':audit})
    print('FINAL_PAUSE_TIMELINE_READY',dict(Counter(r['method'] for r in audit)),flush=True)

if __name__=='__main__':main()
