#!/usr/bin/env python3
import os
import sys
import re
import time
import json
import logging
import random
from datetime import datetime, timezone, timedelta

import requests
from Bio import Entrez, Medline
from apify_client import ApifyClient

# ----------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ENTREZ_EMAIL = os.environ.get("ENTREZ_EMAIL")
APIFY_API_KEY = os.environ.get("APIFY_API")   # secret name is APIFY_API

# Mode: "weekly" or "daily" – set via environment variable
MODE = os.environ.get("DAILY_MODE", "daily").lower()

# File to track queue for daily consumption (built weekly)
QUEUE_FILE = "weekly_queue.json"
LAST_SENT_FILE = "last_sent_date.txt"

# DeepSeek peak pricing windows in UTC
PEAK_WINDOWS = [(1, 4), (6, 10)]
PEAK_BUFFER_MINUTES = 30

# Search terms – now 7
TERMS = {
    "Gene Editing": '"gene editing"',
    "Genome Engineering": '"genome engineering"',
    "Vectors": '"viral vector" OR "non-viral vector"',
    "Gene Therapy": '"gene therapy"',
    "Computational Biology": '"computational biology"',
    "Gene Delivery": '"gene delivery"',
    "Cell Therapy": '"cell therapy"',
}

# Named authors
AUTHOR_TERMS = [
    '"David R Liu"[AU]',
    '"Ciaran M Lee"[AU]',
]

# Subreddits
SUBREDDITS = [
    "bioinformatics",
    "labrats",
    "biotech",
    "molecularbiology",
    "biochemistry",
    "CRISPR",
    "ClinTrials",
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# =============================================================================
# PEAK PRICING CHECKS
# =============================================================================

def is_deepseek_peak_time(dt: datetime) -> bool:
    hour = dt.hour
    for start, end in PEAK_WINDOWS:
        if start <= hour < end:
            return True
    return False

def is_within_buffer_of_peak(dt: datetime, buffer_minutes: int) -> bool:
    hour = dt.hour
    minute = dt.minute
    for start_hour, _ in PEAK_WINDOWS:
        if hour < start_hour:
            minutes_until = (start_hour - hour) * 60 - minute
        elif hour == start_hour:
            minutes_until = -minute
        else:
            continue
        if 0 < minutes_until <= buffer_minutes:
            return True
    return False

def should_run() -> tuple[bool, str]:
    now = datetime.now(timezone.utc)
    if is_deepseek_peak_time(now):
        return False, f"Inside peak window (UTC {now.strftime('%H:%M')})"
    if is_within_buffer_of_peak(now, PEAK_BUFFER_MINUTES):
        return False, f"Within {PEAK_BUFFER_MINUTES}min buffer before peak (UTC {now.strftime('%H:%M')})"
    return True, "All checks passed — running now!"

# =============================================================================
# PUBMED FUNCTIONS
# =============================================================================

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
# DEEPSEEK API FUNCTIONS
# =============================================================================

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

def deepseek_select_title_for_term(term_name, papers):
    """Select the most significant title for a single search term."""
    if not papers:
        return "NO_TITLES"

    titles_text = "\n".join([f"{i+1}. {p['title']}" for i, p in enumerate(papers)])
    prompt = f"""From the following list of titles for the topic "{term_name}", pick the 1 most useful paper based on its title.

Criteria:
- Useful: e.g., a review that explains something well
- Significant: e.g., a primary article with interesting findings
- Relevant: not just an article that mentions {term_name} in passing or uses it as a method for investigating an irrelevant topic

Titles:
{titles_text}

Return only the exact title as it appears, nothing else.
If the list is empty, return "NO_TITLES".
"""
    payload = {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 500,
    }
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    try:
        resp = requests.post(DEEPSEEK_URL, json=payload, headers=headers, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"].strip()
        return content if content else "NO_TITLES"
    except Exception as e:
        logger.error(f"Selection error for {term_name}: {e}")
        # Fallback: return first title
        return papers[0]['title'] if papers else "NO_TITLES"

def deepseek_summarize_single(title, abstract, journal, last_author, item_type="paper"):
    """Generate a 5‑sentence summary for a single item (paper or subreddit post)."""
    prompt = f"""You are a scientific summariser. Write a concise 5‑sentence summary of the following {item_type}.

{item_type.capitalize()} details:
Title: {title}
Journal: {journal}
Last author (lab): {last_author if last_author else "N/A"}
Abstract: {abstract[:1500]}

Rules:
- Provide exactly 5 sentences.
- Capture key findings, significance, and context.
- Preserve ambiguous terms verbatim – do not guess.
- If you cannot determine meaning, err on the side of inclusion.
- Output only the summary text, nothing else.
"""
    payload = {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 500,
    }
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    try:
        resp = requests.post(DEEPSEEK_URL, json=payload, headers=headers, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"Summarisation error: {e}")
        return "⚠️ Summary generation failed."

# =============================================================================
# SUBREDDIT FETCH (via Apify)
# =============================================================================

def fetch_top_reddit_posts(subreddits):
    """Fetch exactly one top weekly post per subreddit."""
    if not APIFY_API_KEY:
        logger.error("APIFY_API secret not set – cannot fetch Reddit posts.")
        return []
    client = ApifyClient(APIFY_API_KEY)
    posts = []
    for sub in subreddits:
        url = f"https://www.reddit.com/r/{sub}/top/?t=week"
        run_input = {
            "startUrls": [{"url": url}],
            "maxItems": 1,
            "includeMediaLinks": True,
            "proxy": {"useApifyProxy": True}
        }
        try:
            run = client.actor("trudax/reddit-scraper-lite").call(run_input=run_input)
            for item in client.dataset(run["defaultDatasetId"]).iterate_items():
                if item.get("dataType") == "post":
                    posts.append(item)
                    break
        except Exception as e:
            logger.error(f"Apify error for {sub}: {e}")
        time.sleep(0.5)  # be gentle
    posts.sort(key=lambda x: x.get("upVotes", 0), reverse=True)
    return posts

# =============================================================================
# QUEUE MANAGEMENT
# =============================================================================

def load_queue():
    if os.path.exists(QUEUE_FILE):
        try:
            with open(QUEUE_FILE, "r") as f:
                data = json.load(f)
                return data.get("entries", [])
        except (json.JSONDecodeError, IOError):
            logger.warning("Could not load queue file, starting fresh.")
            return []
    return []

def save_queue(entries):
    data = {"week_start": datetime.now(timezone.utc).isoformat(), "entries": entries}
    with open(QUEUE_FILE, "w") as f:
        json.dump(data, f, indent=2)

def has_sent_today():
    if not os.path.exists(LAST_SENT_FILE):
        return False
    try:
        with open(LAST_SENT_FILE, "r") as f:
            last_date = f.read().strip()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return last_date == today
    except:
        return False

def mark_sent_today():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with open(LAST_SENT_FILE, "w") as f:
        f.write(today)

# =============================================================================
# TELEGRAM FUNCTIONS
# =============================================================================

def send_telegram(message):
    if not message or not message.strip():
        message = "⚠️ No digest content was generated."
    max_len = 4000
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for i in range(0, len(message), max_len):
        chunk = message[i:i+max_len].strip()
        if not chunk:
            continue
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": chunk}
        try:
            resp = requests.post(url, json=payload, timeout=30)
            resp.raise_for_status()
            logger.info(f"Telegram chunk {i//max_len + 1} sent.")
        except Exception as e:
            logger.error(f"Telegram send error: {e}")
            raise

# =============================================================================
# WEEKLY BUILD
# =============================================================================

def build_weekly_queue():
    now = datetime.now(timezone.utc)
    start_date = now - timedelta(days=7)
    end_date = now

    # 1. Fetch subreddit posts
    logger.info("Fetching top weekly posts from subreddits...")
    reddit_posts = fetch_top_reddit_posts(SUBREDDITS)
    logger.info(f"Got {len(reddit_posts)} subreddit posts.")

    # 2. Fetch PubMed papers per term
    term_papers = {}
    for section_name, query in TERMS.items():
        logger.info(f"Fetching papers for {section_name}...")
        pmids = fetch_all_pmids(query, start_date, end_date)
        logger.info(f"Found {len(pmids)} raw results.")
        if pmids:
            papers = fetch_metadata(pmids)
            logger.info(f"Kept {len(papers)} papers (with authors).")
            term_papers[section_name] = papers
        else:
            term_papers[section_name] = []

    # 3. Deduplicate across search terms by DOI/PMID/title
    all_papers = []
    seen_ids = set()
    for section, papers in term_papers.items():
        for p in papers:
            key = p.get("doi") or p.get("pmid")
            if key and key not in seen_ids:
                seen_ids.add(key)
                all_papers.append(p)
            elif not key:
                all_papers.append(p)

    unique_by_title = {}
    for p in all_papers:
        title = p["title"].strip().lower()
        if title not in unique_by_title:
            unique_by_title[title] = p
    all_papers = list(unique_by_title.values())

    logger.info(f"After deduplication: {len(all_papers)} unique papers.")

    # 4. Select one per term – one DeepSeek call per term
    term_picked_papers = {}
    for term_name, papers in term_papers.items():
        if not papers:
            term_picked_papers[term_name] = None
            continue
        selected_title = deepseek_select_title_for_term(term_name, papers)
        if selected_title == "NO_TITLES":
            term_picked_papers[term_name] = None
            continue
        # Find matching paper in all_papers
        matched = None
        for p in all_papers:
            if p["title"].strip().lower() == selected_title.lower():
                matched = p
                break
        if matched:
            term_picked_papers[term_name] = matched
        else:
            # Fallback: first paper from the original list
            term_picked_papers[term_name] = papers[0] if papers else None

    # 5. Fetch named author papers
    author_papers = []
    for author_query in AUTHOR_TERMS:
        logger.info(f"Fetching papers for {author_query}...")
        pmids = fetch_all_pmids(author_query, start_date, end_date)
        if pmids:
            papers = fetch_metadata(pmids)
            author_papers.extend(papers)

    # Deduplicate author papers against selected
    selected_dois = {p.get("doi") for p in term_picked_papers.values() if p and p.get("doi")}
    selected_pmids = {p.get("pmid") for p in term_picked_papers.values() if p and p.get("pmid")}
    unique_author_papers = []
    for p in author_papers:
        key = p.get("doi") or p.get("pmid")
        if key and key not in selected_dois and key not in selected_pmids:
            unique_author_papers.append(p)
        elif not key:
            title = p["title"].strip().lower()
            if title not in {ap["title"].strip().lower() for ap in term_picked_papers.values() if ap}:
                unique_author_papers.append(p)

    # 6. Build queue
    paper_items = [("term", p) for p in term_picked_papers.values() if p is not None]
    for p in unique_author_papers:
        paper_items.append(("author", p))

    if not reddit_posts:
        logger.warning("No subreddit posts fetched, using dummy placeholder.")
        reddit_posts = [{"title": "No top post found", "body": "", "url": "", "upVotes": 0}]

    # Create entries: one per subreddit post, assign papers in order, extra papers round‑robin
    entries_with_papers = []
    for i, post in enumerate(reddit_posts):
        entry_papers = []
        if i < len(paper_items):
            entry_papers.append(paper_items[i])
        entries_with_papers.append({"subreddit_post": post, "papers": entry_papers})

    extra_papers = paper_items[len(reddit_posts):]
    for idx, paper in enumerate(extra_papers):
        target_idx = idx % len(entries_with_papers)
        entries_with_papers[target_idx]["papers"].append(paper)

    final_queue = []
    for entry in entries_with_papers:
        final_queue.append({
            "subreddit_post": entry["subreddit_post"],
            "papers": entry["papers"]  # list of (type, paper) tuples
        })

    save_queue(final_queue)
    logger.info(f"Built weekly queue with {len(final_queue)} daily entries.")

# =============================================================================
# DAILY SEND
# =============================================================================

def send_daily_digest():
    if has_sent_today():
        logger.info("Already sent today – skipping.")
        return

    entries = load_queue()
    if not entries:
        logger.info("Queue is empty – no digest to send.")
        return

    entry = entries.pop(0)
    subreddit_post = entry.get("subreddit_post")
    papers = entry.get("papers", [])

    digest_parts = []

    # Subreddit post
    if subreddit_post:
        title = subreddit_post.get("title", "No title")
        body = subreddit_post.get("body", "")
        url = subreddit_post.get("url", "")
        upvotes = subreddit_post.get("upVotes", 0)
        subreddit_name = subreddit_post.get("communityName", "unknown")
        # Summarize using DeepSeek (no lab/journal for reddit)
        summary = deepseek_summarize_single(
            title=title,
            abstract=body[:1500],
            journal="Reddit",
            last_author="",
            item_type="subreddit post"
        )
        digest_parts.append(f"Title: {title}\nUpvotes: {upvotes}\nSubreddit: {subreddit_name}\nSummary: {summary}\n")

    # Papers
    for paper_type, paper in papers:
        if not paper:
            continue
        last_author = paper['authors'][-1] if paper.get('authors') else "Unknown"
        journal = paper.get('journal', "Unknown journal")
        summary = deepseek_summarize_single(
            title=paper["title"],
            abstract=paper["abstract"],
            journal=journal,
            last_author=last_author,
            item_type="paper"
        )
        if paper_type == "author":
            digest_parts.append(f"Named Author: {paper['title']}\nLab: {last_author} Lab\nJournal: {journal}\nSummary: {summary}\n")
        else:
            digest_parts.append(f"Title: {paper['title']}\nLab: {last_author} Lab\nJournal: {journal}\nSummary: {summary}\n")

    full_message = "\n".join(digest_parts)
    if not full_message.strip():
        full_message = "No content available for today."

    send_telegram(full_message)
    mark_sent_today()
    save_queue(entries)
    logger.info("Daily digest sent and queue updated.")

# =============================================================================
# MAIN
# =============================================================================

def main():
    ok, reason = should_run()
    now = datetime.now(timezone.utc)
    logger.info(f"Current UTC time: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    if not ok:
        logger.info(f"Skipping run: {reason}")
        return

    if MODE == "weekly":
        logger.info("Running weekly build mode...")
        build_weekly_queue()
    else:
        logger.info("Running daily send mode...")
        send_daily_digest()

if __name__ == "__main__":
    main()
