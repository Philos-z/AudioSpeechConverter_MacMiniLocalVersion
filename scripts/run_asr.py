import argparse
import json
from pathlib import Path
import subprocess
from video import confined

p = argparse.ArgumentParser()
p.add_argument("--root", type=Path, required=True)
p.add_argument("--job", required=True)
args = p.parse_args()
root = args.root.resolve()
job = confined(root, "jobs/" + args.job)
config = json.loads((root / "config/local.json").read_text())
subprocess.run([str(root / "envs/asr/bin/python"), str(root / "scripts/asr.py"),
                "--input", str(job / "audio/asr16k.wav"), "--model", config["asr_model"],
                "--output", str(job / "segments.json")], check=True)
