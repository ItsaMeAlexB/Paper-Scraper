Automated daily summary of relevant papers, clinical trials, and Reddit discussions, and their peer review, delivered to Telegram.

---

## Overview

This script scrapes:
- **PubMed** (peer‑reviewed papers)
- **bioRxiv** (preprints)
- **ClinicalTrials.gov** (clinical trials)
- **Reddit** (12 subreddits)

It then:
- Filters papers/trials **4–5 weeks old** (to allow PubPeer comments to appear).
- Filters Reddit posts **1–8 days old** (to allow votes/comments to settle).
- For each search term and source, fetches **exactly one unique** paper/trial (oldest, avoiding cache and duplicates).
- For Reddit, fetches **top 20** posts per subreddit, deduplicates, keeps **top 10**, computes a normalised engagement ratio, selects the **top 5 subreddits**, and takes the **#1 post** from each.
- Fetches **top 3 comments + top 1 reply** for each selected Reddit post.
- Sends all candidates to **DeepSeek API** to pick the **5 most significant papers/trials** and to summarise the **5 Reddit posts**.
- Delivers a formatted daily digest via **Telegram**.

---

## Search Terms

### Applied to PubMed & bioRxiv

```
"gene therapy"
"gene editing"
"gene delivery"
cell* AND immun*
bioinformatic*
"computational biolog*"
"DNA repair"
epigenetic*
manufactur*
discover* OR engineer*
```

### Applied to ClinicalTrials.gov (subset)

```
"gene therapy"
"gene editing"
"gene delivery"
cell* AND immun*
"DNA repair"
epigenetic*
```

---

## Always‑Include Authors

- David Liu (ORCID: 0000-0002-9943-7557)
- Ciaran Lee (ORCID: 0000-0002-0789-9149)

One paper per author (the **oldest** within the 4–5 week window) is always included.

---

## Reddit Subreddits (12 total)

```
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
```

- Minimum comments: **1** (effectively no filter, since top posts already have ≥1 comment).
- **Top 20** posts fetched per subreddit → dedupe (cache) → keep **top 10** → compute ratio → select **top 5** subreddits → take their **#1** post.

---

## Character Caps (Safe – Above Normal)

| Item | Cap |
| :--- | :--- |
| Abstract (paper/trial) | 2000 chars |
| Reddit selftext | 3000 chars |
| Reddit comment | 500 chars |
| Reddit reply | 300 chars |

Exceeding caps is logged; no meaningful content is lost.

---

## Time Windows

| Item | Window |
| :--- | :--- |
| Papers / Trials | 4–5 weeks old (28–35 days) |
| Reddit posts | 1–8 days old |
| Cache TTL | 5 weeks (35 days) |

---

## Cron & Idempotency

- Runs **every hour** via GitHub Actions (`0 * * * *`).
- At start:
  - Checks if already run **today** → exit if yes.
  - Checks **peak hours** (DeepSeek pricing: UTC 01:00–04:00) → exit if yes.
- If neither, runs the digest and marks today as done.
- This guarantees a run within the first safe hour after peak.

---

## Setup

### 1. Repository Files

Create a repository with:

```
.
├── .github
│   └── workflows
│       └── daily.yml
├── requirements.txt
├── daily_digest.py
├── README.md
└── .gitignore
```

### 2. Secrets (GitHub Actions → Settings → Secrets)

| Secret | Where to get |
| :--- | :--- |
| `DEEPSEEK_API_KEY` | platform.deepseek.com |
| `TELEGRAM_BOT_TOKEN` | @BotFather on Telegram |
| `TELEGRAM_CHAT_ID` | After sending a message to your bot, call `https://api.telegram.org/bot<TOKEN>/getUpdates` |
| `REDDIT_CLIENT_ID` | reddit.com/prefs/apps → create script app |
| `REDDIT_CLIENT_SECRET` | same page |
| `REDDIT_USER_AGENT` | e.g., `"DailyDigest/1.0 (by /u/yourname)"` |
| `ENTREZ_EMAIL` | your email (required by NCBI) |

### 3. Run

Push to GitHub. The workflow will trigger hourly. You can also manually trigger from the Actions tab.

---

## Output Format

```
🧬 **[Date]**

**Papers / Trials (most significant):**
1. [Source via "search_term"] ID: ... – 4-6 sentence summary – Lab name. PubPeer: note.
2. ...

**Reddit (useful posts):**
1. r/subreddit: "Title" – 2-3 sentence summary of post + comments/replies.
2. ...
```

- For papers: ID is DOI; for trials: NCT number.
- Source is `PubMed`, `bioRxiv`, or `ClinicalTrials.gov`.
- If a trial has a linked paper, its abstract and PubPeer are merged.
- Named authors appear as `[Source via "David Liu"]` or `[Source via "Ciaran Lee"]`.
- Summaries are 4–6 sentences for papers, 2–3 for Reddit posts.
- Ambiguities are flagged rather than guessed.

---

## Design Rationale

- **Fetch‑until‑unique**: Ensures exactly one candidate per source+term, avoiding duplicate notifications.
- **Reddit normalisation**: Ratio of top post to average of next nine identifies subs with truly standout content.
- **DeepSeek selection**: Minimal prompt – asks for "most significant" without over‑filtering, preserving recall.
- **PubPeer never cancels**: Comments are noted but papers/trials are never removed.
- **Character caps above normal**: Prevents rambling from clogging tokens while keeping all meaningful information.
- **Hourly cron with idempotency**: Reliable even if a scheduled run is missed.

---

## Troubleshooting

- **Manual trigger**: Actions tab → "Run workflow".
- **Logs**: Check the Actions run output.
- **Telegram not sending**: Verify token and chat ID.
- **DeepSeek failures**: Check API key and balance at platform.deepseek.com.

---

## License

MIT – use and modify freely.