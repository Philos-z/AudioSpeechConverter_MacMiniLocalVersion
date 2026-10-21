#!/usr/bin/env python3
"""Download pinned inference-only files; never silently fetch another model."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--reserve-gib", type=float, default=8.0)
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    os.environ.setdefault("HF_HOME", str(root / "models/.hf"))
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    from huggingface_hub import HfApi, hf_hub_download
    api = HfApi()
    model = root / "models/indextts-2.5"
    specs = [
        ("IndexTeam/IndexTTS-2.5", "model", model,
         lambda name: not name.startswith("qwen0.6bemo4-merge/")),
        ("facebook/w2v-bert-2.0", "model", model / "hf_cache/w2v-bert-2.0",
         lambda name: name.endswith((".json", ".md", ".txt")) or name == "model.safetensors"),
        ("funasr/campplus", "model", model / "hf_cache",
         lambda name: name == "campplus_cn_common.bin"),
        ("nvidia/bigvgan_v2_22khz_80band_256x", "model", model / "hf_cache/bigvgan",
         lambda name: name in ("config.json", "bigvgan_generator.pt", "LICENSE", "README.md")),
        ("IndexTeam/IndexTTS-2-Demo", "space", root / "references/official",
         lambda name: name in ("examples/voice_01.wav", "examples/emo_sad.wav")),
    ]
    report_dir = root / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    lock_path = report_dir / "models.lock.json"
    old = json.loads(lock_path.read_text()) if lock_path.exists() else {"models": []}
    locked = {item["repo"]: item for item in old["models"]}
    plan = {"schema_version": 1, "created_unix": time.time(), "models": []}
    remaining = 0
    for repo, kind, dest, select in specs:
        prior = locked.get(repo)
        revision = prior["revision"] if prior else None
        info = api.repo_info(repo_id=repo, repo_type=kind, revision=revision, files_metadata=True)
        files = []
        for sibling in info.siblings:
            if not select(sibling.rfilename):
                continue
            size = sibling.size
            if size is None:
                raise RuntimeError(f"Missing remote size: {repo}/{sibling.rfilename}")
            local_path = dest / sibling.rfilename
            complete = local_path.is_file() and local_path.stat().st_size == size
            if not complete:
                remaining += size
            files.append({"path": sibling.rfilename, "size": size})
        if not files:
            raise RuntimeError(f"No matching inference files for {repo}")
        plan["models"].append({"repo": repo, "repo_type": kind, "revision": info.sha,
                               "directory": str(dest.relative_to(root)), "files": files})
    free = shutil.disk_usage(root).free
    plan["remaining_download_gib"] = round(remaining / 2**30, 3)
    plan["free_gib"] = round(free / 2**30, 3)
    plan["reserve_gib"] = args.reserve_gib
    lock_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    print(json.dumps({key: value for key, value in plan.items() if key != "models"}), flush=True)
    if args.plan_only:
        return
    if free < remaining + args.reserve_gib * 2**30:
        raise SystemExit("INSUFFICIENT_DISK: requested download would violate the reserved space")
    for item in plan["models"]:
        dest = root / item["directory"]
        for entry in item["files"]:
            if shutil.disk_usage(root).free < args.reserve_gib * 2**30:
                raise SystemExit("INSUFFICIENT_DISK during download")
            print(f"FETCH {item['repo']}/{entry['path']}", flush=True)
            result = Path(hf_hub_download(repo_id=item["repo"], repo_type=item["repo_type"],
                                        revision=item["revision"], filename=entry["path"],
                                        local_dir=str(dest)))
            if result.stat().st_size != entry["size"]:
                raise RuntimeError(f"Size mismatch: {result.name}")
            digest = hashlib.sha256()
            with result.open("rb") as file:
                for chunk in iter(lambda: file.read(8 * 1024 * 1024), b""):
                    digest.update(chunk)
            entry["sha256"] = digest.hexdigest()
            lock_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    plan["download_complete"] = True
    lock_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    print("DOWNLOAD_COMPLETE", flush=True)


if __name__ == "__main__":
    main()
