#!/usr/bin/env python3
import os
import sys
import re
import time
import json
import logging
import random
from datetime import datetime, timezone, timedelta
import zoneinfo

import requests
from Bio import Entrez, Medline

# ----------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ENTREZ_EMAIL = os.environ.get("ENTREZ_EMAIL")

# Search terms (PubMed only)
KEYWORDS = [
    '"gene therapy"',
    '"gene edit*"',
    '"gene deliver*"',
    '"computational biology"',
    '"DNA repair"',
    'epigenetic*',
    'immune response AND (vector OR edit* OR damage OR expression)',
    'manufacture',
    '(discover* OR engineer*) AND protein',
]

# Named authors – full names as specified
AUTHOR_TERMS = [
    '"David R Liu"[AU]',
    '"Ciaran M Lee"[AU]',
]

# DeepSeek peak pricing windows (UTC)
DEEPSEEK_PEAK_STARTS = [1, 6]   # 1:00–4:00 and 6:00–10:00
DEEPSEEK_PEAK_ENDS   = [4, 10]

# ----------------------------------------------------------------------
# LOGGING
# ----------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
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
# PUBMED HELPERS
# ----------------------------------------------------------------------
def fetch_pubmed_top(query):
    """Fetch the newest paper for a given query."""
    Entrez.email = ENTREZ_EMAIL
    try:
        handle = Entrez.esearch(db="pubmed", term=query, retmax=1, sort="pub date")
        record = Entrez.read(handle)
        handle.close()
        pmids = record["IdList"]
        if not pmids:
            return None
        handle = Entrez.efetch(db="pubmed", id=pmids[0], rettype="medline", retmode="text")
        records = list(Medline.parse(handle))
        handle.close()
        if not records:
            return None
        rec = records[0]
        doi = None
        for id_ in rec.get("AID", []):
            if id_.endswith(" [doi]"):
                doi = id_.replace(" [doi]", "")
                break
        if not doi:
            return None
        return {
            "doi": doi,
            "title": rec.get("TI", ""),
            "abstract": rec.get("AB", ""),
            "authors": rec.get("AU", []),
            "journal": rec.get("JT", ""),
            "date": rec.get("DP", ""),
            "pmid": rec.get("PMID", ""),
            "source": "PubMed",
            "query": query,
        }
    except Exception as e:
        logger.warning(f"Error fetching for {query}: {e}")
        return None

# ----------------------------------------------------------------------
# DEEPSEEK
# ----------------------------------------------------------------------
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

def deepseek_summarize(candidates, date_str):
    paper_text = ""
    for i, item in enumerate(candidates, 1):
        source = item.get("source", "PubMed")
        search_term = item.get("query", "")
        identifier = item.get("doi") or "N/A"
        journal = item.get("journal", "")
        lab = item.get("authors", [""])[-1] if item.get("authors") else "unknown"
        title = item.get("title", "")
        abstract = item.get("abstract", "")[:2000]
        paper_text += f"{i}. Source: {source} | Search: \"{search_term}\" | ID: {identifier} | Journal: {journal} | Lab: {lab} | Title: {title} | Abstract: {abstract}\n"

    prompt = f"""You are a scientific summariser. Your task is to produce a daily digest.

From the list of papers below, select the 5 with the most useful or significant information. For each, produce a summary of 3–5 sentences.
**Note:** Results from named author searches (Liu, Lee) must always be included – if they appear in the list, they take priority.

**Output format** (use these sections exactly):

🧬 **{date_str}**

**Papers (most significant):**
1. [Journal via "search_term" or "author name"] ID: ... – summary – Lab name.
2. ...

**Rules for handling uncertainty:**
- If you encounter ambiguous terms, acronyms, or concepts you are unsure about, include them verbatim – do not guess, ignore, or hallucinate. Mention naturally, no need for warnings.
- You may use context and your trained knowledge, but do not invent missing information.
- If you cannot determine meaning or importance of summary, err on the side of inclusion.

Candidates:
{paper_text}"""
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
        content = data["choices"][0]["message"]["content"]
        # Log the full response for debugging
        logger.info("DeepSeek response content (first 2000 chars): %s", content[:2000])
        logger.info("DeepSeek response length: %d characters", len(content))
        # Also save to file for full inspection
        with open("deepseek_response.txt", "w", encoding="utf-8") as f:
            f.write(content)
        logger.info("Full DeepSeek response saved to deepseek_response.txt")
        return content.strip().split("\n")
    except Exception as e:
        logger.error(f"DeepSeek API error: {e}")
        if hasattr(e, 'response') and e.response:
            logger.error(f"Response status: {e.response.status_code}")
            logger.error(f"Response body: {e.response.text[:500]}")
        return ["⚠️ DeepSeek API error – no summary generated."]

# ----------------------------------------------------------------------
# TELEGRAM
# ----------------------------------------------------------------------
def send_telegram(message):
    # Handle empty or whitespace-only messages
    if not message or not message.strip():
        message = "⚠️ No digest content was generated."
        logger.warning("Message was empty – using fallback.")
    # Use a safe chunk size
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
            if hasattr(e, 'response') and e.response:
                logger.error(f"Response status: {e.response.status_code}")
                logger.error(f"Response body: {e.response.text[:500]}")
            raise

# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------
def main():
    # Skip if DeepSeek peak pricing
    if is_deepseek_peak():
        logger.info("Skipping run – DeepSeek peak pricing window.")
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            send_telegram("⏳ Daily digest skipped – DeepSeek peak pricing hours.")
        return

    logger.info("Starting daily digest...")

    all_queries = KEYWORDS + AUTHOR_TERMS
    candidates = []
    seen_dois = set()

    for query in all_queries:
        logger.info(f"Fetching newest paper for: {query}")
        paper = fetch_pubmed_top(query)
        if not paper:
            logger.info(f"No result for {query}")
            continue
        doi = paper.get("doi")
        if doi in seen_dois:
            logger.info(f"Duplicate DOI {doi} for {query}, skipping")
            continue
        seen_dois.add(doi)
        candidates.append(paper)

    logger.info(f"Collected {len(candidates)} unique candidates.")

    if not candidates:
        send_telegram("No new relevant papers found.")
        return

    date_str = datetime.now(timezone.utc).strftime("%B %d, %Y")
    summary_lines = deepseek_summarize(candidates, date_str)
    final_msg = "\n".join(summary_lines)

    # Extra safety: if final_msg is empty after join, use fallback
    if not final_msg or not final_msg.strip():
        final_msg = "⚠️ DeepSeek returned an empty response."
        logger.warning("Final message was empty – using fallback.")

    logger.info(f"Final message length: {len(final_msg)} chars")
    send_telegram(final_msg)

    logger.info("Digest sent successfully.")

if __name__ == "__main__":
    main()
