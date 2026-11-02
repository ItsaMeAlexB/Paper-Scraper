Daily Digest
============

Daily scoping review of gene-therapy literature (PubMed), delivered to
Telegram. Every search term and every named author is queried for the previous
UTC day, then two DeepSeek calls turn the results into one scoping-review
paragraph with clickable citations. Runs once a day at 05:00 UTC; GitHub is just
the code repo — secrets and per-machine state stay on the machine.


Repo layout
-----------

daily_digest.py             The program.
run.sh                      Launcher: run once, then exit.
setup_local.sh              One-time setup (creates the local files below).
.env.example                Template for secrets.
sources.txt.example         Template for terms / authors.
prompt_triage.txt.example   Template for call 1: pick the significant papers.
prompt_review.txt.example   Template for call 2: write the review.
.gitignore                  Keeps secrets and per-machine state out of git.
MIT license                 Free for use and adaptation.

Four local files are created by setup_local.sh and never committed:
`.env`, `sources.txt`, `prompt_triage.txt`, `prompt_review.txt`.

Only two small Python dependencies (no numpy / biopython):
requests, python-dotenv.


Setup (Linux, one-time)
-----------------------

1. Clone and install Python deps into a venv (distros that refuse a system
   `pip install` are the norm now):

       git clone https://github.com/ItsaMeAlexB/Paper-Scraper.git ~/Paper-Scraper
       cd ~/Paper-Scraper
       python3 -m venv .venv
       .venv/bin/pip install -r requirements.txt

   `run.sh` prefers `.venv/bin/python3` when it exists, so cron needs no extra
   configuration.

2. Create local config:

       ./setup_local.sh

   This copies each `.example` file to its local name (only if it doesn't
   already exist) and makes the shell scripts executable.

3. Fill them in:

       nano .env
       nano sources.txt
       nano prompt_triage.txt
       nano prompt_review.txt

4. Smoke test:

       ./run.sh

   Outside the 05:00 UTC hour this exits immediately by design — check
   `logs/daily_digest.log` for the `Outside 05:00 UTC` line. Inside that hour it
   sends a real digest and marks the day done.


Scheduling
----------

The digest is sent once a day at 05:00 UTC — inside DeepSeek's 04:00-06:00 UTC
off-peak window. One cron entry fires, `daily_digest.py` runs, and the machine
goes back to doing nothing until the next 05:00. No daemon, no resident process.

Add an entry to the system crontab:

    crontab -e

Add these lines:

    TZ=UTC
    0 5 * * * $HOME/Paper-Scraper/run.sh

- One process start per day. `run.sh` captures stdout/stderr, so cron has
  nothing to mail and no extra redirect is needed.
- The hour check inside `daily_digest.py` still guards manual runs; the TZ=UTC
  line just sets the job's timezone environment, so DST never shifts the slot.
- If the machine is off or the run fails, that day is skipped. The next run
  covers its own yesterday; nothing is replayed and nothing is queued.
- Logs are truncated every 7 days, so disk use is the same after six months as
  after two days.
- Make sure the cron daemon is enabled so it survives reboots:

      sudo systemctl enable --now cron     # Debian/Ubuntu

Verify it registered:

    crontab -l


What a run does
---------------

1. Every term in `[TERMS]` is searched, then every author in `[AUTHORS]`, over
   the single previous UTC day. A paper hit by two terms is kept once.
2. Call 1 (`deepseek-chat`) sees titles, last authors and journals only, and
   returns the numbers worth reviewing (`prompt_triage.txt`). Named-author
   papers are never triaged out. If the call fails or its JSON is unparseable,
   every paper is kept.
3. Abstracts for the surviving papers are fetched from PubMed.
4. Call 2 (`deepseek-reasoner`) re-reads the selection with full abstracts,
   drops anything the abstract weakens, and writes one scoping-review
   paragraph under 4,096 characters (`prompt_review.txt`).
5. Citations arrive as `[[REF:n]]` and are rendered into clickable links. The
   message is split only if it somehow exceeds Telegram's 4,096-character limit.


Missed-day behaviour
--------------------

- Sent today     -> skip.
- Not sent today -> normal run over yesterday.
- Missed a day   -> nothing happens; the next run covers its own yesterday and
                    the missed day is simply not reported.


Rotating secrets
----------------

Rotate the DeepSeek API key and the Telegram bot token (via /revoke in
@BotFather) if either was ever committed or shared. .gitignore protects future
commits; it does not remove anything from git history.
