#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1
mkdir -p logs
[ -f .env ]                || { cp .env.example .env;                                 echo "Created .env — edit it."; }
[ -f sources.txt ]         || { cp sources.txt.example sources.txt;                   echo "Created sources.txt — edit it."; }
[ -f prompt_triage.txt ]   || { cp prompt_triage.txt.example prompt_triage.txt;       echo "Created prompt_triage.txt — edit it."; }
[ -f prompt_review.txt ]   || { cp prompt_review.txt.example prompt_review.txt;       echo "Created prompt_review.txt — edit it."; }
chmod +x run.sh setup_local.sh
echo "Ready. Edit .env, sources.txt and the two prompt files, then run ./run.sh."
