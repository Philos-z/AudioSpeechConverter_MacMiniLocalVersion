#!/usr/bin/env python3
"""Local file MVP: prepare media, then render reviewed timed Chinese segments."""
import argparse
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time


def call(args):
    result = subprocess.run([str(x) for x in args], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr[-6000:])
    return result


def probe(file):
    return json.loads(call(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", file]).stdout)


def parse_loudness(stderr):
    start = stderr.rfind("{")
    if start < 0:
        raise ValueError("LOUDNESS_MEASUREMENT_MISSING")
    value, _ = json.JSONDecoder().raw_decode(stderr[start:])
    for key in ["input_i", "input_tp", "input_lra", "input_thresh", "target_offset"]:
        if key not in value or not math.isfinite(float(value[key])):
            raise ValueError("INVALID_LOUDNESS_MEASUREMENT")
    return value


def confined(root, path):
    value = (root / path).resolve()
    if not value.is_relative_to(root.resolve()):
        raise ValueError("PATH_OUTSIDE_PROJECT")
    return value


def validate_rows(rows, duration):
    seen = set()
    last_end = 0.0
    for row in sorted(rows, key=lambda r: r["start_s"]):
        identifier = row["segment_id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", identifier) or identifier in seen:
            raise ValueError("INVALID_OR_DUPLICATE_SEGMENT_ID")
        seen.add(identifier)
        start, end = row["start_s"], row["end_s"]
        if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in (start, end)):
            raise ValueError("NONFINITE_TIME")
        if not 0 <= start < end <= duration + 0.02 or start < last_end:
            raise ValueError("OUT_OF_RANGE_OR_OVERLAPPING_SEGMENT")
        if row.get("overlap") or not row.get("translation_reviewed") or not row.get("speaker_reviewed"):
            raise ValueError("SEGMENT_REVIEW_REQUIRED")
        if not isinstance(row.get("text_zh"), str) or not row["text_zh"].strip():
            raise ValueError("EMPTY_CHINESE_TRANSLATION")
        last_end = end
    if not rows:
        raise ValueError("NO_SEGMENTS")


def stamp(seconds):
    value = round(seconds * 1000)
    hours, value = divmod(value, 3600000)
    minutes, value = divmod(value, 60000)
    seconds, millis = divmod(value, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def settings(root):
    path = root / "config/local.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    maximum = float(data.get("max_video_seconds", 600))
    reserve = float(data.get("minimum_free_gib", 8))
    beams = int(data.get("tts_num_beams", 1))
    timing = data.get("tts_timing_mode", "preserve")
    if not 0 < maximum <= 3600 or not math.isfinite(reserve) or reserve < 1 or beams not in [1, 2, 3]:
        raise ValueError("INVALID_LOCAL_CONFIGURATION")
    if timing not in ["preserve", "fit"]:
        raise ValueError("INVALID_TIMING_MODE")
    return maximum, reserve, beams, timing


def prepare(args, root, job):
    maximum, reserve, _, _ = settings(root)
    if job.exists():
        raise ValueError("JOB_EXISTS: choose a new job id")
    source = args.input.resolve()
    metadata = probe(source)
    video = next(x for x in metadata["streams"] if x["codec_type"] == "video")
    audio = [x for x in metadata["streams"] if x["codec_type"] == "audio"][args.audio_track]
    video_start = float(video.get("start_time", 0))
    audio_start = float(audio.get("start_time", 0))
    if abs(video_start) > .05 or abs(audio_start) > .05:
        raise ValueError("MVP_REQUIRES_ZERO_BASED_STREAMS: normalize offsets with review first")
    duration = float(metadata["format"]["duration"])
    if not 0 < duration <= maximum:
        raise ValueError(f"MAX_DURATION_EXCEEDED: configured maximum is {maximum:g} seconds")
    if shutil.disk_usage(root).free < reserve * 2**30:
        raise ValueError("INSUFFICIENT_DISK")
    for folder in ["audio", "translation", "references", "tts/raw", "tts/aligned", "outputs", "logs"]:
        (job / folder).mkdir(parents=True, exist_ok=True)
    (job / "source.json").write_text(json.dumps({"input": str(source), "audio_track": args.audio_track,
                                                "duration_s": duration, "probe": metadata}, indent=2))
    call(["ffmpeg", "-v", "error", "-n", "-i", source, "-map", f"0:a:{args.audio_track}",
          "-vn", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s24le", job / "audio/original.wav"])
    call(["ffmpeg", "-v", "error", "-n", "-i", job / "audio/original.wav", "-ac", "1", "-ar", "16000",
          "-c:a", "pcm_s16le", job / "audio/asr16k.wav"])
    (job / "rights.json").write_text(json.dumps({"media_authorized": None, "voice_authorized": None,
                                                "scope": "fill_from_actual_authorization"}, indent=2))
    (job / "speakers.json").write_text(json.dumps({"S00": {"reference": "", "reviewed": False}}, indent=2))
    print("PREPARED", job, flush=True)


def render(args, root, job):
    import numpy as np
    import soundfile as sf
    import torch
    torch.manual_seed(42)
    _, reserve, beams, timing = settings(root)
    config_path=root/'config/local.json'
    voice_config=json.loads(config_path.read_text()) if config_path.exists() else {}
    calm=voice_config.get('tts_emotion_mode')=='calm'
    calm_strength=float(voice_config.get('tts_calm_strength',.35))
    if not 0<=calm_strength<=.8:raise ValueError('INVALID_CALM_STRENGTH')
    if shutil.disk_usage(root).free < reserve * 2**30:
        raise ValueError("INSUFFICIENT_DISK")
    rights = json.loads((job / "rights.json").read_text())
    if rights.get("media_authorized") is not True or rights.get("voice_authorized") is not True:
        raise ValueError("RIGHTS_UNCONFIRMED")
    source = json.loads((job / "source.json").read_text())
    rows = json.loads((job / "segments.json").read_text())["segments"]
    duration = source["duration_s"]
    validate_rows(rows, duration)
    rows = sorted(rows, key=lambda r: r["start_s"])
    speakers = json.loads((job / "speakers.json").read_text())
    for row in rows:
        speaker = speakers[row["speaker_id"]]
        reference = confined(root, speaker["reference"])
        if not speaker.get("reviewed") or not reference.is_file():
            raise ValueError("SPEAKER_REFERENCE_REVIEW_REQUIRED")
    background = job / "audio/background.wav"
    if not background.is_file() and not args.allow_silent_background:
        raise ValueError("BACKGROUND_MISSING: provide an M&E/background track or explicitly allow silence")
    os.environ.setdefault("HF_HOME", str(root / "models/.hf"))
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("NLTK_DATA", str(root / "models/nltk_data"))
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "0")
    os.environ.setdefault("PYTORCH_MPS_HIGH_WATERMARK_RATIO", "0.7")
    os.environ.setdefault("PYTORCH_MPS_LOW_WATERMARK_RATIO", "0.5")
    if args.device == "mps":
        torch.mps.set_per_process_memory_fraction(.7)
    sys.path.insert(0, str(root / "vendor/index-tts"))
    from indextts.infer_v2_5 import IndexTTS2
    model = root / "models/indextts-2.5"
    tts = IndexTTS2(cfg_path=str(model / "config.yaml"), model_dir=str(model), device=args.device,
                    use_bf16=False, use_cuda_kernel=False, use_deepspeed=False, use_accel=False,
                    use_torch_compile=False, use_qwen_emo=False)
    sr = 48000
    frames = round(duration * sr)
    dialogue = np.zeros((frames, 2), dtype=np.float32)
    metrics = {"device": tts.device, "background": "supplied" if background.is_file() else "explicit_silence",
               "quality": "unreviewed", "timing_mode": timing, "num_beams": beams,
               "emotion_mode":"calm" if calm else "audio_reference", "segments": []}
    for row in rows:
        if shutil.disk_usage(root).free < reserve * 2**30:
            raise ValueError("INSUFFICIENT_DISK")
        budget = row["end_s"] - row["start_s"]
        factor = 1.0
        raw_folder = job / "tts/raw"
        raw_folder.mkdir(parents=True, exist_ok=True)
        reference = confined(root, speakers[row["speaker_id"]]["reference"])
        emotion = confined(root, row["emotion_reference"]) if row.get("emotion_reference") else None
        if emotion and not emotion.is_file():
            raise ValueError("EMOTION_REFERENCE_MISSING")
        started = time.perf_counter()
        for attempt in range(1 if timing == "preserve" else 3):
            if args.device == "mps":
                torch.mps.empty_cache()
            # Retain the natural first attempt when an explicitly selected fit mode retries.
            raw = raw_folder / (row["segment_id"] + f"_attempt{attempt+1}.wav")
            with torch.inference_mode():
                text=row['text_zh'].replace('，','').replace('、','') if calm else row['text_zh']
                tts.infer(spk_audio_prompt=str(reference), text=text, lang="ZH", output_path=str(raw),
                          emo_audio_prompt=str(emotion) if emotion and not calm else None,
                          emo_vector=[0,0,0,0,0,0,0,calm_strength] if calm else None,emo_alpha=1. if calm else .6,
                          duration_factor=factor, use_random=False, num_beams=beams)
            samples, rate = sf.read(raw)
            actual = len(samples) / rate
            if actual <= 0 or not np.isfinite(samples).all() or np.max(np.abs(samples)) < .001:
                raise ValueError("INVALID_TTS_AUDIO")
            ratio = actual / budget
            if timing == "preserve":
                if actual > budget:
                    raise ValueError(f"TIMING_REVIEW_REQUIRED: {row['segment_id']} natural={actual:.3f} "
                                     f"target={budget:.3f}; shorten translation or review the time window")
                break
            if .90 <= ratio <= 1.10:
                break
            factor = min(2.0, max(.5, factor * budget / actual))
        if timing == "fit" and not .90 <= ratio <= 1.10:
            raise ValueError(f"TIMING_REVIEW_REQUIRED: {row['segment_id']} actual={actual:.3f} target={budget:.3f}")
        aligned = job / "tts/aligned" / (row["segment_id"] + ".wav")
        tempo = ratio if timing == "fit" else 1.0
        filters = (f"atempo={tempo:.8f}," if timing == "fit" else "") + f"apad,atrim=duration={budget:.8f}"
        call(["ffmpeg", "-v", "error", "-y", "-i", raw, "-af", filters,
              "-ar", str(sr), "-ac", "2", "-c:a", "pcm_f32le", aligned])
        samples, rate = sf.read(aligned, dtype="float32", always_2d=True)
        start = round(row["start_s"] * sr)
        length = min(len(samples), frames-start)
        dialogue[start:start+length] += samples[:length]
        metrics["segments"].append({"id": row["segment_id"], "budget_s": budget, "raw_duration_s": actual,
                                    "duration_factor": factor, "atempo": tempo,
                                    "padding_s": max(0.0, budget-actual) if timing == "preserve" else 0.0,
                                    "total_s": time.perf_counter()-started})
        (job / "metrics.json").write_text(json.dumps(metrics, indent=2))
    sf.write(job / "outputs/zh_dialogue.wav", dialogue, sr, subtype="PCM_24")
    mix = dialogue.copy()
    if background.is_file():
        converted = job / "audio/background48k.wav"
        call(["ffmpeg", "-v", "error", "-y", "-i", background, "-ar", str(sr), "-ac", "2", converted])
        bg, rate = sf.read(converted, dtype="float32", always_2d=True)
        mix[:min(frames, len(bg))] += bg[:frames]
    premix = job / "outputs/premix.wav"
    sf.write(premix, mix, sr, subtype="FLOAT")
    # A two-pass loudness master; retain the raw mix for review.
    first = call(["ffmpeg", "-hide_banner", "-i", premix, "-af", "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"])
    measured = parse_loudness(first.stderr)
    loudness = ("loudnorm=I=-16:TP=-1.5:LRA=11:linear=true:"
                f"measured_I={measured['input_i']}:measured_TP={measured['input_tp']}:"
                f"measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}:"
                f"offset={measured['target_offset']}")
    final_wav = job / "outputs/zh_mix.wav"
    call(["ffmpeg", "-v", "error", "-y", "-i", premix, "-af", loudness, "-ar", str(sr), "-c:a", "pcm_s24le", final_wav])
    for lang in ["de", "zh"]:
        content = "\n\n".join(f"{i+1}\n{stamp(row['start_s'])} --> {stamp(row['end_s'])}\n{row.get('text_'+lang, '')}" for i, row in enumerate(rows))
        (job / "outputs" / f"{lang}.srt").write_text(content + "\n", encoding="utf-8")
    call(["ffmpeg", "-v", "error", "-y", "-i", source["input"], "-i", final_wav,
          "-i", job / "outputs/zh.srt", "-map", "0:v:0", "-map", "1:a:0", "-map", "2:0",
          "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-c:s", "mov_text",
          "-metadata:s:a:0", "language=zho", "-metadata:s:s:0", "language=zho",
          job / "outputs/dubbed_zh.mp4"])
    call(["ffmpeg", "-v", "error", "-y", "-i", source["input"], "-i", final_wav,
          "-map", "0:v:0", "-map", "1:a:0", "-map", f"0:a:{source['audio_track']}",
          "-c:v", "copy", "-c:a", "aac", "-metadata:s:a:0", "language=zho", "-metadata:s:a:1", "language=deu",
          "-disposition:a:0", "default", "-disposition:a:1", "0", job / "outputs/dual_audio.mkv"])
    metrics["status"] = "rendered_requires_listening_review"
    from export_size import enforce
    metrics["size_limit"] = enforce(job / "outputs/dubbed_zh.mp4")
    metrics["export_probe"] = probe(job / "outputs/dubbed_zh.mp4")
    (job / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2))
    print("RENDERED", job / "outputs", flush=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["prepare", "render"])
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--job", required=True)
    p.add_argument("--input", type=Path)
    p.add_argument("--audio-track", type=int, default=0)
    p.add_argument("--device", choices=["mps", "cpu", "cuda:0"], default="mps")
    p.add_argument("--allow-silent-background", action="store_true")
    args = p.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", args.job):
        p.error("job id must be letters, digits, hyphens or underscores")
    root = args.root.resolve()
    job = confined(root, "jobs/" + args.job)
    if args.action == "prepare":
        if not args.input:
            p.error("prepare requires --input")
        prepare(args, root, job)
    else:
        render(args, root, job)


if __name__ == "__main__":
    main()
