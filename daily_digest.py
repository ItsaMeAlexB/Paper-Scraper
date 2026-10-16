#!/usr/bin/env python3
import os
import sys
import time
import json
import random
import logging
import traceback
from datetime import datetime, timezone, timedelta

import requests
from Bio import Entrez, Medline
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
# CONFIG LOADING
# =============================================================================

def load_config():
    if not os.path.exists(CONFIG_FILE):
        logger.error(f"Config file {CONFIG_FILE} not found.")
        sys.exit(1)
    with open(CONFIG_FILE, "r") as f:
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
# ROTATION (deterministic by date)
# =============================================================================
# GitHub Actions runs on an ephemeral filesystem, so persistent index files
# are lost between runs. Deriving the index from the day number rotates each
# list in order for any length, every N days, with no stored state.
def rotation_index(list_len):
    return datetime.now(timezone.utc).toordinal() % list_len

# =============================================================================
# PUBMED
# =============================================================================

def build_query(term):
    # Wrap plain phrases so PubMed searches them as a phrase. If the term is
    # already a boolean query (has OR/AND or quotes), pass it through verbatim.
    if ' OR ' in term or ' AND ' in term or '"' in term:
        return term
    return f'"{term}"'

def fetch_all_pmids(query, start_date, end_date, max_results=10000):
    Entrez.email = ENTREZ_EMAIL
    start_str = start_date.strftime("%Y/%m/%d")
    end_str = end_date.strftime("%Y/%m/%d")
    pmids = []
    retstart = 0
    retmax = 1000
    while True:
        handle = Entrez.esearch(db="pubmed", term=query,
                                mindate=start_str, maxdate=end_str,
                                retstart=retstart, retmax=retmax, sort="pub date")
        record = Entrez.read(handle)
        handle.close()
        batch = record["IdList"]
        if not batch:
            break
        pmids.extend(batch)
        retstart += retmax
        if retstart >= int(record["Count"]) or retstart >= max_results:
            break
        time.sleep(0.3)
    return pmids

def fetch_metadata(pmids):
    if not pmids:
        return []
    Entrez.email = ENTREZ_EMAIL
    records = []
    for i in range(0, len(pmids), 200):
        chunk = pmids[i:i+200]
        handle = Entrez.efetch(db="pubmed", id=",".join(chunk), rettype="medline", retmode="text")
        recs = list(Medline.parse(handle))
        handle.close()
        records.extend(recs)
        time.sleep(0.3)

    papers = []
    for rec in records:
        authors = rec.get("AU", [])
        if not authors:
            continue
        doi = None
        for id_ in rec.get("AID", []):
            if id_.endswith(" [doi]"):
                doi = id_.replace(" [doi]", "")
                break
        papers.append({
            "doi": doi,
            "title": rec.get("TI", ""),
            "abstract": rec.get("AB", ""),
            "authors": authors,
            "journal": rec.get("JT", ""),
            "date": rec.get("DP", ""),
            "pmid": rec.get("PMID", ""),
        })
    return papers

# =============================================================================
# TITLE SELECTION (DeepSeek scoring)
# =============================================================================

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
# Non-reasoning model: no chain-of-thought tokens competing with the answer,
# so structured JSON output is reliable for large batched scoring.
DEEPSEEK_MODEL = "deepseek-chat"
# [rel, sig] pairs are ~5-7 tokens each; this budget comfortably covers very
# large batches (hundreds of papers) with headroom.
SCORE_MAX_TOKENS = 16000

def _score_papers(papers, prompt, api_key, url, model):
    """Ask the model to score each paper; returns list of (relevance, significance)."""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": SCORE_MAX_TOKENS,
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=240)
    resp.raise_for_status()
    data = resp.json()
    choice = data["choices"][0]
    msg = choice["message"]
    content = (msg.get("content") or "").strip()
    if not content:
        raise ValueError(
            f"empty content from model "
            f"(finish_reason={choice.get('finish_reason')}, usage={data.get('usage')})"
        )
    # Model may wrap the JSON in fences or prose; extract the first JSON array.
    start = content.find("[")
    end = content.rfind("]")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"no JSON array in response: {content[:200]}")
    arr = json.loads(content[start:end + 1])
    if not isinstance(arr, list) or len(arr) != len(papers):
        raise ValueError(f"expected {len(papers)} scores, got {len(arr) if isinstance(arr, list) else type(arr)}")
    out = []
    for pair in arr:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError(f"malformed pair: {pair!r}")
        rel, sig = pair
        # Clamp and coerce to int 0-10.
        rel = min(10, max(0, int(float(rel))))
        sig = min(10, max(0, int(float(sig))))
        out.append((rel, sig))
    return out

def deepseek_select_title(term_name, papers):
    """Score each paper on relevance and significance, pick the best product.

    Returns (selected_title_or_None, note_or_None). The model returns, per title
    (same order), a [relevance, significance] pair each 0-10 (both 0 if not a
    primary source). The winner is the highest relevance*significance product;
    ties are broken randomly. On any failure we fall back to the first paper.
    """
    if not papers:
        return None, None
    titles_text = "\n".join([f"{i+1}. {p['title']}" for i, p in enumerate(papers)])
    prompt = f"""You are scoring papers for a researcher who reads primary sources.

For each title below (topic: "{term_name}"), output exactly one JSON array of
[relevance, significance] pairs, in the SAME order as the titles, nothing else.

Scoring rules (each score is an integer 0-10):
- relevance: how directly the paper is about "{term_name}" itself (not just using it as a method or just mentioning it in passing), AND how directly it pertains to human. Non-human, non-human cell, and non-animal model work scores low here unless described as translational to humans (they may say "would also work in eukarya", which is relevant).
- significance: how much the paper advances the field (new data, new method, new model, or a landmark benchmark/meta-analysis). If the paper is NOT a primary source (review, editorial, news, etc), it scores lowely on significance. Suspected paper mills (possibly identified by the publising journal's reputation), should also be scored lowely.

Titles:
{titles_text}

Return only a JSON array like [[rel,sig],[rel,sig],...] with exactly {len(papers)} entries, one per title, in order. No prose, no code fences.
"""
    if not DEEPSEEK_API_KEY:
        return papers[0]['title'], "DEEPSEEK_API_KEY not set; used first paper"
    try:
        scores = _score_papers(papers, prompt, DEEPSEEK_API_KEY, DEEPSEEK_URL, DEEPSEEK_MODEL)
    except Exception as e:
        logger.error(f"DeepSeek scoring error: {e}")
        return papers[0]['title'], f"DeepSeek scoring error ({e}); used first paper"

    products = [r * s for r, s in scores]
    best = max(products)
    # Random tie-break among all papers with the top product.
    winners = [i for i, p in enumerate(products) if p == best]
    idx = random.choice(winners)
    logger.info(
        f"DeepSeek scored {len(papers)} papers; winner #{idx+1} "
        f"(rel={scores[idx][0]}, sig={scores[idx][1]}, product={best}, tied={len(winners)}): "
        f"{papers[idx]['title']}"
    )
    return papers[idx]['title'], None

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

def send_telegram(message):
    if not message or not message.strip():
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, json=payload, timeout=30).raise_for_status()
        logger.info(f"Telegram sent: {message[:60]}")
    except Exception as e:
        logger.error(f"Telegram error: {e}")
        raise

# =============================================================================
# DAILY STATE
# =============================================================================

def has_sent_today():
    if not os.path.exists(LAST_SENT_FILE):
        return False
    try:
        with open(LAST_SENT_FILE, "r") as f:
            last = f.read().strip()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return last == today
    except Exception:
        return False

def mark_sent_today():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with open(LAST_SENT_FILE, "w") as f:
        f.write(today)

# =============================================================================
# MAIN
# =============================================================================

def main():
    problems = []
    try:
        now = datetime.now(timezone.utc)
        logger.info(f"UTC: {now.strftime('%Y-%m-%d %H:%M:%S')}")

        if has_sent_today():
            logger.info("Already sent today, skipping")
            return

        # Rotation
        term_idx = rotation_index(len(TERMS))
        sub_idx = rotation_index(len(SUBREDDITS))
        term_name = TERMS[term_idx]
        term_query = build_query(term_name)
        subreddit = SUBREDDITS[sub_idx]
        logger.info(f"Term[{term_idx}]='{term_name}', Subreddit[{sub_idx}]='{subreddit}'")

        # --- 1. Reddit top post ---
        M = len(SUBREDDITS)
        valid_post, apify_err = fetch_top_post(subreddit, days_back=M)
        if apify_err:
            problems.append(apify_err)
        if valid_post:
            logger.info(f"Reddit: '{valid_post.get('title')}' ({valid_post.get('upVotes')} upvotes)")
        else:
            problems.append(f"Reddit: no post found for r/{subreddit}")

        # --- 2. PubMed for term (last N days) ---
        N = len(TERMS)
        start_date = now - timedelta(days=N)
        pmids = fetch_all_pmids(term_query, start_date, now)
        papers = fetch_metadata(pmids) if pmids else []
        logger.info(f"Found {len(papers)} papers for '{term_name}'")

        selected_paper = None
        if papers:
            selected_title, sel_note = deepseek_select_title(term_name, papers)
            if sel_note:
                problems.append(f"Paper selection: {sel_note}")
            if selected_title:
                for p in papers:
                    if p["title"].strip().lower() == selected_title.lower():
                        selected_paper = p
                        break
                if not selected_paper:
                    selected_paper = papers[0]
            else:
                selected_paper = papers[0]
        else:
            problems.append(f"PubMed: zero papers for term '{term_name}'")

        # --- 3. Named authors (last 1 day) ---
        named_papers = []
        for auth_q in AUTHOR_QUERIES:
            auth_pmids = fetch_all_pmids(auth_q, now - timedelta(days=1), now)
            if auth_pmids:
                named_papers.extend(fetch_metadata(auth_pmids))
        if selected_paper:
            sel_doi = selected_paper.get("doi")
            sel_pmid = selected_paper.get("pmid")
            named_papers = [p for p in named_papers if p.get("doi") != sel_doi and p.get("pmid") != sel_pmid]
        logger.info(f"Named-author papers: {len(named_papers)}")

        # --- 4. Send Telegram messages ---
        # Selected paper info
        if selected_paper:
            last_author = selected_paper['authors'][-1] if selected_paper.get('authors') else "Unknown"
            journal = selected_paper.get('journal', "Unknown")
            send_telegram(f"{selected_paper['title']}, in {journal}, under {term_name}, by {last_author} lab.")
            if selected_paper.get("doi"):
                link = f"https://doi.org/{selected_paper['doi']}"
            elif selected_paper.get("pmid"):
                link = f"https://pubmed.ncbi.nlm.nih.gov/{selected_paper['pmid']}/"
            else:
                link = "No link available"
            send_telegram(link)

        # Named author papers
        for p in named_papers:
            last_author = p['authors'][-1] if p.get('authors') else "Unknown"
            journal = p.get('journal', "Unknown")
            send_telegram(f"{p['title']}, in {journal}, under named author, by {last_author} lab.")
            if p.get("doi"):
                link = f"https://doi.org/{p['doi']}"
            elif p.get("pmid"):
                link = f"https://pubmed.ncbi.nlm.nih.gov/{p['pmid']}/"
            else:
                link = "No link available"
            send_telegram(link)

        # Reddit post
        if valid_post:
            title = valid_post.get("title", "No title")
            upvotes = valid_post.get("upVotes") or valid_post.get("score") or 0
            author = valid_post.get("authorName", "unknown")
            post_url = valid_post.get("postUrl") or valid_post.get("url")
            send_telegram(f"{title}, r/{subreddit}, {upvotes} upvotes, u/{author}")
            if post_url:
                send_telegram(post_url)

        # --- 5. Report any issues as a separate message ---
        if problems:
            send_telegram("⚠️ Digest issues:\n- " + "\n- ".join(problems))

        # Mark sent
        mark_sent_today()
        logger.info("Digest sent successfully.")

    except Exception as e:
        logger.error(f"Unhandled exception: {e}")
        logger.error(traceback.format_exc())
        raise

if __name__ == "__main__":
    main()
