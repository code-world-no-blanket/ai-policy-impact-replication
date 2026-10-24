"""When each person first authored a commit, pull request, or issue in a repository.

The whole history is read, so a newcomer is someone whose first-ever authored
contribution falls in a given week. Commits are identified as in the churn
collector (login, else hashed email); pull requests and issues by login. Only the
earliest date per person and kind is kept. Reviews and comments are not covered.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from ai_policy_impact.activity import graphql_pages, is_bot_actor
from ai_policy_impact.churn import author_identity
from ai_policy_impact.config import StudyConfig
from ai_policy_impact.github import APIError, GitHubClient, utc_now
from ai_policy_impact.runs import guarded, select_candidates, start_run, write_csv

PAGE_SIZE = 100
MAX_PAGES = 10000
KINDS = ("commit", "pull_request", "issue")

COMMIT_QUERY = """query($owner:String!,$name:String!,$branch:String!,$first:Int!,
  $cursor:String) {
  rateLimit { cost remaining resetAt }
  repository(owner:$owner,name:$name) { ref(qualifiedName:$branch) {
    target { ... on Commit { history(first:$first,after:$cursor) {
      nodes { committedDate author { email user { login } } }
      pageInfo { hasNextPage endCursor }
    } } }
  } }
}"""
ITEM_QUERY = """query($owner:String!,$name:String!,$first:Int!,$cursor:String) {
  rateLimit { cost remaining resetAt }
  repository(owner:$owner,name:$name) {
    %s(first:$first,after:$cursor,orderBy:{field:CREATED_AT,direction:ASC}) {
      nodes { createdAt author { __typename login } }
      pageInfo { hasNextPage endCursor }
    }
  }
}"""


def earliest(first: dict, person: str, kind: str, moment: str) -> None:
    entry = first.setdefault(person, {})
    if kind not in entry or moment < entry[kind]:
        entry[kind] = moment


def normalize(moment: str) -> str:
    """UTC ISO timestamps compare correctly as text."""
    parsed = datetime.fromisoformat(moment.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc).isoformat()


def collect_repository_first_seen(
    client: GitHubClient, candidate: dict, *, sleep=time.sleep
) -> dict:
    record = {
        "requested_repository": candidate["repository"],
        "started_at": utc_now(),
        "status": "complete",
        "errors": [],
    }
    try:
        details = client.get(f"/repos/{candidate['repository']}").data
    except APIError as error:
        record["errors"].append({"component": "metadata", "error": str(error)})
        record.update(status="failed", completed_at=utc_now())
        return record
    if details.get("private"):
        record.update(status="skipped_private", completed_at=utc_now())
        return record
    record.update(
        repository=details["full_name"], default_branch=details["default_branch"]
    )
    owner, name = record["repository"].split("/")
    first: dict[str, dict] = {}
    bots: set[str] = set()
    counts = {}
    try:
        seen = 0
        for connection in graphql_pages(
            client,
            COMMIT_QUERY,
            {
                "owner": owner,
                "name": name,
                "branch": f"refs/heads/{record['default_branch']}",
            },
            lambda data: (
                ((data.get("repository") or {}).get("ref") or {}).get("target") or {}
            ).get("history"),
            max_pages=MAX_PAGES,
            page_size=PAGE_SIZE,
            sleep=sleep,
        ):
            for node in connection["nodes"]:
                ident, _, is_bot = author_identity(node.get("author"))
                ident = ident.lower()
                if is_bot:
                    bots.add(ident)
                earliest(first, ident, "commit", normalize(node["committedDate"]))
                seen += 1
        counts["commit"] = seen
        if details.get("has_issues") is False:
            record["errors"].append({"component": "issue", "error": "issues disabled"})
        for kind, field in (("pull_request", "pullRequests"), ("issue", "issues")):
            if kind == "issue" and details.get("has_issues") is False:
                continue
            seen = 0
            for connection in graphql_pages(
                client,
                ITEM_QUERY % field,
                {"owner": owner, "name": name},
                lambda data, field=field: (data.get("repository") or {}).get(field),
                max_pages=MAX_PAGES,
                page_size=PAGE_SIZE,
                sleep=sleep,
            ):
                for node in connection["nodes"]:
                    author = node.get("author")
                    if not author or not author.get("login"):
                        continue  # deleted accounts
                    login = author["login"].lower()
                    if is_bot_actor(author):
                        bots.add(login)
                    earliest(first, login, kind, normalize(node["createdAt"]))
                    seen += 1
            counts[kind] = seen
    except APIError as error:
        # A partial history would turn old contributors into newcomers.
        record["errors"].append({"component": "history", "error": str(error)})
        record.update(status="failed", completed_at=utc_now())
        return record
    if record["errors"]:
        record["status"] = "partial"
    record["items_read"] = counts
    record["people"] = [
        {"person": person, "is_bot": person in bots, **dates}
        for person, dates in sorted(first.items())
    ]
    record["completed_at"] = utc_now()
    return record


SUMMARY_COLUMNS = [
    "requested_repository",
    "repository",
    "status",
    "started_at",
    "completed_at",
    "people",
]
PEOPLE_COLUMNS = ["repository", "person", "is_bot", *KINDS]


def write_outputs(interim: Path, records: list[dict]) -> None:
    write_csv(
        interim / "summary.csv",
        SUMMARY_COLUMNS,
        [{**r, "people": len(r.get("people", []))} for r in records],
    )
    write_csv(
        interim / "first_seen.csv",
        PEOPLE_COLUMNS,
        [
            {"repository": r.get("repository", r["requested_repository"]), **row}
            for r in records
            for row in r.get("people", [])
        ],
    )


def collect_first_seen(
    study: StudyConfig,
    candidates_path: Path,
    *,
    priority: str | None = None,
    limit: int | None = None,
    progress=print,
) -> tuple[Path, dict]:
    candidates = select_candidates(candidates_path, priority, limit)
    run = start_run(
        study,
        "first_seen",
        candidates_path,
        candidates,
        measurement="first_authored_commit_pr_issue_full_history",
    )
    for index, candidate in enumerate(candidates, 1):
        progress(f"[{index}/{len(candidates)}] {candidate['repository']}")
        record = guarded(
            collect_repository_first_seen, candidate, run.client, candidate
        )
        run.save_record(candidate, record)
        write_outputs(run.interim, run.records)
        progress(
            f"  {record['status']}; people: {len(record.get('people', []))}; "
            f"read: {record.get('items_read')}"
        )
    return run.interim, run.finish()
