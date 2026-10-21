"""Offline native-MLX whole-utterance TTS, with independently verified caches.

Planning and complete audio-cache hits import only the standard library. This
runner never edits a timeline or imports legacy Torch-generated audio. Listening
approval remains required. Run with the isolated native-MLX Python environment.
"""
import argparse
import ast
import contextlib
import fcntl
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import struct
import subprocess
import sys
import time
import wave
import warnings
import zipfile

from continuous_tts import digest, fingerprint, save, read, confined
from mlx_offline import configure_environment, install_strict_local_preprocessing, local_paths

SCHEMA = "native_mlx_whole_utterance_v1"
SPEAKER_SCHEMA = "native_mlx_speaker_features_v1"
BACKEND_COMMIT = "a6d8fef49b3c36fa8bccaf5a780091e9b864f8e7"
TENSOR_KEYS = ("spk_cond_emb", "ref_mel", "style", "prompt_condition")
EXPERIMENTAL_PROFILES = ("native-half25", "hybrid-fp32-25", "hybrid-fp16-25")
# Preserve the original profile group for callers comparing its three recipes.
PAD_EAGER_PROFILE = "hybrid-fp16-25-pad-eager"
GPT_PRESERVE_PAD_EAGER_PROFILE = "hybrid-gpt-preserve-25-pad-eager"
GPT_PRESERVE_8_PAD_EAGER_PROFILE = "hybrid-gpt-preserve-8-pad-eager"
GPT_PRESERVE_PAD_EAGER_PROFILES = (GPT_PRESERVE_PAD_EAGER_PROFILE, GPT_PRESERVE_8_PAD_EAGER_PROFILE)
PAD_EAGER_PROFILES = (PAD_EAGER_PROFILE, *GPT_PRESERVE_PAD_EAGER_PROFILES)
_PROFILE_IMPLEMENTATION_PATH = Path(__file__).resolve()
AVAILABLE_EXPERIMENTAL_PROFILES = EXPERIMENTAL_PROFILES + PAD_EAGER_PROFILES
VERIFIED_COMPILE_MLX_VERSION = "0.32.4"


def build_experimental_profile(name, root):
    """Resolve real experiment inputs using standard-library-only helpers."""
    if name is None:
        return None
    if name not in AVAILABLE_EXPERIMENTAL_PROFILES:
        raise ValueError("Unknown experimental MLX profile")
    from mlx_precision import PrecisionAdapter, AcousticPromptAdapter, VERSION
    if VERSION != "indextts25_mlx_local_precision_v2":
        raise ValueError("Experimental profiles require the reviewed precision v2 helper")
    features = ["gpt_input_fp16", "cfm_inputs_fp16", "cfm_time_embedding_fp16",
                "cfm_rope_fp16", "cfm_wn_mask_fp16", "cfm_attention_mask_fp16"]
    if name in GPT_PRESERVE_PAD_EAGER_PROFILES:
        features.remove("gpt_input_fp16")
    files = ["mlx_precision.py"]
    native = name == "native-half25"
    if native:
        features.append("vocoder_input_fp16")
        files += ["mlx_vocoder_filters.py", "mlx_preclip_guard.py"]
        from mlx_preclip_guard import VERSION as guard_version
        vocoder = {"backend": "native_mlx", "input_precision": "float16",
                   "private_filter_precision": "float16", "private_filter_policy_version":
                   "native_bigvgan_private_resample_buffers_fp16_v2", "expected_buffers": 218,
                   "guard_version": guard_version}
    else:
        files += ["torch_vocoder_bridge.py", "preclip_guard.py"]
        from torch_vocoder_bridge import inventory, identity as bridge_identity
        precision = "float32" if name == "hybrid-fp32-25" else "float16"
        vocoder = bridge_identity(inventory(root), precision=precision, device="mps", preclip=True)
    result = {"name": name, "integration_version": "whole_utterance_experimental_profiles_v1",
            "helper_sha256": {file: digest(Path(__file__).with_name(file)) for file in files},
            "precision": PrecisionAdapter(features).signature,
            "acoustic_prompt": AcousticPromptAdapter(3.).signature,
            "vocoder": vocoder, "safe_ceiling": .95,
            "diffusion_steps": 25, "cfm_compile": True, "hotpath_compile": False,
            "quality_status": "experimental_unapproved_listening_required"}
    if name in PAD_EAGER_PROFILES:
        from mlx_reflect_pad import VERSION as padding_version, WAVENET_SHA256, CFM_SHA256
        file = "mlx_reflect_pad.py"
        result["helper_sha256"][file] = digest(Path(__file__).with_name(file))
        result["cfm_compile"] = False
        result["integration_version"] = "whole_utterance_pad_eager_profile_v1"
        result["cfm_padding"] = {"version": padding_version,
            "source_sha256": result["helper_sha256"][file],
            "wavenet_sha256": WAVENET_SHA256, "cfm_sha256": CFM_SHA256}
        result["cfm_execution"] = {"mode": "eager", "compiled_estimator_present": False,
            "vendor_compile_check_suppressed": True, "fallback_used": False}
    if name in GPT_PRESERVE_PAD_EAGER_PROFILES:
        from mlx_logits_guard import identity as logits_guard_identity
        file = "mlx_logits_guard.py"
        result["helper_sha256"][file] = digest(Path(__file__).with_name(file))
        result["integration_version"] = "whole_utterance_gpt_preserve_pad_eager_profile_v1"
        result["semantic_logits_guard"] = logits_guard_identity(check_mode="all_steps")
        result["gpt_activation_policy"] = "native input/KV promotion; no explicit gpt_input_fp16 cast; GPT8bit weights unchanged"
    if name == GPT_PRESERVE_8_PAD_EAGER_PROFILE:
        result["diffusion_steps"] = 8
        result["helper_sha256"]["mlx_tts.py"] = digest(_PROFILE_IMPLEMENTATION_PATH)
        result["integration_version"] = "whole_utterance_gpt_preserve_8_pad_eager_profile_v1"
        result["cfm_steps_guard"] = _cfm_step_signature()
    return result


def _cfm_step_signature():
    return {"version": "indextts25_eager_cfm_actual_steps_v1",
            "source_sha256": digest(_PROFILE_IMPLEMENTATION_PATH), "requested_steps": 8,
            "cfm_calls": 1, "euler_grid_points": 9, "estimator_calls": 8,
            "tensor_or_sampling_math_changed": False, "extra_tensor_readback": False}


def _valid_cfm_step_record(record, signature):
    if not isinstance(record, dict) or record.get("signature") != signature:
        return False
    transaction = record.get("transaction", {})
    return (isinstance(transaction, dict)
            and isinstance(transaction.get("utterance_id"), str)
            and bool(transaction["utterance_id"].strip())
            and transaction.get("completed") is True
            and type(transaction.get("requested_steps")) is int
            and transaction["requested_steps"] == 8
            and all(type(transaction.get(name)) is int and transaction[name] == expected
                    for name, expected in (("inference_calls", 1), ("original_inference_calls", 1),
                        ("actual_n_timesteps", 8), ("solver_calls", 1), ("original_solver_calls", 1),
                        ("euler_grid_points", 9), ("actual_diffusion_steps", 8),
                        ("estimator_calls", 8), ("original_estimator_calls", 8))))


def validate_compile_runtime(identity, parameters):
    execution = parameters["execution"]
    requested = execution["cfm_compile"] or execution["hotpath_compile"]
    versions = identity["runtime_versions"]
    if requested and any(versions.get(name) != VERIFIED_COMPILE_MLX_VERSION for name in ("mlx", "mlx-metal")):
        raise ValueError("Compilation requires locally verified MLX/Metal == " + VERIFIED_COMPILE_MLX_VERSION
                         + "; other versions must be independently validated before enabling compilation")
    return {"requested": bool(requested), "verified_compile_release": VERIFIED_COMPILE_MLX_VERSION,
            "actual_mlx_release": versions.get("mlx"), "actual_metal_release": versions.get("mlx-metal"),
            "requested_compilation_runtime_verified": bool(requested)}


def emit(event, **fields):
    print(json.dumps({"event": event, "unix": time.time(), **fields}, ensure_ascii=False), flush=True)


def package_version(name):
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def model_identity(root, model_dir):
    vendor = root / "vendor/mlx-indextts2-inference"
    commit = subprocess.check_output(["git", "-C", str(vendor), "rev-parse", "HEAD"], text=True).strip()
    if commit != BACKEND_COMMIT:
        raise ValueError("Native MLX backend revision differs from the pinned runner revision")
    path = model_dir / "model_manifest.json"
    manifest = read(path)
    if str(manifest.get("model_version")) != "2.5" or not manifest.get("source_revision"):
        raise ValueError("Missing or incompatible converted model manifest")
    for component in manifest["components"].values():
        asset = confined(model_dir, component["file"])
        if not asset.is_file() or asset.stat().st_size != component["bytes"]:
            raise ValueError("Converted component missing or size mismatch: " + str(asset))
    paths = local_paths(root)
    auxiliary = [paths["w2v_dir"] / name for name in
                 ("model.safetensors", "config.json", "preprocessor_config.json")]
    auxiliary += [paths["campplus"], paths["source_model"] / "wav2vec2bert_stats.pt"]
    locked = {}
    for model in read(root / "reports/models.lock.json")["models"]:
        for entry in model["files"]:
            locked[str(confined(root, str(Path(model["directory"]) / entry["path"])))] = entry
    aux = []
    for actual in auxiliary:
        entry = locked.get(str(actual.resolve()))
        if not entry or not actual.is_file() or actual.stat().st_size != entry["size"]:
            raise ValueError("Missing or unpinned preprocessing auxiliary: " + str(actual))
        aux.append({"path": str(actual), "sha256": entry["sha256"], "bytes": entry["size"]})
    # Binding actual source code catches local patches even when Git HEAD is unchanged.
    code = {str(p.relative_to(vendor)): digest(p) for p in sorted((vendor / "mlx_indextts").rglob("*.py"))}
    copied_aux = {}
    for name in ("config.yaml", "config.json", "feat1.pt", "feat2.pt", "wav2vec2bert_stats.pt",
                 "multilingual_zh_ja_yue_char_del.tiktoken"):
        asset = model_dir / name
        if asset.is_file():
            copied_aux[name] = digest(asset)
    return {"backend": "native_mlx", "backend_commit": commit, "backend_code_sha256": code,
            "converted_manifest_sha256": digest(path), "model_revision": manifest["source_revision"],
            "quantization": manifest.get("quantization"), "converted_auxiliary_sha256": copied_aux,
            "strict_preprocessing_code_sha256": digest(Path(__file__).with_name("mlx_offline.py")),
            "preprocessing_auxiliaries": aux,
            "runtime_versions": {name: package_version(name) for name in
                                 ("mlx", "mlx-metal", "numpy", "soundfile", "torch", "transformers")},
            "python_version": platform.python_version()}


def effective_parameters(args, config):
    if not 0 < args.memory_limit_gb <= 64:
        raise ValueError("Invalid native MLX memory limit")
    calm = args.calm_strength if args.calm_strength is not None else float(config.get("tts_calm_strength", .35))
    if not 0 <= calm <= .8:
        raise ValueError("Invalid calm strength")
    parameters = {"language": "zh", "emotion": [0, 0, 0, 0, 0, 0, 0, calm]
            if args.emotion_mode == "calm" else None,
            "use_random": False, "diffusion_steps": 25, "cfg_rate": .7,
            "duration_factor": 1., "speed": 1., "seed": 42,
            "max_mel_tokens": 500, "max_text_tokens_per_segment": 580,
            "interval_silence": 0, "temperature": .8, "top_p": .8, "top_k": 30,
            "repetition_penalty": 10., "text_normalization": True,
            "execution": {"device": "mps", "cfm_compile": bool(getattr(args, "compile_cfm", False)),
                          "hotpath_compile": bool(getattr(args, "compile_hot_paths",
                                                          getattr(args, "enable_hotpath_compile", False))),
                          "memory_limit_gb": args.memory_limit_gb, "mlx_cache_limit_mib": 512},
            "output_subtype": "PCM_24", "punctuation_policy": "preserve_whole_utterance",
            "runner_postprocessing": "none",
            "vendor_waveform_policy": "peak-normalize above 1; clip to +/-0.99 inside vendor"}
    profile = build_experimental_profile(getattr(args, "experimental_profile", None), args.root.resolve())
    if profile:
        if parameters["execution"]["hotpath_compile"]:
            raise ValueError("Guarded experimental profiles require eager vocoder; do not add --compile-hot-paths")
        if profile["name"] in PAD_EAGER_PROFILES and bool(getattr(args, "compile_cfm", False)):
            raise ValueError("The pad-eager profile requires CFM compilation disabled; remove --compile-cfm")
        parameters["execution"]["cfm_compile"] = profile["cfm_compile"]
        parameters["execution"]["hotpath_compile"] = False
        parameters["diffusion_steps"] = profile["diffusion_steps"]
        parameters["experimental_profile"] = profile
    return parameters


def content_payload(row, reference_sha256, identity, parameters):
    text = row["text_zh"]
    tts_text = row.get("tts_pronunciation_text") or text
    if not isinstance(text, str) or not text.strip() or not isinstance(tts_text, str) or not tts_text.strip():
        raise ValueError("Empty or invalid utterance")
    return {"schema": SCHEMA, "text_zh": text, "effective_tts_text": tts_text,
            "reference_sha256": reference_sha256, "model_identity": identity, "parameters": parameters}


def speaker_payload(reference_sha256, identity):
    return {"schema": SPEAKER_SCHEMA, "reference_sha256": reference_sha256,
            "backend_commit": identity["backend_commit"],
            "backend_code_sha256": identity["backend_code_sha256"],
            "converted_manifest_sha256": identity["converted_manifest_sha256"],
            "model_revision": identity["model_revision"],
            "converted_auxiliary_sha256": identity["converted_auxiliary_sha256"],
            "strict_preprocessing_code_sha256": identity["strict_preprocessing_code_sha256"],
            "preprocessing_auxiliaries": identity["preprocessing_auxiliaries"],
            "runtime_versions": identity["runtime_versions"],
            "python_version": identity["python_version"]}


def cache_paths(folder, key, suffix=".wav"):
    return Path(folder) / (key + suffix), Path(folder) / (key + ".json")


def cached_entry(folder, key, payload):
    audio, sidecar = cache_paths(folder, key)
    try:
        entry = read(sidecar)
        if entry.get("schema") != SCHEMA or entry.get("cache_key") != key or entry.get("content") != payload:
            return None, "metadata_or_content_mismatch"
        if digest(audio) != entry["sha256"]:
            return None, "audio_sha256_mismatch"
        profile = payload["parameters"].get("experimental_profile")
        if profile:
            expected_steps = 8 if profile.get("name") == GPT_PRESERVE_8_PAD_EAGER_PROFILE else 25
            if (profile.get("name") not in AVAILABLE_EXPERIMENTAL_PROFILES
                    or type(profile.get("diffusion_steps")) is not int
                    or profile["diffusion_steps"] != expected_steps
                    or type(payload["parameters"].get("diffusion_steps")) is not int
                    or payload["parameters"]["diffusion_steps"] != expected_steps):
                return None, "native_profile_diffusion_steps_mismatch"
            record = entry.get("experimental_execution", {})
            if ("actual_diffusion_steps" in record
                    and record["actual_diffusion_steps"] != expected_steps):
                return None, "actual_diffusion_steps_mismatch"
            if expected_steps == 8 and (record.get("actual_diffusion_steps") != 8
                    or profile.get("cfm_steps_guard") != _cfm_step_signature()
                    or not _valid_cfm_step_record(record.get("cfm_step_observation"), profile["cfm_steps_guard"])):
                return None, "actual_cfm_steps_evidence_missing_or_invalid"
            guard = record.get("preclip_guard", {})
            peak = guard.get("protected_peak")
            if (record.get("profile") != profile["name"] or guard.get("conv_post_calls") != 1
                    or not isinstance(peak, (int, float)) or not math.isfinite(peak)
                    or not 0 <= peak <= profile["safe_ceiling"]
                    or guard.get("ceiling") != profile["safe_ceiling"]):
                return None, "experimental_guard_evidence_missing_or_invalid"
            if profile["name"] in PAD_EAGER_PROFILES:
                padding = record.get("cfm_padding", {})
                if (any(padding.get(k) != v for k, v in profile["cfm_padding"].items())
                        or record.get("cfm_execution") != profile["cfm_execution"]):
                    return None, "experimental_eager_padding_evidence_missing_or_invalid"
            if profile["name"] in GPT_PRESERVE_PAD_EAGER_PROFILES:
                raw_guard = record.get("semantic_logits_guard", {})
                raw = raw_guard.get("transaction", {})
                calls = raw.get("sampling_calls")
                if (raw_guard.get("signature") != profile["semantic_logits_guard"]
                        or not isinstance(calls, int) or calls < 1
                        or raw.get("checked_calls") != calls or raw.get("original_sampler_calls") != calls
                        or raw.get("nonfinite_rejected") is not False
                        or raw.get("first_raw_logits", {}).get("all_finite") is not True):
                    return None, "experimental_semantic_logits_evidence_missing_or_invalid"
        with wave.open(str(audio), "rb") as stream:
            if (stream.getnframes() != entry["frames"] or stream.getframerate() != entry["sample_rate"]
                    or stream.getnchannels() != 1 or stream.getsampwidth() != 3
                    or stream.getnframes() <= 0):
                return None, "pcm24_wave_header_mismatch"
        return entry, "verified_sha256_pcm24"
    except FileNotFoundError:
        return None, "not_present"
    except (ValueError, OSError, KeyError, wave.Error, EOFError):
        return None, "unreadable_cache"


def npz_metadata(path):
    """Read the scalar Unicode metadata .npy using stdlib, without NumPy/MLX."""
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        if not {name + ".npy" for name in TENSOR_KEYS} <= names:
            raise ValueError("NPZ feature tensors missing")
        info = archive.getinfo("metadata_json.npy")
        if info.file_size > 1024 * 1024:
            raise ValueError("Oversized NPZ metadata")
        data = archive.read(info)
    if data[:6] != b"\x93NUMPY":
        raise ValueError("Invalid NPZ metadata NPY header")
    version = tuple(data[6:8])
    if version == (1, 0):
        header_size, offset = struct.unpack("<H", data[8:10])[0], 10
    elif version in ((2, 0), (3, 0)):
        header_size, offset = struct.unpack("<I", data[8:12])[0], 12
    else:
        raise ValueError("Unsupported metadata NPY version")
    header = ast.literal_eval(data[offset:offset + header_size].decode("latin1"))
    if header["shape"] != () or header["fortran_order"]:
        raise ValueError("NPZ metadata must be a scalar string")
    dtype = header["descr"]
    payload = data[offset + header_size:]
    if dtype.startswith(("<U", ">U")):
        text = payload.decode("utf-32-le" if dtype.startswith("<") else "utf-32-be")
    elif dtype.startswith("|S"):
        text = payload.decode("utf8")
    else:
        raise ValueError("Unexpected NPZ metadata type")
    return json.loads(text.rstrip("\0"))


def cached_speaker(folder, key, payload):
    path, sidecar = cache_paths(folder, key, ".npz")
    try:
        entry = read(sidecar)
        if entry.get("schema") != SPEAKER_SCHEMA or entry.get("cache_key") != key or entry.get("content") != payload:
            return None, "metadata_or_content_mismatch"
        if digest(path) != entry["sha256"]:
            return None, "npz_sha256_mismatch"
        internal = npz_metadata(path)
        if (internal.get("model_revision") != payload["model_revision"]
                or internal.get("source_audio_sha256") != payload["reference_sha256"]
                or str(internal.get("model_version")) != "2.5"
                or internal.get("cache_schema_version") != 1):
            return None, "npz_source_or_revision_mismatch"
        return entry, "verified_sha256_and_npz_source_revision"
    except FileNotFoundError:
        return None, "not_present"
    except (ValueError, OSError, KeyError, EOFError, zipfile.BadZipFile):
        return None, "unreadable_speaker_cache"


def make_plan(args):
    root = args.root.resolve()
    job = confined(root, str(Path("jobs") / args.job))
    if args.max_segments < 1:
        raise ValueError("--max-segments must be positive")
    permission = read(job / "processing_request.json")
    if permission.get("processing_authorized") is not True or permission.get("scope") != "private_local_evaluation":
        raise ValueError("Authorized private local evaluation request required")
    if args.reference_override and not args.select_speaker:
        raise ValueError("--reference-override requires --select-speaker")
    selected = [row for row in read(job / "segments.json")["segments"]
                if args.select_speaker is None or row["speaker_id"] == args.select_speaker][:args.max_segments]
    if not selected:
        raise ValueError("No selected utterances")
    speakers = read(job / "speakers.json")
    model_dir = args.model_dir.resolve() if args.model_dir else root / "models/indextts-2.5-mlx-8bit"
    identity = model_identity(root, model_dir)
    parameters = effective_parameters(args, read(root / "config/local.json"))
    compile_runtime = validate_compile_runtime(identity, parameters)
    cache = args.cache_dir.resolve() if args.cache_dir else root / "cache/native_mlx_whole_v1"
    refs, planned = {}, []
    for position, row in enumerate(selected):
        if not row.get("translation_reviewed") or not row.get("speaker_reviewed"):
            raise ValueError("Unreviewed utterance: " + row["segment_id"])
        speaker = speakers[row["speaker_id"]]
        if not speaker.get("reviewed"):
            raise ValueError("Unreviewed source reference")
        reference = args.reference_override.resolve() if args.reference_override else confined(root, speaker["reference"])
        if reference.suffix.lower() == ".npz" or not reference.is_file():
            raise ValueError("Reference must be an existing original audio file, not a feature cache")
        if str(reference) not in refs:
            refs[str(reference)] = digest(reference)
        payload = content_payload(row, refs[str(reference)], identity, parameters)
        key = fingerprint(payload)
        entry, reason = cached_entry(cache / "audio", key, payload)
        ref_payload = speaker_payload(refs[str(reference)], identity)
        ref_key = fingerprint(ref_payload)
        ref_entry, ref_reason = cached_speaker(cache / "speakers", ref_key, ref_payload)
        planned.append({"segment_id": row["segment_id"], "speaker_id": row["speaker_id"],
                        "source_index": position, "source_start_s": row.get("start_s"),
                        "source_end_s": row.get("end_s"), "reference": str(reference),
                        "cache_key": key, "content": payload, "status": "cached" if entry else "pending",
                        "cache_check": reason, "audio": str(cache_paths(cache / "audio", key)[0]),
                        "speaker_cache_key": ref_key, "speaker_cache_content": ref_payload,
                        "speaker_cache": str(cache_paths(cache / "speakers", ref_key, ".npz")[0]),
                        "speaker_cache_status": "cached" if ref_entry else "pending",
                        "speaker_cache_check": ref_reason,
                        **({k: entry[k] for k in ("duration_s", "sha256", "frames", "sample_rate", "channels")}
                           if entry else {})})
        if entry and "experimental_execution" in entry:
            planned[-1]["experimental_execution"] = entry["experimental_execution"]
    order = sorted(planned, key=lambda row: (row["speaker_cache_key"], row["source_index"]))
    pending = [row for row in planned if row["status"] == "pending"]
    return {"schema": SCHEMA, "phase": "planned", "job": args.job, "cache_root": str(cache),
            "model_dir": str(model_dir), "created_at_unix": time.time(), "parameters": parameters,
            "selected_utterances": len(planned), "pending_unique_keys": len({r["cache_key"] for r in pending}),
            "pending_rows": [r["segment_id"] for r in pending],
            "reuse_rows": [r["segment_id"] for r in planned if r["status"] == "cached"],
            "generation_order": [r["segment_id"] for r in order], "utterances": planned,
            "model_asset_check": "manifest SHA and backend code SHA; converted weights size checked, not rehashed",
            "preprocessing_asset_check": "pinned lock SHAs and local sizes; actual auxiliary SHAs checked on NPZ miss",
            "quality_status": "unapproved_listening_required", "old_torch_cache_imported": False,
            "timeline_replaced": False, "model_loaded": False, "timings": {},
            "compilation": {"cfm": parameters["execution"]["cfm_compile"],
                            "hot_paths": parameters["execution"]["hotpath_compile"],
                            "explicit_opt_in_required": True, "runtime_guard": compile_runtime}}


def verify_preprocessing_assets(payload):
    for item in payload["preprocessing_auxiliaries"]:
        if digest(item["path"]) != item["sha256"]:
            raise ValueError("Preprocessing auxiliary SHA mismatch: " + item["path"])


def update_audio_row(row, entry):
    row.update({name: entry[name] for name in ("duration_s", "sha256", "frames", "sample_rate", "channels")})
    if "experimental_execution" in entry:
        row["experimental_execution"] = entry["experimental_execution"]


def configure_compilation_environment(execution):
    """Apply the signed option, never inherit an accidental ambient CFM switch."""
    value = "1" if execution["cfm_compile"] else "0"
    os.environ["MLX_INDEXTTS_COMPILE_CFM"] = value
    return {"MLX_INDEXTTS_COMPILE_CFM": value}


def generate_complete_utterance(tts, text, reference, **options):
    """Treat only the vendor's semantic-token-cap truncation warning as fatal."""
    with warnings.catch_warnings():
        warnings.filterwarnings("error", message=r"^generation reached max_mel_tokens=", category=RuntimeWarning)
        return tts.generate(text, reference, **options)


@contextlib.contextmanager
def hotpath_compilation(model_class, enabled):
    """Limit the eager-constructor override to this load, including on failure."""
    original = model_class._compile_hotpaths
    try:
        if not enabled:
            model_class._compile_hotpaths = lambda self: None
        yield
    finally:
        model_class._compile_hotpaths = original


class _EagerCFMExecution:
    """Disable the vendor compile/fallback branch even on a reused model."""
    def __init__(self, cfm):
        self.cfm = cfm
        self.previous = (cfm._compiled_estimator, cfm._compile_attempted)
        self.active = True
        cfm._compiled_estimator = None
        # True means the vendor's compile check is suppressed, not that this
        # adapter attempted compilation. No compiled call or fallback occurs.
        cfm._compile_attempted = True

    def verify(self):
        if self.cfm._compiled_estimator is not None or self.cfm._compile_attempted is not True:
            raise ValueError("Pad-eager CFM execution policy changed during synthesis")

    def restore(self):
        if self.active:
            self.cfm._compiled_estimator, self.cfm._compile_attempted = self.previous
            self.active = False


class _ActualCFMSteps:
    """Eight-step-only observation; call every captured original method once.

    Install after precision/padding so the captured solver retains their scopes.
    No tensor value, dtype, schedule or RNG is changed/read back by this observer.
    """
    INFERENCE_KEYS = ("mu", "x_lens", "prompt", "style", "f0", "n_timesteps",
                      "temperature", "inference_cfg_rate")
    SOLVER_KEYS = ("x", "x_lens", "prompt", "mu", "style", "f0", "t_span", "inference_cfg_rate")

    def __init__(self, cfm, signature):
        if signature != _cfm_step_signature():
            raise ValueError("Unknown eight-step CFM observer identity")
        self.cfm, self.cls, self.signature = cfm, type(cfm), signature
        self.transaction = None
        self.transactions = []
        self.originals = {name: (name in self.cls.__dict__, getattr(self.cls, name))
                          for name in ("inference", "solve_euler", "_call_estimator")}
        original_inference = self.originals["inference"][1]
        original_solver = self.originals["solve_euler"][1]
        original_estimator = self.originals["_call_estimator"][1]
        owner = self

        def inference(obj, *args, **kwargs):
            if obj is not owner.cfm:
                return original_inference(obj, *args, **kwargs)
            state = owner._state()
            values = owner._bind(owner.INFERENCE_KEYS, args, kwargs)
            n = values.get("n_timesteps")
            if type(n) is not int or n != 8:
                raise ValueError("Actual CFM inference is not the requested eight steps")
            state["inference_calls"] += 1
            state["actual_n_timesteps"] = n
            result = original_inference(obj, *args, **kwargs)
            state["original_inference_calls"] += 1
            return result

        def solver(obj, *args, **kwargs):
            if obj is not owner.cfm:
                return original_solver(obj, *args, **kwargs)
            state = owner._state()
            values = owner._bind(owner.SOLVER_KEYS, args, kwargs)
            grid = values.get("t_span")
            if grid is None or len(grid.shape) != 1 or grid.shape[0] != 9:
                raise ValueError("Actual eight-step Euler grid does not contain nine points")
            state["solver_calls"] += 1
            state["euler_grid_points"] = int(grid.shape[0])
            state["actual_diffusion_steps"] = int(grid.shape[0]) - 1
            result = original_solver(obj, *args, **kwargs)
            state["original_solver_calls"] += 1
            return result

        def estimator(obj, *args, **kwargs):
            if obj is not owner.cfm:
                return original_estimator(obj, *args, **kwargs)
            state = owner._state()
            state["estimator_calls"] += 1
            result = original_estimator(obj, *args, **kwargs)
            state["original_estimator_calls"] += 1
            return result

        self.wrappers = {"inference": inference, "solve_euler": solver, "_call_estimator": estimator}
        self.active = True
        for name, function in self.wrappers.items():
            setattr(self.cls, name, function)

    @staticmethod
    def _bind(names, args, kwargs):
        if len(args) > len(names) or set(kwargs) - set(names):
            raise ValueError("Unknown pinned CFM call arguments")
        result = dict(zip(names, args))
        if set(result) & set(kwargs):
            raise ValueError("Duplicate pinned CFM call arguments")
        result.update(kwargs)
        return result

    def _state(self):
        if self.transaction is None:
            raise RuntimeError("Begin actual CFM step observation before synthesis")
        return self.transaction

    def begin(self, identifier):
        self.verify()
        self.transaction = {"utterance_id": str(identifier), "requested_steps": 8, "completed": False,
                            **{name: 0 for name in ("inference_calls", "original_inference_calls",
                               "solver_calls", "original_solver_calls", "estimator_calls",
                               "original_estimator_calls")}}

    def verify(self):
        if not self.active or any(getattr(self.cls, name) is not function
                                  for name, function in self.wrappers.items()):
            raise RuntimeError("Actual CFM step observer was changed during synthesis")

    def finish(self):
        self.verify()
        state = self._state()
        state["completed"] = True
        result = {"signature": self.signature, "transaction": dict(state)}
        if not _valid_cfm_step_record(result, self.signature):
            state["completed"] = False
            raise ValueError("Actual CFM grid/call evidence differs from eight-step generation")
        self.transactions.append(dict(state))
        return result

    def snapshot(self):
        self.verify()
        return {"signature": self.signature, "transactions": list(self.transactions),
                "completed_utterances": len(self.transactions)}

    def restore(self):
        if not self.active:
            return
        self.verify()
        for name, (own, original) in self.originals.items():
            if own:
                setattr(self.cls, name, original)
            else:
                delattr(self.cls, name)
        self.active = False


class ExperimentalExecution:
    """Own process-local hooks and one complete-utterance guard transaction."""
    def __init__(self, profile):
        self.profile = profile
        self.handles = []
        self.guard = self.hybrid = self.precision = self.crop = self.filters = None
        self.padding = self.cfm_eager = None
        self.logits_guard = None
        self.steps_guard = None
        self.hybrid_calls_before = 0

    def install(self, tts, root):
        if not self.profile:
            return
        from mlx_precision import install_fp16_boundaries, install_prompt_crop
        self.precision = install_fp16_boundaries(tts, self.profile["precision"]["features"])
        self.handles.append(self.precision)
        if self.precision.signature != self.profile["precision"]:
            raise ValueError("Installed precision helper differs from planned profile")
        if self.profile["name"] in GPT_PRESERVE_PAD_EAGER_PROFILES:
            from mlx_logits_guard import install as install_logits_guard
            self.logits_guard = install_logits_guard(tts, check_mode="all_steps")
            self.handles.append(self.logits_guard)
            if self.logits_guard.signature != self.profile["semantic_logits_guard"]:
                raise ValueError("Installed raw-logit guard differs from planned profile")
        self.crop = install_prompt_crop(tts, self.profile["acoustic_prompt"]["max_seconds"])
        self.handles.append(self.crop)
        if self.crop.signature != self.profile["acoustic_prompt"]:
            raise ValueError("Installed acoustic crop differs from planned profile")
        if self.profile["name"] in PAD_EAGER_PROFILES:
            from mlx_reflect_pad import install as install_reflect_pad
            self.padding = install_reflect_pad(tts)
            self.handles.append(self.padding)
            if any(self.padding.signature.get(k) != v for k, v in self.profile["cfm_padding"].items()):
                raise ValueError("Installed reflect padding differs from planned profile")
            self.cfm_eager = _EagerCFMExecution(tts.s2mel_mlx.cfm)
            self.handles.append(self.cfm_eager)
        if self.profile["name"] == "native-half25":
            from mlx_vocoder_filters import install_filter_fp16
            from mlx_preclip_guard import install_preclip_guard
            self.filters = install_filter_fp16(tts.bigvgan_mlx)
            self.handles.append(self.filters)
            expected = self.profile["vocoder"]
            if (self.filters.signature["version"] != expected["private_filter_policy_version"]
                    or self.filters.signature["buffers"] != expected["expected_buffers"]):
                raise ValueError("Unexpected native private-filter policy or count")
            self.guard = install_preclip_guard(tts.bigvgan_mlx, enabled=True, ceiling=self.profile["safe_ceiling"])
            self.handles.append(self.guard)
        else:
            from torch_vocoder_bridge import install_mlx_torch_vocoder
            self.hybrid = install_mlx_torch_vocoder(tts, root,
                precision=self.profile["vocoder"]["precision"], preclip=True)
            self.handles.append(self.hybrid)
            if any(self.hybrid.metadata.get(k) != v for k, v in self.profile["vocoder"].items()):
                raise ValueError("Loaded Torch hybrid differs from planned vocoder identity")
        if self.profile["name"] == GPT_PRESERVE_8_PAD_EAGER_PROFILE:
            self.steps_guard = _ActualCFMSteps(tts.s2mel_mlx.cfm, self.profile["cfm_steps_guard"])
            self.handles.append(self.steps_guard)

    def begin(self, identifier):
        if self.logits_guard:
            self.logits_guard.begin(identifier)
        if self.cfm_eager:
            self.cfm_eager.verify()
        if self.guard:
            self.guard.begin(identifier)
        if self.hybrid:
            self.hybrid_calls_before = self.hybrid.vocoder.calls
        if self.steps_guard:
            self.steps_guard.begin(identifier)

    def finish(self):
        if not self.profile:
            return None
        guard = self.guard.finish() if self.guard else None
        hybrid = dict(self.hybrid.last_timing) if self.hybrid else None
        if self.hybrid:
            if self.hybrid.vocoder.calls - self.hybrid_calls_before != 1:
                raise ValueError("Expected one whole-utterance hybrid vocoder call")
            guard = hybrid.get("preclip_guard")
        if not guard or guard.get("conv_post_calls") != 1 or guard.get("protected_peak", 1) > self.profile["safe_ceiling"]:
            raise ValueError("Guarded experiment did not verify one finite protected utterance")
        result = {"profile": self.profile["name"], "preclip_guard": guard,
                "hybrid_timings": hybrid, "acoustic_prompt": self.crop.records[-1] if self.crop.records else None,
                "quality_status": "experimental_unapproved_listening_required"}
        if self.cfm_eager:
            self.cfm_eager.verify()
            result.update(cfm_padding=self.padding.signature,
                          cfm_execution=dict(self.profile["cfm_execution"]))
        if self.logits_guard:
            result["semantic_logits_guard"] = self.logits_guard.finish()
        if self.steps_guard:
            result["actual_diffusion_steps"] = 8
            result["cfm_step_observation"] = self.steps_guard.finish()
        return result

    def snapshot(self):
        if not self.profile:
            return None
        result = {"profile": self.profile["name"], "precision": self.precision.snapshot() if self.precision else None,
                "acoustic_prompt": self.crop.snapshot() if self.crop else None,
                "filters": self.filters.snapshot() if self.filters else None,
                "native_guard": self.guard.snapshot() if self.guard else None,
                "hybrid": self.hybrid.metadata if self.hybrid else None,
                "quality_status": "experimental_unapproved_listening_required"}
        if self.cfm_eager:
            self.cfm_eager.verify()
            result.update(cfm_padding=self.padding.signature,
                          cfm_execution=dict(self.profile["cfm_execution"]))
        if self.logits_guard:
            result["semantic_logits_guard"] = self.logits_guard.snapshot()
        if self.steps_guard:
            result["actual_diffusion_steps"] = 8
            result["cfm_step_observation"] = self.steps_guard.snapshot()
        return result

    def restore(self):
        for handle in reversed(self.handles):
            handle.restore()
        self.handles.clear()


@contextlib.contextmanager
def experimental_execution(tts, root, profile):
    execution = ExperimentalExecution(profile)
    try:
        execution.install(tts, root)
        yield execution
    finally:
        execution.restore()


def run(args, manifest):
    started = time.perf_counter()
    manifest["timings"].update(model_load_s=0., experimental_setup_s=0., ref_prepare_s=0., synthesis_s=0., write_pcm24_s=0.)
    cache = Path(manifest["cache_root"])
    cache.mkdir(parents=True, exist_ok=True)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    destination = output / "manifest.json"
    with (cache / ".runner.lock").open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another native MLX runner holds this cache; no second model loaded") from exc
        current_identity = model_identity(args.root.resolve(), Path(manifest["model_dir"]))
        validate_compile_runtime(current_identity, manifest["parameters"])
        profile = manifest["parameters"].get("experimental_profile")
        if profile and build_experimental_profile(profile["name"], args.root.resolve()) != profile:
            raise ValueError("Experimental helpers/weights changed after planning; rebuild the native MLX plan")
        reference_checks = {}
        by_key = {}
        for row in manifest["utterances"]:
            if row["content"]["model_identity"] != current_identity:
                raise ValueError("Model/runtime changed after planning; rebuild the native MLX plan")
            if row["content"]["parameters"] != manifest["parameters"]:
                raise ValueError("Effective row parameters changed after planning; rebuild the native MLX plan")
            if row["reference"] not in reference_checks:
                reference_checks[row["reference"]] = digest(row["reference"])
            if reference_checks[row["reference"]] != row["content"]["reference_sha256"]:
                raise ValueError("Original reference changed after planning; rebuild the native MLX plan")
            entry, reason = cached_entry(cache / "audio", row["cache_key"], row["content"])
            row.update(status="cached" if entry else "pending", cache_check=reason)
            if entry:
                update_audio_row(row, entry)
            by_key.setdefault(row["cache_key"], row)
        pending = sorted((r for r in by_key.values() if r["status"] == "pending"),
                         key=lambda r: (r["speaker_cache_key"], r["source_index"]))
        manifest.update(phase="loading" if pending else "complete", initial_cache_hit_unique_keys=len(by_key) - len(pending),
                        generated_unique_keys=0, pending_unique_keys=len(pending))
        save(destination, manifest)
        if pending:
            configure_environment(args.root.resolve())
            # This official switch must be set before model import/first CFM call.
            # Earlier local builds crashed here; compilation requires explicit opt-in.
            execution = manifest["parameters"]["execution"]
            manifest["compilation"]["applied_environment"] = configure_compilation_environment(execution)
            vendor = args.root.resolve() / "vendor/mlx-indextts2-inference"
            sys.path.insert(0, str(vendor))
            began = time.perf_counter()
            import mlx.core as mx
            import numpy as np
            import soundfile as sf
            from mlx_indextts import IndexTTSv25
            mx.set_default_device(mx.gpu)
            mx.set_cache_limit(512 * 2**20)
            install_strict_local_preprocessing(IndexTTSv25, args.root.resolve())
            with hotpath_compilation(IndexTTSv25, execution["hotpath_compile"]):
                tts = IndexTTSv25(manifest["model_dir"], device="mps", memory_limit_gb=execution["memory_limit_gb"])
            mx.synchronize()
            manifest["model_loaded"] = True
            manifest["timings"]["model_load_s"] = time.perf_counter() - began
            manifest["timings"].update(ref_prepare_s=0., synthesis_s=0., write_pcm24_s=0.)
            emit("model_loaded", wall_seconds=manifest["timings"]["model_load_s"],
                 compilation=manifest["compilation"])
            profile_began = time.perf_counter()
            with experimental_execution(tts, args.root.resolve(), profile) as experiment:
                mx.synchronize()
                manifest["timings"]["experimental_setup_s"] = time.perf_counter() - profile_began
                generation_options = {k: v for k, v in manifest["parameters"].items() if k in
                    {"language", "emotion", "use_random", "diffusion_steps", "cfg_rate", "duration_factor",
                     "speed", "seed", "max_mel_tokens", "max_text_tokens_per_segment", "interval_silence",
                     "temperature", "top_p", "top_k", "repetition_penalty", "text_normalization"}}
                prepared = {}
                for row in pending:
                    ref_key = row["speaker_cache_key"]
                    if ref_key not in prepared:
                        began = time.perf_counter()
                        ref_payload = row["speaker_cache_content"]
                        ref_path, ref_sidecar = cache_paths(cache / "speakers", ref_key, ".npz")
                        entry, reason = cached_speaker(cache / "speakers", ref_key, ref_payload)
                        from_cache = entry is not None
                        if not entry:
                            verify_preprocessing_assets(ref_payload)
                            ref_path.parent.mkdir(parents=True, exist_ok=True)
                            temporary = ref_path.with_name(ref_key + f".{os.getpid()}.partial.npz")
                            tts.save_speaker(row["reference"], str(temporary))
                            with np.load(temporary, allow_pickle=False) as features:
                                if not all(np.isfinite(features[name]).all() for name in TENSOR_KEYS):
                                    raise ValueError("Nonfinite speaker feature cache")
                            internal = npz_metadata(temporary)
                            if (internal.get("model_revision") != ref_payload["model_revision"]
                                    or internal.get("source_audio_sha256") != ref_payload["reference_sha256"]):
                                raise ValueError("Saved speaker cache source/revision mismatch")
                            temporary.replace(ref_path)
                            entry = {"schema": SPEAKER_SCHEMA, "cache_key": ref_key, "content": ref_payload,
                                     "sha256": digest(ref_path), "created_at_unix": time.time(),
                                     "strict_preprocessing_audit": getattr(tts, "_offline_preprocessing_audit", None)}
                            save(ref_sidecar, entry)
                        # The NPZ path takes vendor's feature-only branch, not W2V preprocessing.
                        tts._process_reference_audio(str(ref_path))
                        mx.synchronize()
                        elapsed = time.perf_counter() - began
                        manifest["timings"]["ref_prepare_s"] += elapsed
                        prepared[ref_key] = {"from_verified_npz": from_cache, "wall_seconds": elapsed,
                                             "npz_sha256": entry["sha256"], "path": str(ref_path)}
                        emit("reference_ready", speaker_id=row["speaker_id"], **prepared[ref_key])
                    row["reference_preparation"] = prepared[ref_key]
                    began = time.perf_counter()
                    experiment.begin(row["segment_id"])
                    waveform = np.asarray(generate_complete_utterance(tts, row["content"]["effective_tts_text"],
                                           prepared[ref_key]["path"], **generation_options))
                    mx.synchronize()
                    experimental_record = experiment.finish()
                    elapsed = time.perf_counter() - began
                    if waveform.ndim != 1 or waveform.size == 0 or not np.isfinite(waveform).all():
                        raise ValueError("Invalid nonfinite/empty/multichannel native waveform")
                    sample_rate = int(tts.sample_rate)
                    duration = waveform.size / sample_rate
                    peak = float(np.max(np.abs(waveform)))
                    if not .1 <= duration <= 180 or not .00001 < peak < 1.:
                        raise ValueError("Native waveform duration or peak outside accepted engineering limits")
                    info = dict(tts.last_generation_info)
                    if info.get("segments") not in (None, 1):
                        raise ValueError("Frontend split this whole utterance; review instead of silently accepting")
                    write_began = time.perf_counter()
                    audio_path, sidecar = cache_paths(cache / "audio", row["cache_key"])
                    audio_path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = audio_path.with_name(audio_path.stem + f".{os.getpid()}.partial.wav")
                    # PCM writing adds no clamp/gain. Explicit profile guard gain is recorded separately.
                    sf.write(str(temporary), waveform, sample_rate, subtype="PCM_24", format="WAV")
                    temporary.replace(audio_path)
                    entry = {"schema": SCHEMA, "cache_key": row["cache_key"], "content": row["content"],
                             "sha256": digest(audio_path), "frames": int(waveform.size), "sample_rate": sample_rate,
                             "channels": 1, "duration_s": duration, "peak_before_pcm24": peak,
                             "near_vendor_ceiling_samples": int(np.sum(np.abs(waveform) >= .98999)),
                             "generation_info": info, "synthesis_wall_seconds": elapsed,
                             "semantic_token_cap_warning_guard": True,
                             "reference_preparation": prepared[ref_key], "quality_status": "unapproved_listening_required",
                             "created_at_unix": time.time()}
                    if experimental_record:
                        entry["experimental_execution"] = experimental_record
                    save(sidecar, entry)
                    manifest["timings"]["synthesis_s"] += elapsed
                    manifest["timings"]["write_pcm24_s"] += time.perf_counter() - write_began
                    for equivalent in manifest["utterances"]:
                        if equivalent["cache_key"] == row["cache_key"]:
                            equivalent.update(status="generated", cache_check="generated_pcm24_sha256")
                            update_audio_row(equivalent, entry)
                    manifest["generated_unique_keys"] += 1
                    manifest["pending_unique_keys"] -= 1
                    manifest["phase"] = "synthesis"
                    save(destination, manifest)
                    emit("utterance_complete", segment_id=row["segment_id"], wall_seconds=elapsed,
                         duration_s=duration, remaining=manifest["pending_unique_keys"])
                manifest["experimental_runtime"] = experiment.snapshot()
        manifest.update(phase="complete", finished_at_unix=time.time(),
                        run_wall_seconds=time.perf_counter() - started,
                        cache_only_no_mlx_or_torch_import=("mlx.core" not in sys.modules and "torch" not in sys.modules))
        manifest["pending_rows"] = []
        manifest["reuse_rows"] = [r["segment_id"] for r in manifest["utterances"] if r["status"] == "cached"]
        save(destination, manifest)
        return manifest


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--root", type=Path, required=True)
    result.add_argument("--job", required=True)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--select-speaker")
    result.add_argument("--reference-override", type=Path)
    result.add_argument("--max-segments", type=int, default=5)
    result.add_argument("--plan-only", action="store_true")
    result.add_argument("--model-dir", type=Path)
    result.add_argument("--cache-dir", type=Path)
    result.add_argument("--emotion-mode", choices=("calm", "reference"), default="reference")
    result.add_argument("--calm-strength", type=float)
    result.add_argument("--memory-limit-gb", type=float, default=8.)
    result.add_argument("--compile-cfm", action="store_true",
                        help="Explicit experimental opt-in to CFM compilation; default is disabled")
    result.add_argument("--compile-hot-paths", "--enable-hotpath-compile", dest="compile_hot_paths",
                        action="store_true", help="Explicit opt-in to general hot-path compilation; default is disabled")
    result.add_argument("--experimental-profile", choices=AVAILABLE_EXPERIMENTAL_PROFILES,
                        help="Explicit reviewed experiment bundle; default leaves the deployed eager25 recipe unchanged")
    return result


def main(argv=None):
    began = time.perf_counter()
    args = parser().parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=True)
    try:
        manifest = make_plan(args)
        manifest["timings"]["planning_s"] = time.perf_counter() - began
        save(args.output / "manifest.json", manifest)
        if not args.plan_only:
            manifest = run(args, manifest)
        manifest["timings"]["cli_s"] = time.perf_counter() - began
        manifest["plan_only_no_mlx_or_torch_import"] = args.plan_only and "mlx.core" not in sys.modules and "torch" not in sys.modules
        save(args.output / "manifest.json", manifest)
        emit("finished", phase=manifest["phase"], selected=manifest["selected_utterances"],
             pending=manifest["pending_unique_keys"], cli_s=manifest["timings"]["cli_s"],
             model_loaded=manifest["model_loaded"])
        return manifest
    except Exception as exc:
        save(args.output / "failure.json", {"error": type(exc).__name__, "message": str(exc),
             "cli_s": time.perf_counter() - began, "timeline_replaced": False})
        raise


if __name__ == "__main__":
    main()
