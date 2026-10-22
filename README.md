Daily Digest
============

Daily scoping review of gene-therapy literature (PubMed + Reddit top post),
delivered to Telegram. Runs on an Android phone under Termux at 20:00 UTC
(DeepSeek off-peak). GitHub is just the code repo — secrets and per-device
state stay on the phone.


Repo layout
-----------

daily_digest.py         The program.
run.sh                  Phone wrapper: git pull, wake lock, run.
setup_local.sh          One-time phone setup (creates .env, sources.txt, logs/).
.env.example            Template for secrets.
sources.txt.example     Template for terms / subreddits / authors.
.gitignore              Keeps secrets and per-device state out of git.
MIT license             Free for use and adaptation.


Phone setup (Termux, one-time)
------------------------------

1. Install packages:

       pkg update && pkg upgrade
       pkg install python git termux-api cronie
       pkg install python-numpy
       termux-setup-storage

   python-numpy is only there to help biopython build; termux-setup-storage
   is optional (only needed if you want logs on shared storage).

2. Clone and install Python deps:

       git clone <your-repo> ~/daily_digest
       cd ~/daily_digest
       pip install -r requirements.txt

3. Create local config:

       ./setup_local.sh

   This copies .env.example -> .env and sources.txt.example -> sources.txt
   (only if they don't already exist) and makes the shell scripts executable.

4. Fill them in:

       nano .env
       nano sources.txt

5. Smoke test:

       ./run.sh


Scheduling
----------

Cronie must be running. Android kills it on reboot, so start it from a boot
script. That boot script does nothing else — no Python, no git, no delay.

    mkdir -p ~/.termux/boot
    cat > ~/.termux/boot/start-crond <<'EOF'
    #!/data/data/com.termux/files/usr/bin/bash
    pgrep -x crond >/dev/null || crond
    EOF
    chmod +x ~/.termux/boot/start-crond

Install Termux:Boot from F-Droid, open it once from the launcher, and set
battery optimisation to Unrestricted for both Termux and Termux:Boot
(Settings -> Apps -> Termux -> Battery -> Unrestricted).

Then install the 20:00 UTC cron entry:

    crontab -e

Add this line:

    TZ=UTC
    0 20 * * * /data/data/com.termux/files/home/daily_digest/run.sh >> /data/data/com.termux/files/home/daily_digest/logs/cron.log 2>&1

TZ=UTC is required: without it, the cron line fires at 20:00 local time
(19:00 UTC in Irish summer time), not 20:00 UTC.

Verify it registered:

    pgrep -l crond
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
