"""Bilingual pages within intact utterances, aligned by actual CPU ASR words.

No audio/EDL cut or proportional timing. Plans bind each unchanged parent text,
speaker, source window and actual WAV SHA; a changing full native manifest is
historical provenance only. Static three-line cues may use the same existing
240px safe top; cues crossing that boundary still need verified pagination.
"""
import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import wave

SCHEMA='verified_bilingual_subtitle_pages_v1'
PAGE_SCHEMA='intact_utterance_bilingual_page_v1'

def read(path):return json.loads(Path(path).read_text())
def sha(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda:handle.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()
def save(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);temporary=path.with_suffix(path.suffix+'.partial')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');temporary.replace(path)
def check(value,message):
    if not value:raise ValueError(message)
def tight(text):return ''.join(text.split())
def row_binding(row):
    return {name:row[name] for name in ('segment_id','speaker_id','start_s','end_s','text_zh','text_de')}
def flatten_words(observation):return [word for segment in observation['segments'] for word in segment['words']]
def _alignment():
    import generic_group_alignment as alignment
    return alignment

def validate_text_pages(row,pages):
    check(isinstance(pages,list) and len(pages)>=2,'A paginated parent needs at least two pages')
    for language in ('zh','de'):
        check(all(isinstance(page.get('text_'+language),str) and page['text_'+language].strip() for page in pages),'Empty bilingual page')
        check(tight(''.join(page['text_'+language] for page in pages))==tight(row['text_'+language]),'Pagination changed or omitted original '+language+' text')


def create_plan(root,job_id,proposal_path,manifest_path,output,cache_dir):
    root=Path(root).resolve();job=root/'jobs'/job_id;rows={row['segment_id']:row for row in read(job/'segments.json')['segments']}
    proposal=read(proposal_path);check(proposal['schema']=='bilingual_page_proposals_v1' and proposal['job_id']==job_id,'Wrong page proposal job/schema')
    manifest=read(manifest_path);check(manifest['phase']=='complete' and manifest['pending_unique_keys']==0,'Native audio must be complete')
    natives={row['segment_id']:row for row in manifest['utterances']};alignment=_alignment();normalize=alignment.normalize_factory(root);observer=alignment.Observer(root,cache_dir)
    parents=[];seen=set();issues=[]
    for item in proposal['items']:
        sid=item['segment_id'];check(sid in rows and sid not in seen,'Unknown or duplicate paginated parent');seen.add(sid)
        row=rows[sid];pages=item['pages'];validate_text_pages(row,pages);native=natives[sid];audio=Path(native['audio']).resolve()
        check(native['content']['text_zh']==row['text_zh'] and native['speaker_id']==row['speaker_id'],'Native text/voice differs from source parent')
        check(sha(audio)==native['sha256'],'Native WAV SHA mismatch')
        with wave.open(str(audio)) as handle:frames,rate,channels=handle.getnframes(),handle.getframerate(),handle.getnchannels()
        check(frames>0 and channels==1,'Invalid complete mono WAV');duration=frames/rate
        binding={'path':str(audio),'sha256':native['sha256'],'duration_s':duration,'frames':frames,'sample_rate':rate}
        if item.get('observation_override'):
            record=item['observation_override'];observation_path=Path(record['path']).resolve()
            check(sha(observation_path)==record['sha256'],'Alternative target-free observation changed')
            observation=read(observation_path);profile=observation['profile'];configured=alignment.asr_profile(root)
            check(profile['model_assets']==configured['model_assets'] and profile['device']=='cpu' and profile['compute_type']=='int8' and profile['local_files_only'] is True and profile['target_conditioning'] is False,'Alternative observation must use the same actual local CPU model')
            options=profile['transcribe_options']
            check(options.get('initial_prompt') is None and options.get('prefix') is None and options.get('hotwords') is None and options.get('word_timestamps') is True,'Alternative observation must remain target-free')
            alignment.validate_observation(observation,binding,profile);hit=True
        else:observation,observation_path,hit=observer.observe(binding)
        expected=normalize(row['text_zh']);boundaries=[];cursor=0
        try:
            for page in pages[:-1]:
                cursor+=len(normalize(page['text_zh']))
                boundaries.append(alignment.choose_boundary(expected,cursor,flatten_words(observation),normalize,duration))
            switches=[point['audio_switch_s'] for point in boundaries]
            check(bool(switches) and switches==sorted(set(switches)) and 0<switches[0] and switches[-1]<duration,'Invalid actual page switch')
        except ValueError as error:
            issues.append({'segment_id':sid,'reason':str(error),'observation':str(observation_path),'proportional_fallback_used':False})
        parents.append({'segment_id':sid,'row_binding':row_binding(row),'audio':binding,'pages':copy.deepcopy(pages),
            'boundaries':boundaries,'observation':{'path':str(observation_path.resolve()),'sha256':sha(observation_path)},
            'asr_profile':observation['profile'],'cache_hit':hit,'target_conditioning':False,'waveform_unchanged':True})
    result={'schema':SCHEMA,'status':'needs_alignment_review' if issues else 'aligned_for_machine_preview','alignment_issues':issues,'job_id':job_id,'root':str(root),'parents':parents,
        'proposal':{'path':str(Path(proposal_path).resolve()),'sha256':sha(proposal_path)},
        'historical_native_manifest':{'path':str(Path(manifest_path).resolve()),'sha256':sha(manifest_path),'required_to_remain_same':False},
        'alignment_implementation_sha256':sha(Path(alignment.__file__)),
        'asr_model_load_s':observer.model_load_s,'asr_inference_s':observer.inference_s,'asr_new_observations':observer.misses,
        'audio_modified':False,'edl_clips_modified':False,'human_reviewed':False,'timing_accuracy':'ASR word onset for captions; not phoneme/audio-cut alignment'}
    save(output,result);return result


def validate_plan(plan,utterances):
    check(plan.get('schema')==SCHEMA and plan.get('status')=='aligned_for_machine_preview','Unverified subtitle page plan')
    lookup={row['segment_id']:row for row in utterances};alignment=_alignment();normalize=alignment.normalize_factory(plan['root'])
    check(sha(Path(alignment.__file__))==plan['alignment_implementation_sha256'],'Subtitle alignment implementation changed')
    parents={};seen=set()
    for parent in plan['parents']:
        sid=parent['segment_id'];check(sid not in seen,'Duplicate subtitle parent');seen.add(sid)
        # A separately marked prefix preview can select a subset of source parents.
        if sid not in lookup:continue
        row=lookup[sid];check(row_binding(row)==parent['row_binding'],'Paged original text, voice or source interval changed: '+sid)
        validate_text_pages(row,parent['pages']);audio=parent['audio']
        check(sha(row['audio'])==audio['sha256']==row['audio_sha256'],'Paged actual WAV changed: '+sid)
        check(abs(row['speech_s']-audio['duration_s'])<1e-7,'Paged audio duration changed')
        observation_record=parent['observation'];check(sha(observation_record['path'])==observation_record['sha256'],'Actual ASR evidence changed')
        observation=read(observation_record['path']);alignment.validate_observation(observation,audio,parent['asr_profile'])
        options=observation['profile']['transcribe_options']
        check(parent['target_conditioning'] is False and observation['profile']['target_conditioning'] is False and
            options.get('initial_prompt') is None and options.get('prefix') is None and options.get('hotwords') is None and
            options.get('word_timestamps') is True,'Target-conditioned page evidence is not permitted')
        expected=normalize(row['text_zh']);cursor=0;actual=[]
        for page in parent['pages'][:-1]:
            cursor+=len(normalize(page['text_zh']));actual.append(alignment.choose_boundary(expected,cursor,flatten_words(observation),normalize,audio['duration_s']))
        check(actual==parent['boundaries'],'Page switch no longer equals actual unique high-confidence word onset')
        parents[sid]=parent
    return parents


def attach_pages(edl,plan_path):
    plan=read(plan_path);parents=validate_plan(plan,edl['utterances']);pages=[]
    for row in edl['utterances']:
        parent=parents.get(row['segment_id'])
        if not parent:
            pages.append({'schema':PAGE_SCHEMA,'parent_id':row['segment_id'],'page_index':0,'page_id':row['segment_id'],
                'text_zh':row['text_zh'],'text_de':row['text_de'],'output_start_s':row['output_start_s'],'output_end_s':row['output_end_s']});continue
        switches=[0.,*[point['audio_switch_s'] for point in parent['boundaries']],row['speech_s']]
        for index,text in enumerate(parent['pages']):
            start=row['output_start_s']+switches[index]
            end=row['output_end_s'] if index==len(parent['pages'])-1 else row['output_start_s']+switches[index+1]
            pages.append({'schema':PAGE_SCHEMA,'parent_id':row['segment_id'],'page_index':index,'page_id':row['segment_id']+'#p'+str(index+1),
                **text,'output_start_s':start,'output_end_s':end,'actual_wav_sha256':parent['audio']['sha256']})
    edl['subtitle_pages']=pages;edl['subtitle_pagination_proof']={'path':str(Path(plan_path).resolve()),'sha256':sha(plan_path),'schema':SCHEMA}
    validate_edl_pages(edl);return pages


def validate_edl_pages(edl):
    pages=edl.get('subtitle_pages');check(isinstance(pages,list) and pages,'Missing subtitle pages')
    proof=edl['subtitle_pagination_proof'];check(sha(proof['path'])==proof['sha256'],'Page plan changed after preparation')
    plan=read(proof['path']);bound=validate_plan(plan,edl['utterances']);lookup={r['segment_id']:r for r in edl['utterances']}
    check(len({page['page_id'] for page in pages})==len(pages),'Duplicate subtitle page IDs')
    check(set(page['parent_id'] for page in pages)==set(lookup),'Missing original subtitle parent')
    for sid,row in lookup.items():
        selected=[p for p in pages if p['parent_id']==sid];check([p['page_index'] for p in selected]==list(range(len(selected))),'Unordered subtitle pages')
        check(all(p['output_start_s']<p['output_end_s'] for p in selected),'Empty subtitle page interval')
        check(abs(selected[0]['output_start_s']-row['output_start_s'])<1e-7 and abs(selected[-1]['output_end_s']-row['output_end_s'])<1e-7,'Incomplete parent subtitle coverage')
        for left,right in zip(selected,selected[1:]):check(abs(left['output_end_s']-right['output_start_s'])<1e-7,'Subtitle page gap/overlap')
        for language in ('zh','de'):check(tight(''.join(p['text_'+language] for p in selected))==tight(row['text_'+language]),'Subtitle page text missing or altered')
        if sid in bound:
            parent=bound[sid];check(len(selected)==len(parent['pages']),'Unexpected page count')
            for i,page in enumerate(selected):
                check(all(page['text_'+lang]==parent['pages'][i]['text_'+lang] for lang in ('zh','de')),'Page text differs from bound actual-word plan')
                if i:check(abs(page['output_start_s']-row['output_start_s']-parent['boundaries'][i-1]['audio_switch_s'])<1e-7,'Page boundary differs from actual word onset')
        else:check(len(selected)==1 and selected[0]['page_id']==sid,'Unapproved parent was paginated')
    return pages


def build_ass(edl,output_path,visual):
    from PIL import ImageFont
    pages=validate_edl_pages(edl);fonts={'zh':ImageFont.truetype(visual.FONT,34),'de':ImageFont.truetype(visual.FONT,24)}
    # Reuse the exact established header/styles without passing an overlong cue.
    empty={**edl,'utterances':[]};temporary=Path(output_path).with_suffix('.header.ass');visual.build_compact_ass(empty,temporary)
    header=temporary.read_text();temporary.unlink();events=[];placements=[]
    for page in pages:
        zh=visual._wrap(page['text_zh'],fonts['zh'],'zh',900);de=visual._wrap(page['text_de'],fonts['de'],'de',900)
        check(len(zh)<=3 and len(de)<=4,'Subtitle page remains too tall: '+page['page_id'])
        de_height=len(de)*sum(fonts['de'].getmetrics());zh_height=len(zh)*sum(fonts['zh'].getmetrics());zh_bottom=520-de_height-10;zh_top=zh_bottom-zh_height
        check(zh_top>=240,'Subtitle page extends above the preserved safe region')
        for language,style,lines,bottom in [('zh','Chinese',zh,zh_bottom),('de','German',de,520)]:
            position='{'+chr(92)+f'an2{chr(92)}pos(480,{bottom}){chr(92)}q2'+'}'
            text=r'\N'.join(visual._escape(line) for line in lines)
            events.append(f'Dialogue: 0,{visual._stamp(page["output_start_s"])},{visual._stamp(page["output_end_s"])},{style},{page["page_id"]},0,0,0,,{position}{text}')
        placements.append({'parent_id':page['parent_id'],'page_id':page['page_id'],'zh_lines':len(zh),'de_lines':len(de),'zh_top':zh_top,'zh_bottom':zh_bottom,'de_bottom':520})
    Path(output_path).write_text(header+'\n'.join(events)+'\n',encoding='utf-8')
    return {'schema':'paged_bilingual_layout_v1','pages':len(pages),'parents':len(edl['utterances']),'fonts':{'zh':34,'de':24},'positions':placements,'safe_top':240,'max_zh_lines':3,'max_de_lines':4}



def validate_ass_layout(events,font_path='/System/Library/Fonts/Supplemental/Arial Unicode.ttf'):
    """Validate actual event geometry, not only the relaxed three-line limit."""
    from PIL import ImageFont
    metrics={style:sum(ImageFont.truetype(font_path,size).getmetrics()) for style,size in [('Chinese',34),('German',24)]}
    grouped={}
    for event in events:
        check(event['style'] in metrics,'Unexpected bilingual subtitle style')
        group=grouped.setdefault(event['id'],{})
        check(event['style'] not in group,'Duplicate actual subtitle style')
        text=event['text'];position=re.match(r'^\{\\an2\\pos\(480,(\d+)\)\\q2\}',text)
        check(position is not None,'Unexpected subtitle position override')
        lines=text.count(r'\N')+1
        check(lines<=(3 if event['style']=='Chinese' else 4),'Actual subtitle line count exceeds preserved geometry')
        group[event['style']]={'bottom':int(position.group(1)),'lines':lines}
    for styles in grouped.values():
        check(set(styles)==set(metrics),'Missing actual bilingual subtitle style')
        zh,de=styles['Chinese'],styles['German']
        check(de['bottom']==520,'German subtitle bottom changed')
        check(zh['bottom']==520-de['lines']*metrics['German']-10,'Bilingual spacing changed')
        check(zh['bottom']-zh['lines']*metrics['Chinese']>=240,'Actual subtitle extends above the preserved safe region')
    return {'pages':len(grouped),'safe_top':240,'font_sizes':{'zh':34,'de':24}}



def write_srt(edl,output,stamp):
    rows=validate_edl_pages(edl) if 'subtitle_pages' in edl else edl['utterances']
    for language in ('zh','de'):
        (Path(output)/(language+'.srt')).write_text('\n\n'.join(f"{i+1}\n{stamp(row['output_start_s'])} --> {stamp(row['output_end_s'])}\n{row['text_'+language]}" for i,row in enumerate(rows))+'\n',encoding='utf-8')
    return len(rows)


def visible_ass_text(text):
    text=re.sub(r'^\{[^}]*\}','',text)
    return text.replace(r'\N','').replace(r'\{','{').replace(r'\}','}').replace('\\\u2060','\\')


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--job',required=True);p.add_argument('--proposal',type=Path,required=True);p.add_argument('--manifest',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--cache-dir',type=Path,required=True);args=p.parse_args()
    result=create_plan(args.root,args.job,args.proposal,args.manifest,args.output,args.cache_dir)
    print(json.dumps({'schema':result['schema'],'parents':len(result['parents']),'new_cpu_asr':result['asr_new_observations'],'output':str(args.output)},ensure_ascii=False))

if __name__=='__main__':main()
