#!/usr/bin/env bash
# Daily Digest launcher — runs once, then exits.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")" || exit 1
mkdir -p logs
PY=".venv/bin/python3"
[ -x "$PY" ] || PY="python3"
exec "$PY" daily_digest.py >> logs/run.log 2>&1
