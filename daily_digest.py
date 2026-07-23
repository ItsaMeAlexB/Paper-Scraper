#!/usr/bin/env python3
"""
Daily Digest – Gene Therapy / Editing / Delivery / Engineering
Scrapes PubMed, bioRxiv, ClinicalTrials.gov, Reddit; selects most significant;
summarises with DeepSeek; sends to Telegram.
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
# CONFIGURATION (environment variables)
# ----------------------------------------------------------------------

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
REDDIT_CLIENT_ID = os.environ.get("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET")
REDDIT_USER_AGENT = os.environ.get("REDDIT_USER_AGENT", "daily-digest/1.0")
ENTREZ_EMAIL = os.environ.get("ENTREZ_EMAIL")

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

# ClinicalTrials.gov only uses a subset
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

MIN_REDDIT_COMMENTS = 1   # was 15, now effectively no filter (top posts have ≥1)
REDDIT_WINDOW_START = 1   # 1 day old minimum
REDDIT_WINDOW_END = 8     # 8 days old maximum

# ----------------------------------------------------------------------
# TIME WINDOWS (papers/trials: 4-5 weeks old)
# ----------------------------------------------------------------------

PAPER_WINDOW_START_DAYS = 35  # 5 weeks ago
PAPER_WINDOW_END_DAYS = 28    # 4 weeks ago

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

CACHE_TTL = 35 * 24 * 60 * 60  # 5 weeks
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
    """DeepSeek peak pricing: Beijing 9-12 & 14-18 = UTC 1-4 & 6-10"""
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
# RETRY WRAPPER
# ----------------------------------------------------------------------

def fetch_with_retry(fetch_func, *args, retries=2, delay=5, **kwargs):
    """Try a fetch function twice; sleep `delay` seconds between attempts."""
    for attempt in range(retries):
        try:
            result = fetch_func(*args, **kwargs)
            if result:  # non-empty (list, dict, etc.)
                return result
        except Exception as e:
            logger.warning(f"Fetch attempt {attempt+1} failed: {e}")
        if attempt < retries - 1:
            logger.info(f"Retrying in {delay} seconds...")
            time.sleep(delay)
    return []  # fallback empty result

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
    """Return abstract and journal for a DOI, or None if not found."""
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
    status_filter = (
        "AREA[OverallStatus]ACTIVE OR "
        "AREA[OverallStatus]NOT_YET_RECRUITING OR "
        "AREA[OverallStatus]RECRUITING OR "
        "AREA[OverallStatus]ENROLLING_BY_INVITATION OR "
        "AREA[OverallStatus]COMPLETED"
    )
    full_query = f"({query}) AND ({status_filter})"
    params = {
        "query.term": full_query,
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

    # Extract linked references (look for PMID or DOI)
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
    """Try to fetch abstract/journal from linked references."""
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

# ----------------------------------------------------------------------
# HELPERS: Reddit
# ----------------------------------------------------------------------

def init_reddit():
    return praw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        user_agent=REDDIT_USER_AGENT,
    )

def fetch_reddit_posts(subreddit_name, limit=20):
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
        if post.num_comments < MIN_REDDIT_COMMENTS:
            continue
        posts.append({
            "id": post.id,
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
        c.replies.replace_more(limit=0)
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

def get_reddit_cache_key(subreddit, post_id):
    return f"reddit_{subreddit}_{post_id}"

# ----------------------------------------------------------------------
# DEEPSEEK
# ----------------------------------------------------------------------

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

def deepseek_summarize(candidates, reddit_posts):
    if not candidates and not reddit_posts:
        return ["No candidates found today."]

    # Build candidate list for papers/trials
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

    # Build reddit text
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

    # ─── EXACT PROMPT PROVIDED BY USER ───
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
    # ─── END USER PROMPT ───

    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 2500,
    }
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(DEEPSEEK_URL, json=payload, headers=headers, timeout=90)
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
    # 1. Check if already run today or peak hours
    if already_run_today():
        logger.info("Already run today, exiting.")
        return
    if is_peak_hours():
        logger.info("Peak hours, exiting (will retry next hour).")
        return

    logger.info("Starting daily digest...")
    sent = load_cache()
    now = datetime.now()
    all_candidates = []
    selected_reddit_posts = []

    # ------------------------------------------------------------------
    # A. FETCH PAPERS / TRIALS
    # ------------------------------------------------------------------

    pubmed_candidates = []
    biorxiv_candidates = []
    ct_candidates = []
    author_candidates = []

    # --- PubMed ---
    for term in KEYWORDS:
        pmids = fetch_with_retry(pubmed_search, term, retmax=200)
        if pmids:
            records = pubmed_fetch(pmids)
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
    time.sleep(1)  # delay between sources

    # --- bioRxiv ---
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

    # --- ClinicalTrials.gov ---
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

    # --- Named authors ---
    for author_rule in AUTHOR_RULES:
        for query in author_rule["pubmed_queries"]:
            pmids = fetch_with_retry(pubmed_search, query, retmax=50)
            if pmids:
                records = pubmed_fetch(pmids)
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

    # Combine non‑author candidates and dedupe by (source, term) → pick oldest
    all_raw = pubmed_candidates + biorxiv_candidates + ct_candidates
    groups = {}
    for cand in all_raw:
        key = (cand.get("source"), cand.get("search_term"))
        if key not in groups:
            groups[key] = []
        groups[key].append(cand)

    selected = []
    for (source, term), items in groups.items():
        sorted_items = sorted(items, key=lambda x: parse_pubmed_date(x.get("date")) or parse_clinicaltrial_date(x.get("date")) or now)
        if sorted_items:
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

    # Final unique set by ID
    seen_ids = set()
    unique_selected = []
    for item in selected + author_final:
        key_id = item.get("doi") or item.get("nct_id")
        if key_id and key_id not in seen_ids:
            seen_ids.add(key_id)
            unique_selected.append(item)

    all_candidates = unique_selected
    logger.info(f"Selected {len(all_candidates)} unique paper/trial candidates.")

    # ------------------------------------------------------------------
    # B. REDDIT
    # ------------------------------------------------------------------

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
        post["comments"] = fetch_comments(post["url"])
        selected_reddit_posts.append(post)

    logger.info(f"Selected {len(selected_reddit_posts)} Reddit posts.")

    # ------------------------------------------------------------------
    # C. DEEPSEEK
    # ------------------------------------------------------------------

    if not all_candidates and not selected_reddit_posts:
        send_telegram("⚠️ No new relevant papers, trials, or Reddit posts found today.")
        return

    summary_lines = deepseek_summarize(all_candidates, selected_reddit_posts)

    # ------------------------------------------------------------------
    # D. SEND TELEGRAM
    # ------------------------------------------------------------------
    date_str = now.strftime("%B %d, %Y")
    if summary_lines:
        # The prompt already includes "🧬 **[Date]**" – we may need to ensure the date is inserted.
        # We'll look for the first line starting with "🧬" and replace it if needed.
        # But the prompt instructs to output "🧬 **[Date]**" – we can let DeepSeek fill it.
        # However, we can also prepend if missing – we'll check.
        if summary_lines and not any("🧬" in line for line in summary_lines[:3]):
            summary_lines.insert(0, f"🧬 **{date_str}**")
        # If there is a line with "🧬 **[Date]**", we can replace it with the actual date.
        for i, line in enumerate(summary_lines):
            if "🧬 **[Date]**" in line:
                summary_lines[i] = f"🧬 **{date_str}**"
                break
    else:
        summary_lines = ["⚠️ DeepSeek summarisation failed."]

    final_msg = "\n".join(summary_lines)
    send_telegram(final_msg)

    # ------------------------------------------------------------------
    # E. UPDATE CACHE
    # ------------------------------------------------------------------
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