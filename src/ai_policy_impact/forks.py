"""Dated fork histories: each surviving public fork by the day it was created.

Like star history, deleted forks are no longer listed, so past counts are
surviving forks only. The listed total is reconciled against the repository's
forks_count, which can differ (it counts forks GitHub no longer lists).
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

from ai_policy_impact.config import StudyConfig
from ai_policy_impact.github import APIError, GitHubClient, utc_now
from ai_policy_impact.runs import (
    guarded,
    paginate,
    select_candidates,
    start_run,
    write_csv,
)

MAX_PAGES = 2000


def daily_rows(created: list[str], first_day: date, last_day: date) -> list[dict]:
    """Oldest-first days from repository creation to retrieval with new forks."""
    per_day = Counter(
        datetime.fromisoformat(c.replace("Z", "+00:00")).date() for c in created
    )
    rows, cumulative, day = [], 0, first_day
    while day <= last_day:
        cumulative += per_day.get(day, 0)
        rows.append(
            {
                "date": day.isoformat(),
                "new_forks": per_day.get(day, 0),
                "cumulative_forks": cumulative,
            }
        )
        day += timedelta(days=1)
    return rows


def collect_fork_history(client: GitHubClient, candidate: dict) -> dict:
    record = {
        "requested_repository": candidate["repository"],
        "started_at": utc_now(),
        "status": "complete",
        "errors": [],
    }
    try:
        details = client.get(f"/repos/{candidate['repository']}")
    except APIError as error:
        record["errors"].append({"component": "metadata", "error": str(error)})
        record.update(status="failed", completed_at=utc_now())
        return record
    if details.data.get("private"):
        record.update(status="skipped_private", completed_at=utc_now())
        return record
    repo = details.data["full_name"]
    record.update(
        repository=repo,
        created_at=details.data["created_at"],
        forks_count=details.data["forks_count"],
        forks_retrieved_at=details.retrieved_at,
    )
    try:
        forks, finished = paginate(
            client, f"/repos/{repo}/forks", max_pages=MAX_PAGES, sort="oldest"
        )
    except APIError as error:
        record["errors"].append({"component": "forks", "error": str(error)})
        record.update(status="failed", completed_at=utc_now())
        return record
    if not finished:
        record["errors"].append(
            {"component": "forks", "error": f"stopped at the {MAX_PAGES}-page limit"}
        )
        record.update(status="failed", completed_at=utc_now())
        return record
    created = [f["created_at"] for f in forks if f.get("created_at")]
    record["listed_forks"] = len(created)
    record["reconciliation"] = (
        "matched" if len(created) == record["forks_count"] else "mismatch"
    )
    first_day = datetime.fromisoformat(record["created_at"].replace("Z", "+00:00"))
    last_day = datetime.fromisoformat(record["forks_retrieved_at"])
    # Only days with new forks are kept; the cumulative count carries forward.
    record["daily"] = [
        row
        for row in daily_rows(created, first_day.date(), last_day.date())
        if row["new_forks"]
    ]
    record["completed_at"] = utc_now()
    return record


SUMMARY_COLUMNS = [
    "requested_repository",
    "repository",
    "status",
    "created_at",
    "forks_count",
    "listed_forks",
    "reconciliation",
    "forks_retrieved_at",
]


def write_outputs(interim: Path, records: list[dict]) -> None:
    write_csv(interim / "summary.csv", SUMMARY_COLUMNS, records)
    write_csv(
        interim / "daily.csv",
        ["repository", "date", "new_forks", "cumulative_forks"],
        [
            {"repository": r["repository"], **row}
            for r in records
            for row in r.get("daily", [])
        ],
    )


def collect_fork_histories(
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
        "fork_history",
        candidates_path,
        candidates,
        measurement="surviving_public_forks_by_fork_date",
    )
    for index, candidate in enumerate(candidates, 1):
        progress(f"[{index}/{len(candidates)}] {candidate['repository']}")
        record = guarded(collect_fork_history, candidate, run.client, candidate)
        record.setdefault("repository", candidate["repository"])
        run.save_record(candidate, record)
        write_outputs(run.interim, run.records)
        progress(
            f"  {record['status']}; forks listed: {record.get('listed_forks')} "
            f"of {record.get('forks_count')}"
        )
    return run.interim, run.finish()
