"""Reconstruct the commit history of candidate AI policy files.

The output proposes a policy adoption date for manual verification; it never
marks a date as verified.
"""

from __future__ import annotations

import base64
import re
from datetime import date
from pathlib import Path
from urllib.parse import quote

from ai_policy_impact.config import StudyConfig
from ai_policy_impact.github import APIError, GitHubClient, utc_now
from ai_policy_impact.runs import paginate, select_candidates, start_run, write_csv

# Case-sensitive acronyms avoid matching words such as "maintain" or "said".
AI_TERMS = re.compile(
    r"\bAI\b|\bA\.I\.|\bLLMs?\b|\bGenAI\b"
    r"|(?i:\bartificial intelligence\b|\blarge language models?\b|\bgenerative\b"
    r"|\bchatgpt\b|\bcopilot\b|\bclaude\b|\bgemini\b|\bopenai\b|\banthropic\b"
    r"|\bmachine[- ]generated\b|\bvibe[- ]cod)"
)
# Patches are inspected only for commits on or after this date, the start of the
# GitHub Copilot technical preview. The file as it stood just before this date is
# checked separately, so earlier AI wording is flagged rather than missed.
INSPECT_SINCE = "2021-06-29T00:00:00Z"
MAX_LIST_PAGES = 30
MAX_DETAILS_PER_PATH = 300


def ai_lines(lines: list[str]) -> list[str]:
    return [line for line in lines if AI_TERMS.search(line)]


def patch_lines(patch: str) -> tuple[list[str], list[str]]:
    added, removed = [], []
    for line in patch.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            removed.append(line[1:])
    return added, removed


def commit_row(detail: dict, path: str) -> dict:
    commit = detail["commit"]
    entry = next(
        (
            f
            for f in detail.get("files", [])
            if f.get("filename") == path or f.get("previous_filename") == path
        ),
        None,
    )
    row = {
        "path": path,
        "sha": detail["sha"],
        "committed_at": commit["committer"]["date"],
        "authored_at": commit["author"]["date"],
        "author_login": (detail.get("author") or {}).get("login"),
        "message_summary": commit["message"].splitlines()[0][:200]
        if commit["message"]
        else "",
        "html_url": detail.get("html_url"),
        "file_status": entry.get("status") if entry else "file_not_in_response",
        "previous_filename": entry.get("previous_filename") if entry else None,
        "patch_available": bool(entry and "patch" in entry),
    }
    if row["patch_available"]:
        added, removed = patch_lines(entry["patch"])
        ai_added, ai_removed = ai_lines(added), ai_lines(removed)
        row.update(
            lines_added=len(added),
            lines_removed=len(removed),
            ai_lines_added=len(ai_added),
            ai_lines_removed=len(ai_removed),
            first_ai_line_added=ai_added[0].strip()[:200] if ai_added else "",
        )
    return row


def file_mentions_ai(client: GitHubClient, repo: str, path: str, ref: str) -> bool:
    response = client.get(f"/repos/{repo}/contents/{quote(path, safe='/')}", ref=ref)
    content = base64.b64decode("".join(response.data.get("content", "").split()))
    return bool(AI_TERMS.search(content.decode("utf-8", errors="replace")))


def collect_path_history(
    client: GitHubClient, repo: str, branch: str, path: str
) -> dict:
    result = {"path": path, "status": "complete", "errors": [], "commits": []}
    listed, complete = paginate(
        client,
        f"/repos/{repo}/commits",
        max_pages=MAX_LIST_PAGES,
        sha=branch,
        path=path,
    )
    result.update(commits_listed=len(listed), listing_complete=complete)
    if not complete:
        result["status"] = "partial"
    if not listed:
        # Missing at this path on the default branch: never added, or moved.
        result["status"] = "no_history"
        return result
    oldest_first = list(reversed(listed))
    before = [
        c for c in oldest_first if c["commit"]["committer"]["date"] < INSPECT_SINCE
    ]
    inspected = [
        c for c in oldest_first if c["commit"]["committer"]["date"] >= INSPECT_SINCE
    ]
    if before:
        try:
            result["ai_text_before_inspection_window"] = file_mentions_ai(
                client, repo, path, before[-1]["sha"]
            )
        except APIError as error:
            # 404 here usually means the file was deleted at that commit.
            result["ai_text_before_inspection_window"] = None
            if error.status != 404:
                result["errors"].append(str(error))
    if len(inspected) > MAX_DETAILS_PER_PATH:
        inspected = inspected[:MAX_DETAILS_PER_PATH]
        result["status"] = "partial"
        result["errors"].append(
            f"inspected only the first {MAX_DETAILS_PER_PATH} commits"
        )
    for listed_commit in inspected:
        try:
            detail = client.get(f"/repos/{repo}/commits/{listed_commit['sha']}")
        except APIError as error:
            result["status"] = "partial"
            result["errors"].append(f"{listed_commit['sha']}: {error}")
            continue
        result["commits"].append(commit_row(detail.data, path))
    result["history_starts_with_rename"] = oldest_first[0]["sha"] in {
        c["sha"] for c in result["commits"] if c["file_status"] == "renamed"
    }
    return result


def collect_policy_history(client: GitHubClient, candidate: dict) -> dict:
    repo = candidate["repository"]
    record = {
        "requested_repository": repo,
        "reported_policy_date": candidate.get("reported_policy_date") or None,
        "started_at": utc_now(),
        "status": "complete",
        "errors": [],
        "paths": [],
        "review_status": "needs_review",
    }
    try:
        details = client.get(f"/repos/{repo}")
    except APIError as error:
        record["errors"].append({"component": "metadata", "error": str(error)})
        record.update(status="failed", completed_at=utc_now())
        return record
    if details.data.get("private"):
        record.update(status="skipped_private", completed_at=utc_now())
        return record
    repo = details.data["full_name"]
    record.update(repository=repo, default_branch=details.data["default_branch"])
    for path in filter(None, candidate["policy_paths"].split("|")):
        try:
            history = collect_path_history(client, repo, record["default_branch"], path)
        except APIError as error:
            history = {
                "path": path,
                "status": "failed",
                "errors": [str(error)],
                "commits": [],
            }
        record["paths"].append(history)
    if any(
        p["status"] in {"partial", "failed"} or p["errors"] for p in record["paths"]
    ):
        record["status"] = "partial"

    ai_commits = sorted(
        (c for p in record["paths"] for c in p["commits"] if c.get("ai_lines_added")),
        key=lambda c: c["committed_at"],
    )
    if ai_commits:
        first = ai_commits[0]
        record.update(
            detected_policy_date=first["committed_at"][:10],
            detected_path=first["path"],
            detected_sha=first["sha"],
            detected_file_status=first["file_status"],
            detected_first_line=first["first_ai_line_added"],
            ai_commits=len(ai_commits),
        )
        if record["reported_policy_date"]:
            record["detected_minus_reported_days"] = (
                date.fromisoformat(record["detected_policy_date"])
                - date.fromisoformat(record["reported_policy_date"])
            ).days
    record["flags"] = sorted(
        {
            f"{p['path']}: {flag}"
            for p in record["paths"]
            for flag, present in [
                ("no_history_at_path", p["status"] == "no_history"),
                (
                    "ai_text_before_inspection_window",
                    p.get("ai_text_before_inspection_window"),
                ),
                ("history_starts_with_rename", p.get("history_starts_with_rename")),
                (
                    "patch_unavailable",
                    any(not c["patch_available"] for c in p["commits"]),
                ),
            ]
            if present
        }
    )
    record["completed_at"] = utc_now()
    return record


EVENT_COLUMNS = [
    "requested_repository",
    "repository",
    "status",
    "review_status",
    "reported_policy_date",
    "detected_policy_date",
    "detected_minus_reported_days",
    "detected_path",
    "detected_sha",
    "detected_file_status",
    "detected_first_line",
    "ai_commits",
    "flags",
]
COMMIT_COLUMNS = [
    "repository",
    "path",
    "sha",
    "committed_at",
    "authored_at",
    "author_login",
    "file_status",
    "previous_filename",
    "patch_available",
    "lines_added",
    "lines_removed",
    "ai_lines_added",
    "ai_lines_removed",
    "first_ai_line_added",
    "message_summary",
    "html_url",
]


def write_outputs(interim: Path, records: list[dict]) -> None:
    write_csv(
        interim / "policy_events.csv",
        EVENT_COLUMNS,
        [{**r, "flags": "; ".join(r.get("flags", []))} for r in records],
    )
    write_csv(
        interim / "policy_commits.csv",
        COMMIT_COLUMNS,
        [
            {"repository": r.get("repository", r["requested_repository"]), **c}
            for r in records
            for p in r["paths"]
            for c in p["commits"]
        ],
    )


def collect_policy_histories(
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
        "policy_history",
        candidates_path,
        candidates,
        measurement="proposed_policy_dates_unverified",
        inspect_since=INSPECT_SINCE,
    )
    for index, candidate in enumerate(candidates, 1):
        progress(f"[{index}/{len(candidates)}] {candidate['repository']}")
        record = collect_policy_history(run.client, candidate)
        run.save_record(candidate, record)
        write_outputs(run.interim, run.records)
        progress(
            f"  {record['status']}; detected: {record.get('detected_policy_date', 'none')}; "
            f"reported: {record['reported_policy_date'] or 'none'}"
        )
    return run.interim, run.finish()
