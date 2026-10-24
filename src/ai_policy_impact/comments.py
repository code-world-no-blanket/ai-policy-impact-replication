"""Comments on pull requests and issues, by time posted, around a policy date.

Two repository-wide REST listings cover every comment: conversation comments on
issues and pull requests, and review comments on pull-request diffs. Only who
commented, when, and on which item are kept; comment text is not.
"""

from __future__ import annotations

import re
import time
from datetime import date
from pathlib import Path

from ai_policy_impact.activity import (
    begin_record,
    coverage_fields,
    fail_component,
    is_bot_actor,
    run_banded_collection,
)
from ai_policy_impact.config import HorizonConfig, StudyConfig
from ai_policy_impact.github import APIError, GitHubClient, utc_now
from ai_policy_impact.runs import write_csv
from ai_policy_impact.windows import BANDS, Window, parse_time

MAX_PAGES = 5000
PER_PAGE = 100
SMALL_PAGE = 30
# The item a comment belongs to, from its API or web URL.
ITEM_NUMBER = re.compile(r"/(?:issues|pulls?)/(\d+)(?:#|$)")
LISTINGS = {
    # `since` filters on the last update, so older comments edited later are
    # also returned; they are dropped by their creation time below.
    "conversation": "/repos/{repo}/issues/comments",
    "review": "/repos/{repo}/pulls/comments",
}


def list_comments(client: GitHubClient, endpoint: str, since: str) -> tuple[list, bool]:
    """Every comment updated since `since`, oldest created first.

    Two GitHub limits on busy repositories are worked around by restarting the
    listing from the newest creation time seen: pages past a fixed depth are
    refused (HTTP 422, "pagination is limited"), and a full page can time out on
    GitHub's side (HTTP 502/504, still failing after the client's retries), which
    is retried once with SMALL_PAGE comments a page. Comments at the restart
    timestamp come back twice; the caller removes repeats. The flag is False when
    MAX_PAGES cut the listing off.
    """
    items: list = []
    start = since
    page, per_page = 1, PER_PAGE
    for _ in range(MAX_PAGES):
        try:
            response = client.get(
                endpoint,
                per_page=per_page,
                page=page,
                since=since,
                sort="created",
                direction="asc",
            )
        except APIError as error:
            newest = max((item["created_at"] for item in items), default=since)
            if error.status in {500, 502, 504} and per_page > SMALL_PAGE:
                since, page, per_page = newest, 1, SMALL_PAGE
                continue
            if error.status in {500, 502, 504} and page == 1:
                # The `since` filter itself is too slow on the largest repositories.
                newer, finished = list_newest_first(client, endpoint, start)
                return items + newer, finished
            if error.status != 422 or page == 1 or newest <= since:
                raise
            since, page = newest, 1
            continue
        if not isinstance(response.data, list):
            raise APIError(200, f"Expected a list from {endpoint}", response.raw_file)
        items.extend(response.data)
        if len(response.data) < per_page:
            return items, True
        page += 1
    return items, False


def list_newest_first(
    client: GitHubClient, endpoint: str, since: str
) -> tuple[list, bool]:
    """Every comment created since `since`, newest first and without a `since`
    filter, stopping at the first older page. The flag is False when MAX_PAGES
    cut the listing off.
    """
    items: list = []
    for page in range(1, MAX_PAGES + 1):
        response = client.get(
            endpoint, per_page=PER_PAGE, page=page, sort="created", direction="desc"
        )
        if not isinstance(response.data, list):
            raise APIError(200, f"Expected a list from {endpoint}", response.raw_file)
        items.extend(response.data)
        if len(response.data) < PER_PAGE or (
            response.data and parse_time(response.data[-1]["created_at"])
            < parse_time(since)
        ):
            return items, True
    return items, False


def comment_row(item: dict, kind: str, window: Window) -> dict | None:
    created = parse_time(item.get("created_at"))
    band = window.band(created)
    if band is None or created < window.start_time:
        return None
    user = item.get("user") or {}
    actor = {
        "__typename": "Bot" if user.get("type") == "Bot" else "User",
        "login": user.get("login"),
    }
    if kind == "review":
        url, on_pull = item.get("pull_request_url") or "", True
    else:
        url = item.get("issue_url") or ""
        on_pull = "/pull/" in (item.get("html_url") or "")
    found = ITEM_NUMBER.search(url)
    return {
        "id": item.get("id"),
        "kind": kind,
        "number": int(found.group(1)) if found else None,
        "on_pull_request": on_pull,
        "author": user.get("login"),
        "author_is_bot": is_bot_actor(actor) if user else False,
        "created_at": item["created_at"],
        "band": band,
    }


def summarize_comments(rows: list[dict], window: Window) -> dict:
    humans = [r for r in rows if not r["author_is_bot"]]
    summary = {
        "comments_in_window": len(rows),
        "bot_comments_in_window": len(rows) - len(humans),
        **coverage_fields(window),
    }
    for name in BANDS:
        band = [r for r in humans if r["band"] == name]
        observed = window.observed(name)
        summary[f"{name}_pr_comments"] = (
            sum(r["on_pull_request"] for r in band) if observed else None
        )
        summary[f"{name}_issue_comments"] = (
            sum(not r["on_pull_request"] for r in band) if observed else None
        )
        summary[f"{name}_commenters"] = (
            len({(r["author"] or "").lower() for r in band}) if observed else None
        )
    return summary


def collect_repository_comments(
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
    rows: list[dict] = []
    for kind, endpoint in LISTINGS.items():
        try:
            items, finished = list_comments(
                client,
                endpoint.format(repo=record["repository"]),
                window.start_time.isoformat().replace("+00:00", "Z"),
            )
        except APIError as error:
            return fail_component(record, f"{kind}_comments", error)
        if not finished:
            return fail_component(
                record,
                f"{kind}_comments",
                APIError(200, f"Stopped at {MAX_PAGES} pages; results are incomplete"),
            )
        rows += [row for item in items if (row := comment_row(item, kind, window))]
    # The listings can repeat a comment edited while pages were fetched, and
    # repeat comments at the timestamp a listing restarted from.
    unique = {(r["kind"], r["id"]): r for r in rows}
    record["comments"] = sorted(unique.values(), key=lambda r: r["created_at"])
    record.update(summarize_comments(record["comments"], window))
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
    "comments_in_window",
    "bot_comments_in_window",
    *[
        f"{b}_{m}"
        for b in BANDS
        for m in ("days_observed", "pr_comments", "issue_comments", "commenters")
    ],
]
COMMENT_COLUMNS = [
    "repository",
    "id",
    "kind",
    "number",
    "on_pull_request",
    "author",
    "author_is_bot",
    "created_at",
    "band",
]


def write_outputs(interim: Path, records: list[dict]) -> None:
    write_csv(interim / "summary.csv", SUMMARY_COLUMNS, records)
    write_csv(
        interim / "comments.csv",
        COMMENT_COLUMNS,
        [
            {"repository": r.get("repository", r["requested_repository"]), **row}
            for r in records
            for row in r.get("comments", [])
        ],
    )


def collect_comments(
    study: StudyConfig, candidates_path: Path, **options
) -> tuple[Path, dict]:
    return run_banded_collection(
        study,
        candidates_path,
        kind="comments",
        measurement="pr_and_issue_comments_around_unverified_anchor",
        collector=collect_repository_comments,
        write_outputs=write_outputs,
        describe=lambda r: (
            f"comments in window: {r.get('comments_in_window')}; "
            f"bots: {r.get('bot_comments_in_window')}"
        ),
        **options,
    )
