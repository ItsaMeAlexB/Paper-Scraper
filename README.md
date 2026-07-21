# Paper-Scraper
# Gene Therapy / Editing / Delivery / Engineering

Automated daily summary of relevant papers and Reddit discussions for gene therapy researchers.

## Overview

This script scrapes:
- **PubMed** (peer-reviewed papers)
- **bioRxiv** (preprints)
- **Reddit** (`r/labrats`, `r/bioinformatics`, `r/machinelearning`, `r/GeneEditing`, `r/clinicaltrials`, `r/biotech`)

It then:
- Filters for papers **4–5 weeks old** (to allow PubPeer comments to appear)
- Filters Reddit posts **1–8 days old** (to allow votes/comments to settle)
- Checks PubPeer (notes comments, never cancels)
- Sends all candidates to **DeepSeek API** for selection and 2–3 sentence summarisation
- Delivers a formatted daily digest via **Telegram**

## Search Keywords

- `"gene therapy"`
- `"gene editing"`
- `"gene delivery"`
- `"gen* engineering"`
- `"machine learning" AND biochem*`
- `"bioinformatics" AND biochem*`
- `"DNA repair"`
- `"epigenetic*"`

## Always-Include Authors

- David Liu (ORCID: 0000-0002-9943-7557)
- Ciaran Lee (ORCID: 0000-0002-0789-9149)

Papers from these authors are always included in the digest.

## Character Caps (Safe – Above Normal)

| Item | Cap |
| :--- | :--- |
| Abstract | 2000 chars |
| Reddit selftext | 3000 chars |
| Reddit comment | 500 chars |
| Reddit reply | 300 chars |

If a cap is exceeded, the system logs it but never truncates meaningful content.

## Why 4 Weeks for Papers?

PubPeer comments have a median delay of ~10 days, but can take up to several weeks. A 4–5 week window:
- Catches most meaningful comments
- Avoids sending papers before community reaction forms
- Still provides timely enough updates for a researcher

## Why 1–8 Days for Reddit?

Reddit votes and comments stabilise within 24–48 hours. The 1–8 day window:
- Skips very fresh posts where votes are still hidden
- Catches posts with established discussion
- Avoids stale content (>8 days)

## Setup

### 1. GitHub Repository

Create a new repository and push these files:
├── .github/workflows/daily.yml
├── requirements.txt
├── daily_digest.py
└── README.mmd


### 2. Secrets

Go to your repo → **Settings** → **Secrets and variables** → **Actions** → add these:

| Secret | Where to get it |
| :--- | :--- |
| `DEEPSEEK_API_KEY` | platform.deepseek.com → API Keys |
| `TELEGRAM_BOT_TOKEN` | @BotFather on Telegram |
| `TELEGRAM_CHAT_ID` | Send a message to your bot, then `https://api.telegram.org/bot<TOKEN>/getUpdates` |
| `REDDIT_CLIENT_ID` | reddit.com/prefs/apps → create script app |
| `REDDIT_CLIENT_SECRET` | same page |
| `REDDIT_USER_AGENT` | e.g., `"DailyDigest/1.0 (by /u/username)"` |
| `ENTREZ_EMAIL` | your email (NCBI requires it) |

### 3. Run

Push to GitHub. The workflow runs daily at **20:00 UTC** (avoids DeepSeek peak pricing). You can also trigger it manually from the Actions tab.

## Cost

- **DeepSeek API**: ~$0.0005 per day with `deepseek-v4-flash` model
- **Free credit**: $5 for new users → enough for ~10,000 days
- **GitHub Actions**: Free for public repositories

## Output Format
🧬 Daily Digest – [Date]

PubMed / bioRxiv:

[source] "title" – 2-3 sentence summary... (PubPeer: note)

Named authors:

[source] "title" – 2-3 sentence summary... (PubPeer: note)

Reddit:

[Post] r/subreddit: "title" – 2-3 sentence summary... (Top comment: "quote" [+score])


## Design Rationale

- **No DeepSeek filtering**: Prevents false negatives where a paper is relevant but the AI misclassifies it.
- **PubPeer never cancels**: Comments are noted but papers are never removed.
- **Reddit not filtered by keywords for top subs**: Useful lab advice, career posts, and discussions are caught even if they don't match keywords.
- **Character caps above normal**: Ensure no meaningful content is lost while preventing rants from clogging the system.
- **Peak hours guardrail**: Skips runs during DeepSeek peak pricing, retries after 2 hours.

## Troubleshooting

- **Manual run**: Go to Actions tab → select workflow → "Run workflow".
- **Check logs**: Actions tab shows stdout/stderr from each run.
- **Telegram not sending**: Verify `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are correct.
- **DeepSeek failures**: Check your API key and balance at platform.deepseek.com.

## License

MIT – use freely, modify as needed.
