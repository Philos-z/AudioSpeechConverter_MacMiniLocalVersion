"""Rank ASR observations for review; this is not perceptual voice-quality scoring."""
import argparse
import json
from pathlib import Path
import re

import cn2an
from opencc import OpenCC

cc=OpenCC('t2s')


def normalize(text):
    text=cc.convert(text)
    # Whisper often writes quantities as 16万 while the script spells 十六万.
    text=re.sub(r'\d+(?:\.\d+)?[十百千万亿]+',lambda m:str(cn2an.cn2an(m.group(),'smart')),text)
    text=cn2an.transform(text,'cn2an')
    return re.sub(r'[^\u4e00-\u9fff0-9a-z]','',text.lower())


def distance(a,b):
    row=list(range(len(b)+1))
    for i,x in enumerate(a,1):
        cur=[i]
        for j,y in enumerate(b,1):cur.append(min(cur[-1]+1,row[j]+1,row[j-1]+(x!=y)))
        row=cur
    return row[-1]


p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--job',required=True)
args=p.parse_args();job=args.root/'jobs'/args.job
obs=json.loads((job/'audio_qa_observations.json').read_text())
current={r['segment_id']:r for r in json.loads((job/'segments.json').read_text())['segments']}
timeline_file=job/'timeline.json'
active_keys={r['segment_id']:r['cache_key'] for r in json.loads(timeline_file.read_text())['segments']} if timeline_file.exists() else None
rows=[]
for o in obs.values():
    if o['id'] not in current or current[o['id']]['text_zh']!=o['expected']:continue
    if active_keys is not None and active_keys.get(o['id'])!=o['cache_key']:continue
    a,b=normalize(o['expected']),normalize(o['recognized']);edits=distance(a,b)
    rows.append({**o,'normalized_expected':a,'normalized_recognized':b,'edits':edits,'cer':edits/max(1,len(a)),
        'review_reason':'ASR differences; may be homophones, names, or recognition errors'})
ranked=sorted(rows,key=lambda r:r['cer'],reverse=True)
data={'method':'CPU faster-whisper small; OpenCC traditional-to-simplified, cn2an numeral normalization, strip punctuation',
    'limitations':'text observations only; no timbre/prosody score, ASR error is not proof of synthesis error',
    'checked':len(rows),'expected_total':len(current),'rows':ranked}
(job/'audio_qa_report.json').write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
for r in ranked[:15]:print(r['id'],round(r['cer'],3),r['expected'],'=>',r['recognized'])
