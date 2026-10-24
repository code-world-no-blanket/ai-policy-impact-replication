"""Write the next batch of matched comparison repos to collect.
Kept as a record: it reads working files (logs/eval/...) from the original run.

usage: python matched_batch.py <match log> <done list> <out.csv>
Reads "[i/n] policy (cluster; stand-in DATE)" / "  -> match (...)" lines from the
matching log, skips matches already listed in <done list> or screened out, and
writes the rest as a candidates CSV. Screening drops AI-tool projects (by name,
description and topics) and automated feeds. Prints the number written; appends
every match it handled, collected or screened, to <done list>, and screening
reasons to <done list>.screened.
"""

import csv
import json
import re
import sys
import urllib.request
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from ai_policy_impact.activity import is_bot_login
from ai_policy_impact.github import github_token

log, done_path, out = (Path(a) for a in sys.argv[1:4])
done = set(done_path.read_text().split()) if done_path.exists() else set()
HEADERS = {"Authorization": "Bearer " + github_token(), "Accept": "application/vnd.github+json"}
AI_WORDS = re.compile(
    r"\b(ai|llm|llms|gpt|chatgpt|openai|anthropic|claude|copilot|codex|agent|agents|"
    r"agentic|genai|generative|language model|machine learning|deep learning|"
    r"rag|prompt|chatbot|mcp|diffusion|transformers?)\b",
    re.IGNORECASE,
)
ACTIONS = {"actions", "actions-user", "github-actions"}


def get(path):
    request = urllib.request.Request("https://api.github.com" + path, headers=HEADERS)
    return json.load(urllib.request.urlopen(request, timeout=60))


matches, policy, stand_in = [], None, None
for line in log.read_text().splitlines():
    head = re.match(r"\[\d+/\d+\] (\S+) \((\w+); stand-in (\d{4}-\d{2}-\d{2})\)", line)
    if head:
        policy, stand_in = head.group(1), head.group(3)
        continue
    hit = re.match(r"\s+-> (\S+) \(", line)
    if hit and policy:
        matches.append((hit.group(1), policy, stand_in))

# Policy repos matched to an already-collected comparison repo need no new match.
existing_path = Path("logs/eval/matched-existing.csv")
existing = ({r["matched_policy_repo"] for r in csv.DictReader(open(existing_path))}
            if existing_path.exists() else set())

rows, handled, screened = [], [], []
for repo, partner, day in matches:
    if repo in done:
        continue
    if partner in existing:
        screened.append(f"{repo}\t{partner}\tpartner has an existing match")
        handled.append(repo)
        continue
    try:
        meta = get(f"/repos/{repo}")
    except Exception as error:  # deleted or renamed since matching
        screened.append(f"{repo}\tmetadata: {error}")
        handled.append(repo)
        continue
    # Name and description only: topic tags flag many projects that merely
    # mention AI features.
    text = " ".join([repo.replace("/", " ").replace("-", " "), meta.get("description") or ""])
    found = AI_WORDS.search(text)
    reason = f"AI-tool project ({found.group(0)!r})" if found else None
    if not reason:
        since = (date.fromisoformat(day) - timedelta(weeks=52)).isoformat()
        commits = get(f"/repos/{repo}/commits?per_page=100&since={since}T00:00:00Z")
        if commits:
            logins = [((c.get("author") or {}).get("login") or "").lower() for c in commits]
            bots = sum(is_bot_login(l) or l in ACTIONS for l in logins) / len(commits)
            stems = Counter(" ".join(re.sub(r"[^A-Za-z ]+", " ", c["commit"]["message"]
                                            .split("\n")[0]).split()[:2]).lower()
                            for c in commits)
            top = stems.most_common(1)[0][1] / len(commits)
            # Large projects have many merge and dependency-bot commits, so a
            # repeated stem only signals a feed when commit volume is also high.
            volume = 0
            if top > 0.5:
                request = urllib.request.Request(
                    f"https://api.github.com/repos/{repo}/commits?per_page=1&since={since}T00:00:00Z",
                    headers=HEADERS)
                link = urllib.request.urlopen(request, timeout=60).headers.get("Link") or ""
                last = re.search(r'[?&]page=(\d+)>; rel="last"', link)
                volume = int(last.group(1)) if last else len(commits)
            if bots > 0.8 or (top > 0.5 and volume > 20000):
                reason = f"automated? bots {bots:.0%}, top stem {top:.0%}, {volume} commits"
    handled.append(repo)
    if reason:
        screened.append(f"{repo}\t{partner}\t{reason}")
        continue
    rows.append({
        "repository": repo, "priority": "evaluation", "policy_paths": "",
        "group": "comparison", "stand_in_date": day, "matched_policy_repo": partner,
    })

with open(out, "w", newline="") as stream:
    writer = csv.DictWriter(stream, fieldnames=["repository", "priority", "policy_paths",
                                                "group", "stand_in_date",
                                                "matched_policy_repo"])
    writer.writeheader()
    writer.writerows(rows)
with open(done_path, "a") as stream:
    stream.writelines(r + "\n" for r in handled)
if screened:
    with open(str(done_path) + ".screened", "a") as stream:
        stream.writelines(s + "\n" for s in screened)
print(len(rows))
