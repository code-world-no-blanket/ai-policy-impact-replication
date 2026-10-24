"""Assign already-collected comparison repos as matches.
Kept as a record: it reads working files (logs/eval/...) from the original run.

usage: python existing_matches.py <match log> <done list> <out.csv>
For each evaluation policy repository not yet given a new match (one already
handled in <done list>), pick the closest original comparison repository that
is a "good" match: same language, stars within 3x, PRs in the past year within
2x (the matching tool's rule). Assignment is one to one, closest pairs first,
by the tool's distance (PR log ratio + 0.5 x star log ratio + 0.25 if owner
types differ). AI-tool projects are skipped. Originals keep their stand-in date,
since their data was collected around it. Prints the pairs.
"""

import csv
import json
import math
import re
import sys
import urllib.request
from pathlib import Path

from ai_policy_impact.github import github_token

log, done_path, out = (Path(a) for a in sys.argv[1:4])
AI_WORDS = re.compile(
    r"\b(ai|llm|llms|gpt|chatgpt|openai|anthropic|claude|copilot|codex|agent|agents|"
    r"agentic|genai|generative|language model|machine learning|deep learning|"
    r"rag|prompt|chatbot|mcp|diffusion|transformers?|voice assistant)\b",
    re.IGNORECASE,
)
HEADERS = {"Authorization": "Bearer " + github_token(), "Accept": "application/vnd.github+json"}

# Policy repos whose new match is already collected or queued.
done = set(done_path.read_text().split()) if done_path.exists() else set()
covered, partner = set(), None
for line in log.read_text().splitlines():
    head = re.match(r"\[\d+/\d+\] (\S+) \(", line)
    if head:
        partner = head.group(1)
        continue
    hit = re.match(r"\s+-> (\S+) \(", line)
    if hit and hit.group(1) in done:
        covered.add(partner.lower())

cand = {}
for name in ("candidate_policy_repos.csv", "candidate_comparison_repos.csv"):
    for row in csv.DictReader(open(f"data/repositories/{name}")):
        cand[row["repository"].lower()] = row
originals = [r for r in csv.DictReader(open("data/repositories/random_comparison_repos.csv"))
             if r["priority"] == "evaluation"]
policies = [r for r in csv.DictReader(open("data/repositories/sampled_policy_repos.csv"))
            if r["priority"] == "evaluation" and r["repository"].lower() not in covered]


def number(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def ai_tool(repo):
    try:
        request = urllib.request.Request(f"https://api.github.com/repos/{repo}", headers=HEADERS)
        meta = json.load(urllib.request.urlopen(request, timeout=60))
    except Exception:
        return True  # cannot check; do not use
    text = " ".join([repo.replace("/", " ").replace("-", " "), meta.get("description") or "",
                     " ".join(meta.get("topics") or [])])
    return bool(AI_WORDS.search(text))


pairs = []
for policy in policies:
    p = cand.get(policy["repository"].lower())
    if not p:
        continue
    p_stars, p_prs = number(p["stars"]), number(p["prs_past_year"])
    for comp in originals:
        c_stars, c_prs = number(comp["stars"]), number(comp["prs_past_year"])
        if None in (p_stars, p_prs, c_stars, c_prs) or comp["language"] != p["language"]:
            continue
        star_ratio = max(c_stars, 1) / max(p_stars, 1)
        pr_ratio = (c_prs + 1) / (p_prs + 1)
        if not (1 / 3 <= star_ratio <= 3 and 0.5 <= pr_ratio <= 2):
            continue
        distance = (abs(math.log(pr_ratio)) + 0.5 * abs(math.log(star_ratio))
                    + (0.25 if comp["owner_type"] != p["owner_type"] else 0.0))
        pairs.append((distance, policy["repository"], comp))

screened, chosen, used_policy, used_comp = {}, [], set(), set()
for distance, policy, comp in sorted(pairs, key=lambda x: x[0]):
    name = comp["repository"]
    if policy in used_policy or name in used_comp:
        continue
    if name not in screened:
        screened[name] = ai_tool(name)
    if screened[name]:
        continue
    used_policy.add(policy)
    used_comp.add(name)
    chosen.append({"repository": name, "priority": "evaluation", "policy_paths": "",
                   "group": "comparison", "stand_in_date": comp["stand_in_date"],
                   "matched_policy_repo": policy, "distance": round(distance, 3)})

with open(out, "w", newline="") as stream:
    writer = csv.DictWriter(stream, fieldnames=list(chosen[0]))
    writer.writeheader()
    writer.writerows(chosen)
print(f"{len(chosen)} existing matches (policy repos already covered by new matches: {len(covered)})")
for row in chosen:
    print(f"  {row['matched_policy_repo']:40} <- {row['repository']} ({row['distance']})")
print("AI-tool originals skipped:", sorted(n for n, flagged in screened.items() if flagged))
