"""Verified, content-addressed fixed-gain measurements. CPU only."""
import hashlib
import json
import math
from pathlib import Path
import subprocess
import time

POLICY={'target_integrated_lufs':-19,'max_true_peak_dbtp':-3,'lra_lu':11,
        'algorithm':'ffmpeg_loudnorm_input_measurement_then_min_fixed_gain_v1'}


def sha256(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def measure_gain(path):
    r=subprocess.run(['ffmpeg','-hide_banner','-threads','2','-i',str(path),'-af',
        'loudnorm=I=-19:TP=-3:LRA=11:print_format=json','-f','null','-'],capture_output=True,text=True)
    if r.returncode:raise RuntimeError(r.stderr[-3000:])
    at=r.stderr.rfind('{')
    if at<0:raise ValueError('LOUDNESS_MEASUREMENT_MISSING')
    m=json.JSONDecoder().raw_decode(r.stderr[at:])[0]
    integrated=float(m['input_i']);peak=float(m['input_tp'])
    if not all(math.isfinite(x) for x in [integrated,peak]):raise ValueError('Invalid input levels')
    return min(-19-integrated,-3-peak),{'integrated_lufs':integrated,'true_peak_dbtp':peak}


class GainCache:
    def __init__(self,path=None,policy=None):
        self.entries={};cache_error=None
        if path and Path(path).is_file():
            try:self.entries=json.loads(Path(path).read_text()).get('entries',{})
            except (OSError,json.JSONDecodeError) as error:cache_error=type(error).__name__
        if policy is not None and policy!=POLICY:
            raise ValueError('Only the existing -19 LUFS/-3 dBTP fixed-gain analysis preset is implemented')
        self.policy=dict(POLICY if policy is None else policy)
        self.stats={'hits':0,'misses':0,'hash_wall_s':0.,'analysis_wall_s':0.}
        if cache_error:self.stats['cache_read_error']=cache_error
        self.pending={}
    def get(self,path):
        start=time.perf_counter();digest=sha256(path);self.stats['hash_wall_s']+=time.perf_counter()-start
        entry=self.entries.get(digest);gain=entry.get('gain_db') if entry else None
        hit=bool(entry and entry.get('source_sha256')==digest and entry.get('measurement_policy')==self.policy
                 and isinstance(gain,(int,float)) and math.isfinite(gain))
        if hit:self.stats['hits']+=1
        else:
            start=time.perf_counter();gain,levels=measure_gain(path)
            self.stats['analysis_wall_s']+=time.perf_counter()-start;self.stats['misses']+=1
            entry={'source_sha256':digest,'gain_db':gain,'measurement_policy':dict(self.policy),
                   'origin':'measured from current source bytes','input_levels':levels}
            self.entries[digest]=entry;self.pending[digest]=entry
        return gain,{'source_sha256':digest,'measurement_cache_hit':hit,'policy':self.policy}
    def save(self,path):
        p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(json.dumps({'schema':'verified_loudness_gain_v1','entries':self.entries},ensure_ascii=False,indent=2)+'\n')
