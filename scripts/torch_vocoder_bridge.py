"""Small opt-in MLX-CFM -> original Torch BigVGAN bridge; no eager imports.

Inventory/import_class are CPU preparation only. load_vocoder explicitly loads
only this vocoder, never full IndexTTS/ASR/W2V/GPT. Nothing writes model files.
"""
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time
import types

BIGVGAN_REPO='nvidia/bigvgan_v2_22khz_80band_256x'
WEIGHT_SHA='e95ba25972d3de0628d99cd156e9315a9c018899bf739988959ebe3544080ced'
CONFIG_SHA='88a1f47acf747db0b21e97a389d838566147f7a5464583ff5c8d819d870f03ee'
SCHEMA='mlx_cfm_original_torch_bigvgan_v1'


def digest(path):
    result=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):result.update(block)
    return result.hexdigest()


def inventory(root, verify_weight_sha=False):
    root=Path(root).resolve()
    folder=root/'vendor/index-tts/indextts/s2mel/modules/bigvgan'
    assets=root/'models/indextts-2.5/hf_cache/bigvgan'
    lock=json.loads((root/'reports/models.lock.json').read_text())
    package=next(item for item in lock['models'] if item['repo']==BIGVGAN_REPO)
    entries={item['path']:item for item in package['files']}
    if entries['bigvgan_generator.pt']['sha256']!=WEIGHT_SHA or entries['config.json']['sha256']!=CONFIG_SHA:
        raise ValueError('BigVGAN model lock differs from the pinned bridge assets')
    for name in ('bigvgan_generator.pt','config.json'):
        if (assets/name).stat().st_size != entries[name]['size']:raise ValueError('BigVGAN file size mismatch: '+name)
    if digest(assets/'config.json')!=CONFIG_SHA:raise ValueError('BigVGAN config SHA mismatch')
    actual=digest(assets/'bigvgan_generator.pt') if verify_weight_sha else None
    if actual is not None and actual!=WEIGHT_SHA:raise ValueError('BigVGAN generator SHA mismatch')
    code={str(path.relative_to(folder)):digest(path) for path in sorted(folder.rglob('*.py'))}
    config=json.loads((assets/'config.json').read_text())
    if config['num_mels']!=80 or config['sampling_rate']!=22050 or config['hop_size']!=256:
        raise ValueError('Unexpected BigVGAN mel or audio geometry')
    return {'schema':SCHEMA,'source_folder':str(folder),'asset_folder':str(assets),
        'weight_sha256':WEIGHT_SHA,'actual_weight_sha256':actual,'weight_sha_verified':actual is not None,
        'config_sha256':CONFIG_SHA,'source_code_sha256':code,'model_revision':package['revision'],
        'sample_rate':22050,'num_mels':80,'hop_length':256,
        'original_output_bound':'BigVGAN forward clamps to +/-1 (use_tanh_at_final=False)',
        'imports_required':['torch','huggingface_hub','numpy'],
        'plotting_import_workaround':'private-package utils exposes original init_weights/get_padding AST only',
        'models_loaded':False,'model_files_modified':False}


def import_class(root):
    """Import the original class without its unused matplotlib/plotting helpers.

    A private package avoids changing indextts modules already used by another
    backend. Both retained utils functions are compiled from original source,
    not reimplemented; the original BigVGAN source itself is executed unchanged.
    """
    info=inventory(root)
    folder=Path(info['source_folder'])
    namespace='_efficiency_original_bigvgan_'+digest(folder/'bigvgan.py')[:16]
    module_name=namespace+'.bigvgan'
    if module_name in sys.modules:return sys.modules[module_name].BigVGAN
    package=types.ModuleType(namespace);package.__path__=[str(folder)]
    package.__package__=namespace;sys.modules[namespace]=package
    tree=ast.parse((folder/'utils.py').read_text())
    selected=[node for node in tree.body if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef))
              and node.name in {'init_weights','get_padding'}]
    if {node.name for node in selected}!={'init_weights','get_padding'}:
        raise ValueError('Original minimal BigVGAN utils contract changed')
    utils=types.ModuleType(namespace+'.utils');utils.__package__=namespace
    exec(compile(ast.Module(body=selected,type_ignores=[]),str(folder/'utils.py'),'exec'),utils.__dict__)
    sys.modules[utils.__name__]=utils
    spec=importlib.util.spec_from_file_location(module_name,folder/'bigvgan.py')
    module=importlib.util.module_from_spec(spec);sys.modules[module_name]=module
    try:spec.loader.exec_module(module)
    except BaseException:
        for name in list(sys.modules):
            if name==namespace or name.startswith(namespace+'.'):sys.modules.pop(name,None)
        raise
    return module.BigVGAN


def identity(info, precision='float32', device='mps', preclip=False):
    if precision not in ('float32','float16'):raise ValueError('Only explicit float32 or opt-in float16 supported')
    if device not in ('cpu','mps'):raise ValueError('Bridge supports cpu or mps only')
    return {'schema':SCHEMA,'weight_sha256':info['weight_sha256'],'config_sha256':info['config_sha256'],
            'source_code_sha256':info['source_code_sha256'],'precision':precision,'device':device,
            'remove_weight_norm':True,'use_cuda_kernel':False,
            'extra_preclip_guard': 'bigvgan_conv_post_static_gain_before_internal_clamp_v2' if preclip else False,
            'prompt_crop':None,'sample_rate':22050,'hop_length':256,
            'quality_status':'unapproved_hybrid_experiment'}


class TorchVocoder:
    def __init__(self,model,metadata,guard=None):
        self.model=model;self.metadata=metadata;self.guard=guard;self.last_timing={};self.calls=0

    def vocode(self,mel):
        """Consume an already materialized [1,80,T] NumPy log-mel; return float32 PCM.

        Caller must first finish MLX CFM/mx.eval, then materialize np.asarray(mel).
        The calls are sequential. No log/exp/transpose/resample/crop is applied.
        """
        import numpy as np
        import torch
        array=np.asarray(mel)
        if array.ndim!=3 or array.shape[:2]!=(1,80) or array.shape[2]<1:
            raise ValueError('Expected complete generated mel shape [1,80,T]')
        if not np.issubdtype(array.dtype,np.floating) or not np.isfinite(array).all():
            raise ValueError('Expected finite floating generated mel')
        device=self.metadata['device'];precision=self.metadata['precision']
        torch_dtype=torch.float32 if precision=='float32' else torch.float16
        began=time.perf_counter()
        # Copy once to writable CPU storage, because MLX-exported arrays may be read-only.
        source=torch.from_numpy(np.array(array,dtype=np.float32,copy=True))
        tensor=source.to(device=device,dtype=torch_dtype)
        if device=='mps':torch.mps.synchronize()
        transferred=time.perf_counter()
        self.calls+=1
        if self.guard:self.guard.begin('hybrid_vocoder_'+str(self.calls))
        with torch.inference_mode():output=self.model(tensor)
        guard_record=self.guard.finish() if self.guard else None
        if device=='mps':torch.mps.synchronize()
        synthesized=time.perf_counter()
        waveform=output[0,0].float().cpu().numpy()
        if not np.isfinite(waveform).all() or waveform.shape!=(array.shape[2]*256,):
            raise ValueError('Invalid original BigVGAN output/frame count; do not crop to fit')
        self.last_timing={'cpu_to_torch_device_s':transferred-began,
            'torch_vocoder_s':synthesized-transferred,'torch_to_numpy_s':time.perf_counter()-synthesized,
            'total_bridge_s':time.perf_counter()-began,'mel_frames':int(array.shape[2]),
            'audio_frames':int(waveform.size),'duration_s':waveform.size/22050.,
            'peak_after_original_bound':float(np.max(np.abs(waveform))),
            'near_fullscale_samples':int(np.sum(np.abs(waveform)>=.9999)),
            'preclip_guard':guard_record}
        return waveform


def load_vocoder(root, *, device='mps', precision='float32', preclip=False):
    """Explicit model/GPU entry point. Not called by inventory or import_class."""
    began=time.perf_counter()
    info=inventory(root,verify_weight_sha=True)
    metadata=identity(info,precision=precision,device=device,preclip=preclip)
    import torch
    if device=='mps' and not torch.backends.mps.is_available():raise RuntimeError('MPS is unavailable; no silent CPU fallback')
    BigVGAN=import_class(root)
    from_module=sys.modules[BigVGAN.__module__]
    config=from_module.load_hparams_from_json(Path(info['asset_folder'])/'config.json')
    # Construct/load/denormalize on CPU in float32, then make any dtype conversion explicit.
    with torch.device('cpu'):
        model=BigVGAN(config,use_cuda_kernel=False)
    checkpoint=torch.load(Path(info['asset_folder'])/'bigvgan_generator.pt',map_location='cpu',weights_only=True,mmap=True)
    state=checkpoint['generator']
    if 'conv_pre.weight_g' not in state:model.remove_weight_norm()
    model.load_state_dict(state,strict=True)
    if 'conv_pre.weight_g' in state:model.remove_weight_norm()
    del state,checkpoint
    model.eval().requires_grad_(False)
    model=model.to(device=device,dtype=torch.float32 if precision=='float32' else torch.float16)
    if device=='mps':torch.mps.synchronize()
    metadata.update(actual_weight_sha256=info['actual_weight_sha256'],weight_sha_verified=True,
        torch_version=torch.__version__,load_wall_seconds=time.perf_counter()-began,
        model_files_modified=False,full_indextts_loaded=False)
    guard=None
    if preclip:
        from preclip_guard import WholeUtterancePreclipGuard
        guard=WholeUtterancePreclipGuard(model,ceiling=.95).install()
    return TorchVocoder(model,metadata,guard)


class MLXHybridHandle:
    """Instance-local, reversible eager boundary adapter; no vendor-file edits."""
    def __init__(self,tts,vocoder):
        self.tts=tts;self.vocoder=vocoder;self.original=tts.bigvgan_mlx
        self.metadata=vocoder.metadata;self.last_timing={}
        tts.bigvgan_mlx=self

    def __call__(self,mel):
        import mlx.core as mx
        import numpy as np
        began=time.perf_counter()
        mx.eval(mel)
        materialized=np.asarray(mel)
        materialize_s=time.perf_counter()-began
        audio=self.vocoder.vocode(materialized)
        began=time.perf_counter()
        result=mx.array(audio[None,None,:])
        mx.eval(result)
        self.last_timing={'mlx_materialize_s':materialize_s,**self.vocoder.last_timing,
                         'numpy_output_to_mlx_s':time.perf_counter()-began}
        return result

    def restore(self):
        if self.tts.bigvgan_mlx is self:self.tts.bigvgan_mlx=self.original
        if self.vocoder.guard:self.vocoder.guard.restore()


def install_mlx_torch_vocoder(tts,root, *, precision='float32',preclip=False):
    """Explicit GPU entry point for a bounded probe. Returns handle.restore().

    Example: handle=install_mlx_torch_vocoder(tts,root,precision='float32');
    generate three samples normally, inspect handle.last_timing, then restore.
    The caller must place this identity in a new cache namespace if persisted.
    """
    vocoder=load_vocoder(root,device='mps',precision=precision,preclip=preclip)
    return MLXHybridHandle(tts,vocoder)
