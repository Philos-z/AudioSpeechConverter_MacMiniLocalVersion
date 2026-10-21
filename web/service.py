"""Control only the Philos project's local HTTP service, without credentials."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import urllib.request


def status():
    try:
        with urllib.request.urlopen('http://127.0.0.1:8766/api/health', timeout=5) as response:
            value = json.load(response)
        return value if value.get('version') == 'philos_lan_web_v1' else None
    except Exception:
        return None


def stop(root):
    report = root / 'reports/web_app'
    pid_file = report / 'server.pid'
    if not pid_file.is_file():
        return
    pid = int(pid_file.read_text().strip())
    command = subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True).stdout
    if str(root / 'web/server.py') not in command:
        return
    os.kill(pid, signal.SIGTERM)
    for _ in range(60):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(.1)
    # Do not force-kill an export or leave its model child running.
    if status():
        raise RuntimeError('服务正在停止，请稍后再试。')


def start(root):
    if status():
        return
    report = root / 'reports/web_app'
    report.mkdir(parents=True, exist_ok=True)
    with (report / 'server.stdout.log').open('ab') as out, (report / 'server.stderr.log').open('ab') as err:
        process = subprocess.Popen([str(root / 'envs/asr/bin/python'), '-B', str(root / 'web/server.py'),
                                    '--root', str(root), '--host', '0.0.0.0', '--port', '8766'],
                                   cwd=root / 'web', stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                   start_new_session=True)
    pid_file = report / 'server.pid'
    pid_file.write_text(str(process.pid) + '\n'); pid_file.chmod(0o600)
    for _ in range(30):
        if status():
            return
        if process.poll() is not None:
            raise RuntimeError('服务未能启动，请查看项目 reports/web_app 中的日志。')
        time.sleep(.1)
    raise RuntimeError('服务尚未响应，请稍后检查。')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['start', 'stop', 'restart', 'status'])
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action in {'stop', 'restart'}: stop(root)
    if args.action in {'start', 'restart'}: start(root)
    print(json.dumps({'running': status() is not None, 'url': 'http://127.0.0.1:8766/'}, ensure_ascii=False))


if __name__ == '__main__': main()
