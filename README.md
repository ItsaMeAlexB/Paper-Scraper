Daily Digest
============

Daily scoping review of gene-therapy literature (PubMed + Reddit top post),
delivered to Telegram. Runs once a day on a Linux laptop, scheduled with cron
at 20:00 UTC (DeepSeek off-peak), then exits. GitHub is just the code repo —
secrets and per-machine state stay on the laptop.


Repo layout
-----------

daily_digest.py         The program.
run.sh                  Launcher: git pull, then one run.
setup_local.sh          One-time setup (creates .env, sources.txt, logs/).
.env.example            Template for secrets.
sources.txt.example     Template for terms / subreddits / authors.
.gitignore              Keeps secrets and per-machine state out of git.
MIT license             Free for use and adaptation.

Only three small Python dependencies (no numpy / biopython):
requests, apify-client, python-dotenv.


Laptop setup (Linux, one-time)
------------------------------

1. Clone and install Python deps:

       git clone <your-repo> ~/daily_digest
       cd ~/daily_digest
       pip install -r requirements.txt

2. Create local config:

       ./setup_local.sh

   This copies .env.example -> .env and sources.txt.example -> sources.txt
   (only if they don't already exist) and makes the shell scripts executable.

3. Fill them in:

       nano .env
       nano sources.txt

4. Smoke test:

       ./run.sh


Scheduling
----------

Add an entry to the system crontab:

    crontab -e

Add these lines:

    TZ=UTC
    0 20 * * * $HOME/daily_digest/run.sh >> $HOME/daily_digest/logs/cron.log 2>&1

- run.sh runs the job once, then exits — nothing stays running in the
  background.
- TZ=UTC is required: without it, the job fires at 20:00 local time, which
  drifts from 20:00 UTC across DST.
- Make sure the cron daemon is enabled so it survives reboots:

      sudo systemctl enable --now cron     # Debian/Ubuntu

Verify it registered:

    crontab -l


Missed-day behaviour
--------------------

- Sent today                -> skip.
- Sent yesterday            -> normal run.
- Missed exactly one day    -> two runs at 20:00 UTC: the missed day (term
                               window N+1 ending yesterday, Reddit month
                               tier) followed by today's normal run.
- Missed two or more days   -> today's run only; backlog logged and ignored.


Rotating secrets
----------------

Rotate the DeepSeek API key, the Telegram bot token (via /revoke in
@BotFather), and the Apify token if any of them were ever committed or
shared. .gitignore protects future commits; it does not remove anything
from git history.
