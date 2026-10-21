"""Bound MP4 exports by actual bytes while copying every encoded audio packet.

50 MB means 50,000,000 bytes. Never use ffmpeg -fs, which can truncate speech.
The source is read only; a checked new output is installed atomically.
"""
import argparse
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

MAX_BYTES = 50_000_000
TARGET_BYTES = 48_000_000
POLICY = 'mp4_under_50_decimal_mb_v1'
FFMPEG = '/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg'
FFPROBE = '/opt/homebrew/opt/ffmpeg-full/bin/ffprobe'


def run(argv):
    result = subprocess.run([str(a) for a in argv], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('Size-bounded export failed: ' + result.stderr[-2000:])
    return result


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def probe(path):
    return json.loads(run([FFPROBE, '-v', 'error', '-show_streams', '-show_format',
                           '-of', 'json', path]).stdout)


def audio_hash(path):
    return run([FFMPEG, '-v', 'error', '-i', path, '-map', '0:a', '-c', 'copy',
                '-f', 'streamhash', '-hash', 'sha256', '-']).stdout.strip()


def audio_payload_bytes(path):
    packets = json.loads(run([FFPROBE, '-v', 'error', '-select_streams', 'a',
        '-show_entries', 'packet=size', '-of', 'json', path]).stdout)['packets']
    return sum(int(packet['size']) for packet in packets)


def video_budget(duration, audio_bytes, target_bytes=TARGET_BYTES):
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('Invalid export duration')
    # Reserve MP4 tables and timestamps separately from the 2 MB safety margin.
    rate = math.floor((target_bytes - audio_bytes - 750_000) * 8 / duration)
    if rate < 32_000:
        raise ValueError('Video is too long for 50 MB with its complete audio tracks')
    return rate


def validate_streams(before, after):
    if [s['codec_type'] for s in before['streams']] != [s['codec_type'] for s in after['streams']]:
        raise ValueError('Compression changed media stream coverage')
    if abs(float(before['format']['duration']) - float(after['format']['duration'])) > .04:
        raise ValueError('Compression changed duration')
    for old, new in zip(before['streams'], after['streams']):
        if old['codec_type'] == 'video':
            if not all(str(s.get('nb_frames', '')).isdigit() and int(s['nb_frames']) > 0 for s in (old, new)):
                raise ValueError('Complete video frame count unavailable')
            fields = ('width', 'height', 'sample_aspect_ratio', 'nb_frames')
            if any(old.get(k) != new.get(k) for k in fields):
                raise ValueError('Compression changed geometry or frame count')
            if Fraction(old['avg_frame_rate']) != Fraction(new['avg_frame_rate']):
                raise ValueError('Compression changed frame rate')
        if old['codec_type'] == 'audio':
            for key in ('codec_name', 'sample_rate', 'channels'):
                if old.get(key) != new.get(key):
                    raise ValueError('Compression changed audio format')
            if old.get('tags', {}).get('language') != new.get('tags', {}).get('language'):
                raise ValueError('Compression changed audio language')
            if old['disposition']['default'] != new['disposition']['default']:
                raise ValueError('Compression changed default audio track')
        if old['codec_type'] == 'subtitle':
            if (old['codec_name'] != new['codec_name'] or
                    old.get('tags', {}).get('language') != new.get('tags', {}).get('language') or
                    old['disposition']['default'] != new['disposition']['default']):
                raise ValueError('Compression changed subtitle track format or language')


def compress(source, output, *, max_bytes=MAX_BYTES, target_bytes=TARGET_BYTES, update=None):
    source = Path(source).resolve(); output = Path(output).resolve()
    if source == output or output.exists():
        raise FileExistsError('Compression requires a new output; preserve the source')
    if not 0 < target_bytes < max_bytes <= MAX_BYTES:
        raise ValueError('Invalid size limit')
    started = time.perf_counter(); before = probe(source)
    visual = [s for s in before['streams'] if s['codec_type'] == 'video']
    if len(visual) != 1 or not any(s['codec_type'] == 'audio' for s in before['streams']):
        raise ValueError('Require exactly one video and complete audio tracks')
    source_sha = sha(source); original_bytes = source.stat().st_size
    output.parent.mkdir(parents=True, exist_ok=True)
    receipt = {'schema': POLICY, 'source': str(source), 'source_sha256': source_sha,
               'source_bytes': original_bytes, 'max_bytes_exclusive': max_bytes,
               'target_bytes': target_bytes, 'audio_mode': 'packet_copy', 'attempts': []}
    with tempfile.TemporaryDirectory(prefix='.size_export_', dir=output.parent) as folder:
        temporary = Path(folder); candidate = temporary / 'candidate.mp4'
        if original_bytes < max_bytes:
            shutil.copyfile(source, candidate)
            receipt['mode'] = 'already_under_limit'
        else:
            audio_bytes = audio_payload_bytes(source)
            bitrate = video_budget(float(before['format']['duration']), audio_bytes, target_bytes)
            receipt.update(mode='h264_two_pass', encoder='libx264', preset='slow',
                           copied_audio_payload_bytes=audio_bytes)
            for attempt in range(1, 4):
                common = [FFMPEG, '-hide_banner', '-loglevel', 'error', '-nostats', '-y',
                          '-i', source, '-map', '0:v:0', '-c:v', 'libx264', '-preset', 'slow',
                          '-threads', '4', '-b:v', bitrate, '-pix_fmt', 'yuv420p',
                          '-vf', 'scale=in_range=auto:out_range=tv', '-color_range', 'tv',
                          '-fps_mode', 'passthrough', '-passlogfile', temporary / 'pass']
                if update: update('compressing_video_pass_1', attempt)
                run([*common, '-pass', '1', '-an', '-f', 'null', '/dev/null'])
                if update: update('compressing_video_pass_2', attempt)
                # Override only video encoding; all audio and metadata are copied.
                run([*common, '-map', '0:a', '-map', '0:s?', '-map_metadata', '0',
                     '-c:a', 'copy', '-c:s', 'copy',
                     '-pass', '2', '-movflags', '+faststart', candidate])
                actual = candidate.stat().st_size
                receipt['attempts'].append({'attempt': attempt, 'video_bitrate_bps': bitrate,
                                            'bytes': actual})
                if actual < max_bytes:
                    break
                bitrate = math.floor(bitrate * min(.90, (target_bytes - audio_bytes) /
                                                   max(1, actual - audio_bytes)))
            else:
                raise ValueError('Video remains over 50 MB; no deliverable was published')
            receipt['video_bitrate_target_bps'] = bitrate
        validate_streams(before, probe(candidate))
        original_audio = audio_hash(source); compressed_audio = audio_hash(candidate)
        if original_audio != compressed_audio:
            raise ValueError('Encoded audio packets changed during compression')
        if candidate.stat().st_size >= max_bytes or sha(source) != source_sha:
            raise ValueError('Export size or source identity changed')
        receipt.update(output=str(output), bytes=candidate.stat().st_size,
                       output_sha256=sha(candidate), audio_packet_sha256=compressed_audio,
                       audio_packets_identical=True, duration_s=float(before['format']['duration']),
                       geometry_and_frame_count_preserved=True, wall_s=time.perf_counter()-started)
        candidate.replace(output)
    return receipt


def enforce(path, update=None):
    """Apply to a freshly rendered output before its verifier or publication."""
    path = Path(path)
    if path.stat().st_size < MAX_BYTES:
        return {'schema': POLICY, 'mode': 'already_under_limit', 'bytes': path.stat().st_size,
                'max_bytes_exclusive': MAX_BYTES}
    with tempfile.TemporaryDirectory(prefix='.bounded_', dir=path.parent) as folder:
        bounded = Path(folder) / path.name
        receipt = compress(path, bounded, update=update)
        bounded.replace(path)
    receipt['output'] = str(path.resolve())
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    def update(phase, attempt):
        print(json.dumps({'phase': phase, 'attempt': attempt}), flush=True)
    result = compress(args.source, args.output, update=update)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'output': result['output'], 'bytes': result['bytes'],
                      'audio_packets_identical': result['audio_packets_identical']}), flush=True)


if __name__ == '__main__':
    main()
