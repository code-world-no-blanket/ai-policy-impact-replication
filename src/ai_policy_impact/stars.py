"""Collect dated star histories from GitHub's weekly stargazer history endpoint."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ai_policy_impact.config import StudyConfig
from ai_policy_impact.github import APIError, GitHubClient, utc_now
from ai_policy_impact.runs import guarded, select_candidates, start_run, write_csv

# The endpoint returns at most 30 weeks per page and at most 100 pages.
WEEKS_PER_PAGE = 30
MAX_PAGES = 100


def week_start(timestamp: int) -> date:
    return datetime.fromtimestamp(timestamp, timezone.utc).date()


def validate_weeks(weeks: list) -> list[str]:
    """Return problems with a newest-first weekly history; empty means valid."""
    problems = []
    for index, entry in enumerate(weeks):
        if not (
            isinstance(entry, dict)
            and isinstance(entry.get("week"), int)
            and isinstance(entry.get("total"), int)
            and isinstance(entry.get("days"), list)
            and len(entry["days"]) == 7
            and all(isinstance(d, int) and d >= 0 for d in entry["days"])
        ):
            problems.append(f"entry {index} is malformed")
            continue
        start = datetime.fromtimestamp(entry["week"], timezone.utc)
        if start.weekday() != 6 or start.time() != datetime.min.time():
            problems.append(f"week {start.isoformat()} is not a Sunday 00:00 UTC")
        if sum(entry["days"]) != entry["total"]:
            problems.append(f"week {start.date()} days do not sum to its total")
    if problems:
        return problems
    for newer, older in zip(weeks, weeks[1:]):
        gap = newer["week"] - older["week"]
        if gap != 7 * 86400:
            # Empty weeks are returned as zeros, so a gap is missing data, not zero.
            problems.append(
                f"weeks {week_start(older['week'])} and {week_start(newer['week'])} "
                "are not consecutive"
            )
    return problems


def history_rows(weeks: list[dict], first_day: date, last_day: date) -> tuple:
    """Oldest-first weekly and daily rows with new and cumulative stars.

    Daily rows are limited to first_day..last_day so days before creation or
    after retrieval are not reported as observed zeros.
    """
    weekly, daily = [], []
    cumulative = 0
    for entry in sorted(weeks, key=lambda e: e["week"]):
        start = week_start(entry["week"])
        for offset, count in enumerate(entry["days"]):
            day = start + timedelta(days=offset)
            cumulative += count
            if first_day <= day <= last_day:
                daily.append(
                    {
                        "date": day.isoformat(),
                        "new_stars": count,
                        "cumulative_stars": cumulative,
                    }
                )
        weekly.append(
            {
                "week_start": start.isoformat(),
                "new_stars": entry["total"],
                "cumulative_stars": cumulative,
            }
        )
    return weekly, daily


def collect_star_history(client: GitHubClient, candidate: dict) -> dict:
    repo = candidate["repository"]
    record = {
        "requested_repository": repo,
        "started_at": utc_now(),
        "status": "complete",
        "errors": [],
    }

    def fail(component: str, error: APIError) -> dict:
        record["errors"].append(
            {
                "component": component,
                "status": error.status,
                "error": str(error),
                "raw_file": error.raw_file,
            }
        )
        record.update(status="failed", completed_at=utc_now())
        return record

    try:
        details = client.get(f"/repos/{repo}")
    except APIError as error:
        return fail("metadata", error)
    if details.data.get("private"):
        record.update(status="skipped_private", completed_at=utc_now())
        return record
    repo = details.data["full_name"]
    record.update(
        repository=repo,
        repository_id=details.data["id"],
        created_at=details.data["created_at"],
        stars_before=details.data["stargazers_count"],
        stars_before_retrieved_at=details.retrieved_at,
    )

    weeks, raw_files = [], []
    for page in range(1, MAX_PAGES + 1):
        try:
            response = client.get(
                f"/repos/{repo}/stargazers/history",
                per_page=WEEKS_PER_PAGE,
                page=page,
            )
        except APIError as error:
            return fail("history", error)
        raw_files.append(response.raw_file)
        if not isinstance(response.data, list):
            record["errors"].append(
                {"component": "history", "error": f"page {page} is not a list"}
            )
            record.update(status="failed", completed_at=utc_now())
            return record
        weeks.extend(response.data)
        if len(response.data) < WEEKS_PER_PAGE:
            break
    else:
        record["errors"].append(
            {"component": "history", "error": f"stopped at the {MAX_PAGES}-page limit"}
        )
    record.update(history_pages=len(raw_files), history_raw_responses=raw_files)

    try:
        count = client.get(f"/repos/{repo}/stargazers/count")
        record.update(
            stars_after=count.data["count"], stars_after_retrieved_at=count.retrieved_at
        )
    except APIError as error:
        record["errors"].append(
            {
                "component": "count",
                "status": error.status,
                "error": str(error),
                "raw_file": error.raw_file,
            }
        )

    problems = validate_weeks(weeks)
    if problems:
        record["errors"].extend(
            {"component": "validation", "error": p} for p in problems
        )
        record.update(status="failed", completed_at=utc_now())
        return record

    total = sum(entry["total"] for entry in weeks)
    record["history_total"] = total
    after = record.get("stars_after")
    if after is None:
        record["reconciliation"] = "count_unavailable"
    elif record["stars_before"] != after:
        # Stars changed while pages were fetched; neither count pins the history.
        record["reconciliation"] = "changed_during_run"
    else:
        record["reconciliation"] = "matched" if total == after else "mismatch"
    created = datetime.fromisoformat(record["created_at"].replace("Z", "+00:00"))
    retrieved = datetime.fromisoformat(record["stars_before_retrieved_at"])
    weekly, daily = history_rows(weeks, created.date(), retrieved.date())
    record.update(
        weeks=len(weekly),
        first_week=weekly[0]["week_start"] if weekly else None,
        last_week=weekly[-1]["week_start"] if weekly else None,
        weekly=weekly,
        daily=daily,
    )
    if record["errors"] or record["reconciliation"] != "matched":
        record["status"] = "partial"
    record["completed_at"] = utc_now()
    return record


SUMMARY_COLUMNS = [
    "requested_repository",
    "repository",
    "repository_id",
    "status",
    "created_at",
    "stars_before",
    "stars_before_retrieved_at",
    "stars_after",
    "stars_after_retrieved_at",
    "history_total",
    "reconciliation",
    "weeks",
    "first_week",
    "last_week",
    "history_pages",
]


def write_outputs(interim: Path, records: list[dict]) -> None:
    write_csv(interim / "summary.csv", SUMMARY_COLUMNS, records)
    for series, key in (("weekly", "week_start"), ("daily", "date")):
        rows = [
            {"repository": r["repository"], **row}
            for r in records
            for row in r.get(series, [])
        ]
        write_csv(
            interim / f"{series}.csv",
            ["repository", key, "new_stars", "cumulative_stars"],
            rows,
        )


def collect_star_histories(
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
        "star_history",
        candidates_path,
        candidates,
        measurement="surviving_stars_by_star_date",
    )
    for index, candidate in enumerate(candidates, 1):
        progress(f"[{index}/{len(candidates)}] {candidate['repository']}")
        record = guarded(collect_star_history, candidate, run.client, candidate)
        record.setdefault("repository", candidate["repository"])
        run.save_record(candidate, record)
        # Persist each completed repository even if a later request is interrupted.
        write_outputs(run.interim, run.records)
        progress(
            f"  {record['status']}; stars: {record.get('stars_after', record.get('stars_before'))}; "
            f"reconciliation: {record.get('reconciliation', 'not_computed')}"
        )
    return run.interim, run.finish()
