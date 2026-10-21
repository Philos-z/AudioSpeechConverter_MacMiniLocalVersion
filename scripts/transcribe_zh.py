"""Run in the ASR environment; serialize observations for an independent QA pass."""
import argparse
import json
from pathlib import Path
from faster_whisper import WhisperModel

p = argparse.ArgumentParser()
p.add_argument("--model", required=True)
p.add_argument("--metrics", type=Path, required=True)
p.add_argument("--output", type=Path, required=True)
args = p.parse_args()
data = json.loads(args.metrics.read_text())
model = WhisperModel(args.model, device="cpu", compute_type="int8", local_files_only=True)
rows = []
for item in data["runs"]:
    segments, _ = model.transcribe(item["file"], language="zh", task="transcribe", beam_size=5)
    text = "".join(segment.text for segment in segments).strip()
    rows.append({"file": item["file"], "expected": item["text"], "recognized": text})
args.output.write_text(json.dumps(rows, ensure_ascii=False, indent=2))
print("CHINESE_ASR_QA_OBSERVATIONS_SAVED", flush=True)
