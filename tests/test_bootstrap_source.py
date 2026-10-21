"""Source bootstrap safeguards; no installs, model imports, or network access."""
import contextlib
import importlib.util
import io
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "release_bootstrap", ROOT / "scripts/bootstrap_source.py"
)
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


def fixture_root(directory):
    root = Path(directory)
    (root / "config").mkdir()
    (root / "requirements").mkdir()
    for filename in ("asr.lock.txt", "torch-extra.lock.txt", "mlx-extra.lock.txt"):
        shutil.copyfile(ROOT / "requirements" / filename, root / "requirements" / filename)
    return root


def snapshot(root):
    return {
        str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
        for path in root.rglob("*")
    }


class BootstrapSourceTests(unittest.TestCase):
    def test_pins_match_audited_deployment(self):
        self.assertEqual(
            bootstrap.pins(ROOT / "requirements/asr.lock.txt")["faster-whisper"], "1.2.1"
        )
        self.assertEqual(
            bootstrap.pins(ROOT / "requirements/torch-extra.lock.txt")["demucs"], "4.0.1"
        )
        self.assertEqual(
            bootstrap.pins(ROOT / "requirements/mlx-extra.lock.txt")["mlx"], "0.32.4"
        )

    def test_default_check_does_not_modify_files(self):
        with tempfile.TemporaryDirectory(prefix="bootstrap source ") as directory:
            root = fixture_root(directory)
            before = snapshot(root)
            with contextlib.redirect_stdout(io.StringIO()):
                status = bootstrap.check(root)
            self.assertEqual(status, 1)  # Missing deployment inputs are reported.
            self.assertEqual(snapshot(root), before)

    def test_config_copy_is_allowlisted_preserves_existing_and_never_reads_proxy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = fixture_root(directory)
            for name in bootstrap.CONFIG_EXAMPLES:
                (root / "config" / f"{name}.example.json").write_text(
                    '{"safe":true}\n', encoding="utf-8"
                )
            local = root / "config/local.json"
            local.write_text("existing local configuration is not parsed", encoding="utf-8")
            proxy = root / "config/web_proxy.json"
            proxy.write_text("fixture value must never be read", encoding="utf-8")
            (root / "config/web_proxy.example.json").write_text(
                "fixture example must never be read", encoding="utf-8"
            )
            original_read = Path.read_text

            def guarded_read(path, *args, **kwargs):
                if path.name.startswith("web_proxy"):
                    raise AssertionError("Bootstrap attempted to read service credentials")
                return original_read(path, *args, **kwargs)

            with patch.object(Path, "read_text", guarded_read):
                with contextlib.redirect_stdout(io.StringIO()):
                    bootstrap.init_configs(root)
            self.assertEqual(
                local.read_text(encoding="utf-8"), "existing local configuration is not parsed"
            )
            for name in ("background_bilingual", "next_run_profile"):
                self.assertEqual(
                    (root / "config" / f"{name}.json").read_text(encoding="utf-8"),
                    '{"safe":true}\n',
                )
            self.assertEqual(proxy.read_text(encoding="utf-8"), "fixture value must never be read")

    def test_install_rejects_unsupported_platform_before_any_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = fixture_root(directory)
            before = snapshot(root)
            with patch.object(bootstrap.platform, "system", return_value="Linux"):
                with patch.object(bootstrap.subprocess, "run") as run:
                    with patch.object(bootstrap.venv, "EnvBuilder") as builder:
                        with self.assertRaisesRegex(ValueError, "Apple Silicon"):
                            bootstrap.install_dependencies(root)
                        run.assert_not_called()
                        builder.assert_not_called()
            self.assertEqual(snapshot(root), before)

    def test_submodules_cannot_borrow_parent_repository_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = fixture_root(directory)
            for relative in bootstrap.SUBMODULES:
                (root / relative).mkdir(parents=True)
            # Without their own Git metadata, rev-parse would discover this parent.
            result = SimpleNamespace(returncode=0, stdout=str(root) + "\n")
            with patch.object(bootstrap.shutil, "which", return_value="/usr/bin/git"):
                with patch.object(bootstrap, "capture", return_value=result):
                    issues = bootstrap.submodule_issues(root)
            self.assertEqual(len(issues), 2)
            self.assertTrue(all("独立子模块" in issue for issue in issues))


if __name__ == "__main__":
    unittest.main()
