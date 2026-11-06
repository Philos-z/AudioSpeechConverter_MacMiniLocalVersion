"""Project-local supervisor; explicit stop is durable and never login autostart.

The supervisor signals only its own unreaped Popen child. Public PID files are
informational and are never used as permission to signal an arbitrary process.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import urllib.request
import uuid

from task_takeover import InstanceLock, read_json, runtime, stamp, write_json, progress_marker, children_quiet

_OWNED_SUPERVISORS = {}  # Direct Popen handles for callers staying in Python.


def status(root=None, port=8766):
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=3) as response:
            value = json.load(response)
        if value.get('version') != 'philos_lan_web_v1': return None
        if root is not None and value.get('project_identity') not in (
                None, hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()):
            return None
        return value
    except Exception:
        return None


def _held(path):
    try:
        with InstanceLock(path): pass
        return False
    except BlockingIOError:
        return True


def _desired(root, generation):
    try:
        value = read_json(runtime(root) / 'control.json', {})
        return value.get('desired') == 'running' and value.get('generation') == generation
    except (ValueError, OSError):
        return False


def _progress(root):
    values = []
    for path in sorted((Path(root) / 'web/state/jobs').glob('web_*.json')):
        try:
            job = read_json(path, {})
            values.append((job['id'], progress_marker(root, job)))
        except (ValueError, OSError, KeyError, TypeError):
            values.append((path.name, 'invalid'))
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def _alive(root, port):
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/liveness', timeout=2) as response:
            value = json.load(response)
        return (value.get('version') == 'philos_lan_web_v1'
            and value.get('project_identity') == hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()
            and value.get('worker_alive') is True and value.get('stopping') is False)
    except (OSError, ValueError): return False


def supervise(root, generation, *, port=8766, host='0.0.0.0', command=None, poll=.2, backoff_scale=1):
    """Bounded real-process loop, injectable only by local test code."""
    root = Path(root).resolve()
    directory = runtime(root)
    if not isinstance(generation, str) or not re.fullmatch(r'[a-f0-9]{32}', generation):
        raise ValueError('服务启动身份无效。')
    with InstanceLock(directory / 'supervisor.lock'):
        process = None
        stopped = False
        journal_path = directory / ('restarts_' + generation + '.json')
        journal = read_json(journal_path, {'schema': 'service_supervision_v1', 'generation': generation, 'events': []})
        events = journal.get('events') if isinstance(journal, dict) else None
        if (not isinstance(journal, dict) or journal.get('schema') != 'service_supervision_v1'
                or journal.get('generation') != generation or not isinstance(events, list) or len(events) > 6
                or any(not isinstance(event, dict) or type(event.get('number')) is not int
                    or event['number'] != index + 1 or type(event.get('returncode')) is not int
                    or not re.fullmatch(r'[a-f0-9]{64}', str(event.get('progress_marker', '')))
                    or not isinstance(event.get('at'), str)
                    for index, event in enumerate(events))):
            raise ValueError('服务重启历史无效，未重置记录。')
        def halt(signum, frame):
            nonlocal stopped
            stopped = True
            # A direct supervisor stop is an explicit stop too. It cannot be
            # mistaken for permission to launch a new service generation.
            if _desired(root, generation):
                write_json(directory / 'control.json', {'desired': 'stopped', 'generation': generation, 'at': stamp()})
        old_signals = {}
        for sig in (signal.SIGTERM, signal.SIGINT):
            old_signals[sig] = signal.signal(sig, halt)
        python = root / 'envs/asr/bin/python'
        default = [str(python if python.is_file() else Path(sys.executable)), '-B',
                   str(Path(__file__).parent / 'server.py'), '--root', str(root),
                   '--host', host, '--port', str(port), '--control-generation', generation]
        try:
            while not stopped and _desired(root, generation):
                marker = _progress(root)
                same = sum(event.get('progress_marker') == marker for event in events)
                if len(events) >= 6 or same >= 2:
                    journal.update(state='needs_attention', reason='服务连续中断或重启已达上限，已停止反复启动。')
                    write_json(journal_path, journal)
                    break
                if events:
                    retry_at = time.time() + min(120, 2 ** same * 2) * backoff_scale
                    journal.update(state='backoff', retry_at_unix=retry_at)
                    write_json(journal_path, journal)
                    while time.time() < retry_at and not stopped and _desired(root, generation): time.sleep(poll)
                    if stopped or not _desired(root, generation): break
                # If another valid server still owns the lock, do not launch a
                # competing server. It may be finishing its graceful shutdown.
                if _held(directory / 'server.lock'):
                    journal.update(state='waiting_for_previous_service')
                    write_json(journal_path, journal)
                    time.sleep(poll)
                    continue
                report = root / 'reports/web_app'
                report.mkdir(parents=True, exist_ok=True)
                with (report / 'server.stdout.log').open('ab') as out, (report / 'server.stderr.log').open('ab') as err:
                    process = subprocess.Popen(command or default, cwd=root, stdin=subprocess.DEVNULL,
                        stdout=out, stderr=err, start_new_session=True)
                # Kept for legacy operators only; no signalling relies on it.
                (report / 'server.pid').write_text(str(process.pid) + '\n')
                journal.update(state='running', child_pid=process.pid)
                write_json(journal_path, journal)
                next_probe, failed_probes = time.monotonic() + 15, 0
                while process.poll() is None and not stopped and _desired(root, generation):
                    time.sleep(poll)
                    # Tests inject a tiny non-HTTP process. Production watches
                    # only a local liveness endpoint; no Proxy/model/API call.
                    if command is None and time.monotonic() >= next_probe:
                        next_probe = time.monotonic() + 5
                        failed_probes = 0 if _alive(root, port) else failed_probes + 1
                        if failed_probes >= 3:
                            journal.update(state='worker_unresponsive')
                            write_json(journal_path, journal)
                            break
                if process.poll() is None:
                    process.terminate()  # Our own direct child is still unreaped.
                    try: process.wait(timeout=12)
                    except subprocess.TimeoutExpired:
                        journal.update(state='stopping', reason='服务仍在安全停止中，未强制启动新进程。')
                        write_json(journal_path, journal)
                        process.wait()
                code = process.wait()
                process = None
                if stopped or not _desired(root, generation): break
                events.append({'number': len(events) + 1, 'at': stamp(), 'returncode': code, 'progress_marker': marker})
                journal['events'] = events
                write_json(journal_path, journal)
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait()
            if not _desired(root, generation): journal['state'] = 'stopped'
            write_json(journal_path, journal)
            for sig, handler in old_signals.items(): signal.signal(sig, handler)
    return journal


def stop(root, *, port=8766, timeout=20):
    root = Path(root).resolve()
    directory = runtime(root)
    with InstanceLock(directory / 'control.lock', blocking=True):
        control = read_json(directory / 'control.json', {})
        write_json(directory / 'control.json', {'desired': 'stopped',
            'generation': control.get('generation'), 'at': stamp()})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (not _held(directory / 'supervisor.lock') and not _held(directory / 'server.lock')
                and children_quiet(root)):
            if status(root, port):
                raise RuntimeError('当前为旧版未受管理的服务；需先用原控制器安全停止，不能按旧PID猜测终止。')
            owned = _OWNED_SUPERVISORS.pop((str(root), port), None)
            if owned is not None: owned.wait(timeout=2)
            return
        time.sleep(.1)
    raise RuntimeError('服务正在安全停止，已有成果保留；不会强行启动另一份服务。')


def start(root, *, port=8766, host='0.0.0.0'):
    root = Path(root).resolve()
    directory = runtime(root)
    if _held(directory / 'supervisor.lock'):
        control = read_json(directory / 'control.json', {})
        if control.get('desired') == 'running': return
        raise RuntimeError('服务正在安全停止，请稍后再启动。')
    if status(root, port):
        raise RuntimeError('当前为未受本控制器管理的服务，需先安全停止后启用自动接管。')
    # Serialize concurrent start requests, including the gap before the new
    # supervisor has taken its lifetime lock.
    with InstanceLock(directory / 'control.lock', blocking=True):
        if _held(directory / 'supervisor.lock'): return
        generation = uuid.uuid4().hex
        write_json(directory / 'control.json', {'desired': 'running', 'generation': generation, 'at': stamp()})
        report = root / 'reports/web_app'
        report.mkdir(parents=True, exist_ok=True)
        python = root / 'envs/asr/bin/python'
        with (report / 'supervisor.stdout.log').open('ab') as out, (report / 'supervisor.stderr.log').open('ab') as err:
            process = subprocess.Popen([str(python if python.is_file() else Path(sys.executable)), '-B',
                str(Path(__file__).resolve()), 'supervise', '--root', str(root), '--generation', generation,
                '--port', str(port), '--host', host], cwd=root, stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True)
        _OWNED_SUPERVISORS[(str(root), port)] = process
        for _ in range(100):
            if _held(directory / 'supervisor.lock') and status(root, port): return
            if process.poll() is not None: raise RuntimeError('服务未能启动，请查看项目 reports/web_app 中的日志。')
            time.sleep(.1)
        raise RuntimeError('服务仍在准备，自动接管器已保留当前状态；请稍后检查。')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['start', 'stop', 'restart', 'status', 'supervise'])
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--generation')
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--host', default='0.0.0.0')
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == 'supervise':
        if not args.generation or not __import__('re').fullmatch(r'[a-f0-9]{32}', args.generation): raise SystemExit(2)
        supervise(root, args.generation, port=args.port, host=args.host)
        return
    if args.action in {'stop', 'restart'}: stop(root, port=args.port)
    if args.action in {'start', 'restart'}: start(root, port=args.port, host=args.host)
    current = status(root, args.port)
    print(json.dumps({'running': current is not None,
        'supervised': _held(runtime(root) / 'supervisor.lock'), 'login_autostart': False,
        'url': f'http://127.0.0.1:{args.port}/'}, ensure_ascii=False))


if __name__ == '__main__': main()
