"""Job-scoped CPU rechecks for actual translation rejections; no downloads."""
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess


def lexical_hints(exact, contextual, context):
    """A bounded idiom hint for a real second decode, never a text rewrite."""
    phrase = r'\bGeldhang\s+zugemacht\s+werden\b'
    if (re.search(phrase, exact, re.I) and re.search(phrase, contextual, re.I)
            and re.search(r'\b(?:Länderfinanzausgleich|Finanzausgleich|Finanzierung|Geldmittel)\b', context, re.I)):
        return ['Geldhahn']
    return []


def _hash(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024**2), b''):
            h.update(block)
    return h.hexdigest()


def slot_tail_end(row, rows, duration):
    """Use only the row's already allocated trailing gap, never a neighbor."""
    end = row['end_s']
    slot = row.get('slot_end_s', end)
    following = min((r['start_s'] for r in rows if r['start_s'] > row['start_s']), default=duration)
    if (row.get('overlap') is False and type(slot) in (int, float)
            and math.isfinite(slot) and end < slot <= min(end+.75, following, duration)):
        return slot
    return end


def slot_head_start(row, rows):
    start = row['start_s']
    previous = max((r for r in rows if r['start_s'] < start), key=lambda r:r['start_s'], default=None)
    if row.get('overlap') is not False or previous is None or previous.get('overlap') is not False:
        return start
    boundary = previous.get('slot_end_s', previous['end_s']+.06)
    if (type(boundary) in (int,float) and math.isfinite(boundary)
            and previous['end_s'] <= boundary < start):
        return max(boundary, start-1.)
    return start


def exact_window_matches(proof, row, rows, duration):
    if proof.get('method') == 'complete_existing_source_slot_without_neighbor_speech':
        left, right = proof.get('window_start_s', -1), proof.get('window_end_s', -1)
        return (proof.get('no_neighbor_sentence_included') is True
                and proof.get('original_start_s') == row['start_s']
                and proof.get('original_end_s') == row['end_s']
                and slot_head_start(row,rows)-1e-6 <= left <= row['start_s']
                and row['end_s'] <= right <= slot_tail_end(row,rows,duration)+1e-6)
    if abs(proof.get('window_start_s', -1)-row['start_s']) > 1e-6:
        return False
    end = proof.get('window_end_s', -1)
    if abs(end-row['end_s']) <= 1e-6:
        return True
    return (proof.get('method') == 'complete_existing_source_slot_without_next_sentence'
            and proof.get('no_next_sentence_included') is True
            and proof.get('original_end_s') == row['end_s']
            and row['end_s'] < end <= slot_tail_end(row, rows, duration)+1e-6)


def recheck(root, folder, rows, evidence_dir, source_sha256):
    """Offer exact, locally recognized alternatives, never verified source words.

    A model-selected source must equal a complete alternative returned here.
    Context words are filtered by their actual absolute word midpoints so a
    neighboring sentence cannot silently be imported into this row.
    """
    import website_recovery as recovery
    root, folder = Path(root).resolve(), Path(folder).resolve()
    source = recovery._read(recovery._safe(folder, 'source.json'))
    video = Path(source.get('input', ''))
    ancestor = next((p for p in video.parents if p.resolve() == root), None)
    if not video.is_absolute() or ancestor is None:
        raise ValueError('局部复核原片位置不合法')
    video = recovery._safe(root, video.relative_to(ancestor))
    if _hash(video) != source_sha256:
        raise ValueError('局部复核原片身份不符')
    duration, track = source.get('duration_s'), source.get('audio_track')
    if (type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0
            or type(track) is not int or track < 0 or not 1 <= len(rows) <= 8):
        raise ValueError('局部复核范围不合法')
    # An installed medium model is preferred for a second decoding pass. An
    # absent cache falls back to the installed production model, never a fetch.
    candidates = sorted((Path.home()/'.cache/huggingface/hub/models--Systran--faster-whisper-medium/snapshots').glob('*'))
    config = recovery._read(root/'config/local.json')
    model_path = next((p for p in candidates if (p/'model.bin').is_file()), Path(config['asr_model']))
    if not (model_path/'model.bin').is_file():
        raise RuntimeError('缺少已安装的局部复核模型，未下载新模型')
    model_hash = _hash(model_path/'model.bin')
    ffmpeg = shutil.which('ffmpeg') or '/opt/homebrew/bin/ffmpeg'
    from faster_whisper import WhisperModel
    model = WhisperModel(str(model_path.resolve()), device='cpu', compute_type='int8',
                         cpu_threads=2, num_workers=1, local_files_only=True)
    result = {}
    _, all_rows = recovery._rows(folder)
    for row in rows:
        sid = row['segment_id']
        start, end = row.get('start_s'), row.get('end_s')
        if (type(start) not in (int, float) or type(end) not in (int, float)
                or not all(math.isfinite(x) for x in (start, end))
                or not 0 <= start < end <= duration + .05 or end-start > 30):
            raise ValueError('待复核句子超出有界源窗')
        alternatives = {'original': row['text_de']}
        proofs = []
        for kind, margin in (('exact', 0), ('contextual', 1)):
            left, right = max(0, start-margin), min(duration, end+margin)
            clip = evidence_dir/(sid+'_'+kind+'.wav')
            command = [ffmpeg, '-v', 'error', '-nostdin', '-y', '-ss', str(left), '-i', str(video),
                       '-t', str(right-left), '-map', '0:a:'+str(track), '-vn', '-ac', '1', '-ar', '16000',
                       '-c:a', 'pcm_s16le', str(clip)]
            subprocess.run(command, check=True, capture_output=True, timeout=90)
            segments, info = model.transcribe(str(clip), language=row.get('source_language', 'de'),
                task='transcribe', vad_filter=False, word_timestamps=True, beam_size=5,
                condition_on_previous_text=False)
            words, texts = [], []
            for segment in segments:
                texts.append(segment.text)
                for word in segment.words or []:
                    words.append({'word': word.word, 'start_s': word.start+left,
                                  'end_s': word.end+left, 'probability': word.probability})
            selected = [w for w in words if start <= (w['start_s']+w['end_s'])/2 < end]
            text = ''.join(texts).strip() if kind=='exact' else ''.join(w['word'] for w in selected).strip()
            # Context decoding is corroborating evidence, not a selectable
            # source replacement: timestamp jitter can trim boundary words.
            if kind=='exact' and text and len(text) <= 600:
                alternatives[kind] = text
            proofs.append({'kind': kind, 'clip': str(clip.relative_to(folder)), 'clip_sha256': _hash(clip),
                           'window_start_s': left, 'window_end_s': right, 'full_text': ''.join(texts).strip(),
                           'bounded_text': text, 'words': words})
        # If the context pass places a final word across the old ASR cut,
        # decode the complete allocated slot once. Keep the old clip as proof.
        tail_end = slot_tail_end(row, all_rows, duration)
        head_start = slot_head_start(row, all_rows)
        crosses = any(start <= w['start_s'] < end < w['end_s'] <= tail_end
                      and w['probability'] >= .4 for w in words)
        crosses_head = any(head_start <= w['start_s'] < start < w['end_s'] <= end
                           and w['probability'] >= .4 for w in words)
        complete_start = head_start if crosses_head else start
        complete_end = tail_end if crosses else end
        if complete_start < start or complete_end > end:
            clip = evidence_dir/(sid+'_slot_complete.wav')
            subprocess.run([ffmpeg, '-v', 'error', '-nostdin', '-y', '-ss', str(complete_start), '-i', str(video),
                '-t', str(complete_end-complete_start), '-map', '0:a:'+str(track), '-vn', '-ac', '1', '-ar', '16000',
                '-c:a', 'pcm_s16le', str(clip)], check=True, capture_output=True, timeout=90)
            segments, _ = model.transcribe(str(clip), language=row.get('source_language', 'de'),
                task='transcribe', vad_filter=False, word_timestamps=True, beam_size=5,
                condition_on_previous_text=False)
            text = ''.join(s.text for s in segments).strip()
            if text and len(text) <= 600:
                alternatives['exact'] = text
                proofs[0]['kind'] = 'original_exact'
                proofs.append({'kind':'exact', 'clip':str(clip.relative_to(folder)), 'clip_sha256':_hash(clip),
                    'window_start_s':complete_start, 'window_end_s':complete_end, 'full_text':text,
                    'original_start_s':start, 'original_end_s':end, 'no_neighbor_sentence_included':True,
                    'method':'complete_existing_source_slot_without_neighbor_speech'})
        # A repeated, known lexical confusion gets one additional real decode
        # on the same complete audio window. Keep both unhinted transcripts and
        # mark the hinted result; no German candidate is manufactured in text.
        context = ' '.join(r['text_de'] for r in all_rows
            if row['start_s']-30 <= r['start_s'] <= row['end_s']+30)
        witness = ' '.join(p.get('full_text','') for p in proofs if p['kind']=='contextual')
        hints = lexical_hints(alternatives.get('exact',''), witness, context)
        decoding = {'policy':'bounded_same_window_lexical_hint_v1','hotwords':hints,
                    'hinted_candidate_used':False,'human_verified':False}
        if hints:
            prior = next(p for p in proofs if p['kind']=='exact')
            segments, _ = model.transcribe(str(folder/prior['clip']), language=row.get('source_language','de'),
                task='transcribe', vad_filter=False, word_timestamps=True, beam_size=5,
                condition_on_previous_text=False, hotwords=' '.join(hints))
            decoded = list(segments)
            text = ''.join(s.text for s in decoded).strip()
            if text and len(text)<=600 and re.search(r'\bGeldhahn\b',text,re.I):
                proof = {**prior,'full_text':text,'bounded_text':text,
                    'asr_hotwords':hints,'prompted_machine_transcription':True,
                    'words':[{'word':w.word,'start_s':w.start+prior['window_start_s'],
                              'end_s':w.end+prior['window_start_s'],'probability':w.probability}
                             for s in decoded for w in (s.words or [])]}
                prior['kind']='unhinted_exact'
                proofs.append(proof)
                alternatives['exact']=text
                decoding['hinted_candidate_used']=True
        result[sid] = {'alternatives': alternatives, 'evidence': proofs, 'decoding':decoding,
                       'model_path': str(model_path.resolve()), 'model_sha256': model_hash,
                       'original_text_sha256': hashlib.sha256(row['text_de'].encode()).hexdigest(),
                       'input_source_sha256': source_sha256, 'source_words_verified': False,
                       'human_reviewed': False, 'method': 'local_cpu_asr_exact_and_contextual_windows_v1'}
        recovery._save(evidence_dir/(sid+'_source_recheck.json'), result[sid])
        print(json.dumps({'event': 'translation_source_recheck', 'segment_id': sid,
                          'completed': len(result), 'total': len(rows), 'human_reviewed': False}), flush=True)
    return result
