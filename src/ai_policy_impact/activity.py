"""Shared machinery for the outcome collectors (pulls, issues, releases, churn).

Each collector reads one repository's activity across the observed window around
its (unverified) anchor date and summarizes it by band. Event-level rows are kept
so later definitions can be applied without recollecting.
"""

from __future__ import annotations

import re
import statistics
import time
from collections.abc import Callable, Iterator
from datetime import date, datetime, timezone
from pathlib import Path

from ai_policy_impact.config import HorizonConfig, StudyConfig
from ai_policy_impact.github import APIError, GitHubClient, utc_now
from ai_policy_impact.runs import guarded, select_candidates, start_run
from ai_policy_impact.windows import (
    BANDS,
    Window,
    anchor_dates,
    parse_time,
    read_detected_dates,
    require_policy_run,
)

# Wait for the GraphQL rate-limit reset when fewer points than this remain.
RATE_LIMIT_FLOOR = 100
MAX_RATE_LIMIT_WAIT_SECONDS = 3700
# Statuses GitHub returns when a GraphQL page is too expensive to finish in time.
TIMEOUT_STATUSES = {502, 504}
MIN_PAGE_SIZE = 5

Collector = Callable[[GitHubClient, dict, date | None, str, HorizonConfig, date], dict]


def begin_record(
    client: GitHubClient, candidate: dict, anchor: date | None, anchor_source: str
) -> tuple[dict, dict | None]:
    """Common record fields plus repository metadata; metadata is None to stop."""
    record = {
        "requested_repository": candidate["repository"],
        "reported_policy_date": candidate.get("reported_policy_date") or None,
        "anchor_date": anchor.isoformat() if anchor else None,
        "anchor_source": anchor_source,
        "started_at": utc_now(),
        "status": "complete",
        "errors": [],
    }
    try:
        details = client.get(f"/repos/{candidate['repository']}").data
    except APIError as error:
        record["errors"].append({"component": "metadata", "error": str(error)})
        record.update(status="failed", completed_at=utc_now())
        return record, None
    if details.get("private"):
        record.update(status="skipped_private", completed_at=utc_now())
        return record, None
    record.update(
        repository=details["full_name"], default_branch=details["default_branch"]
    )
    if anchor is None:
        record["errors"].append({"component": "window", "error": "no anchor date"})
        record.update(status="partial", completed_at=utc_now())
        return record, None
    return record, details


def fail_component(record: dict, component: str, error: Exception) -> dict:
    record["errors"].append({"component": component, "error": str(error)})
    record.update(status="partial", completed_at=utc_now())
    return record


def pace(data: dict, sleep=time.sleep) -> float:
    """Sleep until the reset when the GraphQL budget is nearly spent."""
    limit = data.get("rateLimit") or {}
    remaining = limit.get("remaining")
    if remaining is None or remaining >= max(
        RATE_LIMIT_FLOOR, 2 * limit.get("cost", 1)
    ):
        return 0.0
    reset = parse_time(limit.get("resetAt"))
    if reset is None:
        return 0.0
    wait = (reset - datetime.now(timezone.utc)).total_seconds() + 5
    if wait > MAX_RATE_LIMIT_WAIT_SECONDS:
        raise APIError(200, f"Rate limit resets in {wait:.0f}s; stopping")
    if wait > 0:
        sleep(wait)
        return wait
    return 0.0


def is_timeout(error: APIError) -> bool:
    """GitHub's answer when a GraphQL page took too long or too much to compute."""
    message = str(error).lower()
    return error.status in TIMEOUT_STATUSES or (
        error.status == 200
        and ("timeout" in message or "resource limits for this query" in message)
    )


def graphql_pages(
    client: GitHubClient,
    query: str,
    variables: dict,
    locate: Callable[[dict], dict],
    *,
    max_pages: int,
    page_size: int | None = None,
    skip: Callable[[str], str] | None = None,
    unreadable: Callable[[APIError], bool] | None = None,
    sleep=time.sleep,
) -> Iterator[dict]:
    """Yield connections page by page, pacing on the rate limit and checking cursors.

    With `page_size`, the query takes a `$first` variable; a page that times out is
    retried at the same cursor with half as many nodes, down to MIN_PAGE_SIZE.
    With `skip` as well, pages shrink to a single node, and a node that still times
    out is stepped over: `skip` turns the cursor before it into the cursor after it.
    Page size then grows back after each page read. `unreadable` (default: a
    timeout) says which errors shrink the page and, at one node, skip it.
    """
    unreadable = unreadable or is_timeout
    cursor = None
    seen: set[str] = set()
    pages = 0
    full = page_size
    floor = 1 if skip else MIN_PAGE_SIZE
    while pages < max_pages:
        sized = {"first": page_size} if page_size else {}
        try:
            data = client.graphql(query, {**variables, **sized, "cursor": cursor})
        except APIError as error:
            if page_size and page_size > floor and unreadable(error):
                page_size = max(floor, page_size // 2)
                continue
            if skip and cursor and page_size == 1 and unreadable(error):
                cursor = skip(cursor)
                continue
            raise
        if skip and page_size and full:
            page_size = min(full, page_size * 2)
        pages += 1
        connection = locate(data)
        if connection is None:
            raise APIError(200, "Missing GraphQL connection in response")
        pace(data, sleep)
        yield connection
        info = connection["pageInfo"]
        if not info["hasNextPage"]:
            return
        cursor = info["endCursor"]
        if not cursor or cursor in seen:
            raise APIError(200, "Invalid or repeated pagination cursor")
        seen.add(cursor)
    raise APIError(200, f"Stopped at {max_pages} pages; results are incomplete")


def sweep_updated(
    client: GitHubClient,
    query: str,
    variables: dict,
    locate: Callable[[dict], dict],
    since: datetime,
    *,
    max_pages: int,
    page_size: int | None = None,
    sleep=time.sleep,
) -> dict[int, dict]:
    """Every node updated at or after `since`, from a list ordered by UPDATED_AT DESC.

    Anything created, merged, or closed in the window was updated in it, so the
    sweep stops at the first older node. An item updated during the sweep jumps to
    the head of the list, possibly past the cursor, so a second pass re-reads the
    head down to the sweep's start time; later versions replace earlier ones.
    """
    nodes: dict[int, dict] = {}
    started = datetime.now(timezone.utc)
    for floor in (since, started):
        for connection in graphql_pages(
            client,
            query,
            variables,
            locate,
            max_pages=max_pages,
            page_size=page_size,
            sleep=sleep,
        ):
            older = False
            for node in connection["nodes"]:
                if parse_time(node["updatedAt"]) < floor:
                    older = True
                    break
                nodes[node["number"]] = node
            if older:
                break
    return nodes


def is_bot_login(login: str | None) -> bool:
    """A login named as an automated account: GitHub's `[bot]` suffix, or a part
    ending in "bot" (qiskit-bot, macportsbot, JuliaTagBot) but not "robot"."""
    login = (login or "").lower()
    if login.endswith("[bot]"):
        return True
    parts = re.split(r"[^a-z]+", login)
    return any(p.endswith("bot") and not p.endswith("robot") for p in parts)


def is_bot_actor(actor: dict | None) -> bool:
    if not actor:
        return False
    return actor.get("__typename") == "Bot" or is_bot_login(actor.get("login"))


def hours_between(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return round((end - start).total_seconds() / 3600, 3)


def median(values: list[float]) -> float | None:
    return round(statistics.median(values), 3) if values else None


def band_counts(
    rows: list[dict], window: Window, column: str, prefix: str
) -> dict[str, int | None]:
    """Rows whose `column` band is each band; an unobserved band is missing, not zero."""
    return {
        f"{name}_{prefix}": (
            sum(r[column] == name for r in rows) if window.observed(name) else None
        )
        for name in BANDS
    }


def coverage_fields(window: Window) -> dict:
    return {
        "window_start": window.start.isoformat(),
        "window_end": window.end.isoformat(),
        **{f"{name}_days_observed": window.coverage[name] for name in BANDS},
    }


def run_banded_collection(
    study: StudyConfig,
    candidates_path: Path,
    *,
    kind: str,
    measurement: str,
    collector: Collector,
    write_outputs: Callable[[Path, list[dict]], None],
    describe: Callable[[dict], str],
    policy_run: Path | None = None,
    priority: str | None = None,
    limit: int | None = None,
    progress=print,
    **manifest_fields,
) -> tuple[Path, dict]:
    event = study.treatment.anchor_event
    require_policy_run(event, policy_run)
    candidates = select_candidates(candidates_path, priority, limit)
    detected = read_detected_dates(policy_run)
    run = start_run(
        study,
        kind,
        candidates_path,
        candidates,
        measurement=measurement,
        policy_run=str(policy_run) if policy_run else None,
        anchor_event=event.value,
        horizons=study.horizons.model_dump(),
        **manifest_fields,
    )
    today = datetime.now(timezone.utc).date()
    for index, candidate in enumerate(candidates, 1):
        progress(f"[{index}/{len(candidates)}] {candidate['repository']}")
        anchor, source = anchor_dates(candidate, detected, event)
        record = guarded(
            collector,
            candidate,
            run.client,
            candidate,
            anchor,
            source,
            study.horizons,
            today,
        )
        run.save_record(candidate, record)
        write_outputs(run.interim, run.records)
        progress(
            f"  {record['status']}; anchor: {record['anchor_date']} ({source}); "
            + describe(record)
        )
    return run.interim, run.finish()
