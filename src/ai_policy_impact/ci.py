"""CI results across each pull request's lifecycle, around a policy date.

For every PR opened in the window, the combined check-and-status result of each
commit pushed to it, oldest first. CI usually runs on the head of each push, so
commits with a result stand for pushes; commits pushed together without their
own run have none. A failing run later re-run to success shows only the success.
"""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path

from ai_policy_impact.activity import (
    begin_record,
    coverage_fields,
    fail_component,
    is_bot_actor,
    run_banded_collection,
    sweep_updated,
)
from ai_policy_impact.config import HorizonConfig, StudyConfig
from ai_policy_impact.github import APIError, GitHubClient, utc_now
from ai_policy_impact.runs import write_csv
from ai_policy_impact.windows import BANDS, Window, parse_time

PAGE_SIZE = 25
MAX_PAGES = 4000
# The earliest and latest commits of each PR; the middle of longer PRs is skipped.
COMMITS_PER_END = 50
SUCCESS = {"SUCCESS"}
FAILURE = {"FAILURE", "ERROR"}

COMMITS = """{ totalCount nodes { commit { oid committedDate
  statusCheckRollup { state } } } }"""
QUERY = """query($owner:String!,$name:String!,$first:Int!,$cursor:String) {
  rateLimit { cost remaining resetAt }
  repository(owner:$owner,name:$name) {
    pullRequests(first:$first,after:$cursor,orderBy:{field:UPDATED_AT,direction:DESC}) {
      nodes { number createdAt updatedAt author { __typename login }
        early: commits(first:%(n)d) %(c)s
        late: commits(last:%(n)d) %(c)s
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}""" % {"n": COMMITS_PER_END, "c": COMMITS}


def lifecycle(node: dict) -> dict:
    """Per-PR CI facts from its commits' combined results, oldest first."""
    commits: dict[str, dict] = {}
    total = 0
    for part in ("early", "late"):
        connection = node.get(part) or {}
        total = max(total, connection.get("totalCount") or 0)
        for entry in connection.get("nodes") or []:
            commit = entry.get("commit") or {}
            commits[commit["oid"]] = commit
    ordered = sorted(commits.values(), key=lambda c: c.get("committedDate") or "")
    states = [
        state
        for c in ordered
        if (state := (c.get("statusCheckRollup") or {}).get("state"))
    ]
    failures = sum(s in FAILURE for s in states)
    final = states[-1] if states else None
    return {
        "commits": total,
        "commits_truncated": total > len(commits),
        "ci_results": len(states),
        "ci_failures": failures,
        "first_ci_state": states[0] if states else None,
        "final_ci_state": final,
        "ci_fixed": failures > 0 and final in SUCCESS,
    }


def pull_ci_row(node: dict, window: Window) -> dict | None:
    created = parse_time(node["createdAt"])
    band = window.band(created)
    if band is None or created < window.start_time:
        return None
    author = node.get("author") or {}
    return {
        "number": node["number"],
        "author": author.get("login"),
        "author_is_bot": is_bot_actor(author),
        "created_at": node["createdAt"],
        "created_band": band,
        **lifecycle(node),
    }


def summarize_ci(rows: list[dict], window: Window) -> dict:
    humans = [r for r in rows if not r["author_is_bot"] and r["ci_results"]]
    summary = {
        "prs_in_window": len(rows),
        "prs_with_ci": sum(bool(r["ci_results"]) for r in rows),
        "prs_commits_truncated": sum(r["commits_truncated"] for r in rows),
        **coverage_fields(window),
    }
    for name in BANDS:
        band = [r for r in humans if r["created_band"] == name]
        observed = window.observed(name)
        summary[f"{name}_prs_with_ci"] = len(band) if observed else None
        summary[f"{name}_prs_first_ci_failed"] = (
            sum(r["first_ci_state"] in FAILURE for r in band) if observed else None
        )
        summary[f"{name}_prs_ci_fixed"] = (
            sum(r["ci_fixed"] for r in band) if observed else None
        )
    return summary


def collect_repository_ci(
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
    try:
        nodes = sweep_updated(
            client,
            QUERY,
            {"owner": owner, "name": name},
            lambda data: (data.get("repository") or {}).get("pullRequests"),
            window.start_time,
            max_pages=MAX_PAGES,
            page_size=PAGE_SIZE,
            sleep=sleep,
        )
    except APIError as error:
        return fail_component(record, "pull_request_ci", error)
    rows = [row for node in nodes.values() if (row := pull_ci_row(node, window))]
    record["pulls"] = sorted(rows, key=lambda r: r["number"])
    record.update(summarize_ci(rows, window))
    record["completed_at"] = utc_now()
    return record


SUMMARY_COLUMNS = [
    "requested_repository",
    "repository",
    "status",
    "anchor_date",
    "anchor_source",
    "window_start",
    "window_end",
    "prs_in_window",
    "prs_with_ci",
    "prs_commits_truncated",
    *[
        f"{b}_{m}"
        for b in BANDS
        for m in ("days_observed", "prs_with_ci", "prs_first_ci_failed", "prs_ci_fixed")
    ],
]
PULL_COLUMNS = [
    "repository",
    "number",
    "author",
    "author_is_bot",
    "created_at",
    "created_band",
    "commits",
    "commits_truncated",
    "ci_results",
    "ci_failures",
    "first_ci_state",
    "final_ci_state",
    "ci_fixed",
]


def write_outputs(interim: Path, records: list[dict]) -> None:
    write_csv(interim / "summary.csv", SUMMARY_COLUMNS, records)
    write_csv(
        interim / "pull_request_ci.csv",
        PULL_COLUMNS,
        [
            {"repository": r.get("repository", r["requested_repository"]), **row}
            for r in records
            for row in r.get("pulls", [])
        ],
    )


def collect_ci(
    study: StudyConfig, candidates_path: Path, **options
) -> tuple[Path, dict]:
    return run_banded_collection(
        study,
        candidates_path,
        kind="ci",
        measurement="pull_request_ci_lifecycle_around_unverified_anchor",
        collector=collect_repository_ci,
        write_outputs=write_outputs,
        describe=lambda r: (
            f"PRs: {r.get('prs_in_window')}; with CI: {r.get('prs_with_ci')}; "
            f"truncated: {r.get('prs_commits_truncated')}"
        ),
        **options,
    )
