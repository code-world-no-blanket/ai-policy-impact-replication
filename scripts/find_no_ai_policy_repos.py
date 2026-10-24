#!/usr/bin/env python3
"""
Find recently-updated, non-fork GitHub repositories that:
  * have NO AI policy signal, i.e. none of:
      - an AI_POLICY.md file (anywhere)
      - a .ai/AI_POLICY.md file
      - a CONTRIBUTING.md file that mentions an AI term (CONTRIBUTING_TERMS:
        "AI", "artificial intelligence", "LLM", "ChatGPT", "coding agent", ...)
  * have more than 10 stars
  * have multiple contributors
  * have recent commits (pushed within the last N days)

This is the counterpart of find_ai_policy_repos.py, for the comparison group.
It applies the same criteria; only the policy signal is reversed. Code search
can find files that exist but not their absence, so candidates come from the
repository-search API, and each repository's default-branch file tree is then
checked for the three signals. "Mentions AI" follows code search's matching:
each term in any case, as a whole word or phrase. Repositories already in the policy
registry are skipped.

Usage:
    export GITHUB_TOKEN=ghp_your_token_here
    python find_no_ai_policy_repos.py

A token is required: unauthenticated requests are rate-limited to 10
search requests/minute and 60 core requests/hour, which is not enough.
Create one at https://github.com/settings/tokens (no scopes needed for
public data; `public_repo` if you want to be safe).

Configuration lives in the CONFIG block below — edit those variables to
change the search criteria.
"""

from __future__ import annotations

import base64
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from urllib.parse import quote

import requests

API = "https://api.github.com"

# The AI-policy signals checked for absence, mirroring find_ai_policy_repos.py:
#   - AI_POLICY.md anywhere in the repo
#   - the file specifically under a .ai/ directory
#   - CONTRIBUTING.md (anywhere) whose text mentions any CONTRIBUTING_TERMS
POLICY_FILENAME = "AI_POLICY.md"
CONTRIBUTING_FILENAME = "CONTRIBUTING.md"
# AI terms searched for in CONTRIBUTING.md, kept identical in
# find_ai_policy_repos.py and find_no_ai_policy_repos.py so no repository can
# land in both groups. Multi-word terms are matched as phrases; code search
# ignores case, hyphens and spacing between the words of a phrase.
CONTRIBUTING_TERMS = [
    "AI",
    "artificial intelligence",
    "LLM",
    "LLMs",
    "large language model",
    "large language models",
    "generative AI",
    "GenAI",
    "ChatGPT",
    "Claude",
    "OpenAI",
    "Anthropic",
    "Copilot",
    "AI-generated",
    "AI-assisted",
    "machine-generated",
    "vibe coding",
    "AI agent",
    "coding agent",
]
MENTIONS_AI = re.compile(
    r"\b(?:"
    + "|".join(
        r"[-\s]+".join(map(re.escape, t.replace("-", " ").split()))
        for t in CONTRIBUTING_TERMS
    )
    + r")\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# CONFIG — edit these to change the search criteria.
# ---------------------------------------------------------------------------
DAYS = 60  # max days since last push (recent commits)
MIN_AGE_DAYS = 183  # repo must be at least this old (~6 months)
MIN_STARS = 10  # repo must have MORE than this many stars
MIN_CONTRIBUTORS = 2  # minimum number of contributors
MAX_REPOS = 300  # max candidate repos to inspect
SEARCH_SORT = "updated"  # repository-search order: "updated", "stars", or "forks"
REGISTRY = (
    Path(__file__).resolve().parent.parent / "data/repositories/candidate_policy_repos.csv"
)
JSON_OUT = None  # path to write JSON results, or None to skip
TABLE_OUT = "no_ai_policy_repos.txt"  # path to write the text table, or None to skip
# ---------------------------------------------------------------------------


class GitHub:
    """Thin GitHub REST client that transparently handles rate limits."""

    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "ai-policy-repo-finder",
            }
        )

    def get(self, url: str, params: dict | None = None) -> requests.Response:
        """GET with retry/backoff for both flavours of GitHub rate limiting."""
        while True:
            resp = self.session.get(url, params=params)

            # Primary rate limit: wait until the window resets.
            if (
                resp.status_code == 403
                and resp.headers.get("X-RateLimit-Remaining") == "0"
            ):
                reset = int(resp.headers.get("X-RateLimit-Reset", "0"))
                wait = max(reset - int(time.time()), 1) + 1
                print(f"  rate limited; sleeping {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue

            # Secondary/abuse rate limit: honour Retry-After.
            if resp.status_code in (403, 429) and "Retry-After" in resp.headers:
                wait = int(resp.headers["Retry-After"]) + 1
                print(f"  secondary limit; sleeping {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue

            return resp


def registry_repos() -> set[str]:
    """Repositories already in the AI-policy registry (lower-cased)."""
    if not REGISTRY.is_file():
        return set()
    with REGISTRY.open(newline="", encoding="utf-8-sig") as stream:
        return {row["repository"].lower() for row in csv.DictReader(stream)}


def search_repos(gh: GitHub, cutoff: datetime, age_cutoff: datetime) -> Iterator[dict]:
    """
    Yield repositories matching the activity criteria, from the repository-search
    API. The search pre-filters on fork, stars, push date, and creation date; the
    same filters are re-checked against full metadata in collect().

    Note: repository search is capped at 1000 results per query.
    """
    query = (
        f"fork:false stars:>{MIN_STARS} "
        f"pushed:>={cutoff:%Y-%m-%d} created:<={age_cutoff:%Y-%m-%d}"
    )
    page = 1
    per_page = 100
    while True:
        resp = gh.get(
            f"{API}/search/repositories",
            params={
                "q": query,
                "sort": SEARCH_SORT,
                "order": "desc",
                "per_page": per_page,
                "page": page,
            },
        )
        if resp.status_code != 200:
            print(
                f"repository search failed for {query!r} "
                f"({resp.status_code}): {resp.text[:200]}",
                file=sys.stderr,
            )
            return

        data = resp.json()
        items = data.get("items", [])
        if not items:
            return
        yield from items

        # Repository search caps out at 1000 results (10 pages of 100).
        if page * per_page >= min(data.get("total_count", 0), 1000):
            return
        page += 1
        # Search allows ~30 req/min authenticated; be gentle.
        time.sleep(2)


def repo_details(gh: GitHub, full_name: str) -> dict | None:
    """Fetch full repo metadata (stars, fork flag, pushed_at, etc.)."""
    resp = gh.get(f"{API}/repos/{full_name}")
    if resp.status_code != 200:
        return None
    return resp.json()


def contributor_count(gh: GitHub, full_name: str, need: int) -> int:
    """
    Return the number of contributors, short-circuiting once we have enough.
    Uses the Link header to read the count cheaply when there are many.
    """
    resp = gh.get(
        f"{API}/repos/{full_name}/contributors",
        params={"per_page": 100, "anon": "true"},
    )
    if resp.status_code != 200:
        return 0

    contributors = resp.json()
    # If there is more than one page, parse the last-page number from Link.
    link = resp.headers.get("Link", "")
    if 'rel="last"' in link:
        for part in link.split(","):
            if 'rel="last"' in part:
                # ...&page=N>; rel="last"
                last = part.split("page=")[-1].split(">")[0]
                try:
                    # Assume full pages before the last one.
                    return (int(last) - 1) * 100 + 1
                except ValueError:
                    pass
    return len(contributors)


def policy_signal(gh: GitHub, details: dict) -> str | None:
    """
    The first AI-policy signal found on the default branch, "" when there is
    none, or None when absence cannot be confirmed (tree unavailable or
    truncated, or a CONTRIBUTING.md could not be read).
    """
    full_name = details["full_name"]
    resp = gh.get(
        f"{API}/repos/{full_name}/git/trees/{quote(details['default_branch'], safe='')}",
        params={"recursive": "1"},
    )
    if resp.status_code != 200:
        return None
    tree = resp.json()
    if tree.get("truncated"):
        return None
    paths = [e["path"] for e in tree.get("tree", []) if e.get("type") == "blob"]

    for path in paths:
        if path == f".ai/{POLICY_FILENAME}":
            return f".ai/{POLICY_FILENAME}"
    for path in paths:
        if path.rsplit("/", 1)[-1] == POLICY_FILENAME:
            return f"{POLICY_FILENAME} ({path})"
    for path in paths:
        if path.rsplit("/", 1)[-1] != CONTRIBUTING_FILENAME:
            continue
        file_resp = gh.get(
            f"{API}/repos/{full_name}/contents/{quote(path, safe='/')}",
            params={"ref": details["default_branch"]},
        )
        if file_resp.status_code != 200:
            return None
        content = base64.b64decode("".join(file_resp.json().get("content", "").split()))
        found = MENTIONS_AI.search(content.decode("utf-8", errors="replace"))
        if found:
            return f"CONTRIBUTING.md mentions {found.group(0)!r} ({path})"
    return ""


def collect(gh: GitHub) -> list[dict]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=DAYS)
    # Repo must have been created at least this long ago ("more than 6 months old").
    age_cutoff = now - timedelta(days=MIN_AGE_DAYS)
    registry = registry_repos()
    results: list[dict] = []
    checked = 0

    for repo in search_repos(gh, cutoff, age_cutoff):
        if checked >= MAX_REPOS:
            break
        full_name = repo["full_name"]
        if full_name.lower() in registry:
            continue
        checked += 1

        details = repo_details(gh, full_name)
        if details is None:
            continue

        # --- apply filters ---------------------------------------------
        if details.get("fork"):
            continue
        if details.get("stargazers_count", 0) <= MIN_STARS:
            continue

        created_at = datetime.strptime(
            details["created_at"], "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
        if created_at > age_cutoff:  # too young — created more recently than the cutoff
            continue

        pushed_at = datetime.strptime(
            details["pushed_at"], "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
        if pushed_at < cutoff:
            continue

        contribs = contributor_count(gh, full_name, MIN_CONTRIBUTORS)
        if contribs < MIN_CONTRIBUTORS:
            continue

        signal = policy_signal(gh, details)
        if signal is None:
            print(
                f"  ? {full_name}  could not confirm no AI policy; skipped",
                file=sys.stderr,
            )
            continue
        if signal:
            print(
                f"  ✗ {full_name}  has an AI-policy signal: {signal}", file=sys.stderr
            )
            continue

        entry = {
            "full_name": full_name,
            "html_url": details["html_url"],
            "notes": "",  # manually filled in, e.g. checked for other AI wording
            "description": details.get("description"),
            "stars": details["stargazers_count"],
            "contributors": contribs,
            "created_at": details["created_at"],
            "pushed_at": details["pushed_at"],
            "language": details.get("language"),
            "topics": details.get("topics", []),
        }
        results.append(entry)
        print(
            f"  ✓ {full_name}  "
            f"★{entry['stars']}  "
            f"{contribs} contributors  "
            f"created {entry['created_at'][:10]}  "
            f"pushed {entry['pushed_at']}",
            file=sys.stderr,
        )

    results.sort(key=lambda r: r["pushed_at"], reverse=True)
    return results


def render_table(results: list[dict]) -> str:
    """Render results as a plain-text table with aligned columns."""
    headers = [
        "Repository",
        "Stars",
        "Contrib",
        "Created",
        "Last Push",
        "Language",
        "URL",
        "Notes",
    ]
    rows = [
        [
            r["full_name"],
            str(r["stars"]),
            str(r["contributors"]),
            r["created_at"][:10],
            r["pushed_at"][:10],
            r.get("language") or "",
            r.get("html_url") or "",
            r.get("notes") or "",
        ]
        for r in results
    ]

    # Column widths sized to the widest cell (header or value) in each column.
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(cells: list[str]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))

    sep = "  ".join("-" * w for w in widths)
    lines = [fmt(headers), sep] + [fmt(row) for row in rows]
    return "\n".join(lines)


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("error: set GITHUB_TOKEN environment variable", file=sys.stderr)
        return 1

    gh = GitHub(token)
    print(
        "Searching for active repos with no AI-policy signal "
        f"({POLICY_FILENAME}, .ai/{POLICY_FILENAME}, "
        f"{CONTRIBUTING_FILENAME} mentioning an AI term)...",
        file=sys.stderr,
    )
    results = collect(gh)

    print(f"\nFound {len(results)} matching repositories:\n")
    table = render_table(results)
    print(table)

    if TABLE_OUT and results:
        with open(TABLE_OUT, "w") as f:
            f.write(f"Found {len(results)} matching repositories\n\n")
            f.write(table + "\n")
        print(f"\nWrote table to {TABLE_OUT}", file=sys.stderr)

    if JSON_OUT:
        with open(JSON_OUT, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\nWrote {len(results)} results to {JSON_OUT}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
