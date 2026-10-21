"""Opt-in raw semantic-logit finite guard, before any sampling/filtering.

Checks do not consume random numbers or alter logits, sampling arguments, token
history, or the original sampler result. A bad raw tensor is rejected before
categorical sampling. Detailed first-step diagnostics copy only 8194 logits to
CPU; checking all steps adds a scalar synchronization and must be benchmarked.
"""
import hashlib
import inspect
import json
import math
from pathlib import Path

VERSION = 'indextts25_raw_semantic_logits_guard_v1'
SOURCE_SHA256 = {
    'gpt_v25.py': 'b5cca534ae14f1d428784e9c3bdbeef92efd1ece5f79992013c2df140c9b7f8a',
    'gpt_v2.py': '3a2837c77e39bbc2ba441cd21a90b71c16877a258dde6c9debb0c8d33cf12144',
    'sampling.py': 'be7c9930a52654d12d00cf6a5234f678e765bde664824be75c2f195a6df2a398',
}
_PATCHES = {}

class NonfiniteSemanticLogits(ValueError): pass
class FirstStepDiagnosticComplete(RuntimeError): pass

def identity(*, check_mode='all_steps', stop_after_first_sample=False):
    if check_mode not in {'all_steps', 'first_step'}:
        raise ValueError('Unknown raw-logit check mode')
    result = {'version': VERSION, 'source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'vendor_source_sha256': dict(SOURCE_SHA256), 'check_mode': check_mode,
        'point': 'gpt._sample raw input BEFORE repetition/top-k/top-p/temperature/categorical',
        'nonfinite_policy': 'raise before original sampler; no token or audio fallback',
        'stop_after_first_sample': bool(stop_after_first_sample), 'sampling_math_or_rng_changed': False,
        'synchronization': 'one finite scalar per checked step; detailed CPU summary first/bad step only'}
    result['sha256'] = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return result

def summarize_values(rows, *, dtype, shape):
    """Small stdlib summary; no random or sampling operation."""
    values = [float(value) for row in rows for value in row]
    finite = [value for value in values if math.isfinite(value)]
    top = []
    for batch, row in enumerate(rows):
        candidates = [(index, float(value)) for index, value in enumerate(row) if math.isfinite(float(value))]
        candidates.sort(key=lambda item: item[1], reverse=True)
        top.append({'batch': batch, 'tokens': [{'token': index, 'raw_logit': value} for index, value in candidates[:5]]})
    return {'dtype': str(dtype), 'shape': list(shape), 'values': len(values),
        'finite_count': len(finite), 'nan_count': sum(math.isnan(value) for value in values),
        'positive_inf_count': sum(value == math.inf for value in values),
        'negative_inf_count': sum(value == -math.inf for value in values),
        'all_finite': len(finite) == len(values), 'finite_min': min(finite) if finite else None,
        'finite_max': max(finite) if finite else None, 'top_finite_candidates': top,
        'top_candidates_are_raw_logits_not_sampled_tokens': True}

class RawLogitsGuard:
    def __init__(self, tts, *, check_mode='all_steps', stop_after_first_sample=False, _runtime=None, _verify=True):
        self.gpt = tts.gpt
        self.cls = type(self.gpt)
        self.signature = identity(check_mode=check_mode, stop_after_first_sample=stop_after_first_sample)
        self.runtime = _runtime
        self.active = False
        self.transaction = None
        if _verify:
            if self.cls.__module__ != 'mlx_indextts.models.gpt_v25' or self.cls.__name__ != 'UnifiedVoiceV25':
                raise RuntimeError('Raw-logit guard requires the pinned IndexTTS2.5 GPT')
            parent = Path(inspect.getsourcefile(self.cls)).parent
            if any(hashlib.sha256((parent/name).read_bytes()).hexdigest() != digest
                   for name, digest in SOURCE_SHA256.items()):
                raise RuntimeError('Unrecognized GPT/sampler source; no raw-logit guard installed')
        record = _PATCHES.get(self.cls)
        if record is None:
            original = self.cls._sample
            record = {'original': original, 'own': '_sample' in self.cls.__dict__, 'owners': {}}
            def sample(obj, *args, **kwargs):
                selected = record['owners'].get(id(obj))
                if selected is None: return original(obj, *args, **kwargs)
                return selected._sample(lambda *a, **k: original(obj, *a, **k), args, kwargs)
            record['wrapper'] = sample
        elif id(self.gpt) in record['owners']:
            raise RuntimeError('Raw-logit guard already installed for this GPT')
        if self.cls not in _PATCHES:
            self.cls._sample = record['wrapper']
            _PATCHES[self.cls] = record
        record['owners'][id(self.gpt)] = self
        self.active = True

    def begin(self, identifier):
        self.transaction = {'utterance_id': str(identifier), 'sampling_calls': 0, 'checked_calls': 0,
            'original_sampler_calls': 0, 'first_raw_logits': None, 'nonfinite_rejected': False,
            'diagnostic_only': self.signature['stop_after_first_sample']}

    def _sample(self, original, args, kwargs):
        if self.transaction is None:
            raise RuntimeError('Call raw-logit guard.begin before generation')
        state = self.transaction
        state['sampling_calls'] += 1
        logits = args[0] if args else kwargs['logits']
        if len(logits.shape) != 2 or logits.shape[0] != 1 or logits.shape[1] != 8194:
            raise ValueError('Unknown semantic-logit layout; refusing to sample')
        if state['sampling_calls'] == 1 or self.signature['check_mode'] == 'all_steps':
            if self.runtime is None:
                import mlx.core as mx
                self.runtime = mx
            finite = bool(self.runtime.all(self.runtime.isfinite(logits)).item())
            state['checked_calls'] += 1
            if state['sampling_calls'] == 1 or not finite:
                import numpy as np
                values = np.asarray(logits)
                summary = summarize_values(values.tolist(), dtype=logits.dtype, shape=logits.shape)
                if state['sampling_calls'] == 1: state['first_raw_logits'] = summary
                if not finite: state['rejected_raw_logits'] = summary
            if not finite:
                state['nonfinite_rejected'] = True
                state['rejected_step_1based'] = state['sampling_calls']
                raise NonfiniteSemanticLogits('Nonfinite raw semantic logits at step ' + str(state['sampling_calls']) + '; sampling stopped before categorical')
        # Preserve the exact original logits object and all arguments; this
        # function never calls an RNG and never samples replacement tokens.
        result = original(*args, **kwargs)
        state['original_sampler_calls'] += 1
        if self.signature['stop_after_first_sample']:
            raise FirstStepDiagnosticComplete('First finite semantic-logit diagnostic complete; stopped after one unchanged original sampler call')
        return result

    def snapshot(self):
        return {'signature': self.signature, 'transaction': self.transaction}

    def finish(self):
        state = self.transaction
        if (state is None or state['sampling_calls'] < 1 or state['nonfinite_rejected']
                or state['sampling_calls'] != state['original_sampler_calls']
                or (self.signature['check_mode'] == 'all_steps' and state['checked_calls'] != state['sampling_calls'])):
            raise ValueError('One complete finite semantic-logit guard transaction required')
        return self.snapshot()

    def restore(self):
        if not self.active: return
        record = _PATCHES[self.cls]
        if self.cls._sample is not record['wrapper']:
            raise RuntimeError('Remove later sampler adapters before restoring raw-logit guard')
        record['owners'].pop(id(self.gpt))
        if not record['owners']:
            if record['own']: self.cls._sample = record['original']
            else: delattr(self.cls, '_sample')
            del _PATCHES[self.cls]
        self.active = False

def install(tts, **options): return RawLogitsGuard(tts, **options)
