# Paper-Scraper — Daily Digest

A small daily automation that surfaces **one significant scientific paper** and
**one top Reddit post** per day, delivering them to Telegram as short,
link-based messages — titles and metadata only, no summaries, so the primary
source is read directly (in a phone speed-reading / RSVP app).

## How it runs

- **Schedule:** daily at **05:00 UTC** via **GitHub Actions**
  (`.github/workflows/daily-digest.yml`).
- **Runtime:** a single Python 3 script (`daily_digest.py`), run inside its own
  environment with the dependencies in `requirements.txt`.
- **State:** none that needs to persist. Rotation is derived from the date, so
  nothing is lost between the ephemeral GitHub Actions runners.

## The daily cycle

There are **7 search terms** and **7 subreddits** in `sources.txt`. Each day the
script picks **one term** and **one subreddit**, cycling through each list in
order and repeating weekly:

| Day | Term | Subreddit |
|-----|------|-----------|
| 1 | gene editing | bioinformatics |
| 2 | genome engineering | labrats |
| 3 | "viral vector" OR "non-viral vector" | biotech |
| 4 | gene therapy | molecularbiology |
| 5 | computational biology | biochemistry |
| 6 | gene delivery | CRISPR |
| 7 | cell therapy | ClinTrials |
| 8 | gene editing | bioinformatics |

…and so on. The index is `date.toordinal() % 7`, which advances by one each day,
so each list always cycles in order regardless of how many items it holds.

**Named authors are an *addition*, not a substitution.** Every day, regardless
of which term/subreddit is in rotation, the script also checks PubMed for the
tracked authors (last 24 hours). Any paper by a tracked author is sent **in
addition to** the daily term paper and subreddit post.

## What gets delivered (and in what order)

Each item is **two messages** — one with title + short metadata, one with the
link — so the link can be copied into the RSVP app with a single tap:

1. `<paper title>, in <journal>, under <term>, by <last author> lab.`
2. `<DOI or PubMed link>`
3. …repeat (1)–(2) for each named-author paper (labelled "under named author")…
4. `<post title>, r/<subreddit>, <upvotes> upvotes, u/<author>`
5. `<post URL>`

No summaries. The only decision delegated to an LLM is *which* paper title to
pick from the day's list — everything else is mechanical.

## Selection rules (paper)

PubMed returns every paper matching the day's term from the last **7 days**
(the term's rotation period, so nothing is missed between its weekly visits).
DeepSeek then picks the single best title, biased toward:

1. **Primary research** — new experimental data, a new method, model, or
   benchmark that advances the field.
2. **Meta-analyses / systematic reviews** of primary data are acceptable and
   valued.
3. **Relevance** to the term itself (not a technique used as a tool for an
   unrelated question).

Explicitly excluded: narrative/topical reviews, perspectives, editorials,
highlights, news, and commentary.

**Fallback:** if DeepSeek is out of credit or down, the script silently uses
the first paper in the list, so the digest still goes out.

## Selection rules (Reddit)

The Apify `harshmaur/reddit-scraper` actor fetches the top posts of the day's
subreddit (comments are **not** crawled). Posts are filtered to the last **7
days** and the one with the most upvotes is kept (link/image and self posts
alike). Time filter is `week` for a 7-day window (matching the weekly
rotation).

## Failure handling

- **DeepSeek down/out of credit** → fall back to the first paper; digest still sends.
- **Apify error** → Reddit section skipped; paper section still sends.
- **PubMed returns zero papers** → paper section skipped; Reddit still sends.
- **Telegram error** → logged and re-raised; run marked failed; nothing is
  marked "sent", so the next run retries.
- **Already sent today** (`last_sent_date.txt`) → skipped without any network call.

## Directory layout

```
.github/workflows/daily-digest.yml   # cron schedule (05:00 UTC)
daily_digest.py                      # the whole app
sources.txt                          # terms, subreddits, authors
requirements.txt                     # dependencies
.env.example                         # which secrets to set
logs/daily_digest.log                # runtime log (gitignored)
last_sent_date.txt                   # daily-sent guard (gitignored)
```

## Configuration

`sources.txt` — three sections, one item per line:

```ini
[TERMS]        # PubMed search phrases (plain phrases are quoted; boolean
               # queries with OR/AND are passed through verbatim)
[SUBREDDITS]   # subreddit names without "r/"
[AUTHORS]      # author names, converted to "Name"[AU] queries
```

Secrets (set as GitHub Actions repository secrets — never committed):

```ini
DEEPSEEK_API_KEY
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
ENTREZ_EMAIL      # required by NCBI eutils
APIFY_API
```

## Running locally

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in real values
python daily_digest.py
```

## License

MIT.
