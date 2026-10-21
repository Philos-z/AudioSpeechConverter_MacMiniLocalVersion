"""Plan speech-safe source episodes without loading an inference model.

The complete source is transcribed once by the caller.  Plans own every source
second and every ASR token exactly once; they never manufacture missing words.
Only internal cuts are frame-aligned.  The final fractional video frame is
recorded explicitly so the original tail is retained.
"""
from __future__ import annotations

import argparse
import bisect
import copy
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import uuid


EPS = 1e-7
TEXT_FIELDS = ("text_source", "text_de", "text", "source_text", "text_original")


class EpisodePlanningError(ValueError):
    def __init__(self, code, detail):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _fail(code, detail):
    raise EpisodePlanningError(code, detail)


def _number(value, name):
    try:
        number = float(value)
    except (ValueError, TypeError):
        _fail("INVALID_EPISODE_INPUT", f"{name} must be a finite number")
    if not math.isfinite(number):
        _fail("INVALID_EPISODE_INPUT", f"{name} must be finite")
    return number


def _frame_ceiling(seconds, fps):
    scaled = seconds * fps
    nearest = round(scaled)
    # Arithmetic on large source offsets can produce an extra frame from a
    # few nanoseconds of subtraction error.  This is less than 0.4 microsecond
    # at 25 fps, never a meaningful fraction of a frame or audio sample.
    return nearest if abs(scaled - nearest) <= 1e-5 else math.ceil(scaled)


def _times(row, name):
    return (_number(row.get("start_s", row.get("start")), name + ".start"),
            _number(row.get("end_s", row.get("end")), name + ".end"))


def _language(asr):
    language = asr.get("source_language") or asr.get("language")
    if not language:
        values = {r.get("source_language") for r in asr.get("segments", [])
                  if r.get("source_language")}
        if len(values) == 1:
            language = values.pop()
    if not isinstance(language, str) or not language.strip() or language in ("auto", "und"):
        _fail("SOURCE_LANGUAGE_REVIEW_REQUIRED", "full-source detected language is missing")
    return language.strip().lower()


def _text(row):
    for field in TEXT_FIELDS:
        if isinstance(row.get(field), str) and row[field].strip():
            return field, row[field]
    _fail("INVALID_EPISODE_ASR", "every source segment must contain text")


def _compact(text):
    return "".join(text.split())


def _character_edges(text, words):
    """Map word edges to the exact original characters, including whitespace."""
    if _compact(text) != _compact("".join(w["text"] for w in words)):
        return None
    nonspace = [i for i, character in enumerate(text) if not character.isspace()]
    counts, count = [0], 0
    for word in words:
        count += len(_compact(word["text"]))
        counts.append(count)
    # Assign spaces before the next word to that next word.  Concatenating
    # fragments therefore reproduces even nonstandard original spacing.
    edges = [0]
    for count in counts[1:-1]:
        edges.append(nonspace[count - 1] + 1 if count else 0)
    return edges + [len(text)]


def _parse(asr, duration):
    if not isinstance(asr, dict) or not isinstance(asr.get("segments"), list):
        _fail("INVALID_EPISODE_ASR", "segments must be a list")
    language = _language(asr)
    if not asr["segments"]:
        _fail("EPISODE_NO_SPEECH", "no source speech was recognized")
    result, ids, previous_end = [], set(), 0.0
    for row_index, row in enumerate(asr["segments"]):
        if not isinstance(row, dict):
            _fail("INVALID_EPISODE_ASR", "segment must be an object")
        sid = str(row.get("segment_id", row.get("id", f"seg{row_index + 1:04d}")))
        if sid in ids:
            _fail("INVALID_EPISODE_ASR", f"duplicate segment id {sid}")
        ids.add(sid)
        start, end = _times(row, sid)
        if start < -EPS or end <= start or end > duration + EPS or start < previous_end - EPS:
            _fail("INVALID_EPISODE_ASR", f"{sid}: overlap, invalid duration, or out-of-range segment")
        if row.get("overlap"):
            _fail("EPISODE_BOUNDARY_REVIEW_REQUIRED", f"{sid}: overlapping speech requires review")
        previous_end = end
        field, text = _text(row)
        words, word_end = [], start
        raw_words = row.get("words", [])
        if not isinstance(raw_words, list):
            _fail("INVALID_EPISODE_ASR", f"{sid}: words must be a list")
        for word_index, raw in enumerate(raw_words):
            if not isinstance(raw, dict):
                _fail("INVALID_EPISODE_ASR", f"{sid}: word must be an object")
            ws, we = _times(raw, f"{sid}.word{word_index}")
            token = raw.get("word", raw.get("text", ""))
            if (not isinstance(token, str) or not token.strip() or ws < start - EPS
                    or we > end + EPS or we < ws or ws < word_end - EPS):
                _fail("INVALID_EPISODE_ASR", f"{sid}: invalid, overlapping or empty word timestamps")
            probability = raw.get("probability")
            reliable = probability is None or _number(probability, "word probability") >= .3
            words.append({"raw": raw, "start": ws, "end": we, "text": token,
                          "id": f"{sid}:w{word_index + 1:04d}", "reliable": reliable})
            word_end = we
        edges = _character_edges(text, words) if words else None
        splittable = edges is not None and all(w["reliable"] for w in words)
        result.append({"raw": row, "id": sid, "start": start, "end": end,
                       "field": field, "text": text, "words": words,
                       "edges": edges, "splittable": splittable})
    return language, result


def _free_intervals(rows, duration):
    protected = []
    for row in rows:
        protected.extend([(w["start"], w["end"]) for w in row["words"]
                          if w["end"] > w["start"]] if row["splittable"]
                         else [(row["start"], row["end"])])
    protected.sort()
    free, end = [], 0.0
    for left, right in protected:
        if left >= end - EPS:
            free.append((end, left))
        end = max(end, right)
    if end <= duration:
        free.append((end, duration))
    return free


def _pick_cut(rows, free, start, limit, fps, max_seconds):
    if not any(row["start"] < limit - EPS and row["end"] > start + EPS for row in rows):
        _fail("EPISODE_NO_SPEECH", f"no recognized speech between {start:.3f} and {limit:.3f} seconds")
    candidates = []
    for left, right in free:
        frame = math.floor(min(right, limit) * fps + EPS)
        cut = frame / fps
        if cut <= start + EPS or cut < left - EPS or cut > limit + EPS:
            continue
        # A leading-music-only episode cannot enter the dubbing pipeline.
        if not any(row["start"] < cut - EPS and row["end"] > start + EPS for row in rows):
            continue
        complete = not any(row["start"] + EPS < cut < row["end"] - EPS for row in rows)
        candidates.append((cut, complete, left, right))
    if not candidates:
        _fail("EPISODE_BOUNDARY_REVIEW_REQUIRED",
              f"no speech-safe {fps:g} fps boundary in ({start:.3f}, {limit:.3f}] seconds")
    latest = max(candidates)
    # A complete source utterance takes priority over filling the episode.
    # Otherwise a short final fragment such as "According to the" loses the
    # attribution context and may have no safe Chinese dubbing time slot.
    preferred = [c for c in candidates if c[1]]
    chosen = max(preferred) if preferred else latest
    return chosen[0], {"method": "segment_pause" if chosen[1] else "word_gap",
                       "safe_interval_start_s": chosen[2], "safe_interval_end_s": chosen[3],
                       "acoustic_silence_verified": False,
                       "evidence": "validated full-source ASR timestamps"}


def _owner(boundaries, start, end):
    # Zero-duration punctuation exactly on an internal boundary belongs right.
    index = min(bisect.bisect_right(boundaries, start + EPS) - 1, len(boundaries) - 2)
    if end > boundaries[index + 1] + EPS:
        _fail("EPISODE_BOUNDARY_REVIEW_REQUIRED", "a proposed cut crosses an ASR word")
    return index


def _shift(raw, start, end, offset):
    result = copy.deepcopy(raw)
    result["start_s"], result["end_s"] = round(start - offset, 9), round(end - offset, 9)
    for key, value in (("start", start), ("end", end)):
        if key in result:
            result[key] = round(value - offset, 9)
    result["original_source_start_s"], result["original_source_end_s"] = start, end
    return result


def plan_episodes(asr_dict, duration_s, max_seconds=1800, fps=25):
    """Return a complete frame-clock plan or raise a review-required error.

    Source language is detected once by the caller, never guessed per episode.
    Text mismatch, low-confidence or missing word timing prevents splitting a
    segment, while safe complete-segment boundaries remain available.
    """
    duration = _number(duration_s, "duration_s")
    maximum, fps = _number(max_seconds, "max_seconds"), _number(fps, "fps")
    if duration <= 0 or maximum <= 0 or fps <= 0 or maximum * fps < 1:
        _fail("INVALID_EPISODE_INPUT", "duration, maximum and fps must be positive")
    language, rows = _parse(asr_dict, duration)
    free = _free_intervals(rows, duration)
    boundaries, evidence = [0.0], []
    maximum_frames = math.floor(maximum * fps + EPS)
    while duration - boundaries[-1] > maximum + EPS:
        limit = (round(boundaries[-1] * fps) + maximum_frames) / fps
        cut, proof = _pick_cut(rows, free, boundaries[-1], limit, fps, maximum)
        boundaries.append(cut)
        evidence.append(proof)
    boundaries.append(duration)
    evidence.append({"method": "source_tail", "source_tail_preserved": True})
    episodes = []
    for index, (start, end) in enumerate(zip(boundaries, boundaries[1:])):
        episodes.append({"episode_index": index + 1, "episode_number": index + 1,
                         "source_start_s": start, "source_end_s": end,
                         "original_source_start_s": start, "original_source_end_s": end,
                         "duration_s": round(end - start, 9), "fps": fps,
                         "max_episode_seconds": maximum,
                         "start_frame": round(start * fps),
                         "end_frame": _frame_ceiling(end, fps),
                         "source_language": language, "segments": [], "word_ids": [],
                         "segment_ids": [], "end_boundary": evidence[index]})
    all_word_ids, seen_word_ids, fragments = [], [], {r["id"]: [] for r in rows}
    for row in rows:
        groups = {}
        for wi, word in enumerate(row["words"]):
            owner = _owner(boundaries, word["start"], word["end"])
            groups.setdefault(owner, []).append(wi)
            all_word_ids.append(word["id"])
        if not row["words"]:
            groups[_owner(boundaries, row["start"], row["end"])] = []
        if len(groups) > 1 and not row["splittable"]:
            _fail("EPISODE_BOUNDARY_REVIEW_REQUIRED", f"{row['id']}: words do not map to full source text")
        split = len(groups) > 1
        for owner, indices in groups.items():
            episode = episodes[owner]
            offset, end = episode["source_start_s"], episode["source_end_s"]
            local_start, local_end = max(row["start"], offset), min(row["end"], end)
            fragment = row["text"]
            if split:
                fragment = row["text"][row["edges"][indices[0]]:row["edges"][indices[-1] + 1]]
            output = _shift(row["raw"], local_start, local_end, offset)
            output.update(segment_id=(f"{row['id']}.e{owner + 1:03d}" if split else row["id"]),
                          original_segment_id=row["id"], source_language=language,
                          source_text_fragment=fragment, source_text_original=row["text"],
                          source_start_s=local_start, source_end_s=local_end,
                          episode_index=owner + 1)
            for field in TEXT_FIELDS:
                if row["raw"].get(field) == row["text"]:
                    output[field] = fragment
            output[row["field"]] = fragment
            # Legacy downstream readers use text_de as a source-text slot.
            # The language field identifies the actual language.
            if "text_source" in output:
                output["text_source"] = fragment
            output["words"] = []
            for wi in indices:
                word = row["words"][wi]
                item = _shift(word["raw"], word["start"], word["end"], offset)
                item["source_word_id"] = word["id"]
                output["words"].append(item)
                episode["word_ids"].append(word["id"])
                seen_word_ids.append(word["id"])
            fragments[row["id"]].append(fragment)
            episode["segment_ids"].append(output["segment_id"])
            episode["segments"].append(output)
    for episode in episodes:
        if not episode["segments"]:
            _fail("EPISODE_NO_SPEECH", f"episode {episode['episode_index']} has no recognized speech")
        if episode["duration_s"] > maximum + EPS:
            _fail("INVALID_EPISODE_PLAN", "episode exceeds the requested maximum")
    if all_word_ids != seen_word_ids or len(set(seen_word_ids)) != len(seen_word_ids):
        _fail("INVALID_EPISODE_PLAN", "source word ownership is incomplete or duplicated")
    if any("".join(fragments[r["id"]]) != r["text"] for r in rows):
        _fail("INVALID_EPISODE_PLAN", "full source text ownership is incomplete")
    return {"schema_version": 1, "planner": "speech_safe_episodes_v1", "status": "ready",
            "source_language": language, "source_duration_s": duration,
            "max_episode_seconds": maximum, "fps": fps, "episode_count": len(episodes),
            "episodes": episodes,
            "validation": {"continuous_source_coverage": True, "source_tail_preserved": True,
                           "no_episode_overlap": True, "all_words_owned_once": True,
                           "full_source_text_preserved": True, "source_word_count": len(all_word_ids),
                           "internal_cuts_on_frame_clock": True, "cuts_inside_words": 0,
                           "asr_input_mutated": False, "human_listening_verified": False}}


def slice_asr(asr, episode):
    """Copy a planned episode into the existing ASR schema, retaining provenance."""
    result = copy.deepcopy(asr)
    result.update(segments=copy.deepcopy(episode["segments"]),
                  language=episode["source_language"], source_language=episode["source_language"],
                  duration_s=episode["duration_s"], episode_index=episode["episode_index"],
                  original_source_start_s=episode["source_start_s"],
                  original_source_end_s=episode["source_end_s"],
                  episode_asr_reused_from_complete_source=True)
    return result


def _binary(explicit, name):
    binary = explicit or shutil.which(name) or f"/opt/homebrew/bin/{name}"
    if not Path(binary).is_file():
        raise FileNotFoundError(f"{name} is not available")
    return str(binary)


def _run(argv):
    return subprocess.run([str(x) for x in argv], check=True, capture_output=True,
                          text=True, timeout=7200)


def _probe(path, ffprobe):
    return json.loads(_run([ffprobe, "-v", "error", "-show_streams", "-show_format",
                            "-of", "json", path]).stdout)


def _video_end(source, stream, format_duration, ffprobe):
    """Find video coverage independently of a potentially longer audio track."""
    start = _number(stream.get("start_time", 0), "video start")
    if stream.get("duration") not in (None, "N/A"):
        return start + _number(stream["duration"], "source video duration"), "stream_duration"
    if stream.get("duration_ts") is not None and stream.get("time_base"):
        numerator, denominator = stream["time_base"].split("/")
        duration = float(stream["duration_ts"]) * float(numerator) / float(denominator)
        return start + _number(duration, "source video duration"), "stream_time_base"
    # MKV/WebM often omit per-stream duration.  Reading tail packet metadata
    # seeks near EOF and does not decode the full source or load a model.
    result = json.loads(_run([ffprobe, "-v", "error", "-read_intervals",
        f"{max(0., format_duration - 2.):.9f}%", "-select_streams", "v:0", "-show_packets",
        "-show_entries", "packet=pts_time,duration_time", "-of", "json", source]).stdout)
    packet_ends = []
    rate = stream.get("avg_frame_rate", "0/1").split("/")
    frame_duration = float(rate[1]) / float(rate[0]) if len(rate) == 2 and float(rate[0]) > 0 else 0
    for packet in result.get("packets", []):
        if packet.get("pts_time") not in (None, "N/A"):
            packet_ends.append(_number(packet["pts_time"], "video packet PTS") +
                _number(packet.get("duration_time", frame_duration), "video packet duration"))
    if not packet_ends:
        _fail("EPISODE_BOUNDARY_REVIEW_REQUIRED", "source video tail coverage could not be verified")
    return max(packet_ends), "tail_packet_metadata"


def export_episode(source, output, episode, *, encoder="libx264", ffmpeg=None,
                   ffprobe=None, video_bitrate="1200k"):
    """Precisely seek and re-encode one source clip; never overwrite any file.

    This is an intermediate source clip, not the final <50 MB dubbed delivery.
    All source audio streams are retained at their original speed.  No speech
    model is loaded, and the source is never modified.
    """
    source, output = Path(source).resolve(), Path(output).absolute()
    if output.exists() or output.is_symlink() or output.resolve() == source:
        raise FileExistsError(f"episode destination already exists: {output}")
    if encoder not in ("libx264", "h264_videotoolbox"):
        raise ValueError("unsupported episode encoder")
    start = _number(episode["source_start_s"], "source_start_s")
    end = _number(episode["source_end_s"], "source_end_s")
    fps = _number(episode.get("fps", 25), "fps")
    duration = end - start
    requested_duration = duration
    maximum = _number(episode.get("max_episode_seconds", 1800), "max_episode_seconds")
    if start < 0 or duration <= 0 or fps <= 0:
        _fail("INVALID_EPISODE_INPUT", "invalid episode source range")
    if abs(start * fps - round(start * fps)) > 1e-5:
        _fail("INVALID_EPISODE_INPUT", "episode start is not on the planned frame clock")
    if maximum <= 0 or maximum > 1800 or duration > maximum + EPS:
        _fail("INVALID_EPISODE_INPUT", "episode exceeds its duration limit")
    if abs(duration * fps - round(duration * fps)) <= 1e-5:
        duration = round(duration * fps) / fps
    ffmpeg, ffprobe = _binary(ffmpeg, "ffmpeg"), _binary(ffprobe, "ffprobe")
    before = _probe(source, ffprobe)
    videos = [s for s in before["streams"] if s["codec_type"] == "video"]
    audio = [s for s in before["streams"] if s["codec_type"] == "audio"]
    if not videos or not audio:
        _fail("EPISODE_EMPTY_MEDIA", "source must contain video and at least one audio stream")
    source_duration = _number(before["format"]["duration"], "source media duration")
    if end > source_duration + .08:
        _fail("INVALID_EPISODE_INPUT", "episode exceeds actual source duration")
    video_end, video_end_method = _video_end(source, videos[0], source_duration, ffprobe)
    logical_tail_extension = max(0., end - video_end)
    if logical_tail_extension > .5 + EPS:
        _fail("EPISODE_BOUNDARY_REVIEW_REQUIRED", "source audio/video tail differs by more than 0.5 seconds")
    # Two extra frames are only filter headroom: trim discards them at the
    # planned frame count.  They prevent EOF/fps rounding from dropping a tail.
    tail_padding = logical_tail_extension + 2 / fps
    # Decode a short preroll, then trim on the local timeline.  In particular,
    # input-side '-ss 0' drops an AAC priming frame with some FFmpeg demuxers;
    # omitting the seek at source zero retains those original samples.
    seek = max(0., start - 1.)
    if seek < .04:
        seek = 0.
    preroll = start - seek
    preroll_frames = round(preroll * fps)
    expected_frames = _frame_ceiling(duration, fps)
    pcm_bytes_per_second = sum(int(s.get("channels", 0)) * int(s.get("sample_rate", 0)) * 3 for s in audio)
    if any(not 1 <= int(s.get("channels", 0)) <= 8 for s in audio) or pcm_bytes_per_second > 1_500_000:
        _fail("EPISODE_BOUNDARY_REVIEW_REQUIRED", "source multitrack lossless audio needs a separate space review")
    audio_filter = f"atrim=start={preroll:.9f}:end={preroll + duration:.9f},asetpts=PTS-STARTPTS"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.parent / f".{output.stem}.{uuid.uuid4().hex}.part.mp4"
    try:
        argv = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-n"]
        if seek:
            argv += ["-ss", f"{seek:.9f}"]
        argv += ["-noautorotate", "-i", source,
                "-t", f"{expected_frames / fps:.9f}", "-map", "0:v:0", "-map", "0:a",
                "-map_metadata", "0", "-vf",
                f"fps=fps={fps:g}:start_time=0,tpad=stop_mode=clone:stop_duration={tail_padding:.9f},"
                f"trim=start_frame={preroll_frames}:end_frame={preroll_frames + expected_frames},setpts=PTS-STARTPTS",
                "-af", audio_filter,
                "-c:v", encoder, "-b:v", video_bitrate, "-pix_fmt", "yuv420p"]
        argv += (["-preset", "fast"] if encoder == "libx264" else ["-allow_sw", "1"])
        # ALAC is an intermediate source format.  Avoiding a second lossy AAC
        # generation also makes complete decoded source PCM provable below.
        argv += ["-c:a", "alac", "-sample_fmt", "s32p", "-movflags", "+faststart", temporary]
        _run(argv)
        after = _probe(temporary, ffprobe)
        out_v = [s for s in after["streams"] if s["codec_type"] == "video"]
        out_a = [s for s in after["streams"] if s["codec_type"] == "audio"]
        if len(out_v) != 1 or len(out_a) != len(audio):
            _fail("EPISODE_EXPORT_INVALID", "source video or an audio stream is missing")
        for field in ("width", "height"):
            if videos[0].get(field) != out_v[0].get(field):
                _fail("EPISODE_EXPORT_INVALID", f"source {field} changed")
        old_sar = videos[0].get("sample_aspect_ratio") or "1:1"
        new_sar = out_v[0].get("sample_aspect_ratio") or "1:1"
        if old_sar != new_sar:
            _fail("EPISODE_EXPORT_INVALID", "source sample_aspect_ratio changed")
        actual_frames = int(out_v[0].get("nb_frames", 0))
        if actual_frames != expected_frames:
            _fail("EPISODE_EXPORT_INVALID", f"frame coverage differs: {actual_frames} != {expected_frames}")
        video_duration = _number(out_v[0].get("duration", after["format"]["duration"]), "video duration")
        if abs(video_duration - duration) > 1 / fps + .005:
            _fail("EPISODE_EXPORT_INVALID", "video duration differs from planned source range")
        container_duration = _number(after["format"]["duration"], "encoded episode duration")
        if max(container_duration, video_duration) > maximum + .000001:
            _fail("EPISODE_EXPORT_INVALID", "encoded episode exceeds its duration limit")
        audio_durations = []
        for old, new in zip(audio, out_a):
            actual = _number(new.get("duration", after["format"]["duration"]), "audio duration")
            if actual <= 0 or abs(actual - duration) > .08:
                _fail("EPISODE_EMPTY_MEDIA", "an audio stream is empty or truncated")
            if old.get("channels") != new.get("channels"):
                _fail("EPISODE_EXPORT_INVALID", "audio channel count changed")
            old_lang = old.get("tags", {}).get("language") or "und"
            new_lang = new.get("tags", {}).get("language") or "und"
            if old_lang != new_lang:
                _fail("EPISODE_EXPORT_INVALID", "audio language metadata changed")
            audio_durations.append(actual)
        _run([ffmpeg, "-hide_banner", "-v", "error", "-xerror", "-i", temporary,
              "-map", "0:v", "-map", "0:a", "-f", "null", "-"])
        expected_argv = [ffmpeg, "-v", "error", "-nostdin"]
        if seek:
            expected_argv += ["-ss", f"{seek:.9f}"]
        expected_argv += ["-i", source, "-t", f"{duration:.9f}", "-map", "0:a", "-vn", "-af", audio_filter,
                          "-c:a", "pcm_s24le", "-f", "streamhash", "-hash", "sha256", "-"]
        source_audio_hashes = _run(expected_argv).stdout.strip()
        output_audio_hashes = _run([ffmpeg, "-v", "error", "-nostdin", "-i", temporary,
            "-map", "0:a", "-vn", "-c:a", "pcm_s24le", "-f", "streamhash", "-hash", "sha256", "-"]).stdout.strip()
        if not source_audio_hashes or source_audio_hashes != output_audio_hashes:
            _fail("EPISODE_EXPORT_INVALID", "decoded source audio differs from the complete planned source interval")
        # Hard-link publication is exclusive even if another writer creates
        # the destination while validation runs.  The temp shares its volume.
        os.link(temporary, output)
        return {"schema_version": 1, "output": str(output), "source": str(source),
                "source_start_s": start, "source_end_s": end, "duration_s": duration,
                "numeric_frame_clock_adjustment_s": duration - requested_duration,
                "actual_video_duration_s": video_duration, "actual_audio_duration_s": audio_durations,
                "actual_container_duration_s": container_duration, "max_episode_seconds": maximum,
                "source_video_end_s": video_end, "source_video_end_method": video_end_method,
                "logical_video_tail_extension_s": logical_tail_extension,
                "video_tail_frame_extension_s": round(max(0., start + video_duration - video_end), 9),
                "tail_frame_policy": "clone last source frame; preserve complete audio tail",
                "expected_frames": expected_frames, "actual_frames": actual_frames,
                "audio_tracks": len(audio), "source_audio_speed": 1.0,
                "intermediate_audio_codec": "alac", "decoded_source_audio_pcm24_identical": True,
                "decoded_audio_hashes": output_audio_hashes.splitlines(),
                "all_source_audio_tracks_retained": True, "full_decode_passed": True,
                "input_side_accurate_seek": bool(seek), "input_seek_s": seek,
                "decoded_preroll_s": preroll, "no_accumulated_full_source_decode": True,
                "encoder": encoder,
                "bytes": output.stat().st_size}
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--asr", type=Path, required=True)
    plan.add_argument("--duration", type=float, required=True)
    plan.add_argument("--max-seconds", type=float, default=1800)
    plan.add_argument("--fps", type=float, default=25)
    plan.add_argument("--output", type=Path, required=True)
    export = sub.add_parser("export")
    export.add_argument("--source", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--plan", type=Path, required=True)
    export.add_argument("--episode", type=int, required=True)
    export.add_argument("--receipt", type=Path)
    export.add_argument("--encoder", default="libx264")
    args = parser.parse_args()
    if args.action == "plan":
        result = plan_episodes(json.loads(args.asr.read_text()), args.duration, args.max_seconds, args.fps)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        plan = json.loads(args.plan.read_text())
        if args.episode < 1 or args.episode > len(plan["episodes"]):
            parser.error("episode number is outside the plan")
        result = export_episode(args.source, args.output, plan["episodes"][args.episode - 1], encoder=args.encoder)
        if args.receipt:
            args.receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
