"""Observed CER check; ASR errors do not by themselves prove a synthesis error."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import cn2an


def normalize(text):
    text = cn2an.transform(text, "cn2an")
    return re.sub(r"[^\u4e00-\u9fff0-9a-z]", "", text.lower())


def distance(a, b):
    row = list(range(len(b)+1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(current[-1]+1, row[j]+1, row[j-1]+(x != y)))
        row = current
    return row[-1]


p = argparse.ArgumentParser()
p.add_argument("--root", type=Path, required=True)
p.add_argument("--metrics", type=Path, required=True)
p.add_argument("--output", type=Path, required=True)
args = p.parse_args()
root = args.root.resolve()
config = json.loads((root / "config/local.json").read_text())
observations = args.output.with_suffix(".observations.json")
subprocess.run([str(root / "envs/asr/bin/python"), str(root / "scripts/transcribe_zh.py"),
                "--model", config["asr_model"], "--metrics", str(args.metrics), "--output", str(observations)], check=True)
rows = json.loads(observations.read_text())
errors = 0
characters = 0
for row in rows:
    expected, recognized = normalize(row["expected"]), normalize(row["recognized"])
    count = distance(expected, recognized)
    row.update({"normalized_expected": expected, "normalized_recognized": recognized,
                "edit_distance": count, "reference_characters": len(expected), "cer": count/max(1,len(expected))})
    errors += count
    characters += len(expected)
result = {"normalization": "cn2an numerals; remove punctuation/whitespace; lowercase Latin",
          "method": "independent CPU faster-whisper small Chinese ASR",
          "limitations": "ASR observation only; not a human listening, timbre, emotion or legal review",
          "total_edit_distance": errors, "total_reference_characters": characters,
          "aggregate_cer": errors/max(1,characters), "rows": rows}
args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
print(json.dumps({k:v for k,v in result.items() if k != "rows"},ensure_ascii=False,indent=2))
