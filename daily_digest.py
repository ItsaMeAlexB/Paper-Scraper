#!/usr/bin/env python3
import os
import sys
import time
import logging
import traceback
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

logger.info(f"Loaded {len(TERMS)} terms, {len(SUBREDDITS)} subreddits, {len(AUTHORS)} authors")

# =============================================================================
# INDEX MANAGEMENT
# =============================================================================

def read_index(file_path):
    if not os.path.exists(file_path):
        logger.info(f"Index file {file_path} not found, starting at 0")
        return 0
    try:
        with open(file_path, "r") as f:
            return int(f.read().strip())
    except Exception as e:
        logger.warning(f"Could not read {file_path}: {e}, resetting to 0")
        return 0

def write_index(file_path, value):
    with open(file_path, "w") as f:
        f.write(str(value))
    logger.debug(f"Wrote {value} to {file_path}")

def get_next_index(file_path, max_val):
    idx = read_index(file_path)
    if idx >= max_val:
        idx = 0
    logger.info(f"Current index from {file_path}: {idx}")
    return idx

def increment_index(file_path, max_val):
    idx = read_index(file_path) + 1
    if idx >= max_val:
        idx = 0
    write_index(file_path, idx)
    logger.info(f"Incremented {file_path} to {idx}")

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
    logger.info(f"Peak check at {now.strftime('%H:%M')} UTC")
    if is_deepseek_peak_time(now):
        return False, f"Inside peak window (UTC {now.strftime('%H:%M')})"
    if is_within_buffer_of_peak(now, PEAK_BUFFER_MINUTES):
        return False, f"Within {PEAK_BUFFER_MINUTES}min buffer before peak"
    return True, "All checks passed"

# =============================================================================
# PUBMED
# =============================================================================

def fetch_all_pmids(query, start_date, end_date, max_results=10000):
    logger.info(f"Querying PubMed: '{query}' from {start_date} to {end_date}")
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
    logger.info(f"Found {len(pmids)} PMIDs")
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
    logger.info(f"Fetched metadata for {len(papers)} papers")
    return papers

# =============================================================================
# DEEPSEEK
# =============================================================================

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

def deepseek_select_title(term_name, papers):
    logger.info(f"Selecting title for term '{term_name}' from {len(papers)} papers")
    if not papers:
        logger.warning("No papers to select from")
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
            logger.info(f"DeepSeek selected title: {content}")
            return content
        else:
            logger.warning("DeepSeek returned NO_TITLES or empty")
            return None
    except Exception as e:
        logger.error(f"Selection error: {e}")
        return papers[0]['title'] if papers else None

def deepseek_summarize_paper(title, abstract, journal, last_author):
    prompt = f"""You are a scientific summariser. Write a concise summary of the following paper in exactly 4 sentences.

Paper details:
Title: {title}
Journal: {journal}
Last author (lab): {last_author if last_author else "N/A"}
Abstract: {abstract[:1500]}

Rules:
- Exactly 4 sentences.
- Capture key findings, significance, and context.
- Preserve ambiguous terms verbatim – do not guess.
- If you cannot determine meaning, err on the side of inclusion.
- Output only the summary text, nothing else.
"""
    payload = {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 400,
    }
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    try:
        resp = requests.post(DEEPSEEK_URL, json=payload, headers=headers, timeout=60)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"Paper summarisation error: {e}")
        return "⚠️ Summary generation failed."

def deepseek_summarize_reddit(title, body, subreddit, upvotes, comment_body, comment_upvotes):
    prompt = f"""You are a scientific summariser. Write a concise summary of the following Reddit post and its top comment in exactly 4 sentences.

Reddit post details:
Title: {title}
Subreddit: r/{subreddit}
Upvotes: {upvotes}
Post body: {body}
Top comment: {comment_body}
Top comment upvotes: {comment_upvotes}

Rules:
- Exactly 4 sentences.
- Summarise the main question, discussion, or insight.
- Include the context of the post and the key point from the top comment.
- Preserve ambiguous terms verbatim – do not guess.
- If you cannot determine meaning, err on the side of inclusion.
- Output only the summary text, nothing else.
"""
    payload = {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 400,
    }
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    try:
        resp = requests.post(DEEPSEEK_URL, json=payload, headers=headers, timeout=60)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"Reddit summarisation error: {e}")
        return "⚠️ Summary generation failed."

# =============================================================================
# REDDIT (harshmaur/reddit-scraper)
# =============================================================================

def fetch_top_posts(subreddit, days_back=7, max_posts=50):
    logger.info(f"Fetching top posts from r/{subreddit} for the last {days_back} days")
    if not APIFY_API_KEY:
        logger.error("APIFY_API secret not set")
        return []
    client = ApifyClient(APIFY_API_KEY)

    # Determine time filter based on days_back
    if days_back <= 7:
        time_filter = "week"
    else:
        time_filter = "month"
    url = f"https://www.reddit.com/r/{subreddit}/top/?t={time_filter}"

    run_input = {
        "startUrls": [{"url": url}],
        "maxPostsCount": max_posts,
        "crawlCommentsPerPost": False,
        "includeNSFW": False,
        "proxy": {"useApifyProxy": True, "apifyProxyGroups": ["RESIDENTIAL"]}
    }
    try:
        run = client.actor("harshmaur/reddit-scraper").call(run_input=run_input)
        items = list(client.dataset(run.default_dataset_id).iterate_items())
        posts = [i for i in items if i.get("dataType") == "post"]
        logger.info(f"Retrieved {len(posts)} raw posts")
        return posts
    except Exception as e:
        logger.error(f"Reddit fetch error: {e}")
        return []

def fetch_top_comment(post_url):
    logger.info(f"Fetching top comment from {post_url}")
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
            top = comments[0]
            logger.info(f"Retrieved top comment with score {top.get('upVotes') or 0}")
            return top
        logger.info("No comments found")
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
        logger.info("No last_sent_date file, first run of the day")
        return False
    try:
        with open(LAST_SENT_FILE, "r") as f:
            last = f.read().strip()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if last == today:
            logger.info(f"Already sent today ({today})")
            return True
        else:
            logger.info(f"Last sent on {last}, today is {today}")
            return False
    except Exception as e:
        logger.warning(f"Could not read last_sent_date: {e}")
        return False

def mark_sent_today():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with open(LAST_SENT_FILE, "w") as f:
        f.write(today)
    logger.info(f"Marked sent for {today}")

# =============================================================================
# MAIN
# =============================================================================

def main():
    try:
        # 1. Peak check
        ok, reason = should_run()
        now = datetime.now(timezone.utc)
        logger.info(f"UTC: {now.strftime('%Y-%m-%d %H:%M:%S')}")
        if not ok:
            logger.info(f"Skipping: {reason}")
            return

        # 2. Daily sent check
        if has_sent_today():
            logger.info("Skipping duplicate run")
            return

        # --- Rotation using persistent indices ---
        term_idx = get_next_index(TERM_INDEX_FILE, len(TERMS))
        sub_idx = get_next_index(SUBREDDIT_INDEX_FILE, len(SUBREDDITS))
        term_name = TERMS[term_idx]
        term_query = f'"{TERMS[term_idx]}"'  # quoted for phrase search
        subreddit = SUBREDDITS[sub_idx]

        logger.info(f"Selected term[{term_idx}]='{term_name}', subreddit[{sub_idx}]='{subreddit}'")

        # --- 1. Reddit: top post from last M days ---
        M = len(SUBREDDITS)
        posts = fetch_top_posts(subreddit, days_back=M, max_posts=50)
        # Filter to last M days (keep only posts newer than M days ago)
        cutoff = now - timedelta(days=M)
        filtered = []
        for p in posts:
            created_str = p.get("createdAt")
            if not created_str:
                continue
            try:
                # Handle various formats: sometimes it's like "2026-08-28T19:26:27.000Z"
                created = datetime.fromisoformat(created_str.replace('Z', '+00:00'))
                if created >= cutoff:
                    filtered.append(p)
            except Exception as e:
                logger.warning(f"Could not parse date {created_str}: {e}")
                continue
        # Sort by upvotes descending
        filtered.sort(key=lambda p: p.get("upVotes") or p.get("score") or 0, reverse=True)
        valid_post = None
        for p in filtered:
            body = p.get("body", "") or ""
            if len(body) > 0:
                valid_post = p
                logger.info(f"Selected post: '{p.get('title')}' with {p.get('upVotes') or 0} upvotes")
                break
        if not valid_post:
            logger.warning("No post with body content found in the last M days")

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
                "author": valid_post.get("authorName", "unknown"),
                "comment": comment["body"] if comment else None,
                "comment_upvotes": comment.get("upVotes") or 0 if comment else None,
                "comment_author": comment.get("authorName", "unknown") if comment else None
            }

        # --- 2. PubMed: papers for term from last N days ---
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
                        logger.info(f"Matched selected paper: {p['title']}")
                        break
                if not selected_paper:
                    logger.warning("Could not match selected title, using first paper")
                    selected_paper = papers[0]
            else:
                logger.warning("No title selected, using first paper")
                selected_paper = papers[0]
        else:
            logger.warning("No papers found for this term")

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
        logger.info(f"Found {len(named_papers)} named-author papers (after dedup)")

        # --- 4. Build message ---
        lines = []

        # Reddit
        if reddit_entry:
            summary = deepseek_summarize_reddit(
                title=reddit_entry["title"],
                body=reddit_entry["body"][:1500],
                subreddit=reddit_entry["subreddit"],
                upvotes=reddit_entry["upvotes"],
                comment_body=reddit_entry["comment"][:1500] if reddit_entry.get("comment") else "",
                comment_upvotes=reddit_entry.get("comment_upvotes") or 0
            )
            lines.append(f"Reddit Post: {reddit_entry['title']}")
            lines.append(f"Subreddit: r/{reddit_entry['subreddit']}  |  Upvotes: {reddit_entry['upvotes']}  |  Author: {reddit_entry.get('author', 'unknown')}")
            if reddit_entry.get("comment"):
                lines.append(f"Top Comment (upvotes: {reddit_entry['comment_upvotes']}) by {reddit_entry.get('comment_author', 'unknown')}")
            lines.append(f"Summary: {summary}")
            lines.append("")

        # Selected paper
        if selected_paper:
            last_author = selected_paper['authors'][-1] if selected_paper.get('authors') else "Unknown"
            journal = selected_paper.get('journal', "Unknown")
            summary = deepseek_summarize_paper(
                title=selected_paper['title'],
                abstract=selected_paper.get('abstract', ""),
                journal=journal,
                last_author=last_author
            )
            lines.append(f"Paper (Term: {term_name}): {selected_paper['title']}")
            lines.append(f"Lab: {last_author} Lab  |  Journal: {journal}")
            lines.append(f"Summary: {summary}")
            lines.append("")

        # Named author papers
        for p in named_papers:
            last_author = p['authors'][-1] if p.get('authors') else "Unknown"
            journal = p.get('journal', "Unknown")
            summary = deepseek_summarize_paper(
                title=p['title'],
                abstract=p.get('abstract', ""),
                journal=journal,
                last_author=last_author
            )
            lines.append(f"Named Author Paper: {p['title']}")
            lines.append(f"Lab: {last_author} Lab  |  Journal: {journal}")
            lines.append(f"Summary: {summary}")
            lines.append("")

        if not lines:
            lines = ["No content available today."]

        full_message = "\n".join(lines).strip()
        logger.info(f"Prepared message with {len(lines)} sections")
        send_telegram(full_message)
        mark_sent_today()

        # Increment indices for next day
        increment_index(TERM_INDEX_FILE, len(TERMS))
        increment_index(SUBREDDIT_INDEX_FILE, len(SUBREDDITS))

        logger.info("Digest sent successfully.")

    except Exception as e:
        logger.error(f"Unhandled exception: {e}")
        logger.error(traceback.format_exc())
        raise

if __name__ == "__main__":
    main()
