#!/usr/bin/env python3
"""
Find recently-updated, non-fork GitHub repositories that:
  * contain an AI policy signal, one of:
      - an AI_POLICY.md file (anywhere)
      - a .ai/AI_POLICY.md file
      - a CONTRIBUTING.md file that mentions an AI term (CONTRIBUTING_TERMS:
        "AI", "artificial intelligence", "LLM", "ChatGPT", "coding agent", ...)
  * have more than 10 stars
  * have multiple contributors
  * have recent commits (pushed within the last N days)

Usage:
    export GITHUB_TOKEN=ghp_your_token_here
    python find_ai_policy_repos.py

A token is required: unauthenticated requests are rate-limited to 10
code-search requests/minute and 60 core requests/hour, which is not enough.
Create one at https://github.com/settings/tokens (no scopes needed for
public data; `public_repo` if you want to be safe).

Configuration lives in the CONFIG block below — edit those variables to
change the search criteria.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Iterator

import requests

API = "https://api.github.com"

# Code-search queries that signal an AI policy. Each is (label, query).
# They are tried in order; a repo is kept the first time any query matches it.
#   - filename:AI_POLICY.md   → AI_POLICY.md anywhere in the repo
#   - path:.ai/AI_POLICY.md   → the file specifically under a .ai/ directory
#   - AI filename:CONTRIBUTING.md → CONTRIBUTING.md whose text mentions "AI"
#     (one such query per term in CONTRIBUTING_TERMS; a quoted phrase matches
#     the exact phrase, a plain term matches the whole word)
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


def term_query(term: str) -> str:
    words = term.replace("-", " ").split()
    return f'"{" ".join(words)}"' if len(words) > 1 else term


SEARCHES = [
    ("AI_POLICY.md", "filename:AI_POLICY.md"),
    (".ai/AI_POLICY.md", "path:.ai/AI_POLICY.md"),
    *[
        (f"CONTRIBUTING.md (mentions {term})", f"{term_query(term)} filename:CONTRIBUTING.md")
        for term in CONTRIBUTING_TERMS
    ],
]

# ---------------------------------------------------------------------------
# CONFIG — edit these to change the search criteria.
# ---------------------------------------------------------------------------
DAYS = 60               # max days since last push (recent commits)
MIN_AGE_DAYS = 183      # repo must be at least this old (~6 months)
MIN_STARS = 10          # repo must have MORE than this many stars
MIN_CONTRIBUTORS = 2    # minimum number of contributors
MAX_REPOS = 300         # max candidate repos to inspect
JSON_OUT = None         # path to write JSON results, or None to skip
TABLE_OUT = "ai_policy_repos.txt"   # path to write the text table, or None to skip
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


def search_code_repos(gh: GitHub, cutoff: datetime) -> Iterator[dict]:
    """
    Yield unique matches (deduped by repo) whose default branch contains one of
    the AI-policy signals defined in SEARCHES. Uses the code-search API, which
    searches file paths with `filename:`/`path:` and file contents with plain
    terms.

    Each yielded dict has:
      "repository":  the repo object
      "policy_url":  html_url of the matching file itself
      "policy_path": path of the matching file within the repo
      "match_label": which SEARCHES entry matched

    Note: code search only covers the default branch and can be incomplete for
    very large result sets (capped at 1000 results per query), but it is the
    only way to query file contents/paths across all of GitHub.
    """
    seen: set[str] = set()  # deduped across all queries, first match wins
    for label, query in SEARCHES:
        page = 1
        per_page = 100
        while True:
            resp = gh.get(
                f"{API}/search/code",
                params={
                    "q": query,
                    "per_page": per_page,
                    "page": page,
                },
            )
            if resp.status_code != 200:
                print(
                    f"code search failed for {query!r} "
                    f"({resp.status_code}): {resp.text[:200]}",
                    file=sys.stderr,
                )
                break

            data = resp.json()
            items = data.get("items", [])
            if not items:
                break

            for item in items:
                repo = item["repository"]
                full_name = repo["full_name"]
                if full_name in seen:
                    continue
                seen.add(full_name)
                yield {
                    "repository": repo,
                    "policy_url": item.get("html_url"),
                    "policy_path": item.get("path"),
                    "match_label": label,
                }

            # code search caps out at 1000 results (10 pages of 100).
            if page * per_page >= min(data.get("total_count", 0), 1000):
                break
            page += 1
            # Code search allows ~10 req/min authenticated; be gentle.
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


def collect(gh: GitHub) -> list[dict]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=DAYS)
    # Repo must have been created at least this long ago ("more than 6 months old").
    age_cutoff = now - timedelta(days=MIN_AGE_DAYS)
    results: list[dict] = []
    checked = 0

    for match in search_code_repos(gh, cutoff):
        if checked >= MAX_REPOS:
            break
        checked += 1

        repo = match["repository"]
        full_name = repo["full_name"]

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

        entry = {
            "full_name": full_name,
            "html_url": details["html_url"],
            "policy_url": match.get("policy_url"),
            "policy_path": match.get("policy_path"),
            "match_label": match.get("match_label"),
            "notes": "",  # manually filled in to describe the AI policy
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
    headers = ["Repository", "Stars", "Contrib", "Created",
               "Last Push", "Signal", "AI Policy URL", "Notes"]
    rows = [
        [
            r["full_name"],
            str(r["stars"]),
            str(r["contributors"]),
            r["created_at"][:10],
            r["pushed_at"][:10],
            r.get("match_label") or "",
            r.get("policy_url") or "",
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
    labels = ", ".join(label for label, _ in SEARCHES)
    print(f"Searching for repos with AI-policy signals ({labels})...",
          file=sys.stderr)
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
