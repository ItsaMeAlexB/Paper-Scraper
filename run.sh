#!/data/data/com.termux/files/usr/bin/bash
cd "$HOME/daily_digest" || exit 1
mkdir -p logs
termux-wake-lock
git pull --ff-only --quiet || echo "$(date -u) git pull failed; running local code"
python daily_digest.py >> logs/run.log 2>&1
termux-wake-unlock
