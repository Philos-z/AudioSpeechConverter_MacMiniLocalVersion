"""Offline Flow regressions: review inputs invalidate only translation state.

The actual staged Flow.stages/cached/remember implementations run against tiny
temporary fixtures. Analysis/model commands are never executed; the only fake
command writes a reviewed translation fixture, then execution stops before any
background, TTS, or media work.
"""
import ast
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_WEB = ROOT / "web"
old_path = list(sys.path)
try:
    sys.path.insert(0, str(PRODUCTION_WEB))
    import website_recovery
    spec = importlib.util.spec_from_file_location("offline_translation_cache_fix", ROOT / "web/pipeline.py")
    pipeline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pipeline)
finally:
    sys.path[:] = old_path


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class TranslationCheckedStop(RuntimeError):
    """The real Flow reached the background boundary after translation checks."""


class FakeFlow(pipeline.Flow):
    def acquire_source(self):
        return self.root / "source.mp4"

    def _analyze_audio(self, config):
        return self.folder / "segments.asr.json"

    def cached(self, name, signature, outputs):
        if name in {"prepare", "speakers"}:
            return True  # Those independent fixture stages were already done.
        if name == "translation":
            self.translation_signature = copy.deepcopy(signature)
            return super().cached(name, signature, outputs)
        if name == "background":
            raise TranslationCheckedStop()
        raise AssertionError("Unexpected model/media stage: " + name)

    def command(self, stage, start, end, argv, **kwargs):
        if stage != "机器翻译与第二遍审校" or "_translate" not in [str(x) for x in argv]:
            raise AssertionError("No real model/media commands are allowed: " + stage)
        self.translation_calls.append(list(argv))
        rows = pipeline.read(self.folder / "translation/input.json")["segments"]
        updated = [{**row, "text_zh": "你好。", "translation_reviewed": True,
            "human_reviewed": False, "translation_review_method": "proxy_second_pass_and_numeric_checks"}
            for row in rows]
        pipeline.save(self.folder / "segments.json", {"segments": updated})
        pipeline.save(self.folder / "translation/report.json", {
            "source_language": "de", "human_reviewed": False,
            "review_method": "two_pass_machine_review_and_numeric_invariants",
            "offline_translation_call": len(self.translation_calls)})
        return {"returncode": 0, "stdout": ""}


class TranslationSignatureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="philos-tsig-offline-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / "jobs/web_signature_fixture"
        self.hints = self.folder / "translation/source_review_hints.json"
        self.revision = self.folder / "source_plan_revision.json"
        self.rows = [{"segment_id": "line0001", "speaker_id": "S01", "start_s": 0.0,
            "end_s": 2.0, "text_de": "Hallo.", "text_source": "Hallo.", "source_language": "de",
            "overlap": False, "source_ids": ["seg0001"], "translation_reviewed": False}]
        self.calls = []
        for path in (self.root / "source.mp4", self.root / "tools/python",
                     self.root / "assets/philos_club_serif_wordmark.png",
                     self.root / "scripts/speaker_plan.py",
                     self.root / "web/proxy_client.py",
                     self.folder / "separated/htdemucs/original/vocals.wav",
                     self.folder / "separated/htdemucs/original/no_vocals.wav",
                     self.folder / "audio/original.wav", self.folder / "audio/asr16k.wav"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"offline fixture; never executable")
        # __file__ is patched to this fixture only for identity reads. The
        # implementation executing below remains the real staged Flow class.
        self.fixture_pipeline = self.root / "web/pipeline.py"
        self.fixture_pipeline.write_bytes((ROOT / "web/pipeline.py").read_bytes())
        write_json(self.root / "config/local.json", {"minimum_free_gib": 0})
        write_json(self.folder / "source.json", {"input": str((self.root / "source.mp4").resolve()),
            "duration_s": 3.0, "audio_track": 0, "source_language": "de", "source_language_label": "德语",
            "requested_source_language": "de", "probe": {"format": {"duration": "3.0"},
                "streams": [{"codec_type": "video", "width": 480, "height": 270},
                            {"codec_type": "audio"}]}})
        write_json(self.folder / "segments.asr.json", {"segments": self.rows, "source_language": "de"})
        write_json(self.folder / "segments.plan.json", {"schema_version": 2, "segments": self.rows})
        write_json(self.folder / "speakers.json", {"S01": {"reference": "unused-fixture.wav"}})
        self.job = {"id": "web_signature_fixture", "input_path": str(self.root / "source.mp4"),
            "options": {"source_language": "de", "target_language": "zh", "voice_pilot_groups": 0}}

    def run_to_translation_boundary(self):
        flow = FakeFlow(self.root, self.job, lambda *a, **kw: None,
            lambda *a, **kw: self.fail("A real subprocess would have been launched"), lambda: False)
        flow.py = flow.mlx_py = flow.asr_py = self.root / "tools/python"
        flow.translation_calls = self.calls
        before = len(self.calls)
        with patch.object(pipeline, "__file__", str(self.fixture_pipeline)), \
             patch.object(pipeline, "FFMPEG", str(flow.py)), patch.object(pipeline, "FFPROBE", str(flow.py)), \
             patch.object(pipeline, "space_preflight", return_value={"sufficient": True}):
            with self.assertRaises(TranslationCheckedStop):
                flow.stages()
        return flow, len(self.calls) - before

    def write_hints(self, note):
        write_json(self.hints, {"schema": "source_review_hints_v1", "rows": {"line0001": {
            "source_text_sha256": hashlib.sha256(b"Hallo.").hexdigest(), "note": note}}})

    def write_revision(self, authorization):
        # A valid evidence-bound revision whose row text stays byte-for-byte the
        # same lets this test isolate revision metadata from plan/ASR changes.
        derived = self.folder / "revisions/source_review_v1/segments.plan.json"
        proof = self.folder / "revisions/source_review_v1/evidence/proof.json"
        write_json(derived, {"schema_version": 2, "segments": self.rows})
        write_json(proof, {"fixture_only": True, "confirmed_source": "Hallo."})
        text_sha = hashlib.sha256(b"Hallo.").hexdigest()
        write_json(self.revision, {"schema": "source_plan_revision_v1",
            "original_plan_sha256": sha(self.folder / "segments.plan.json"),
            "input_source_sha256": sha(self.root / "source.mp4"), "revised_plan_sha256": sha(derived),
            "changes": [{"segment_id": "line0001", "action": "replace_source_text",
                "original_text_sha256": text_sha, "replacement_text_sha256": text_sha,
                "evidence": [{"path": str(proof.relative_to(self.folder)), "sha256": sha(proof)}]}],
            "unresolved": [], "blocking_unresolved": [], "user_authorization": authorization})

    def test_absent_review_files_use_none_and_unchanged_input_reuses_real_receipt(self):
        first, calls = self.run_to_translation_boundary()
        self.assertEqual(calls, 1)
        self.assertIsNone(first.translation_signature["source_review_hints_sha256"])
        self.assertIsNone(first.translation_signature["source_plan_revision_sha256"])
        output_bytes = (self.folder / "segments.json").read_bytes()
        second, calls = self.run_to_translation_boundary()
        self.assertEqual(calls, 0)
        self.assertIn("translation", second.metrics["stage_cache_hits"])
        self.assertEqual((self.folder / "segments.json").read_bytes(), output_bytes)

    def test_only_hints_addition_or_change_enters_translate_then_unchanged_reuses(self):
        self.run_to_translation_boundary()
        source_bytes = (self.folder / "segments.plan.json").read_bytes()
        self.write_hints("保留问候语。")
        with_hints, calls = self.run_to_translation_boundary()
        self.assertEqual(calls, 1)
        self.assertEqual(with_hints.translation_signature["source_review_hints_sha256"], sha(self.hints))
        _, calls = self.run_to_translation_boundary()
        self.assertEqual(calls, 0)
        self.write_hints("用户新增说明：使用自然口语问候。")
        _, calls = self.run_to_translation_boundary()
        self.assertEqual(calls, 1)
        self.assertEqual((self.folder / "segments.plan.json").read_bytes(), source_bytes)

    def test_only_revision_addition_or_metadata_change_enters_translate(self):
        self.run_to_translation_boundary()
        source_bytes = (self.folder / "segments.plan.json").read_bytes()
        self.write_revision("初次记录")
        revised, calls = self.run_to_translation_boundary()
        self.assertEqual(calls, 1)
        self.assertEqual(revised.translation_signature["source_plan_revision_sha256"], sha(self.revision))
        _, calls = self.run_to_translation_boundary()
        self.assertEqual(calls, 0)
        self.write_revision("新增用户授权；源行和时间完全不变")
        _, calls = self.run_to_translation_boundary()
        self.assertEqual(calls, 1)
        self.assertEqual((self.folder / "segments.plan.json").read_bytes(), source_bytes)

    def test_review_file_removal_also_invalidates_prior_stage_receipt(self):
        self.write_hints("离线测试说明。")
        self.write_revision("离线测试授权")
        self.run_to_translation_boundary()
        self.hints.unlink()
        _, calls = self.run_to_translation_boundary()
        self.assertEqual(calls, 1)
        self.revision.unlink()
        _, calls = self.run_to_translation_boundary()
        self.assertEqual(calls, 1)
        _, calls = self.run_to_translation_boundary()
        self.assertEqual(calls, 0)

    def test_output_integrity_still_required_even_when_review_inputs_are_unchanged(self):
        self.run_to_translation_boundary()
        path = self.folder / "segments.json"
        old = json.loads(path.read_text())
        old["segments"][0]["text_zh"] = "这不是经过该收据验证的内容。"
        write_json(path, old)
        _, calls = self.run_to_translation_boundary()
        self.assertEqual(calls, 1)
        self.assertEqual(json.loads(path.read_text())["segments"][0]["text_zh"], "你好。")



if __name__ == "__main__":
    unittest.main()
