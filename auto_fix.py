#!/usr/bin/env python3
import os
import re
import sys
import subprocess
import requests
from github import Github, GithubException
from datetime import datetime

DEEPSEEK_API_KEY = os.environ["DEEPSEEK_API_KEY"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPO_NAME = os.environ["GITHUB_REPOSITORY"]
ERROR_LOG = os.environ.get("ERROR_LOG", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def send_telegram(msg):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg})

def extract_error_context(error_log):
    """Return (file_path, line_num, func_name, error_summary)"""
    tb_pattern = r'File "(.+?)", line (\d+), in (.+?)\n'
    matches = re.findall(tb_pattern, error_log)
    if not matches:
        return None, None, None, error_log[:500]
    # Find the first .py file that is not the auto-fix script itself
    for file_path, line_num, func_name in matches:
        if file_path.endswith(".py") and "auto_fix" not in file_path:
            return file_path, int(line_num), func_name, error_log[:500]
    # If no Python error, look for any file (could be YAML)
    for file_path, line_num, func_name in matches:
        return file_path, int(line_num), func_name, error_log[:500]
    return None, None, None, error_log[:500]

def extract_block(file_path, line_num, func_name=None, context_lines=10):
    """Extract a block: entire function if Python, else surrounding lines."""
    if not os.path.exists(file_path):
        return None
    with open(file_path, "r") as f:
        lines = f.readlines()
    if file_path.endswith(".py") and func_name:
        # Extract the whole function definition
        start = None
        indent = None
        for i, line in enumerate(lines):
            if re.match(rf"def {func_name}\(", line):
                start = i
                indent = len(line) - len(line.lstrip())
                break
        if start is None:
            return None
        end = len(lines)
        for i in range(start+1, len(lines)):
            if re.match(r"^\s*def ", lines[i]) and (len(lines[i]) - len(lines[i].lstrip())) <= indent:
                end = i
                break
        return "".join(lines[start:end])
    else:
        # For YAML or others: return context around the error line
        start = max(0, line_num - context_lines - 1)
        end = min(len(lines), line_num + context_lines)
        return "".join(lines[start:end])

def call_deepseek(error_summary, block, file_path):
    prompt = f"""
Fix the error in the following code block. The error occurred in file: {file_path}

Error:
{error_summary}

Code block:
{block}

Return only the corrected code block, with no extra text or explanations.
Keep the code as close as possible to the original intended function.
If the API format needs adjustment, refer to known documentation.
"""
    resp = requests.post(
        "https://api.deepseek.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
        json={
            "model": "deepseek-v4-flash",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 800,  # enough for a function
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]

def patch_file(file_path, old_block, new_block):
    """Replace old_block with new_block in file."""
    with open(file_path, "r") as f:
        content = f.read()
    # Use simple string replace (the block is unique)
    new_content = content.replace(old_block, new_block)
    if new_content == content:
        raise ValueError("No replacement made – block not found.")
    with open(file_path, "w") as f:
        f.write(new_content)
    return new_content

def test_fix():
    try:
        subprocess.run(["python", "-c", "import daily_digest"], check=True, capture_output=True, timeout=30)
        return True
    except subprocess.CalledProcessError:
        return False

def main():
    try:
        # Parse error
        file_path, line_num, func_name, error_summary = extract_error_context(ERROR_LOG)
        if not file_path:
            send_telegram("❌ Auto-fix: Could not locate error file.")
            sys.exit(1)

        # Extract block
        block = extract_block(file_path, line_num, func_name)
        if not block:
            send_telegram(f"❌ Auto-fix: Could not extract block from {file_path}.")
            sys.exit(1)

        # Get fix from DeepSeek
        fixed_block = call_deepseek(error_summary, block, file_path)
        if not fixed_block.strip():
            send_telegram("❌ Auto-fix: DeepSeek returned empty.")
            sys.exit(1)

        # Apply patch
        patch_file(file_path, block, fixed_block)

        # Test
        if test_fix():
            # Commit and push
            repo = Github(GITHUB_TOKEN).get_repo(REPO_NAME)
            commit_msg = f"Auto-fix: {file_path} – {error_summary[:100]}"
            try:
                contents = repo.get_contents(file_path, ref="main")
                repo.update_file(file_path, commit_msg, open(file_path).read(), contents.sha, branch="main")
            except GithubException:
                repo.create_file(file_path, commit_msg, open(file_path).read(), branch="main")
            # Telegram with diff (show changed lines)
            send_telegram(
                f"✅ Auto-fix applied to {file_path}\n"
                f"Error: {error_summary[:200]}\n"
                f"Commit: {commit_msg}\n"
                f"Replaced block:\n```\n{block[:300]}...\n```\nwith:\n```\n{fixed_block[:300]}...\n```"
            )
        else:
            send_telegram(f"❌ Auto-fix failed test after patching {file_path}. Manual check needed.")
            sys.exit(1)

    except Exception as e:
        send_telegram(f"❌ Auto-fix crashed: {str(e)}")
        raise

if __name__ == "__main__":
    main()
