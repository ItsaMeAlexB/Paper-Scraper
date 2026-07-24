Overview
This script runs manually via GitHub Actions (workflow_dispatch). It:

PubMed – Fetches first 5 results per search term, filtered by date (28–35 days old).

bioRxiv – Same as above.

ClinicalTrials.gov – Fetches first 5 trials per search term, filtered by update date, with active statuses.

Named authors (David Liu, Ciaran Lee) – Searched across all three sources, deduplicated, and the oldest result is included.

Reddit – For each subreddit, fetches top 5 posts of the week, deduplicates across subreddits, takes the highest-scoring post, then fetches top 3 comments and top 1 reply.

PubPeer – For each paper/trial candidate, retrieves: evaluation summary (if available), 3 comments (oldest, newest, middle), and 1 reply per comment (if present).

DeepSeek – Selects 5 most significant papers/trials and 5 most relevant Reddit discussions, summarises with uncertainty handling.

Telegram – Sends the final digest.

Workflow
Trigger: Manual only – from the GitHub Actions tab.

No automatic cron; you run it when you want.

Runs once, sends digest, updates cache to avoid duplicates.

Search Terms
PubMed & bioRxiv
"gene therapy"
"gene editing"
"gene delivery"
cell* AND immun*
bioinformatic*
"computational biolog"
"DNA repair"
epigenetic
manufactur*
discover* OR engineer*

ClinicalTrials.gov (subset)
"gene therapy"
"gene editing"
"gene delivery"
cell* AND immun*
"DNA repair"
epigenetic*

Always-Include Authors
David Liu – queries: "Liu DR"[AU], ("Liu DR"[AU] AND (Broad[AD] OR Harvard[AD] OR HHMI[AD])), bioRxiv: "David Liu" OR "D. Liu", ClinicalTrials: "Liu DR"

Ciaran Lee – queries: "Lee CM"[AU], "Lee C"[AU], bioRxiv: "Ciaran Lee" OR "C. Lee", ClinicalTrials: "Lee C"

One oldest result per author (across all sources) is always included.

Reddit Subreddits (12 total)
labrats
bioinformatics
machinelearning
GeneEditing
clinicaltrials
biotech
computationalbiology
immunology
biochemistry
molecularbiology
cellbiology
statistics

Fetch top 5 posts of the week per subreddit.

Deduplicate posts across subreddits (by post ID).

Keep highest-scoring post per subreddit.

For each kept post: fetch comments via Reddit JSON, extract top 3 comments (by score) and top 1 reply per comment.

All kept posts (<=12) are sent to DeepSeek; it selects top 5.

PubPeer Comments
For each unique paper/trial candidate:

Fetch evaluation_summary (if present).

Select 3 comments: oldest, newest, and the middle one.

For each selected comment, include 1 reply (if exists).

Text capped at 300 chars per comment, 150 chars per reply.

Text Caps (pre-sampling values)
Reddit selftext: 1000 chars
Reddit comment: 300 chars
Reddit reply: 150 chars
Paper/trial abstract: 2000 chars

These are temporary; will be refined after sampling actual data from your sources.

DeepSeek Prompt
The prompt instructs:

Select 5 most significant papers/trials – 4–6 sentences each.

Select 5 most scientifically relevant Reddit discussions – 2–3 sentences each.

Named author results always included.

Uncertainty handling: ambiguous terms/acronyms are included verbatim – no guessing, ignoring, or hallucinating.

Output format with exact headers.

Setup
1. Repository Files
.
├── .github
│ └── workflows
│ └── daily.yml
├── daily_digest.py
├── requirements.txt
├── README.md
└── .gitignore

2. Secrets (GitHub Actions -> Settings -> Secrets)
DEEPSEEK_API_KEY – platform.deepseek.com
TELEGRAM_BOT_TOKEN – @BotFather on Telegram
TELEGRAM_CHAT_ID – After sending a message to your bot, call https://api.telegram.org/bot<TOKEN>/getUpdates
ENTREZ_EMAIL – Your email (required by NCBI)

3. Run
Go to the Actions tab of your repository.

Select the Daily Digest workflow.

Click Run workflow.

Wait for completion; check Telegram for the digest.

Caching
Caches sent items for 35 days (same as paper window).

Prevents duplicate notifications.

Cache files: sent_cache.txt, last_run_date.txt (currently not used since manual trigger).

Output Format
🧬 Date

Papers / Trials (most significant):

[Journal/ClinicalTrials.gov via "search_term" or "author name"] ID: ... – summary – Lab name. PubPeer: [brief summary].

...

Reddit (useful posts):

r/subreddit: "Title" – summary of post, then summary of comments/replies.

...

Summaries preserve ambiguous terms (no hallucinations).

PubPeer comments are summarised in the note.

Troubleshooting
Manual trigger: Actions tab -> "Run workflow".

Logs: Check the Actions run output.

Telegram not sending: Verify token and chat ID.

DeepSeek failures: Check API key and balance.

