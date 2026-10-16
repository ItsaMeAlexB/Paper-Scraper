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
TOP_N = 5  # how many top titles survive stage-1 ranking for the novelty check
# Stage-1 scores one paper batch per call; large batches make deepseek-chat
# truncate at max_tokens and abandon the [[rel,sig],...] format, so keep each
# call small.
SCORE_BATCH = 100

def _extract_json_array(content, expected_len):
    """Pull expected_len JSON entries out of model text. Pure.

    1) Preferred: a single wrapping array of expected_len entries.
    2) Fallback: consecutive standalone JSON values (e.g. newline-separated
       [rel,sig] pairs) that deepseek-chat sometimes emits instead of one array.
    """
    start = content.find("[")
    if start != -1:
        end = content.rfind("]")
        if end > start:
            try:
                arr = json.loads(content[start:end + 1])
                if isinstance(arr, list) and len(arr) == expected_len:
                    return arr
            except ValueError:
                pass
    # Fallback: parse each standalone JSON value in order (unwrap 1-element
    # sub-arrays like ["txt"] -> "txt").
    dec = json.JSONDecoder()
    out = []
    i = content.find("[")
    while i != -1 and len(out) < expected_len:
        try:
            val, idx = dec.raw_decode(content[i:])
        except ValueError:
            break
        if isinstance(val, list) and len(val) == 1:
            val = val[0]
        out.append(val)
        i = content.find("[", i + idx)
    if len(out) == expected_len:
        return out
    raise ValueError(f"expected {expected_len} entries, got {len(out)} (content[:200]={content[:200]!r})")

def _deepseek_json_array(prompt, expected_len):
    """Run one DeepSeek call and return the first JSON array in the response.

    Shared by the three selection stages (score, condense, novelty). Raises on
    any error or a wrong-length/unusable response; callers fall back.
    """
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": SCORE_MAX_TOKENS,
    }
    resp = requests.post(DEEPSEEK_URL, json=payload, headers=headers, timeout=240)
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
    return _extract_json_array(content, expected_len)

def _score_papers(papers, prompt):
    """Score each paper; returns list of (relevance, significance) pairs."""
    arr = _deepseek_json_array(prompt, len(papers))
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

def deepseek_score_papers(term_name, papers):
    """Stage 1: score each paper on relevance & significance. Prompt unchanged.

    Scored in SCORE_BATCH-sized chunks so the model never has to emit one huge
    array (which truncates at max_tokens and loses the [rel,sig] format).
    """
    scores = []
    for i in range(0, len(papers), SCORE_BATCH):
        batch = papers[i:i + SCORE_BATCH]
        titles_text = "\n".join([f"{j+1}. {p['title']}" for j, p in enumerate(batch)])
        prompt = f"""You are scoring papers for a researcher who reads primary sources.

For each title below (topic: "{term_name}"), output exactly one JSON array of
[relevance, significance] pairs, in the SAME order as the titles, nothing else.

Scoring rules (each score is an integer 0-10):
- relevance: how directly the paper is about "{term_name}" itself (not just using it as a method or just mentioning it in passing), AND how directly it pertains to human. Non-human, non-human cell, and non-animal model work scores low here unless described as translational to humans (they may say "would also work in eukarya", which is relevant).
- significance: how much the paper advances the field (new data, new method, new model, or a landmark benchmark/meta-analysis). If the paper is NOT a primary source (review, editorial, news, etc), it scores lowely on significance. Suspected paper mills (possibly identified by the publising journal's reputation), should also be scored lowely.

Titles:
{titles_text}

Return only a JSON array like [[rel,sig],[rel,sig],...] with exactly {len(batch)} entries, one per title, in order. No prose, no code fences.
"""
        scores.extend(_score_papers(batch, prompt))
    return scores

def deepseek_condense_titles(papers):
    """Stage 2: condense each title into a short generic 'what they did' phrase.

    Returns list of short plain-language descriptions (same order as papers),
    phrased to work as PubMed keyword searches.
    """
    titles_text = "\n".join([f"{i+1}. {p['title']}" for i, p in enumerate(papers)])
    prompt = f"""For a researcher, rewrite each title below as a short plain-language description of what the paper DID — a generic explanation of the work, not the catchy title. Examples: "a model designed to predict large serine recombinase off-targets", "a lipid nanoparticle formulated to avoid hepatic accumulation". Phrase it so a PubMed keyword search would find papers doing the same kind of work.

Titles:
{titles_text}

Output exactly one JSON array of {len(papers)} strings (one per title, same order), nothing else. No prose, no code fences.
"""
    arr = _deepseek_json_array(prompt, len(papers))
    out = []
    for s in arr:
        if not isinstance(s, str) or not s.strip():
            raise ValueError(f"malformed condensed title: {s!r}")
        out.append(s.strip())
    return out

# How many similar titles to pull per stage-3 search, and the fetch buffer so
# that excluding the original paper still leaves SIMILAR_TOP titles.
SIMILAR_TOP = 5
SIMILAR_FETCH = 8

def search_similar_titles(query, exclude_pmids=None):
    """Search all of PubMed for papers doing what `query` describes.

    Returns up to SIMILAR_TOP titles most similar to the query, dropping any
    PMID in exclude_pmids (the original paper itself is its own top match).
    Sorted by relevance, all-time, so long-existing work is surfaced.
    """
    Entrez.email = ENTREZ_EMAIL
    exclude_pmids = exclude_pmids or set()
    try:
        handle = Entrez.esearch(db="pubmed", term=query, retmax=SIMILAR_FETCH, sort="relevance")
    except Exception:
        # shortcut: some NCBI mirrors reject an explicit sort; fall back to default.
        handle = Entrez.esearch(db="pubmed", term=query, retmax=SIMILAR_FETCH)
    record = Entrez.read(handle)
    handle.close()
    time.sleep(0.3)
    pmids = [p for p in record["IdList"] if p not in exclude_pmids][:SIMILAR_TOP]
    if not pmids:
        return []
    return [p["title"] for p in fetch_metadata(pmids)]

def deepseek_reevaluate_novelty(papers, similar_by_idx):
    """Stage 4: score each top paper's novelty against similar papers in PubMed.

    Returns list of int novelty scores 0-10 (same order as papers).
    """
    blocks = []
    for i, p in enumerate(papers):
        lines = [f"{i+1}. ORIGINAL: {p['title']}"]
        sims = similar_by_idx.get(i, [])
        if sims:
            lines.append("   Similar papers already in PubMed:")
            lines += [f"   - {t}" for t in sims]
        else:
            lines.append("   (no closely similar papers found)")
        blocks.append("\n".join(lines))
    body = "\n\n".join(blocks)
    prompt = f"""You are judging whether each paper below is actually NOVEL compared with papers that already exist in PubMed. Consider ONLY novelty — ignore relevance and significance.

For each ORIGINAL paper, weigh the "Similar papers already in PubMed" listed under it and score novelty 0-10:
- 10 = the similar papers do NOT do the same thing (a new method, target, or result the existing literature lacks).
- 5 = partial overlap: related but with a real difference.
- 0 = the same work (or essentially the same result) already exists in PubMed.

{body}

Output exactly one JSON array of {len(papers)} integers (one novelty score per ORIGINAL paper, same order), nothing else. No prose, no code fences.
"""
    arr = _deepseek_json_array(prompt, len(papers))
    return [min(10, max(0, int(float(v)))) for v in arr]

def _pick_winner(n, products, novelty):
    """Index in [0, n) of the most novel paper; ties by stage-1 product, then random."""
    return min(range(n), key=lambda j: (-novelty[j], -products[j], random.random()))

def select_paper(term_name, papers):
    """Pick the single most relevant, significant, and NOVEL paper.

    Stages: score all titles (same prompt as before) -> condense the top 5 into
    generic 'what they did' phrases -> search PubMed for each phrase and collect
    the 5 most similar existing titles -> score each top paper's novelty against
    those (novelty only). Winner = the top-5 paper with the highest novelty score;
    ties broken by the stage-1 relevance*significance product, then randomly.

    Returns (selected_title_or_None, note_or_None). On any failure we fall back
    to the stage-1 top-ranked paper so the digest still goes out.
    """
    if not papers:
        return None, None
    if not DEEPSEEK_API_KEY:
        return papers[0]['title'], "DEEPSEEK_API_KEY not set; used first paper"
    if len(papers) < 2:
        # A single paper is the only choice; nothing to rank or compare against.
        return papers[0]['title'], None

    try:
        scores = deepseek_score_papers(term_name, papers)
        ranking = sorted(range(len(papers)), key=lambda i: scores[i][0] * scores[i][1], reverse=True)
    except Exception as e:
        logger.error(f"DeepSeek scoring error: {e}")
        return papers[0]['title'], f"DeepSeek scoring error ({e}); used first paper"

    top = ranking[:TOP_N]
    products = [scores[i][0] * scores[i][1] for i in top]
    fallback_title = papers[top[0]]['title']

    # Stage 2 — condense the top titles into generic search phrases.
    try:
        phrases = deepseek_condense_titles([papers[i] for i in top])
    except Exception as e:
        logger.error(f"DeepSeek condense error: {e}")
        return fallback_title, f"Novelty check skipped (condense error {e}); used stage-1 top paper"

    # Stage 3 — PubMed similarity searches, keyed by position within `top`.
    similar_by_idx = {}
    searches_failed = False
    for k, i in enumerate(top):
        try:
            sims = search_similar_titles(phrases[k], {papers[j].get("pmid") for j in top})
        except Exception as e:
            logger.warning(f"Similarity search failed for '#{k+1} {phrases[k]}': {e}")
            searches_failed = True
            sims = []
        if sims:
            similar_by_idx[k] = sims
    if not similar_by_idx:
        return fallback_title, "Novelty check skipped (no similarity search results); used stage-1 top paper"

    # Stage 4 — novelty-only reevaluation.
    try:
        novelty = deepseek_reevaluate_novelty([papers[i] for i in top], similar_by_idx)
    except Exception as e:
        logger.error(f"DeepSeek novelty error: {e}")
        return fallback_title, f"Novelty check skipped (novelty error {e}); used stage-1 top paper"

    winner_k = _pick_winner(len(top), products, novelty)
    idx = top[winner_k]
    logger.info(
        f"DeepSeek scored {len(papers)} papers; top-{len(top)} novelty="
        f"{[novelty[j] for j in range(len(top))]} for "
        f"{[papers[top[j]]['title'][:40] for j in range(len(top))]}; "
        f"winner #{idx+1} (novelty={novelty[winner_k]}): {papers[idx]['title']}"
    )
    note = "One or more PubMed similarity searches failed; novelty check is partial" if searches_failed else None
    return papers[idx]['title'], note

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
            selected_title, sel_note = select_paper(term_name, papers)
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
