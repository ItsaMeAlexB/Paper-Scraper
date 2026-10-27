#!/usr/bin/env python3
import os
import sys
import time
import re
import html
import logging
import traceback
from datetime import datetime, timezone, timedelta

import requests
from apify_client import ApifyClient
from dotenv import load_dotenv

# Base directory (script location)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# ----------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ENTREZ_EMAIL = os.environ.get("ENTREZ_EMAIL")
APIFY_API_KEY = os.environ.get("APIFY_API")

REQUIRED_ENV = ["DEEPSEEK_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                "ENTREZ_EMAIL", "APIFY_API"]

LAST_SENT_FILE = os.path.join(BASE_DIR, "last_sent_date.txt")
CONFIG_FILE = os.path.join(BASE_DIR, "sources.txt")
LOG_FILE = os.path.join(BASE_DIR, "logs", "daily_digest.log")
ROTATION_MARKER = os.path.join(BASE_DIR, "logs", ".last_rotation")

LOG_ROTATION_SECONDS = 7 * 24 * 3600  # 7 days

# =============================================================================
# LOGGING (weekly overwrite)
# =============================================================================

def setup_logging():
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    now = time.time()
    if os.path.exists(ROTATION_MARKER):
        try:
            with open(ROTATION_MARKER) as f:
                last = float(f.read().strip())
            if now - last > LOG_ROTATION_SECONDS:
                with open(LOG_FILE, "w"):
                    pass
                with open(ROTATION_MARKER, "w") as f:
                    f.write(str(now))
        except Exception:
            pass
    else:
        with open(ROTATION_MARKER, "w") as f:
            f.write(str(now))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler()
        ]
    )
    return logging.getLogger(__name__)

logger = setup_logging()

# =============================================================================
# CONFIG VALIDATION & LOADING
# =============================================================================

def _check_env():
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        logger.error(
            f"Missing environment variables: {', '.join(missing)}. "
            f"Copy .env.example to .env and fill it in."
        )
        sys.exit(1)

_check_env()

def load_config():
    if not os.path.exists(CONFIG_FILE):
        logger.error(
            f"Config file {CONFIG_FILE} not found. Copy sources.txt.example "
            f"to sources.txt and fill in [TERMS], [SUBREDDITS], and [AUTHORS]."
        )
        sys.exit(1)
    with open(CONFIG_FILE) as f:
        lines = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    terms, subreddits, authors = [], [], []
    current = None
    for line in lines:
        if line.startswith("[TERMS]"):
            current = "terms"
            continue
        elif line.startswith("[SUBREDDITS]"):
            current = "subreddits"
            continue
        elif line.startswith("[AUTHORS]"):
            current = "authors"
            continue
        if current == "terms":
            terms.append(line)
        elif current == "subreddits":
            subreddits.append(line)
        elif current == "authors":
            authors.append(line)

    if not terms or not subreddits:
        logger.error("Config must have at least one term and one subreddit.")
        sys.exit(1)
    return terms, subreddits, authors

TERMS, SUBREDDITS, AUTHORS = load_config()
AUTHOR_QUERIES = [f'"{a}"[AU]' for a in AUTHORS]
logger.info(f"Loaded {len(TERMS)} terms, {len(SUBREDDITS)} subreddits, {len(AUTHORS)} authors")

# =============================================================================
# PUBMED
# =============================================================================

def build_query(term):
    # Wrap plain phrases so PubMed searches them as a phrase. If the term is
    # already a boolean query (has OR/AND or quotes), pass it through verbatim.
    if ' OR ' in term or ' AND ' in term or '"' in term:
        return term
    return f'"{term}"'

# NCBI E-utilities over plain HTTP — drops the biopython (and its numpy) dependency.
ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"

def fetch_all_pmids(query, start_date, end_date, max_results=10000):
    start_str = start_date.strftime("%Y/%m/%d")
    end_str = end_date.strftime("%Y/%m/%d")
    pmids = []
    retstart = 0
    while True:
        params = {
            "db": "pubmed", "term": query,
            "mindate": start_str, "maxdate": end_str,
            "retstart": retstart, "retmax": 1000, "sort": "pub date",
            "retmode": "json", "email": ENTREZ_EMAIL,
        }
        record = requests.get(ESEARCH_URL, params=params, timeout=60).json()["esearchresult"]
        batch = record.get("idlist", [])
        if not batch:
            break
        pmids.extend(batch)
        retstart += 1000
        if retstart >= int(record.get("count", 0)) or retstart >= max_results:
            break
        time.sleep(0.3)
    return pmids

def fetch_metadata(pmids):
    if not pmids:
        return []
    papers = []
    for i in range(0, len(pmids), 200):
        chunk = pmids[i:i+200]
        params = {"db": "pubmed", "id": ",".join(chunk), "retmode": "json",
                  "email": ENTREZ_EMAIL}
        result = requests.get(ESUMMARY_URL, params=params, timeout=60).json()["result"]
        for pmid in chunk:
            rec = result.get(pmid)
            if not rec:
                continue
            authors = [a.get("name", "").strip()
                       for a in rec.get("authors", []) if a.get("name")]
            if not authors:
                continue
            doi = next((a.get("value") for a in rec.get("articleids", [])
                        if a.get("idtype") == "doi"), None)
            papers.append({
                "doi": doi,
                "title": rec.get("title", ""),
                "authors": authors,
                "journal": rec.get("fulljournalname", ""),
                "date": rec.get("pubdate", ""),
                "pmid": pmid,
            })
        time.sleep(0.3)
    return papers

# =============================================================================
# SCOPING REVIEW (single DeepSeek call)
# =============================================================================

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
# Reasoning model: the scoping-review task is judgement + prose, not structured
# JSON, so chain-of-thought is an asset rather than a distraction. max_tokens
# is not set: the reasoner picks its own output budget.
DEEPSEEK_MODEL = "deepseek-reasoner"

# Model emits [[REF:n]]; we render the anchor. Tokens are tiny, so this saves
# context and makes malformed HTML impossible by construction.
REF_RE = re.compile(r"\[\[REF:(\d+)\]\]")
TAG_RE = re.compile(r"<[^>]+>")
UNIT_RE = re.compile(r'<a [^>]*>[^<]*</a>|\s+|[^\s<]+', re.S)

def _paper_key(p):
    return p.get("pmid") or p.get("doi") or p.get("title", "").strip().lower()

def _paper_link(p):
    if p.get("doi"):
        return f"https://doi.org/{p['doi']}"
    return f"https://pubmed.ncbi.nlm.nih.gov/{p['pmid']}/"

def _last_author(p):
    return p["authors"][-1] if p.get("authors") else "Unknown"

def _deepseek_text(prompt):
    """Run one DeepSeek call and return the response text. Retries once on an
    empty response; HTTP errors propagate."""
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}",
               "Content-Type": "application/json"}
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "user", "content": prompt}],
    }
    last_err = None
    for attempt in (0, 1):
        resp = requests.post(DEEPSEEK_URL, json=payload, headers=headers, timeout=600)
        resp.raise_for_status()
        data = resp.json()
        choice = data["choices"][0]
        content = (choice["message"].get("content") or "").strip()
        if content:
            return content
        last_err = ValueError(
            f"empty content (finish_reason={choice.get('finish_reason')}, "
            f"usage={data.get('usage')})"
        )
        logger.warning(f"DeepSeek empty response (attempt {attempt+1}/2)")
        time.sleep(1)
    raise last_err

def _render_citations(text, links):
    """Escape prose, then replace [[REF:n]] with a clickable anchor labelled [n].
    Unknown refs (model hallucination) are left as plain text and logged."""
    safe = html.escape(text, quote=False)   # & < > only

    def sub(m):
        n = int(m.group(1))
        url = links.get(n)
        if not url:
            logger.warning(f"unknown reference [[REF:{n}]] in scoping review")
            return f"[{n}]"
        return f'<a href="{html.escape(url, quote=True)}">[{n}]</a>'

    return REF_RE.sub(sub, safe)

def _visible(s):
    return html.unescape(TAG_RE.sub("", s))

def _utf16_len(s):
    return len(s.encode("utf-16-le")) // 2   # Telegram counts UTF-16 units

def _chunk_html(text, limit=3800):
    """Split HTML into chunks whose visible UTF-16 length is <= limit. Anchors
    are atomic units, so a chunk can never end inside a tag."""
    chunks, cur, cur_len = [], [], 0
    for unit in UNIT_RE.findall(text):
        n = _utf16_len(_visible(unit))
        if cur and cur_len + n > limit:
            chunks.append("".join(cur).strip())
            cur, cur_len = [], 0
            if not unit.strip():        # skip leading whitespace after split
                continue
        cur.append(unit)
        cur_len += n
    if cur:
        chunks.append("".join(cur).strip())
    return chunks

def deepseek_scoping_review(term_name, papers, named_papers):
    """Write a single scoping-review paragraph over all papers. Returns
    (paragraph, {ref_number: url}) or (None, {})."""
    if not papers and not named_papers:
        return None, {}

    entries, links = [], {}

    def add(p):
        n = len(entries) + 1
        links[n] = _paper_link(p)
        entries.append(
            f"[{n}] {p['title']} — {_last_author(p)} — {p.get('journal', 'Unknown')}"
        )

    topic_start = len(entries) + 1
    for p in papers:
        add(p)
    topic_end = len(entries)

    named_start = len(entries) + 1
    for p in named_papers:
        add(p)
    named_end = len(entries)

    def section(header, lo, hi):
        if lo > hi:
            return f"{header}\n(none)"
        return header + "\n" + "\n".join(entries[lo - 1:hi])

    body = "\n\n".join([
        section(f"TOPIC-SEARCH PAPERS ({topic_start}–{topic_end})", topic_start, topic_end),
        section(f"NAMED-AUTHOR PAPERS ({named_start}–{named_end})", named_start, named_end),
    ])

    prompt = f"""You are writing a scoping review of recent gene therapy literature for a researcher who already knows the search term "{term_name}" well. The papers below are in two sections: a PubMed topic search, and papers by named authors. Summarise them in one naturally flowing paragraph.

Rules for the TOPIC-SEARCH section only — keep primary research articles relevant to gene therapy:
- Relevant means a novel or field-advancing contribution — e.g. a new Cas variant, a new vector, or a finding such as RNA-seq being unchanged by vector dosing.
- A prokaryotic mechanism is relevant only if the paper states it is transferable to mammalian cells; a protein-folding model is relevant if Cas could be engineered from it.
- Viral-vector work can be relevant, but vaccine work is not.
- CRISPR used merely as a method for an unrelated question is not relevant.
- Judge novelty relative to the field, in context. Do not be swayed by buzzwords: using CRISPR/Cas9 on a new disease is not novel per se, since many diseases have been treated in vitro; it is novel only if it is a clinical trial, a new mechanism, disease-agnostic, or similarly field-advancing.
- Exclude suspected paper mills (suspicious authors, tortured phrases, low-reputation journals, etc.).

Rules for the NAMED-AUTHOR section — these papers are MANDATORY. Every one of them must be weaved into the paragraph. They are exempt from the relevance, novelty, and paper-mill filtering above; the reader follows these labs regardless.

Length:
- Write at least 10 sentences and at most 20.
- Do not default to the upper end. Choose the length the material actually needs.
- If there are fewer than 100 papers, or few significant papers, or the material is easy to summarise briefly, stay at 10 sentences.
- Only if there are 200 or more papers, or the work requires a lot of explaining, or there are many genuinely good papers, may you use up to 20.

Style:
- Refer to each paper by its lab (last author), e.g. "the Liu lab".
- Cite ACS-style: immediately after the claim it supports and BEFORE the closing punctuation, write that paper's number in double square brackets, e.g. "…engineered a new non-viral vector [[REF:4]]."
- Use the numbers exactly as shown in the paper lists. Never write a URL, a DOI, a PMID, or any HTML.
- Assume the reader is comfortable with "{term_name}" itself, so keep technical terms rather than simplifying them away; but briefly explain any niche abbreviation, complex term, concept, or jargon in-line the first time it appears.

Papers:

{body}

Write only the paragraph. No headings, no bullet points, no preamble.
"""
    return _deepseek_text(prompt), links

# =============================================================================
# REDDIT (harshmaur/reddit-scraper)
# =============================================================================

def fetch_top_post(subreddit, days_back=7):
    """Returns (post_or_None, error_string_or_None) for the single top post."""
    if not APIFY_API_KEY:
        logger.error("APIFY_API secret not set")
        return None, "APIFY_API secret not set"
    client = ApifyClient(APIFY_API_KEY)

    if days_back <= 1:
        time_filter = "day"
    elif days_back <= 7:
        time_filter = "week"
    else:
        time_filter = "month"
    url = f"https://www.reddit.com/r/{subreddit}/top/?t={time_filter}"

    run_input = {
        "startUrls": [{"url": url}],
        "maxPostsCount": 1,
        "crawlCommentsPerPost": False,
        "includeNSFW": False,
        "fastMode": False,
        "proxy": {"useApifyProxy": True, "apifyProxyGroups": ["RESIDENTIAL"]}
    }
    try:
        run = client.actor("harshmaur/reddit-scraper").call(run_input=run_input)
        items = list(client.dataset(run.default_dataset_id).iterate_items())
        posts = [i for i in items if i.get("dataType") == "post"]
        logger.info(f"Retrieved {len(posts)} post(s) from r/{subreddit}")
        return (posts[0] if posts else None), None
    except Exception as e:
        logger.error(f"Reddit fetch error: {e}")
        return None, f"Reddit (Apify) error: {e}"

# =============================================================================
# TELEGRAM
# =============================================================================

def send_telegram(message, parse_mode=None):
    if not message or not message.strip():
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    if parse_mode:
        payload["parse_mode"] = parse_mode
        payload["disable_web_page_preview"] = True
    try:
        requests.post(url, json=payload, timeout=30).raise_for_status()
        logger.info(f"Telegram sent: {_visible(message)[:60]}")
    except Exception as e:
        logger.error(f"Telegram error: {e}")
        raise

# =============================================================================
# DAILY STATE
# =============================================================================

def days_since_last_sent():
    """None = never sent. 0 = sent today (skip). N>=1 = N-1 days missed."""
    if not os.path.exists(LAST_SENT_FILE):
        return None
    try:
        with open(LAST_SENT_FILE) as f:
            last = datetime.strptime(f.read().strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc).date() - last.date()).days)
    except Exception:
        return None

def mark_sent_today():
    with open(LAST_SENT_FILE, "w") as f:
        f.write(datetime.now(timezone.utc).strftime("%Y-%m-%d"))

# =============================================================================
# ONE DIGEST RUN
# =============================================================================

def run_digest(run_date, now):
    """One digest run. run_date picks the rotation; windows reach back to
    run_date - rotation_length, ending at run_date (for catch-up) or at now
    (for today). Returns a list of problems."""
    days_late = (now.date() - run_date.date()).days
    is_today = (days_late == 0)
    end_date = now if is_today else run_date
    problems = []

    term_idx = run_date.toordinal() % len(TERMS)
    sub_idx = run_date.toordinal() % len(SUBREDDITS)
    term_name = TERMS[term_idx]
    term_query = build_query(term_name)
    subreddit = SUBREDDITS[sub_idx]
    logger.info(f"[{run_date.date()}] term[{term_idx}]='{term_name}', "
                f"sub[{sub_idx}]='{subreddit}', days_late={days_late}")

    if not is_today:
        send_telegram(f"📅 Catch-up digest for {run_date.strftime('%Y-%m-%d')}")

    # Reddit: catch-up steps up from week to month tier.
    M = len(SUBREDDITS)
    days_back = M if is_today else 8
    valid_post, apify_err = fetch_top_post(subreddit, days_back=days_back)
    if apify_err:
        problems.append(apify_err)
    if valid_post:
        logger.info(f"[{run_date.date()}] Reddit: '{valid_post.get('title')}' "
                    f"({valid_post.get('upVotes')} upvotes)")
    else:
        problems.append(f"Reddit: no post found for r/{subreddit}")

    # PubMed: term search ends at end_date so today's papers aren't doubled.
    N = len(TERMS)
    term_start = run_date - timedelta(days=N)
    pmids = fetch_all_pmids(term_query, term_start, end_date)
    papers = fetch_metadata(pmids) if pmids else []
    logger.info(f"[{run_date.date()}] {len(papers)} papers for '{term_name}'")
    if not papers:
        problems.append(f"PubMed: zero papers for term '{term_name}'")

    # Named authors (last day of the run's window).
    auth_start = run_date - timedelta(days=1)
    named_papers = []
    for auth_q in AUTHOR_QUERIES:
        auth_pmids = fetch_all_pmids(auth_q, auth_start, end_date)
        if auth_pmids:
            named_papers.extend(fetch_metadata(auth_pmids))
    seen = {_paper_key(p) for p in papers}
    named_papers = [p for p in named_papers if _paper_key(p) not in seen]
    logger.info(f"[{run_date.date()}] {len(named_papers)} named-author papers")

    # Scoping review.
    summary, links = None, {}
    if papers or named_papers:
        try:
            summary, links = deepseek_scoping_review(term_name, papers, named_papers)
        except Exception as e:
            logger.error(f"DeepSeek scoping-review error: {e}")
            problems.append(f"Scoping review error: {e}")

    if summary:
        for chunk in _chunk_html(_render_citations(summary, links)):
            send_telegram(chunk, parse_mode="HTML")
    else:
        problems.append("Scoping review: no summary produced")

    # Reddit message: just the post link.
    if valid_post:
        post_url = valid_post.get("postUrl") or valid_post.get("url")
        if post_url:
            send_telegram(post_url)
        else:
            send_telegram(valid_post.get("title", "No title"))

    return problems

# =============================================================================
# MAIN
# =============================================================================

def main():
    try:
        now = datetime.now(timezone.utc)
        logger.info(f"UTC: {now.strftime('%Y-%m-%d %H:%M:%S')}")

        # Deliver only within the 12:00-24:00 UTC window. A periodic cron tick
        # retries, so the first tick the laptop is available in-window sends it.
        if not (12 <= now.hour < 24):
            logger.info(f"Outside 12:00-24:00 UTC window (hour {now.hour}); will retry later")
            return

        days_since = days_since_last_sent()
        if days_since == 0:
            logger.info("Already sent today, skipping")
            return

        if days_since is None or days_since == 1:
            run_dates = [now]                                     # normal / first run
        elif days_since == 2:
            run_dates = [now - timedelta(days=1), now]            # one catch-up
        else:
            logger.info(f"{days_since - 1} day(s) missed — replaying today only; "
                        f"backlog ignored")
            run_dates = [now]

        logger.info(f"Runs: {[d.strftime('%Y-%m-%d') for d in run_dates]}")

        all_problems = []
        for run_date in run_dates:
            try:
                all_problems.extend(run_digest(run_date, now))
            except Exception as e:
                logger.error(f"Run {run_date.date()} crashed: {e}")
                logger.error(traceback.format_exc())
                all_problems.append(f"Run {run_date.date()}: {e}")

        if all_problems:
            send_telegram("⚠️ Digest issues:\n- " + "\n- ".join(all_problems))

        mark_sent_today()
        logger.info("All digests sent.")

    except Exception as e:
        logger.error(f"Unhandled exception: {e}")
        logger.error(traceback.format_exc())
        raise

if __name__ == "__main__":
    main()
