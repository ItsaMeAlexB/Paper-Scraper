## Overview

This script scrapes:
- **PubMed** (peer-reviewed papers)
- **bioRxiv** (preprints)
- **ClinicalTrials.gov** (clinical trials)
- **Reddit** (`r/labrats`, `r/bioinformatics`, `r/machinelearning`, `r/GeneEditing`, `r/clinicaltrials`, `r/biotech`)

It then:
- Filters for papers/trials **4–5 weeks old** (to allow PubPeer comments to appear)
- Filters Reddit posts **1–8 days old** (to allow votes/comments to settle)
- For **PubMed, bioRxiv, and ClinicalTrials.gov**: takes the **3 oldest** items per source and sends them to DeepSeek, which selects **1 most significant** from each.
- For **named authors** (David Liu, Ciaran Lee): takes the **1 oldest** paper per author and summarises directly.
- For **top Reddit subs** (`r/labrats`, `r/bioinformatics`, `r/machinelearning`, `r/GeneEditing`): takes **1 highest-rated** post per sub and summarises directly.
- For **filtered Reddit subs** (`r/clinicaltrials`, `r/biotech`): takes **2 highest-rated** posts per sub and sends them to DeepSeek, which **evaluates relevance** and includes only those clearly matching your keywords.
- Checks PubPeer (notes comments, never cancels papers or trials).
- Delivers a formatted daily digest via **Telegram** with separate sections for PubMed, bioRxiv, ClinicalTrials, named authors, and Reddit.

## Search Keywords (for PubMed, bioRxiv, ClinicalTrials.gov, and relevance evaluation on filtered Reddit)

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

Papers from these authors are always included in the digest (1 oldest each, summarised directly).

## Character Caps (Safe – Above Normal)

| Item | Cap |
| :--- | :--- |
| Abstract | 2000 chars |
| Reddit selftext | 3000 chars |
| Reddit comment | 500 chars |
| Reddit reply | 300 chars |

If a cap is exceeded, the system logs it but never truncates meaningful content.

## Why 4 Weeks for Papers/Trials?

PubPeer comments have a median delay of ~10 days, but can take up to several weeks. A 4–5 week window:
- Catches most meaningful comments
- Avoids sending items before community reaction forms
- Still provides timely enough updates for a researcher

## Why 1–8 Days for Reddit?

Reddit votes and comments stabilise within 24–48 hours. The 1–8 day window:
- Skips very fresh posts where votes are still hidden
- Catches posts with established discussion
- Avoids stale content (>8 days)

## Setup

### 1. GitHub Repository

Create a new repository and push these files:

```
.
├── .github
│   └── workflows
│       └── daily.yml
├── requirements.txt
├── daily_digest.py
└── README.md
```

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

- **DeepSeek API**: ~$0.0007 per day with `deepseek-chat` model
- **Free credit**: $5 for new users → enough for ~10,000 days
- **GitHub Actions**: Free for public repositories

## Output Format

```
🧬 **Daily Digest – [Date]**

**PubMed (most significant):**
1. [source] "title" – 2-3 sentence summary... (PubPeer: note)

**bioRxiv (most significant):**
1. [source] "title" – 2-3 sentence summary... (PubPeer: note)

**Clinical Trials (most significant):**
1. [ClinicalTrials.gov] NCT123456: "title" – summary including status, phase, enrollment... (PubPeer: note)

**Named authors:**
1. [source] "title" – 2-3 sentence summary... (PubPeer: note)

**Reddit (top subs):**
1. [Post] r/subreddit: "title" – 2-3 sentence summary... (Top comment: "quote" [+score])

**Reddit (filtered subs – relevant only):**
1. [Post] r/subreddit: "title" – 2-3 sentence summary... (Top comment: "quote" [+score])
```

## Design Rationale

- **No local relevance filtering for filtered Reddit**: DeepSeek evaluates relevance, avoiding false negatives from simplistic keyword matching.
- **3 oldest papers/trials per source**: Gives DeepSeek enough options to pick the most significant, while ensuring we see the earliest (closest to 5 weeks) publications.
- **PubPeer never cancels**: Comments are noted but papers/trials are never removed.
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