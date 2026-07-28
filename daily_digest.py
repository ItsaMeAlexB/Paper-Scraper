#!/usr/bin/env python3
import os
import sys
import re
import time
import json
import logging
import random
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import requests
from Bio import Entrez, Medline

# ----------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ENTREZ_EMAIL = os.environ.get("ENTREZ_EMAIL")

# File to track last successful run (timestamp)
LAST_RUN_FILE = "/tmp/daily_digest_last_run.txt"

# DeepSeek peak pricing windows in UTC
# Morning peak: 01:00-04:00 UTC (09:00-12:00 Beijing)
# Afternoon peak: 06:00-10:00 UTC (14:00-18:00 Beijing)
PEAK_WINDOWS = [(1, 4), (6, 10)]  # (start_hour, end_hour) inclusive start, exclusive end
PEAK_BUFFER_MINUTES = 30
COOLDOWN_HOURS = 12

# Search terms – only original research papers will be kept (authors must exist)
TERMS = {
    "Gene Therapy": '"gene therapy"',
    "Editing": '"gene edit*"',
    "Computation": '"computational biology"',
    "Delivery": '"vector"',
}

AUTHOR_TERMS = [
    '"David R Liu"[AU]',
    '"Ciaran M Lee"[AU]',
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


# =============================================================================
# DEEPSEEK PEAK PRICING CHECKS
# =============================================================================

def is_deepseek_peak_time(dt: datetime) -> bool:
    """
    Check if given datetime falls inside DeepSeek peak pricing.
    Peak windows in UTC: 01:00-04:00 and 06:00-10:00
    """
    hour = dt.hour
    for start, end in PEAK_WINDOWS:
        if start <= hour < end:
            return True
    return False


def is_within_buffer_of_peak(dt: datetime, buffer_minutes: int) -> bool:
    """
    Check if dt is within buffer_minutes BEFORE peak starts.
    Returns True if a peak window starts within buffer_minutes.
    """
    hour = dt.hour
    minute = dt.minute

    # Check each peak window start time
    for start_hour, _ in PEAK_WINDOWS:
        # Calculate minutes until this peak window starts
        # Handle the case where peak starts at the same hour but later minute
        if hour < start_hour:
            # Peak is later today
            minutes_until = (start_hour - hour) * 60 - minute
        elif hour == start_hour:
            # Peak starts at this hour, at minute 0
            minutes_until = -minute  # negative if we're past the start
        else:
            # Peak was earlier today, check tomorrow's start
            # We don't need to check this because we only care about the next peak
            # which will be handled by the main logic
            continue

        if 0 < minutes_until <= buffer_minutes:
            return True

    return False


def has_run_recently(dt: datetime, cooldown_hours: int) -> tuple[bool, str]:
    """Check if we've run successfully within the last N hours."""
    if not os.path.exists(LAST_RUN_FILE):
        return False, "No previous run found"

    try:
        with open(LAST_RUN_FILE, "r") as f:
            last_run_str = f.read().strip()
        last_run = datetime.fromisoformat(last_run_str)
    except (ValueError, FileNotFoundError):
        return False, "Corrupted or missing timestamp file"

    time_since = dt - last_run
    hours_since = time_since.total_seconds() / 3600.0

    if hours_since < cooldown_hours:
        return False, f"Last run was {hours_since:.1f}h ago (< {cooldown_hours}h cooldown)"

    return True, f"Cooldown satisfied ({hours_since:.1f}h since last run)"


def mark_run(dt: datetime):
    """Record the timestamp of this successful run."""
    with open(LAST_RUN_FILE, "w") as f:
        f.write(dt.isoformat())


def should_run() -> tuple[bool, str]:
    """Determine if the job should run now."""
    now = datetime.now(timezone.utc)

    # 1. Check cooldown (12 hours)
    ok, msg = has_run_recently(now, COOLDOWN_HOURS)
    if not ok:
        return False, msg

    # 2. Check if currently inside peak
    if is_deepseek_peak_time(now):
        return False, f"Inside peak window (UTC {now.strftime('%H:%M')})"

    # 3. Check if within 30-min buffer before peak
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
    """Fetch metadata and filter out papers with no authors (e.g., news, highlights)."""
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
        # Skip if no authors (likely news, highlights, editorials)
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


def deepseek_select_titles(term_papers):
    """Select one title per section (single call)."""
    sections_text = ""
    for section_name, papers in term_papers.items():
        sections_text += f"{section_name}:\n"
        if not papers:
            sections_text += "NO_TITLES\n\n"
        else:
            for i, p in enumerate(papers, 1):
                sections_text += f"{i}. {p['title']}\n"
            sections_text += "\n"

    prompt = f"""For each of the following topics, from the list of titles provided, pick the 1 most useful paper based on its title (e.g. if it's a review it would explain something well), significant (e.g. if it's primary article it has interesting findings), and relevant (e.g. not just an article that mentions gene editing in passing or uses it as a method for investigating an irrelevant topic).

Return your answer in the exact format below, with one title per topic. Use the exact titles as they appear.

Topics and titles:

{sections_text}

If the list is empty, return "NO_TITLES".

Format:
Gene Therapy: <exact title>
Editing: <exact title>
Computation: <exact title>
Delivery: <exact title>
"""

    payload = {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 2000,
    }
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}

    try:
        resp = requests.post(DEEPSEEK_URL, json=payload, headers=headers, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"].strip()
        if content:
            return content
        else:
            logger.warning("DeepSeek returned empty content – using fallback (first titles).")
            selected = {}
            for section_name, papers in term_papers.items():
                selected[section_name] = papers[0]['title'] if papers else "NO_TITLES"
            response = "\n".join([f"{k}: {v}" for k, v in selected.items()])
            return response
    except Exception as e:
        logger.error(f"Selection error: {e}")
        # Fallback to first titles
        selected = {}
        for section_name, papers in term_papers.items():
            selected[section_name] = papers[0]['title'] if papers else "NO_TITLES"
        response = "\n".join([f"{k}: {v}" for k, v in selected.items()])
        return response


def deepseek_summarize(selected_papers, author_papers, date_str):
    section_text = ""
    for section_name, paper in selected_papers.items():
        if paper:
            section_text += f"[{section_name}]\n"
            section_text += f"Title: {paper['title']}\n"
            section_text += f"Journal: {paper['journal']}\n"
            section_text += f"DOI: {paper['doi'] or 'N/A'}\n"
            section_text += f"Authors: {', '.join(paper['authors'][:5])}\n"
            section_text += f"Abstract: {paper['abstract'][:1500]}\n\n"

    author_text = ""
    if author_papers:
        author_text = "[Named Authors]\n"
        for paper in author_papers:
            author_text += f"Title: {paper['title']}\n"
            author_text += f"Journal: {paper['journal']}\n"
            author_text += f"DOI: {paper['doi'] or 'N/A'}\n"
            author_text += f"Authors: {', '.join(paper['authors'][:5])}\n"
            author_text += f"Abstract: {paper['abstract'][:1500]}\n\n"

    prompt = f"""You are a scientific summariser. Your task is to produce a daily digest.

Today is {date_str}.

For each of the following sections, there is a selected paper. Write a summary of 4–6 sentences that captures the key findings, significance, and context.

Sections:
{section_text}

Additionally, if any papers from named authors (David R. Liu or Ciaran M. Lee) are provided, write a separate summary for each (same length) as extra paragraphs.

Named author papers:
{author_text}

Output format:
Use the section names as headers (e.g., "Gene Therapy:", "Editing:", "Computation:", "Delivery:"). For each, provide the journal, DOI, summary, and lab name (last author) in the following format:

Gene Therapy: Journal, DOI, summary, lab name
Editing: Journal, DOI, summary, lab name
...

For named authors, use the same format:
David R Liu: Journal, DOI, summary, lab name
Ciaran M Lee: Journal, DOI, summary, lab name

Rules:
- Preserve ambiguous terms verbatim – do not guess or hallucinate.
- If you cannot determine meaning, err on the side of inclusion.
- Keep summaries concise and informative.

Return only the final digest text, no extra commentary.
"""
    payload = {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 2500,
    }
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    try:
        resp = requests.post(DEEPSEEK_URL, json=payload, headers=headers, timeout=90)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"Summarisation error: {e}")
        return "⚠️ DeepSeek summarisation failed."


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
# MAIN
# =============================================================================

def main():
    # Check if we should run
    ok, reason = should_run()
    now = datetime.now(timezone.utc)
    logger.info(f"Current UTC time: {now.strftime('%Y-%m-%d %H:%M:%S')}")

    if not ok:
        logger.info(f"Skipping run: {reason}")
        # Optional: Send a Telegram notification about the skip (only once per day)
        # Commented out to avoid spam — uncomment if you want notifications
        # if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        #     send_telegram(f"⏳ Daily digest skipped: {reason}")
        return

    logger.info("Starting daily digest...")
    start_date = now - timedelta(days=1)
    end_date = now

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

    author_papers_all = []
    for author_query in AUTHOR_TERMS:
        logger.info(f"Fetching papers for {author_query}...")
        pmids = fetch_all_pmids(author_query, start_date, end_date)
        logger.info(f"Found {len(pmids)} raw results.")
        if pmids:
            papers = fetch_metadata(pmids)
            logger.info(f"Kept {len(papers)} papers (with authors).")
            author_papers_all.extend(papers)

    # Selection
    selection_response = deepseek_select_titles(term_papers)
    if not selection_response:
        logger.error("Selection failed – using fallback.")
        selected_papers = {}
        for section_name, papers in term_papers.items():
            if papers:
                selected_papers[section_name] = papers[0]
    else:
        # Parse selection response
        selected_titles = {}
        for line in selection_response.splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if key in TERMS and value.lower() != "no_titles":
                selected_titles[key] = value

        # Match titles to papers
        selected_papers = {}
        for section_name, title in selected_titles.items():
            papers = term_papers.get(section_name, [])
            matched = None
            for p in papers:
                if p["title"].strip() == title.strip():
                    matched = p
                    break
            if not matched:
                # Case-insensitive fallback
                for p in papers:
                    if p["title"].strip().lower() == title.strip().lower():
                        matched = p
                        break
            if matched:
                selected_papers[section_name] = matched
            else:
                logger.warning(f"Could not match title '{title}' for {section_name}.")

        # If any section missing, fallback to first paper
        for section_name in TERMS:
            if section_name not in selected_papers and term_papers.get(section_name):
                selected_papers[section_name] = term_papers[section_name][0]

    # Summarise
    date_str = now.strftime("%B %d, %Y")
    digest = deepseek_summarize(selected_papers, author_papers_all, date_str)
    send_telegram(digest)
    logger.info("Digest sent successfully.")

    # Mark this run as successful (only after everything completes)
    mark_run(now)
    logger.info(f"Marked successful run at {now.isoformat()}")


if __name__ == "__main__":
    main()
