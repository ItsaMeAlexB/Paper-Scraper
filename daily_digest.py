CACHE_FILE = "sent_doi_cache.txt"

def load_cache():
    try:
        with open(CACHE_FILE, "r") as f:
            return set(line.strip() for line in f)
    except FileNotFoundError:
        return set()

def save_cache(ids):
    with open(CACHE_FILE, "w") as f:
        for doi in ids:
            f.write(f"{doi}\n")

# In main:
sent = load_cache()
all_items = [item for item in all_items if item.get("doi") not in sent]

# After sending, add new DOIs to cache and save
new_dois = [item["doi"] for item in selected if item.get("doi")]
sent.update(new_dois)
save_cache(sent)

#!/usr/bin/env python3
"""
Daily Digest – Gene Therapy / Editing / Delivery
Scrapes PubMed, bioRxiv, Reddit; filters; summarises with DeepSeek; sends to Telegram.
"""

import os
import re
import sys
import time
import logging
from datetime import datetime
from collections import defaultdict

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
ENTREZ_EMAIL = os.environ.get("ENTREZ_EMAIL")  # Required by NCBI

# ----------------------------------------------------------------------
# KEYWORDS (used for PubMed, bioRxiv, and filtered Reddit subs)
# ----------------------------------------------------------------------

KEYWORDS = [
    '"gene therapy"',
    '"gene editing"',
    '"gene delivery"',
    '"machine learning" AND biochem*',
    '"bioinformatics" AND biochem*',
    '"DNA repair"',
    '"epigenetic*"',
]

# ----------------------------------------------------------------------
# ALWAYS‑INCLUDE AUTHORS (David Liu and Ciaran Lee)
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

# Subreddits that are fetched by "hot/top" (no keyword filter, only quality)
TOP_SUBREDDITS = ["labrats", "bioinformatics", "machinelearning"]

# Subreddits that are filtered by keywords + comment count
FILTERED_SUBREDDITS = ["clinicaltrials", "biotech", "GeneEditing"]

# Minimum comments for top subreddits
MIN_COMMENTS_TOP = 20
MIN_COMMENTS_FILTERED = 15

# Skip posts with these words in title or flair
SKIP_WORDS = ["meme", "shitpost", "rant", "vent", "joke", "humor"]

# ----------------------------------------------------------------------
# LOGGING
# ----------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# HELPERS: PubMed
# ----------------------------------------------------------------------

def pubmed_search(query, retmax=200):
    """Search PubMed and return list of PMIDs."""
    Entrez.email = ENTREZ_EMAIL
    handle = Entrez.esearch(db="pubmed", term=query, retmax=retmax)
    record = Entrez.read(handle)
    handle.close()
    return record["IdList"]

def pubmed_fetch(pmids):
    """Fetch full records for a list of PMIDs."""
    if not pmids:
        return []
    Entrez.email = ENTREZ_EMAIL
    handle = Entrez.efetch(db="pubmed", id=",".join(pmids), rettype="medline", retmode="text")
    records = list(Medline.parse(handle))
    handle.close()
    return records

def extract_pubmed_data(record):
    """Extract DOI, title, abstract, authors, journal, year."""
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
        "year": record.get("DP", "")[:4],
        "source": "PubMed",
        "pmid": record.get("PMID", ""),
    }

# ----------------------------------------------------------------------
# HELPERS: bioRxiv
# ----------------------------------------------------------------------

BIORXIV_API = "https://api.biorxiv.org/details/biorxiv/"

def biorxiv_search(query, limit=200):
    """Search bioRxiv by keyword (full‑text search)."""
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
    """Convert bioRxiv API item to common dict."""
    return {
        "doi": paper.get("doi"),
        "title": paper.get("title", ""),
        "abstract": paper.get("abstract", ""),
        "authors": paper.get("authors", "").split(", "),
        "journal": "bioRxiv",
        "year": paper.get("date", "")[:4],
        "source": "bioRxiv",
        "pmid": None,
    }

# ----------------------------------------------------------------------
# HELPERS: PubPeer
# ----------------------------------------------------------------------

PUBPEER_API = "https://pubpeer.com/api/v2/publications"

def get_pubpeer_comments(doi):
    """Return list of comments with sentiment classification (critical/non‑critical)."""
    if not doi:
        return []
    try:
        resp = requests.get(f"{PUBPEER_API}/{doi}", timeout=20)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        data = resp.json()
        # Comments are inside data['evaluation_summary']
        comments = []
        for eval_ in data.get("evaluation_summary", []):
            text = eval_.get("comment", "")
            # Heuristic: flag as critical if contains certain words
            critical_words = ["retract", "fabricat", "manipul", "fals", "inconsist", "concern", "error"]
            is_critical = any(w in text.lower() for w in critical_words)
            comments.append({
                "text": text,
                "is_critical": is_critical,
            })
        return comments
    except Exception as e:
        logger.warning(f"PubPeer fetch failed for {doi}: {e}")
        return []

def should_keep_paper(paper, is_always_include=False):
    """Check PubPeer: discard if ≥2 critical comments and not always‑include."""
    if is_always_include:
        return True
    comments = get_pubpeer_comments(paper.get("doi"))
    critical = [c for c in comments if c["is_critical"]]
    if len(critical) >= 2:
        logger.info(f"Discarding {paper.get('doi')} due to {len(critical)} critical PubPeer comments.")
        return False
    return True

# ----------------------------------------------------------------------
# HELPERS: Reddit
# ----------------------------------------------------------------------

def init_reddit():
    return praw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        user_agent=REDDIT_USER_AGENT,
    )

def fetch_top_posts(subreddit_name, limit=50, min_comments=MIN_COMMENTS_TOP):
    """Fetch top posts from a subreddit (day), apply quality filters."""
    reddit = init_reddit()
    sub = reddit.subreddit(subreddit_name)
    posts = []
    for post in sub.top(time_filter="day", limit=limit):
        if post.score < 0:
            continue
        if any(w in post.title.lower() for w in SKIP_WORDS):
            continue
        if post.num_comments < min_comments:
            continue
        # Flair check
        if post.link_flair_text and any(w in post.link_flair_text.lower() for w in SKIP_WORDS):
            continue
        posts.append({
            "title": post.title,
            "url": post.url,
            "score": post.score,
            "num_comments": post.num_comments,
            "subreddit": subreddit_name,
            "created_utc": datetime.utcfromtimestamp(post.created_utc),
            "selftext": post.selftext[:1000],  # limit
            "comments": [],  # will be fetched later
        })
    return posts

def fetch_filtered_posts(subreddit_name, keywords, limit=50, min_comments=MIN_COMMENTS_FILTERED):
    """Fetch hot posts and filter by keyword in title/selftext."""
    reddit = init_reddit()
    sub = reddit.subreddit(subreddit_name)
    posts = []
    for post in sub.hot(limit=limit):
        if post.score < 0:
            continue
        if any(w in post.title.lower() for w in SKIP_WORDS):
            continue
        if post.link_flair_text and any(w in post.link_flair_text.lower() for w in SKIP_WORDS):
            continue
        if post.num_comments < min_comments:
            continue
        text = (post.title + " " + post.selftext).lower()
        # Check any keyword pattern (simple OR)
        if any(re.search(kw.lower(), text) for kw in keywords):
            posts.append({
                "title": post.title,
                "url": post.url,
                "score": post.score,
                "num_comments": post.num_comments,
                "subreddit": subreddit_name,
                "created_utc": datetime.utcfromtimestamp(post.created_utc),
                "selftext": post.selftext[:1000],
                "comments": [],
            })
    return posts

def fetch_comments_for_post(post_dict, limit=20):
    """Fetch top-level comments and return list of strings."""
    reddit = init_reddit()
    submission = reddit.submission(url=post_dict["url"])
    submission.comments.replace_more(limit=0)
    comments = [c.body for c in submission.comments[:limit] if hasattr(c, "body")]
    return comments

# ----------------------------------------------------------------------
# DEEPSEEK API
# ----------------------------------------------------------------------

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

def deepseek_summarize(items, prompt_suffix=""):
    """
    Send a batch of items to DeepSeek and ask for top 5 summaries.
    items: list of dicts with keys: title, abstract/summary, source, type
    Returns: list of strings (formatted as per example).
    """
    if not items:
        return []

    # Build the prompt
    texts = []
    for i, item in enumerate(items, 1):
        title = item.get("title", "Untitled")
        abstract = item.get("abstract", item.get("selftext", ""))[:500]
        source = item.get("source", "Unknown")
        if "preprint" in source.lower():
            tag = "[Preprint]"
        elif "reddit" in source.lower():
            tag = f"[Post] r/{item.get('subreddit','')}"
        else:
            tag = f"[{source}]"
        texts.append(f"{i}. {tag} {title}\n   {abstract}")

    prompt = f"""
You are a scientific summariser. Below is a list of papers and Reddit posts about gene therapy, gene editing, gene delivery, DNA repair, epigenetics, bioinformatics, or machine learning in biochemistry.

From this list, select the **5 most significant** items for a gene therapy researcher. **Always include** any papers from David Liu or Ciaran Lee labs (if present).

For each selected item, write **2–3 sentences** covering:
- What was done
- What was found
- How (method / approach)

Format your answer exactly like this:

🧬 **Daily Digest – [Date]**

1. [Preprint] Liu Lab: "Title" – explanation...
2. [Post] r/biotech: "Title" – explanation...
... (5 items total)

List of items:

{chr(10).join(texts)}

{chr(10)}{prompt_suffix}
"""

    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 800,
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
# MAIN PIPELINE
# ----------------------------------------------------------------------

def main():
    logger.info("Starting daily digest...")
    all_items = []

    # 1. Fetch PubMed papers (keywords + always‑include authors)
    pubmed_items = []

    # Keyword search
    for kw in KEYWORDS:
        logger.info(f"PubMed keyword: {kw}")
        pmids = pubmed_search(kw, retmax=100)
        if pmids:
            records = pubmed_fetch(pmids)
            for rec in records:
                data = extract_pubmed_data(rec)
                if data["doi"]:
                    pubmed_items.append(data)

    # Always‑include authors
    for author_rule in AUTHOR_RULES:
        for query in author_rule["pubmed_queries"]:
            logger.info(f"PubMed author query: {query}")
            pmids = pubmed_search(query, retmax=50)
            if pmids:
                records = pubmed_fetch(pmids)
                for rec in records:
                    data = extract_pubmed_data(rec)
                    if data["doi"]:
                        # Mark as always‑include
                        data["always_include"] = True
                        data["author_label"] = author_rule["name"]
                        pubmed_items.append(data)

    # Deduplicate PubMed by DOI
    seen = set()
    unique_pubmed = []
    for item in pubmed_items:
        doi = item["doi"]
        if doi and doi not in seen:
            seen.add(doi)
            unique_pubmed.append(item)
    logger.info(f"PubMed: {len(unique_pubmed)} unique items before PubPeer filter.")

    # PubPeer filter (keep always‑include)
    for item in unique_pubmed:
        is_always = item.get("always_include", False)
        if should_keep_paper(item, is_always):
            item["source"] = "PubMed"
            all_items.append(item)

    # 2. Fetch bioRxiv papers
    biorxiv_items = []

    # Keyword search
    for kw in KEYWORDS:
        # bioRxiv API uses spaces as OR, but we need to handle quotes
        query = kw.replace('"', '').replace(' AND ', '+').replace(' ', '+')
        logger.info(f"bioRxiv keyword: {query}")
        papers = biorxiv_search(query, limit=100)
        for p in papers:
            data = parse_biorxiv_paper(p)
            if data["doi"]:
                biorxiv_items.append(data)

    # Always‑include authors (name search)
    for author_rule in AUTHOR_RULES:
        query = author_rule["biorxiv_query"].replace('"', '').replace(' ', '+')
        logger.info(f"bioRxiv author query: {query}")
        papers = biorxiv_search(query, limit=50)
        for p in papers:
            data = parse_biorxiv_paper(p)
            if data["doi"]:
                data["always_include"] = True
                data["author_label"] = author_rule["name"]
                biorxiv_items.append(data)

    # Deduplicate
    seen = set()
    unique_biorxiv = []
    for item in biorxiv_items:
        doi = item["doi"]
        if doi and doi not in seen:
            seen.add(doi)
            unique_biorxiv.append(item)
    logger.info(f"bioRxiv: {len(unique_biorxiv)} unique items before PubPeer filter.")

    for item in unique_biorxiv:
        is_always = item.get("always_include", False)
        if should_keep_paper(item, is_always):
            item["source"] = "bioRxiv"
            all_items.append(item)

    # 3. Reddit – top subreddits (no keyword filter)
    reddit_items = []
    for sub in TOP_SUBREDDITS:
        logger.info(f"Fetching top posts from r/{sub}")
        posts = fetch_top_posts(sub, limit=50)
        for post in posts:
            # Fetch comments for sentiment later
            post["comments"] = fetch_comments_for_post(post, limit=15)
            post["source"] = "Reddit"
            reddit_items.append(post)

    # 4. Reddit – filtered subreddits
    for sub in FILTERED_SUBREDDITS:
        logger.info(f"Fetching filtered posts from r/{sub}")
        posts = fetch_filtered_posts(sub, KEYWORDS, limit=50)
        for post in posts:
            post["comments"] = fetch_comments_for_post(post, limit=15)
            post["source"] = "Reddit"
            reddit_items.append(post)

    logger.info(f"Reddit: {len(reddit_items)} posts fetched.")
    all_items.extend(reddit_items)

    # 5. If no items, send a fallback message and exit
    if not all_items:
        send_telegram("⚠️ No new relevant papers or posts found today.")
        return

# 6. Filter to last 7 days only
cutoff = datetime.now() - timedelta(days=7)
all_items = [
    item for item in all_items
    if item.get("created_utc", datetime.now()) >= cutoff
]

# 7. Separate always-include authors
always = [i for i in all_items if i.get("always_include")]
others = [i for i in all_items if not i.get("always_include")]

# 8. Pick exactly 5 from each source (PubMed, bioRxiv, Reddit)
#    For Reddit, combine all subreddits and pick top 5 by score
def pick_top(items, n=5):
    return sorted(items, key=lambda x: x.get("score", 0), reverse=True)[:n]

source_sampled = {
    "PubMed": pick_top([i for i in others if i.get("source") == "PubMed"]),
    "bioRxiv": pick_top([i for i in others if i.get("source") == "bioRxiv"]),
    "Reddit": pick_top([i for i in others if i.get("source") == "Reddit"]),
}

# 9. Combine: always-include (unlimited, but typically 1-2/day) + top 5 from each source
selected = always + source_sampled["PubMed"] + source_sampled["bioRxiv"] + source_sampled["Reddit"]

# 10. If > 20, trim to top 20 by score (to avoid token issues)
if len(selected) > 20:
    selected = sorted(selected, key=lambda x: x.get("score", 0), reverse=True)[:20]

# 11. Now send selected to DeepSeek

    # 7. Call DeepSeek
    logger.info("Sending items to DeepSeek for summarisation...")
    summary_lines = deepseek_summarize(all_items)

    if not summary_lines:
        # fallback: send raw list
        fallback = "DeepSeek summary failed. Raw items:\n"
        for i, item in enumerate(all_items[:10], 1):
            fallback += f"{i}. {item.get('title', 'No title')}\n"
        send_telegram(fallback)
        return

    # 8. Send to Telegram
    date_str = datetime.now().strftime("%B %d, %Y")
    # Insert date into first line if not present
    if summary_lines and "Daily Digest" not in summary_lines[0]:
        summary_lines.insert(0, f"🧬 **Daily Digest – {date_str}**")
    final_msg = "\n".join(summary_lines)
    send_telegram(final_msg)
    logger.info("Digest sent successfully.")

if __name__ == "__main__":
    main()