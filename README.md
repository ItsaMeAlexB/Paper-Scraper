Daily Digest – PubMed + Reddit

This script fetches the newest papers from PubMed for seven defined topics, scrapes the top weekly posts from seven subreddits, and sends a daily digest via Telegram.

It runs in two modes:
- Weekly: builds a queue of up to 7 daily entries (each with one subreddit post and one or more papers). Runs every Sunday at 04:15 UTC.
- Daily: consumes one entry from the queue and sends a summary. Runs multiple times per day (off-peak) but only sends once per day.

Setup

1. Clone the repository and install dependencies:
   pip install -r requirements.txt

2. Set environment variables (or add to GitHub Secrets):
   ENTREZ_EMAIL – your email (required by NCBI)
   DEEPSEEK_API_KEY – your DeepSeek API key
   TELEGRAM_BOT_TOKEN – Telegram bot token from @BotFather
   TELEGRAM_CHAT_ID – your Telegram chat ID (get via getUpdates)
   APIFY_API – your Apify API key (from Apify Console > Settings > Integrations)

3. Commit the initial state files:
   weekly_queue.json (empty)
   last_sent_date.txt (empty)

4. Push to GitHub. The workflows will run automatically.

Search Terms (PubMed)

The script searches for papers from the last 7 days:
- Gene Editing
- Genome Engineering
- Vectors (viral vector OR non-viral vector)
- Gene Therapy
- Computational Biology
- Gene Delivery
- Cell Therapy

Named authors (always included if they published that week):
- David R Liu
- Ciaran M Lee

Subreddits (top weekly posts):
- bioinformatics
- labrats
- biotech
- molecularbiology
- biochemistry
- CRISPR
- ClinTrials

How It Works (Weekly Build)

1. For each subreddit, fetch the top post of the week (sorted by upvotes).
2. For each PubMed search term, fetch all papers from the last 7 days.
3. Deduplicate papers across terms (by DOI/PMID/title).
4. Use DeepSeek to select the most significant one per term.
5. Fetch named author papers (if any).
6. Build a queue of daily entries. Each entry contains one subreddit post and one or more papers (extra papers are added to the first entries).
7. Save the queue to weekly_queue.json and commit it.

How It Works (Daily Send)

1. Check if already sent today (using last_sent_date.txt) – if so, exit.
2. Load the queue from weekly_queue.json.
3. Pop the first entry.
4. Generate a 5-sentence summary for the subreddit post and each paper using DeepSeek.
5. Assemble a plain text message.
6. Send via Telegram (chunked if >4000 chars).
7. Mark today as sent.
8. Save the updated queue back to the file and commit.

Peak Pricing Protection

The script checks the current UTC time against DeepSeek peak windows (01:00-04:00 and 06:00-10:00 UTC) and exits early if inside or within 30 minutes of a peak window.

Workflows

- weekly.yml: runs Sunday at 04:15 UTC (off-peak) and can be triggered manually.
- daily.yml: runs at 23:15, 00:15, 01:15, 02:15, 03:15, 04:15, 05:15, 06:15, 07:15 UTC – will only send once per day.

Files

- daily_digest.py – main script (handles both modes)
- requirements.txt – dependencies
- .github/workflows/weekly.yml – weekly build workflow
- .github/workflows/daily.yml – daily send workflow
- weekly_queue.json – queue state (tracked)
- last_sent_date.txt – date of last send (tracked)

License

MIT
