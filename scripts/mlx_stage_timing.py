"""Optional synchronized GPT / CFM / BigVGAN wall-clock diagnostics.

Stdlib-only import. The installation does not evaluate a model. At stage entry
pending input arrays are materialized outside the stage clock; at stage exit
returned arrays are materialized and the MLX stream synchronized before stopping
the clock. Original arguments and exact returned objects are preserved. No
casts, arithmetic, RNG calls, parameter/weight edits or cache mutation are added.

These barriers change scheduling and are diagnostic overhead. Measure actual
speedups again WITHOUT this helper and verify baseline audio hashes both ways.
Install after other adapters; restore this one first (strict reverse order).
"""
import hashlib
import json
import time

VERSION = "indextts25_mlx_stage_eval_sync_v1"
_PATCHES = {}


def _leaves(value, array_type):
    if isinstance(value, array_type):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _leaves(child, array_type)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _leaves(child, array_type)


def _patch(handle, instance, name, callback):
    key = type(instance), name
    record = _PATCHES.get(key)
    if record is None:
        original = getattr(key[0], name)
        record = {"original": original, "own": name in key[0].__dict__, "handlers": {}}
        def dispatched(obj, *args, **kwargs):
            selected = record["handlers"].get(id(obj))
            if selected is None:
                return original(obj, *args, **kwargs)
            return selected(lambda *a, **k: original(obj, *a, **k), args, kwargs)
        record["wrapper"] = dispatched
        setattr(key[0], name, dispatched)
        _PATCHES[key] = record
    if id(instance) in record["handlers"]:
        raise RuntimeError("Timing is already installed for this model method")
    record["handlers"][id(instance)] = callback
    handle._methods.append((key, id(instance)))


class _TimedVocoder:
    """Works for eager nn.Module and an already compiled callable alike."""
    def __init__(self, original, callback):
        self.original = original
        self.callback = callback
    def __call__(self, *args, **kwargs):
        return self.callback(self.original, args, kwargs)
    def __getattr__(self, name):
        return getattr(self.original, name)


class StageTimer:
    def __init__(self, runtime, *, _clock=time.perf_counter):
        self.runtime = runtime
        self.clock = _clock
        self.records = []
        self._methods = []
        self._vocoder = None
        self.active = None
        self.signature = {
            "version": VERSION, "diagnostic_barriers": True,
            "stages": ["gpt_semantic_generation", "cfm_inference", "bigvgan"],
            "input_policy": "eval all MLX input leaves + synchronize before stage clock",
            "output_policy": "eval all MLX result leaves + synchronize inside stage clock",
            "math_or_rng_changed": False,
            "official_speed_claim_use": "disabled; use an uninstrumented matched rerun",
        }
        self.signature["sha256"] = hashlib.sha256(json.dumps(
            self.signature, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _drain(self, value):
        arrays = list(_leaves(value, self.runtime.array))
        if arrays:
            self.runtime.eval(*arrays)
        self.runtime.synchronize()

    def _timed(self, name):
        def timed(original, args, kwargs):
            before = self.clock()
            self._drain((args, kwargs))
            ready = self.clock()
            record = {"stage": name, "utterance": None if self.active is None else self.active["name"],
                      "input_drain_s": ready - before, "success": False}
            try:
                result = original(*args, **kwargs)
                self._drain(result)
                record["wall_s"] = self.clock() - ready
                record["success"] = True
                return result
            except BaseException as exc:
                record["wall_s"] = self.clock() - ready
                record["error_type"] = type(exc).__name__
                raise
            finally:
                self.records.append(record)
        return timed

    def install(self, tts):
        if self._methods or self._vocoder is not None:
            raise RuntimeError("Stage timer is already installed")
        original_vocoder = tts.bigvgan_mlx
        if not callable(original_vocoder):
            raise TypeError("Expected callable native MLX BigVGAN")
        try:
            _patch(self, tts, "_generate_semantic_codes", self._timed("gpt_semantic_generation"))
            _patch(self, tts.s2mel_mlx.cfm, "inference", self._timed("cfm_inference"))
            proxy = _TimedVocoder(original_vocoder, self._timed("bigvgan"))
            # IndexTTSv25 is a plain Python controller, not an MLX nn.Module.
            # Its vocoder can already be mx.compile(...) with an immutable type;
            # do not monkeypatch that native callable's class or Module tree.
            tts.bigvgan_mlx = proxy
            self._vocoder = tts, original_vocoder, proxy
            return self
        except BaseException:
            self.restore()
            raise

    def begin(self, name):
        if self.active is not None:
            raise RuntimeError("Finish the previous timed utterance first")
        # Any model/reference work must be drained by caller before this begin
        # if the desired scope is a single generation rather than cold setup.
        self.active = {"name": str(name), "index": len(self.records), "start": self.clock()}

    def finish(self):
        if self.active is None:
            raise RuntimeError("Begin a timed utterance before finishing")
        elapsed = self.clock() - self.active["start"]
        rows = self.records[self.active["index"]:]
        totals = {name: sum(row["wall_s"] for row in rows if row["stage"] == name)
                  for name in self.signature["stages"]}
        drains = sum(row["input_drain_s"] for row in rows)
        result = {"utterance": self.active["name"], "instrumented_wall_s": elapsed,
                  "stages": list(rows), "stage_wall_totals_s": totals,
                  "input_drain_total_s": drains,
                  "other_cpu_preparation_and_hook_s": elapsed - sum(totals.values()) - drains,
                  "successful": bool(rows) and all(row["success"] for row in rows),
                  "speedup_claim_eligible": False}
        self.active = None
        return result

    def snapshot(self):
        return {"signature": self.signature, "records": list(self.records)}

    def restore(self):
        if self._vocoder is not None:
            tts, original, proxy = self._vocoder
            if tts.bigvgan_mlx is not proxy:
                raise RuntimeError("Restore newer adapters first; vocoder was replaced later")
            tts.bigvgan_mlx = original
            self._vocoder = None
        for key, identity in reversed(self._methods):
            record = _PATCHES[key]
            record["handlers"].pop(identity, None)
            if not record["handlers"]:
                cls, name = key
                if getattr(cls, name) is not record["wrapper"]:
                    raise RuntimeError("Restore newer adapters first; timed method was replaced later")
                if record["own"]:
                    setattr(cls, name, record["original"])
                else:
                    delattr(cls, name)
                del _PATCHES[key]
        self._methods.clear()
        self.active = None


def install_stage_timing(tts, *, _runtime=None, _clock=time.perf_counter):
    """Return .begin(name), .finish(), .snapshot(), .signature, .restore()."""
    if _runtime is None:
        import mlx.core as mx
        _runtime = mx
    return StageTimer(_runtime, _clock=_clock).install(tts)
