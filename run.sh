#!/usr/bin/env bash
# Daily Digest launcher — Linux laptop. Runs once, then exits.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")" || exit 1
mkdir -p logs
python3 daily_digest.py >> logs/run.log 2>&1
