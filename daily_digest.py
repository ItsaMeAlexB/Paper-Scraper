#!/usr/bin/env python3
import os
import sys
import re
import time
import json
import logging
import random
from datetime import datetime, timezone, timedelta

import requests
from Bio import Entrez, Medline

# ----------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ENTREZ_EMAIL = os.environ.get("ENTREZ_EMAIL")

# Four search terms (sections)
TERMS = {
    "Gene Therapy": '"gene therapy" OR "genome therapy" OR "genetic therapy"',
    "Editing": '"gene edit*" OR "genome edit*" OR "gene engineer*" OR "genome engineer*" OR "genetic edit*" OR "genetic engineer*"',
    "Computation": '"computational biology"',
    "Delivery": 'delivery OR vector',
}

# Named authors – full names as specified
AUTHOR_TERMS = [
    '"David R Liu"[AU]',
    '"Ciaran M Lee"[AU]',
]

# DeepSeek peak pricing windows (UTC)
DEEPSEEK_PEAK_STARTS = [1, 6]
DEEPSEEK_PEAK_ENDS   = [4, 10]

# ----------------------------------------------------------------------
# LOGGING
# ----------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# DEEPSEEK PEAK CHECK
# ----------------------------------------------------------------------
def is_deepseek_peak():
    now_utc = datetime.now(timezone.utc)
    hour = now_utc.hour
    for start, end in zip(DEEPSEEK_PEAK_STARTS, DEEPSEEK_PEAK_ENDS):
        if start <= hour < end:
            return True
    return False

# ----------------------------------------------------------------------
# PUBMED HELPERS – fetch ALL results for a query in a date range
# ----------------------------------------------------------------------
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
        doi = None
        for id_ in rec.get("AID", []):
            if id_.endswith(" [doi]"):
                doi = id_.replace(" [doi]", "")
                break
        papers.append({
            "doi": doi,
            "title": rec.get("TI", ""),
            "abstract": rec.get("AB", ""),
            "authors": rec.get("AU", []),
            "journal": rec.get("JT", ""),
            "date": rec.get("DP", ""),
            "pmid": rec.get("PMID", ""),
        })
    return papers

# ----------------------------------------------------------------------
# DEEPSEEK HELPERS
# ----------------------------------------------------------------------
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

def deepseek_select_titles(term_papers):
    """Single call to select one title per section, with logging and fallback."""
    # Build the prompt
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

    # Log prompt length
    prompt_len = len(prompt)
    logger.info(f"Selection prompt length: {prompt_len} characters (approx {prompt_len//4} tokens)")

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
        data = resp.json()
        response_text = data["choices"][0]["message"]["content"].strip()
        logger.info(f"Selection response (first 500 chars): {response_text[:500]}")
        return response_text
    except Exception as e:
        logger.error(f"DeepSeek selection error: {e}")
        if hasattr(e, 'response') and e.response:
            logger.error(f"Response status: {e.response.status_code}")
            logger.error(f"Response body: {e.response.text[:1000]}")
        # Fallback: use first title per section
        logger.warning("Using fallback – first title per section.")
        selected = {}
        for section_name, papers in term_papers.items():
            if papers:
                selected[section_name] = papers[0]['title']
            else:
                selected[section_name] = "NO_TITLES"
        response = "\n".join([f"{k}: {v}" for k, v in selected.items()])
        logger.info(f"Fallback selection:\n{response}")
        return response

def deepseek_summarize(selected_papers, author_papers, date_str):
    section_text = ""
    for section_name, paper in selected_papers.items():
        if paper:
            section_text += f"[{section_name}]\n"
            section_text += f"Title: {paper['title']}\n"
            section_text += f"Journal: {paper['journal']}\n"
            section_text += f"Authors: {', '.join(paper['authors'][:5])}\n"
            section_text += f"Abstract: {paper['abstract'][:1500]}\n\n"

    author_text = ""
    if author_papers:
        author_text = "[Named Authors]\n"
        for paper in author_papers:
            author_text += f"Title: {paper['title']}\n"
            author_text += f"Journal: {paper['journal']}\n"
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
        logger.error(f"DeepSeek summarization error: {e}")
        return "⚠️ DeepSeek summarization failed."

# ----------------------------------------------------------------------
# TELEGRAM
# ----------------------------------------------------------------------
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

# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------
def main():
    if is_deepseek_peak():
        logger.info("Skipping run – DeepSeek peak pricing window.")
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            send_telegram("⏳ Daily digest skipped – DeepSeek peak pricing hours.")
        return

    logger.info("Starting daily digest...")

    now = datetime.now(timezone.utc)
    start_date = now - timedelta(days=1)
    end_date = now

    # 1. Fetch all papers for each term
    term_papers = {}
    for section_name, query in TERMS.items():
        logger.info(f"Fetching all papers for {section_name}...")
        pmids = fetch_all_pmids(query, start_date, end_date)
        logger.info(f"Found {len(pmids)} papers for {section_name}.")
        if pmids:
            papers = fetch_metadata(pmids)
            term_papers[section_name] = papers
        else:
            term_papers[section_name] = []

    # 2. Fetch all papers for named authors
    author_papers_all = []
    for author_query in AUTHOR_TERMS:
        logger.info(f"Fetching all papers for {author_query}...")
        pmids = fetch_all_pmids(author_query, start_date, end_date)
        logger.info(f"Found {len(pmids)} papers for {author_query}.")
        if pmids:
            papers = fetch_metadata(pmids)
            author_papers_all.extend(papers)

    # 3. DeepSeek selection (one call for all sections)
    selection_response = deepseek_select_titles(term_papers)
    if not selection_response:
        logger.error("Selection response is empty or None.")
        send_telegram("⚠️ DeepSeek selection failed – no digest generated.")
        return

    logger.info(f"Raw selection response:\n{selection_response}")

    # Parse the response to extract selected titles per section
    selected_titles = {}
    for line in selection_response.splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if key in TERMS and value.lower() != "no_titles":
                selected_titles[key] = value

    # Match titles to actual paper dicts
    selected_papers = {}
    for section_name, title in selected_titles.items():
        papers = term_papers.get(section_name, [])
        matched = None
        for p in papers:
            if p["title"].strip() == title.strip():
                matched = p
                break
        if not matched:
            for p in papers:
                if p["title"].strip().lower() == title.strip().lower():
                    matched = p
                    break
        if matched:
            selected_papers[section_name] = matched
        else:
            logger.warning(f"Could not match title '{title}' for {section_name}.")

    # If some sections missing, we can still continue with those we have
    if not selected_papers:
        logger.warning("No papers were selected – using first paper per section as fallback.")
        for section_name, papers in term_papers.items():
            if papers:
                selected_papers[section_name] = papers[0]

    # 4. Generate summaries via DeepSeek
    date_str = now.strftime("%B %d, %Y")
    digest = deepseek_summarize(selected_papers, author_papers_all, date_str)

    # 5. Send to Telegram
    send_telegram(digest)
    logger.info("Digest sent successfully.")

if __name__ == "__main__":
    main()
