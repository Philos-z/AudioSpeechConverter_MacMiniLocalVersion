"""Project-local, bounded crash takeover and child lifetime guards.

No PID-file signalling: kernel locks prove ownership. A command guardian owns
its process group and a parent pipe; EOF stops only that owned group. The lock
stays held until the group is stopped, including after an HTTP server crash.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import tempfile
import time

POLICY = 'bounded_restart_v1'
MAX_TAKEOVERS = 6
MAX_SAME_PROGRESS = 2
_SHA = re.compile(r'^[a-f0-9]{64}$')


def stamp():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def read_json(path, default=None):
    try:
        path = Path(path)
        if path.is_symlink() or path.stat().st_size > 16 * 1024 ** 2:
            raise ValueError('接管记录位置或大小无效')
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


class InstanceLock:
    """An open descriptor, never a PID, is the authority for this owner."""
    def __init__(self, path, *, shared=False, blocking=False):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        self.stream = os.fdopen(descriptor, 'a+')
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
                        | (0 if blocking else fcntl.LOCK_NB))
        except BaseException:
            self.stream.close()
            raise

    def close(self):
        if not self.stream.closed: self.stream.close()

    def __enter__(self): return self
    def __exit__(self, *args): self.close()


def runtime(root):
    path = Path(root).resolve() / 'web/state/service'
    path.mkdir(parents=True, exist_ok=True)
    return path


def children_quiet(root):
    try:
        with InstanceLock(runtime(root) / 'children.lock'): pass
        return True
    except BlockingIOError:
        return False


def _sha(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 ** 2), b''): value.update(block)
    return value.hexdigest()


def _request_path(root, job):
    jid = job.get('id')
    if not isinstance(jid, str) or not re.fullmatch(r'web_[A-Za-z0-9_-]{1,80}', jid):
        raise ValueError('下载任务身份无效。')
    return Path(root).resolve() / 'web/state/source_requests' / (jid + '.json')


def authorize_source_request(root, job):
    """Persist URL authority at submission, before any download can begin."""
    url = job.get('source_url')
    if not isinstance(url, str) or not url or job.get('input_path'):
        raise ValueError('首次下载请求不完整。')
    from source_acquisition import request_binding
    value = {'schema': 'authorized_source_request_v1', 'job': job['id'],
             'request': request_binding(url), 'at': stamp()}
    path = _request_path(root, job)
    if path.exists():
        if read_json(path) != value: raise ValueError('下载请求已存在，未覆盖原授权。')
    else: write_json(path, value)
    job.setdefault('takeover', {}).update(source_request_binding=value,
        phase='acquiring_source', source_ever_bound=False)


def _request_bound(root, job):
    state = job.get('takeover', {})
    request = state.get('source_request_binding')
    from source_acquisition import request_binding
    if (not isinstance(request, dict) or request.get('schema') != 'authorized_source_request_v1'
            or request.get('job') != job['id'] or request.get('request') != request_binding(job.get('source_url', ''))
            or read_json(_request_path(root, job)) != request or job.get('input_path')):
        raise ValueError('下载接管与首次授权请求不一致。')
    return request


def _acquiring_source(root, job):
    """A missing source binding can never be downgraded to a new download."""
    state = job.get('takeover', {})
    request = _request_bound(root, job)
    bound = _request_path(root, job).with_suffix('.bound.json')
    if (state.get('phase') != 'acquiring_source' or state.get('source_ever_bound') is not False
            or 'source_binding' in state or bound.exists() or job.get('source_sha256')):
        raise ValueError('已绑定过原片的任务不能退回首次下载。')
    # Any existing final receipt must still be valid; damaged completed files
    # are never silently ignored in favour of a fresh URL download.
    from source_acquisition import read_final_source
    read_final_source(root, job['id'], job['source_url'])
    return request


def _source_file(root, job):
    root = Path(root).resolve()
    if job.get('input_path'):
        candidates = [Path(job['input_path'])]
    else:
        from source_acquisition import read_final_source
        candidate = read_final_source(root, job['id'], job.get('source_url', ''))
        candidates = [candidate] if candidate is not None else []
    if len(candidates) != 1: raise ValueError('原片尚未完整保存，等待核对后继续。')
    path = candidates[0]
    if not path.is_absolute() or not path.is_relative_to(root / 'inputs'):
        raise ValueError('原片位置与本任务不一致。')
    current = path
    while current != root:
        if current.is_symlink(): raise ValueError('原片不能通过符号链接接管。')
        current = current.parent
    if not path.is_file(): raise ValueError('原片尚未完整保存，等待核对后继续。')
    allowed = {root / 'inputs/web' / job['id'], root / 'inputs' / job['id']}
    if not job.get('input_path'):
        from source_acquisition import directory
        allowed.add(directory(root, job['id'], job['source_url']))
    if path.parent not in allowed:
        raise ValueError('原片位置与本任务不一致。')
    return path


def bind_source(root, job):
    """Record once while the original authorised attempt is still alive."""
    if job.get('takeover', {}).get('source_binding'): return False
    if job.get('takeover', {}).get('source_request_binding'): _request_bound(root, job)
    path = _source_file(root, job)
    before = path.stat()
    digest = _sha(path)
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError('原片正在变化，尚未建立接管绑定。')
    expected = job.get('source_sha256')
    if expected is not None and expected != digest: raise ValueError('原片校验与上传记录不一致。')
    binding = {
        'path': str(path.relative_to(Path(root).resolve())), 'sha256': digest,
        'bytes': before.st_size, 'source_url': job.get('source_url'), 'at': stamp()}
    bound_path = _request_path(root, job).with_suffix('.bound.json')
    if bound_path.exists():
        old = read_json(bound_path)
        if not isinstance(old, dict) or any(old.get(k) != binding[k] for k in ('path','sha256','bytes','source_url')):
            raise ValueError('原片与首次保存的完整绑定不一致。')
        binding = old
    else: write_json(bound_path, binding)
    job.setdefault('takeover', {}).update(source_binding=binding, source_ever_bound=True, phase='bound_source')
    job['source_sha256'] = digest
    return True


def validate_source(root, job):
    path = _source_file(root, job)
    before = path.stat()
    value = job.get('takeover', {}).get('source_binding', {})
    if (not isinstance(value, dict) or not _SHA.fullmatch(str(value.get('sha256', '')))
            or value.get('path') != str(path.relative_to(Path(root).resolve()))
            or value.get('source_url') != job.get('source_url')
            or type(value.get('bytes')) is not int or value['bytes'] != path.stat().st_size
            or _sha(path) != value['sha256']
            or (job.get('source_sha256') and job['source_sha256'] != value['sha256'])):
        raise ValueError('原片身份校验未通过，已有成果保留，等待核对。')
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError('接管校验期间原片已变化，已有成果保留。')
    return value


def progress_marker(root, job):
    """Only durable output/content progress counts; timestamps do not."""
    folder = Path(root) / 'jobs' / job['id']
    ledger = read_json(folder / 'pipeline_receipts.json', {})
    checkpoint = read_json(folder / 'translation/checkpoint.json', {})
    progress = float(job.get('progress', 0))
    floor = float(job.get('takeover', {}).get('progress_floor', 0))
    if not all(math.isfinite(value) and 0 <= value <= 100 for value in (progress, floor)):
        raise ValueError('进度记录无效。')
    payload = {'high_water': max(progress, floor),
               'stages': ledger, 'translated': checkpoint.get('completed', 0)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _valid_journal(value):
    if not isinstance(value, list) or len(value) > MAX_TAKEOVERS: return False
    for index, entry in enumerate(value):
        if (not isinstance(entry, dict) or type(entry.get('number')) is not int or entry.get('number') != index + 1
                or not _SHA.fullmatch(str(entry.get('progress_marker', '')))
                or type(entry.get('retry_at_unix')) not in (int, float)
                or not math.isfinite(entry['retry_at_unix'])): return False
    return True


def prepare_takeover(root, job, *, timestamp=None):
    """Mutates only a running, policy-authorised, input-bound interrupted job.

    Existing text/media journals are validated and never reset or consumed by
    this scheduling action. Their own action limits still gate production.
    """
    if job.get('status') != 'running': return False
    state = job.setdefault('takeover', {})
    if not isinstance(state, dict):
        job.update(status='failed', stage='failed', error='自动接管记录无效，未重置记录。', updated_at=stamp())
        return False
    reason = None
    try:
        if job.get('cancel_requested_at'): raise ValueError('任务已由用户取消，不会自动重启。')
        if job.get('options', {}).get('restart_policy') != POLICY:
            raise ValueError('旧任务尚未启用自动接管，已有成果保留，可手动继续。')
        if job.get('options', {}).get('website_recovery', {}).get('enabled') is not True:
            raise ValueError('本任务未启用自动恢复。')
        acquiring = not state.get('source_binding') and bool(state.get('source_request_binding'))
        request_identity = _acquiring_source(root, job) if acquiring else None
        if not acquiring: validate_source(root, job)
        from website_recovery import _journal
        from media_recovery import journal
        _journal(Path(root) / 'jobs' / job['id'])
        journal(Path(root) / 'jobs' / job['id'])
        from failure_triage import retry_message
        blocked = retry_message(root, job)
        if blocked: raise ValueError('已保存的内容问题仍待核对，不会借服务重启绕过。')
        events = state.get('events', [])
        if not _valid_journal(events): raise ValueError('自动接管次数记录无效，未重置记录。')
        marker = progress_marker(root, job)
        if len(events) >= MAX_TAKEOVERS: raise ValueError('自动接管已达6次上限，已有成果保留。')
        same = sum(entry['progress_marker'] == marker for entry in events)
        if same >= MAX_SAME_PROGRESS: raise ValueError('连续中断未取得新进展，已停止反复启动。')
        timestamp = time.time() if timestamp is None else timestamp
        delay = min(120, 2 * 2 ** same)
        event = {'number': len(events) + 1, 'at': stamp(), 'progress_marker': marker,
                 'retry_at_unix': timestamp + delay, 'previous_stage': job.get('stage'),
                 'previous_error': job.get('error'),
                 'phase': 'acquiring_source' if acquiring else 'bound_source',
                 'source_sha256': None if acquiring else state['source_binding']['sha256']}
        if acquiring:
            event['source_request_binding'] = dict(request_identity)
        # Full pre-interruption state is immutable evidence, before queue changes.
        evidence = Path(root) / 'reports/web_app/takeovers' / job['id'] / (f"{len(events)+1:02d}.json")
        if evidence.exists():
            previous = read_json(evidence)
            saved_event = previous.get('proposed_event', {}) if isinstance(previous, dict) else {}
            if (not isinstance(previous, dict) or previous.get('schema') != 'interrupted_attempt_evidence_v1' or previous.get('job') != job
                    or saved_event.get('number') != len(events) + 1
                    or saved_event.get('progress_marker') != marker
                    or saved_event.get('source_sha256') != event['source_sha256']
                    or saved_event.get('phase') != event['phase']
                    or saved_event.get('source_request_binding') != event.get('source_request_binding')
                    or not _valid_journal([*events, saved_event])):
                raise ValueError('自动接管历史已存在且不匹配，未覆盖旧记录。')
            event = saved_event
        else:
            write_json(evidence, {'schema': 'interrupted_attempt_evidence_v1', 'at': stamp(),
                                  'job': job, 'proposed_event': event})
        events = [*events, event]
        state.update(events=events, state='waiting', retry_at_unix=event['retry_at_unix'],
                     progress_floor=max(float(job.get('progress', 0)), float(state.get('progress_floor', 0))))
        job.setdefault('attempts', []).append({'ended_at': job.get('updated_at'), 'status': 'interrupted',
            'stage': job.get('stage'), 'error': job.get('error'), 'takeover_number': len(events)})
        job.update(status='queued', stage='服务中断，校验通过后自动继续', error=None, updated_at=stamp(),
                   queue_reason='automatic_takeover')
        return True
    except (ValueError, OSError, TypeError, KeyError) as error:
        reason = str(error)
        if not re.search('[\u3400-\u9fff]', reason) or re.search(r'[/\\]|Traceback', reason):
            reason = '自动接管校验未完成，已有成果保留，等待核对本机记录。'
    state.update(state='needs_attention', reason=reason)
    job.update(status='canceled' if job.get('cancel_requested_at') else 'failed',
               stage='canceled' if job.get('cancel_requested_at') else 'failed',
               error=reason, updated_at=stamp())
    return False


def validate_queued_takeover(root, job):
    """Recheck restart authority at dispatch, not only at service boot.

    A queued task cannot become an initial submission by losing its source
    binding. Immutable interrupted-attempt evidence is independent of the
    mutable state file. Explicit manual retries remain separately recorded.
    """
    if job.get('status') != 'queued' or job.get('cancel_requested_at'):
        raise ValueError('已取消或不在队列中的任务不能接管。')
    state = job.get('takeover', {})
    if not isinstance(state, dict): raise ValueError('自动接管记录无效，未启动任务。')
    events = state.get('events', [])
    if not _valid_journal(events): raise ValueError('自动接管次数记录无效，未重置记录。')
    evidence_dir = Path(root) / 'reports/web_app/takeovers' / job['id']
    has_history = bool(events) or evidence_dir.exists()
    automatic = job.get('queue_reason') == 'automatic_takeover' or (
        has_history and job.get('queue_reason') != 'manual_retry')
    if automatic:
        if (job.get('options', {}).get('restart_policy') != POLICY
                or job.get('options', {}).get('website_recovery', {}).get('enabled') is not True):
            raise ValueError('自动接管授权已改变，已有成果保留。')
        if not events: raise ValueError('自动接管缺少次数记录，未启动任务。')
        latest = events[-1]
        acquiring = latest.get('phase') == 'acquiring_source'
        if acquiring:
            request_identity = _acquiring_source(root, job)
            if latest.get('source_request_binding') != request_identity:
                raise ValueError('排队下载与已授权请求不一致。')
        elif not state.get('source_binding'):
            raise ValueError('自动接管缺少原片身份，未启动任务。')
        evidence = read_json(evidence_dir / (f"{len(events):02d}.json"))
        if (not isinstance(evidence, dict) or evidence.get('schema') != 'interrupted_attempt_evidence_v1'
                or evidence.get('proposed_event') != latest or not isinstance(evidence.get('job'), dict)
                or evidence['job'].get('id') != job['id']
                or evidence['job'].get('takeover', {}).get('source_binding') != state.get('source_binding')
                or evidence['job'].get('takeover', {}).get('source_request_binding') != state.get('source_request_binding')
                or evidence['job'].get('input_path') != job.get('input_path')
                or evidence['job'].get('source_url') != job.get('source_url')
                or latest.get('source_sha256') != (None if acquiring else state['source_binding'].get('sha256'))
                or state.get('retry_at_unix') != latest['retry_at_unix']):
            raise ValueError('排队接管与原中断证据不一致，未启动任务。')
        from website_recovery import _journal
        from media_recovery import journal
        _journal(Path(root) / 'jobs' / job['id']); journal(Path(root) / 'jobs' / job['id'])
        from failure_triage import retry_message
        if retry_message(root, job): raise ValueError('当前内容问题仍待核对，未自动启动。')
    if state.get('source_binding'):
        validate_source(root, job)
    elif has_history:
        # An explicit manual retry may also continue its never-completed
        # initial URL request; permanent bound-source evidence still forbids
        # downgrading any previously acquired source.
        _acquiring_source(root, job)
    elif job.get('input_path') and job.get('options', {}).get('restart_policy') == POLICY:
        # A first upload is already byte-bound at submission. Missing that
        # record must not silently grant authority for replacement media.
        raise ValueError('上传任务缺少原片绑定，未启动任务。')
    elif state.get('source_request_binding'):
        _acquiring_source(root, job)
    # A first queued URL submission legitimately precedes download/binding.
    return automatic


def public_state(job):
    terminal = {'failed': 'needs_attention', 'completed': 'completed', 'canceled': 'canceled'}.get(job.get('status'))
    value = job.get('takeover', {})
    if not isinstance(value, dict):
        return {'enabled': False, 'state': terminal or 'needs_attention', 'used': MAX_TAKEOVERS, 'max': MAX_TAKEOVERS,
                'retry_at_unix': None, 'cancel_requested': bool(job.get('cancel_requested_at'))}
    events = value.get('events', [])
    valid = _valid_journal(events)
    return {'enabled': job.get('options', {}).get('restart_policy') == POLICY,
            'state': terminal or value.get('state', 'ready'), 'used': len(events) if valid else MAX_TAKEOVERS,
            'max': MAX_TAKEOVERS, 'retry_at_unix': value.get('retry_at_unix'),
            'cancel_requested': bool(job.get('cancel_requested_at'))}


def supervision_summary(root):
    """Whitelisted service health; no private paths, PIDs or raw errors."""
    result = {'state': 'unmanaged', 'restarts_used': 0, 'restarts_max': 6, 'login_autostart': False}
    try:
        control = read_json(runtime(root) / 'control.json', {})
        generation = control.get('generation')
        if not isinstance(generation, str) or not re.fullmatch(r'[a-f0-9]{32}', generation): return result
        value = read_json(runtime(root) / ('restarts_' + generation + '.json'), {})
        events = value.get('events', [])
        if not isinstance(events, list) or len(events) > 6: raise ValueError('invalid supervision history')
        result.update(state=value.get('state', 'starting'), restarts_used=len(events),
                      desired=control.get('desired'), retry_at_unix=value.get('retry_at_unix'))
    except (OSError, ValueError, TypeError, AttributeError): result['state'] = 'needs_attention'
    return result


def guardian(root, parent_fd, receipt, argv):
    """Own group + lease are created before executing any production command."""
    ended = False
    def interrupted(signum, frame):
        nonlocal ended
        ended = True
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    with InstanceLock(runtime(root) / 'children.lock', shared=True, blocking=True):
        # Propagate this shared lease to the immediate real command as well.
        # This closes the tiny wrapper-death-before-wait race for that process.
        with InstanceLock(runtime(root) / 'children.lock', shared=True, blocking=True) as lease:
            process = subprocess.Popen(argv, pass_fds=(lease.stream.fileno(),), stdin=subprocess.DEVNULL)
            stop_at = None
            while process.poll() is None:
                if select.select([parent_fd], [], [], .1)[0] and not os.read(parent_fd, 1): ended = True
                if ended:
                    if stop_at is None:
                        stop_at = time.monotonic()
                        os.killpg(os.getpgrp(), signal.SIGTERM)
                    elif time.monotonic() - stop_at > 3:
                        os.killpg(os.getpgrp(), signal.SIGKILL)
            code = process.wait()
            write_json(receipt, {'schema': 'owned_command_exit_v1', 'command_id': Path(receipt).stem,
                                 'returncode': code, 'at': stamp()})
            # A real command may have spawned a surviving descendant. Never let
            # it outlive the owned group or release resources early.
            os.killpg(os.getpgrp(), signal.SIGTERM)
            time.sleep(.1)
            # SIGKILL includes this guardian. Its real command outcome was
            # committed above; the caller reads that uniquely named receipt.
            # Thus no release-before-cleanup race or reused-PID signal exists.
            os.killpg(os.getpgrp(), signal.SIGKILL)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--guardian', action='store_true')
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--parent-fd', type=int, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if (not args.guardian or not command or not re.fullmatch(r'[a-f0-9]{32}', args.receipt.stem)
            or args.receipt.parent != runtime(args.root) / 'commands'):
        raise SystemExit(2)
    raise SystemExit(guardian(args.root, args.parent_fd, args.receipt, command))


if __name__ == '__main__': main()
