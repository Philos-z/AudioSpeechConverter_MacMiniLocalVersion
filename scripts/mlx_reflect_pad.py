"""Opt-in, scoped pure-MLX equivalent of the pinned WaveNet reflect padding.

No model/runtime is imported until ``install``. No tensor is evaluated, read
back, converted to NumPy, or cached here. Compiling is the caller's decision;
compiler exceptions are never swallowed by this adapter. Use normal
``mx.compile(..., shapeless=False)``: reflection indices depend on the input
length, and shapeless compilation can silently capture the first length.
Restore before any
adapter installed earlier, and discard graphs compiled while this is active
when restoring: a compiled graph captures the operations, not Python scope.
"""
from contextvars import ContextVar
import hashlib
import importlib
import inspect
import json
import operator
from pathlib import Path

VERSION = "indextts25_scoped_mlx_reflect_pad_v1"
WAVENET_SHA256 = "7cbe9023b4465797c26561e9f22a80345e71bbbdb37b625c07ccc324936116a2"
CFM_SHA256 = "b40c14cbd54c238cd5234892c11e898718627e35f4cd325c9db4d6379a9e85d5"
_SCOPE = ContextVar("indextts25_reflect_pad_scope", default=None)
_PAD_PATCHES = {}
_SOLVER_PATCHES = {}


def reflect_pad_nlc(x, pad_left, pad_right, *, runtime=None):
    """NumPy ``pad(..., mode='reflect')`` default even reflection on axis 1.

    For length > 1, periodically fold coordinates around the two end samples;
    this also covers padding longer than the signal. NumPy repeats the only
    sample for length 1 and rejects a positive reflect pad of an empty axis.
    Shape/pad sizes are Python metadata; all data operations stay lazy in MLX.
    """
    left, right = operator.index(pad_left), operator.index(pad_right)
    if left < 0 or right < 0:
        raise ValueError("index can't contain negative values")
    if left == 0 and right == 0:
        return x
    if len(x.shape) != 3:
        raise ValueError("Reflect padding requires the pinned NLC rank-3 layout")
    length = x.shape[1]
    if length == 0:
        raise ValueError("can't extend empty axis 1 using modes other than 'constant' or 'empty'")
    if length + left + right > 2**31 - 1 or left > 2**31 - 1:
        raise ValueError("Reflect padding exceeds the supported int32 index range")
    if runtime is None:
        import mlx.core as runtime
    if length == 1:
        indices = runtime.zeros((left + 1 + right,), dtype=runtime.int32)
    else:
        period = 2 * (length - 1)
        if period > 2**31 - 1:
            raise ValueError("Reflect padding period exceeds the supported int32 index range")
        positions = runtime.arange(-left, length + right, dtype=runtime.int32)
        folded = runtime.remainder(positions, period)
        indices = runtime.minimum(folded, period - folded)
    return runtime.take(x, indices, axis=1)


def _verified_source(obj, expected):
    source = inspect.getsourcefile(obj)
    if source is None or hashlib.sha256(Path(source).read_bytes()).hexdigest() != expected:
        raise RuntimeError("Unrecognized IndexTTS source; reflect padding was not changed")


class ReflectPadAdapter:
    def __init__(self, tts):
        import mlx.core as mx
        self.cfm = tts.s2mel_mlx.cfm
        if type(self.cfm).__module__ != "mlx_indextts.models.s2mel.cfm" or type(self.cfm).__name__ != "CFM":
            raise RuntimeError("Reflect padding only supports the pinned IndexTTS 2.5 CFM")
        module = importlib.import_module("mlx_indextts.models.s2mel.wavenet")
        _verified_source(module, WAVENET_SHA256)
        _verified_source(type(self.cfm), CFM_SHA256)
        self.module, self.cls, self.active = module, type(self.cfm), False
        self.signature = {
            "version": VERSION,
            "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "wavenet_sha256": WAVENET_SHA256, "cfm_sha256": CFM_SHA256,
            "operation": "axis1_even_reflect_periodic_gather",
            "scope": "only_selected_cfm_solve_euler; compiled_graphs_capture_operations",
            "length_one": "repeat", "empty_positive_pad": "raise",
            "dtype": "unchanged", "steps_or_weights_changed": False,
            "host_tensor_readback": False, "forces_compilation": False,
            "compile_contract": "shape-specialized_only; shapeless=True_not_supported",
        }
        self.signature["sha256"] = hashlib.sha256(json.dumps(
            self.signature, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        solver_record = _SOLVER_PATCHES.get(self.cls)
        if solver_record is not None and id(self.cfm) in solver_record["owners"]:
            raise RuntimeError("Reflect padding already installed for this CFM")
        pad_record = _PAD_PATCHES.get(module)
        if pad_record is None:
            original = module._pad1d
            pad_record = {"original": original, "owners": set()}
            def padded(x, pad_left, pad_right, mode="reflect"):
                current = _SCOPE.get()
                if current not in pad_record["owners"] or mode != "reflect":
                    return original(x, pad_left, pad_right, mode)
                return reflect_pad_nlc(x, pad_left, pad_right, runtime=mx)
            pad_record["wrapper"] = padded
        if solver_record is None:
            original_solver = self.cls.solve_euler
            solver_record = {"original": original_solver,
                             "own": "solve_euler" in self.cls.__dict__, "owners": {}}
            def solve(obj, *args, **kwargs):
                current = solver_record["owners"].get(id(obj))
                if current is None:
                    return original_solver(obj, *args, **kwargs)
                token = _SCOPE.set(current)
                try:
                    return original_solver(obj, *args, **kwargs)
                finally:
                    _SCOPE.reset(token)
            solver_record["wrapper"] = solve
        # All source/type checks happen before installing either dispatch.
        if module not in _PAD_PATCHES:
            module._pad1d = pad_record["wrapper"]
            _PAD_PATCHES[module] = pad_record
        if self.cls not in _SOLVER_PATCHES:
            self.cls.solve_euler = solver_record["wrapper"]
            _SOLVER_PATCHES[self.cls] = solver_record
        pad_record["owners"].add(self)
        solver_record["owners"][id(self.cfm)] = self
        self.active = True

    def restore(self):
        if not self.active:
            return
        pad_record, solver_record = _PAD_PATCHES[self.module], _SOLVER_PATCHES[self.cls]
        # Validate the whole restore before mutating registries, so out-of-order
        # restores can be retried once an outer adapter has been removed.
        if self.module._pad1d is not pad_record["wrapper"] or self.cls.solve_euler is not solver_record["wrapper"]:
            raise RuntimeError("Remove later padding/solver adapters before restoring reflect padding")
        pad_record["owners"].remove(self)
        solver_record["owners"].pop(id(self.cfm))
        if not solver_record["owners"]:
            if solver_record["own"]:
                self.cls.solve_euler = solver_record["original"]
            else:
                delattr(self.cls, "solve_euler")
            del _SOLVER_PATCHES[self.cls]
        if not pad_record["owners"]:
            self.module._pad1d = pad_record["original"]
            del _PAD_PATCHES[self.module]
        self.active = False


def install(tts):
    """Return a source-pinned, selected-instance handle: .signature/.restore()."""
    return ReflectPadAdapter(tts)
