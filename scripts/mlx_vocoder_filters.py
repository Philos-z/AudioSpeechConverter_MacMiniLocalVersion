"""Opt-in, process-local FP16 resampling buffers for native BigVGANV2.

The upstream private Kaiser-sinc buffers are initialized FP32 and excluded from
serialized model parameters. With FP16 input/weights they promote the whole
vocoder back to FP32. This experiment casts only those two named buffer types,
never source weights, and must have an independent cache/quality approval.
"""
import hashlib
from pathlib import Path

class FilterPrecision:
    def __init__(self, vocoder, *, _runtime=None):
        if _runtime is None:
            import mlx.core as mx
        else:
            mx = _runtime
        self.originals=[]
        self.records=[]
        prepared=[]
        for name, module in vocoder.named_modules():
            if type(module).__module__ != 'mlx_indextts.models.activations' or type(module).__name__ not in ('UpSample1d','DownSample1d'):
                continue
            original=module._filter
            if original.ndim != 3 or original.shape[0] != 1 or original.shape[-1] != 1 or original.shape[1] < 1:
                raise ValueError('Unexpected private Kaiser-sinc buffer shape')
            if str(original.dtype).split('.')[-1] not in ('float16','float32'):
                raise ValueError('Unexpected private Kaiser-sinc buffer dtype')
            converted=original if original.dtype == mx.float16 else original.astype(mx.float16)
            prepared.append((name,module,original,converted))
        if not prepared:raise ValueError('No expected private resampling buffers found')
        # Complete preflight first: malformed later modules or a failed cast
        # must leave every original buffer untouched. Assignment itself can
        # still fail, so retain the current original BEFORE attempting it.
        try:
            for name,module,original,converted in prepared:
                self.originals.append((module,original))
                module._filter=converted
                self.records.append({'path':name,'type':type(module).__name__,
                    'old_dtype':str(original.dtype),'new_dtype':str(converted.dtype),'shape':list(original.shape)})
        except BaseException:
            self.restore()
            raise
        self.signature={'version':'native_bigvgan_private_resample_buffers_fp16_v2',
            'source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'buffers':len(self.records),'quality_approved':False}
    def snapshot(self):return {'signature':self.signature,'buffers':self.records}
    def restore(self):
        for module,original in reversed(self.originals):module._filter=original
        self.originals=[]

def install_filter_fp16(vocoder, **kwargs):return FilterPrecision(vocoder, **kwargs)
