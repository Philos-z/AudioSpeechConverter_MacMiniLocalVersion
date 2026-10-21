"""Exercise the delivery size boundary and retained media, without long videos."""
import copy
import importlib.util
import math
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


spec = importlib.util.spec_from_file_location(
    "export_size", Path(__file__).parents[1] / "scripts/export_size.py")
export_size = importlib.util.module_from_spec(spec)
spec.loader.exec_module(export_size)


def media_metadata():
    return {"format": {"duration": "10.000000"}, "streams": [
        {"codec_type": "video", "width": 960, "height": 540,
         "sample_aspect_ratio": "1:1", "nb_frames": "250",
         "avg_frame_rate": "25/1"},
        {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000",
         "channels": 2, "tags": {"language": "zho"},
         "disposition": {"default": 1}},
        {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000",
         "channels": 2, "tags": {"language": "deu"},
         "disposition": {"default": 0}},
    ]}


class ExportSizeContractTests(unittest.TestCase):
    def test_size_is_strict_decimal_50_mb(self):
        self.assertEqual(export_size.MAX_BYTES, 50_000_000)
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "export.mp4"
            # Sparse files exercise the exact production boundary cheaply.
            with source.open("wb") as handle:
                handle.truncate(49_999_999)
            with mock.patch.object(export_size, "compress") as compress:
                receipt = export_size.enforce(source)
                self.assertEqual(receipt["mode"], "already_under_limit")
                compress.assert_not_called()
            with source.open("r+b") as handle:
                handle.truncate(50_000_000)

            def fake_compress(original, bounded, **kwargs):
                self.assertEqual(Path(original), source)
                Path(bounded).write_bytes(b"bounded")
                return {"bytes": 7, "output": str(bounded)}

            with mock.patch.object(export_size, "compress", side_effect=fake_compress) as compress:
                export_size.enforce(source)
                compress.assert_called_once()
                self.assertEqual(source.read_bytes(), b"bounded")

    def test_budget_reserves_complete_audio_and_container_space(self):
        duration, audio_bytes = 850.84, 13_800_000
        rate = export_size.video_budget(duration, audio_bytes)
        self.assertGreaterEqual(rate, 32_000)
        self.assertLess(rate, export_size.video_budget(duration, 0))
        self.assertLessEqual(rate * duration / 8 + audio_bytes + 750_000,
                             export_size.TARGET_BYTES)

    def test_impossible_full_audio_budget_or_invalid_duration_is_rejected(self):
        for duration in (0, -1, math.nan, math.inf):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                export_size.video_budget(duration, 0)
        for duration, audio_bytes in ((60, 48_000_000), (20_000, 0)):
            with self.subTest(duration=duration, audio_bytes=audio_bytes), self.assertRaises(ValueError):
                export_size.video_budget(duration, audio_bytes)

    def test_source_existing_destination_and_alias_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            source, destination = Path(folder) / "source.mp4", Path(folder) / "destination.mp4"
            source.write_bytes(b"original")
            destination.write_bytes(b"previous delivery")
            alias = Path(folder) / "alias.mp4"
            alias.symlink_to(source)
            for output in (source, destination, alias):
                with self.subTest(output=output), self.assertRaises(FileExistsError):
                    export_size.compress(source, output)
            self.assertEqual(source.read_bytes(), b"original")
            self.assertEqual(destination.read_bytes(), b"previous delivery")

    def test_compression_limit_cannot_exceed_decimal_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / "source.mp4", Path(folder) / "new.mp4"
            for limits in ({"max_bytes": 50_000_001}, {"target_bytes": 50_000_000},
                           {"max_bytes": 0, "target_bytes": 0}):
                with self.subTest(limits=limits), self.assertRaises(ValueError):
                    export_size.compress(source, output, **limits)

    def test_frame_loss_audio_removal_and_timing_drift_are_rejected(self):
        before = media_metadata()
        for mutation in (lambda data: data["streams"][0].update(nb_frames="249"),
                         lambda data: data["streams"].pop(),
                         lambda data: data["format"].update(duration="10.081"),
                         lambda data: data["streams"][0].update(avg_frame_rate="24/1"),
                         lambda data: data["streams"][0].update(width=640),
                         lambda data: data["streams"][1]["tags"].update(language="deu"),
                         lambda data: data["streams"][1]["disposition"].update(default=0),
                         lambda data: data["streams"][1].update(channels=1)):
            after = copy.deepcopy(before)
            mutation(after)
            with self.subTest(after=after), self.assertRaises(ValueError):
                export_size.validate_streams(before, after)

    def test_equivalent_frame_rate_fraction_is_accepted(self):
        before, after = media_metadata(), media_metadata()
        after["streams"][0]["avg_frame_rate"] = "50/2"
        export_size.validate_streams(before, after)


@unittest.skipUnless(Path(export_size.FFMPEG).is_file() and Path(export_size.FFPROBE).is_file(),
                     "Local ffmpeg/ffprobe are required for the two-second media check")
class ExportSizeShortMediaTests(unittest.TestCase):
    @staticmethod
    def create_source(source):
        # The noisy center makes the source exceed the test cap. The black
        # and white edges expose a full/limited range conversion mistake.
        source_filter = ("testsrc2=size=320x180:rate=25:duration=2,"
            "noise=alls=100:allf=t+u,"
            "drawbox=x=0:y=0:w=80:h=180:color=black:t=fill,"
            "drawbox=x=240:y=0:w=80:h=180:color=white:t=fill,"
            "scale=in_range=tv:out_range=pc,format=yuv420p,setparams=range=full")
        export_size.run([export_size.FFMPEG, "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", source_filter,
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=2",
            "-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "libx264",
            "-preset", "ultrafast", "-crf", "0", "-pix_fmt", "yuv420p", "-color_range", "pc",
            "-c:a", "aac", "-b:a", "64k", "-metadata:s:a:0", "language=zho",
            "-metadata:s:a:1", "language=deu", "-disposition:a:0", "default",
            "-disposition:a:1", "0", source])

    def test_two_pass_export_retains_both_audio_tracks_frames_and_color_levels(self):
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / "source.mp4", Path(folder) / "bounded.mp4"
            self.create_source(source)
            before_sha = export_size.sha(source)
            self.assertGreater(source.stat().st_size, 1_400_000)
            receipt = export_size.compress(source, output, max_bytes=1_400_000, target_bytes=1_200_000)
            self.assertEqual(receipt["mode"], "h264_two_pass")
            self.assertLess(output.stat().st_size, 1_400_000)
            self.assertTrue(receipt["audio_packets_identical"])
            self.assertEqual(export_size.sha(source), before_sha)
            self.assertEqual(export_size.audio_hash(source), export_size.audio_hash(output))
            before, after = export_size.probe(source), export_size.probe(output)
            export_size.validate_streams(before, after)
            self.assertEqual(after["streams"][0]["nb_frames"], "50")
            self.assertEqual([s["tags"]["language"] for s in after["streams"][1:]], ["zho", "deu"])
            for path in (source, output):
                pixels = subprocess.run([export_size.FFMPEG, "-v", "error", "-i", str(path),
                    "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
                    capture_output=True, check=True).stdout
                for x, expected in ((40, 0), (280, 255)):
                    position = (90 * 320 + x) * 3
                    with self.subTest(path=path.name, x=x):
                        self.assertTrue(all(abs(value - expected) <= 3
                                            for value in pixels[position:position + 3]))
            decoded = export_size.run([export_size.FFMPEG, "-v", "error", "-i", output,
                                      "-map", "0:v", "-map", "0:a", "-f", "null", "-"])
            self.assertFalse(decoded.stderr.strip())

    def test_two_pass_export_preserves_bilingual_mov_text_subtitles(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            source, subtitled, output = (folder / name for name in
                                          ("source.mp4", "subtitled.mp4", "bounded.mp4"))
            self.create_source(source)
            texts = ("第一句，保留事实。", "Der erste Satz bleibt erhalten.")
            for language, text in zip(("zh", "de"), texts):
                (folder / (language + ".srt")).write_text(
                    "1\n00:00:00,240 --> 00:00:01,760\n" + text + "\n", encoding="utf-8")
            export_size.run([export_size.FFMPEG, "-hide_banner", "-loglevel", "error",
                "-i", source, "-i", folder / "zh.srt", "-i", folder / "de.srt",
                "-map", "0", "-map", "1:0", "-map", "2:0", "-c", "copy", "-c:s", "mov_text",
                "-metadata:s:s:0", "language=zho", "-metadata:s:s:1", "language=deu",
                "-disposition:s:0", "default", "-disposition:s:1", "0", subtitled])
            original_hash = export_size.sha(subtitled)
            receipt = export_size.compress(subtitled, output,
                                          max_bytes=1_400_000, target_bytes=1_200_000)
            self.assertLess(receipt["bytes"], 1_400_000)
            self.assertEqual(export_size.sha(subtitled), original_hash)
            self.assertEqual(export_size.audio_hash(subtitled), export_size.audio_hash(output))
            before, after = export_size.probe(subtitled), export_size.probe(output)
            export_size.validate_streams(before, after)
            subtitles = [stream for stream in after["streams"] if stream["codec_type"] == "subtitle"]
            self.assertEqual([stream["codec_name"] for stream in subtitles], ["mov_text", "mov_text"])
            self.assertEqual([stream["tags"]["language"] for stream in subtitles], ["zho", "deu"])
            self.assertEqual([stream["disposition"]["default"] for stream in subtitles], [1, 0])
            subtitle_hashes = []
            for path in (subtitled, output):
                subtitle_hashes.append(export_size.run([export_size.FFMPEG, "-v", "error",
                    "-i", path, "-map", "0:s", "-c", "copy", "-f", "streamhash",
                    "-hash", "sha256", "-"]).stdout)
            self.assertEqual(*subtitle_hashes)
            for index, text in enumerate(texts):
                extracted = export_size.run([export_size.FFMPEG, "-v", "error", "-i", output,
                    "-map", "0:s:" + str(index), "-c:s", "srt", "-f", "srt", "-"]).stdout
                self.assertIn(text, extracted)
                self.assertIn("00:00:00,240 --> 00:00:01,760", extracted)


if __name__ == "__main__":
    unittest.main()
