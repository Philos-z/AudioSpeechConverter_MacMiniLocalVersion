"""Protect source coverage, word ownership, and real non-keyframe clip cuts."""
import copy
import array
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock


spec = importlib.util.spec_from_file_location(
    "episode_planner", Path(__file__).parents[1] / "web/episode_planner.py")
planner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(planner)


def row(identifier, start, end, words=None, text=None):
    result = {"segment_id": identifier, "start_s": start, "end_s": end,
              "text_de": text or "".join(w[2] for w in (words or [])) or identifier,
              "source_language": "fr"}
    if words is not None:
        result["words"] = [{"start_s": s, "end_s": e, "word": t} for s, e, t in words]
    return result


class EpisodePlannerTests(unittest.TestCase):
    def test_complete_utterance_wins_over_a_later_partial_attribution(self):
        source={'language':'en','segments':[
            row('first',0,2.16,text='The library opens tomorrow.'),
            row('second',2.58,3.86,text='It does not close at noon.'),
            row('third',4.2,8,[(4.2,4.4,'According'),(4.5,4.7,' to'),(4.8,4.96,' the'),
                (5.1,6,' announcement,'),(6.1,8,' visitors may stay.')],
                'According to the announcement, visitors may stay.') ]}
        plan=planner.plan_episodes(source,9,max_seconds=5)
        self.assertEqual(plan['episodes'][0]['source_end_s'],4.2)
        self.assertEqual(plan['episode_count'],2)
        self.assertEqual(plan['episodes'][1]['segments'][0]['text_de'],source['segments'][2]['text_de'])

    def test_default_thirty_minute_limit_retains_full_tail_and_one_language(self):
        source = {"language": "fr", "segments": [
            row("a", 0, 1795), row("b", 1798, 1802), row("c", 1805, 3590),
            row("d", 3591, 3603)]}
        original = copy.deepcopy(source)
        plan = planner.plan_episodes(source, 3605.017)
        self.assertEqual(source, original)
        self.assertEqual(plan["episode_count"], 3)
        self.assertEqual(plan["episodes"][0]["source_end_s"], 1798)
        self.assertEqual(plan["episodes"][-1]["source_end_s"], 3605.017)
        for index, episode in enumerate(plan["episodes"]):
            self.assertLessEqual(episode["duration_s"], 1800)
            self.assertEqual(episode["source_language"], "fr")
            self.assertTrue(episode["segments"])
            if index:
                self.assertEqual(episode["source_start_s"], plan["episodes"][index-1]["source_end_s"])
            self.assertAlmostEqual(episode["source_start_s"] * 25, episode["start_frame"])

    def test_word_split_preserves_exact_text_and_global_word_provenance(self):
        original_text = "Bonjour,  le monde! Salut."
        source = {"language": "fr", "segments": [row("long", .12, 4.4,
            [(.12, 1.08, "Bonjour,"), (1.2, 2.03, " le"),
             (2.08, 3.1, " monde!"), (3.2, 4.4, " Salut.")], original_text)]}
        source["segments"][0]["text_source"] = original_text
        snapshot = copy.deepcopy(source)
        plan = planner.plan_episodes(source, 4.51, max_seconds=2.1)
        self.assertEqual(source, snapshot)
        rows = [r for ep in plan["episodes"] for r in ep["segments"]]
        self.assertEqual("".join(r["source_text_fragment"] for r in rows), original_text)
        self.assertEqual("".join(r["text_de"] for r in rows), original_text)
        self.assertEqual("".join(r["text_source"] for r in rows), original_text)
        words = [w for r in rows for w in r["words"]]
        self.assertEqual([w["source_word_id"] for w in words],
                         [f"long:w{i:04d}" for i in range(1, 5)])
        for ep in plan["episodes"]:
            for r in ep["segments"]:
                for word in r["words"]:
                    self.assertGreaterEqual(word["start_s"], 0)
                    self.assertLessEqual(word["end_s"], ep["duration_s"] + 1e-7)
                    self.assertAlmostEqual(word["start_s"] + ep["source_start_s"], word["original_source_start_s"])
        local = planner.slice_asr(source, plan["episodes"][1])
        self.assertEqual(local["language"], "fr")
        self.assertTrue(local["episode_asr_reused_from_complete_source"])
        local["segments"][0]["text_de"] = "edited"
        self.assertEqual(source, snapshot)

    def test_off_grid_adjacent_words_are_never_rounded_into_a_word(self):
        source = {"language": "ja", "segments": [row("x", 0, 3,
            [(0, 1.015, "一"), (1.015, 2.015, "二"), (2.015, 3, "三")])]}
        with self.assertRaisesRegex(planner.EpisodePlanningError, "EPISODE_BOUNDARY_REVIEW_REQUIRED"):
            planner.plan_episodes(source, 3, max_seconds=1.6)

    def test_a_very_short_gap_without_a_frame_boundary_is_not_cut(self):
        source = {"language": "ja", "segments": [row("x", 0, 3,
            [(0, 1.013, "一"), (1.017, 2.015, "二"), (2.015, 3, "三")])]}
        with self.assertRaisesRegex(planner.EpisodePlanningError, "EPISODE_BOUNDARY_REVIEW_REQUIRED"):
            planner.plan_episodes(source, 3, max_seconds=1.6)

    def test_segment_gaps_remain_usable_without_word_timestamps(self):
        source = {"source_language": "es", "segments": [row("uno", .1, 1.8), row("dos", 2.1, 3.9)]}
        plan = planner.plan_episodes(source, 4, max_seconds=2)
        self.assertEqual([p["source_end_s"] for p in plan["episodes"]], [2, 4])
        self.assertEqual(plan["episodes"][0]["end_boundary"]["method"], "segment_pause")

    def test_unmapped_or_unreliable_words_cannot_split_a_long_segment(self):
        source = {"language": "en", "segments": [row("a", 0, 3,
            [(0, 1, "one"), (1.1, 2, " two"), (2.1, 3, " three")], "one two MISSING three")]}
        for mutation in ("mismatch", "low_probability"):
            with self.subTest(mutation=mutation):
                example = copy.deepcopy(source)
                if mutation == "low_probability":
                    example["segments"][0]["text_de"] = "one two three"
                    example["segments"][0]["words"][1]["probability"] = .1
                with self.assertRaisesRegex(planner.EpisodePlanningError, "EPISODE_BOUNDARY_REVIEW_REQUIRED"):
                    planner.plan_episodes(example, 3, max_seconds=1.6)

    def test_empty_unknown_language_overlap_invalid_numbers_and_word_times_are_rejected(self):
        valid = {"language": "en", "segments": [row("a", .1, 1,
                 [(.1, .5, "one"), (.5, 1, " two")])]}
        mutations = [lambda x: x.update(segments=[]), lambda x: x.pop("language"),
                     lambda x: x["segments"][0].update(overlap=True),
                     lambda x: x["segments"][0].update(start_s=float("nan")),
                     lambda x: x["segments"][0]["words"][1].update(start_s=.3),
                     lambda x: x["segments"].append(copy.deepcopy(x["segments"][0])),
                     lambda x: x["segments"][0]["words"][0].update(word="")]
        for index, mutate in enumerate(mutations):
            example = copy.deepcopy(valid)
            if index == 1:
                example["segments"][0].pop("source_language")
            mutate(example)
            with self.subTest(index=index), self.assertRaises(planner.EpisodePlanningError):
                planner.plan_episodes(example, 2)
        for duration, maximum, fps in [(0, 1800, 25), (2, 0, 25), (2, 1, 0), (2, .01, 25),
                                        (float("inf"), 1800, 25)]:
            with self.subTest(duration=duration, maximum=maximum, fps=fps), self.assertRaises(ValueError):
                planner.plan_episodes(valid, duration, maximum, fps)

    def test_long_untranscribed_tail_requests_review_instead_of_empty_tts_episode(self):
        with self.assertRaisesRegex(planner.EpisodePlanningError, "EPISODE_NO_SPEECH"):
            planner.plan_episodes({"language": "en", "segments": [row("a", .1, .5)]}, 5, max_seconds=2)

    def test_zero_duration_punctuation_at_cut_is_owned_once(self):
        source = {"language": "en", "segments": [row("a", 0, 3,
            [(0, 1, "one"), (1, 1, ","), (1.1, 2, " two"), (2.1, 3, " three")])]}
        plan = planner.plan_episodes(source, 3, max_seconds=1.5)
        self.assertEqual(plan["validation"]["source_word_count"], 4)
        self.assertTrue(plan["validation"]["all_words_owned_once"])

    def test_thirty_minute_frame_ceiling_ignores_only_float_arithmetic_noise(self):
        self.assertEqual(planner._frame_ceiling(1800.0000000001, 25), 45000)
        self.assertEqual(planner._frame_ceiling(1800.001, 25), 45001)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe are required")
class EpisodeExportTests(unittest.TestCase):
    def test_audio_tail_is_retained_with_last_video_frame_and_large_gaps_require_review(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.mp4"
            subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                "testsrc2=size=160x90:rate=25:duration=2", "-f", "lavfi", "-i",
                "sine=frequency=440:sample_rate=48000:duration=2.1", "-c:v", "libx264",
                "-preset", "ultrafast", "-c:a", "aac", str(source)], check=True)
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            receipt = planner.export_episode(source, Path(folder) / "tail.mp4",
                {"source_start_s": 0, "source_end_s": 2.1, "fps": 25})
            self.assertEqual(receipt["actual_frames"], 53)
            self.assertAlmostEqual(receipt["actual_audio_duration_s"][0], 2.1, places=3)
            self.assertAlmostEqual(receipt["logical_video_tail_extension_s"], .1)
            self.assertAlmostEqual(receipt["video_tail_frame_extension_s"], .12)
            self.assertTrue(receipt["full_decode_passed"])
            self.assertTrue(receipt["decoded_source_audio_pcm24_identical"])
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)
            # No spurious 51st frame when an exact 2s window has floating
            # subtraction noise; this is the same clock rule as 30 minutes.
            exact = planner.export_episode(source, Path(folder) / "exact.mp4",
                {"source_start_s": 0, "source_end_s": 2.0000000001, "fps": 25,
                 "max_episode_seconds": 2})
            self.assertEqual(exact["actual_frames"], 50)
            self.assertLessEqual(exact["actual_container_duration_s"], 2)
            # Replace only the fixture's video duration metadata to exercise
            # the review gate without creating another source video.
            before = planner._probe(source, shutil.which("ffprobe"))
            before["streams"][0]["duration"] = "1.5"
            with mock.patch.object(planner, "_probe", return_value=before):
                with self.assertRaisesRegex(planner.EpisodePlanningError, "0.5 seconds"):
                    planner.export_episode(source, Path(folder) / "gap.mp4",
                        {"source_start_s": 0, "source_end_s": 2.1, "fps": 25})

    def test_tail_packet_fallback_handles_containers_without_stream_duration(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.mkv"
            subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                "testsrc2=size=160x90:rate=25:duration=2", "-f", "lavfi", "-i",
                "sine=frequency=440:sample_rate=48000:duration=2.1", "-c:v", "libx264",
                "-preset", "ultrafast", "-c:a", "aac", str(source)], check=True)
            before = planner._probe(source, shutil.which("ffprobe"))
            self.assertNotIn("duration", before["streams"][0])
            receipt = planner.export_episode(source, Path(folder) / "tail.mp4",
                {"source_start_s": 0, "source_end_s": float(before["format"]["duration"]), "fps": 25})
            self.assertEqual(receipt["source_video_end_method"], "tail_packet_metadata")
            self.assertGreater(receipt["video_tail_frame_extension_s"], 0)
            self.assertTrue(receipt["full_decode_passed"])

    def test_real_non_keyframe_cuts_retain_both_audio_tracks_tail_and_source(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.mp4"
            subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                "testsrc2=size=160x90:rate=25:duration=3.12", "-f", "lavfi", "-i",
                "sine=frequency=440:sample_rate=48000:duration=3.12", "-f", "lavfi", "-i",
                "sine=frequency=880:sample_rate=48000:duration=3.12", "-map", "0:v", "-map", "1:a", "-map", "2:a",
                "-c:v", "libx264", "-g", "250", "-preset", "ultrafast", "-c:a", "aac",
                "-metadata:s:a:0", "language=fra", "-metadata:s:a:1", "language=eng", str(source)], check=True)
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            asr = {"language": "fr", "segments": [row("a", .1, 1), row("b", 1.3, 2.9)]}
            plan = planner.plan_episodes(asr, 3.12, max_seconds=2)
            self.assertEqual(plan["episodes"][0]["source_end_s"], 1.28)
            receipts = []
            for ep in plan["episodes"]:
                output = Path(folder) / f"episode{ep['episode_index']}.mp4"
                receipt = planner.export_episode(source, output, ep)
                self.assertTrue(receipt["full_decode_passed"])
                self.assertEqual(receipt["audio_tracks"], 2)
                self.assertEqual(receipt["actual_frames"], receipt["expected_frames"])
                receipts.append(receipt)
                with self.assertRaises(FileExistsError):
                    planner.export_episode(source, output, ep)
            self.assertEqual(sum(r["actual_frames"] for r in receipts), 78)
            self.assertAlmostEqual(sum(r["actual_audio_duration_s"][0] for r in receipts), 3.12, places=2)
            # Independent timing oracle: decode the tiny fixture from source
            # zero, then trim at the true 1.28s cut.  This catches a shifted
            # preroll even if the production hash repeats the same mistake.
            for track in (0, 1):
                def samples(path, trim=False):
                    command = [shutil.which("ffmpeg"), "-v", "error", "-i", str(path),
                               "-map", f"0:a:{track}", "-vn"]
                    if trim:
                        command += ["-af", "atrim=start=1.28:end=3.12,asetpts=PTS-STARTPTS"]
                    command += ["-c:a", "pcm_f32le", "-f", "f32le", "-"]
                    return array.array("f", subprocess.run(command, check=True, capture_output=True).stdout)
                expected_audio = samples(source, True)
                actual_audio = samples(Path(folder) / "episode2.mp4")
                self.assertEqual(len(actual_audio), len(expected_audio))
                # AAC perceptual-noise decoder state can differ after seek;
                # require sample alignment and <1 PCM16 LSB RMS difference.
                rms = math.sqrt(sum((a-b)**2 for a, b in zip(actual_audio, expected_audio)) / len(actual_audio))
                shifted_rms = math.sqrt(sum((a-b)**2 for a, b in zip(actual_audio[1:], expected_audio[:-1])) / (len(actual_audio)-1))
                self.assertLess(rms, 1 / 32768)
                self.assertGreater(shifted_rms, rms * 100)
            # The first frame of episode two must be the true non-keyframe
            # source position, not the preceding keyframe at source zero.
            def frame(path, seek):
                return subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-ss", str(seek),
                    "-i", str(path), "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
                    check=True, capture_output=True).stdout
            expected = frame(source, 1.28)
            actual = frame(Path(folder) / "episode2.mp4", 0)
            self.assertEqual(len(actual), 160 * 90 * 3)
            self.assertLess(sum(abs(a-b) for a, b in zip(actual, expected)) / len(actual), 4)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)
            self.assertFalse(list(Path(folder).glob("*.part.mp4")))
            with self.assertRaises(FileExistsError):
                planner.export_episode(source, source, plan["episodes"][0])


if __name__ == "__main__":
    unittest.main()
