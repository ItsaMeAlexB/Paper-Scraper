#!/usr/bin/env python3
import os
import sys
import re
import json
import subprocess
import requests
from github import Github, GithubException
from datetime import datetime

DEEPSEEK_API_KEY = os.environ["DEEPSEEK_API_KEY"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPO_NAME = os.environ["GITHUB_REPOSITORY"]
ERROR_LOG = os.environ.get("ERROR_LOG", "No error log captured.")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Files to potentially modify
FILES = {
    "daily_digest.py": "daily_digest.py",
    ".github/workflows/auto-fix.yml": ".github/workflows/auto-fix.yml",
    ".github/workflows/daily.yml": ".github/workflows/daily.yml",
    "requirements.txt": "requirements.txt",
    "README.md": "README.md",
}

def send_telegram(msg):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg})

def call_deepseek(error_log, files_content):
    prompt = f"""
You are an expert Python/DevOps engineer. Fix the error in the provided files.
The error occurred while running the digest script.

Error log:
{error_log[:8000]}

Current file contents:
"""
    for path, content in files_content.items():
        prompt += f"\n### FILE: {path}\n{content}\n"

    prompt += """
Return the corrected files. For each file, output:
### FILE: <path>
<entire corrected content>

Do not include any other text. Keep the file paths exactly as given.
Stay as close as possible to the intended function.
"""
    resp = requests.post(
        "https://api.deepseek.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
        json={
            "model": "deepseek-v4-flash",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": 4000,
        },
        timeout=90,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]

def parse_deepseek_response(response_text):
    pattern = r"### FILE: (.+?)\n(.*?)(?=\n### FILE:|$)"
    matches = re.findall(pattern, response_text, re.DOTALL)
    return {path.strip(): content.strip() for path, content in matches}

def test_fix():
    try:
        subprocess.run(["python", "-c", "import daily_digest"], check=True, capture_output=True, timeout=30)
        return True
    except subprocess.CalledProcessError:
        return False

def main():
    try:
        # Read current files
        files_content = {}
        for path, rel_path in FILES.items():
            try:
                with open(rel_path, "r") as f:
                    files_content[path] = f.read()
            except FileNotFoundError:
                files_content[path] = "# File not found"

        # Call DeepSeek
        fixed_response = call_deepseek(ERROR_LOG, files_content)
        new_files = parse_deepseek_response(fixed_response)
        if not new_files:
            send_telegram("❌ Auto-fix: DeepSeek returned no files.")
            sys.exit(1)

        # Write new files
        for path, content in new_files.items():
            if path in FILES:
                actual_path = FILES[path]
                with open(actual_path, "w") as f:
                    f.write(content)
                print(f"Updated {actual_path}")

        # Test
        if test_fix():
            # Commit and push
            repo = Github(GITHUB_TOKEN).get_repo(REPO_NAME)
            branch = repo.get_branch("main")
            commit_msg = f"Auto-fix: attempt to resolve error at {datetime.utcnow().isoformat()}"
            for path, content in new_files.items():
                if path in FILES:
                    actual_path = FILES[path]
                    try:
                        contents = repo.get_contents(actual_path, ref="main")
                        repo.update_file(actual_path, commit_msg, content, contents.sha, branch="main")
                    except GithubException:
                        repo.create_file(actual_path, commit_msg, content, branch="main")
            send_telegram(f"✅ Auto-fix applied. Changes pushed to main.\nCommit: {commit_msg}")
        else:
            send_telegram("❌ Auto-fix attempted but test failed. Manual intervention required.")
            sys.exit(1)

    except Exception as e:
        send_telegram(f"❌ Auto-fix crashed: {str(e)}")
        raise

if __name__ == "__main__":
    main()
