#!/usr/bin/env python3
"""CPU German ASR using an explicitly selected, already cached Whisper model."""
import argparse
import json
from pathlib import Path
import time


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--model", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    from faster_whisper import WhisperModel
    start = time.perf_counter()
    model = WhisperModel(str(args.model.resolve()), device="cpu", compute_type="int8", local_files_only=True)
    segments, info = model.transcribe(str(args.input), language="de", task="transcribe",
                                      vad_filter=True, word_timestamps=True, beam_size=5)
    rows = []
    for i, seg in enumerate(segments):
        if not seg.text.strip():
            continue
        rows.append({"segment_id": f"seg{i+1:04d}", "speaker_id": "S00",
                     "start_s": seg.start, "end_s": seg.end,
                     "text_de": seg.text.strip(), "text_zh": "",
                     "source_language": "de", "target_language": "zh", "overlap": False,
                     "translation_reviewed": False, "speaker_reviewed": False,
                     "words": [{"start_s": w.start, "end_s": w.end, "word": w.word} for w in (seg.words or [])]})
    args.output.write_text(json.dumps({"schema_version": 1, "segments": rows,
                                      "asr_model": str(args.model), "language": info.language,
                                      "elapsed_s": time.perf_counter()-start,
                                      "speaker_method": "unverified_single_speaker_placeholder",
                                      "alignment": "faster_whisper_word_timestamps_not_whisperx_forced_alignment"},
                                     ensure_ascii=False, indent=2))
    print(f"ASR_COMPLETE: {len(rows)} segments; translate and review before synthesis")


if __name__ == "__main__":
    main()
