"""Offline local-model mapping and strict MLX reference preprocessing.

Import/inventory/cache setup use the standard library, not MLX/Torch. Vendor
files and source weights are never modified. Call configure_environment before
importing huggingface_hub or mlx_indextts in a fresh inference/conversion process.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys


BIGVGAN_REPO = "nvidia/bigvgan_v2_22khz_80band_256x"
BIGVGAN_REVISION = "633ff708ed5b74903e86ff1298cf4a98e921c513"
BIGVGAN_SHA256 = "e95ba25972d3de0628d99cd156e9315a9c018899bf739988959ebe3544080ced"
CAMPPLUS_REPO = "funasr/campplus"
CAMPPLUS_REVISION = "e4b6ede7ce16997aff4ae69fbca1f0175e2afede"
CAMPPLUS_SHA256 = "3388cf5fd3493c9ac9c69851d8e7a8badcfb4f3dc631020c4961371646d5ada8"
SOURCE_FILES = ("config.yaml", "gpt.pth", "codec.pth", "s2mel.pth", "feat1.pt", "feat2.pt",
                "wav2vec2bert_stats.pt", "multilingual_zh_ja_yue_char_del.tiktoken")


def digest(path):
    sha = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024*1024), b""):
            sha.update(block)
    return sha.hexdigest()


def local_paths(root):
    root = Path(root).resolve()
    model = root/"models/indextts-2.5"
    return {"root": root, "source_model": model,
            "w2v_dir": model/"hf_cache/w2v-bert-2.0",
            "campplus": model/"hf_cache/campplus_cn_common.bin",
            "bigvgan_weights": model/"hf_cache/bigvgan/bigvgan_generator.pt",
            "bigvgan_config": model/"hf_cache/bigvgan/config.json"}


def inventory(root, verify_large=False):
    paths = local_paths(root)
    model = paths["source_model"]
    manifest = json.loads((paths["root"]/"reports/models.lock.json").read_text())
    expected = {}
    for package in manifest["models"]:
        for entry in package["files"]:
            path = (paths["root"]/package["directory"]/entry["path"]).resolve()
            expected[str(path)] = entry
    files = []
    targets = [model/name for name in SOURCE_FILES]
    targets += [paths["w2v_dir"]/name for name in ("model.safetensors", "config.json", "preprocessor_config.json")]
    targets += [paths[name] for name in ("campplus", "bigvgan_weights", "bigvgan_config")]
    for target in targets:
        entry = expected.get(str(target), {})
        exists = target.is_file()
        size = target.stat().st_size if exists else None
        should_hash = exists and (verify_large or size < 1024*1024 or target == paths["campplus"])
        sha = digest(target) if should_hash else None
        files.append({"relative_path": str(target.relative_to(model)), "exists": exists,
            "bytes": size, "expected_bytes": entry.get("size"),
            "size_matches_model_lock": size == entry.get("size") if entry else None,
            "sha256": sha, "expected_sha256": entry.get("sha256"),
            "sha256_checked": should_hash,
            "sha256_matches_model_lock": sha == entry.get("sha256") if should_hash and entry else None})
    text = (model/"config.yaml").read_text() if (model/"config.yaml").is_file() else ""
    fields = {}
    for name in ("gpt_checkpoint", "s2mel_checkpoint", "w2v_stat", "version", "number_text_tokens"):
        match = re.search(r"^\s*"+re.escape(name)+r":\s*([^#\n]+)", text, re.MULTILINE)
        fields[name] = match.group(1).strip().strip("\"'") if match else None
    required = all(item["exists"] and item["size_matches_model_lock"] is not False
                   and item["sha256_matches_model_lock"] is not False for item in files)
    return {"source_model": str(model), "source_files": files, "config_fields": fields,
        "relative_path_compatibility": {"gpt_checkpoint": fields["gpt_checkpoint"] == "gpt.pth",
            "s2mel_checkpoint": fields["s2mel_checkpoint"] == "s2mel.pth",
            "w2v_stat": fields["w2v_stat"] == "wav2vec2bert_stats.pt",
            "v25_vocabulary": fields["number_text_tokens"] == "60509"},
        "complete_local_inputs": required, "large_weights_rehashed": verify_large,
        "torch_imported": "torch" in sys.modules, "models_loaded": False,
        "source_weights_modified": False, "qwen_emotion_model_needed_for_reference_or_vector": False}


def _checked_symlink(destination, target):
    destination, target = Path(destination), Path(target).resolve()
    if not target.is_file():
        raise FileNotFoundError(str(target))
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        if destination.resolve() != target:
            raise ValueError("Existing cache symlink points elsewhere: " + str(destination))
    elif destination.exists():
        raise FileExistsError("Refusing to replace cache file: " + str(destination))
    else:
        destination.symlink_to(target)
    return {"link": str(destination), "target": str(target), "kind": "symlink", "weight_bytes_copied": 0}


def _seed_repo(hub, repo, revision, files):
    base = Path(hub)/("models--"+repo.replace("/", "--"))
    records = [_checked_symlink(base/"snapshots"/revision/name, target) for name, target in files.items()]
    ref = base/"refs/main"
    ref.parent.mkdir(parents=True, exist_ok=True)
    if ref.exists() and ref.read_text().strip() != revision:
        raise ValueError("Refusing to replace a conflicting isolated refs/main: " + str(ref))
    if not ref.exists():
        ref.write_text(revision+"\n")
    return {"repo": repo, "revision": revision, "refs_main": str(ref), "files": records}


def configure_environment(root, cache_home=None):
    paths = local_paths(root)
    home = Path(cache_home).resolve() if cache_home else paths["root"]/"models/.mlx-isolated-hf"
    if home == paths["source_model"] or home.is_relative_to(paths["source_model"]):
        raise ValueError("Isolated cache must not be inside the source model")
    values = {"HF_HOME": str(home), "HF_HUB_CACHE": str(home/"hub"),
        "HUGGINGFACE_HUB_CACHE": str(home/"hub"), "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1", "INDEXTTS_W2V_BERT_DIR": str(paths["w2v_dir"]),
        "TOKENIZERS_PARALLELISM": "false"}
    os.environ.update(values)
    return {"environment": values,
            "hf_constants_were_already_imported": "huggingface_hub.constants" in sys.modules,
            "call_before_hf_import_for_conversion": True}


def seed_offline_cache(root, cache_home=None):
    paths = local_paths(root)
    # Check known fixed bytes before exposing them through a hub cache alias.
    if digest(paths["campplus"]) != CAMPPLUS_SHA256:
        raise ValueError("Local CAMPPlus SHA256 mismatch")
    if digest(paths["bigvgan_weights"]) != BIGVGAN_SHA256:
        raise ValueError("Local BigVGAN SHA256 mismatch")
    environment = configure_environment(root, cache_home)
    hub = Path(environment["environment"]["HF_HUB_CACHE"])
    packages = [_seed_repo(hub, BIGVGAN_REPO, BIGVGAN_REVISION,
                          {"bigvgan_generator.pt": paths["bigvgan_weights"], "config.json": paths["bigvgan_config"]}),
                _seed_repo(hub, CAMPPLUS_REPO, CAMPPLUS_REVISION,
                          {"campplus_cn_common.bin": paths["campplus"]})]
    return {**environment, "packages": packages, "weight_bytes_copied": 0,
            "network_used": False, "source_weights_modified": False, "models_loaded": False}


def _strict_init_pytorch_modules(instance, root, cache_home=None):
    paths = local_paths(root)
    configure_environment(root, cache_home)
    campplus_sha = digest(paths["campplus"])
    if campplus_sha != CAMPPLUS_SHA256:
        raise ValueError("Local CAMPPlus SHA256 mismatch; reference preprocessing aborted")
    for name in ("model.safetensors", "config.json", "preprocessor_config.json"):
        if not (paths["w2v_dir"]/name).is_file():
            raise FileNotFoundError("Missing local W2V-BERT file: " + name)
    relative_stat = Path(str(instance.cfg.w2v_stat))
    if relative_stat.is_absolute() or ".." in relative_stat.parts:
        raise ValueError("Expected local relative W2V stats path")
    stats = Path(instance.mlx_model_dir)/relative_stat
    if not stats.is_file():
        stats = Path(instance.model_dir)/relative_stat
    if not stats.is_file():
        raise FileNotFoundError("Missing converted-model W2V statistics")
    stats_sha = digest(stats)
    if stats_sha != digest(paths["source_model"]/"wav2vec2bert_stats.pt"):
        raise ValueError("Converted W2V statistics differ from the local source")

    # Lazy imports: all file guards above run before any Torch/model import.
    import torch
    from transformers import AutoFeatureExtractor
    from mlx_indextts.indextts.utils.maskgct_utils import build_semantic_model, get_w2v_bert_source
    from mlx_indextts.indextts.s2mel.modules.campplus.DTDNN import CAMPPlus as CAMPPlusModel
    if Path(get_w2v_bert_source()).resolve() != paths["w2v_dir"]:
        raise ValueError("W2V loader is not using the explicitly selected local directory")
    print("Loading verified local W2V-BERT and CAMPPlus; network and random fallback disabled")
    semantic_model, semantic_mean, semantic_std = build_semantic_model(path_=str(stats))
    semantic_model = semantic_model.to(instance.device).eval()
    semantic_mean, semantic_std = semantic_mean.to(instance.device), semantic_std.to(instance.device)
    extractor = AutoFeatureExtractor.from_pretrained(str(paths["w2v_dir"]), local_files_only=True)
    campplus = CAMPPlusModel(feat_dim=80, embedding_size=192)
    state = torch.load(str(paths["campplus"]), map_location="cpu", weights_only=True)
    campplus.load_state_dict(state, strict=True)
    campplus = campplus.to(instance.device).eval()
    # Publish a valid pair together; any exception above propagates to the caller.
    instance.semantic_model, instance.semantic_mean, instance.semantic_std = semantic_model, semantic_mean, semantic_std
    instance.extract_features, instance.campplus = extractor, campplus
    instance._offline_preprocessing_audit = {"campplus_path": str(paths["campplus"]),
        "campplus_sha256": campplus_sha, "campplus_strict_load": True,
        "w2v_directory": str(paths["w2v_dir"]), "statistics_sha256": stats_sha,
        "device": str(instance.device), "network_used": False, "random_fallback": False}


def install_strict_local_preprocessing(index_tts_v25_class, root, cache_home=None):
    """Patch only the already-imported v25 class in this process, not vendor files."""
    root = Path(root).resolve()
    prior = getattr(index_tts_v25_class, "_strict_local_preprocess_root", None)
    if prior is not None:
        if prior != str(root):
            raise ValueError("Class already bound to another local model root")
        return {"installed": True, "already_installed": True, "root": str(root)}
    def strict(self):
        return _strict_init_pytorch_modules(self, root, cache_home)
    index_tts_v25_class._init_pytorch_modules = strict
    index_tts_v25_class._strict_local_preprocess_root = str(root)
    return {"installed": True, "root": str(root), "random_fallback": False,
            "vendor_files_modified": False, "models_loaded_by_install": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--cache-home", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--verify-large-source", action="store_true")
    args = parser.parse_args()
    report = inventory(args.root, args.verify_large_source)
    if not report["complete_local_inputs"]:
        raise ValueError("Local source inventory is incomplete or differs from model lock")
    if args.prepare:
        report["offline_cache_setup"] = seed_offline_cache(args.root, args.cache_home)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n")
    print(json.dumps({"complete_local_inputs": report["complete_local_inputs"],
        "prepared": args.prepare, "torch_imported": "torch" in sys.modules,
        "models_loaded": False, "report": str(args.report.resolve())}))


if __name__ == "__main__":
    main()
