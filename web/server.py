"""Project-scoped LAN UI and one-at-a-time video processing queue."""
from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import mimetypes
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import socket
import subprocess
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlsplit
import uuid
import wave
import urllib.request

from pipeline import DEFAULT_TTS_PROFILE, DEFAULT_TTS_GROUPING, DEFAULT_MASTERING_POLICY
from source_language import normalize_language, source_language_metadata

ID = re.compile(r'^web_\d{8}_\d{6}_[a-f0-9]{8}$')
VIDEO_EXT = {'.mp4', '.mov', '.m4v', '.mkv', '.webm', '.avi', '.ts', '.mpeg', '.mpg', '.flv', '.wmv'}
ACTIVE = {'queued', 'running'}
MAX_UPLOAD = 1024 ** 3
RESERVE = 8 * 1024 ** 3
VERSION = 'philos_lan_web_v1'


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.pending')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temp.chmod(0o600)
    temp.replace(path)


def safe_message(error):
    message = str(error).splitlines()[0][:250]
    if re.search(r'(?i)bearer|api.?key|secret|token|password|authorization|sk-[a-z0-9]|/Users/|/home/|traceback', message):
        return '处理未完成，详细记录已保存在本机。可以重新提交素材。'
    return message if re.search('[\u3400-\u9fff]', message) else '处理未完成，详细记录已保存在本机。'


def validate_source_url(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 4096:
        raise ValueError('请填写有效的视频网址。')
    url = urlsplit(value.strip())
    if url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password:
        raise ValueError('请使用公开的视频网页链接。')
    try:
        port = url.port or (443 if url.scheme == 'https' else 80)
        addresses = socket.getaddrinfo(url.hostname, port, type=socket.SOCK_STREAM)
    except (OSError, ValueError):
        raise ValueError('无法找到这个视频网站，请检查链接。') from None
    # The browser submission endpoint cannot be used to probe private services.
    if not addresses or any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses):
        raise ValueError('网址入口仅接受公开视频网站；本地视频请使用上传。')
    return url.geturl()


class Cancelled(RuntimeError):
    pass


class JobManager:
    def __init__(self, root, start_worker=True):
        self.root = Path(root).resolve()
        self.state = self.root / 'web/state/jobs'
        self.state.mkdir(parents=True, exist_ok=True)
        self.state.parent.chmod(0o700)
        self.lock = threading.RLock()
        self.jobs = {}
        self.pending = deque()
        self.condition = threading.Condition(self.lock)
        self.cancel_events = {}
        self.processes = {}
        self.stopping = threading.Event()
        self.upload_slots = threading.BoundedSemaphore(1)
        self.thread = None
        self.proxy_checked_at = 0
        self.proxy_ok = False
        self.proxy_model = ''
        for path in sorted(self.state.glob('web_*.json')):
            try:
                job = json.loads(path.read_text())
                if not ID.fullmatch(job['id']):
                    continue
                if job['status'] == 'running':
                    job.update(status='failed', stage='failed', error='上次服务已中断，请重新提交素材。', updated_at=now())
                    atomic_json(path, job)
                if job['status'] == 'queued':
                    self.pending.append(job['id'])
                self.jobs[job['id']] = job
                self.cancel_events[job['id']] = threading.Event()
            except (ValueError, KeyError, OSError):
                continue
        if start_worker:
            self.thread = threading.Thread(target=self.worker, name='single-video-worker', daemon=True)
            self.thread.start()

    def public(self, job):
        keys = ('id', 'title', 'status', 'stage', 'progress', 'created_at', 'updated_at', 'error', 'warnings', 'metrics', 'duration_s', 'review_status', 'source_language', 'source_language_label', 'episode_count', 'episodes')
        result = {k: job[k] for k in keys if k in job}
        result['artifacts'] = [
            {'label': item['label'], 'kind': item.get('kind', 'file'),
             'url': '/api/jobs/' + job['id'] + '/artifacts/' + str(i) + '?v=' + hashlib.sha256(
                 (str(item.get('kind', 'file')) + '\0' + str(item['path']) + '\0' + str(item.get('sha256', ''))).encode()
             ).hexdigest()[:16]}
            for i, item in enumerate(job.get('artifacts', []))
        ]
        return result

    def save(self, job):
        atomic_json(self.state / (job['id'] + '.json'), job)

    def new_id(self):
        return datetime.now().strftime('web_%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:8]

    def add(self, *, input_path=None, source_url=None, title='', source_sha256=None, job_id=None, source_language='auto'):
        source_language = normalize_language(source_language)
        with self.condition:
            if sum(x['status'] in ACTIVE for x in self.jobs.values()) >= 3:
                raise ValueError('当前有3个任务在等待或处理中，请稍后提交。')
            job_id = job_id or self.new_id()
            if not ID.fullmatch(job_id) or job_id in self.jobs:
                raise ValueError('任务编号重复，请重新提交。')
            job = {'id': job_id, 'title': str(title).strip()[:120] or '视频译配',
                   'status': 'queued', 'stage': 'queued', 'progress': 0,
                   'created_at': now(), 'updated_at': now(), 'artifacts': [],
                   'options': {'max_output_height': 540, 'preserve_orientation': True,
                               'experimental_profile': DEFAULT_TTS_PROFILE, 'emotion_mode': 'reference',
                               'tts_grouping': DEFAULT_TTS_GROUPING,
                               'mastering_policy': DEFAULT_MASTERING_POLICY,
                               'voice_pilot_groups': 2,
                               'source_language': source_language, 'target_language': 'zh',
                               'episode_max_seconds': 1800, 'split_long_source': True,
                               'subtitles': 'zh_above_source', 'human_reviewed': False},
                   **source_language_metadata(source_language),
                   'review_status': '机器审稿预览，仍需核对专名与听感'}
            if input_path:
                path = Path(input_path).resolve()
                if not path.is_relative_to(self.root / 'inputs/web') or not path.is_file():
                    raise ValueError('上传的视频未完整接收，请重新上传。')
                job.update(input_path=str(path), source_sha256=source_sha256)
            elif source_url:
                job['source_url'] = source_url
            else:
                raise ValueError('请先选择视频或填写链接。')
            self.save(job)
            self.jobs[job_id] = job
            self.cancel_events[job_id] = threading.Event()
            self.pending.append(job_id)
            self.condition.notify()
            return self.public(job)

    def _pilot_file(self, job_id, value, expected_sha, *, receipt=False):
        """Only direct regular, SHA-bound files in this job's private pilot tree."""
        if (not isinstance(value, str) or not isinstance(expected_sha, str)
                or not re.fullmatch(r'[0-9a-f]{64}', expected_sha)):
            raise ValueError('试听文件校验格式无效。')
        path = Path(value)
        folder = self.root / 'jobs' / job_id / 'outputs' / 'voice_pilot'
        if not path.is_absolute() or '..' in path.parts or not path.is_relative_to(folder):
            raise ValueError('试听文件必须位于当前任务的试听目录。')
        # Reject all links, including directory links. The publisher copies raw
        # WAVs here; it must not expose a native cache or other private file.
        current = path
        while current != self.root:
            if current.is_symlink():
                raise ValueError('试听文件不能通过符号链接发布。')
            current = current.parent
        actual = path.resolve(strict=True)
        if not actual.is_relative_to(folder) or not actual.is_file():
            raise ValueError('试听文件位置无效。')
        if receipt:
            if actual != folder / 'pilot_receipt.json' or actual.stat().st_size > 1024 ** 2:
                raise ValueError('试听发布记录位置或大小无效。')
        elif actual.suffix.lower() != '.wav' or not 44 <= actual.stat().st_size <= 64 * 1024 ** 2:
            raise ValueError('试听只能发布完整 WAV 音频。')
        digest = hashlib.sha256()
        with actual.open('rb') as source:
            for block in iter(lambda: source.read(1024 ** 2), b''):
                digest.update(block)
        if digest.hexdigest() != expected_sha:
            raise ValueError('试听文件已改变，校验未通过。')
        if not receipt:
            try:
                with wave.open(str(actual), 'rb') as audio:
                    frames, channels, width = audio.getnframes(), audio.getnchannels(), audio.getsampwidth()
                    if (frames < 1 or channels not in (1, 2) or width not in (1, 2, 3, 4)
                            or not 8000 <= audio.getframerate() <= 192000
                            or audio.getcomptype() != 'NONE'
                            or len(audio.readframes(frames + 1)) != frames * channels * width):
                        raise ValueError('试听 WAV 不完整。')
            except (wave.Error, EOFError) as error:
                raise ValueError('试听 WAV 格式无效。') from error
        return actual

    def _voice_pilot(self, job, payload):
        options = job.get('options', {})
        limit = options.get('voice_pilot_groups', 0) if isinstance(options, dict) else None
        if (job.get('status') != 'running' or type(limit) is not int or limit != 2
                or self.cancel_events[job['id']].is_set()):
            raise ValueError('当前任务未启用运行中试听。')
        required = {'schema', 'artifacts', 'receipt_path', 'receipt_sha256', 'complete_job', 'human_reviewed'}
        if (not isinstance(payload, dict) or set(payload) != required
                or payload['schema'] != 'early_voice_pilot_v1'
                or payload['complete_job'] is not False or payload['human_reviewed'] is not False
                or not isinstance(payload['artifacts'], list) or not 1 <= len(payload['artifacts']) <= limit):
            raise ValueError('试听发布格式无效。')
        receipt_path = self._pilot_file(job['id'], payload['receipt_path'], payload['receipt_sha256'], receipt=True)
        receipt_bytes = receipt_path.read_bytes()
        if hashlib.sha256(receipt_bytes).hexdigest() != payload['receipt_sha256']:
            raise ValueError('试听发布记录在读取时已改变。')
        receipt = json.loads(receipt_bytes)
        if (not isinstance(receipt, dict) or receipt.get('job_id') != job['id']
                or any(receipt.get(k) != payload[k] for k in ('schema', 'artifacts'))
                or receipt.get('complete_job') is not False or receipt.get('human_reviewed') is not False):
            raise ValueError('试听发布与当前任务记录不一致。')
        artifacts = []
        for item in payload['artifacts']:
            if (not isinstance(item, dict) or set(item) != {'label', 'path', 'kind', 'sha256'}
                    or item['kind'] != 'audio' or not isinstance(item['label'], str)
                    or not item['label'].strip() or len(item['label']) > 120
                    or re.search(r'(?i)[/\\\r\n\x00]|bearer|api.?key|secret|password|authorization|token', item['label'])):
                raise ValueError('试听只能发布带有效标签的音频。')
            path = self._pilot_file(job['id'], item['path'], item['sha256'])
            artifacts.append({'label': item['label'], 'path': str(path), 'kind': 'audio',
                              'sha256': item['sha256'], 'voice_pilot': True})
        if len({item['path'] for item in artifacts}) != len(artifacts):
            raise ValueError('试听音频不能重复发布。')
        attempt = len(job.get('attempts', []))
        previous = job.get('_voice_pilot_publication', {})
        if previous.get('attempt') == attempt:
            old = previous['artifacts']
            if len(artifacts) < len(old) or artifacts[:len(old)] != old:
                raise ValueError('试听只能按相同顺序增加完整音频。')
        publication = {'attempt': attempt, 'artifacts': artifacts, 'receipt_sha256': payload['receipt_sha256']}
        summary = {'voice_pilot': {'schema': 'early_voice_pilot_v1', 'ready_groups': len(artifacts),
                                  'complete_job': False, 'human_reviewed': False}}
        return artifacts, publication, summary

    def update(self, job_id, stage, progress, details=None):
        with self.lock:
            job = self.jobs[job_id]
            preview = None
            if isinstance(details, dict) and 'voice_pilot' in details:
                # Validate everything before mutating the persisted job. A pilot
                # is an early artifact publication, never a completion signal.
                preview = self._voice_pilot(job, details['voice_pilot'])
                if not math.isfinite(float(progress)):
                    raise ValueError('试听进度无效。')
            job.update(stage=str(stage)[:100], progress=max(job.get('progress', 0), min(99, float(progress))), updated_at=now())
            if preview:
                artifacts, publication, summary = preview
                job.update(artifacts=artifacts, _voice_pilot_publication=publication, stage_details=summary,
                           progress=min(99, job['progress']))
            elif details:
                job['stage_details'] = details
            self.save(job)

    def cancel(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job['status'] in ACTIVE:
                self.cancel_events[job_id].set()
                if job['status'] == 'queued':
                    job.update(status='canceled', stage='canceled', updated_at=now())
                else:
                    job['stage'] = '正在停止当前步骤'
                for process in tuple(self.processes.get(job_id, ())):
                    # A finished group leader can leave live descendants.
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                self.save(job)
            return self.public(job)

    def retry(self, job_id):
        with self.condition:
            job = self.jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job['status'] not in {'failed', 'canceled'}:
                raise ValueError('当前任务无需重试，请等待处理结束。')
            if sum(x['status'] in ACTIVE for x in self.jobs.values()) >= 3:
                raise ValueError('当前任务较多，请稍后继续。')
            if job.get('input_path') and not Path(job['input_path']).is_file():
                raise ValueError('原视频已不在本机，请重新上传。')
            job.setdefault('attempts', []).append({'ended_at': job.get('updated_at'),
                                                  'status': job['status'], 'error': job.get('error')})
            job.update(status='queued', stage='queued', progress=0, error=None, updated_at=now())
            self.cancel_events[job_id] = threading.Event()
            self.save(job)
            self.pending.append(job_id)
            self.condition.notify()
            return self.public(job)

    def command(self, job_id, argv, cwd=None, env=None, on_line=None, timeout=None, should_abort=None):
        if self.cancel_events[job_id].is_set():
            raise Cancelled('任务已取消')
        if should_abort and should_abort():
            raise RuntimeError('并行分析另一项失败，未启动当前步骤')
        if not isinstance(argv, (list, tuple)) or not argv:
            raise ValueError('Invalid subprocess arguments')
        run_env = dict(os.environ)
        run_env.update({'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1',
                        'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
                        'OMP_NUM_THREADS': '2', 'MKL_NUM_THREADS': '2',
                        'PATH': str(self.root / 'tools') + ':/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'})
        if env:
            run_env.update({str(k): str(v) for k, v in env.items()})
        log_dir = self.root / 'reports/web_app/jobs'
        log_dir.mkdir(parents=True, exist_ok=True)
        began = time.monotonic()
        process = subprocess.Popen([str(x) for x in argv], cwd=cwd, env=run_env,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=True, bufsize=0)
        with self.lock:
            self.processes.setdefault(job_id, set()).add(process)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        tail = deque(maxlen=1000)
        partial = b''
        termination_at = None
        timed_out = False
        aborted = False
        def emit(raw):
            line = raw.decode('utf-8', errors='replace').rstrip('\r')
            # Proxy credentials never enter child processes; still redact common formats.
            line = re.sub(r'(?i)(bearer\s+|api[_-]?key[=:]\s*)(\S+)', r'\1[redacted]', line)
            tail.append(line)
            log.write(line + '\n')
            log.flush()
            if on_line:
                on_line(line)
        try:
            with (log_dir / (job_id + '.log')).open('a') as log:
                os.chmod(log.name, 0o600)
                log.write('\n' + now() + ' · ' + Path(str(argv[0])).name + '\n')
                while True:
                    canceled = self.cancel_events[job_id].is_set() or self.stopping.is_set()
                    aborted = aborted or bool(should_abort and should_abort())
                    timed_out = timed_out or bool(timeout and time.monotonic() - began > timeout)
                    if canceled or timed_out or aborted:
                        if termination_at is None:
                            termination_at = time.monotonic()
                            try: os.killpg(process.pid, signal.SIGTERM)
                            except ProcessLookupError: pass
                        elif time.monotonic() - termination_at > 5:
                            try: os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError: pass
                    for key, _ in selector.select(.25):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        partial += chunk
                        while b'\n' in partial or b'\r' in partial:
                            split = min(x for x in (partial.find(b'\n'), partial.find(b'\r')) if x >= 0)
                            line, partial = partial[:split], partial[split + 1:]
                            if line: emit(line)
                        if len(partial) > 65536:
                            emit(partial[:65536]); partial = partial[65536:]
                    if process.poll() is not None and not selector.get_map():
                        break
                if partial:
                    emit(partial)
                process.wait()
        finally:
            selector.close()
            process.stdout.close()
            with self.lock:
                registered = self.processes.get(job_id)
                if registered is not None:
                    registered.discard(process)
                    if not registered:self.processes.pop(job_id, None)
            # Clean the owned process group even after its leader has exited;
            # inherited stdout or a surviving helper must not outlive the job.
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            process.wait()
        if self.cancel_events[job_id].is_set() or self.stopping.is_set():
            raise Cancelled('任务已取消')
        if timed_out:
            raise RuntimeError('当前步骤用时过长，已停止并保留任务。')
        if aborted:
            raise RuntimeError('并行分析另一项失败，已停止当前步骤')
        return {'returncode': process.returncode, 'stdout': '\n'.join(tail), 'elapsed_s': time.monotonic() - began}

    def worker(self):
        while not self.stopping.is_set():
            with self.condition:
                while not self.pending and not self.stopping.is_set():
                    self.condition.wait(timeout=1)
                if self.stopping.is_set():
                    return
                job_id = self.pending.popleft()
                job = self.jobs[job_id]
                if job['status'] != 'queued':
                    continue
                job.update(status='running', stage='准备素材', updated_at=now())
                self.save(job)
            try:
                from pipeline import run_job
                result = run_job(self.root, dict(job),
                                 lambda stage, progress, details=None: self.update(job_id, stage, progress, details),
                                 lambda argv, **kw: self.command(job_id, argv, **kw),
                                 self.cancel_events[job_id].is_set)
                if self.cancel_events[job_id].is_set():
                    raise Cancelled('任务已取消')
                artifacts = []
                for item in result.get('artifacts', []):
                    path = Path(item['path']).resolve()
                    if not path.is_relative_to(self.root / 'jobs' / job_id) or not path.is_file():
                        raise RuntimeError('导出文件位置异常，任务已保留。')
                    artifacts.append({'label': str(item['label']), 'path': str(path), 'kind': item.get('kind', 'file')})
                    if re.fullmatch(r'[0-9a-f]{64}', str(item.get('sha256', ''))):
                        artifacts[-1]['sha256'] = item['sha256']
                if not artifacts or not any(x['kind'] == 'video' for x in artifacts):
                    raise RuntimeError('尚未生成可预览的视频，任务已保留。')
                with self.lock:
                    if self.cancel_events[job_id].is_set():
                        raise Cancelled('任务已取消')
                    job.update(status='completed', stage='completed', progress=100,
                               artifacts=artifacts, updated_at=now(),
                               metrics=result.get('metrics', {}), duration_s=result.get('duration_s'),
                               warnings=result.get('warnings', []))
                    for field in ('source_language', 'source_language_label', 'episode_count', 'episodes'):
                        if field in result:job[field] = result[field]
                    self.save(job)
            except Exception as error:
                with self.lock:
                    canceled = isinstance(error, Cancelled) or self.cancel_events[job_id].is_set()
                    job.update(status='canceled' if canceled else 'failed',
                               stage='canceled' if canceled else 'failed',
                               error='任务已取消' if canceled else safe_message(error), updated_at=now())
                    self.save(job)
                report = self.root / 'reports/web_app/jobs' / (job_id + '.error.json')
                # Keep diagnostic paths only in the local, private report, never the API.
                atomic_json(report, {'at': now(), 'type': type(error).__name__, 'message': safe_message(error)})

    def health(self):
        required = ['envs/asr/bin/python', 'vendor/mlx-indextts2-inference/.venv/bin/python',
                    'models/indextts-2.5-mlx-8bit/manifest.json', 'scripts/mlx_tts.py', 'web/pipeline.py', 'config/web_proxy.json']
        paths = {x: (self.root / x).exists() for x in required}
        # manifest naming differs across conversion revisions; use the actual runner manifest.
        paths['models/indextts-2.5-mlx-8bit/manifest.json'] = (self.root / 'models/indextts-2.5-mlx-8bit/model_manifest.json').is_file()
        configured = all(paths.values())
        proxy_configured = paths['config/web_proxy.json']
        return {'version': VERSION, 'ready': configured and self.proxy_ok and bool(self.thread and self.thread.is_alive()),
                'configured': configured, 'worker': {'ready': bool(self.thread and self.thread.is_alive()), 'concurrency': 1},
                'llm': {'configured': proxy_configured, 'ready': self.proxy_ok, 'model': self.proxy_model,
                        'provider': 'application_dedicated_proxy', 'text_only': True},
                'compute': {'local': ['音频识别', '背景分离', '说话人分组', '中文配音', '混音', '字幕和视频导出'],
                            'cloud_via_proxy': ['翻译', '口语化', '机器复核']},
                'queue': sum(j['status'] == 'queued' for j in self.jobs.values()),
                'limits': {'upload_bytes': MAX_UPLOAD, 'episode_seconds': 1800,
                           'split_long_source': True, 'source_language_auto': True,
                           'output_video_bytes': 50000000, 'reserved_gib': 8},
                'free_gib': round(shutil.disk_usage(self.root).free / 1024 ** 3, 2),
                'quality': '自动生成预览；吸气声仍在优化，需核对专名和听感'}

    def check_proxy(self):
        if time.monotonic() - self.proxy_checked_at < 30:
            return
        self.proxy_checked_at = time.monotonic()
        try:
            config = json.loads((self.root / 'config/web_proxy.json').read_text())
            request = urllib.request.Request(config['base_url'].rstrip('/') + '/models',
                                             headers={'Authorization': 'Bearer ' + config['api_key']})
            with urllib.request.urlopen(request, timeout=3) as response:
                available = {item['id'] for item in json.load(response).get('data', [])}
            self.proxy_model = str(config.get('model', ''))
            self.proxy_ok = self.proxy_model in available
        except Exception:
            self.proxy_ok = False


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'PhilosDubbing/1'
    sys_version = ''

    def log_message(self, fmt, *args):
        # Do not log user URLs, filenames or request payloads to a shared console.
        pass

    @property
    def manager(self):
        return self.server.manager

    def valid_request(self, mutate=False):
        try:
            host = urlsplit('http://' + self.headers.get('Host', '')).hostname
        except ValueError:
            host = None
        if host not in self.server.allowed_hosts:
            self.json_response(403, {'detail': '请通过本机或指定局域网地址访问。'}, close=True)
            return False
        if mutate:
            origin = self.headers.get('Origin')
            if origin and origin != 'http://' + self.headers.get('Host'):
                self.json_response(403, {'detail': '请从配音应用页面提交任务。'}, close=True)
                return False
            if self.headers.get('Sec-Fetch-Site') == 'cross-site':
                self.json_response(403, {'detail': '请从配音应用页面提交任务。'}, close=True)
                return False
            if self.headers.get('Transfer-Encoding'):
                self.json_response(400, {'detail': '不支持此上传方式。'}, close=True)
                return False
        return True

    def security_headers(self):
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'same-origin')
        self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")

    def json_response(self, code, value, close=False):
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.security_headers()
        if close:
            self.send_header('Connection', 'close')
            self.close_connection = True
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)

    def serve_file(self, path, label=None):
        path = Path(path)
        try:
            size = path.stat().st_size
            handle = path.open('rb')
        except OSError:
            return self.json_response(404, {'detail': '文件暂时不可用。'})
        start, end, partial = 0, size - 1, False
        raw = self.headers.get('Range')
        if raw:
            match = re.fullmatch(r'bytes=(\d*)-(\d*)', raw)
            if not match or (not match[1] and not match[2]):
                handle.close()
                return self.json_response(416, {'detail': '文件读取范围无效。'})
            if match[1]:
                start = int(match[1]); end = min(int(match[2]) if match[2] else end, end)
            else:
                start = max(0, size - int(match[2]))
            if start >= size or start > end:
                handle.close()
                self.send_response(416)
                self.send_header('Content-Range', 'bytes */' + str(size))
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            partial = True
        self.send_response(206 if partial else 200)
        mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        if path.suffix == '.js': mime = 'text/javascript; charset=utf-8'
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(max(0, end - start + 1)))
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Cache-Control', 'no-store')
        if partial:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        if label:
            self.send_header('Content-Disposition', "inline; filename*=UTF-8''" + quote(path.name))
        self.security_headers()
        self.end_headers()
        try:
            if self.command != 'HEAD':
                handle.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = handle.read(min(256 * 1024, remaining))
                    if not chunk: break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            handle.close()

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self.valid_request():
            return
        path = urlsplit(self.path).path
        static = {'/': 'index.html', '/static/app.js': 'app.js', '/static/style.css': 'style.css'}
        if path in static:
            return self.serve_file(self.server.static_root / static[path])
        if path == '/api/health':
            self.manager.check_proxy()
            with self.manager.lock:
                snapshot = self.manager.health()
            return self.json_response(200, snapshot)
        if path == '/api/jobs':
            with self.manager.lock:
                jobs = sorted(self.manager.jobs.values(), key=lambda x: x['created_at'], reverse=True)
                snapshot = {'jobs': [self.manager.public(x) for x in jobs[:100]]}
            return self.json_response(200, snapshot)
        match = re.fullmatch(r'/api/jobs/([^/]+)(?:/artifacts/(\d+))?', path)
        if match and ID.fullmatch(match[1]):
            with self.manager.lock:
                job = self.manager.jobs.get(match[1])
                if job is not None:
                    if match[2] is None:
                        snapshot = self.manager.public(job)
                        selected = None
                    else:
                        snapshot = None
                        index = int(match[2])
                        artifacts = job.get('artifacts', [])
                        if index < len(artifacts):
                            item = artifacts[index]
                            file = Path(item['path']).resolve()
                            if item.get('voice_pilot') is True:
                                try:
                                    file = self.manager._pilot_file(job['id'], item['path'], item['sha256'])
                                    selected = (file, item.get('label'))
                                except (OSError, ValueError, KeyError):
                                    selected = None
                            elif file.is_relative_to(self.manager.root / 'jobs' / job['id']) and file.is_file():
                                selected = (file, item.get('label'))
                            else: selected = None
                        else: selected = None
                else:
                    snapshot = selected = None
            if snapshot is not None:
                return self.json_response(200, snapshot)
            if selected:
                return self.serve_file(*selected)
        self.json_response(404, {'detail': '找不到这个任务或文件。'})

    def do_POST(self):
        if not self.valid_request(mutate=True):
            return
        url = urlsplit(self.path)
        if url.path == '/api/jobs/upload':
            return self.upload(parse_qs(url.query))
        if url.path == '/api/jobs':
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 1 <= length <= 16384:
                    return self.json_response(400, {'detail': '视频链接提交格式无效。'}, close=True)
                body = json.loads(self.rfile.read(length))
                source_url = validate_source_url(body.get('source_url'))
                if shutil.disk_usage(self.manager.root).free < RESERVE + 512 * 1024 ** 2:
                    return self.json_response(507, {'detail': '空间不足，已保留8GB供系统运行。'})
                result = self.manager.add(source_url=source_url, title=body.get('title', ''),
                                          source_language=body.get('source_language', 'auto'))
                return self.json_response(201, result)
            except (ValueError, AttributeError, TypeError) as error:
                return self.json_response(400, {'detail': safe_message(error)})
        match = re.fullmatch(r'/api/jobs/([^/]+)/(cancel|retry)', url.path)
        if match and ID.fullmatch(match[1]):
            try:
                operation = self.manager.cancel if match[2] == 'cancel' else self.manager.retry
                result = operation(match[1])
                return self.json_response(200, result, close=True)
            except KeyError:
                return self.json_response(404, {'detail': '找不到这个任务。'}, close=True)
            except ValueError as error:
                return self.json_response(400, {'detail': safe_message(error)}, close=True)
        return self.json_response(404, {'detail': '找不到这个操作。'}, close=True)

    def upload(self, query):
        try:
            size = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            size = 0
        if size <= 0 or size > MAX_UPLOAD:
            return self.json_response(413 if size > MAX_UPLOAD else 400, {'detail': '请选择不超过1GB的视频。'}, close=True)
        if not self.manager.upload_slots.acquire(blocking=False):
            return self.json_response(429, {'detail': '正在接收另一个视频，请稍后重试。'}, close=True)
        part = target = folder = None
        registered = False
        try:
            if shutil.disk_usage(self.manager.root).free < RESERVE + size + 512 * 1024 ** 2:
                return self.json_response(507, {'detail': '空间不足以接收这个视频，已保留8GB供系统运行。'}, close=True)
            with self.manager.lock:
                if sum(x['status'] in ACTIVE for x in self.manager.jobs.values()) >= 3:
                    return self.json_response(429, {'detail': '当前任务较多，请稍后提交。'}, close=True)
            source_language = normalize_language(query.get('source_language', ['auto'])[0])
            name = Path(str(query.get('name', ['video.mp4'])[0]).replace('\\', '/')).name
            extension = Path(name).suffix.lower()
            if extension not in VIDEO_EXT:
                return self.json_response(400, {'detail': '请选择常见格式的视频文件。'}, close=True)
            job_id = self.manager.new_id()
            folder = self.manager.root / 'inputs/web' / job_id
            folder.mkdir(parents=True, mode=0o700)
            part = folder / 'source.part'
            target = folder / ('source' + extension)
            sha = hashlib.sha256()
            remaining = size
            self.connection.settimeout(90)
            with part.open('xb') as output:
                while remaining:
                    chunk = self.rfile.read(min(1024 ** 2, remaining))
                    if not chunk:
                        raise ValueError('视频上传中断，请重新提交。')
                    sha.update(chunk); output.write(chunk); remaining -= len(chunk)
            part.replace(target)
            result = self.manager.add(input_path=target, source_sha256=sha.hexdigest(), title=Path(name).stem, job_id=job_id,
                                      source_language=source_language)
            registered = True
            return self.json_response(201, result)
        except (OSError, ValueError) as error:
            if part and part.exists(): part.unlink()
            if not registered and target and target.exists(): target.unlink()
            if not registered and folder and folder.exists() and not any(folder.iterdir()): folder.rmdir()
            return self.json_response(400, {'detail': safe_message(error)}, close=True)
        finally:
            self.manager.upload_slots.release()


def make_server(root, host='0.0.0.0', port=8766, start_worker=True, static_root=None, lan_host=None):
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.manager = JobManager(root, start_worker=start_worker)
    server.static_root = Path(static_root or Path(__file__).parent / 'static').resolve()
    server.allowed_hosts = {'127.0.0.1', 'localhost', lan_host or os.environ.get('PHILOS_LAN_HOST', 'localhost')}
    return server


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--host', default='0.0.0.0')
    p.add_argument('--port', type=int, default=8766)
    p.add_argument('--lan-host', default=os.environ.get('PHILOS_LAN_HOST', 'localhost'))
    a = p.parse_args()
    os.umask(0o077)
    server = make_server(a.root, a.host, a.port, lan_host=a.lan_host)
    def stop(signum, frame):
        server.manager.stopping.set()
        with server.manager.condition:
            server.manager.condition.notify_all()
        for job_id, job in list(server.manager.jobs.items()):
            if job['status'] == 'running': server.manager.cancel(job_id)
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(json.dumps({'service': VERSION, 'listen': a.host, 'port': a.port, 'started_at': now()}), flush=True)
    try:
        server.serve_forever(poll_interval=.5)
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
