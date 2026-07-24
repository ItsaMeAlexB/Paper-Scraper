#!/usr/bin/env python3
"""
Daily Digest – Gene Therapy / Editing / Delivery / Engineering
Scrapes PubMed, bioRxiv, ClinicalTrials.gov, Reddit (via Apify REST API);
selects most significant; summarises with DeepSeek V4 Flash; sends to Telegram.
"""

import os
import sys
import re
import time
import json
import logging
import random
from datetime import datetime, timedelta

import requests
from Bio import Entrez, Medline

# ----------------------------------------------------------------------
# CONFIGURATION (environment variables)
# ----------------------------------------------------------------------

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ENTREZ_EMAIL = os.environ.get("ENTREZ_EMAIL")
APIFY_TOKEN = os.environ.get("APIFY_TOKEN")   # <-- your Apify secret

# ----------------------------------------------------------------------
# SEARCH TERMS
# ----------------------------------------------------------------------

KEYWORDS = [
    '"gene therapy"',
    '"gene editing"',
    '"gene delivery"',
    'cell* AND immun*',
    'bioinformatic*',
    '"computational biolog*"',
    '"DNA repair"',
    'epigenetic*',
    'manufactur*',
    'discover* OR engineer*',
]

CT_KEYWORDS = [
    '"gene therapy"',
    '"gene editing"',
    '"gene delivery"',
    'cell* AND immun*',
    '"DNA repair"',
    'epigenetic*',
]

# ----------------------------------------------------------------------
# AUTHORS TO ALWAYS INCLUDE
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
# REDDIT SUBREDDITS
# ----------------------------------------------------------------------

SUBREDDITS = [
    "labrats",
    "bioinformatics",
    "machinelearning",
    "GeneEditing",
    "clinicaltrials",
    "biotech",
    "computationalbiology",
    "immunology",
    "biochemistry",
    "molecularbiology",
    "cellbiology",
    "statistics",
]

MIN_REDDIT_COMMENTS = 1
REDDIT_WINDOW_START = 1
REDDIT_WINDOW_END = 8

# ----------------------------------------------------------------------
# TIME WINDOWS (papers/trials: 4-5 weeks old)
# ----------------------------------------------------------------------

PAPER_WINDOW_START_DAYS = 35
PAPER_WINDOW_END_DAYS = 28

# ----------------------------------------------------------------------
# CHARACTER CAPS
# ----------------------------------------------------------------------

CAPS = {
    "abstract": 2000,
    "selftext": 3000,
    "comment": 500,
    "reply": 300,
}

# ----------------------------------------------------------------------
# CACHE
# ----------------------------------------------------------------------

CACHE_TTL = 35 * 24 * 60 * 60
CACHE_FILE = "sent_cache.txt"
LAST_RUN_FILE = "last_run_date.txt"

def load_cache():
    cache = {}
    try:
        with open(CACHE_FILE, "r") as f:
            for line in f:
                parts = line.strip().split("|")
                if len(parts) == 2:
                    key, ts = parts
                    if time.time() - float(ts) < CACHE_TTL:
                        cache[key] = float(ts)
    except FileNotFoundError:
        pass
    return cache

def save_cache(cache):
    with open(CACHE_FILE, "w") as f:
        for key, ts in cache.items():
            f.write(f"{key}|{ts}\n")

def already_run_today():
    try:
        with open(LAST_RUN_FILE, "r") as f:
            last_date = f.read().strip()
            return last_date == datetime.utcnow().strftime("%Y-%m-%d")
    except FileNotFoundError:
        return False

def mark_run_today():
    with open(LAST_RUN_FILE, "w") as f:
        f.write(datetime.utcnow().strftime("%Y-%m-%d"))

# ----------------------------------------------------------------------
# PEAK HOURS GUARDRAIL
# ----------------------------------------------------------------------

def is_peak_hours():
    h = datetime.utcnow().hour
    return (1 <= h < 4) or (6 <= h < 10)

# ----------------------------------------------------------------------
# LOGGING
# ----------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# ROBUST RETRY WRAPPER (exception‑safe, with jitter & backoff)
# ----------------------------------------------------------------------

def fetch_with_retry(fetch_func, *args, retries=3, delay=5, backoff=2, **kwargs):
    """
    Call fetch_func(*args, **kwargs) with exponential backoff and jitter.
    Raises the last exception if all retries fail.
    """
    last_exception = None
    for attempt in range(retries):
        try:
            return fetch_func(*args, **kwargs)
        except Exception as e:
            last_exception = e
            logger.warning(f"Attempt {attempt+1} failed for {fetch_func.__name__}: {e}")
            if attempt < retries - 1:
                sleep_time = delay * (backoff ** attempt) + random.uniform(0, 1)
                logger.info(f"Retrying in {sleep_time:.1f}s...")
                time.sleep(sleep_time)
    logger.error(f"All {retries} attempts failed for {fetch_func.__name__}")
    raise last_exception

# ----------------------------------------------------------------------
# HELPERS: PubMed
# ----------------------------------------------------------------------

def parse_pubmed_date(dp):
    if not dp:
        return None
    for fmt in ("%Y %b %d", "%Y %b", "%Y"):
        try:
            return datetime.strptime(dp, fmt)
        except ValueError:
            continue
    return None

def pubmed_search(query, retmax=100):
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
        "pmid": record.get("PMID", ""),
    }

def fetch_pubmed_abstract_by_doi(doi):
    try:
        pmids = pubmed_search(f'{doi}[doi]', retmax=1)
        if not pmids:
            return None
        records = pubmed_fetch(pmids)
        if not records:
            return None
        rec = records[0]
        return {
            "abstract": rec.get("AB", ""),
            "journal": rec.get("JT", ""),
            "title": rec.get("TI", ""),
            "authors": rec.get("AU", []),
        }
    except Exception:
        return None

# ----------------------------------------------------------------------
# HELPERS: bioRxiv
# ----------------------------------------------------------------------

BIORXIV_API = "https://api.biorxiv.org/details/biorxiv/"

def biorxiv_search(query, limit=100):
    url = f"{BIORXIV_API}{query}/{limit}/0/json"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get("collection", [])

def parse_biorxiv_paper(paper):
    return {
        "doi": paper.get("doi"),
        "title": paper.get("title", ""),
        "abstract": paper.get("abstract", ""),
        "authors": paper.get("authors", "").split(", "),
        "journal": "bioRxiv",
        "date": paper.get("date", ""),
    }

# ----------------------------------------------------------------------
# HELPERS: ClinicalTrials.gov
# ----------------------------------------------------------------------

CLINICALTRIALS_API = "https://clinicaltrials.gov/api/v2/studies"

def clinicaltrials_search(query, limit=100):
    statuses = "ACTIVE,NOT_YET_RECRUITING,RECRUITING,ENROLLING_BY_INVITATION,COMPLETED"
    params = {
        "query.term": query,
        "filter.overallStatus": statuses,
        "pageSize": min(limit, 100),
        "format": "json",
    }
    resp = requests.get(CLINICALTRIALS_API, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get("studies", [])

def parse_clinicaltrial_date(dp):
    if not dp:
        return None
    try:
        return datetime.strptime(dp[:10], "%Y-%m-%d")
    except ValueError:
        return None

def parse_clinicaltrial(study):
    protocol = study.get("protocolSection", {})
    id_module = protocol.get("identificationModule", {})
    status_module = protocol.get("statusModule", {})
    design_module = protocol.get("designModule", {})
    conditions_module = protocol.get("conditionsModule", {})
    interventions_module = protocol.get("interventionsModule", {})
    refs_module = protocol.get("referencesModule", {})

    date_str = status_module.get("lastUpdatePostDate") or status_module.get("firstPostedDate") or ""
    summary = (conditions_module.get("briefSummary", "") or "")
    if not summary:
        summary = conditions_module.get("detailedDescription", "") or ""

    linked_refs = []
    for ref in refs_module.get("references", []):
        citation = ref.get("citation", "")
        if "doi" in citation.lower():
            match = re.search(r'10\.\d+/\S+', citation)
            if match:
                linked_refs.append({"type": "doi", "id": match.group()})
        elif "pmid" in citation.lower():
            match = re.search(r'\d{8}', citation)
            if match:
                linked_refs.append({"type": "pmid", "id": match.group()})

    return {
        "nct_id": id_module.get("nctId"),
        "title": id_module.get("briefTitle") or id_module.get("officialTitle") or "",
        "abstract": summary[:CAPS["abstract"]],
        "conditions": conditions_module.get("conditions", []),
        "interventions": interventions_module.get("interventions", []),
        "phase": design_module.get("phases", []),
        "status": status_module.get("overallStatus", ""),
        "enrollment": design_module.get("enrollmentInfo", {}).get("count", ""),
        "date": date_str,
        "linked_refs": linked_refs,
        "source": "ClinicalTrials.gov",
    }

def get_paper_from_clinicaltrial_refs(linked_refs):
    for ref in linked_refs:
        if ref["type"] == "doi":
            result = fetch_pubmed_abstract_by_doi(ref["id"])
            if result:
                return result
        elif ref["type"] == "pmid":
            continue
    return None

# ----------------------------------------------------------------------
# HELPERS: PubPeer
# ----------------------------------------------------------------------

PUBPEER_API = "https://pubpeer.com/api/v2/publications"

def get_pubpeer_note(identifier):
    if not identifier:
        return "No PubPeer data"
    for attempt in range(2):
        try:
            resp = requests.get(f"{PUBPEER_API}/{identifier}", timeout=20)
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
            logger.warning(f"PubPeer attempt {attempt+1} failed for {identifier}: {e}")
            if attempt == 0:
                time.sleep(5)
            else:
                return "PubPeer unavailable"
    return "PubPeer unavailable"

# ===========================================================================
# REDDIT SCRAPING (via Apify REST API – no SDK, no x402)
# ===========================================================================

def call_apify_actor(input_data, timeout_seconds=120):
    """
    Run an Apify actor via REST API and return the dataset items.
    Uses Bearer token authentication – no x402.
    """
    if not APIFY_TOKEN:
        raise ValueError("APIFY_TOKEN environment variable is not set.")

    run_url = "https://api.apify.com/v2/acts/apify/reddit-scraper/runs"
    headers = {
        "Authorization": f"Bearer {APIFY_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "input": input_data,
        "waitForFinish": timeout_seconds
    }

    resp = requests.post(run_url, json=payload, headers=headers, timeout=timeout_seconds + 10)
    resp.raise_for_status()
    run_data = resp.json()

    dataset_id = run_data["data"]["defaultDatasetId"]

    dataset_url = f"https://api.apify.com/v2/datasets/{dataset_id}/items"
    items_resp = requests.get(dataset_url, headers=headers)
    items_resp.raise_for_status()

    return items_resp.json()


def fetch_reddit_posts(subreddit, limit=20):
    """Fetch top posts from a subreddit using Apify REST API."""
    input_data = {
        "subreddit": subreddit,
        "sort": "top",
        "limit": limit,
        "time": "week"
    }
    items = fetch_with_retry(call_apify_actor, input_data, retries=3, delay=10)

    posts = []
    now = datetime.utcnow()
    for p in items:
        created = p.get("created_utc", 0)
        if not created:
            continue
        post_age_days = (now - datetime.utcfromtimestamp(created)).days
        if not (REDDIT_WINDOW_START <= post_age_days <= REDDIT_WINDOW_END):
            continue
        score = p.get("score", 0)
        num_comments = p.get("num_comments", 0)
        if score < 0:
            continue
        if num_comments < MIN_REDDIT_COMMENTS:
            continue
        posts.append({
            "id": p.get("id", ""),
            "title": p.get("title", ""),
            "url": p.get("url", ""),
            "score": score,
            "num_comments": num_comments,
            "subreddit": subreddit,
            "created_utc": created,
            "selftext": p.get("selftext", "")[:CAPS["selftext"]],
        })
    return posts


def fetch_comments(post_url):
    """Fetch top comments from a post using Apify REST API."""
    input_data = {
        "postUrl": post_url,
        "limit": 10,
    }
    try:
        items = fetch_with_retry(call_apify_actor, input_data, retries=2, delay=10)
    except Exception as e:
        logger.warning(f"Apify comments fetch failed for {post_url}: {e}")
        return []

    top_comments = sorted(items, key=lambda c: c.get("score", 0), reverse=True)[:3]
    result = []
    for c in top_comments:
        replies = c.get("replies", [])
        if replies:
            top_reply = sorted(replies, key=lambda r: r.get("score", 0), reverse=True)[0]
        else:
            top_reply = {"body": "", "score": 0}
        result.append({
            "comment": c.get("body", "")[:CAPS["comment"]],
            "score": c.get("score", 0),
            "reply": top_reply.get("body", "")[:CAPS["reply"]],
            "reply_score": top_reply.get("score", 0),
        })
    return result


def get_reddit_cache_key(subreddit, post_id):
    return f"reddit_{subreddit}_{post_id}"

# ===========================================================================
# DEEPSEEK V4 FLASH
# ===========================================================================

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

def deepseek_summarize(candidates, reddit_posts):
    if not candidates and not reddit_posts:
        return ["No candidates found today."]

    paper_text = ""
    for i, item in enumerate(candidates, 1):
        source = item.get("source", "Unknown")
        search_term = item.get("search_term", "unknown")
        doi = item.get("doi")
        nct_id = item.get("nct_id")
        identifier = doi or nct_id or "N/A"
        title = item.get("title", "No title")
        abstract = item.get("abstract", "")[:CAPS["abstract"]]
        journal = item.get("journal", "")
        authors = item.get("authors", [])
        lab = authors[-1] if authors else "unknown lab"
        pubpeer_note = item.get("pubpeer_note", "No PubPeer data")
        paper_text += f"{i}. Source: {source} | Search term: \"{search_term}\" | ID: {identifier} | Journal: {journal} | Lab: {lab} | Title: {title} | Abstract: {abstract} | PubPeer: {pubpeer_note}\n"

    reddit_text = ""
    for i, post in enumerate(reddit_posts, 1):
        sub = post.get("subreddit", "unknown")
        title = post.get("title", "No title")
        selftext = post.get("selftext", "")[:CAPS["selftext"]]
        comments = post.get("comments", [])
        comment_lines = []
        for c in comments:
            comment_lines.append(f"Comment (score {c['score']}): {c['comment']}")
            if c['reply']:
                comment_lines.append(f"  Reply: {c['reply']}")
        reddit_text += f"{i}. r/{sub}: \"{title}\"\n   Selftext: {selftext}\n   {chr(10).join(comment_lines)}\n"

    prompt = f"""
You are a scientific summariser. Your task is to produce a daily digest.

**For papers/trials**: From the list below, select the **5 most significant** papers/preprints/trials. Summarise with available information to state key information and explain what was done/is being done, how it was done/is being done, and what was found/hoping to be found. 

**For Reddit posts**: Summarise each of the provided Reddit posts using the comments and replies to capture the consensus.

**Rules**:
- Use the context and your trained knowledge to understand and summarise, but if you are unsure about something or there's ambiguous, include that thing or ambiguity in the summary for the reader to understand by themselves. You are not to hallucinate information not present nor guess things you're unsure of.
- Paper summaries: 4–6 sentences.
- Reddit summaries: 2–3 sentences.

**Output format** (use these sections exactly):

🧬 **[Date]**

**Papers / Trials (most significant):**
1. [Source via "search_term" or name of author search for] ID: ... – summary – Lab name. PubPeer: note or state lack of comments.
2. ...

**Reddit (useful posts):**
1. r/subreddit: "Title" – summarised post + comments/replies.
2. ...

Candidates:
{paper_text}

Reddit posts:
{reddit_text}
"""

    payload = {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 2500,
    }
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    resp = requests.post(DEEPSEEK_URL, json=payload, headers=headers, timeout=90)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip().split("\n")

# ----------------------------------------------------------------------
# TELEGRAM
# ----------------------------------------------------------------------

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()  # will be caught by retry wrapper
    logger.info("Telegram message sent.")

# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------

def main():
    if already_run_today():
        logger.info("Already run today, exiting.")
        return
    if is_peak_hours():
        logger.info("Peak hours, exiting (will retry next hour).")
        return

    logger.info("Starting daily digest...")
    sent = load_cache()
    now = datetime.now()

    # ------------------- FETCH PAPERS -------------------
    pubmed_candidates = []
    biorxiv_candidates = []
    ct_candidates = []
    author_candidates = []

    # PubMed
    for term in KEYWORDS:
        pmids = fetch_with_retry(pubmed_search, term, retmax=200)
        if pmids:
            records = fetch_with_retry(pubmed_fetch, pmids)   # <-- now wrapped
            for rec in records:
                data = extract_pubmed_data(rec)
                if data["doi"] and data["doi"] not in sent:
                    pub_date = parse_pubmed_date(data["date"])
                    if pub_date:
                        age_days = (now - pub_date).days
                        if PAPER_WINDOW_START_DAYS >= age_days >= PAPER_WINDOW_END_DAYS:
                            data["source"] = "PubMed"
                            data["search_term"] = term
                            data["pubpeer_note"] = get_pubpeer_note(data["doi"])
                            pubmed_candidates.append(data)
    time.sleep(1)

    # bioRxiv
    for term in KEYWORDS:
        query = term.replace('"', '').replace(' AND ', '+').replace(' ', '+')
        papers = fetch_with_retry(biorxiv_search, query, limit=100)
        for p in papers:
            data = parse_biorxiv_paper(p)
            if data["doi"] and data["doi"] not in sent:
                try:
                    pub_date = datetime.strptime(data["date"], "%Y-%m-%d")
                except:
                    continue
                age_days = (now - pub_date).days
                if PAPER_WINDOW_START_DAYS >= age_days >= PAPER_WINDOW_END_DAYS:
                    data["source"] = "bioRxiv"
                    data["search_term"] = term
                    data["pubpeer_note"] = get_pubpeer_note(data["doi"])
                    biorxiv_candidates.append(data)
    time.sleep(1)

    # ClinicalTrials.gov
    for term in CT_KEYWORDS:
        studies = fetch_with_retry(clinicaltrials_search, term, limit=100)
        for study in studies:
            data = parse_clinicaltrial(study)
            if data["nct_id"] and data["nct_id"] not in sent:
                pub_date = parse_clinicaltrial_date(data["date"])
                if pub_date:
                    age_days = (now - pub_date).days
                    if PAPER_WINDOW_START_DAYS >= age_days >= PAPER_WINDOW_END_DAYS:
                        linked_paper = get_paper_from_clinicaltrial_refs(data["linked_refs"]) if data["linked_refs"] else None
                        if linked_paper:
                            data["abstract"] = linked_paper.get("abstract", data["abstract"])[:CAPS["abstract"]]
                            data["journal"] = linked_paper.get("journal", "ClinicalTrials.gov record")
                            linked_doi = None
                            for ref in data["linked_refs"]:
                                if ref["type"] == "doi":
                                    linked_doi = ref["id"]
                                    break
                            if linked_doi:
                                data["paper_pubpeer"] = get_pubpeer_note(linked_doi)
                            else:
                                data["paper_pubpeer"] = None
                        else:
                            data["journal"] = "ClinicalTrials.gov record"
                        data["source"] = "ClinicalTrials.gov"
                        data["search_term"] = term
                        data["pubpeer_note"] = get_pubpeer_note(data["nct_id"])
                        ct_candidates.append(data)
    time.sleep(1)

    # Named authors
    for author_rule in AUTHOR_RULES:
        for query in author_rule["pubmed_queries"]:
            pmids = fetch_with_retry(pubmed_search, query, retmax=50)
            if pmids:
                records = fetch_with_retry(pubmed_fetch, pmids)   # <-- wrapped
                for rec in records:
                    data = extract_pubmed_data(rec)
                    if data["doi"] and data["doi"] not in sent:
                        pub_date = parse_pubmed_date(data["date"])
                        if pub_date:
                            age_days = (now - pub_date).days
                            if PAPER_WINDOW_START_DAYS >= age_days >= PAPER_WINDOW_END_DAYS:
                                data["source"] = "PubMed"
                                data["search_term"] = f'"{author_rule["name"]}"'
                                data["pubpeer_note"] = get_pubpeer_note(data["doi"])
                                data["author_label"] = author_rule["name"]
                                author_candidates.append(data)
        query = author_rule["biorxiv_query"].replace('"', '').replace(' ', '+')
        papers = fetch_with_retry(biorxiv_search, query, limit=50)
        for p in papers:
            data = parse_biorxiv_paper(p)
            if data["doi"] and data["doi"] not in sent:
                try:
                    pub_date = datetime.strptime(data["date"], "%Y-%m-%d")
                except:
                    continue
                age_days = (now - pub_date).days
                if PAPER_WINDOW_START_DAYS >= age_days >= PAPER_WINDOW_END_DAYS:
                    data["source"] = "bioRxiv"
                    data["search_term"] = f'"{author_rule["name"]}"'
                    data["pubpeer_note"] = get_pubpeer_note(data["doi"])
                    data["author_label"] = author_rule["name"]
                    author_candidates.append(data)

    # Combine and dedupe
    all_raw = pubmed_candidates + biorxiv_candidates + ct_candidates
    groups = {}
    for cand in all_raw:
        key = (cand.get("source"), cand.get("search_term"))
        groups.setdefault(key, []).append(cand)

    selected = []
    for (source, term), items in groups.items():
        sorted_items = sorted(items, key=lambda x: parse_pubmed_date(x.get("date")) or parse_clinicaltrial_date(x.get("date")) or now)
        for item in sorted_items:
            key_id = item.get("doi") or item.get("nct_id")
            if key_id and key_id not in sent:
                selected.append(item)
                break

    # Named authors: 1 oldest per author
    author_final = []
    author_groups = {}
    for item in author_candidates:
        label = item.get("author_label")
        if label:
            author_groups.setdefault(label, []).append(item)
    for label, items in author_groups.items():
        sorted_auth = sorted(items, key=lambda x: parse_pubmed_date(x.get("date")) or now)
        if sorted_auth:
            author_final.append(sorted_auth[0])

    seen_ids = set()
    all_candidates = []
    for item in selected + author_final:
        key_id = item.get("doi") or item.get("nct_id")
        if key_id and key_id not in seen_ids:
            seen_ids.add(key_id)
            all_candidates.append(item)

    logger.info(f"Selected {len(all_candidates)} unique paper/trial candidates.")

    # ------------------- REDDIT (Apify REST) -------------------
    reddit_candidates = []
    for sub in SUBREDDITS:
        posts = fetch_reddit_posts(sub, limit=20)
        fresh = [p for p in posts if get_reddit_cache_key(sub, p["id"]) not in sent]
        top_ten = sorted(fresh, key=lambda x: x["score"], reverse=True)[:10]
        if len(top_ten) >= 2:
            top1_score = top_ten[0]["score"]
            avg_rest = sum(p["score"] for p in top_ten[1:]) / (len(top_ten) - 1)
            ratio = top1_score / avg_rest if avg_rest > 0 else top1_score
            reddit_candidates.append({
                "subreddit": sub,
                "posts": top_ten,
                "ratio": ratio,
                "top_post": top_ten[0],
            })

    reddit_candidates.sort(key=lambda x: x["ratio"], reverse=True)
    top_subs = reddit_candidates[:5]

    selected_reddit_posts = []
    for entry in top_subs:
        post = entry["top_post"]
        comments = fetch_comments(post["url"])
        post["comments"] = comments
        selected_reddit_posts.append(post)

    logger.info(f"Selected {len(selected_reddit_posts)} Reddit posts.")

    # ------------------- DEEPSEEK -------------------
    if not all_candidates and not selected_reddit_posts:
        send_telegram("⚠️ No new relevant papers, trials, or Reddit posts found today.")
        return

    summary_lines = fetch_with_retry(deepseek_summarize, all_candidates, selected_reddit_posts, retries=2, delay=10)

    # ------------------- TELEGRAM -------------------
    date_str = now.strftime("%B %d, %Y")
    if summary_lines:
        for i, line in enumerate(summary_lines):
            if "🧬 **[Date]**" in line:
                summary_lines[i] = f"🧬 **{date_str}**"
                break
        else:
            if not any("🧬" in line for line in summary_lines[:3]):
                summary_lines.insert(0, f"🧬 **{date_str}**")
    else:
        summary_lines = ["⚠️ DeepSeek summarisation failed."]

    final_msg = "\n".join(summary_lines)
    fetch_with_retry(send_telegram, final_msg, retries=3, delay=5)

    # ------------------- CACHE -------------------
    for item in all_candidates:
        key = item.get("doi") or item.get("nct_id")
        if key:
            sent[key] = time.time()
    for post in selected_reddit_posts:
        key = get_reddit_cache_key(post["subreddit"], post["id"])
        sent[key] = time.time()
    save_cache(sent)
    mark_run_today()

    logger.info("Digest sent successfully.")

if __name__ == "__main__":
    main()
