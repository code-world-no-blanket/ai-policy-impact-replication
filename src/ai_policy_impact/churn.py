"""Lines added and deleted by default-branch commits in bands around a policy date."""

from __future__ import annotations

import hashlib
import re
import time
from datetime import date, timedelta
from pathlib import Path

from ai_policy_impact.activity import (
    begin_record,
    coverage_fields,
    fail_component,
    graphql_pages,
    is_timeout,
    median,
    run_banded_collection,
)
from ai_policy_impact.config import HorizonConfig, StudyConfig
from ai_policy_impact.github import APIError, GitHubClient, utc_now
from ai_policy_impact.runs import write_csv
from ai_policy_impact.windows import BANDS, Window, parse_time

PAGE_SIZE = 100
MAX_PAGES = 1000

QUERY = """query($owner:String!,$name:String!,$branch:String!,$first:Int!,
  $cursor:String,$since:GitTimestamp!,$until:GitTimestamp!) {
  rateLimit { cost remaining resetAt }
  repository(owner:$owner,name:$name) { ref(qualifiedName:$branch) {
    target { ... on Commit {
      history(first:$first,after:$cursor,since:$since,until:$until) {
        nodes { oid committedDate authoredDate additions deletions message
          changedFilesIfAvailable parents { totalCount }
          author { email user { login } } committer { user { login } }
          statusCheckRollup { state }
        }
        pageInfo { hasNextPage endCursor }
      }
    } }
  } }
}"""

# How a commit names the pull request it lands. Only two places count, because
# messages also mention PRs they supersede, follow up or merely discuss:
# - the title: GitHub's squash suffix "(#N)", "Merge pull request #N", or a
#   "Merge owner/repo#N" merge title;
# - a line that starts with a landing keyword, such as pytorch's "Pull Request
#   resolved: <url>", openssl's "(Merged from <url>)", or "Closes #N".
# Issues share the numbering, so the panel keeps only known pull requests.
LANDING_KEYWORD = (
    r"(?:merged[- ]from|pull request resolved|pr-url|pr|"
    r"close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b\s*:?\s*"
)
LANDING_LINE = re.compile(rf"^\(?{LANDING_KEYWORD}", re.IGNORECASE)
TITLE_SUFFIX = re.compile(r"\(#(\d+)\)")
TITLE_MERGE = re.compile(r"^merge pull request #(\d+)\b", re.IGNORECASE)


def pr_references(message: str | None, repository: str) -> list[int]:
    """Pull-request numbers of this repository that a commit message says it lands."""
    if not message:
        return []
    owner, name = (re.escape(part) for part in repository.split("/"))
    # A bare "#N", this repository's "owner/repo#N", or a link to the PR itself
    # (not to one of its comments); other repositories' references are ignored.
    # Only the reference right after each keyword: "Fixes #1, Closes #2" lands
    # both, while "Fixes #1, supersedes #2" lands only #1.
    reference = re.compile(
        rf"{LANDING_KEYWORD}(?:(?:(?<![\w/.-])|\b{owner}/{name})#(\d+)\b"
        rf"|https?://github\.com/{owner}/{name}/pull/(\d+)(?![\d#]))",
        re.IGNORECASE,
    )
    lines = [line.strip() for line in message.splitlines()]
    title = lines[0] if lines else ""
    if title.lower().startswith("revert"):
        # A revert quotes the reverted title, "(#N)" included; it lands nothing.
        return []
    found = TITLE_SUFFIX.findall(title) + TITLE_MERGE.findall(title)
    merge_title = re.match(rf"^merge {owner}/{name}#(\d+)\b", title, re.IGNORECASE)
    if merge_title:
        found.append(merge_title.group(1))
    for line in lines[1:]:
        if LANDING_LINE.match(line):
            found += [a or b for a, b in reference.findall(line)]
    return sorted({int(n) for n in found})


def author_identity(author: dict | None) -> tuple[str, str | None, bool]:
    """Same identity rule as the contributors collector: login, else hashed email."""
    author = author or {}
    login = (author.get("user") or {}).get("login")
    email = (author.get("email") or "").lower()
    if login:
        return login, login, login.endswith("[bot]") or "[bot]" in email
    digest = hashlib.sha256(email.encode()).hexdigest()[:16]
    return f"email:{digest}", None, "[bot]" in email


def unreadable_commit(error: APIError) -> bool:
    """A page GitHub could not compute: a timeout, or a commit whose line counts
    it cannot report ("The additions count for this commit is unavailable.")."""
    return is_timeout(error) or "count for this commit is unavailable" in str(error)


def skip_commit(cursor: str, skipped: list[int]) -> str:
    """The history cursor one commit further on; history cursors are "<oid> <n>"."""
    head, _, position = cursor.rpartition(" ")
    if not head or not position.isdigit():
        raise APIError(200, f"Cannot skip past history cursor {cursor!r}")
    skipped.append(int(position) + 1)
    return f"{head} {int(position) + 1}"


def commit_row(node: dict, window: Window, repository: str) -> dict:
    ident, login, is_bot = author_identity(node.get("author"))
    return {
        "sha": node["oid"],
        "committed_at": node["committedDate"],
        "authored_at": node.get("authoredDate"),
        "contributor_id": ident,
        "login": login,
        "is_bot": is_bot,
        "is_merge": node["parents"]["totalCount"] > 1,
        "additions": node.get("additions"),
        "deletions": node.get("deletions"),
        "changed_files": node.get("changedFilesIfAvailable"),
        "band": window.band(parse_time(node["committedDate"])),
        # Who put the commit on the branch, e.g. the maintainer landing a PR.
        "committer_login": ((node.get("committer") or {}).get("user") or {}).get(
            "login"
        ),
        "ci_state": (node.get("statusCheckRollup") or {}).get("state"),
        # The message itself is not kept: it can contain co-author emails.
        "referenced_prs": pr_references(node.get("message"), repository),
    }


def summarize_churn(rows: list[dict], window: Window) -> dict:
    # Merge commits repeat their branch's changes; bot commits are not team effort.
    counted = [r for r in rows if not r["is_merge"] and not r["is_bot"]]
    summary = {
        "commits_in_window": len(rows),
        "merge_commits": sum(r["is_merge"] for r in rows),
        "bot_commits": sum(r["is_bot"] and not r["is_merge"] for r in rows),
        **coverage_fields(window),
    }
    for name in BANDS:
        band = [r for r in counted if r["band"] == name]
        observed = window.observed(name)
        summary[f"{name}_commits"] = len(band) if observed else None
        summary[f"{name}_additions"] = (
            sum(r["additions"] or 0 for r in band) if observed else None
        )
        summary[f"{name}_deletions"] = (
            sum(r["deletions"] or 0 for r in band) if observed else None
        )
        summary[f"{name}_median_lines_per_commit"] = median(
            [
                r["additions"] + r["deletions"]
                for r in band
                if r["additions"] is not None and r["deletions"] is not None
            ]
        )
        summary[f"{name}_files_unavailable"] = (
            sum(r["changed_files"] is None for r in band) if observed else None
        )
    return summary


def collect_repository_churn(
    client: GitHubClient,
    candidate: dict,
    anchor: date | None,
    anchor_source: str,
    horizons: HorizonConfig,
    today: date,
    *,
    sleep=time.sleep,
) -> dict:
    record, details = begin_record(client, candidate, anchor, anchor_source)
    if details is None:
        return record
    window = Window.around(anchor, horizons, today)
    owner, name = record["repository"].split("/")
    variables = {
        "owner": owner,
        "name": name,
        "branch": f"refs/heads/{record['default_branch']}",
        "since": window.start_time.isoformat(),
        "until": (window.cutoff - timedelta(seconds=1)).isoformat(),
    }
    skipped: list[int] = []
    nodes: list[dict] = []
    try:
        for connection in graphql_pages(
            client,
            QUERY,
            variables,
            lambda data: (
                ((data.get("repository") or {}).get("ref") or {}).get("target") or {}
            ).get("history"),
            max_pages=MAX_PAGES,
            page_size=PAGE_SIZE,
            skip=lambda cursor: skip_commit(cursor, skipped),
            unreadable=unreadable_commit,
            sleep=sleep,
        ):
            nodes.extend(connection["nodes"])
    except APIError as error:
        # Keep the commits read before the failure; the record stays partial.
        record["commits"] = [
            commit_row(node, window, record["repository"]) for node in nodes
        ]
        record["skipped_commits"] = len(skipped)
        return fail_component(record, "history", error)
    rows = [commit_row(node, window, record["repository"]) for node in nodes]
    record["commits"] = rows
    # Commits GitHub could not compute even alone (e.g. huge generated diffs).
    record["skipped_commits"] = len(skipped)
    record.update(summarize_churn(rows, window))
    record["completed_at"] = utc_now()
    return record


SUMMARY_COLUMNS = [
    "requested_repository",
    "repository",
    "status",
    "anchor_date",
    "anchor_source",
    "reported_policy_date",
    "window_start",
    "window_end",
    "commits_in_window",
    "merge_commits",
    "bot_commits",
    *[
        f"{b}_{m}"
        for b in BANDS
        for m in (
            "days_observed",
            "commits",
            "additions",
            "deletions",
            "median_lines_per_commit",
            "files_unavailable",
        )
    ],
]
COMMIT_COLUMNS = [
    "repository",
    "sha",
    "committed_at",
    "authored_at",
    "contributor_id",
    "login",
    "is_bot",
    "is_merge",
    "additions",
    "deletions",
    "changed_files",
    "band",
    "referenced_prs",
    "committer_login",
    "ci_state",
]


def write_outputs(interim: Path, records: list[dict]) -> None:
    write_csv(interim / "summary.csv", SUMMARY_COLUMNS, records)
    write_csv(
        interim / "commits.csv",
        COMMIT_COLUMNS,
        [
            {
                "repository": r.get("repository", r["requested_repository"]),
                **row,
                "referenced_prs": ";".join(map(str, row.get("referenced_prs", []))),
            }
            for r in records
            for row in r.get("commits", [])
        ],
    )


def collect_churn(
    study: StudyConfig, candidates_path: Path, **options
) -> tuple[Path, dict]:
    return run_banded_collection(
        study,
        candidates_path,
        kind="churn",
        measurement="default_branch_commit_lines_around_unverified_anchor",
        collector=collect_repository_churn,
        write_outputs=write_outputs,
        describe=lambda r: (
            f"commits in window: {r.get('commits_in_window')}; "
            f"merges: {r.get('merge_commits')}"
        ),
        **options,
    )
