"""Opt-in whole-utterance gain before the known BigVGANV2 internal clamp.

Never wraps the already clipped final waveform. Only the pinned eager native
BigVGANV2 use_tanh=False path is accepted. Compiled/wrapped/unknown vocoders are
refused. No vendor file, model weight, speed, pitch or timing is changed.

Default off. Enabled use: install_preclip_guard(tts.bigvgan_mlx, enabled=True),
then begin(id), generate ONE unsplit utterance, finish(), restore(). Do not
compile or replace the vocoder after installing. A finite-value check adds one
whole-utterance synchronization before the clamp; gain itself stays lazy.
"""
import hashlib
import inspect
import math
from pathlib import Path

VERSION = "native_mlx_bigvgan_conv_post_static_preclamp_gain_v1"
PINNED_BIGVGAN_SHA256 = "aa7020b0c679cec8c7d67ec0ff9c7861c087c07e701c4936430f396d10b3d062"
_PATCHES = {}


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _validate_module(module):
    qualified = type(module).__module__ + "." + type(module).__name__
    if qualified != "mlx_indextts.models.bigvgan_v2.BigVGANV2":
        raise ValueError("Preclip guard accepts only eager native BigVGANV2; compiled/wrapped/unknown vocoder refused")
    if getattr(module, "use_tanh", None) is not False:
        raise ValueError("Explicit BigVGANV2 use_tanh=False is required")
    config = getattr(module, "config", None)
    if getattr(config, "use_tanh_at_final", None) is not False:
        raise ValueError("Explicit config.use_tanh_at_final=False is required")
    source = inspect.getsourcefile(type(module))
    if not source or _sha(source) != PINNED_BIGVGAN_SHA256:
        raise ValueError("Unknown BigVGANV2 implementation: internal clamp position is not pinned")
    target = getattr(module, "conv_post", None)
    if target is None or not callable(target):
        raise ValueError("BigVGANV2 conv_post callable is required")
    return target, {"qualified_class": qualified, "source_sha256": _sha(source),
                    "branch": "explicit use_tanh=False; conv_post -> mx.clip(-1,1)"}


def _patch(handle, target):
    key = type(target), "__call__"
    record = _PATCHES.get(key)
    if record is None:
        original = getattr(type(target), "__call__")
        record = {"original": original, "own": "__call__" in type(target).__dict__, "handlers": {}}
        def dispatched(obj, *args, **kwargs):
            selected = record["handlers"].get(id(obj))
            if selected is None:
                return original(obj, *args, **kwargs)
            return selected(lambda *a, **k: original(obj, *a, **k), args, kwargs)
        record["wrapper"] = dispatched
        setattr(type(target), "__call__", dispatched)
        _PATCHES[key] = record
    if id(target) in record["handlers"]:
        raise RuntimeError("This conv_post already has a preclip guard")
    record["handlers"][id(target)] = handle._call
    handle._patch = key, id(target)


class WholeUtteranceMLXPreclipGuard:
    def __init__(self, module, *, enabled=False, ceiling=.95, _runtime=None):
        if not isinstance(ceiling, (int, float)) or not math.isfinite(ceiling) or not 0 < ceiling <= .95:
            raise ValueError("Preclip ceiling must be finite and in (0, .95]")
        self.module = module
        self.enabled = bool(enabled)
        self.ceiling = float(ceiling)
        self.runtime = _runtime
        self._patch = None
        self.active_id = None
        self.call_count = 0
        self.pending = None
        self.history = []
        self.signature = {"version": VERSION, "enabled": self.enabled, "ceiling": self.ceiling,
                          "guard_source_sha256": _sha(__file__),
                          "point": "BigVGANV2.conv_post.__call__ output BEFORE internal clamp",
                          "gain_policy": "one uniform scalar per unsplit utterance; min(1, safe_ceiling/peak)",
                          "safe_ceiling_policy": "ceiling * (1 - 2 * finfo(dtype).eps)",
                          "synchronization": "strict finite check before clamp; scalar peak/gain read in finish",
                          "vocoder_requirement": "pinned eager BigVGANV2; no tanh/compile/wrapper",
                          "time_pitch_or_compression_changed": False}

    def install(self):
        if self._patch is not None:
            raise RuntimeError("Preclip guard is already installed")
        if not self.enabled:
            return self
        self.target, identity = _validate_module(self.module)
        self.signature["vocoder_identity"] = identity
        if self.runtime is None:
            import mlx.core as mx
            self.runtime = mx
        _patch(self, self.target)
        return self

    def begin(self, identifier):
        if self.active_id is not None:
            raise RuntimeError("Finish or restore the previous guarded utterance first")
        if self.enabled and self._patch is None:
            raise RuntimeError("Install the preclip guard before beginning")
        if identifier is None:
            raise ValueError("An utterance identifier is required")
        self.active_id = str(identifier)
        self.call_count = 0
        self.pending = None

    def _call(self, original, args, kwargs):
        if self.active_id is None:
            raise RuntimeError("Begin a whole utterance before guarded conv_post")
        self.call_count += 1
        if self.call_count != 1:
            raise RuntimeError("Multiple conv_post calls refused: one gain must cover exactly one whole utterance")
        raw = original(*args, **kwargs)
        mx = self.runtime
        if not isinstance(raw, mx.array):
            raise TypeError("Preclip guard requires an MLX floating array")
        dtype_name = str(raw.dtype).split(".")[-1]
        if dtype_name not in {"float16", "float32", "float64", "bfloat16"}:
            raise TypeError("Preclip guard refuses non-floating conv_post output")
        if len(raw.shape) != 3 or raw.shape[0] != 1 or raw.shape[-1] != 1 or raw.shape[1] < 1:
            raise ValueError("Expected one nonempty NLC utterance (B=1, C=1) before BigVGAN clamp")
        # Refuse NaN/Inf BEFORE they can reach and be hidden by the internal clamp.
        finite = mx.all(mx.isfinite(raw))
        mx.eval(finite)
        if not bool(finite.item()):
            raise ValueError("Non-finite pre-internal-clamp waveform refused")
        eps = float(mx.finfo(raw.dtype).eps)
        safe = self.ceiling * (1. - 2. * eps)
        peak = mx.max(mx.abs(raw))
        safe_mx = mx.array(safe, dtype=raw.dtype)
        one = mx.array(1., dtype=raw.dtype)
        gain = mx.minimum(one, safe_mx / mx.maximum(peak, safe_mx))
        protected = raw * gain
        self.pending = {"peak": peak, "gain": gain, "protected_peak": mx.max(mx.abs(protected)),
                        "protected_finite": mx.all(mx.isfinite(protected)),
                        "shape": list(raw.shape), "dtype": str(raw.dtype), "safe_ceiling": safe}
        return protected

    def finish(self):
        if self.active_id is None:
            raise RuntimeError("Begin a guarded utterance before finishing")
        if not self.enabled:
            result = {"utterance": self.active_id, "enabled": False, "applied": False}
            self.active_id = None
            return result
        if self.call_count != 1 or self.pending is None:
            raise RuntimeError("Exactly one complete eager conv_post call is required; compiled/bypassed/missing calls refused")
        mx = self.runtime
        pending = self.pending
        mx.eval(pending["peak"], pending["gain"], pending["protected_peak"], pending["protected_finite"])
        peak = float(pending["peak"].item())
        gain = float(pending["gain"].item())
        after = float(pending["protected_peak"].item())
        if (not bool(pending["protected_finite"].item()) or not all(map(math.isfinite, (peak, gain, after)))
                or not 0 < gain <= 1 or after > self.ceiling):
            raise ValueError("Protected waveform failed finite/gain/ceiling invariant; refuse delivery")
        result = {"utterance": self.active_id, "enabled": True, "version": VERSION,
                  "point": self.signature["point"], "conv_post_calls": self.call_count,
                  "pre_internal_clamp_peak": peak, "protected_peak": after,
                  "ceiling": self.ceiling, "safe_ceiling": pending["safe_ceiling"],
                  "static_gain": gain, "gain_db": 20 * math.log10(gain), "applied": gain < 1.,
                  "shape": pending["shape"], "dtype": pending["dtype"],
                  "prevents_internal_clamp_for_this_tensor": True,
                  "time_pitch_or_compression_changed": False,
                  "quality_status": "amplitude protection only; naturalness/timbre/listening not assessed"}
        self.history.append(result)
        self.active_id = None
        self.pending = None
        return result

    def snapshot(self):
        return {"signature": self.signature, "utterances": list(self.history)}

    def restore(self):
        if self._patch is not None:
            key, identity = self._patch
            record = _PATCHES[key]
            if getattr(key[0], key[1]) is not record["wrapper"]:
                raise RuntimeError("Restore newer adapters first; guarded conv_post class was replaced later")
            record["handlers"].pop(identity, None)
            if not record["handlers"]:
                if record["own"]:
                    setattr(key[0], key[1], record["original"])
                else:
                    delattr(key[0], key[1])
                del _PATCHES[key]
            self._patch = None
        self.active_id = None
        self.pending = None


def install_preclip_guard(module, *, enabled=False, ceiling=.95, **kwargs):
    return WholeUtteranceMLXPreclipGuard(module, enabled=enabled, ceiling=ceiling, **kwargs).install()
