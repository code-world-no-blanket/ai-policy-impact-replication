"""Issues opened and closed in bands around a policy date (pull requests excluded)."""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path

from ai_policy_impact.activity import (
    begin_record,
    band_counts,
    coverage_fields,
    fail_component,
    hours_between,
    is_bot_actor,
    median,
    run_banded_collection,
    sweep_updated,
)
from ai_policy_impact.config import HorizonConfig, StudyConfig
from ai_policy_impact.github import APIError, GitHubClient, utc_now
from ai_policy_impact.runs import write_csv
from ai_policy_impact.windows import BANDS, Window, parse_time

PAGE_SIZE = 100
MAX_PAGES = 1000

# The GraphQL issues connection never includes pull requests, unlike REST /issues.
QUERY = (
    """query($owner:String!,$name:String!,$cursor:String) {
  rateLimit { cost remaining resetAt }
  repository(owner:$owner,name:$name) {
    issues(first:%d,after:$cursor,orderBy:{field:UPDATED_AT,direction:DESC}) {
      nodes { number createdAt updatedAt closedAt state stateReason
        authorAssociation author { __typename login } comments { totalCount }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}"""
    % PAGE_SIZE
)


def issue_row(node: dict, window: Window) -> dict | None:
    created = parse_time(node["createdAt"])
    closed = parse_time(node.get("closedAt"))
    bands = {"created_band": window.band(created), "closed_band": window.band(closed)}
    if not any(bands.values()):
        return None
    author = node.get("author") or {}
    return {
        "number": node["number"],
        "author": author.get("login"),
        "author_is_bot": is_bot_actor(author),
        "author_association": node.get("authorAssociation"),
        "state": node.get("state"),
        "state_reason": node.get("stateReason"),
        "created_at": node["createdAt"],
        "closed_at": node.get("closedAt"),
        **bands,
        "hours_to_close": hours_between(created, closed)
        if closed and closed < window.cutoff
        else None,
        "open_at_cutoff": closed is None or closed >= window.cutoff,
        "comments": (node.get("comments") or {}).get("totalCount"),
    }


def summarize_issues(rows: list[dict], window: Window) -> dict:
    humans = [r for r in rows if not r["author_is_bot"]]
    completed = [r for r in humans if r["state_reason"] == "COMPLETED"]
    summary = {
        "issues_in_window": len(rows),
        "bot_issues_in_window": len(rows) - len(humans),
        **coverage_fields(window),
        **band_counts(humans, window, "created_band", "issues_opened"),
        **band_counts(humans, window, "closed_band", "issues_closed"),
        **band_counts(completed, window, "closed_band", "issues_closed_completed"),
    }
    for name in BANDS:
        opened = [r for r in humans if r["created_band"] == name]
        summary[f"{name}_issues_open_at_cutoff"] = (
            sum(r["open_at_cutoff"] for r in opened) if window.observed(name) else None
        )
        summary[f"{name}_median_hours_to_close"] = median(
            [r["hours_to_close"] for r in opened if r["hours_to_close"] is not None]
        )
    return summary


def collect_repository_issues(
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
    # Current setting only; a tracker disabled now may have been enabled before.
    record["has_issues"] = details.get("has_issues")
    if not details.get("has_issues"):
        # No tracker on GitHub is missing data, not zero issues.
        record["errors"].append({"component": "issues", "error": "issues disabled"})
        record.update(status="partial", completed_at=utc_now())
        return record
    window = Window.around(anchor, horizons, today)
    owner, name = record["repository"].split("/")
    try:
        nodes = sweep_updated(
            client,
            QUERY,
            {"owner": owner, "name": name},
            lambda data: (data.get("repository") or {}).get("issues"),
            window.start_time,
            max_pages=MAX_PAGES,
            sleep=sleep,
        )
    except APIError as error:
        return fail_component(record, "issues", error)
    rows = [row for node in nodes.values() if (row := issue_row(node, window))]
    record["issues"] = sorted(rows, key=lambda r: r["number"])
    record.update(summarize_issues(rows, window))
    record["completed_at"] = utc_now()
    return record


SUMMARY_COLUMNS = [
    "requested_repository",
    "repository",
    "status",
    "has_issues",
    "anchor_date",
    "anchor_source",
    "reported_policy_date",
    "window_start",
    "window_end",
    "issues_in_window",
    "bot_issues_in_window",
    *[
        f"{b}_{m}"
        for b in BANDS
        for m in (
            "days_observed",
            "issues_opened",
            "issues_closed",
            "issues_closed_completed",
            "issues_open_at_cutoff",
            "median_hours_to_close",
        )
    ],
]
ISSUE_COLUMNS = [
    "repository",
    "number",
    "author",
    "author_is_bot",
    "author_association",
    "state",
    "state_reason",
    "created_at",
    "closed_at",
    "created_band",
    "closed_band",
    "hours_to_close",
    "open_at_cutoff",
    "comments",
]


def write_outputs(interim: Path, records: list[dict]) -> None:
    write_csv(interim / "summary.csv", SUMMARY_COLUMNS, records)
    write_csv(
        interim / "issues.csv",
        ISSUE_COLUMNS,
        [
            {"repository": r.get("repository", r["requested_repository"]), **row}
            for r in records
            for row in r.get("issues", [])
        ],
    )


def collect_issues(
    study: StudyConfig, candidates_path: Path, **options
) -> tuple[Path, dict]:
    return run_banded_collection(
        study,
        candidates_path,
        kind="issues",
        measurement="issues_around_unverified_anchor",
        collector=collect_repository_issues,
        write_outputs=write_outputs,
        describe=lambda r: (
            f"issues in window: {r.get('issues_in_window')}; "
            f"bots: {r.get('bot_issues_in_window')}"
        ),
        **options,
    )
