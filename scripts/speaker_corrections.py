"""Explicit, file-bound repairs of unreliable subsecond acoustic labels.

This module never discovers or silently applies a speaker merge. It accepts only
an existing job-specific correction record, rechecks its acoustic/context proof,
and preserves every source row and word. Labels do not verify real identities.
"""
import copy
import hashlib
import json
from pathlib import Path
import re

SCHEMA = 'job_scoped_speaker_correction_v1'

def sha(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda:handle.read(1024*1024),b''):
            digest.update(chunk)
    return digest.hexdigest()

def save(path,value):
    Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')

def bindings(root,job):
    root=Path(root).resolve(); job=Path(job).resolve()
    source=Path(json.loads((job/'source.json').read_text())['input']).resolve()
    if not source.is_relative_to(root):
        raise ValueError('Correction source is outside the project')
    files={'source_video':source,'asr':job/'segments.asr.json',
        'vocals':job/'separated/htdemucs/original/vocals.wav',
        'embeddings':job/'speaker_embeddings.npz'}
    return {name:{'path':str(path.relative_to(root)),'sha256':sha(path)} for name,path in files.items()}

def evidence_for(rows,vectors,index,target):
    import numpy as np
    current=rows[index]
    if index==0 or not 0<current['end_s']-current['start_s']<=.8:
        raise ValueError('Correction only supports an explicitly identified subsecond fragment')
    previous=rows[index-1]
    if previous['speaker_id']!=target or not -.001<=current['start_s']-previous['end_s']<=.12:
        raise ValueError('Correction is not a contiguous continuation of its target speaker')
    if previous.get('overlap') or current.get('overlap'):
        raise ValueError('Overlapping speech cannot use this correction')
    if re.search(r'[.!?]\s*["”’»]?\s*$',previous['text_de']):
        raise ValueError('Correction cannot cross a completed preceding sentence')
    if not re.search(r'[.!?]\s*["”’»]?\s*$',current['text_de']):
        raise ValueError('Correction must finish the incomplete preceding sentence')
    values=np.asarray(vectors,dtype=np.float64)
    if values.ndim!=2 or len(values)!=len(rows) or not np.isfinite(values).all():
        raise ValueError('Invalid acoustic evidence')
    norms=np.linalg.norm(values,axis=1)
    if not np.all((norms>.99)&(norms<1.01)):
        raise ValueError('Acoustic vectors are not normalized')
    distances=1-np.einsum('ij,j->i',values,values[index])
    eligible=[i for i,row in enumerate(rows) if row['end_s']-row['start_s']>=2 and i!=index]
    nearest=sorted(eligible,key=lambda i:(float(distances[i]),i))[:15]
    if len(nearest)!=15 or any(rows[i]['speaker_id']!=target for i in nearest):
        raise ValueError('Fifteen nearest full fragments do not agree on the target label')
    if max(float(distances[i]) for i in nearest)>.55:
        raise ValueError('Acoustic evidence is too distant')
    return {'fragment_duration_s':current['end_s']-current['start_s'],
        'previous_segment_id':previous['segment_id'],'previous_gap_s':current['start_s']-previous['end_s'],
        'nearest_complete_segments':[{'segment_id':rows[i]['segment_id'],
            'speaker_id':rows[i]['speaker_id'],'cosine_distance':float(distances[i])} for i in nearest],
        'nearest_complete_min_duration_s':2,'nearest_complete_count':15,
        'acoustic_labels_only':True,'identities_verified':False,'human_reviewed':False}

def apply_corrections(root,job,rows,vectors,clustering):
    root=Path(root).resolve();job=Path(job).resolve();file=job/'speaker_corrections.json'
    if not file.exists():
        return rows,{'schema':SCHEMA,'applied':[],'input_correction_present':False,
            'identities_verified':False,'human_reviewed':False}
    spec=json.loads(file.read_text())
    if spec.get('schema')!=SCHEMA or spec.get('job_id')!=job.name:
        raise ValueError('Speaker correction belongs to a different job or schema')
    actual_bindings=bindings(root,job)
    if spec.get('bindings')!=actual_bindings or spec.get('clustering')!=clustering:
        raise ValueError('Speaker correction source or clustering settings changed')
    edits=spec.get('edits')
    if not isinstance(edits,list) or not edits:
        raise ValueError('Explicit speaker edits required')
    if len({r['segment_id'] for r in rows})!=len(rows):
        raise ValueError('Source segment IDs are not unique')
    index={r['segment_id']:i for i,r in enumerate(rows)}
    updated=copy.deepcopy(rows);applied=[];seen=set()
    for edit in edits:
        sid=edit.get('segment_id')
        if sid in seen or sid not in index:
            raise ValueError('Duplicate or missing correction segment')
        seen.add(sid);i=index[sid];original=rows[i];target=edit.get('target_speaker_id')
        if original['speaker_id']!=edit.get('expected_from_speaker_id') or target==original['speaker_id']:
            raise ValueError('Expected original speaker label does not match')
        if edit.get('review_kind')!='machine_acoustic_and_sentence_context' or not str(edit.get('reason','')).strip():
            raise ValueError('Explicit machine review reason required')
        proof=evidence_for(rows,vectors,i,target)
        expected=edit.get('evidence',{})
        if expected!=proof:
            raise ValueError('Speaker correction evidence differs from bound acoustic observations')
        updated[i]['speaker_id']=target
        applied.append({'segment_id':sid,'from_speaker_id':original['speaker_id'],
            'to_speaker_id':target,'reason':edit['reason'],'evidence':proof})
    for old,new in zip(rows,updated):
        if {k:v for k,v in old.items() if k!='speaker_id'}!={k:v for k,v in new.items() if k!='speaker_id'}:
            raise ValueError('Speaker correction changed source content')
    receipt={'schema':SCHEMA,'job_id':job.name,'correction_file_sha256':sha(file),
        'helper_sha256':sha(Path(__file__)),'bindings':actual_bindings,'clustering':clustering,
        'original_assignments_sha256':hashlib.sha256(json.dumps(rows,ensure_ascii=False,sort_keys=True).encode()).hexdigest(),
        'original_assignments':rows,'applied':applied,'all_source_rows_and_words_preserved':True,
        'identities_verified':False,'human_reviewed':False,'model_audio_duplicated_or_stretched':False}
    return updated,receipt
