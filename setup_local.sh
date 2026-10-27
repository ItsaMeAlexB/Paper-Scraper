#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1
mkdir -p logs
[ -f .env ]        || { cp .env.example .env;              echo "Created .env — edit it."; }
[ -f sources.txt ] || { cp sources.txt.example sources.txt; echo "Created sources.txt — edit it."; }
chmod +x run.sh setup_local.sh
echo "Ready. Edit .env and sources.txt, then run ./run.sh."
