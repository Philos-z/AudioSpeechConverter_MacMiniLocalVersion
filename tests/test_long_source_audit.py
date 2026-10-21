"""Independent orchestration audit: no models, real API, or source media run."""
import copy
import fcntl
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock


PROJECT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT / "web"))
spec = importlib.util.spec_from_file_location("long_source_pipeline_audit", PROJECT / "web/pipeline.py")
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)


class LongSourceAuditTests(unittest.TestCase):
    def fixture(self, root, *, cancel=None):
        disk = mock.patch.object(pipeline.shutil, 'disk_usage',
                                 return_value=SimpleNamespace(free=100 * 1024**3))
        disk.start()
        self.addCleanup(disk.stop)
        source = root / "source.mp4"
        source.write_bytes(b"source fixture; never decoded")
        (root / "scripts").mkdir()
        (root / "scripts/asr.py").write_text("# CPU ASR mocked by this test\n")
        job = {"id": "web_long_audit", "title": "audit", "input_path": str(source),
               "options": {"source_language": "auto", "split_long_source": True,
                           "episode_max_seconds": 2}}
        events = []
        flow = pipeline.Flow(root, job, lambda *args: events.append(args),
                             mock.Mock(side_effect=AssertionError("unexpected subprocess")),
                             cancel.is_set if cancel is not None else lambda: False)
        for name in ("audio", "outputs"):
            (flow.folder / name).mkdir(parents=True, exist_ok=True)
        pipeline.save(flow.folder / "source.json", {"input": str(source), "duration_s": 6, "probe": {}})
        source_asr = {"language": "fr", "segments": [
            {"segment_id": f"s{i:02d}", "start_s": round(i * .4 + .04, 9),
             "end_s": round(i * .4 + .3, 9), "text_de": f"texte {i}."}
            for i in range(15)]}
        calls = {"asr": 0, "clip": 0, "children": []}

        def command(stage, start, end, argv, **kwargs):
            argv = [str(x) for x in argv]
            if stage == "准备长视频识别音轨":
                Path(argv[-1]).write_bytes(b"full source audio")
            elif stage == "识别完整原文与分集边界":
                calls["asr"] += 1
                pipeline.save(Path(argv[argv.index("--output") + 1]), source_asr)
            elif "--episode" in argv:
                calls["clip"] += 1
                target = Path(argv[argv.index("--output") + 1])
                target.write_bytes((target.name + " clip").encode())
                pipeline.save(Path(argv[argv.index("--receipt") + 1]), {"full_decode_passed": True})
            else:
                raise AssertionError(f"unexpected operation: {stage}")
            return {"returncode": 0, "stdout": ""}

        flow.command = command
        return flow, source, calls, source_asr, events

    @staticmethod
    def child_result(root, child, duration=None, size=6):
        asr = pipeline.read(child["_episode_asr"]["path"])
        output = root / "jobs" / child["id"] / "outputs/video.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("wb") as handle:
            handle.truncate(size)
        return {"duration_s": asr["duration_s"] if duration is None else duration,
                "artifacts": [{"label": "video", "kind": "video", "path": str(output),
                               "sha256": pipeline.digest(output)}], "metrics": {}}

    def test_replan_keeps_one_full_source_asr_and_accurately_reports_it(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            flow, source, calls, asr, events = self.fixture(root)
            first = True

            def child(*args, deadline):
                nonlocal first
                self.assertEqual(deadline, flow.deadline)
                child_job = args[1]
                calls["children"].append(copy.deepcopy(child_job))
                result = self.child_result(root, child_job, duration=3 if first else None)
                first = False
                return result

            with mock.patch.object(pipeline, "_run_with_recovery", side_effect=child):
                result = flow.process_episodes({"asr_model": "cached", "minimum_free_gib": 0}, source,
                    pipeline.digest(source), {}, 6, 0, "auto", 2)
            self.assertEqual(calls["asr"], 1)
            self.assertEqual(result["metrics"]["full_source_asr_runs"], calls["asr"])
            self.assertEqual(result["metrics"]["duration_replan_attempts"], 1)
            self.assertTrue(all(ep["duration_s"] <= 2 for ep in result["episodes"]))
            self.assertEqual(result["episodes"][0]["source_start_s"], 0)
            self.assertEqual(result["episodes"][-1]["source_end_s"], 6)
            for previous, following in zip(result["episodes"], result["episodes"][1:]):
                self.assertEqual(previous["source_end_s"], following["source_start_s"])
            self.assertTrue(all(c["options"]["source_language"] == "fr" for c in calls["children"]))

    def test_children_keep_parent_lock_and_never_reenter_run_job(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            flow, source, calls, asr, events = self.fixture(root)
            lockfile = root / "cache/.web_pipeline.lock"
            lockfile.parent.mkdir()

            def child(*args, deadline):
                self.assertEqual(deadline, flow.deadline)
                with lockfile.open("a+") as contender:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self.child_result(root, args[1])

            with lockfile.open("a+") as held:
                fcntl.flock(held.fileno(), fcntl.LOCK_EX)
                with mock.patch.object(pipeline, "_run_with_recovery", side_effect=child), \
                     mock.patch.object(pipeline, "run_job", side_effect=AssertionError("recursive flock")):
                    result = flow.process_episodes({"asr_model": "cached", "minimum_free_gib": 0}, source,
                        pipeline.digest(source), {}, 6, 0, "auto", 2)
            self.assertEqual(result["episode_count"], 3)

    def test_strict_50_mb_gate_rejects_exactly_50_million_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            flow, source, calls, asr, events = self.fixture(root)
            def oversized_child(*args, deadline):
                self.assertEqual(deadline, flow.deadline)
                return self.child_result(root, args[1], size=50_000_000)
            with mock.patch.object(pipeline, "_run_with_recovery",
                    side_effect=oversized_child):
                with self.assertRaisesRegex(RuntimeError, "50MB"):
                    flow.process_episodes({"asr_model": "cached", "minimum_free_gib": 0}, source,
                        pipeline.digest(source), {}, 6, 0, "auto", 2)
            self.assertFalse(list((flow.folder / "outputs").glob("episode_*/*.mp4")))

    def test_cancel_between_children_stops_before_the_next_model(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cancellation = threading.Event()
            flow, source, calls, asr, events = self.fixture(root, cancel=cancellation)

            def child(*args, deadline):
                self.assertEqual(deadline, flow.deadline)
                result = self.child_result(root, args[1])
                cancellation.set()
                return result

            with mock.patch.object(pipeline, "_run_with_recovery", side_effect=child) as child_call:
                with self.assertRaisesRegex(RuntimeError, "取消"):
                    flow.process_episodes({"asr_model": "cached", "minimum_free_gib": 0}, source,
                        pipeline.digest(source), {}, 6, 0, "auto", 2)
            self.assertEqual(child_call.call_count, 1)
            self.assertEqual(source.read_bytes(), b"source fixture; never decoded")

    def test_prepared_asr_reuses_exact_offsets_and_rejects_changed_binding(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            flow, source, calls, full_asr, events = self.fixture(root)
            path = flow.folder / "episode_asr/part.json"
            local_asr = {"source_language": "fr", "language": "fr", "duration_s": 1,
                         "original_source_start_s": 2, "original_source_end_s": 3,
                         "segments": [{"segment_id": "a", "start_s": .04, "end_s": .8,
                                       "original_source_start_s": 2.04, "original_source_end_s": 2.8,
                                       "text_de": "Texte."}]}
            pipeline.save(path, local_asr)
            child_job = {"id": "web_prepared_audit", "input_path": str(source),
                         "options": {"source_language": "fr"},
                         "_episode_asr": {"parent_id": flow.name, "path": str(path),
                                          "sha256": pipeline.digest(path), "clip_sha256": pipeline.digest(source)}}
            child = pipeline.Flow(root, child_job, lambda *args: None,
                                  mock.Mock(side_effect=AssertionError("ASR model started")), lambda: False)
            for name in ("audio", "separated"):
                (child.folder / name).mkdir(parents=True, exist_ok=True)
            (child.folder / "audio/asr16k.wav").write_bytes(b"audio")
            outputs = [child.folder / "separated/vocals.wav", child.folder / "separated/background.wav"]
            for output in outputs:
                output.write_bytes(b"separated fixture")
            child.separate = lambda **kwargs: {"name": "separation", "signature": {"fixture": True},
                                               "outputs": outputs, "cache_hit": False}
            result_path = child._analyze_audio({"asr_model": "cached"})
            self.assertEqual(pipeline.read(result_path), local_asr)
            child.runner.assert_not_called()
            path.write_text("{}")
            with self.assertRaisesRegex(RuntimeError, "绑定不符"):
                child._analyze_audio({"asr_model": "cached"})


if __name__ == "__main__":
    unittest.main()
