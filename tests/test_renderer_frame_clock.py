"""Offline renderer-command regressions; no model/codec execution here."""
import ast
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import types
import unittest


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


class LogoFixture:
    width, height = 100, 20

    def convert(self, *args): return self
    def resize(self, *args): return self
    def getchannel(self, *args): return self
    def point(self, *args): return self
    def putalpha(self, *args): pass
    def save(self, path): Path(path).write_bytes(b"offline logo")


class FrameClockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="philos-clock-command-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "render_cache"
        self.work.mkdir()
        self.edl = {"source_input": str(self.root / "source.mp4"), "issues": [],
            "duration_s": 4.0, "frames": 100, "speech_segments": 1,
            "clips": [{"source_start_s": 3.69, "source_duration_s": 4.0,
                       "freeze_s": 0.0, "frames": 100}]}
        self.commands = []

    def renderer(self):
        parsed = ast.parse((ROOT / "scripts/compact_edit.py").read_text())
        function = next(node for node in parsed.body if isinstance(node, ast.FunctionDef)
                        and node.name == "_render_locked")

        def write_json(path, value):
            Path(path).write_text(json.dumps(value))

        def fake_call(command, *args):
            self.commands.append(command)
            Path(command[-1]).write_bytes(b"offline encoded output")

        def load_size_policy(value):
            value.enforce = lambda *args, **kwargs: {"encoder": "offline_no_codec"}

        loader = types.SimpleNamespace(exec_module=load_size_policy)
        namespace = {"Path": Path, "json": json, "hashlib": hashlib, "time": time,
            "FPS": 25, "WIDTH": 960, "HEIGHT": 540, "FFMPEG": "never-executed-ffmpeg",
            "sf": types.SimpleNamespace(info=lambda path: types.SimpleNamespace(frames=192000, samplerate=48000)),
            "Image": types.SimpleNamespace(open=lambda path: LogoFixture(),
                Resampling=types.SimpleNamespace(LANCZOS=0)),
            "logo_style": lambda path: {"width_px": 144},
            "build_portrait_ass": lambda edl, path: Path(path).write_text("offline ASS"),
            "_json_write": write_json, "_call": fake_call,
            "_filter_path": lambda path: "'" + str(Path(path).resolve()) + "'",
            "__file__": str(ROOT / "scripts/compact_edit.py"),
            "importlib": types.SimpleNamespace(util=types.SimpleNamespace(
                spec_from_file_location=lambda *args: types.SimpleNamespace(loader=loader),
                module_from_spec=lambda spec: types.SimpleNamespace()))}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "offline_renderer", "exec"), namespace)
        return namespace["_render_locked"]

    def cached_clip(self):
        row = self.edl["clips"][0]
        signature = hashlib.sha256(json.dumps([self.edl["source_input"], row["source_start_s"],
            row["frames"], row["source_duration_s"], row["freeze_s"], 960, 540], sort_keys=True).encode()).hexdigest()[:12]
        path = self.work / ("clip_0000_" + signature + ".mp4")
        path.write_bytes(b"preserve verified cached clip")
        path.with_suffix(".done").write_text("complete\n")
        return path

    def render(self):
        return self.renderer()(self.edl, self.root / "mix.wav", self.root / "candidate.mp4",
            self.root / "subtitles.ass", self.root / "logo.png", self.work)

    def test_cached_clips_reused_without_reextracting_or_changing_edl(self):
        clip = self.cached_clip()
        original = copy.deepcopy(self.edl)
        self.render()
        self.assertEqual(len(self.commands), 2)  # concat and final burn only
        self.assertFalse(any("-ss" in command for command in self.commands))
        self.assertEqual(clip.read_bytes(), b"preserve verified cached clip")
        self.assertEqual(self.edl, original)

    def test_final_clock_precedes_subtitles_and_explicitly_pins_frame_count(self):
        self.cached_clip()
        self.render()
        command = self.commands[-1]
        filters = command[command.index("-filter_complex") + 1]
        self.assertTrue(filters.startswith("[0:v:0]settb=1/25,setpts=N,ass=filename="))
        self.assertIn("overlay=16:24:shortest=1", filters)
        self.assertEqual(command[command.index("-r") + 1], 25)
        self.assertEqual(command[command.index("-fps_mode") + 1], "cfr")
        self.assertEqual(command[command.index("-frames:v") + 1], 100)
        self.assertEqual(command[command.index("-t") + 1], 4.0)
        self.assertEqual(command[command.index("-c:v") + 1], "h264_videotoolbox")
        self.assertIn("1:a:0", command)
        self.assertEqual(command[command.index("-c:a") + 1], "aac")

    def test_uncached_extraction_recipe_is_unchanged(self):
        self.render()
        self.assertEqual(len(self.commands), 3)
        extraction = self.commands[0]
        self.assertIn("-ss", extraction)
        self.assertEqual(extraction[extraction.index("-frames:v") + 1], 100)
        self.assertEqual(extraction[extraction.index("-vf") + 1],
            "fps=25,scale=960:540:flags=lanczos,setsar=1,tpad=stop_mode=clone:stop_duration=0.080000")




if __name__ == "__main__":
    unittest.main()
