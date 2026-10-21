"""Finalize a reviewed job after its pause audit and fallback synthesis finish."""
import argparse
import json
from pathlib import Path
import subprocess
import time

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,required=True);p.add_argument('--job',required=True)
    p.add_argument('--background',type=Path,required=True)
    args=p.parse_args();root=args.root.resolve();job=root/'jobs'/args.job
    out=job/'outputs/landscape_compact';out.mkdir(parents=True,exist_ok=True)
    py=root/'vendor/index-tts/.venv/bin/python';scripts=root/'scripts'
    progress={'phase':'starting','started_at_unix':time.time(),'job':args.job}
    def run(phase, argv):
        progress.update(phase=phase,updated_at_unix=time.time())
        (job/'compact_pipeline_progress.json').write_text(json.dumps(progress,indent=2)+'\n')
        print('COMPACT_PHASE',phase,flush=True)
        subprocess.run([str(x) for x in argv],check=True)
    common=['--root',root,'--job',args.job]
    run('selecting_final_pause_audio',[py,scripts/'assemble_compact_timeline.py','--job',job])
    run('building_edit_and_subtitles',[py,scripts/'compact_edit.py',*common])
    edl=out/'edit_decisions.json'
    if json.loads(edl.read_text())['issues']:
        raise ValueError('Edit windows require review; no audio or video was truncated.')
    run('mixing_independent_audio',[py,scripts/'mix_compact.py','--job',job,'--edl',edl,'--background',args.background])
    run('verifying_pause_waveforms',[py,scripts/'verify_pause_audio.py','--job',job,'--output',out/'pause_verification.json'])
    run('rendering',[py,scripts/'compact_edit.py',*common,'--render','--mixed-wav',out/'zh_mix.wav'])
    run('verifying_complete_media',[py,scripts/'verify_compact.py','--edl',edl,'--video',out/'landscape_compact_zh.mp4',
        '--ass',out/'compact_bilingual.ass','--mixed-wav',out/'zh_mix.wav',
        '--export-report',out/'compact_export.json','--output',out/'verification.json'])
    progress.update(phase='engineering_verified_requires_frame_review',finished_at_unix=time.time())
    (job/'compact_pipeline_progress.json').write_text(json.dumps(progress,indent=2)+'\n')
    print('COMPACT_RENDER_READY',out/'landscape_compact_zh.mp4',flush=True)

if __name__=='__main__':main()
