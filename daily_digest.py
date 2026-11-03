#!/usr/bin/env python3
import os
import sys
import time
import re
import html
import json
import logging
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from string import Template

import requests
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
NCBI_API_KEY = os.environ.get("NCBI_API_KEY")   # optional; lifts 3/s -> 10/s

REQUIRED_ENV = ["DEEPSEEK_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                "ENTREZ_EMAIL"]

LAST_SENT_FILE = os.path.join(BASE_DIR, "last_sent_date.txt")
CONFIG_FILE = os.path.join(BASE_DIR, "sources.txt")
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "daily_digest.log")
ROTATION_MARKER = os.path.join(LOG_DIR, ".last_rotation")

LOG_ROTATION_SECONDS = 7 * 24 * 3600  # 7 days

RUN_HOUR_UTC = 5          # off-peak window is 04:00-06:00 UTC
TELEGRAM_LIMIT = 4096     # characters per Telegram message
REVIEW_CHAR_CAP = 4096    # character cap handed to the model
NCBI_TOOL = "paper-scraper"   # E-utilities identification; reduces throttling
NCBI_DELAY = 0.5              # seconds between E-utilities calls (NCBI asks <=3/s)

# =============================================================================
# LOGGING (weekly overwrite)
# =============================================================================

def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    now = time.time()
    try:
        with open(ROTATION_MARKER) as f:
            last = float(f.read().strip())
    except Exception:
        last = None

    if last is None or now - last > LOG_ROTATION_SECONDS:
        # Truncate every log file, so disk use stays bounded by one rotation
        # window however long the job keeps running.
        for name in os.listdir(LOG_DIR):
            if name.endswith(".log"):
                open(os.path.join(LOG_DIR, name), "w").close()
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
# CONFIG VALIDATION & LOADING
# =============================================================================

def _check_env():
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        logger.error(
            f"Missing environment variables: {', '.join(missing)}. "
            f"Copy .env.example to .env and fill it in."
        )
        sys.exit(1)

_check_env()

def load_config():
    if not os.path.exists(CONFIG_FILE):
        logger.error(
            f"Config file {CONFIG_FILE} not found. Copy sources.txt.example "
            f"to sources.txt and fill in [TERMS] and [AUTHORS]."
        )
        sys.exit(1)
    with open(CONFIG_FILE) as f:
        lines = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    terms, authors = [], []
    current = None
    for line in lines:
        if line.startswith("[TERMS]"):
            current = "terms"
            continue
        elif line.startswith("[AUTHORS]"):
            current = "authors"
            continue
        elif line.startswith("[SUBREDDITS]"):
            current = None      # retired section; tolerated so old configs still load
            continue
        if current == "terms":
            terms.append(line)
        elif current == "authors":
            authors.append(line)

    if not terms:
        logger.error("Config must have at least one term.")
        sys.exit(1)
    return terms, authors

TERMS, AUTHORS = load_config()
AUTHOR_QUERIES = [f'"{a}"[AU]' for a in AUTHORS]
logger.info(f"Loaded {len(TERMS)} terms, {len(AUTHORS)} authors")

# =============================================================================
# PUBMED
# =============================================================================

def build_query(term):
    # Wrap plain phrases so PubMed searches them as a phrase. If the term is
    # already a boolean query (has OR/AND or quotes), pass it through verbatim.
    if ' OR ' in term or ' AND ' in term or '"' in term:
        return term
    return f'"{term}"'

# NCBI E-utilities over plain HTTP — drops the biopython (and its numpy) dependency.
ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"

def _eutils_params(**extra):
    """Shared E-utilities params. An NCBI API key is optional, but when present
    it lifts the rate limit from 3/s to 10/s."""
    params = {"db": "pubmed", "email": ENTREZ_EMAIL, "tool": NCBI_TOOL}
    if NCBI_API_KEY:
        params["api_key"] = NCBI_API_KEY
    params.update(extra)
    return params

def _ncbi_json(url, params, attempts=3):
    """GET an E-utilities JSON endpoint. NCBI intermittently answers with an
    error payload or a 429 instead of the expected key; one bad response must not
    kill an unattended run, so back off, retry, then degrade to {}."""
    name = url.rsplit("/", 1)[-1]
    for attempt in range(1, attempts + 1):
        wait = 2 ** attempt                     # 2s, 4s, 8s
        try:
            resp = requests.get(url, params=params, timeout=60)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            # Never interpolate the exception text: it embeds the request URL,
            # and that URL carries the NCBI api_key.
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status == 429:
                retry_after = e.response.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    wait = int(retry_after)
            detail = f"HTTP {status}" if status else type(e).__name__
            logger.warning(f"NCBI {name} attempt {attempt}/{attempts} failed: {detail}")
            if status == 400 and params.pop("api_key", None):
                # A rejected key must not cost us the whole digest: drop it and
                # retry immediately at the anonymous 3 req/s rate.
                logger.warning(f"NCBI {name} rejected the api_key; retrying without it")
                continue
        else:
            if not data.get("error"):
                return data
            logger.warning(f"NCBI {name} error payload: {str(data['error'])[:200]}")
        if attempt < attempts:
            time.sleep(wait)
    logger.error(f"NCBI {name} gave up after {attempts} attempts")
    return {}

def fetch_all_pmids(query, start_date, end_date, max_results=10000):
    start_str = start_date.strftime("%Y/%m/%d")
    end_str = end_date.strftime("%Y/%m/%d")
    pmids = []
    retstart = 0
    while True:
        params = _eutils_params(
            term=query, mindate=start_str, maxdate=end_str,
            # edat = date the record entered PubMed. That is "new to us", and
            # unlike pdat it is never month-granular, so a one-day window means
            # one day. NCBI defaults to edat, but say it out loud.
            datetype="edat",
            retstart=retstart, retmax=1000, sort="pub date", retmode="json",
        )
        record = _ncbi_json(ESEARCH_URL, params).get("esearchresult", {})
        batch = record.get("idlist", [])
        if not batch:
            break
        pmids.extend(batch)
        retstart += 1000
        if retstart >= int(record.get("count", 0)) or retstart >= max_results:
            break
        time.sleep(NCBI_DELAY)
    return pmids

def fetch_metadata(pmids):
    if not pmids:
        return []
    papers = []
    for i in range(0, len(pmids), 200):
        chunk = pmids[i:i+200]
        params = _eutils_params(id=",".join(chunk), retmode="json")
        result = _ncbi_json(ESUMMARY_URL, params).get("result", {})
        if not result:
            logger.warning(f"esummary returned nothing for {len(chunk)} pmid(s)")
            continue
        for pmid in chunk:
            rec = result.get(pmid)
            if not rec:
                continue
            authors = [a.get("name", "").strip()
                       for a in rec.get("authors", []) if a.get("name")]
            if not authors:
                continue
            doi = next((a.get("value") for a in rec.get("articleids", [])
                        if a.get("idtype") == "doi"), None)
            papers.append({
                "doi": doi,
                "title": rec.get("title", ""),
                "authors": authors,
                "journal": rec.get("fulljournalname", ""),
                "date": rec.get("pubdate", ""),
                "pmid": pmid,
            })
        time.sleep(NCBI_DELAY)
    return papers

EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

def fetch_abstracts(pmids):
    """Return {pmid: abstract_text}. Papers efetch cannot resolve are simply
    absent from the dict; the caller reports the gap."""
    abstracts = {}
    for i in range(0, len(pmids), 100):
        chunk = pmids[i:i+100]
        params = _eutils_params(id=",".join(chunk), rettype="abstract", retmode="xml")
        try:
            resp = requests.get(EFETCH_URL, params=params, timeout=60)
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
        except Exception as e:
            logger.error(f"efetch error: {e}")
            continue
        for art in root.iter("PubmedArticle"):
            pmid = (art.findtext("./MedlineCitation/PMID") or "").strip()
            parts = []
            for node in art.iterfind("./MedlineCitation/Article/Abstract/AbstractText"):
                text = "".join(node.itertext()).strip()
                if not text:
                    continue
                label = (node.get("Label") or "").strip()
                parts.append(f"{label}: {text}" if label else text)
            if pmid and parts:
                abstracts[pmid] = " ".join(parts)
        time.sleep(NCBI_DELAY)

    missing = len(pmids) - len(abstracts)
    if missing:
        logger.warning(f"efetch returned no abstract for {missing} paper(s)")
    return abstracts

# =============================================================================
# DEEPSEEK
# =============================================================================

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
# Triage is a structured selection task, so it runs on the cheaper chat model in
# JSON mode. The review is judgement + prose, so chain-of-thought is an asset and
# it runs on the reasoner, which picks its own output budget (no max_tokens).
DEEPSEEK_CHAT_MODEL = "deepseek-chat"
DEEPSEEK_REASONER_MODEL = "deepseek-reasoner"

def load_prompt(filename):
    """Prompts live beside the script, not in it. They are local-only files
    (gitignored) created by setup_local.sh from the .example templates."""
    path = os.path.join(BASE_DIR, filename)
    if not os.path.exists(path):
        logger.error(
            f"Prompt file {filename} not found. Copy {filename}.example to "
            f"{filename} and edit it."
        )
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        return Template(f.read())

TRIAGE_TEMPLATE = load_prompt("prompt_triage.txt")
REVIEW_TEMPLATE = load_prompt("prompt_review.txt")

# Model emits [[REF:n]]; we render the anchor. Tokens are tiny, so this saves
# context and makes malformed HTML impossible by construction.
REF_RE = re.compile(r"\[\[REF:(\d+)\]\]")
TAG_RE = re.compile(r"<[^>]+>")
UNIT_RE = re.compile(r'<a [^>]*>[^<]*</a>|\s+|[^\s<]+', re.S)

def _paper_key(p):
    return p.get("pmid") or p.get("doi") or p.get("title", "").strip().lower()

def _paper_link(p):
    if p.get("doi"):
        return f"https://doi.org/{p['doi']}"
    return f"https://pubmed.ncbi.nlm.nih.gov/{p['pmid']}/"

def _last_author(p):
    return p["authors"][-1] if p.get("authors") else "Unknown"

def _deepseek_text(prompt, model=DEEPSEEK_REASONER_MODEL,
                   response_format=None, max_tokens=None):
    """Run one DeepSeek call and return the response text. Retries once on an
    empty response; HTTP errors propagate."""
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}",
               "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }
    if response_format:
        payload["response_format"] = response_format
    if max_tokens:
        payload["max_tokens"] = max_tokens
    last_err = None
    for attempt in (0, 1):
        resp = requests.post(DEEPSEEK_URL, json=payload, headers=headers, timeout=600)
        resp.raise_for_status()
        data = resp.json()
        choice = data["choices"][0]
        content = (choice["message"].get("content") or "").strip()
        if content:
            return content
        last_err = ValueError(
            f"empty content (finish_reason={choice.get('finish_reason')}, "
            f"usage={data.get('usage')})"
        )
        logger.warning(f"DeepSeek empty response (attempt {attempt+1}/2)")
        time.sleep(1)
    raise last_err

def _render_citations(text, links):
    """Escape prose, then replace [[REF:n]] with a clickable anchor labelled [n].
    Unknown refs (model hallucination) are left as plain text and logged."""
    safe = html.escape(text, quote=False)   # & < > only

    def sub(m):
        n = int(m.group(1))
        url = links.get(n)
        if not url:
            logger.warning(f"unknown reference [[REF:{n}]] in scoping review")
            return f"[{n}]"
        return f'<a href="{html.escape(url, quote=True)}">[{n}]</a>'

    return REF_RE.sub(sub, safe)

def _visible(s):
    return html.unescape(TAG_RE.sub("", s))

def _utf16_len(s):
    return len(s.encode("utf-16-le")) // 2   # Telegram counts UTF-16 units

def _chunk_html(text, limit=TELEGRAM_LIMIT):
    """Split HTML into chunks whose visible UTF-16 length is <= limit. Anchors
    are atomic units, so a chunk can never end inside a tag."""
    chunks, cur, cur_len = [], [], 0
    for unit in UNIT_RE.findall(text):
        n = _utf16_len(_visible(unit))
        if cur and cur_len + n > limit:
            chunks.append("".join(cur).strip())
            cur, cur_len = [], 0
            if not unit.strip():        # skip leading whitespace after split
                continue
        cur.append(unit)
        cur_len += n
    if cur:
        chunks.append("".join(cur).strip())
    return chunks

def _terms_label():
    return ", ".join(TERMS)

def _format_entry(n, paper, abstract=None):
    line = (f"[{n}] {paper['title']} — {_last_author(paper)} — "
            f"{paper.get('journal', 'Unknown')}")
    if abstract is not None:
        line += f"\nAbstract: {abstract}"
    return line

def _block(header, lines):
    return header + "\n" + ("\n".join(lines) if lines else "(none)")

def deepseek_pick(papers):
    """Call 1: pick the topic papers worth reviewing, on titles alone.
    `papers` is [(number, paper)]. Returns the kept subset in order. A parse
    failure keeps everything — losing a whole digest is worse than a bad filter."""
    if not papers:
        return []
    body = _block("TOPIC-SEARCH PAPERS", [_format_entry(n, p) for n, p in papers])
    prompt = TRIAGE_TEMPLATE.safe_substitute(terms=_terms_label(), body=body)
    text = _deepseek_text(prompt, model=DEEPSEEK_CHAT_MODEL,
                          response_format={"type": "json_object"}, max_tokens=400)

    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        logger.error("Triage returned no JSON — keeping all papers")
        return papers
    try:
        selected = {int(x) for x in json.loads(match.group(0)).get("selected", [])}
    except Exception as e:
        logger.error(f"Triage JSON unparseable ({e}) — keeping all papers")
        return papers
    return [(n, p) for n, p in papers if n in selected]

def deepseek_review(topic, named, abstracts):
    """Call 2: re-judge the selection against full abstracts and write the
    review. `topic` and `named` are [(number, paper)]; numbers are shared with
    call 1 so [[REF:n]] stays valid."""
    def entries(pairs):
        return [_format_entry(n, p, abstracts.get(p.get("pmid"), "Abstract unavailable."))
                for n, p in pairs]

    body = "\n\n".join([
        _block("TOPIC-SEARCH PAPERS", entries(topic)),
        _block("NAMED-AUTHOR PAPERS", entries(named)),
    ])
    prompt = REVIEW_TEMPLATE.safe_substitute(terms=_terms_label(), body=body,
                                             cap=REVIEW_CHAR_CAP)
    return _deepseek_text(prompt)

# =============================================================================
# TELEGRAM
# =============================================================================

def send_telegram(message, parse_mode=None):
    if not message or not message.strip():
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    if parse_mode:
        payload["parse_mode"] = parse_mode
        payload["disable_web_page_preview"] = True
    try:
        requests.post(url, json=payload, timeout=30).raise_for_status()
        logger.info(f"Telegram sent: {_visible(message)[:60]}")
    except Exception as e:
        logger.error(f"Telegram error: {e}")
        raise

# =============================================================================
# DAILY STATE
# =============================================================================

def days_since_last_sent():
    """None = never sent. 0 = sent today (skip). N>=1 = last sent N days ago.
    There is no catch-up: a missed day is skipped and the next run simply picks
    up the newest window."""
    if not os.path.exists(LAST_SENT_FILE):
        return None
    try:
        with open(LAST_SENT_FILE) as f:
            last = datetime.strptime(f.read().strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc).date() - last.date()).days)
    except Exception:
        return None

def mark_sent_today():
    with open(LAST_SENT_FILE, "w") as f:
        f.write(datetime.now(timezone.utc).strftime("%Y-%m-%d"))

# =============================================================================
# ONE DIGEST RUN
# =============================================================================

def run_digest(now):
    """One digest run over the single previous UTC day. Every term and every
    named author is searched; there is no rotation. Returns a list of problems."""
    problems = []
    day = (now - timedelta(days=1)).date()
    logger.info(f"Window: {day} (1 day); {len(TERMS)} terms, {len(AUTHORS)} authors")

    # Topic search: every term, deduped globally so two terms cannot give the
    # same paper two numbers.
    papers, seen = [], set()
    for term in TERMS:
        pmids = fetch_all_pmids(build_query(term), day, day)
        fresh = 0
        for p in (fetch_metadata(pmids) if pmids else []):
            key = _paper_key(p)
            if key in seen:
                continue
            seen.add(key)
            papers.append(p)
            fresh += 1
        logger.info(f"'{term}': {fresh} new paper(s)")
        time.sleep(NCBI_DELAY)
    if not papers:
        problems.append(f"PubMed: zero papers for the {day} window")

    # Named authors: mandatory, never triaged out.
    named_papers = []
    for query in AUTHOR_QUERIES:
        pmids = fetch_all_pmids(query, day, day)
        for p in (fetch_metadata(pmids) if pmids else []):
            key = _paper_key(p)
            if key in seen:
                continue
            seen.add(key)
            named_papers.append(p)
        time.sleep(NCBI_DELAY)
    logger.info(f"{len(named_papers)} named-author paper(s)")

    if not papers and not named_papers:
        return problems

    # Numbering spans both calls so [[REF:n]] stays valid across them.
    topic = list(enumerate(papers, 1))
    named = list(enumerate(named_papers, len(papers) + 1))
    links = {n: _paper_link(p) for n, p in topic + named}

    kept = deepseek_pick(topic)
    logger.info(f"Triage kept {len(kept)}/{len(topic)} topic paper(s)")
    if not kept and not named:
        problems.append("Triage kept no papers")
        return problems

    selected = kept + named
    abstracts = fetch_abstracts([p["pmid"] for _, p in selected if p.get("pmid")])
    no_abstract = [n for n, p in selected if p.get("pmid") not in abstracts]
    if no_abstract:
        problems.append(f"No abstract for paper(s) {no_abstract}")

    summary = deepseek_review(kept, named, abstracts)
    rendered = _render_citations(summary, links)
    logger.info(f"Review: {len(_visible(rendered))} chars")
    for chunk in _chunk_html(rendered):
        send_telegram(chunk, parse_mode="HTML")

    return problems

# =============================================================================
# MAIN
# =============================================================================

def main():
    try:
        now = datetime.now(timezone.utc)
        logger.info(f"UTC: {now.strftime('%Y-%m-%d %H:%M:%S')}")

        # One daily slot, inside the 04:00-06:00 UTC DeepSeek off-peak window.
        # Cron fires once at 05:00; there is no retry and no catch-up, so a
        # failed day is simply skipped.
        if now.hour != RUN_HOUR_UTC:
            logger.info(f"Outside {RUN_HOUR_UTC:02d}:00 UTC; will retry later")
            return

        if days_since_last_sent() == 0:
            logger.info("Already sent today, skipping")
            return

        problems = run_digest(now)

        if problems:
            send_telegram("⚠️ Digest issues:\n- " + "\n- ".join(problems))

        mark_sent_today()
        logger.info("Digest sent.")

    except Exception as e:
        logger.error(f"Unhandled exception: {e}")
        logger.error(traceback.format_exc())
        raise

if __name__ == "__main__":
    main()
