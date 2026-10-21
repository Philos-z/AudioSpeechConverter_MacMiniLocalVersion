import argparse
import json
from pathlib import Path
from video import confined
from translate import translate

p = argparse.ArgumentParser()
p.add_argument("--root", type=Path, required=True)
p.add_argument("--job", required=True)
args = p.parse_args()
root = args.root.resolve()
job = confined(root, "jobs/" + args.job)
config = json.loads((root / "config/local.json").read_text())
file = job / "segments.json"
data = json.loads(file.read_text())
for row in data["segments"]:
    if row.get("translation_reviewed") or row.get("text_zh"):
        continue
    row["text_zh"] = translate(row["text_de"], config["translation_model"])
    row["translation_reviewed"] = False
    row["translation_model"] = config["translation_model"]
    temp = file.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    temp.replace(file)
    print(row["segment_id"], "草稿已保存，等待审核。", flush=True)
