#!/usr/bin/env python3
"""Check this source release or explicitly prepare its local Python environments.

The default is read-only. This utility never downloads models, reads service
credentials, launches inference, or replaces an existing configuration. Dependency
installation requires --install-deps; copying non-secret examples requires
--init-config. Run with Python 3.11 on an Apple Silicon Mac for installation.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import venv


SUBMODULES = {
    "vendor/index-tts": "d9e41aac89fd00b3d71497fddb287b7f24613712",
    "vendor/mlx-indextts2-inference": "a6d8fef49b3c36fa8bccaf5a780091e9b864f8e7",
}
ENVIRONMENTS = (
    ("ASR", "envs/asr", "requirements/asr.lock.txt", None, None),
    ("Torch", "vendor/index-tts/.venv", "requirements/torch-extra.lock.txt",
     "vendor/index-tts", ("indextts", "2.0.0")),
    ("MLX", "vendor/mlx-indextts2-inference/.venv", "requirements/mlx-extra.lock.txt",
     "vendor/mlx-indextts2-inference", ("mlx-indextts", "0.3.0")),
)
# Deliberately excludes every service/API configuration, including its examples.
CONFIG_EXAMPLES = ("local", "background_bilingual", "next_run_profile")
PIN = re.compile(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9.+_-]+)")


def pins(path):
    result = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = PIN.fullmatch(line)
        if not match:
            raise ValueError(f"Unsupported requirement at {path.name}:{number}")
        result[match[1]] = match[2]
    return result


def capture(argv):
    return subprocess.run([str(x) for x in argv], text=True, capture_output=True,
                          timeout=30, check=False)


def submodule_issues(root):
    issues = []
    if not shutil.which("git"):
        return ["缺少 Git；请先安装 Git。"]
    for relative, expected in SUBMODULES.items():
        folder = root / relative
        if not folder.is_dir():
            issues.append(f"子模块尚未初始化：{relative}")
            continue
        top = capture(["git", "-C", folder, "rev-parse", "--show-toplevel"])
        if top.returncode or Path(top.stdout.strip()).resolve() != folder.resolve():
            issues.append(f"缺少独立子模块 Git 元数据：{relative}")
            continue
        revision = capture(["git", "-C", folder, "rev-parse", "HEAD"])
        if revision.returncode or revision.stdout.strip() != expected:
            issues.append(f"子模块版本与锁定提交不同：{relative}（需要 {expected}）")
            continue
        changes = capture(["git", "-C", folder, "status", "--porcelain", "--untracked-files=no"])
        if changes.returncode or changes.stdout.strip():
            issues.append(f"子模块含已跟踪源码改动，先自行保存并核对：{relative}")
    return issues


def environment_status(root, entry):
    name, relative, requirements, editable, project = entry
    executable = root / relative / "bin/python"
    expected = pins(root / requirements)
    if project:
        expected[project[0]] = project[1]
    if not executable.is_file():
        return [f"{name} 环境缺失：{relative}"], False
    # Only standard-library package metadata is inspected; no model module import.
    code = """import importlib.metadata as m,json,sys
names=json.loads(sys.argv[1]); result={}
for name in names:
    try: result[name]=m.version(name)
    except m.PackageNotFoundError: result[name]=None
print(json.dumps({'python':list(sys.version_info[:2]),'versions':result}))
"""
    result = capture([executable, "-c", code, json.dumps(list(expected))])
    if result.returncode:
        return [f"{name} 环境无法读取包信息：{relative}"], False
    try:
        observed = json.loads(result.stdout)
        differences = [f"{package}={observed['versions'].get(package) or '缺失'}（需要 {version}）"
                       for package, version in expected.items()
                       if observed["versions"].get(package) != version]
    except (ValueError, TypeError, KeyError):
        return [f"{name} 环境返回无效包信息：{relative}"], False
    if observed.get("python") != [3, 11]:
        return [f"{name} 环境需使用 Python 3.11；不会覆盖已有环境。"], False
    if differences:
        return [f"{name} 环境需补齐锁定版本：" + "; ".join(differences)], False
    if editable:
        # Confirm the installed upstream project points to this submodule, without
        # importing it or printing package metadata such as install URLs.
        code = """import importlib.metadata as m,json,pathlib,sys,urllib.parse
try:
    value=json.loads(m.distribution(sys.argv[1]).read_text('direct_url.json') or '{}')
    url=urllib.parse.urlsplit(value.get('url',''))
    ok=(url.scheme=='file' and value.get('dir_info',{}).get('editable') is True
        and pathlib.Path(urllib.parse.unquote(url.path)).resolve()==pathlib.Path(sys.argv[2]).resolve())
except (ValueError,TypeError,KeyError,m.PackageNotFoundError): ok=False
sys.exit(0 if ok else 1)
"""
        result = capture([executable, "-c", code, project[0], root / editable])
        if result.returncode:
            return [f"{name} 的 editable 包未绑定本项目子模块。"], False
    return [], True


def print_manual_commands(root):
    prefix = ["python3.11", str(root / "scripts/bootstrap_source.py")]
    print("\n需要时手动执行：")
    print("  " + shlex.join(["git", "-C", str(root), "submodule", "update", "--init", "--recursive"]))
    print("  " + shlex.join(prefix + ["--init-config"]))
    print("  " + shlex.join(prefix + ["--install-deps"]))
    print("模型与外部文本服务需另行准备；本脚本不会下载模型或检查、复制服务凭证。")


def init_configs(root):
    for name in CONFIG_EXAMPLES:
        source = root / "config" / f"{name}.example.json"
        target = root / "config" / f"{name}.json"
        if target.exists() or target.is_symlink():
            print(f"保留已有配置：config/{target.name}")
            continue
        if not source.is_file():
            print(f"未提供可选示例：config/{source.name}")
            continue
        # Validate only the explicitly allowlisted non-secret example.
        json.loads(source.read_text(encoding="utf-8"))
        with target.open("x", encoding="utf-8") as stream:
            stream.write(source.read_text(encoding="utf-8"))
        print(f"已复制非凭证示例：config/{target.name}")


def install_dependencies(root):
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ValueError("当前锁定的 MLX 制作流程要求 Apple Silicon macOS；未创建或修改环境。")
    if sys.version_info[:2] != (3, 11):
        raise ValueError("请使用 python3.11 执行 --install-deps；未创建或修改环境。")
    issues = submodule_issues(root)
    if issues:
        raise ValueError("\n".join(issues))
    # Resolve every requirement file before making any changes.
    for entry in ENVIRONMENTS:
        pins(root / entry[2])
    constraints = root / "config/native_mlx_runtime.constraints.txt"
    if not constraints.is_file():
        raise ValueError("缺少 MLX 运行时约束文件；未创建或修改环境。")
    # Reject malformed existing paths before changing any environment.
    for entry in ENVIRONMENTS:
        folder = root / entry[1]
        if folder.exists() and (folder.is_symlink() or not (folder / "pyvenv.cfg").is_file()):
            raise ValueError(f"目标目录不是独立 venv，拒绝覆盖：{entry[1]}")
        if (folder / "bin/python").is_file():
            result = capture([folder / "bin/python", "-c", "import sys;sys.exit(0 if sys.version_info[:2]==(3,11) else 1)"])
            if result.returncode:
                raise ValueError(f"已有环境不是 Python 3.11，拒绝覆盖：{entry[1]}")
    for entry in ENVIRONMENTS:
        name, relative, requirement, editable, _ = entry
        _, ready = environment_status(root, entry)
        if ready:
            print(f"{name} 已满足锁定版本，跳过安装。")
            continue
        folder = root / relative
        if not folder.exists():
            print(f"创建 {name} 独立 Python 3.11 环境。")
            venv.EnvBuilder(with_pip=True).create(folder)
        executable = folder / "bin/python"
        command = [str(executable), "-m", "pip", "install", "-r", str(root / requirement)]
        if editable:
            package = str(root / editable) + ("[v25]" if name == "MLX" else "")
            command += ["-e", package]
        if name == "MLX":
            command += ["-c", str(constraints)]
        # No force-reinstall, environment removal, pip uninstall or model command.
        print(f"安装 {name} 的锁定运行依赖（保留已有环境）。")
        subprocess.run(command, cwd=root, check=True)
        subprocess.run([str(executable), "-m", "pip", "check"], cwd=root, check=True)
        problems, ready = environment_status(root, entry)
        if not ready:
            raise ValueError("\n".join(problems))


def check(root):
    issues = submodule_issues(root)
    for entry in ENVIRONMENTS:
        problems, ready = environment_status(root, entry)
        issues.extend(problems)
        if ready:
            print(f"{entry[0]} 环境版本检查通过。")
    for relative in ("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg",
                     "/opt/homebrew/opt/ffmpeg-full/bin/ffprobe"):
        if not Path(relative).is_file():
            issues.append("缺少锁定流程使用的本机工具：" + relative)
    if not (root / "config/local.json").is_file():
        issues.append("本机配置尚未建立：config/local.json（使用 --init-config 后填写本机模型路径）")
    if issues:
        print("源码部署检查尚未完成：")
        for issue in issues:
            print("- " + issue)
    else:
        print("源码、子模块和运行依赖检查通过。模型、服务与实际短片仍需单独验证。")
    print_manual_commands(root)
    return 1 if issues else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--check", action="store_true", help="仅检查；也是无参数时的默认行为")
    parser.add_argument("--init-config", action="store_true", help="仅复制缺失的非凭证配置示例；不覆盖已有配置")
    parser.add_argument("--install-deps", action="store_true", help="显式创建三套独立环境并安装依赖；不下载模型")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.check and (args.init_config or args.install_deps):
        parser.error("--check 是只读操作，不能与写入操作同时使用")
    try:
        if args.init_config:
            init_configs(root)
        if args.install_deps:
            install_dependencies(root)
        if args.check or not (args.init_config or args.install_deps):
            return check(root)
        print("准备步骤完成。请再执行 --check；模型和服务未由本脚本配置。")
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        # Never include pip/environment variables or service configuration values.
        print("准备未完成：" + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
