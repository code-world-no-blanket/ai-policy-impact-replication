"""Re-match the policy repos left without a matched comparison repo.
Kept as a record: it reads working files (logs/eval/...) from the original run.

usage: python rematch.py <selection run> <out.csv>
For the ten policy repos whose best match was screened out (an AI-tool project or
an automated feed), take the next candidate from the matcher's checked list
(closest first) that passes the same screening and is not used anywhere else;
the stand-in date stays the one the matcher drew. Databuddy, deleted from GitHub
after its data was collected, was skipped by the matcher: it gets a stand-in date
drawn from its date cluster's policy dates (seed below) and a fresh search, with
its language, stars, owner type and past-year PRs taken from the candidate list.
"""

import csv
import glob
import json
import random
import re
import sys
import tempfile
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ai_policy_impact import comparison as cmp
from ai_policy_impact.activity import is_bot_login
from ai_policy_impact.github import GitHubClient, github_token

run, out = Path(sys.argv[1]), Path(sys.argv[2])
SEED = 20261024
TOKEN = github_token()
HEADERS = {"Authorization": "Bearer " + TOKEN, "Accept": "application/vnd.github+json"}
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


def screen(repo, day):
    """None if usable, else the reason (same rules as matched_batch.py)."""
    try:
        meta = get(f"/repos/{repo}")
    except Exception as error:
        return f"metadata: {error}"
    text = " ".join([repo.replace("/", " ").replace("-", " "), meta.get("description") or ""])
    found = AI_WORDS.search(text)
    if found:
        return f"AI-tool project ({found.group(0)!r})"
    since = (date.fromisoformat(day) - timedelta(weeks=52)).isoformat()
    commits = get(f"/repos/{repo}/commits?per_page=100&since={since}T00:00:00Z")
    if not commits:
        return None
    logins = [((c.get("author") or {}).get("login") or "").lower() for c in commits]
    bots = sum(is_bot_login(l) or l in ACTIONS for l in logins) / len(commits)
    stems = Counter(" ".join(re.sub(r"[^A-Za-z ]+", " ", c["commit"]["message"]
                                    .split("\n")[0]).split()[:2]).lower() for c in commits)
    top = stems.most_common(1)[0][1] / len(commits)
    volume = 0
    if top > 0.5:
        request = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/commits?per_page=1&since={since}T00:00:00Z",
            headers=HEADERS)
        link = urllib.request.urlopen(request, timeout=60).headers.get("Link") or ""
        last = re.search(r'[?&]page=(\d+)>; rel="last"', link)
        volume = int(last.group(1)) if last else len(commits)
    if bots > 0.8 or (top > 0.5 and volume > 20000):
        return f"automated? bots {bots:.0%}, top stem {top:.0%}, {volume} commits"
    return None


# Every repository already in the study, as policy or comparison.
used = set()
for path in (["data/repositories/candidate_policy_repos.csv", "data/repositories/candidate_comparison_repos.csv",
              "data/repositories/random_comparison_repos.csv", "logs/eval/matched-existing.csv"]
             + glob.glob("logs/eval/matched-batch-*.csv")):
    used |= {r["repository"].lower() for r in csv.DictReader(open(path))}
screened_out = {line.split("\t")[0].lower()
                for line in open("logs/eval/matched-done.txt.screened")}
used |= screened_out

matched = set()
for path in ["logs/eval/matched-existing.csv"] + glob.glob("logs/eval/matched-batch-*.csv"):
    matched |= {r["matched_policy_repo"] for r in csv.DictReader(open(path))}
policies = {r["repository"]: r for r in csv.DictReader(open("data/repositories/sampled_policy_repos.csv"))
            if r["priority"] == "evaluation"}
records = {}
for path in glob.glob(f"{run}/repositories/*.json"):
    record = json.load(open(path))
    records[record["requested_repository"]] = record

rows = []


def take(policy, candidate, stand_in, quality):
    used.add(candidate.lower())
    rows.append({"repository": candidate, "priority": "evaluation", "policy_paths": "",
                 "group": "comparison", "stand_in_date": stand_in,
                 "matched_policy_repo": policy, "match_quality": quality})
    print(f"  {policy:32} <- {candidate} (stand-in {stand_in}, {quality})")


todo = sorted(set(policies) - matched)
print("unmatched:", todo)
for policy in todo:
    record = records.get(policy)
    if record is None:
        continue
    stand_in = record["stand_in_date"]
    for c in record["checked"]:
        if c["repository"].lower() in used:
            continue
        reason = screen(c["repository"], stand_in)
        if reason:
            print(f"    x {c['repository']}: {reason}")
            used.add(c["repository"].lower())
            continue
        take(policy, c["repository"], stand_in, cmp.quality(c, record["policy"]))
        break
    else:
        print(f"  {policy}: no usable candidate in the checked list")

# Databuddy: deleted, so build its matching inputs from the candidate list.
name = "databuddy-analytics/Databuddy"
if name in todo:
    meta = next(r for r in csv.DictReader(open("data/repositories/candidate_policy_repos.csv"))
                if r["repository"] == name)
    policy_dates = {r: date.fromisoformat(p["reported_policy_date"]) for r, p in policies.items()}
    cluster = cmp.cluster_of(policy_dates[name], cmp.DEFAULT_BOUNDS)
    donors = sorted(r for r, d in policy_dates.items()
                    if r != name and cmp.cluster_of(d, cmp.DEFAULT_BOUNDS) == cluster)
    rng = random.Random(SEED)
    base = policy_dates[rng.choice(donors)]
    stand_in = base + timedelta(days=rng.randint(-cmp.JITTER_DAYS, cmp.JITTER_DAYS))
    policy = {"language": meta["language"], "owner_type": meta["owner_type"],
              "stars": int(meta["stars"]), "prs": int(meta["prs_past_year"])}
    client = GitHubClient(Path(tempfile.mkdtemp()), TOKEN)
    search = cmp.Search(client)
    log = []
    checked = cmp.match_one(client, search, policy, stand_in, set(used),
                            datetime.now(timezone.utc).date(), log)
    print("\n".join(log))
    for c in checked:
        if c["repository"].lower() in used:
            continue
        reason = screen(c["repository"], stand_in.isoformat())
        if reason:
            print(f"    x {c['repository']}: {reason}")
            continue
        take(name, c["repository"], stand_in.isoformat(), cmp.quality(c, policy))
        break

with open(out, "w", newline="") as stream:
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
print(len(rows), "new matches ->", out)
