import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("video", Path(__file__).parents[1] / "scripts/video.py")
video = importlib.util.module_from_spec(spec)
spec.loader.exec_module(video)


def segment(identifier, start, end):
    return {"segment_id": identifier, "start_s": start, "end_s": end,
            "speaker_id": "S00", "translation_reviewed": True, "speaker_reviewed": True,
            "text_zh": "这是已审核的测试台词。", "overlap": False}


class MediaContractTests(unittest.TestCase):
    def test_preserve_gaps_and_accept_adjacent_speakers(self):
        video.validate_rows([segment("a", 0, 2), segment("b", 2, 3), segment("c", 5, 8)], 10)

    def test_reject_overlap_or_out_of_range(self):
        for rows in [[segment("a", 0, 3), segment("b", 2, 4)], [segment("a", -1, 2)],
                     [segment("a", 5, 4)], [segment("a", 0, 11)]]:
            with self.assertRaises(ValueError):
                video.validate_rows(rows, 10)

    def test_reject_nonfinite_and_duplicate_times(self):
        for rows in [[segment("a", float("nan"), 2)], [segment("a", 0, float("inf"))],
                     [segment("a", 0, 2), segment("a", 3, 5)]]:
            with self.assertRaises(ValueError):
                video.validate_rows(rows, 10)

    def test_unreviewed_translation_must_not_render(self):
        row = segment("a", 0, 2)
        row["translation_reviewed"] = False
        with self.assertRaisesRegex(ValueError, "REVIEW_REQUIRED"):
            video.validate_rows([row], 10)

    def test_reject_path_traversal_and_symlink_escape(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "project"
            root.mkdir()
            (root / "escape").symlink_to(Path(folder))
            for path in ["../outside.wav", "escape/outside.wav"]:
                with self.assertRaisesRegex(ValueError, "OUTSIDE_PROJECT"):
                    video.confined(root, path)

    def test_subtitle_clock_rolls_over_without_invalid_milliseconds(self):
        self.assertEqual(video.stamp(59.9999), "00:01:00,000")
        self.assertEqual(video.stamp(3600.125), "01:00:00,125")

    def test_loudness_json_can_be_followed_by_ffmpeg_progress_output(self):
        log = ('[Parsed_loudnorm]\\n{"input_i":"-21.2","input_tp":"-4.2",'
               '"input_lra":"0.0","input_thresh":"-31.2","target_offset":"0.0"}'
               '\\n[out] size=200kB time=00:00:05.88 speed=100x')
        result = video.parse_loudness(log)
        self.assertEqual(result["input_i"], "-21.2")


if __name__ == "__main__":
    unittest.main()
