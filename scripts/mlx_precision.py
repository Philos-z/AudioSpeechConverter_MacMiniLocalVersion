"""Process-local, opt-in IndexTTS 2.5 dtype tracing and precision boundaries.

Importing this file does not import MLX, Torch or a model. ``install(tts)`` only
observes lazy tensor shape/dtype metadata. No eval/item/tolist/NumPy conversion
is used. Casts require named experimental features and belong in an audio cache
key through ``handle.signature``. The module never rewrites vendor files.

Use only eager CFM while diagnosing. A compiled graph can bypass Python hooks;
these hooks are evidence about the executed eager path, not kernel timings.
"""
from contextvars import ContextVar
import hashlib
import json

VERSION = "indextts25_mlx_local_precision_v2"
FEATURES = frozenset({
    "gpt_input_fp16", "vocoder_input_fp16", "cfm_inputs_fp16",
    "cfm_time_embedding_fp16", "cfm_rope_fp16", "cfm_wn_mask_fp16",
    "cfm_attention_mask_fp16",
})
_CFM_SCOPE = ContextVar("indextts25_precision_scope", default=None)
_METHOD_PATCHES = {}
_FUNCTION_PATCHES = {}


def _metadata(value, depth=0):
    """Describe a lazy value without evaluating it or reading tensor contents."""
    if hasattr(value, "dtype") and hasattr(value, "shape"):
        return {"dtype": str(value.dtype), "shape": list(value.shape)}
    if depth >= 6:
        return {"type": type(value).__name__}
    if isinstance(value, dict):
        return {str(key): _metadata(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_metadata(item, depth + 1) for item in value[:64]]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return {"type": type(value).__name__}


def _replace_argument(args, kwargs, index, name, transform):
    if len(args) > index:
        changed = list(args)
        changed[index] = transform(changed[index])
        return tuple(changed), kwargs
    if name in kwargs:
        changed = dict(kwargs)
        changed[name] = transform(changed[name])
        return args, changed
    return args, kwargs


def _argument(args, kwargs, index, name, default=None):
    return args[index] if len(args) > index else kwargs.get(name, default)


def _floating_mask_to_activation(mask, activation):
    """Convert additive floating masks only, preserving boolean/integer masks.

    GPT2Model may construct an F32 causal mask after its entry hook. Therefore
    this boundary is also applied at each selected GPT2Attention entry. It does
    not read mask values or change visibility semantics. Large negative F32
    sentinels may become -inf in F16, as expected for an additive causal mask.
    """
    if mask is None or not hasattr(mask, "dtype"):
        return mask
    dtype_name = str(mask.dtype).split(".")[-1]
    if dtype_name not in {"float16", "float32", "float64", "bfloat16"}:
        return mask
    return mask if mask.dtype == activation.dtype else mask.astype(activation.dtype)


def _patch_method(handle, instance, name, callback):
    """Patch special methods at class level, dispatching only one instance.

    Avoid storing callables on MLX nn.Module instances. In particular, do not
    rely on an instance __call__ override: Python special lookup ignores it.
    """
    cls = type(instance)
    key = (cls, name)
    record = _METHOD_PATCHES.get(key)
    if record is None:
        original = getattr(cls, name)
        own = name in cls.__dict__
        record = {"original": original, "own": own, "handlers": {}}

        def dispatched(obj, *args, **kwargs):
            selected = record["handlers"].get(id(obj))
            if selected is None:
                return original(obj, *args, **kwargs)
            return selected(lambda *a, **k: original(obj, *a, **k), args, kwargs)

        record["wrapper"] = dispatched
        _METHOD_PATCHES[key] = record
        setattr(cls, name, dispatched)
    if id(instance) in record["handlers"]:
        raise RuntimeError("This model method already has a precision adapter: " + name)
    record["handlers"][id(instance)] = callback
    handle._methods.append((key, id(instance)))


def _patch_rope(handle, module):
    key = (module, "apply_rotary_emb")
    record = _FUNCTION_PATCHES.get(key)
    if record is None:
        original = module.apply_rotary_emb
        record = {"original": original, "owners": set()}

        def apply_rotary_emb(x, freqs_cis):
            current = _CFM_SCOPE.get()
            if current is None or current not in record["owners"]:
                return original(x, freqs_cis)
            current.record("cfm.rope.input", x=x, freqs_cis=freqs_cis)
            if "cfm_rope_fp16" in current.features:
                # Compute phase/cos/sin in vendor F32, then cast the coefficients
                # at the multiply boundary. Never replace the stored RoPE table.
                freqs_cis = freqs_cis.astype(x.dtype)
            result = original(x, freqs_cis)
            current.record("cfm.rope.effective_output", freqs_cis=freqs_cis, output=result)
            return result

        record["wrapper"] = apply_rotary_emb
        _FUNCTION_PATCHES[key] = record
        module.apply_rotary_emb = apply_rotary_emb
    record["owners"].add(handle)
    handle._functions.append(key)


class PrecisionAdapter:
    def __init__(self, features=(), *, _half_dtype=None):
        self.features = frozenset(features)
        unknown = self.features - FEATURES
        if unknown:
            raise ValueError("Unknown precision feature(s): " + ", ".join(sorted(unknown)))
        internal = {name for name in self.features if name.startswith("cfm_")}
        if internal - {"cfm_inputs_fp16"} and "cfm_inputs_fp16" not in internal:
            raise ValueError("CFM internal casts require cfm_inputs_fp16")
        self.half_dtype = _half_dtype
        self.events = []
        self.seen = set()
        self._methods = []
        self._functions = []
        self._installed = False
        self.signature = {"version": VERSION, "features": sorted(self.features),
                          "default_precision_changed": bool(self.features),
                          "cfm_euler_state_policy": "unchanged; derivative returned in incoming state dtype",
                          "timestep_phase_policy": "vendor float32; optional post-sincos cast only",
                          "gpt_mask_policy": "gpt_input_fp16 casts floating additive masks to per-attention activation dtype; bool/int unchanged"}
        self.signature["sha256"] = hashlib.sha256(json.dumps(
            self.signature, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def record(self, label, **values):
        if label not in self.seen:
            self.seen.add(label)
            self.events.append({"point": label, **{name: _metadata(value) for name, value in values.items()}})

    def _half(self, value):
        if self.half_dtype is None:
            import mlx.core as mx
            self.half_dtype = mx.float16
        return value if value.dtype == self.half_dtype else value.astype(self.half_dtype)

    def _observe_call(self, label, *, input_transform=None):
        def wrapped(original, args, kwargs):
            self.record(label + ".input", args=args, kwargs=kwargs)
            if input_transform is not None:
                args, kwargs = input_transform(args, kwargs)
                self.record(label + ".effective_input", args=args, kwargs=kwargs)
            output = original(*args, **kwargs)
            self.record(label + ".output", output=output)
            return output
        return wrapped

    def install(self, tts):
        if self._installed:
            raise RuntimeError("Precision adapter is already installed")
        # Resolve all required objects before mutating classes.
        gpt = tts.gpt
        cfm = tts.s2mel_mlx.cfm
        estimator = cfm.estimator
        vocoder = tts.bigvgan_mlx
        if getattr(cfm, "_compiled_estimator", None) is not None:
            raise ValueError("Dtype tracing requires eager CFM; a compiled estimator is already installed")
        self._installed = True
        try:
            def prepare_inputs(original, args, kwargs):
                self.record("gpt.prepare_inputs.input", args=args, kwargs=kwargs)
                result = original(*args, **kwargs)
                self.record("gpt.prepare_inputs.output", output=result)
                if "gpt_input_fp16" in self.features:
                    result = (self._half(result[0]), *result[1:])
                self.record("gpt.prepare_inputs.effective_output", output=result)
                return result
            _patch_method(self, gpt, "prepare_inputs", prepare_inputs)

            def generate_step(original, args, kwargs):
                phase = "prefill" if _argument(args, kwargs, 1, "cache") is None else "decode"
                self.record("gpt.generate_step.input", args=args, kwargs=kwargs)
                self.record("gpt.generate_step." + phase + ".input", args=args, kwargs=kwargs)
                if "gpt_input_fp16" in self.features:
                    args, kwargs = _replace_argument(args, kwargs, 0, "input_emb", self._half)
                self.record("gpt.generate_step.effective_input", args=args, kwargs=kwargs)
                self.record("gpt.generate_step." + phase + ".effective_input", args=args, kwargs=kwargs)
                output = original(*args, **kwargs)
                # Includes lazy K/V arrays for each cache layer; no values read.
                self.record("gpt.generate_step.output_and_kv", output=output)
                self.record("gpt.generate_step." + phase + ".output_and_kv", output=output)
                return output
            _patch_method(self, gpt, "generate_step", generate_step)
            if hasattr(gpt, "gpt"):
                def gpt_mask_inputs(args, kwargs):
                    if "gpt_input_fp16" in self.features:
                        activation = _argument(args, kwargs, 0, "inputs_embeds")
                        args, kwargs = _replace_argument(args, kwargs, 1, "mask",
                            lambda mask: _floating_mask_to_activation(mask, activation))
                    return args, kwargs
                _patch_method(self, gpt.gpt, "__call__", self._observe_call(
                    "gpt.backbone", input_transform=gpt_mask_inputs))
                for index, block in enumerate(getattr(gpt.gpt, "h", ())):
                    attention = getattr(block, "attn", None)
                    if attention is None:
                        continue
                    def gpt_attention_inputs(args, kwargs):
                        if "gpt_input_fp16" in self.features:
                            activation = _argument(args, kwargs, 0, "x")
                            args, kwargs = _replace_argument(args, kwargs, 1, "mask",
                                lambda mask: _floating_mask_to_activation(mask, activation))
                        return args, kwargs
                    _patch_method(self, attention, "__call__", self._observe_call(
                        "gpt.attention." + str(index), input_transform=gpt_attention_inputs))

            def estimator_call(original, args, kwargs):
                self.record("cfm.estimator.input", args=args, kwargs=kwargs)
                state = _argument(args, kwargs, 0, "x")
                if "cfm_inputs_fp16" in self.features:
                    for index, name in ((0, "x"), (1, "prompt_x"), (4, "style"), (5, "condition")):
                        args, kwargs = _replace_argument(args, kwargs, index, name, self._half)
                self.record("cfm.estimator.effective_input", args=args, kwargs=kwargs)
                token = _CFM_SCOPE.set(self)
                try:
                    output = original(*args, **kwargs)
                finally:
                    _CFM_SCOPE.reset(token)
                self.record("cfm.estimator.raw_output", output=output)
                if "cfm_inputs_fp16" in self.features and output.dtype != state.dtype:
                    output = output.astype(state.dtype)
                self.record("cfm.estimator.state_dtype_output", output=output)
                return output
            _patch_method(self, cfm, "_call_estimator", estimator_call)
            _patch_method(self, estimator, "__call__", self._observe_call("cfm.dit"))
            for name in ("cond_projection", "cond_x_merge_linear", "transformer", "conv1", "conv2", "final_layer"):
                child = getattr(estimator, name, None)
                if child is not None:
                    _patch_method(self, child, "__call__", self._observe_call("cfm." + name))
            for name in ("t_embedder", "t_embedder2"):
                child = getattr(estimator, name, None)
                if child is None:
                    continue
                _patch_method(self, child, "__call__", self._observe_call("cfm." + name))

                def time_embedding(original, args, kwargs, label=name):
                    result = original(*args, **kwargs)
                    self.record("cfm." + label + ".sincos_output", output=result)
                    if "cfm_time_embedding_fp16" in self.features:
                        result = self._half(result)
                    self.record("cfm." + label + ".mlp_input", output=result)
                    return result
                _patch_method(self, child, "timestep_embedding", time_embedding)
            transformer = getattr(estimator, "transformer", None)
            if transformer is not None:
                rope = getattr(transformer, "rope", None)
                if rope is not None:
                    self.record("cfm.rope.stored_table", freqs_cis=rope.freqs_cis)
                for index, layer in enumerate(transformer.layers):
                    attention = getattr(layer, "attention", None)
                    if attention is None:
                        continue
                    def attention_inputs(args, kwargs):
                        if "cfm_attention_mask_fp16" in self.features:
                            args, kwargs = _replace_argument(args, kwargs, 2, "mask",
                                lambda value: self._half(value) if value is not None else None)
                        return args, kwargs
                    _patch_method(self, attention, "__call__", self._observe_call(
                        "cfm.attention." + str(index), input_transform=attention_inputs))
            wavenet = getattr(estimator, "wavenet", None)
            if wavenet is not None:
                def wn_inputs(args, kwargs):
                    if "cfm_wn_mask_fp16" in self.features:
                        args, kwargs = _replace_argument(args, kwargs, 1, "x_mask", self._half)
                    return args, kwargs
                _patch_method(self, wavenet, "__call__", self._observe_call("cfm.wavenet", input_transform=wn_inputs))
            # Import the estimator's already-loaded module, not a model/package.
            import sys
            dit_module = sys.modules.get(type(estimator).__module__)
            if dit_module is not None and hasattr(dit_module, "apply_rotary_emb"):
                _patch_rope(self, dit_module)

            def vocoder_inputs(args, kwargs):
                if "vocoder_input_fp16" in self.features:
                    args, kwargs = _replace_argument(args, kwargs, 0, "x", self._half)
                return args, kwargs
            _patch_method(self, vocoder, "__call__", self._observe_call("vocoder", input_transform=vocoder_inputs))
            for name in ("conv_pre", "conv_post"):
                child = getattr(vocoder, name, None)
                if child is not None:
                    _patch_method(self, child, "__call__", self._observe_call("vocoder." + name))
            return self
        except BaseException:
            self.restore()
            raise

    def snapshot(self):
        return {"signature": self.signature, "events": list(self.events),
                "observation_policy": "first call per point; lazy shape/dtype only; no added eval",
                "quality_status": "experimental casts require independent audio and listening validation"}

    def restore(self):
        for key, identity in reversed(self._methods):
            record = _METHOD_PATCHES[key]
            record["handlers"].pop(identity, None)
            if not record["handlers"]:
                cls, name = key
                if getattr(cls, name) is not record["wrapper"]:
                    raise RuntimeError("A later adapter replaced a method; refusing to overwrite it")
                if record["own"]:
                    setattr(cls, name, record["original"])
                else:
                    delattr(cls, name)
                del _METHOD_PATCHES[key]
        self._methods.clear()
        for key in reversed(self._functions):
            record = _FUNCTION_PATCHES[key]
            record["owners"].discard(self)
            if not record["owners"]:
                module, name = key
                if getattr(module, name) is not record["wrapper"]:
                    raise RuntimeError("A later adapter replaced RoPE; refusing to overwrite it")
                setattr(module, name, record["original"])
                del _FUNCTION_PATCHES[key]
        self._functions.clear()
        self._installed = False


def install(tts, features=(), **kwargs):
    """Default: observe only. Optional features never become implicit defaults."""
    return PrecisionAdapter(features, **kwargs).install(tts)


def install_dtype_observation(tts):
    """Return a handle exposing .signature, .snapshot() and .restore()."""
    return install(tts)


def install_fp16_boundaries(tts, features):
    """Explicit experimental features, with no implicit group/global half cast.

    Start with ["vocoder_input_fp16"] or ["gpt_input_fp16"] independently.
    CFM's progressively wider experiment is ["cfm_inputs_fp16",
    "cfm_time_embedding_fp16", "cfm_rope_fp16", "cfm_wn_mask_fp16",
    "cfm_attention_mask_fp16"]. Euler state/timestep phase remain unchanged.
    Every combination has a different cache signature and needs audio review.
    """
    if not features:
        raise ValueError("Select explicit precision features, or use dtype observation")
    return install(tts, features)


PROMPT_VERSION = "indextts25_mlx_acoustic_prompt_tail_crop_v1"


class AcousticPromptAdapter:
    """Crop paired acoustic prompt frames only; never shorten GPT references.

    Install with ``max_seconds=3.0`` to enable. ``None`` is an explicit no-op.
    The native 2.5 acoustic frame clock is hop 256 / sample rate 22050 Hz.
    Arrays are sliced as lazy views. No silence, speed or diffusion edit occurs.
    """
    def __init__(self, max_seconds=None):
        import math
        if max_seconds is not None and (not math.isfinite(max_seconds) or max_seconds <= 0):
            raise ValueError("Acoustic prompt duration must be positive and finite")
        self.max_seconds = max_seconds
        self.max_frames = None if max_seconds is None else int(max_seconds * 22050 // 256)
        if self.max_frames is not None and self.max_frames < 1:
            raise ValueError("Acoustic prompt limit is shorter than one frame")
        self.records = []
        self._methods = []
        self._functions = []
        self.signature = {"version": PROMPT_VERSION, "enabled": max_seconds is not None,
                          "max_seconds": max_seconds, "max_frames": self.max_frames,
                          "sample_rate": 22050, "hop_length": 256, "keep": "paired_tail_frames",
                          "gpt_reference_style_conditioning_codes": "unchanged"}
        self.signature["sha256"] = hashlib.sha256(json.dumps(
            self.signature, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def install(self, tts):
        if self._methods:
            raise RuntimeError("Acoustic prompt adapter is already installed")
        if self.max_frames is None:
            return self
        def synthesize(original, args, kwargs):
            if args:
                raise TypeError("Expected keyword-only IndexTTSv25._synthesize_segment")
            prompt = kwargs["prompt_condition"]
            mel = kwargs["ref_mel"]
            if len(prompt.shape) != 3 or len(mel.shape) != 3 or prompt.shape[0] != mel.shape[0]:
                raise ValueError("Acoustic prompt shapes must be compatible B,T,C and B,M,T")
            original_frames = prompt.shape[1]
            if original_frames != mel.shape[-1] or original_frames < 1:
                raise ValueError("Paired acoustic prompt and mel must have equal positive frame counts")
            kept = min(original_frames, self.max_frames)
            changed = dict(kwargs)
            if kept < original_frames:
                changed["prompt_condition"] = prompt[:, -kept:, :]
                changed["ref_mel"] = mel[:, :, -kept:]
            self.records.append({"original_frames": original_frames, "kept_frames": kept,
                                 "removed_frames": original_frames - kept,
                                 "kept_seconds": kept * 256 / 22050,
                                 "applied": kept < original_frames})
            return original(**changed)
        _patch_method(self, tts, "_synthesize_segment", synthesize)
        return self

    def snapshot(self):
        return {"signature": self.signature, "calls": list(self.records),
                "quality_status": "experimental acoustic context reduction; listening approval required"}

    def restore(self):
        # The restoration mechanism is shared and contains no MLX operations.
        PrecisionAdapter.restore(self)


def install_prompt_crop(tts, max_seconds=None):
    return AcousticPromptAdapter(max_seconds).install(tts)
