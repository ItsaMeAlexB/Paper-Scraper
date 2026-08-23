#!/usr/bin/env python3
import os
import sys
import time
import logging
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
APIFY_API_KEY = os.environ.get("APIFY_API")

LAST_SENT_FILE = "last_sent_date.txt"
CONFIG_FILE = "sources.txt"
TERM_INDEX_FILE = "term_index.txt"
SUBREDDIT_INDEX_FILE = "subreddit_index.txt"

PEAK_WINDOWS = [(1, 4), (6, 10)]
PEAK_BUFFER_MINUTES = 30

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# =============================================================================
# LOAD CONFIG
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

# =============================================================================
# INDEX MANAGEMENT
# =============================================================================

def read_index(file_path):
    if not os.path.exists(file_path):
        return 0
    try:
        with open(file_path, "r") as f:
            return int(f.read().strip())
    except:
        return 0

def write_index(file_path, value):
    with open(file_path, "w") as f:
        f.write(str(value))

def get_next_index(file_path, max_val):
    idx = read_index(file_path)
    if idx >= max_val:
        idx = 0
    return idx

def increment_index(file_path, max_val):
    idx = read_index(file_path) + 1
    if idx >= max_val:
        idx = 0
    write_index(file_path, idx)

# =============================================================================
# PEAK PRICING
# =============================================================================

def is_deepseek_peak_time(dt):
    hour = dt.hour
    for start, end in PEAK_WINDOWS:
        if start <= hour < end:
            return True
    return False

def is_within_buffer_of_peak(dt, buffer_minutes):
    hour, minute = dt.hour, dt.minute
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

def should_run():
    now = datetime.now(timezone.utc)
    if is_deepseek_peak_time(now):
        return False, f"Inside peak window (UTC {now.strftime('%H:%M')})"
    if is_within_buffer_of_peak(now, PEAK_BUFFER_MINUTES):
        return False, f"Within {PEAK_BUFFER_MINUTES}min buffer before peak"
    return True, "All checks passed"

# =============================================================================
# PUBMED
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
# DEEPSEEK
# =============================================================================

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

def deepseek_select_title(term_name, papers):
    if not papers:
        return None
    titles_text = "\n".join([f"{i+1}. {p['title']}" for i, p in enumerate(papers)])
    prompt = f"""From the following list of titles for the topic "{term_name}", pick the 1 most useful paper based on its title.

Criteria:
- Useful: e.g., a review that explains something well **and** offers a novel synthesis or perspective (not a mere summary)
- Significant: e.g., a primary article with interesting findings, a meta‑analysis, benchmarking, or a new model
- Relevant: not just an article that mentions {term_name} in passing or uses it as a method for investigating an irrelevant topic
- Avoid scoping reviews that do nothing novel; they can be included only if they present new insights

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
        content = resp.json()["choices"][0]["message"]["content"].strip()
        if content and content != "NO_TITLES":
            return content
        return None
    except Exception as e:
        logger.error(f"Selection error for {term_name}: {e}")
        return papers[0]['title'] if papers else None

def deepseek_summarize_single(title, abstract, journal, last_author, item_type="paper"):
    prompt = f"""You are a scientific summariser. Write a concise summary of the following {item_type}.

{item_type.capitalize()} details:
Title: {title}
Journal: {journal}
Last author (lab): {last_author if last_author else "N/A"}
Abstract: {abstract[:1500]}

Rules:
- Provide a concise, informative summary – aim for 4‑6 sentences, but use your judgment.
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
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"Summarisation error: {e}")
        return "⚠️ Summary generation failed."

# =============================================================================
# REDDIT (harshmaur/reddit-scraper)
# =============================================================================

def fetch_top_posts(subreddit, max_posts=3, days_back=7):
    if not APIFY_API_KEY:
        logger.error("APIFY_API secret not set")
        return []
    client = ApifyClient(APIFY_API_KEY)
    now = datetime.now(timezone.utc)
    posted_after = (now - timedelta(days=days_back)).strftime("%Y-%m-%d")
    posted_before = now.strftime("%Y-%m-%d")
    url = f"https://www.reddit.com/r/{subreddit}/top/"
    run_input = {
        "startUrls": [{"url": url}],
        "maxPostsCount": max_posts,
        "postedAfter": posted_after,
        "postedBefore": posted_before,
        "crawlCommentsPerPost": False,
        "includeNSFW": False,
        "proxy": {"useApifyProxy": True, "apifyProxyGroups": ["RESIDENTIAL"]}
    }
    try:
        run = client.actor("harshmaur/reddit-scraper").call(run_input=run_input)
        items = list(client.dataset(run.default_dataset_id).iterate_items())
        posts = [i for i in items if i.get("dataType") == "post"]
        return posts
    except Exception as e:
        logger.error(f"Reddit fetch error: {e}")
        return []

def fetch_top_comment(post_url):
    if not post_url:
        return None
    client = ApifyClient(APIFY_API_KEY)
    run_input = {
        "startUrls": [{"url": post_url}],
        "maxCommentsCount": 1,
        "crawlCommentsPerPost": True,
        "includeNSFW": False,
        "proxy": {"useApifyProxy": True, "apifyProxyGroups": ["RESIDENTIAL"]}
    }
    try:
        run = client.actor("harshmaur/reddit-scraper").call(run_input=run_input)
        items = list(client.dataset(run.default_dataset_id).iterate_items())
        comments = [i for i in items if i.get("dataType") == "comment"]
        if comments:
            comments.sort(key=lambda c: c.get("upVotes") or 0, reverse=True)
            return comments[0]
        return None
    except Exception as e:
        logger.error(f"Comment fetch error: {e}")
        return None

# =============================================================================
# TELEGRAM
# =============================================================================

def send_telegram(message):
    if not message or not message.strip():
        message = "⚠️ No digest content."
    max_len = 4000
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for i in range(0, len(message), max_len):
        chunk = message[i:i+max_len].strip()
        if not chunk:
            continue
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": chunk}
        try:
            requests.post(url, json=payload, timeout=30).raise_for_status()
            logger.info("Telegram chunk sent.")
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
        return last == datetime.now(timezone.utc).strftime("%Y-%m-%d")
    except:
        return False

def mark_sent_today():
    with open(LAST_SENT_FILE, "w") as f:
        f.write(datetime.now(timezone.utc).strftime("%Y-%m-%d"))

# =============================================================================
# MAIN
# =============================================================================

def main():
    ok, reason = should_run()
    now = datetime.now(timezone.utc)
    logger.info(f"UTC: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    if not ok:
        logger.info(f"Skipping: {reason}")
        return

    if has_sent_today():
        logger.info("Already sent today – skipping.")
        return

    # --- Rotation using persistent indices ---
    term_idx = get_next_index(TERM_INDEX_FILE, len(TERMS))
    sub_idx = get_next_index(SUBREDDIT_INDEX_FILE, len(SUBREDDITS))
    term_name = TERMS[term_idx]
    term_query = TERMS[term_idx]
    subreddit = SUBREDDITS[sub_idx]

    logger.info(f"Today: term='{term_name}' (index {term_idx}), subreddit='{subreddit}' (index {sub_idx})")

    # --- 1. Reddit: top post from last M days (M = number of subreddits) ---
    M = len(SUBREDDITS)
    posts = fetch_top_posts(subreddit, max_posts=3, days_back=M)
    valid_post = None
    for p in posts:
        if len(p.get("body", "") or "") > 0:
            valid_post = p
            break

    reddit_entry = None
    comment = None
    if valid_post:
        post_url = valid_post.get("postUrl") or valid_post.get("url")
        if post_url:
            comment = fetch_top_comment(post_url)
        reddit_entry = {
            "title": valid_post.get("title", "No title"),
            "upvotes": valid_post.get("upVotes") or valid_post.get("score") or 0,
            "subreddit": subreddit,
            "body": valid_post.get("body", ""),
            "url": post_url,
            "comment": comment["body"] if comment else None
        }

    # --- 2. PubMed: papers for term from last N days (N = number of terms) ---
    N = len(TERMS)
    start_date = now - timedelta(days=N)
    pmids = fetch_all_pmids(term_query, start_date, now)
    papers = fetch_metadata(pmids) if pmids else []
    logger.info(f"Found {len(papers)} papers for term '{term_name}'.")

    selected_paper = None
    if papers:
        selected_title = deepseek_select_title(term_name, papers)
        if selected_title:
            for p in papers:
                if p["title"].strip().lower() == selected_title.lower():
                    selected_paper = p
                    break
            if not selected_paper:
                selected_paper = papers[0]

    # --- 3. Named authors: last 1 day ---
    named_papers = []
    for auth_q in AUTHOR_QUERIES:
        auth_pmids = fetch_all_pmids(auth_q, now - timedelta(days=1), now)
        if auth_pmids:
            auth_papers = fetch_metadata(auth_pmids)
            named_papers.extend(auth_papers)

    # deduplicate against selected
    if selected_paper:
        sel_doi = selected_paper.get("doi")
        sel_pmid = selected_paper.get("pmid")
        named_papers = [p for p in named_papers if p.get("doi") != sel_doi and p.get("pmid") != sel_pmid]

    # --- 4. Build message ---
    lines = []

    # Reddit
    if reddit_entry:
        summary = deepseek_summarize_single(
            title=reddit_entry["title"],
            abstract=reddit_entry["body"][:1500],
            journal="Reddit",
            last_author="",
            item_type="subreddit post"
        )
        lines.append(f"Reddit Post: {reddit_entry['title']}")
        lines.append(f"Upvotes: {reddit_entry['upvotes']}  |  Subreddit: r/{reddit_entry['subreddit']}")
        lines.append(f"Summary: {summary}")
        if reddit_entry.get("comment"):
            comment_summary = deepseek_summarize_single(
                title="Top Comment",
                abstract=reddit_entry["comment"][:1500],
                journal="Reddit",
                last_author="",
                item_type="comment"
            )
            lines.append(f"Top Comment: {comment_summary}")
        lines.append("")

    # Selected paper
    if selected_paper:
        last_author = selected_paper['authors'][-1] if selected_paper.get('authors') else "Unknown"
        journal = selected_paper.get('journal', "Unknown")
        summary = deepseek_summarize_single(
            title=selected_paper['title'],
            abstract=selected_paper.get('abstract', ""),
            journal=journal,
            last_author=last_author,
            item_type="paper"
        )
        lines.append(f"Paper: {selected_paper['title']}")
        lines.append(f"Lab: {last_author} Lab  |  Journal: {journal}")
        lines.append(f"Summary: {summary}")
        lines.append("")

    # Named author papers
    for p in named_papers:
        last_author = p['authors'][-1] if p.get('authors') else "Unknown"
        journal = p.get('journal', "Unknown")
        summary = deepseek_summarize_single(
            title=p['title'],
            abstract=p.get('abstract', ""),
            journal=journal,
            last_author=last_author,
            item_type="paper"
        )
        lines.append(f"Named Author Paper: {p['title']}")
        lines.append(f"Lab: {last_author} Lab  |  Journal: {journal}")
        lines.append(f"Summary: {summary}")
        lines.append("")

    if not lines:
        lines = ["No content available today."]

    full_message = "\n".join(lines).strip()
    send_telegram(full_message)
    mark_sent_today()

    # Increment indices for next day
    increment_index(TERM_INDEX_FILE, len(TERMS))
    increment_index(SUBREDDIT_INDEX_FILE, len(SUBREDDITS))

    logger.info("Digest sent successfully.")

if __name__ == "__main__":
    main()
