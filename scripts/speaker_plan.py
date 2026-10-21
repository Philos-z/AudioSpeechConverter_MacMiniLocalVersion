"""Local CAMPPlus clustering and bounded sentence grouping for a full episode.

Cluster IDs are acoustic labels, not verified real-world identities.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

import numpy as np
import soundfile as sf


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n')


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--root',type=Path,required=True)
    p.add_argument('--job',required=True)
    p.add_argument('--threshold',type=float,default=.45)
    p.add_argument('--method',choices=['average','complete'],default='average')
    args=p.parse_args()
    root=args.root.resolve(); job=root/'jobs'/args.job
    original=job/'segments.asr.json'
    if not original.exists():
        original.write_bytes((job/'segments.json').read_bytes())
    data=json.loads(original.read_text()); rows=data['segments']
    cache=job/'speaker_embeddings.npz'
    fingerprint=hashlib.sha256(original.read_bytes()).hexdigest()
    if cache.exists():
        cached=np.load(cache)
        if str(cached['fingerprint'])!=fingerprint:
            raise ValueError('EMBEDDING_INPUT_CHANGED')
        vectors=cached['vectors']
    else:
        import torch
        import torchaudio
        torch.set_num_threads(2)
        sys.path.insert(0,str(root/'vendor/index-tts'))
        from indextts.s2mel.modules.campplus.DTDNN import CAMPPlus
        model=CAMPPlus(feat_dim=80,embedding_size=192)
        model.load_state_dict(torch.load(root/'models/indextts-2.5/hf_cache/campplus_cn_common.bin',map_location='cpu',weights_only=True))
        model.eval()
        audio,sr=sf.read(job/'audio/asr16k.wav',dtype='float32')
        vectors=[]
        with torch.inference_mode():
            for i,row in enumerate(rows):
                start=max(0,row['start_s']-.03);end=min(len(audio)/sr,row['end_s']+.03)
                clip=audio[round(start*sr):round(end*sr)]
                if len(clip)<sr:
                    clip=np.tile(clip,max(1,int(np.ceil(sr/max(1,len(clip))))))
                clip=clip[:8*sr]
                feat=torchaudio.compliance.kaldi.fbank(torch.from_numpy(clip.copy()).unsqueeze(0),num_mel_bins=80,dither=0,sample_frequency=sr)
                feat-=feat.mean(dim=0,keepdim=True)
                v=model(feat.unsqueeze(0)).squeeze().numpy()
                vectors.append(v/max(np.linalg.norm(v),1e-8))
                if i%40==0: print('VOICE_EMBEDDINGS',i+1,len(rows),flush=True)
        vectors=np.stack(vectors)
        np.savez(cache,vectors=vectors,fingerprint=fingerprint)
    from scipy.cluster.hierarchy import linkage,fcluster
    from scipy.spatial.distance import pdist
    tree=linkage(pdist(vectors,metric='cosine'),method=args.method)
    print('CLUSTER_COUNTS',{str(t):len(set(fcluster(tree,t=t,criterion='distance'))) for t in [.35,.40,.45,.50]},flush=True)
    labels=fcluster(tree,t=args.threshold,criterion='distance')
    # Very short fragments are weak speaker evidence; interpolate only when both neighbours agree.
    for i in range(1,len(rows)-1):
        if rows[i]['end_s']-rows[i]['start_s']<1.4 and labels[i-1]==labels[i+1]:
            labels[i]=labels[i-1]
    remap={};names=[]
    for label in labels:
        remap.setdefault(int(label),f'S{len(remap):02}')
        names.append(remap[int(label)])
    assignments=[]
    for row,name in zip(rows,names):
        assignments.append({**row,'speaker_id':name})
    from speaker_corrections import apply_corrections
    assignments,correction_receipt=apply_corrections(root,job,assignments,vectors,
        {'method':args.method,'cosine_distance_threshold':args.threshold})
    names=[row['speaker_id'] for row in assignments]
    save(job/'speaker_correction_receipt.json',correction_receipt)
    save(job/'speaker_assignments.json',{'method':f'CAMPPlus {args.method}-link acoustic clusters, not identity verification',
        'cosine_distance_threshold':args.threshold,'segments':assignments,
        'explicit_machine_corrections':correction_receipt['applied'],'identities_verified':False,'human_reviewed':False})
    # Pick a long homogeneous run, then cut a maximum 8-second speaker reference.
    runs=[]
    for i,row in enumerate(assignments):
        if runs and runs[-1]['speaker_id']==row['speaker_id'] and row['start_s']-runs[-1]['end_s']<.8:
            runs[-1]['end_s']=row['end_s'];runs[-1]['indices'].append(i)
        else:
            runs.append({'speaker_id':row['speaker_id'],'start_s':row['start_s'],'end_s':row['end_s'],'indices':[i]})
    stem=job/'separated/htdemucs/original/vocals.wav'
    reference_source=stem if stem.exists() else job/'audio/original.wav'
    speakers={}
    for name in sorted(set(names)):
        candidates=[r for r in runs if r['speaker_id']==name]
        best=max(candidates,key=lambda r:min(8,r['end_s']-r['start_s']))
        start=max(0,best['start_s']-.03);end=min(best['end_s']+.03,start+8)
        with sf.SoundFile(reference_source) as source:
            sr=source.samplerate;source.seek(round(start*sr))
            audio=source.read(round((end-start)*sr),dtype='float32',always_2d=True).mean(axis=1)
        ref=job/'references'/f'{name}.wav'
        sf.write(ref,audio,sr,subtype='PCM_24')
        speakers[name]={'reference':str(ref.relative_to(root)),'reviewed':True,
                        'review_method':'acoustic run selection; identities not verified',
                        'reference_window_s':[start,end], 'reference_source':str(reference_source.relative_to(root)),
                        'source_segments':[r['segment_id'] for r in assignments if r['speaker_id']==name]}
    save(job/'speakers.json',speakers)
    grouped=[]
    for row in assignments:
        if grouped:
            prior=grouped[-1]
            can_join=(prior['speaker_id']==row['speaker_id'] and row['start_s']-prior['end_s']<=1.1
                and row['end_s']-prior['start_s']<=9.5 and len((prior['text_de']+' '+row['text_de']).split())<=32)
            if re.search(r'[.!?]\s*$',prior['text_de']) and prior['end_s']-prior['start_s']>=4.2:
                can_join=False
        else: can_join=False
        if can_join:
            prior['end_s']=row['end_s'];prior['text_de']+=' '+row['text_de'];prior['source_ids'].append(row['segment_id'])
        else:
            grouped.append({'segment_id':f'line{len(grouped)+1:04}','speaker_id':row['speaker_id'],
                'start_s':row['start_s'],'end_s':row['end_s'],'text_de':row['text_de'],
                'text_zh':'','source_ids':[row['segment_id']],'translation_reviewed':False,
                'speaker_reviewed':True,'speaker_review_method':'local acoustic clustering, not human identity validation','overlap':False})
    duration=json.loads((job/'source.json').read_text())['duration_s']
    for i,row in enumerate(grouped):
        next_start=grouped[i+1]['start_s'] if i+1<len(grouped) else duration
        row['slot_end_s']=min(next_start-.06,row['end_s']+.8,duration)
        row['target_characters']=max(5,int((row['slot_end_s']-row['start_s'])*3.8))
    save(job/'segments.plan.json',{'schema_version':2,'segments':grouped,'source':'faster-whisper small; cross-check supplied subtitles','speaker_method':'CAMPPlus acoustic clustering','identities_verified':False})
    print('PLAN_READY',len(grouped),'lines',len(speakers),'acoustic speakers',flush=True)
    print('FIRST_GROUPS',[(r['start_s'],r['end_s'],r['speaker_id'],r['text_de'][:60]) for r in grouped[:15]],flush=True)


if __name__=='__main__': main()
