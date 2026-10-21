"""Bounded local content QA for new cached utterances during the current render."""
import argparse
import json
from pathlib import Path
import time

from faster_whisper import WhisperModel

p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--job',required=True)
p.add_argument('--max-seconds',type=int,default=7200);args=p.parse_args()
root=args.root.resolve();job=root/'jobs'/args.job
config=json.loads((root/'config/local.json').read_text())
model=WhisperModel(config['asr_model'],device='cpu',compute_type='int8',local_files_only=True,cpu_threads=2)
path=job/'audio_qa_observations.json'
observations=json.loads(path.read_text()) if path.exists() else {}
began=time.monotonic()
while time.monotonic()-began<args.max_seconds:
    rows={r['segment_id']:r for r in json.loads((job/'segments.json').read_text())['segments']}
    count=0
    for metadata in sorted((job/'tts/cache').glob('*.json')):
        info=json.loads(metadata.read_text());key=info['cache_key'];identifier=info['id']
        if key in observations or identifier not in rows or info['text']!=rows[identifier]['text_zh']:
            continue
        segments,_=model.transcribe(info['file'],language='zh',task='transcribe',beam_size=5,condition_on_previous_text=False)
        recognized=''.join(s.text for s in segments).strip()
        observations[key]={'id':identifier,'cache_key':key,'file':info['file'],'expected':info['text'],'recognized':recognized}
        tmp=path.with_suffix('.partial.json');tmp.write_text(json.dumps(observations,ensure_ascii=False,indent=2)+'\n');tmp.replace(path)
        count+=1;print('AUDIO_QA',identifier,len(observations),flush=True)
    if (job/'qa.stop').exists():break
    if not count:time.sleep(10)
print('AUDIO_QA_STOPPED',len(observations),flush=True)
