Daily Digest
============

Daily scoping review of gene-therapy literature (PubMed + Reddit top post),
delivered to Telegram. Runs once a day on a Linux laptop: a light cron check
every 15 minutes delivers the digest the first time you're on during
12:00-24:00 UTC, then skips until the next day. GitHub is just the code repo —
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

The digest is delivered at the first opportunity within the 12:00-24:00 UTC
window. A light cron tick fires every 15 minutes all day; `daily_digest.py`
returns immediately unless it is inside the window and today's digest hasn't
been sent yet, so it sends once per day no matter when the laptop is on.

Add an entry to the system crontab:

    crontab -e

Add these lines:

    TZ=UTC
    */15 * * * * $HOME/daily_digest/run.sh >> $HOME/daily_digest/logs/cron.log 2>&1

- Each tick exits immediately — no background process or resident daemon; the
  send happens at most once per day.
- The 12:00-24:00 UTC check is DST-proof because `daily_digest.py` gates on
  UTC time itself; the TZ=UTC line just sets the job's timezone environment.
- Make sure the cron daemon is enabled so it survives reboots:

      sudo systemctl enable --now cron     # Debian/Ubuntu

Verify it registered:

    crontab -l


Missed-day behaviour
--------------------

- Sent today                -> skip.
- Sent yesterday            -> normal run.
- Sent exactly one day    -> two runs in the next window: the missed day (term
                             window N+1 ending yesterday, Reddit month
                             tier) followed by today's normal run.
- Missed two or more days   -> today's run only; backlog logged and ignored.


Rotating secrets
----------------

Rotate the DeepSeek API key, the Telegram bot token (via /revoke in
@BotFather), and the Apify token if any of them were ever committed or
shared. .gitignore protects future commits; it does not remove anything
from git history.
