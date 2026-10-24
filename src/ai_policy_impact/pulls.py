"""Pull requests, their reviews, and their size, in bands around a policy date."""

from __future__ import annotations

import time
from datetime import date
from functools import partial
from pathlib import Path

from ai_policy_impact.activity import (
    begin_record,
    band_counts,
    coverage_fields,
    fail_component,
    graphql_pages,
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

PAGE_SIZE = 50
REVIEWS_PER_PR = 30
MAX_PAGES = 2000

QUERY_TEMPLATE = """query($owner:String!,$name:String!,$first:Int!,$cursor:String,
$withReviews:Boolean!) {
  rateLimit { cost remaining resetAt }
  repository(owner:$owner,name:$name) {
    pullRequests(first:$first,after:$cursor,%(states)s
      orderBy:{field:UPDATED_AT,direction:DESC}) {
      nodes { number createdAt updatedAt closedAt mergedAt isDraft state
        baseRefName authorAssociation author { __typename login }
        mergedBy { __typename login }
        additions deletions changedFiles
        commits { totalCount } comments { totalCount }
        headCommit: commits(last:1) { nodes { commit { statusCheckRollup { state } } } }
        reviews(first:%(reviews)d) @include(if:$withReviews) {
          totalCount nodes { author { __typename login } state submittedAt }
        }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}"""
QUERY = QUERY_TEMPLATE % {"states": "", "reviews": REVIEWS_PER_PR}
# PRs still open now. One opened before the window and untouched since is open
# throughout it, but the updated-since sweep never reaches it; the backlog needs it.
OPEN_QUERY = QUERY_TEMPLATE % {"states": "states:[OPEN],", "reviews": REVIEWS_PER_PR}
# The combined result of a commit's checks and statuses, as GitHub reports it now.
CI_SUCCESS = {"SUCCESS"}
CI_FAILURE = {"FAILURE", "ERROR"}


def ci_state(commit: dict | None) -> str | None:
    """SUCCESS, FAILURE, ERROR, PENDING or EXPECTED; None when no CI reported."""
    return ((commit or {}).get("statusCheckRollup") or {}).get("state")


def head_ci_state(node: dict) -> str | None:
    heads = (node.get("headCommit") or {}).get("nodes") or []
    return ci_state(heads[0].get("commit")) if heads else None


def pull_row(node: dict, window: Window, default_branch: str) -> dict | None:
    """One PR's timestamps, size, and review facts; None when outside the window."""
    created = parse_time(node["createdAt"])
    merged = parse_time(node.get("mergedAt"))
    closed = parse_time(node.get("closedAt"))
    unmerged_close = closed if merged is None else None
    bands = {
        "created_band": window.band(created),
        "merged_band": window.band(merged),
        "closed_unmerged_band": window.band(unmerged_close),
    }
    # Open at some point in the window: needed for the open-PR backlog.
    open_in_window = created < window.cutoff and (
        closed is None or closed >= window.start_time
    )
    if not any(bands.values()) and not open_in_window:
        return None
    author = node.get("author") or {}
    merger = node.get("mergedBy") or {}
    row = {
        "number": node["number"],
        "author": author.get("login"),
        "author_is_bot": is_bot_actor(author),
        "author_association": node.get("authorAssociation"),
        "base_branch": node.get("baseRefName"),
        "targets_default_branch": node.get("baseRefName") == default_branch,
        "is_draft": node.get("isDraft"),
        "state": node.get("state"),
        "created_at": node["createdAt"],
        "merged_at": node.get("mergedAt"),
        "closed_at": node.get("closedAt"),
        **bands,
        # Resolution after the cutoff was not observable for this window.
        "hours_to_merge": hours_between(created, merged)
        if merged and merged < window.cutoff
        else None,
        "hours_to_close_unmerged": hours_between(created, unmerged_close)
        if unmerged_close and unmerged_close < window.cutoff
        else None,
        "open_at_cutoff": (closed is None or closed >= window.cutoff),
        "additions": node.get("additions"),
        "deletions": node.get("deletions"),
        "changed_files": node.get("changedFiles"),
        "commits": (node.get("commits") or {}).get("totalCount"),
        "comments": (node.get("comments") or {}).get("totalCount"),
        "merged_by": merger.get("login"),
        "merged_by_is_bot": is_bot_actor(merger) if merger else None,
        "ci_state": head_ci_state(node),
    }
    reviews = node.get("reviews")
    if reviews is not None:
        row.update(review_facts(reviews, author.get("login"), created, window))
    return row


def review_facts(reviews: dict, author: str | None, created, window: Window) -> dict:
    """Counts over reviews by people other than the PR author; bots excluded."""
    others = [
        r
        for r in reviews["nodes"]
        if r.get("submittedAt")
        and not is_bot_actor(r.get("author"))
        and (r.get("author") or {}).get("login") != author
        and parse_time(r["submittedAt"]) < window.cutoff
    ]
    first = min((parse_time(r["submittedAt"]) for r in others), default=None)
    return {
        "reviews_total": reviews["totalCount"],
        "reviews_truncated": reviews["totalCount"] > len(reviews["nodes"]),
        "reviews_by_others": len(others),
        "reviewers": len({(r.get("author") or {}).get("login") for r in others}),
        "approvals": sum(r["state"] == "APPROVED" for r in others),
        "changes_requested": sum(r["state"] == "CHANGES_REQUESTED" for r in others),
        "first_review_at": first.isoformat() if first else None,
        "hours_to_first_review": hours_between(created, first),
    }


def summarize_pulls(rows: list[dict], window: Window, with_reviews: bool) -> dict:
    humans = [r for r in rows if not r["author_is_bot"]]
    summary = {
        "prs_in_window": len(rows),
        "bot_prs_in_window": len(rows) - len(humans),
        **coverage_fields(window),
        **band_counts(humans, window, "created_band", "prs_opened"),
        **band_counts(humans, window, "merged_band", "prs_merged"),
        **band_counts(humans, window, "closed_unmerged_band", "prs_closed_unmerged"),
    }
    for name in BANDS:
        opened = [r for r in humans if r["created_band"] == name]
        merged = [r for r in humans if r["merged_band"] == name]
        observed = window.observed(name)
        summary[f"{name}_prs_open_at_cutoff"] = (
            sum(r["open_at_cutoff"] for r in opened) if observed else None
        )
        summary[f"{name}_median_hours_to_merge"] = median(
            [r["hours_to_merge"] for r in opened if r["hours_to_merge"] is not None]
        )
        summary[f"{name}_merged_additions"] = (
            sum(r["additions"] or 0 for r in merged) if observed else None
        )
        summary[f"{name}_merged_deletions"] = (
            sum(r["deletions"] or 0 for r in merged) if observed else None
        )
        if with_reviews:
            summary[f"{name}_prs_without_review"] = (
                sum(r["first_review_at"] is None for r in opened) if observed else None
            )
            summary[f"{name}_median_hours_to_first_review"] = median(
                [
                    r["hours_to_first_review"]
                    for r in opened
                    if r["hours_to_first_review"] is not None
                ]
            )
    if with_reviews:
        summary["prs_reviews_truncated"] = sum(r["reviews_truncated"] for r in rows)
    return summary


def collect_repository_pulls(
    client: GitHubClient,
    candidate: dict,
    anchor: date | None,
    anchor_source: str,
    horizons: HorizonConfig,
    today: date,
    *,
    with_reviews: bool = True,
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
            {"owner": owner, "name": name, "withReviews": with_reviews},
            lambda data: (data.get("repository") or {}).get("pullRequests"),
            window.start_time,
            max_pages=MAX_PAGES,
            page_size=PAGE_SIZE,
            sleep=sleep,
        )
        for connection in graphql_pages(
            client,
            OPEN_QUERY,
            {"owner": owner, "name": name, "withReviews": with_reviews},
            lambda data: (data.get("repository") or {}).get("pullRequests"),
            max_pages=MAX_PAGES,
            page_size=PAGE_SIZE,
            sleep=sleep,
        ):
            for node in connection["nodes"]:
                nodes.setdefault(node["number"], node)
    except APIError as error:
        # A partial sweep would undercount every band; withhold the counts.
        return fail_component(record, "pull_requests", error)
    rows = [
        row
        for node in nodes.values()
        if (row := pull_row(node, window, record["default_branch"]))
    ]
    record["pulls"] = sorted(rows, key=lambda r: r["number"])
    record["reviews_collected"] = with_reviews
    record["open_prs_collected"] = True
    record.update(summarize_pulls(rows, window, with_reviews))
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
    "prs_in_window",
    "bot_prs_in_window",
    "prs_reviews_truncated",
    *[
        f"{b}_{m}"
        for b in BANDS
        for m in (
            "days_observed",
            "prs_opened",
            "prs_merged",
            "prs_closed_unmerged",
            "prs_open_at_cutoff",
            "median_hours_to_merge",
            "merged_additions",
            "merged_deletions",
            "prs_without_review",
            "median_hours_to_first_review",
        )
    ],
]
PULL_COLUMNS = [
    "repository",
    "number",
    "author",
    "author_is_bot",
    "author_association",
    "base_branch",
    "targets_default_branch",
    "is_draft",
    "state",
    "created_at",
    "merged_at",
    "closed_at",
    "created_band",
    "merged_band",
    "closed_unmerged_band",
    "hours_to_merge",
    "hours_to_close_unmerged",
    "open_at_cutoff",
    "additions",
    "deletions",
    "changed_files",
    "commits",
    "comments",
    "reviews_total",
    "reviews_truncated",
    "reviews_by_others",
    "reviewers",
    "approvals",
    "changes_requested",
    "first_review_at",
    "hours_to_first_review",
    "merged_by",
    "merged_by_is_bot",
    "ci_state",
]


def write_outputs(interim: Path, records: list[dict]) -> None:
    write_csv(interim / "summary.csv", SUMMARY_COLUMNS, records)
    write_csv(
        interim / "pull_requests.csv",
        PULL_COLUMNS,
        [
            {"repository": r.get("repository", r["requested_repository"]), **row}
            for r in records
            for row in r.get("pulls", [])
        ],
    )


def collect_pulls(
    study: StudyConfig,
    candidates_path: Path,
    *,
    reviews: bool = True,
    **options,
) -> tuple[Path, dict]:
    return run_banded_collection(
        study,
        candidates_path,
        kind="pull_requests",
        measurement="pull_requests_around_unverified_anchor",
        collector=partial(collect_repository_pulls, with_reviews=reviews),
        write_outputs=write_outputs,
        describe=lambda r: (
            f"PRs in window: {r.get('prs_in_window')}; "
            f"bots: {r.get('bot_prs_in_window')}"
        ),
        reviews_collected=reviews,
        **options,
    )
