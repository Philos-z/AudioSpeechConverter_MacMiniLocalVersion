"""Conservative CPU-only punctuation pause repair, preserving raw TTS caches.

Only accepts aligned ASR word boundaries with a nearby low-energy interval.
Any uncertain comma makes the complete utterance needs_tts_punctuation_regen;
no partly repaired WAV is emitted. No voice samples are deleted or resampled.
"""
import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

import numpy as np
try:
    import soundfile as sf
except ImportError:
    sf = None

VERSION = 'conservative_comma_pause_v3'


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, data):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix+'.partial')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n')
    temporary.replace(path)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def simplifier(root):
    try:
        from opencc import OpenCC
    except ImportError:
        # The existing pure-Python package is imported; no install or environment mutation.
        site_dirs = sorted((Path(root)/'vendor/index-tts/.venv/lib').glob('python*/site-packages'))
        previous = list(sys.path)
        try:
            sys.path.extend(str(x) for x in site_dirs)
            from opencc import OpenCC
        finally:
            sys.path[:] = previous
    return OpenCC('t2s').convert


def normalize(text, simplify):
    return ''.join(c for c in simplify(text) if c.isalnum())


def expected_commas(text, simplify):
    result = []
    for match in re.finditer('[，,]', text):
        before = normalize(text[:match.start()], simplify)
        after = normalize(text[match.end():], simplify)
        if before and after:
            result.append({'text_index': match.start(), 'character_boundary': len(before),
                           'left_context': before[-3:], 'right_context': after[:3]})
    return result


def select_latest_cache(root, job, row, active_profile):
    config_path=root/'config/local.json'
    if 'tts_preserve_commas' not in row and config_path.exists():
        if read_json(config_path).get('tts_punctuation_mode')=='preserve_commas':
            row={**row,'tts_preserve_commas':True}
    speakers = read_json(job/'speakers.json')
    ref = Path(speakers[row['speaker_id']]['reference'])
    if not ref.is_absolute():ref = root/ref
    reference_sha = sha256(ref)
    candidates = []
    for metadata in (job/'tts/cache').glob(row['segment_id']+'_*.json'):
        info = read_json(metadata)
        if info.get('text') != row['text_zh']:
            continue
        profile = info.get('profile')
        if active_profile is not None and profile != active_profile:
            continue
        payload = {'text': row['text_zh'], 'reference_sha256': reference_sha, 'profile': profile}
        if row.get('tts_pronunciation_text'):
            payload['tts_text_override'] = row['tts_pronunciation_text']
        if row.get('tts_preserve_commas') is True:
            payload['punctuation_mode'] = 'preserve_commas'
        key = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        wav = metadata.with_suffix('.wav')
        if key != info.get('cache_key') or not wav.exists():
            continue
        if not wav.name.startswith(row['segment_id']+'_'+key[:14]):
            continue
        if info.get('sha256') != sha256(wav):
            continue
        candidates.append((metadata.stat().st_mtime_ns, wav, info))
    if not candidates:
        raise ValueError('No current text/profile/reference cache matched: '+row['segment_id'])
    candidates.sort(reverse=True, key=lambda x: x[0])
    return candidates[0][1:]


def _sequence_mapping(expected, recognized):
    mapping={}
    matcher=difflib.SequenceMatcher(None,expected,recognized,autojunk=False)
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):mapping[block.a+offset]=block.b+offset
    return mapping,len(mapping)/max(1,len(expected))


def _unique_window(sequence,window):
    if not window:return False
    return sum(sequence[i:i+len(window)]==window
               for i in range(len(sequence)-len(window)+1))==1


def _han(character):
    return '\u3400'<=character<='\u9fff' or '\U00020000'<=character<='\U0003134f'


def _phonetic_tokens(text):
    # Timing alignment only: no rewriting or pronunciation-quality score.
    from pypinyin import lazy_pinyin,Style
    tokens=lazy_pinyin(text,style=Style.NORMAL,errors=lambda text:list(text))
    return tokens if len(tokens)==len(text) else None


def aligned_boundaries(expected, words, simplify, commas):
    chars=[]
    for word_index,word in enumerate(words):
        text=normalize(word['word'],simplify)
        for index,character in enumerate(text):
            chars.append({'char':character,'word_index':word_index,
                          'first':index==0,'last':index==len(text)-1})
    recognized=''.join(c['char'] for c in chars)
    character_map,matched_fraction=_sequence_mapping(expected,recognized)
    # Compute this lazily only after an actual character-alignment failure.
    phonetics=None
    outcomes=[]
    for comma in commas:
        base=dict(comma);boundary=comma['character_boundary']
        character_reason=None
        selected=None
        for method in ['character','phonetic_timing_only']:
            item=dict(base);reason=None
            if method=='character':
                mapping,fraction=character_map,matched_fraction
                expected_tokens,recognized_tokens=expected,recognized
            else:
                if phonetics is None:
                    e,r=_phonetic_tokens(expected),_phonetic_tokens(recognized)
                    phonetics=(e,r,*_sequence_mapping(e,r)) if e is not None and r is not None else False
                if phonetics is False:
                    item.update(alignment_status='phonetic_mapping_not_one_character_per_token',
                                character_alignment_status=character_reason)
                    selected=item;break
                expected_tokens,recognized_tokens,mapping,fraction=phonetics
                item.update(phonetic_match_fraction=fraction,
                            character_alignment_status=character_reason)
            item['alignment_method']=method
            if fraction<.90:reason='global_'+method+'_match_below_0.90'
            left,right=mapping.get(boundary-1),mapping.get(boundary)
            if reason is None and (left is None or right is None or right!=left+1):
                reason='comma_adjacent_tokens_not_exactly_aligned'
            context=range(max(0,boundary-3),min(len(expected_tokens),boundary+3))
            if reason is None and any(index not in mapping for index in context):
                reason='local_context_not_exactly_aligned'
            if reason is None and any(mapping[index]!=left+index-(boundary-1) for index in context):
                reason='local_context_not_contiguous'
            if reason is None and method=='phonetic_timing_only':
                if len(context)!=6:
                    reason='phonetic_context_requires_six_syllables'
                else:
                    first=context.start;last=context.stop
                    rec_first=mapping[first];rec_last=mapping[last-1]+1
                    window=expected_tokens[first:last]
                    item['phonetic_context']=window
                    # Numeric/Latin transliteration is never guessed. Only a unique
                    # six-Han-character syllable window may locate a comma.
                    if not all(_han(c) for c in expected[first:last]+recognized[rec_first:rec_last]):
                        reason='phonetic_context_contains_numeric_or_latin_text'
                    elif not (_unique_window(expected_tokens,window) and _unique_window(recognized_tokens,window)):
                        reason='phonetic_context_not_unique_in_both_sequences'
                    else:item['phonetic_context_unique_in_both']=True
            if reason is None and (not chars[left]['last'] or not chars[right]['first']):
                reason='comma_inside_recognized_word_not_safe_to_split'
            if reason is None:
                left_word=words[chars[left]['word_index']]
                right_word=words[chars[right]['word_index']]
                minimum=min(left_word['probability'],right_word['probability'])
                item.update(left_word=left_word,right_word=right_word,
                            asr_gap_s=right_word['start']-left_word['end'],
                            boundary_probability_min=minimum)
                exact_override=fraction>=.97 and len(context)==6
                item['token_probability_override_by_exact_context']=bool(exact_override and minimum<.65)
                if minimum<.65 and not exact_override:
                    reason='adjacent_word_probability_below_0.65_without_high_exact_match'
                elif right_word['start']<left_word['end']-.015:
                    reason='overlapping_asr_word_times'
            item['alignment_status']=reason or 'trusted_word_boundary'
            if reason is None:
                selected=item;break
            if method=='character':character_reason=reason
            selected=item
        outcomes.append(selected)
    return outcomes,recognized,matched_fraction


def find_low_energy_cut(audio, sr, boundary, target_s):
    mono = np.mean(audio, axis=1) if audio.ndim == 2 else audio
    left, right = boundary['left_word']['end'], boundary['right_word']['start']
    midpoint = (left+right)/2
    # Stay near the ASR boundary; never search more widely into an adjacent word.
    search_lo = max(0., midpoint-.025, left-.015)
    search_hi = min(len(mono)/sr, midpoint+.025, right+.015)
    if search_hi <= search_lo:return {'status': 'invalid_boundary_interval'}
    centers = np.arange(round(search_lo*sr), round(search_hi*sr)+1, max(1, round(.001*sr)))
    half_window = max(1, round(.003*sr))
    def energy(index):
        window = mono[max(0,index-half_window):min(len(mono),index+half_window)]
        return float(np.sqrt(np.mean(window.astype(np.float64)**2))) if len(window) else 1.
    values = [energy(int(index)) for index in centers]
    index = int(centers[int(np.argmin(values))]); minimum = min(values)
    neighborhood = mono[max(0,index-round(.20*sr)):min(len(mono),index+round(.20*sr))]
    local_rms = float(np.sqrt(np.mean(neighborhood.astype(np.float64)**2)))
    # A genuinely quiet boundary is required both in absolute and relative terms.
    threshold = min(10**(-30/20), max(10**(-45/20), local_rms*.20))
    if minimum > threshold:
        return {'status': 'no_low_energy_boundary', 'minimum_rms': minimum,
                'threshold_rms': threshold, 'candidate_s': index/sr}
    step = max(1, round(.001*sr)); lower = upper = index
    # Measure only the contiguous quiet interval around the cut, up to 250ms.
    while lower-step >= max(0,index-round(.25*sr)) and energy(lower-step) <= threshold:lower -= step
    while upper+step < min(len(mono),index+round(.25*sr)) and energy(upper+step) <= threshold:upper += step
    existing = (upper-lower)/sr
    added_samples = max(0, round((target_s-existing)*sr))
    return {'status': 'already_has_pause' if added_samples == 0 else 'insert_pause',
            'cut_sample': index, 'cut_s': index/sr, 'existing_quiet_s': existing,
            'added_samples': added_samples, 'added_s': added_samples/sr,
            'minimum_rms': minimum, 'threshold_rms': threshold}


def insert_silence(audio, sr, cuts):
    result = np.array(audio, copy=True)
    fade_samples = max(1, round(.002*sr))
    for cut in sorted(cuts, key=lambda x:x['cut_sample'], reverse=True):
        index, extra = cut['cut_sample'], cut['added_samples']
        if not extra:continue
        left_n, right_n = min(fade_samples,index), min(fade_samples,len(result)-index)
        shape = (-1,1) if result.ndim == 2 else (-1,)
        if left_n:result[index-left_n:index] *= np.linspace(1,0,left_n).reshape(shape)
        if right_n:result[index:index+right_n] *= np.linspace(0,1,right_n).reshape(shape)
        silence = np.zeros((extra,)+result.shape[1:], dtype=result.dtype)
        result = np.concatenate((result[:index], silence, result[index:]), axis=0)
    return result


def process_one(root, job, row, model, simplify, destination, target_s=.18):
    progress_path = job/'progress.json'
    profile = read_json(progress_path).get('profile') if progress_path.exists() else None
    source, cache = select_latest_cache(root,job,row,profile)
    signature = hashlib.sha256(json.dumps({'version':VERSION,'raw_cache_key':cache['cache_key'],
        'raw_sha256':cache['sha256'],'expected':row['text_zh'],'target_pause_s':target_s},
        sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    base = Path(destination)/(row['segment_id']+'_'+signature[:14])
    meta_path, wav_path = base.with_suffix('.json'), base.with_suffix('.wav')
    if meta_path.exists():
        prior = read_json(meta_path)
        if prior.get('signature') == signature and (prior.get('status') != 'repaired' or
            wav_path.exists() and sha256(wav_path) == prior.get('output_sha256')):
            return prior
    report = {'version':VERSION,'signature':signature,'id':row['segment_id'],'text':row['text_zh'],
              'raw_file':str(source),'raw_cache_key':cache['cache_key'],'raw_sha256':cache['sha256'],
              'target_pause_s':target_s,'asr_device':'cpu','asr_compute_type':'int8',
              'subjective_listening_performed':False,'raw_duration_s':cache.get('duration_s')}
    commas = expected_commas(row['text_zh'],simplify)
    if not commas:
        report.update(status='no_comma',output_file=str(source),added_duration_s=0,duration_s=cache.get('duration_s'))
        write_json(meta_path,report);return report
    segments, info = model.transcribe(str(source),language='zh',beam_size=3,
        word_timestamps=True,vad_filter=False,condition_on_previous_text=False,
        initial_prompt=None)
    words = [{'word':w.word,'start':w.start,'end':w.end,'probability':w.probability}
             for segment in segments for w in (segment.words or [])]
    boundaries, recognized, fraction = aligned_boundaries(normalize(row['text_zh'],simplify),
                                                         words,simplify,commas)
    samples, sr = sf.read(source,dtype='float32',always_2d=False)
    all_trusted = True;cuts=[]
    for boundary in boundaries:
        if boundary['alignment_status'] != 'trusted_word_boundary':
            boundary['status']='needs_tts_punctuation_regen';all_trusted=False;continue
        quiet=find_low_energy_cut(samples,sr,boundary,target_s);boundary.update(quiet)
        if quiet['status'] not in ('insert_pause','already_has_pause'):
            all_trusted=False;boundary['status']='needs_tts_punctuation_regen'
        else:cuts.append(quiet)
    report.update({'recognized':recognized,'character_match_fraction':fraction,'words':words,
                   'commas':boundaries,'sample_rate':sr,'input_samples':len(samples)})
    if not all_trusted:
        report.update(status='needs_tts_punctuation_regen',output_file=None,added_duration_s=0,
                      reason='One or more commas lack an exact word boundary and near-silence; no partial WAV emitted.')
    else:
        result=insert_silence(samples,sr,cuts)
        Path(destination).mkdir(parents=True,exist_ok=True)
        sf.write(wav_path,result,sr,subtype='PCM_24')
        added=(len(result)-len(samples))/sr
        report.update(status='repaired',output_file=str(wav_path.resolve()),output_sha256=sha256(wav_path),
                      output_samples=len(result),added_duration_s=added,duration_s=len(result)/sr,
                      voice_samples_deleted=0,rate_or_pitch_changed=False,fade_ms=2)
    write_json(meta_path,report);return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True);parser.add_argument('--job',required=True)
    parser.add_argument('--ids',nargs='+');parser.add_argument('--output-dir',type=Path)
    parser.add_argument('--target-pause',type=float,default=.18)
    parser.add_argument('--watch',action='store_true',help='Wait for each current text/profile cache to become available')
    parser.add_argument('--watch-timeout',type=float,default=3600)
    args=parser.parse_args()
    if not .12 <= args.target_pause <= .25:parser.error('Conservative target range is 0.12–0.25s')
    os.environ['HF_HUB_OFFLINE']='1';os.environ['TRANSFORMERS_OFFLINE']='1'
    root=args.root.resolve();job=root/'jobs'/args.job
    global sf
    if sf is None:
        import importlib
        old_path=list(sys.path)
        try:
            sys.path.extend(str(x) for x in sorted((root/'vendor/index-tts/.venv/lib').glob('python*/site-packages')))
            sf=importlib.import_module('soundfile')
        finally:
            sys.path[:]=old_path
    model_path=Path(read_json(root/'config/local.json')['asr_model'])
    if not model_path.is_dir():raise ValueError('LOCAL_ASR_MODEL_MISSING; downloads forbidden')
    from faster_whisper import WhisperModel
    model=WhisperModel(str(model_path),device='cpu',compute_type='int8',cpu_threads=2,num_workers=1,
                       local_files_only=True)
    simplify=simplifier(root)
    rows=read_json(job/'segments.json')['segments'];wanted=set(args.ids or [])
    output=args.output_dir or job/'tts/punctuated';reports={}
    rows=[row for row in rows if not wanted or row['segment_id'] in wanted]
    began=time.monotonic();deadline=began+args.watch_timeout
    while len(reports)<len(rows):
        progressed=False
        for row in rows:
            if row['segment_id'] in reports:continue
            try:
                report=process_one(root,job,row,model,simplify,output,args.target_pause)
            except ValueError as error:
                if args.watch and str(error).startswith('No current text/profile/reference cache matched:'):continue
                raise
            reports[row['segment_id']]=report;progressed=True
            print(json.dumps({'id':row['segment_id'],'status':report['status'],
                'added_duration_s':report.get('added_duration_s'),'commas':report.get('commas',[])},ensure_ascii=False),flush=True)
            write_json(output/'punctuation_report.json',{'version':VERSION,'utterances':list(reports.values()),
                'pending_ids':[r['segment_id'] for r in rows if r['segment_id'] not in reports],
                'elapsed_s':time.monotonic()-began,
                'caveat':'ASR and energy gates are technical heuristics, not a subjective naturalness guarantee.'})
        if len(reports)==len(rows):break
        if not args.watch or time.monotonic()>=deadline or (output/'stop').exists():break
        if not progressed:time.sleep(2)
    pending=[r['segment_id'] for r in rows if r['segment_id'] not in reports]
    print(json.dumps({'processed':len(reports),'total':len(rows),'pending_ids':pending,
        'needs_tts_punctuation_regen':[i for i,r in reports.items() if r['status']=='needs_tts_punctuation_regen'],
        'added_duration_s':sum(r.get('added_duration_s',0) for r in reports.values())},ensure_ascii=False),flush=True)
    if pending:raise SystemExit(2)

if __name__=='__main__':main()
