Daily Digest – PubMed Only

This script fetches the newest papers from PubMed for four defined topics, uses DeepSeek to select the most significant one per topic, and sends a daily digest via Telegram.

Setup

1. Clone the repository and install dependencies:
   pip install -r requirements.txt

2. Set environment variables (or add to GitHub Secrets):
   ENTREZ_EMAIL – your email (required by NCBI)
   DEEPSEEK_API_KEY – your DeepSeek API key
   TELEGRAM_BOT_TOKEN – Telegram bot token from @BotFather
   TELEGRAM_CHAT_ID – your Telegram chat ID (get via getUpdates)

3. Run manually:
   python daily_digest.py

GitHub Actions Workflow

The script is set to run automatically every day at 04:15 UTC (off-peak for DeepSeek). It can also be triggered manually from the Actions tab.

Search Terms

The script searches PubMed for four topics (all papers from the last 24 hours):

- Gene Therapy: "gene therapy"
- Editing: "gene edit*"
- Computation: "computational biology"
- Delivery: "vector"

Named authors (always included if they published that day):
- "David R Liu"[AU]
- "Ciaran M Lee"[AU]

How It Works

1. Fetch: All papers (with authors) from the last 24 hours are fetched for each topic.
2. Filter: Papers without authors (e.g., news highlights, editorials) are automatically skipped.
3. Select: DeepSeek picks the 1 most useful, significant, and relevant paper per topic based on titles.
4. Summarise: For each selected paper, DeepSeek generates a 4–6 sentence summary including journal, DOI, key findings, and lab name.
5. Named authors: If David R. Liu or Ciaran M. Lee published that day, their papers are added as extra paragraphs.
6. Send: The digest is sent via Telegram (plain text, chunked if >4000 characters).

Output Format

Gene Therapy: Journal, DOI, summary, lab name
Editing: Journal, DOI, summary, lab name
Computation: Journal, DOI, summary, lab name
Delivery: Journal, DOI, summary, lab name

(Optional) David R Liu: Journal, DOI, summary, lab name
(Optional) Ciaran M Lee: Journal, DOI, summary, lab name

Peak Pricing Protection

The script checks the current UTC time against DeepSeek's peak windows (1:00–4:00 and 6:00–10:00 UTC). If run during a peak window, it exits early and sends a Telegram notification. The scheduled run at 04:15 UTC is always off-peak.

Files

- daily_digest.py – main script
- requirements.txt – dependencies (biopython, requests)
- .github/workflows/daily.yml – GitHub Actions workflow with pip caching and scheduled trigger

License

MIT
