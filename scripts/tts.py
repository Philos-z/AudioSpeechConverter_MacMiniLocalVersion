#!/usr/bin/env python3
"""Single-utterance IndexTTS 2.5 runner with explicit device verification."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import sys
import time


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--reference", type=Path, required=True)
    p.add_argument("--emotion", type=Path)
    p.add_argument("--text", default="大家好，这是一段用于验证本地中文配音的测试音频。")
    p.add_argument("--texts-file", type=Path)
    p.add_argument("--device", choices=["mps", "cpu", "cuda:0"], default="mps")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--emotion-alpha", type=float, default=0.6)
    p.add_argument("--calm-strength", type=float,
                   help="Optional calm emotion-vector weight, 0 to 0.8; mutually exclusive with --emotion")
    p.add_argument("--duration-factor", type=float, default=1.0)
    p.add_argument("--num-beams", type=int, choices=[1, 2, 3], default=1)
    p.add_argument("--mps-memory-fraction", type=float, default=0.7)
    p.add_argument("--max-mel-tokens", type=int, default=1500)
    p.add_argument("--compare-beams", action="store_true",
                   help="Compare beams 1 and 3 on one text, at natural duration, using the same seed")
    p.add_argument("--compare-emotion", action="store_true",
                   help="Compare speaker-reference emotion with a separate scene reference")
    p.add_argument("--compare-prosody", action="store_true",
                   help="Natural-duration comparison: scene alpha .7, scene alpha .25, calm vector .35")
    args = p.parse_args()
    root = args.root.resolve()
    os.environ.setdefault("HF_HOME", str(root / "models/.hf"))
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "0")
    os.environ.setdefault("PYTORCH_MPS_HIGH_WATERMARK_RATIO", str(args.mps_memory_fraction))
    os.environ.setdefault("PYTORCH_MPS_LOW_WATERMARK_RATIO", "0.5")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ.setdefault("NLTK_DATA", str(root / "models/nltk_data"))
    os.environ.setdefault("MPLCONFIGDIR", str(root / "models/.runtime/matplotlib"))
    os.environ.setdefault("MODELSCOPE_CACHE", str(root / "models/.modelscope"))
    os.environ.setdefault("XDG_CACHE_HOME", str(root / "models/.runtime"))
    for file in [args.reference, args.emotion]:
        if file and not file.is_file():
            raise SystemExit(f"REFERENCE_MISSING: {file}")
    if not 0 <= args.emotion_alpha <= 1 or not 0.5 <= args.duration_factor <= 2:
        raise SystemExit("Invalid emotion alpha or duration factor")
    if not 1 <= args.repeat <= 10 or not args.text.strip():
        raise SystemExit("Invalid repeat count or empty text")
    comparing = args.compare_beams or args.compare_emotion or args.compare_prosody
    if sum([args.compare_beams, args.compare_emotion, args.compare_prosody]) > 1:
        p.error("Choose one comparison mode")
    if (args.compare_emotion or args.compare_prosody) and not args.emotion:
        p.error("Emotion/prosody comparison requires --emotion")
    if args.calm_strength is not None and (not 0 <= args.calm_strength <= .8 or args.emotion or comparing):
        p.error("--calm-strength requires a value from 0 to .8, without --emotion or comparison modes")
    if comparing and (args.repeat != 1 or args.duration_factor != 1):
        p.error("Comparison requires --repeat 1 and --duration-factor 1")
    if (args.compare_beams or args.compare_prosody) and args.texts_file:
        p.error("Beam/prosody comparison requires one --text")
    if not 0.5 <= args.mps_memory_fraction <= 0.8 or not 100 <= args.max_mel_tokens <= 1500:
        p.error("Invalid memory fraction or token limit")
    texts = json.loads(args.texts_file.read_text()) if args.texts_file else [args.text] * args.repeat
    if not isinstance(texts, list) or not 1 <= len(texts) <= 10 or not all(isinstance(t, str) and t.strip() for t in texts):
        raise SystemExit("texts-file must contain 1 to 10 nonempty strings")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report_path = args.output.with_suffix(".metrics.json")
    report = {"requested_device": args.device, "texts": texts,
              "num_beams": [1, 3] if args.compare_beams else args.num_beams,
              "reference": str(args.reference), "emotion": str(args.emotion) if args.emotion else None,
              "duration_factor": args.duration_factor, "emotion_alpha": args.emotion_alpha,
              "comparison": "prosody" if args.compare_prosody else "emotion" if args.compare_emotion else "beams" if args.compare_beams else None,
              "calm_strength": args.calm_strength,
              "mps_memory_fraction": args.mps_memory_fraction, "max_mel_tokens": args.max_mel_tokens,
              "status": "starting", "fallback_enabled": os.environ["PYTORCH_ENABLE_MPS_FALLBACK"],
              "output": str(args.output), "runs": [],
              "postprocessing": "none: native waveform, no time stretch, EQ, mixing or normalization",
              "perceptual_quality": "unreviewed"}
    try:
        import torch
        import numpy as np
        import soundfile as sf
        report["torch"] = torch.__version__
        if args.device == "mps" and not torch.backends.mps.is_available():
            raise RuntimeError("DEVICE_UNAVAILABLE: MPS")
        if args.device == "mps":
            torch.mps.set_per_process_memory_fraction(args.mps_memory_fraction)
            report["mps_recommended_max_bytes"] = torch.mps.recommended_max_memory()
        if shutil.disk_usage(root).free < 2 * 2**30:
            raise RuntimeError("INSUFFICIENT_DISK_FOR_PREVIEW")
        if args.device.startswith("cuda") and not torch.cuda.is_available():
            raise RuntimeError("DEVICE_UNAVAILABLE: CUDA")
        torch.manual_seed(42)
        sys.path.insert(0, str(root / "vendor/index-tts"))
        from indextts.infer_v2_5 import IndexTTS2
        model_dir = root / "models/indextts-2.5"
        start = time.perf_counter()
        tts = IndexTTS2(cfg_path=str(model_dir / "config.yaml"), model_dir=str(model_dir),
                        device=args.device, use_bf16=False, use_cuda_kernel=False,
                        use_deepspeed=False, use_accel=False,
                        use_torch_compile=False, use_qwen_emo=False)
        report["load_s"] = time.perf_counter() - start
        report["actual_device"] = tts.device
        report["submodel_devices"] = {name: str(next(getattr(tts, name).parameters()).device)
                                       for name in ["gpt", "semantic_model", "semantic_codec", "s2mel", "campplus_model", "bigvgan"]}
        if str(tts.device) != args.device:
            raise RuntimeError("DEVICE_MISMATCH")
        def sync():
            if args.device == "mps":
                torch.mps.synchronize()
            elif args.device.startswith("cuda"):
                torch.cuda.synchronize()
        if args.device.startswith("cuda"):
            torch.cuda.reset_peak_memory_stats()
        if args.compare_beams:
            variants = [(texts[0], b, args.emotion, f"beams{b}", args.emotion_alpha, None) for b in [1, 3]]
        elif args.compare_emotion:
            variants = [(t, args.num_beams, e, (f"seg{i+1:02}_" if len(texts)>1 else "") + name, args.emotion_alpha, None)
                        for i,t in enumerate(texts)
                        for e,name in [(None,"speaker_emotion"),(args.emotion,"scene_emotion")]]
        elif args.compare_prosody:
            variants = [(texts[0], args.num_beams, args.emotion, "scene_0p7", .7, None),
                        (texts[0], args.num_beams, args.emotion, "scene_0p25", .25, None),
                        (texts[0], args.num_beams, None, "calm_0p35", 1.0, .35)]
        else:
            variants = [(t, args.num_beams, args.emotion, None, args.emotion_alpha, args.calm_strength) for t in texts]
        for index, (utterance, beams, emotion, label, alpha, calm) in enumerate(variants):
            if shutil.disk_usage(root).free < 2 * 2**30:
                raise RuntimeError("INSUFFICIENT_DISK_FOR_PREVIEW")
            if args.device == "mps":
                torch.mps.empty_cache()
            if comparing:
                torch.manual_seed(42)
                np.random.seed(42)
                out = args.output.with_stem(args.output.stem + f"_{label}")
            else:
                out = args.output if index == 0 else args.output.with_stem(args.output.stem + f"_{index+1}")
            temp = out.with_name(out.stem + ".partial.wav")
            sync()
            start = time.perf_counter()
            emotion_vector = [0., 0., 0., 0., 0., 0., 0., calm] if calm is not None else None
            with torch.inference_mode():
                tts.infer(spk_audio_prompt=str(args.reference), text=utterance, lang="ZH",
                          output_path=str(temp),
                          emo_audio_prompt=str(emotion) if emotion else None,
                          emo_alpha=alpha, emo_vector=emotion_vector, duration_factor=args.duration_factor,
                          use_random=False, num_beams=beams, max_mel_tokens=args.max_mel_tokens, verbose=True)
            sync()
            elapsed = time.perf_counter() - start
            data, rate = sf.read(temp)
            if len(data) == 0 or not np.isfinite(data).all() or float(np.max(np.abs(data))) < 0.001:
                raise RuntimeError("INVALID_OR_SILENT_AUDIO")
            duration = len(data) / rate
            temp.replace(out)
            report["runs"].append({"file": str(out), "text": utterance, "num_beams": beams,
                                   "variant": label,
                                   "emotion": str(emotion) if emotion else None,
                                   "effective_emotion_alpha": alpha if emotion or emotion_vector is not None else 1.0,
                                   "emotion_vector": emotion_vector,
                                   "sample_rate": rate,
                                   "duration_s": duration, "inference_s": elapsed,
                                   "rtf": elapsed / duration,
                                   "peak_amplitude": float(np.max(np.abs(data)))})
            report["status"] = "running"
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        if args.device == "mps":
            report["mps_end_allocated_bytes"] = torch.mps.current_allocated_memory()
            report["mps_end_driver_bytes"] = torch.mps.driver_allocated_memory()
        elif args.device.startswith("cuda"):
            report["cuda_peak_allocated_bytes"] = torch.cuda.max_memory_allocated()
        report["status"] = "technical_smoke_pass_quality_unreviewed"
    except KeyboardInterrupt:
        report["status"] = "interrupted"
        raise
    except Exception as exc:
        report["status"] = "failed"
        report["error_type"] = type(exc).__name__
        report["error"] = str(exc)
        raise
    finally:
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
