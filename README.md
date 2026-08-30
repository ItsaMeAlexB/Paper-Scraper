Daily Digest – PubMed + Reddit

This script fetches one search term and one subreddit daily (rotating through config lists), retrieves the top post of the last M days and its top comment (using harshmaur/reddit-scraper), selects the most significant paper for the term (using DeepSeek), checks named authors for the last day, and sends a Telegram digest with summaries.

Setup

Create sources.txt with sections [TERMS], [SUBREDDITS], [AUTHORS] – one per line.

Set environment variables / GitHub Secrets:
ENTREZ_EMAIL – your email for PubMed
DEEPSEEK_API_KEY – DeepSeek API key
TELEGRAM_BOT_TOKEN – Telegram bot token
TELEGRAM_CHAT_ID – Telegram chat ID
APIFY_API – Apify API key (for harshmaur/reddit-scraper)

Install dependencies: pip install -r requirements.txt

Commit sources.txt, last_sent_date.txt (empty), term_index.txt (0), subreddit_index.txt (0) to repo.

The GitHub Action daily.yml runs daily (off‑peak) and sends once per day.

Rotation: uses persistent indices (term_index.txt and subreddit_index.txt) to cycle through terms and subreddits in order. After a successful send, both indices increment. Date windows:

PubMed terms: fetch from last N days (N = number of terms)

Reddit subreddit: fetch top posts from the last M days (M = number of subreddits). The script fetches either the top week (if M <= 7) or top month (if M > 7) and then filters in Python to the exact M-day window. For M < 7, a post from 6 or 7 days ago might appear because it was among the week's top posts – this trade‑off keeps the actor call simple and cost‑effective.

Named authors: checked from last 1 day

Reddit: fetches top 50 posts from the appropriate time window (week or month), filters to the last M days, sorts by upvotes, picks the first with body length > 0, then fetches its top comment.

Output: one Telegram message with:

Reddit post: title, subreddit, upvotes, author, summary (4 sentences)

Top comment (if any): upvotes, author, included in the Reddit summary

Selected paper: title, lab (last author), journal, summary (4 sentences), and the search term

Named author papers: same format, prefixed with "Named Author Paper"

Peak pricing protection: skips runs during DeepSeek peak windows (01-04 and 06-10 UTC) and within 30 minutes before.

Daily‑send guard: uses last_sent_date.txt to ensure only one digest per day, even if the workflow triggers multiple times.

Files: daily_digest.py, sources.txt, daily.yml, last_sent_date.txt, term_index.txt, subreddit_index.txt, requirements.txt, .gitignore.

License: MIT
