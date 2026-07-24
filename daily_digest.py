#!/usr/bin/env python3
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
# CONFIGURATION
# ----------------------------------------------------------------------
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ENTREZ_EMAIL = os.environ.get("ENTREZ_EMAIL")

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

AUTHOR_RULES = [
    {
        "name": "David Liu",
        "pubmed_queries": ['"Liu DR"[AU]', '("Liu DR"[AU] AND (Broad[AD] OR Harvard[AD] OR HHMI[AD]))'],
        "biorxiv_query": '"David Liu" OR "D. Liu"',
        "clintrials_query": '"Liu DR"',
    },
    {
        "name": "Ciaran Lee",
        "pubmed_queries": ['"Lee CM"[AU]', '"Lee C"[AU]'],
        "biorxiv_query": '"Ciaran Lee" OR "C. Lee"',
        "clintrials_query": '"Lee C"',
    },
]

PAPER_WINDOW_START_DAYS = 35
PAPER_WINDOW_END_DAYS = 28

CAPS = {
    "selftext": 1000,
    "comment": 300,
    "reply": 150,
    "abstract": 2000,
}

CACHE_TTL = 35 * 24 * 60 * 60
CACHE_FILE = "sent_cache.txt"

# ----------------------------------------------------------------------
# LOGGING
# ----------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# CACHE
# ----------------------------------------------------------------------
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

# ----------------------------------------------------------------------
# RETRY WRAPPER
# ----------------------------------------------------------------------
def fetch_with_retry(fetch_func, *args, retries=3, delay=5, backoff=2, **kwargs):
    last_exception = None
    for attempt in range(retries):
        try:
            return fetch_func(*args, **kwargs)
        except Exception as e:
            last_exception = e
            logger.warning(f"Attempt {attempt+1} failed for {fetch_func.__name__}: {e}")
            if attempt < retries - 1:
                sleep_time = delay * (backoff ** attempt) + random.uniform(0, 1)
                time.sleep(sleep_time)
    raise last_exception

# ----------------------------------------------------------------------
# PUBMED
# ----------------------------------------------------------------------
def pubmed_search_date_filtered(query, start_date, end_date, retmax=5):
    Entrez.email = ENTREZ_EMAIL
    start_str = start_date.strftime("%Y/%m/%d")
    end_str = end_date.strftime("%Y/%m/%d")
    handle = Entrez.esearch(db="pubmed", term=query, mindate=start_str, maxdate=end_str, retmax=retmax, sort="pub date")
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

def parse_pubmed_date(dp):
    if not dp:
        return None
    for fmt in ("%Y %b %d", "%Y %b", "%Y"):
        try:
            return datetime.strptime(dp, fmt)
        except ValueError:
            continue
    return None

# ----------------------------------------------------------------------
# BIORXIV
# ----------------------------------------------------------------------
BIORXIV_API = "https://api.biorxiv.org/details/biorxiv/"

def biorxiv_search(query, limit=20):
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
# CLINICALTRIALS.GOV
# ----------------------------------------------------------------------
CLINICALTRIALS_API = "https://clinicaltrials.gov/api/v2/studies"

def clinicaltrials_search_date_filtered(query, start_date, end_date, limit=5):
    statuses = "ACTIVE_NOT_RECRUITING,NOT_YET_RECRUITING,RECRUITING,ENROLLING_BY_INVITATION,COMPLETED"
    start_str = start_date.strftime("%Y-%m-%dT00:00:00+00:00")
    end_str = end_date.strftime("%Y-%m-%dT23:59:59+00:00")
    params = {
        "query.term": query.strip('"'),
        "filter.overallStatus": statuses,
        "filter.updatedDate": f"{start_str} TO {end_str}",
        "pageSize": min(limit, 100),
        "format": "json",
        "sort": "lastUpdatePostDate",
    }
    resp = requests.get(CLINICALTRIALS_API, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get("studies", [])

def parse_clinicaltrial(study):
    protocol = study.get("protocolSection", {})
    id_module = protocol.get("identificationModule", {})
    status_module = protocol.get("statusModule", {})
    design_module = protocol.get("designModule", {})
    conditions_module = protocol.get("conditionsModule", {})
    interventions_module = protocol.get("interventionsModule", {})
    refs_module = protocol.get("referencesModule", {})
    date_str = status_module.get("lastUpdatePostDate") or status_module.get("firstPostedDate") or ""
    summary = conditions_module.get("briefSummary", "") or conditions_module.get("detailedDescription", "") or ""
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

def parse_clinicaltrial_date(dp):
    if not dp:
        return None
    try:
        return datetime.strptime(dp[:10], "%Y-%m-%d")
    except ValueError:
        return None

# ----------------------------------------------------------------------
# PUBPEER
# ----------------------------------------------------------------------
PUBPEER_API = "https://pubpeer.com/api/v2/publications"

def get_pubpeer_comments(identifier):
    if not identifier:
        return "No PubPeer data", []
    try:
        resp = requests.get(f"{PUBPEER_API}/{identifier}", timeout=20)
        if resp.status_code == 404:
            return "No PubPeer comments", []
        resp.raise_for_status()
        data = resp.json()
        eval_summary = data.get("evaluation_summary", "")
        comments_data = data.get("comments", [])
        if not comments_data and isinstance(eval_summary, list):
            comments_data = eval_summary
        comments_data.sort(key=lambda c: c.get("date", ""))
        total = len(comments_data)
        selected = []
        if total > 0:
            indices = [0]
            if total > 1:
                indices.append(total - 1)
            if total > 2:
                indices.append(total // 2)
            indices = sorted(set(indices))
            for idx in indices:
                c = comments_data[idx]
                comment_text = c.get("comment", "")
                replies = c.get("replies", [])
                top_reply = replies[0].get("comment", "") if replies else ""
                selected.append({
                    "author": c.get("author", ""),
                    "date": c.get("date", ""),
                    "text": comment_text[:CAPS["comment"]],
                    "reply": top_reply[:CAPS["reply"]] if top_reply else "",
                })
        if isinstance(eval_summary, str):
            summary_text = eval_summary[:300]
        elif isinstance(eval_summary, list):
            summary_text = " ".join([e.get("comment", "") for e in eval_summary[:3]])[:300]
        else:
            summary_text = "No PubPeer evaluation summary"
        return summary_text, selected
    except Exception as e:
        logger.warning(f"PubPeer error for {identifier}: {e}")
        return "PubPeer unavailable", []

# ----------------------------------------------------------------------
# REDDIT
# ----------------------------------------------------------------------
def fetch_reddit_posts(subreddit, limit=5):
    url = f"https://old.reddit.com/r/{subreddit}/top.json?sort=top&t=week&limit={limit}"
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    posts = []
    for child in data.get("data", {}).get("children", []):
        p = child.get("data", {})
        posts.append({
            "id": p.get("id"),
            "title": p.get("title", ""),
            "selftext": p.get("selftext", ""),
            "score": p.get("score", 0),
            "num_comments": p.get("num_comments", 0),
            "subreddit": subreddit,
            "created_utc": p.get("created_utc", 0),
            "permalink": p.get("permalink", ""),
            "url": p.get("url", ""),
        })
    return posts

def fetch_reddit_comments(subreddit, post_id):
    url = f"https://old.reddit.com/r/{subreddit}/comments/{post_id}.json"
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if len(data) < 2:
        return []
    comments_data = data[1]["data"]["children"]
    top_comments = []
    for item in comments_data:
        if item["kind"] == "t1":
            c = item["data"]
            replies = []
            if c.get("replies") and c["replies"]["kind"] == "t1":
                reply_children = c["replies"]["data"]["children"]
                if reply_children:
                    top_reply = reply_children[0]["data"]["body"]
                else:
                    top_reply = ""
            else:
                top_reply = ""
            top_comments.append({
                "body": c.get("body", ""),
                "score": c.get("score", 0),
                "reply": top_reply,
            })
    top_comments.sort(key=lambda x: x["score"], reverse=True)
    return top_comments[:3]

# ----------------------------------------------------------------------
# DEEPSEEK
# ----------------------------------------------------------------------
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

def deepseek_summarize(candidates, reddit_posts, date_str):
    paper_text = ""
    for i, item in enumerate(candidates, 1):
        source = item.get("source", "Unknown")
        search_term = item.get("search_term", "")
        identifier = item.get("doi") or item.get("nct_id") or "N/A"
        journal = item.get("journal", "")
        lab = item.get("authors", [""])[-1] if item.get("authors") else "unknown"
        title = item.get("title", "")
        abstract = item.get("abstract", "")[:CAPS["abstract"]]
        pubpeer_summary = item.get("pubpeer_summary", "No PubPeer data")
        pubpeer_comments = item.get("pubpeer_comments", [])
        comment_lines = []
        for c in pubpeer_comments[:3]:
            comment_lines.append(f"Comment by {c.get('author','')}: {c.get('text','')}")
            if c.get('reply'):
                comment_lines.append(f"  Reply: {c.get('reply')}")
        pubpeer_text = pubpeer_summary + "\n" + "\n".join(comment_lines) if pubpeer_comments else pubpeer_summary
        paper_text += f"{i}. Source: {source} | Search: \"{search_term}\" | ID: {identifier} | Journal: {journal} | Lab: {lab} | Title: {title} | Abstract: {abstract} | PubPeer: {pubpeer_text}\n"

    reddit_text = ""
    for i, post in enumerate(reddit_posts, 1):
        sub = post.get("subreddit", "")
        title = post.get("title", "")
        selftext = post.get("selftext", "")[:CAPS["selftext"]]
        comments = post.get("comments", [])
        comment_lines = []
        for c in comments[:3]:
            comment_lines.append(f"Comment (score {c['score']}): {c['body']}")
            if c.get('reply'):
                comment_lines.append(f"  Reply: {c['reply']}")
        reddit_text += f"{i}. r/{sub}: \"{title}\"\n   Selftext: {selftext}\n   {chr(10).join(comment_lines)}\n"

    prompt = f"""
You are a scientific summariser. Your task is to produce a daily digest.

From the list of papers/preprints/trials below, select the **5 most significant** ones. For each, produce a summary of 4–6 sentences.
**Note:** Results from named author searches (Liu, Lee) must always be included – if they appear in the list, they take priority.

From the list of Reddit posts below, select the **5 most scientifically relevant discussions**. For each, produce a summary of 2–3 sentences.

**Output format** (use these sections exactly):

🧬 **{date_str}**

**Papers / Trials (most significant):**
1. [Journal/ClinicalTrials.gov via "search_term" or "author name"] ID: ... – summary – Lab name. PubPeer: [brief summary, e.g., "2 critical comments, question Fig.2's authenticity"].
2. ...

**Reddit (useful posts):**
1. r/subreddit: "Title" – summary of post, then summary of comments/replies, e.g., "Comments agree on usefulness but caution against high voltage." or "Comments answer with Gibson Assembly or TEDA as the best method".

**Rules for handling uncertainty:**
- If you encounter ambiguous terms, acronyms, or concepts you are unsure about, include them verbatim – do not guess, ignore, or hallucinate.
- You may use context and your trained knowledge, but do not invent missing information.
- Example: if you don't know "TEDA" but it appears important, mention it naturally (e.g., "comments suggest using TEDA or Gibson Assembly").
- If you cannot determine importance, err on the side of inclusion.

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
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
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
    resp.raise_for_status()
    logger.info("Telegram message sent.")

# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------
def main():
    logger.info("Starting daily digest...")
    sent = load_cache()
    now = datetime.utcnow()
    end_date = now - timedelta(days=PAPER_WINDOW_END_DAYS)
    start_date = now - timedelta(days=PAPER_WINDOW_START_DAYS)

    all_candidates = []
    seen_ids = set()

    # Helper to add a candidate after dedupe and sorting
    def add_candidate(cand, term_type, term_value):
        if not cand:
            return
        identifier = cand.get("doi") or cand.get("nct_id")
        if not identifier:
            return
        if identifier in seen_ids or identifier in sent:
            return
        cand["search_term"] = f"{term_type}: {term_value}"
        # Add PubPeer
        summary, comments = get_pubpeer_comments(identifier)
        cand["pubpeer_summary"] = summary
        cand["pubpeer_comments"] = comments
        all_candidates.append(cand)
        seen_ids.add(identifier)

    # 1a. Process KEYWORDS for PubMed and bioRxiv
    for term in KEYWORDS:
        raw = []
        # PubMed
        try:
            pmids = fetch_with_retry(pubmed_search_date_filtered, term, start_date, end_date, retmax=5)
            if pmids:
                records = fetch_with_retry(pubmed_fetch, pmids)
                for rec in records:
                    data = extract_pubmed_data(rec)
                    if data["doi"] and data["doi"] not in seen_ids and data["doi"] not in sent:
                        data["source"] = "PubMed"
                        raw.append(data)
        except Exception as e:
            logger.warning(f"PubMed error for {term}: {e}")
        # bioRxiv
        try:
            papers = fetch_with_retry(biorxiv_search, term, limit=20)
            for p in papers:
                d = parse_biorxiv_paper(p)
                if d["doi"] and d["doi"] not in seen_ids and d["doi"] not in sent:
                    try:
                        pub_date = datetime.strptime(d["date"], "%Y-%m-%d")
                        if start_date <= pub_date <= end_date:
                            d["source"] = "bioRxiv"
                            raw.append(d)
                    except:
                        continue
        except Exception as e:
            logger.warning(f"bioRxiv error for {term}: {e}")
        # Dedupe and pick oldest
        unique = {}
        for c in raw:
            key = c.get("doi")
            if key:
                unique[key] = c
        if unique:
            # sort by date
            def get_date(c):
                if c.get("source") == "bioRxiv":
                    try:
                        return datetime.strptime(c["date"], "%Y-%m-%d")
                    except:
                        return datetime.max
                else:
                    return parse_pubmed_date(c.get("date")) or datetime.max
            oldest = min(unique.values(), key=get_date)
            add_candidate(oldest, "search", term)

    # 1b. Process CT_KEYWORDS for ClinicalTrials only
    for term in CT_KEYWORDS:
        raw = []
        try:
            studies = fetch_with_retry(clinicaltrials_search_date_filtered, term, start_date, end_date, limit=5)
            for study in studies:
                data = parse_clinicaltrial(study)
                if data["nct_id"] and data["nct_id"] not in seen_ids and data["nct_id"] not in sent:
                    data["source"] = "ClinicalTrials.gov"
                    raw.append(data)
        except Exception as e:
            logger.warning(f"ClinicalTrials error for {term}: {e}")
        if raw:
            # Dedupe by NCT (should be unique anyway)
            unique = {c["nct_id"]: c for c in raw}
            # Pick oldest by date
            def get_date(c):
                return parse_clinicaltrial_date(c.get("date")) or datetime.max
            oldest = min(unique.values(), key=get_date)
            add_candidate(oldest, "CTsearch", term)

    # 1c. Named authors – search all three sources
    for author in AUTHOR_RULES:
        raw = []
        # PubMed
        for query in author["pubmed_queries"]:
            try:
                pmids = fetch_with_retry(pubmed_search_date_filtered, query, start_date, end_date, retmax=5)
                if pmids:
                    records = fetch_with_retry(pubmed_fetch, pmids)
                    for rec in records:
                        data = extract_pubmed_data(rec)
                        if data["doi"] and data["doi"] not in seen_ids and data["doi"] not in sent:
                            data["source"] = "PubMed"
                            raw.append(data)
            except Exception as e:
                logger.warning(f"Author PubMed error for {author['name']}: {e}")
        # bioRxiv
        try:
            papers = fetch_with_retry(biorxiv_search, author["biorxiv_query"], limit=20)
            for p in papers:
                d = parse_biorxiv_paper(p)
                if d["doi"] and d["doi"] not in seen_ids and d["doi"] not in sent:
                    try:
                        pub_date = datetime.strptime(d["date"], "%Y-%m-%d")
                        if start_date <= pub_date <= end_date:
                            d["source"] = "bioRxiv"
                            raw.append(d)
                    except:
                        continue
        except Exception as e:
            logger.warning(f"Author bioRxiv error for {author['name']}: {e}")
        # ClinicalTrials
        try:
            studies = fetch_with_retry(clinicaltrials_search_date_filtered, author["clintrials_query"], start_date, end_date, limit=5)
            for study in studies:
                data = parse_clinicaltrial(study)
                if data["nct_id"] and data["nct_id"] not in seen_ids and data["nct_id"] not in sent:
                    data["source"] = "ClinicalTrials.gov"
                    raw.append(data)
        except Exception as e:
            logger.warning(f"Author ClinicalTrials error for {author['name']}: {e}")

        # Dedupe across sources and pick oldest
        unique = {}
        for c in raw:
            key = c.get("doi") or c.get("nct_id")
            if key:
                unique[key] = c
        if unique:
            def get_date(c):
                if c.get("source") == "bioRxiv":
                    try:
                        return datetime.strptime(c["date"], "%Y-%m-%d")
                    except:
                        return datetime.max
                elif c.get("source") == "PubMed":
                    return parse_pubmed_date(c.get("date")) or datetime.max
                else:  # ClinicalTrials
                    return parse_clinicaltrial_date(c.get("date")) or datetime.max
            oldest = min(unique.values(), key=get_date)
            add_candidate(oldest, "author", author["name"])

    logger.info(f"Collected {len(all_candidates)} paper/trial candidates.")

    # ------------------------------------------------------------
    # 2. REDDIT
    # ------------------------------------------------------------
    reddit_posts = []
    for sub in SUBREDDITS:
        try:
            posts = fetch_reddit_posts(sub, limit=5)
            # Filter cached
            fresh = [p for p in posts if f"reddit_{sub}_{p['id']}" not in sent]
            if not fresh:
                continue
            # Take highest score
            top_post = max(fresh, key=lambda x: x["score"])
            # Fetch comments
            comments = fetch_reddit_comments(sub, top_post["id"])
            top_post["comments"] = comments
            reddit_posts.append(top_post)
        except Exception as e:
            logger.warning(f"Reddit error for {sub}: {e}")

    logger.info(f"Collected {len(reddit_posts)} Reddit posts.")

    # ------------------------------------------------------------
    # 3. DEEPSEEK
    # ------------------------------------------------------------
    if not all_candidates and not reddit_posts:
        send_telegram("⚠️ No new relevant papers, trials, or Reddit posts found today.")
        return

    date_str = now.strftime("%B %d, %Y")
    summary_lines = fetch_with_retry(deepseek_summarize, all_candidates, reddit_posts, date_str, retries=2, delay=10)

    final_msg = "\n".join(summary_lines)
    send_telegram(final_msg)

    # ------------------------------------------------------------
    # 4. UPDATE CACHE
    # ------------------------------------------------------------
    for item in all_candidates:
        key = item.get("doi") or item.get("nct_id")
        if key:
            sent[key] = time.time()
    for post in reddit_posts:
        key = f"reddit_{post['subreddit']}_{post['id']}"
        sent[key] = time.time()
    save_cache(sent)

    logger.info("Digest sent successfully.")

if __name__ == "__main__":
    main()
