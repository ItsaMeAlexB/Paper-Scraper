This script fetches the newest paper for each predefined search term and author,
then uses DeepSeek to select the 5 most significant ones and sends them via Telegram.

Setup
-----
1. Install dependencies:
   pip install -r requirements.txt

2. Set environment variables (or add to .env):
   ENTREZ_EMAIL        – your email (required by NCBI)
   DEEPSEEK_API_KEY    – your DeepSeek API key
   TELEGRAM_BOT_TOKEN  – Telegram bot token from @BotFather
   TELEGRAM_CHAT_ID    – your Telegram chat ID (get via getUpdates)

3. Run:
   python daily_digest.py

Search Terms
------------
The script uses the following PubMed queries (all are exact as given):
  "gene therapy"
  "gene edit*"
  "gene deliver*"
  "computational biology"
  "DNA repair"
  epigenetic*
  immune response AND (vector OR edit* OR damage OR expression)
  manufacture
  (discover* OR engineer*) AND protein

Named Authors (always included if present):
  "David R. Liu"[AU]
  "Ciaran M. Lee"[AU]

Logic
-----
For each query, the script fetches the single newest paper (no date filter).
Deduplication is done by DOI across all queries.
The resulting unique candidates are sent to DeepSeek.
DeepSeek selects the 5 most useful/significant papers.
The digest is sent as a plain text message via Telegram (split into chunks if >4096 chars).

Output Format
-------------
🧬 Date
Papers (most significant):
1. [Journal via search_term] ID: doi – summary – Lab name.
...

Uncertainty Handling
--------------------
Ambiguous terms are preserved verbatim; no guessing or hallucination.
If a paper's importance is borderline, it is included.

No caching – each run fetches fresh results.
