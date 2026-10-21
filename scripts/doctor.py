#!/usr/bin/env python3
"""Read-only system checks. No serial numbers, credentials, or media uploads."""
import argparse
import json
import platform
from pathlib import Path
import shutil
import subprocess
import sys
import time


def capture(args):
    result = subprocess.run(args, text=True, capture_output=True, timeout=20)
    return {"returncode": result.returncode, "output": result.stdout.strip(),
            "error": result.stderr.strip()}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, required=True)
    args = p.parse_args()
    root = args.root.resolve()
    report = {"time_unix": time.time(), "os": platform.platform(), "arch": platform.machine(),
              "python": sys.version, "free_gib": round(shutil.disk_usage(root).free / 2**30, 3),
              "tools": {}, "model_status": "not_checked"}
    for name in ["uv", "ffmpeg", "ffprobe"]:
        executable = shutil.which(name)
        report["tools"][name] = capture([executable, "--version" if name == "uv" else "-version"]) if executable else None
    if platform.system() == "Darwin":
        report["memory_bytes"] = capture(["sysctl", "-n", "hw.memsize"])
        report["chip"] = capture(["sysctl", "-n", "machdep.cpu.brand_string"])
    try:
        import torch
        report["torch"] = torch.__version__
        report["mps_built"] = torch.backends.mps.is_built()
        report["mps_available"] = torch.backends.mps.is_available()
        report["cuda_available"] = torch.cuda.is_available()
        device = "cuda:0" if torch.cuda.is_available() else ("mps" if report["mps_available"] else "cpu")
        x = torch.randn(256, 256, device=device)
        y = x @ x
        if device == "mps":
            torch.mps.synchronize()
        elif device.startswith("cuda"):
            torch.cuda.synchronize()
        report["tensor_probe"] = {"device": str(y.device), "finite": bool(torch.isfinite(y).all().cpu())}
    except ImportError:
        report["torch"] = "not_installed_in_current_interpreter"
    output = root / "reports/environment.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
