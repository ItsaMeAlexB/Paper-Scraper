Daily Digest – PubMed + Reddit

This script fetches one search term and one subreddit daily (rotating through config lists), retrieves the top weekly post and its top comment (using harshmaur/reddit-scraper), selects the most significant paper for the term (using DeepSeek), checks named authors (David R Liu, Ciaran M Lee) for the last day, and sends a Telegram digest with summaries.

Setup

1. Create sources.txt with sections [TERMS], [SUBREDDITS], [AUTHORS] – one per line.
2. Set environment variables / GitHub Secrets:
   ENTREZ_EMAIL – your email for PubMed
   DEEPSEEK_API_KEY – DeepSeek API key
   TELEGRAM_BOT_TOKEN – Telegram bot token
   TELEGRAM_CHAT_ID – Telegram chat ID
   APIFY_API – Apify API key (for harshmaur/reddit-scraper)
3. Install dependencies: pip install -r requirements.txt
4. Commit sources.txt and last_sent_date.txt (empty) to repo.
5. The GitHub Action daily.yml runs daily (off‑peak) and sends once per day.

Rotation: uses weekday index to cycle through terms and subreddits. Date windows:
- PubMed terms: fetch from last N days (N = number of terms)
- Reddit subreddit: fetch top posts from last M days (M = number of subreddits)
- Named authors: checked from last 1 day

Reddit: fetches top 3 posts from the dynamic window, picks first with body length > 0, then fetches its top comment.

Output: one Telegram message with Reddit post + top comment (if any), one selected paper, and all named author papers from the last day.

Peak pricing protection: skips runs during DeepSeek peak windows (01-04 and 06-10 UTC) and within 30 minutes before.

Future: Consider switching to a lightweight model (e.g., Qwen3-1.7B PubMed) for summarization to reduce API costs.

Files: daily_digest.py, sources.txt, daily.yml, last_sent_date.txt, requirements.txt, .gitignore.

License: MIT
