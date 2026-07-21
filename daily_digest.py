#!/usr/bin/env python3
"""
Daily Digest – Gene Therapy / Editing / Delivery / Engineering
Scrapes PubMed, bioRxiv, Reddit; selects most significant; summarises with DeepSeek; sends to Telegram.
"""

import os
import sys
import re
import time
import logging
from datetime import datetime, timedelta

import requests
import praw
from Bio import Entrez, Medline

# ----------------------------------------------------------------------
# CONFIGURATION (reads from environment)
# ----------------------------------------------------------------------

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
REDDIT_CLIENT_ID = os.environ.get("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET")
REDDIT_USER_AGENT = os.environ.get("REDDIT_USER_AGENT", "daily-digest/1.0")
ENTREZ_EMAIL = os.environ.get("ENTREZ_EMAIL")

# ----------------------------------------------------------------------
# KEYWORDS
# ----------------------------------------------------------------------

KEYWORDS = [
    '"gene therapy"',
    '"gene editing"',
    '"gene delivery"',
    '"gen* engineering"',
    '"machine learning" AND biochem*',
    '"bioinformatics" AND biochem*',
    '"DNA repair"',
    '"epigenetic*"',
]

# ----------------------------------------------------------------------
# ALWAYS-INCLUDE AUTHORS
# ----------------------------------------------------------------------

AUTHOR_RULES = [
    {
        "name": "David Liu",
        "orcid": "0000-0002-9943-7557",
        "pubmed_queries": [
            '"Liu DR"[AU]',
            '("Liu DR"[AU] AND (Broad[AD] OR Harvard[AD] OR HHMI[AD]))',
        ],
        "biorxiv_query": '"David Liu" OR "D. Liu"',
    },
    {
        "name": "Ciaran Lee",
        "orcid": "0000-0002-0789-9149",
        "pubmed_queries": [
            '"Lee CM"[AU]',
            '"Lee C"[AU]',
        ],
        "biorxiv_query": '"Ciaran Lee" OR "C. Lee"',
    },
]

# ----------------------------------------------------------------------
# REDDIT CONFIGURATION
# ----------------------------------------------------------------------

TOP_SUBREDDITS = ["labrats", "bioinformatics", "machinelearning", "GeneEditing"]
FILTERED_SUBREDDITS = ["clinicaltrials", "biotech"]

MIN_COMMENTS = 15
REDDIT_WINDOW_START = 1   # 1 day old minimum
REDDIT_WINDOW_END = 8     # 8 days old maximum

# ----------------------------------------------------------------------
# TIME WINDOWS (papers: 4-5 weeks old)
# ----------------------------------------------------------------------

PAPER_WINDOW_START_DAYS = 35  # 5 weeks ago
PAPER_WINDOW_END_DAYS = 28    # 4 weeks ago

# ----------------------------------------------------------------------
# CHARACTER CAPS (safe – above normal)
# ----------------------------------------------------------------------

CAPS = {
    "abstract": 2000,
    "selftext": 3000,
    "comment": 500,
    "reply": 300,
}

CACHE_TTL = 35 * 24 * 60 * 60  # 5 weeks

# ----------------------------------------------------------------------
# LOGGING
# ----------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# CACHE
# ----------------------------------------------------------------------

CACHE_FILE = "sent_cache.txt"

def load_cache():
    cache = {}
    try:
        with open(CACHE_FILE, "r") as f:
            for line in f:
                parts = line.strip().split("|")
                if len(parts) == 2:
                    doi, ts = parts
                    if time.time() - float(ts) < CACHE_TTL:
                        cache[doi] = float(ts)
    except FileNotFoundError:
        pass
    return cache

def save_cache(cache):
    with open(CACHE_FILE, "w") as f:
        for doi, ts in cache.items():
            f.write(f"{doi}|{ts}\n")

# ----------------------------------------------------------------------
# PEAK HOURS GUARDRAIL
# ----------------------------------------------------------------------

def is_peak_hours():
    """DeepSeek peak pricing: Beijing 9-12 & 14-18 = UTC 1-4 & 6-10"""
    h = datetime.utcnow().hour
    return (1 <= h < 4) or (6 <= h < 10)

def run_if_not_peak(max_attempts=2, wait_seconds=7200):
    for attempt in range(max_attempts):
        if not is_peak_hours():
            return True
        logger.info(f"Peak hours, waiting {wait_seconds}s (attempt {attempt+1}/{max_attempts})")
        time.sleep(wait_seconds)
    send_telegram("⏰ Still peak hours after retries. Skipping today.")
    return False

# ----------------------------------------------------------------------
# HELPERS: PubMed
# ----------------------------------------------------------------------

def pubmed_search(query, retmax=50):
    Entrez.email = ENTREZ_EMAIL
    handle = Entrez.esearch(db="pubmed", term=query, retmax=retmax)
    record = Entrez.read(handle)
    handle.close()
    return record["IdList"]

def pubmed_fetch(pmids):
    if not pmids:
        return []
    Entrez.email = ENTREZ_EMAIL
    handle = Entrez.efetch(db="pubmed", id=",".join(pmids), rettype="medline", retmode="text")
    records = list(Medline.parse(handle))
    handle.close()
    return records

def extract_pubmed_data(record):
    doi = None
    for id_ in record.get("AID", []):
        if id_.endswith(" [doi]"):
            doi = id_.replace(" [doi]", "")
            break
    return {
        "doi": doi,
        "title": record.get("TI", ""),
        "abstract": record.get("AB", ""),
        "authors": record.get("AU", []),
        "journal": record.get("JT", ""),
        "date": record.get("DP", ""),
        "source": "PubMed",
        "pmid": record.get("PMID", ""),
    }

# ----------------------------------------------------------------------
# HELPERS: bioRxiv
# ----------------------------------------------------------------------

BIORXIV_API = "https://api.biorxiv.org/details/biorxiv/"

def biorxiv_search(query, limit=50):
    url = f"{BIORXIV_API}{query}/{limit}/0/json"
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data.get("collection", [])
    except Exception as e:
        logger.error(f"bioRxiv search failed: {e}")
        return []

def parse_biorxiv_paper(paper):
    return {
        "doi": paper.get("doi"),
        "title": paper.get("title", ""),
        "abstract": paper.get("abstract", ""),
        "authors": paper.get("authors", "").split(", "),
        "journal": "bioRxiv",
        "date": paper.get("date", ""),
        "source": "bioRxiv",
        "pmid": None,
    }

# ----------------------------------------------------------------------
# HELPERS: PubPeer (never cancel, just note)
# ----------------------------------------------------------------------

PUBPEER_API = "https://pubpeer.com/api/v2/publications"

def get_pubpeer_note(doi):
    if not doi:
        return "No PubPeer data"
    try:
        resp = requests.get(f"{PUBPEER_API}/{doi}", timeout=20)
        if resp.status_code == 404:
            return "No PubPeer comments"
        resp.raise_for_status()
        data = resp.json()
        comments = data.get("evaluation_summary", [])
        if not comments:
            return "No PubPeer comments"
        total = len(comments)
        critical = sum(1 for c in comments if any(w in c.get("comment", "").lower() for w in ["retract", "fabricat", "manipul", "fals", "inconsist", "concern", "error"]))
        return f"{total} comments ({critical} critical)"
    except Exception as e:
        logger.warning(f"PubPeer fetch failed for {doi}: {e}")
        return "PubPeer fetch failed"

# ----------------------------------------------------------------------
# HELPERS: Reddit
# ----------------------------------------------------------------------

def init_reddit():
    return praw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        user_agent=REDDIT_USER_AGENT,
    )

def fetch_reddit_posts(subreddit_name, keywords=None, limit=50):
    reddit = init_reddit()
    sub = reddit.subreddit(subreddit_name)
    posts = []
    now = datetime.utcnow()
    for post in sub.top(time_filter="week", limit=limit):
        post_age_days = (now - datetime.utcfromtimestamp(post.created_utc)).days
        if not (REDDIT_WINDOW_START <= post_age_days <= REDDIT_WINDOW_END):
            continue
        if post.score < 0:
            continue
        if post.num_comments < MIN_COMMENTS:
            continue
        # Keyword filter for filtered subs
        if keywords:
            text = (post.title + " " + post.selftext).lower()
            if not any(re.search(kw.lower(), text) for kw in keywords):
                continue
        posts.append({
            "title": post.title,
            "url": post.url,
            "score": post.score,
            "num_comments": post.num_comments,
            "subreddit": subreddit_name,
            "created_utc": post.created_utc,
            "selftext": post.selftext[:CAPS["selftext"]],
        })
    return posts

def fetch_comments(post_url):
    reddit = init_reddit()
    submission = reddit.submission(url=post_url)
    submission.comments.replace_more(limit=0)
    top_comments = sorted(submission.comments, key=lambda c: c.score, reverse=True)[:3]
    result = []
    for c in top_comments:
        comment_text = c.body[:CAPS["comment"]]
        if len(c.body) > CAPS["comment"]:
            logger.info(f"Comment truncated for {post_url} (len={len(c.body)})")
        replies = sorted(c.replies, key=lambda r: r.score, reverse=True)[:1]
        reply_text = replies[0].body[:CAPS["reply"]] if replies else ""
        if replies and len(replies[0].body) > CAPS["reply"]:
            logger.info(f"Reply truncated for {post_url} (len={len(replies[0].body)})")
        result.append({
            "comment": comment_text,
            "score": c.score,
            "reply": reply_text,
            "reply_score": replies[0].score if replies else 0,
        })
    return result

# ----------------------------------------------------------------------
# DEEPSEEK
# ----------------------------------------------------------------------

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

def deepseek_summarize(candidates):
    if not candidates:
        return ["No candidates found today."]

    prompt = f"""
You are a scientific summariser for gene therapy researchers.

You will receive a list of candidates from PubMed, bioRxiv, and Reddit.
Your tasks:

1. **Select the most significant item per category** (one per keyword for PubMed/bioRxiv, one per author, one per subreddit).
2. **Prioritise** papers where gene editing, delivery, therapy, or engineering is the **core focus**.
3. If no such paper exists for a keyword, still include the best available match.
4. For each selected item, write **2–3 sentences** covering:
   - What was done
   - What was found
   - How (method/approach)

Output format (sections):

🧬 **Daily Digest – [Date]**

**PubMed / bioRxiv:**
1. [source] "title" – summary... (PubPeer: note)

**Named authors:**
1. [source] "title" – summary... (PubPeer: note)

**Reddit:**
1. [Post] r/subreddit: "title" – summary... (Top comment: "quote" [+score])

Candidates:
{chr(10).join([f"- {i.get('source')}: {i.get('title')} | {i.get('abstract', i.get('selftext', ''))[:CAPS['abstract']]} | PubPeer: {i.get('pubpeer_note', 'N/A')}" for i in candidates])}
"""

    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 1200,
    }
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(DEEPSEEK_URL, json=payload, headers=headers, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip().split("\n")
    except Exception as e:
        logger.error(f"DeepSeek API error: {e}")
        return []

# ----------------------------------------------------------------------
# TELEGRAM
# ----------------------------------------------------------------------

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        resp = requests.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        logger.info("Telegram message sent.")
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")

# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------

def main():
    if not run_if_not_peak():
        return

    logger.info("Starting daily digest...")
    sent = load_cache()
    all_candidates = []
    now = datetime.now()

    # 1. PubMed (search terms)
    for kw in KEYWORDS:
        logger.info(f"PubMed keyword: {kw}")
        pmids = pubmed_search(kw, retmax=50)
        if pmids:
            records = pubmed_fetch(pmids)
            for rec in records:
                data = extract_pubmed_data(rec)
                if data["doi"] and data["doi"] not in sent:
                    # Check age window (4-5 weeks)
                    try:
                        pub_date = datetime.strptime(data["date"], "%Y %b %d")
                    except:
                        continue
                    age_days = (now - pub_date).days
                    if PAPER_WINDOW_START_DAYS >= age_days >= PAPER_WINDOW_END_DAYS:
                        data["pubpeer_note"] = get_pubpeer_note(data["doi"])
                        data["source_type"] = "PubMed"
                        all_candidates.append(data)

    # 2. bioRxiv (search terms)
    for kw in KEYWORDS:
        query = kw.replace('"', '').replace(' AND ', '+').replace(' ', '+')
        logger.info(f"bioRxiv keyword: {query}")
        papers = biorxiv_search(query, limit=50)
        for p in papers:
            data = parse_biorxiv_paper(p)
            if data["doi"] and data["doi"] not in sent:
                try:
                    pub_date = datetime.strptime(data["date"], "%Y-%m-%d")
                except:
                    continue
                age_days = (now - pub_date).days
                if PAPER_WINDOW_START_DAYS >= age_days >= PAPER_WINDOW_END_DAYS:
                    data["pubpeer_note"] = get_pubpeer_note(data["doi"])
                    data["source_type"] = "bioRxiv"
                    all_candidates.append(data)

    # 3. Named authors (separate, always include)
    for author_rule in AUTHOR_RULES:
        for query in author_rule["pubmed_queries"]:
            logger.info(f"PubMed author: {query}")
            pmids = pubmed_search(query, retmax=10)
            if pmids:
                records = pubmed_fetch(pmids)
                for rec in records:
                    data = extract_pubmed_data(rec)
                    if data["doi"] and data["doi"] not in sent:
                        try:
                            pub_date = datetime.strptime(data["date"], "%Y %b %d")
                        except:
                            continue
                        age_days = (now - pub_date).days
                        if PAPER_WINDOW_START_DAYS >= age_days >= PAPER_WINDOW_END_DAYS:
                            data["pubpeer_note"] = get_pubpeer_note(data["doi"])
                            data["source_type"] = "Named author"
                            data["author_label"] = author_rule["name"]
                            all_candidates.append(data)
        # bioRxiv author search
        query = author_rule["biorxiv_query"].replace('"', '').replace(' ', '+')
        papers = biorxiv_search(query, limit=10)
        for p in papers:
            data = parse_biorxiv_paper(p)
            if data["doi"] and data["doi"] not in sent:
                try:
                    pub_date = datetime.strptime(data["date"], "%Y-%m-%d")
                except:
                    continue
                age_days = (now - pub_date).days
                if PAPER_WINDOW_START_DAYS >= age_days >= PAPER_WINDOW_END_DAYS:
                    data["pubpeer_note"] = get_pubpeer_note(data["doi"])
                    data["source_type"] = "Named author"
                    data["author_label"] = author_rule["name"]
                    all_candidates.append(data)

    # 4. Reddit (top subs)
    for sub in TOP_SUBREDDITS:
        posts = fetch_reddit_posts(sub, keywords=None)
        if posts:
            best = sorted(posts, key=lambda x: x["score"], reverse=True)[0]
            best["comments"] = fetch_comments(best["url"])
            best["source"] = "Reddit"
            best["source_type"] = "Reddit"
            all_candidates.append(best)

    # 5. Reddit (filtered subs)
    for sub in FILTERED_SUBREDDITS:
        posts = fetch_reddit_posts(sub, keywords=KEYWORDS)
        if posts:
            best = sorted(posts, key=lambda x: x["score"], reverse=True)[0]
            best["comments"] = fetch_comments(best["url"])
            best["source"] = "Reddit"
            best["source_type"] = "Reddit"
            all_candidates.append(best)

    # 6. Dedupe by DOI/title (simple)
    seen = set()
    unique = []
    for item in all_candidates:
        key = item.get("doi") or item.get("title")
        if key and key not in seen:
            seen.add(key)
            unique.append(item)

    logger.info(f"Collected {len(unique)} candidates.")

    if not unique:
        send_telegram("⚠️ No new relevant papers or posts found today.")
        return

    # 7. DeepSeek summarisation
    summary_lines = deepseek_summarize(unique)

    # 8. Send to Telegram
    date_str = now.strftime("%B %d, %Y")
    if summary_lines and "Daily Digest" not in summary_lines[0]:
        summary_lines.insert(0, f"🧬 **Daily Digest – {date_str}**")
    final_msg = "\n".join(summary_lines)
    send_telegram(final_msg)

    # 9. Update cache
    for item in unique:
        doi = item.get("doi")
        if doi:
            sent[doi] = time.time()
    save_cache(sent)

    logger.info("Digest sent successfully.")

if __name__ == "__main__":
    main()