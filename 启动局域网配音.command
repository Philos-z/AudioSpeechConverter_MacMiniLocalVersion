#!/bin/zsh
set -eu
PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
"$PROJECT_ROOT/envs/asr/bin/python" -B "$PROJECT_ROOT/web/service.py" start --root "$PROJECT_ROOT"
open "http://127.0.0.1:8766/"
