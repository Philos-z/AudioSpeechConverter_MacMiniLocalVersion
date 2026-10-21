"""Independent, content-cached whole-utterance IndexTTS runner.

Plan mode uses only Python's standard library. Execution does not replace any
timeline or old TTS cache. One model, grouped reference ordering, intact commas,
and no post-generation silence/tempo processing. Default scope is five samples.
"""
import argparse
import contextlib
import fcntl
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import random
import sys
import time
import warnings
import wave


SCHEMA = "continuous_utterance_v1"
CORE_REPOS = {"IndexTeam/IndexTTS-2.5", "facebook/w2v-bert-2.0", "funasr/campplus",
              "nvidia/bigvgan_v2_22khz_80band_256x"}
CODE_FILES = ("indextts/infer_v2_5.py", "indextts/gpt/model_v2.py",
              "indextts/s2mel/modules/flow_matching.py")


def digest(path):
    sha = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for part in iter(lambda: stream.read(1024*1024), b""):
            sha.update(part)
    return sha.hexdigest()


def fingerprint(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.partial")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def read(path):
    return json.loads(Path(path).read_text())


def confined(root, relative):
    result = (root / relative).resolve()
    if not result.is_relative_to(root):
        raise ValueError("Path escapes project root")
    return result


def model_identity(root):
    manifest = read(root / "reports/models.lock.json")
    models = []
    for item in manifest["models"]:
        if item["repo"] not in CORE_REPOS:
            continue
        if not item.get("revision"):
            raise ValueError("Unpinned model revision")
        files = []
        for entry in item["files"]:
            actual = confined(root, str(Path(item["directory"]) / entry["path"]))
            if not actual.is_file() or actual.stat().st_size != entry["size"]:
                raise ValueError("Missing model asset or size mismatch: " + entry["path"])
            files.append({key: entry[key] for key in ("path", "size", "sha256")})
        models.append({"repo": item["repo"], "revision": item["revision"],
                       "files": sorted(files, key=lambda entry: entry["path"])})
    if {item["repo"] for item in models} != CORE_REPOS:
        raise ValueError("Required pinned model entries missing")
    code = {name: digest(root / "vendor/index-tts" / name) for name in CODE_FILES}
    glossary = root / "models/indextts-2.5/glossary.yaml"
    return {"models": sorted(models, key=lambda item: item["repo"]), "code_sha256": code,
            "actual_config_sha256": digest(root / "models/indextts-2.5/config.yaml"),
            "glossary_sha256": digest(glossary) if glossary.exists() else None,
            "runtime_versions": {name: importlib.metadata.version(name) for name in ("torch", "transformers", "torchaudio")}}


def effective_parameters(args, config):
    if args.cfm_steps not in (25, 12, 8):
        raise ValueError("Only 25, 12, or 8 CFM steps are supported")
    if args.cfm_steps != 25 and not args.experimental_preview:
        raise ValueError("12/8 steps require --experimental-preview; production default remains 25")
    calm = args.calm_strength if args.calm_strength is not None else float(config.get("tts_calm_strength", .35))
    if not 0 <= calm <= .8:
        raise ValueError("Invalid calm strength")
    parameters = {"device": args.device, "dtype": "float32", "use_bf16": False,
            "use_cuda_kernel": False, "use_deepspeed": False, "use_accel": False,
            "use_torch_compile": False, "use_qwen_emo": False, "lang": "ZH",
            "emo_vector": [0, 0, 0, 0, 0, 0, 0, calm] if args.emotion_mode == "calm" else None,
            "emo_alpha": 1., "duration_factor": 1., "num_beams": 1, "seed": 42,
            "use_random": False, "do_sample": True, "top_p": .8, "top_k": 30,
            "temperature": .8, "repetition_penalty": 10., "length_penalty": 0.,
            "max_mel_tokens": 500, "max_text_tokens_per_segment": 580,
            "text_normalization": True, "interval_silence": 0,
            "cfm_steps": args.cfm_steps, "cfm_cfg_rate": .7,
            "mps_math_environment": {name: os.environ.get(name) for name in
                                     ("PYTORCH_MPS_FAST_MATH", "PYTORCH_MPS_PREFER_METAL")},
            "punctuation_policy": "preserve_entire_utterance", "postprocessing": "none"}
    if getattr(args,"preclip_guard",False):
        parameters["preclip_guard"]={"mode":"bigvgan_conv_post_static_gain_before_internal_clamp_v2",
                                    "ceiling":.95,"scope":"single whole-utterance static gain",
                                    "supported_model_branch":"use_tanh_at_final=False"}
    return parameters


def content_payload(row, reference_sha256, identity, parameters):
    text = row["text_zh"]
    tts_text = row.get("tts_pronunciation_text") or text
    if not isinstance(text, str) or not text.strip() or not isinstance(tts_text, str) or not tts_text.strip():
        raise ValueError("Empty or invalid utterance")
    return {"schema": SCHEMA, "text_zh": text, "effective_tts_text": tts_text,
            "reference_sha256": reference_sha256, "model_identity": identity,
            "parameters": parameters}


def cache_paths(folder, key):
    return folder / (key + ".wav"), folder / (key + ".json")


def cached_entry(folder, key, payload):
    audio, metadata = cache_paths(folder, key)
    if not audio.is_file() or not metadata.is_file():
        return None, "not_present"
    try:
        entry = read(metadata)
        if entry.get("schema") != SCHEMA or entry.get("cache_key") != key or entry.get("content") != payload:
            return None, "metadata_or_content_mismatch"
        if entry.get("sha256") != digest(audio):
            return None, "audio_sha256_mismatch"
        with wave.open(str(audio), "rb") as stream:
            length = stream.getnframes()/stream.getframerate()
            if (stream.getnframes() != entry["frames"] or stream.getframerate() != entry["sample_rate"]
                    or stream.getnchannels() != entry["channels"] or not length > 0):
                return None, "wave_header_mismatch"
        return entry, "verified_sha256"
    except (ValueError, OSError, KeyError, wave.Error, EOFError):
        return None, "unreadable_cache"


def make_plan(args):
    root = args.root.resolve()
    job = confined(root, str(Path("jobs") / args.job))
    config = read(root / "config/local.json")
    permission = read(job / "processing_request.json")
    if permission.get("processing_authorized") is not True or permission.get("scope") != "private_local_evaluation":
        raise ValueError("Local processing request is required")
    rows = read(job / "segments.json")["segments"]
    speakers = read(job / "speakers.json")
    if args.reference_override is not None and args.select_speaker is None:
        raise ValueError("--reference-override requires --select-speaker")
    selected = [row for row in rows if args.select_speaker is None or row["speaker_id"] == args.select_speaker]
    selected = selected[:args.max_segments]
    if not selected:
        raise ValueError("No selected utterances")
    identity = model_identity(root)
    parameters = effective_parameters(args, config)
    cache = confined(root, "cache/continuous_tts_v1")
    references = {}
    planned = []
    for position, row in enumerate(selected):
        if not row.get("translation_reviewed") or not row.get("speaker_reviewed"):
            raise ValueError("Unreviewed selected utterance: " + row["segment_id"])
        speaker = speakers[row["speaker_id"]]
        if not speaker.get("reviewed"):
            raise ValueError("Unreviewed source reference")
        reference = args.reference_override.resolve() if args.reference_override else confined(root, speaker["reference"])
        if not reference.is_file():
            raise ValueError("Reference missing")
        if str(reference) not in references:
            references[str(reference)] = digest(reference)
        payload = content_payload(row, references[str(reference)], identity, parameters)
        key = fingerprint(payload)
        entry, reason = cached_entry(cache, key, payload)
        planned.append({"segment_id": row["segment_id"], "speaker_id": row["speaker_id"],
            "source_index": position, "source_start_s": row.get("start_s"),
            "source_end_s": row.get("end_s"), "reference": str(reference), "cache_key": key,
            "content": payload, "status": "cached" if entry else "pending",
            "cache_check": reason, "audio": str(cache_paths(cache, key)[0]),
            "duration_s": entry.get("duration_s") if entry else None,
            "sha256": entry.get("sha256") if entry else None,
            "frames": entry.get("frames") if entry else None,
            "sample_rate": entry.get("sample_rate") if entry else None,
            "channels": entry.get("channels") if entry else None})
    order = sorted(planned, key=lambda row: (row["content"]["reference_sha256"], row["speaker_id"], row["source_index"]))
    return {"schema": SCHEMA, "phase": "planned", "job": args.job, "cache_root": str(cache),
            "scope": "private_local_evaluation", "created_at_unix": time.time(),
            "default_sample_limit": 5, "selected_utterances": len(planned),
            "unique_content_keys": len({row["cache_key"] for row in planned}),
            "pending_unique_keys": len({row["cache_key"] for row in planned if row["status"] == "pending"}),
            "experimental_preview": args.cfm_steps != 25,
            "model_asset_check": "pinned manifest and local file sizes; large weights not rehashed in planning",
            "old_cache_imported": False, "timeline_replaced": False,
            "processing": "whole utterance; commas retained; no postprocess, inserted silence or tempo change",
            "generation_order": [row["segment_id"] for row in order], "utterances": planned,
            "parameters": parameters, "model_loaded": False}


def configure_environment(root):
    for key, value in {"HF_HOME": root/"models/.hf", "NLTK_DATA": root/"models/nltk_data",
        "MPLCONFIGDIR": root/"models/.runtime/matplotlib", "MODELSCOPE_CACHE": root/"models/.modelscope",
        "XDG_CACHE_HOME": root/"models/.runtime"}.items():
        os.environ.setdefault(key, str(value))
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] = "0"
    os.environ.setdefault("PYTORCH_MPS_HIGH_WATERMARK_RATIO", ".7")
    os.environ.setdefault("PYTORCH_MPS_LOW_WATERMARK_RATIO", ".5")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


def run(args, manifest):
    run_started = time.perf_counter()
    root = args.root.resolve()
    output = args.output.resolve()
    cache = Path(manifest["cache_root"])
    cache.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    destination = output / "manifest.json"
    # This mutex protects this runner's models and cache writes. Other legacy
    # runners/benchmarks must be scheduled by the caller; they do not use this lock.
    with (cache / ".runner.lock").open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another continuous_tts runner is active; no second model was loaded") from exc
        lock.seek(0); lock.truncate(); lock.write(json.dumps({"pid": os.getpid(), "started_at_unix": time.time()})); lock.flush()
        by_key = {}
        for row in manifest["utterances"]:
            entry, reason = cached_entry(cache, row["cache_key"], row["content"])
            row["status"] = "cached" if entry else "pending"
            row["cache_check"] = reason
            if entry:
                row.update({name: entry[name] for name in ("duration_s", "sha256", "frames", "sample_rate", "channels")})
            if entry and "preclip_guard" in entry:row["preclip_guard"]=entry["preclip_guard"]
            by_key.setdefault(row["cache_key"], row)
        pending = sorted((row for row in by_key.values() if row["status"] == "pending"),
                         key=lambda row: (row["content"]["reference_sha256"], row["speaker_id"], row["source_index"]))
        manifest["initial_cache_hit_unique_keys"] = len(by_key)-len(pending)
        manifest["pending_unique_keys"] = len(pending)
        manifest["phase"] = "ready_without_model" if not pending else "loading"
        save(destination, manifest)
        if not pending:
            manifest["phase"] = "complete"
            manifest["generated_unique_keys"] = 0
            manifest["finished_at_unix"] = time.time()
            manifest["run_wall_seconds"] = time.perf_counter()-run_started
            manifest["torch_imported_at_finish"] = "torch" in sys.modules
            manifest["cache_only_no_torch_import"] = "torch" not in sys.modules
            save(destination, manifest)
            return manifest

        configure_environment(root)
        import numpy as np
        import soundfile as sf
        import torch
        torch.set_num_threads(2)
        if args.device == "mps":
            if not torch.backends.mps.is_available():
                raise RuntimeError("MPS unavailable; no silent fallback")
            torch.mps.set_per_process_memory_fraction(.7)
        elif args.device == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("CUDA unavailable; no silent fallback")
        def synchronize():
            if args.device == "mps": torch.mps.synchronize()
            elif args.device == "cuda": torch.cuda.synchronize()
        sys.path.insert(0, str(root / "vendor/index-tts"))
        parameters = manifest["parameters"]
        with (output / "model.log").open("a", buffering=1) as log:
            started = time.perf_counter()
            with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                from indextts.infer_v2_5 import IndexTTS2, apply_pronunciation_annotations
                model = root / "models/indextts-2.5"
                tts = IndexTTS2(cfg_path=str(model/"config.yaml"), model_dir=str(model), device=args.device,
                    use_bf16=False, use_cuda_kernel=False, use_deepspeed=False, use_accel=False,
                    use_torch_compile=False, use_qwen_emo=False)
            synchronize()
            manifest["load_seconds"] = time.perf_counter()-started
            manifest["model_loaded"] = True
            manifest["submodel_devices"] = {name: str(next(getattr(tts, name).parameters()).device)
                for name in ("gpt", "semantic_model", "semantic_codec", "s2mel", "campplus_model", "bigvgan")}
            manifest["gpt_attention_class"] = type(tts.gpt.gpt.h[0].attn).__module__ + "." + type(tts.gpt.gpt.h[0].attn).__name__
            manifest["gpt_attention_implementation"] = tts.gpt.gpt.config._attn_implementation
            if parameters["cfm_steps"] != 25:
                original_cfm = tts.s2mel.models["cfm"].inference
                def experimental_cfm(mu, x_lens, prompt, style, f0, n_timesteps, *positional, **keywords):
                    return original_cfm(mu, x_lens, prompt, style, f0, parameters["cfm_steps"], *positional, **keywords)
                tts.s2mel.models["cfm"].inference = experimental_cfm
            preclip_guard=None
            if "preclip_guard" in parameters:
                from preclip_guard import WholeUtterancePreclipGuard
                preclip_guard=WholeUtterancePreclipGuard(tts.bigvgan,
                    parameters["preclip_guard"]["ceiling"]).install()
            manifest["phase"] = "generating"
            manifest["generated_unique_keys"] = 0
            save(destination, manifest)
            for row in pending:
                key = row["cache_key"]
                audio, metadata = cache_paths(cache, key)
                partial = cache / (key + f".{os.getpid()}.partial.wav")
                text = row["content"]["effective_tts_text"]
                # Refuse unintended model token chunking; each request remains one
                # utterance. Exact same ZH normalization as infer_v2_5 is previewed.
                normalized = tts.text_process.clean_pattern.sub(lambda match: tts.text_process.char_rep_map[match.group()], text)
                normalized = apply_pronunciation_annotations(tts.text_process.normalize(normalized).lower())
                if len(tts.split_text_by_tokens(normalized, parameters["max_text_tokens_per_segment"], "<|zh|> ")) != 1:
                    raise ValueError("Utterance needs semantic shortening before synthesis: " + row["segment_id"])
                random.seed(42); np.random.seed(42); torch.manual_seed(42)
                if args.device == "mps": torch.mps.manual_seed(42)
                began = time.perf_counter()
                options = {name: parameters[name] for name in ("emo_alpha", "duration_factor", "num_beams", "use_random",
                    "do_sample", "top_p", "top_k", "temperature", "repetition_penalty", "length_penalty", "max_mel_tokens",
                    "max_text_tokens_per_segment", "text_normalization", "interval_silence")}
                if parameters["emo_vector"] is not None:
                    options["emo_vector"] = parameters["emo_vector"]
                try:
                    guard_report=None
                    if preclip_guard is not None:preclip_guard.begin(row["segment_id"])
                    with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log), torch.inference_mode(), warnings.catch_warnings(record=True) as caught:
                        warnings.simplefilter("always")
                        tts.infer(spk_audio_prompt=row["reference"], text=text, output_path=str(partial), lang="ZH", **options)
                    synchronize()
                    if preclip_guard is not None:guard_report=preclip_guard.finish()
                    elapsed = time.perf_counter()-began
                    if any("generation stopped due to exceeding" in str(item.message) for item in caught):
                        raise ValueError("Model hit max_mel_tokens; output requires a shorter utterance")
                    samples, sample_rate = sf.read(partial, dtype="float32", always_2d=True)
                    if not len(samples) or not np.isfinite(samples).all() or np.max(np.abs(samples)) < .001:
                        raise ValueError("Invalid generated waveform")
                    entry = {"schema": SCHEMA, "cache_key": key, "content": row["content"],
                        "sha256": digest(partial), "frames": len(samples), "sample_rate": sample_rate,
                        "channels": samples.shape[1], "duration_s": len(samples)/sample_rate,
                        "inference_seconds": elapsed, "rtf": elapsed/(len(samples)/sample_rate),
                        "generated_at_unix": time.time(), "quality": "technical waveform check only; listening pending",
                        "experimental_preview": parameters["cfm_steps"] != 25,
                        "postprocessing": "none", "warnings": [str(item.message) for item in caught]}
                    if guard_report is not None:entry["preclip_guard"]=guard_report
                    partial.replace(audio)
                    save(metadata, entry)
                    manifest["generated_unique_keys"] += 1
                    for alias in manifest["utterances"]:
                        if alias["cache_key"] == key:
                            alias.update(status="generated", duration_s=entry["duration_s"], inference_seconds=elapsed,
                                         sha256=entry["sha256"], frames=entry["frames"],
                                         sample_rate=entry["sample_rate"], channels=entry["channels"])
                            if guard_report is not None:alias["preclip_guard"]=guard_report
                    manifest["pending_unique_keys"] = len({alias["cache_key"] for alias in manifest["utterances"]
                                                           if alias["status"] == "pending"})
                    save(destination, manifest)
                    print("GENERATED", row["segment_id"], round(elapsed, 3), flush=True)
                except Exception as exc:
                    partial.unlink(missing_ok=True)
                    manifest["phase"] = "failed"
                    manifest["error"] = {"segment_id": row["segment_id"], "type": type(exc).__name__, "message": str(exc)}
                    save(destination, manifest)
                    raise
            manifest["phase"] = "complete"
            manifest["finished_at_unix"] = time.time()
            manifest["run_wall_seconds"] = time.perf_counter()-run_started
            manifest["torch_imported_at_finish"] = "torch" in sys.modules
            manifest["cache_only_no_torch_import"] = False
            save(destination, manifest)
    return manifest


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--select-speaker")
    parser.add_argument("--reference-override", type=Path)
    parser.add_argument("--max-segments", type=int, default=5)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--device", choices=("mps", "cpu", "cuda"), default="mps")
    parser.add_argument("--cfm-steps", type=int, default=25)
    parser.add_argument("--experimental-preview", action="store_true")
    parser.add_argument("--emotion-mode", choices=("calm", "reference"), default="reference")
    parser.add_argument("--calm-strength", type=float)
    parser.add_argument("--preclip-guard", action="store_true",
                        help="Experiment v2: uniform conv_post gain before internal hard clamp; tanh branch refused")
    args = parser.parse_args(argv)
    if args.max_segments < 1:
        parser.error("--max-segments must be positive; default sample scope is five")
    return args


def main():
    cli_started = time.perf_counter()
    cli_started_unix = time.time()
    args = parse_args()
    manifest = make_plan(args)
    manifest["cli_started_at_unix"] = cli_started_unix
    try:
        if not args.plan_only:
            manifest = run(args, manifest)
    finally:
        manifest["cli_wall_seconds"] = time.perf_counter()-cli_started
        manifest["torch_imported_at_finish"] = "torch" in sys.modules
        manifest["cache_only_no_torch_import"] = (not manifest["model_loaded"] and "torch" not in sys.modules)
        save(args.output.resolve()/("plan.json" if args.plan_only else "manifest.json"), manifest)
    print(json.dumps({"phase": manifest["phase"], "selected": manifest["selected_utterances"],
        "pending_unique_keys": manifest["pending_unique_keys"], "model_loaded": manifest["model_loaded"],
        "output": str(args.output.resolve())}, ensure_ascii=False))


if __name__ == "__main__":
    main()
