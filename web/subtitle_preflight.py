"""Text-only subtitle layout preflight, before any speech model is loaded."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check_layout(root,folder):
    root=Path(root).resolve();folder=Path(folder).resolve()
    if folder.parent!=root/'jobs':raise ValueError('Invalid subtitle preflight job')
    source=folder/'segments.json';rows=json.loads(source.read_text())['segments']
    if not rows or len({r['segment_id'] for r in rows})!=len(rows):raise ValueError('Invalid subtitle source rows')
    renderer=root/'scripts/compact_edit.py'
    spec=importlib.util.spec_from_file_location('_subtitle_preflight_renderer',renderer)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    layouts=[]
    for row in rows:
        layout=module.fit_subtitle_layout(row['text_zh'],row['text_de'],segment_id=row['segment_id'])
        layouts.append({'segment_id':row['segment_id'],**layout})
    return {'schema':'subtitle_text_layout_preflight_v1','phase':'complete','source_rows':len(rows),
        'segments_sha256':sha(source),'renderer_sha256':sha(renderer),'layouts':layouts,
        'text_changed':False,'timing_created_or_changed':False,'models_loaded':False,
        'human_visual_reviewed':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True);parser.add_argument('--job',required=True)
    args=parser.parse_args()
    if not re.fullmatch(r'web_[A-Za-z0-9_-]{1,54}',args.job):raise ValueError('Invalid job')
    folder=args.root.resolve()/'jobs'/args.job
    result=check_layout(args.root,folder)
    output=folder/'subtitle_preflight.json';temporary=output.with_suffix('.partial')
    temporary.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');temporary.replace(output)
    print(json.dumps({'phase':'complete','source_rows':result['source_rows'],
        'text_changed':False,'timing_created_or_changed':False,'models_loaded':False},ensure_ascii=False))


if __name__=='__main__':main()
