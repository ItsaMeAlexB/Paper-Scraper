Daily Digest
============

Daily scoping review of new literature on whatever topic you configure (PubMed),
delivered to Telegram. Every search term and every named author is queried for
the previous UTC day, then two DeepSeek calls turn the results into one
scoping-review paragraph with clickable citations. Runs once a day at 06:30
machine-local time; GitHub is just the code repo — secrets and per-machine state
stay on the machine.


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

   If the image has no `python3-venv` and no `pip` at all (common on minimal
   VPS images), those two commands fail with PEP 668 errors. Without root,
   bootstrap pip into your home directory instead:

       curl -fsSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
       python3 /tmp/get-pip.py --user --break-system-packages
       python3 -m pip install --user --break-system-packages -r requirements.txt

   `--user` keeps everything inside `~/.local`, so nothing system-wide is
   touched. `run.sh` then falls back to the system `python3`, which already sees
   user site-packages.

2. Create local config:

       ./setup_local.sh

   This copies each `.example` file to its local name (only if it doesn't
   already exist) and makes the shell scripts executable.

3. Fill them in:

       nano .env
       nano sources.txt
       nano prompt_triage.txt
       nano prompt_review.txt

   `NCBI_API_KEY` is optional — free from
   <https://account.ncbi.nlm.nih.gov/settings/> — and lifts PubMed's rate limit
   from 3 to 10 requests per second. `ENTREZ_EMAIL` is required.

4. Smoke test:

       ./run.sh

   This sends a real digest and marks the day done. Running it again on the same
   UTC day logs `Already sent today` and exits, so it is safe to invoke by hand.


Scheduling
----------

The digest is sent once a day at 06:30 machine-local time. One cron entry fires,
`daily_digest.py` runs, and the machine goes back to doing nothing until the next
day. No daemon, no resident process.

Add an entry to the system crontab:

    crontab -e

Add these lines:

    TZ=UTC
    30 6 * * * $HOME/Paper-Scraper/run.sh

- Cron schedules in **local** time. On Ubuntu/Debian, cron (vixie-cron) ignores
  `TZ=UTC` when reading the schedule; the variable only reaches the job's
  environment, where it keeps the log timestamps in UTC.
- DeepSeek peak hours are 01:00-04:00 and 06:00-10:00 UTC, Monday to Friday;
  every other hour is off-peak, and weekends are off-peak all day. 06:30 local
  is 05:30 UTC in winter (CET) and 04:30 UTC in summer (CEST), so either way it
  sits in the 04:00-06:00 UTC weekday gap. A one-hour DST shift cannot move a
  two-hour window, so the slot never drifts into peak.
- On a winter weekday the run starts 05:30 UTC, leaving 30 minutes before peak
  resumes; a normal run takes a few. Overrunning only makes the trailing calls
  bill at peak rates.
- There is no hour check in the program — cron owns the schedule, and there are
  no timezone conversions to get wrong. The only guard is same-day idempotency,
  so a second invocation logs `Already sent today` instead of posting twice.
  `run.sh` captures stdout/stderr, so cron has nothing to mail and no extra
  redirect is needed.
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
