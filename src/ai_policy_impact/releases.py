"""Published GitHub releases in bands around a policy date."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from ai_policy_impact.activity import (
    begin_record,
    band_counts,
    coverage_fields,
    fail_component,
    run_banded_collection,
)
from ai_policy_impact.config import HorizonConfig, StudyConfig
from ai_policy_impact.github import APIError, GitHubClient, utc_now
from ai_policy_impact.runs import paginate, write_csv
from ai_policy_impact.windows import BANDS, Window, parse_time

# The full list is read so releases_total and first_release_at stay exact; a
# release per build (llama.cpp: ~7,700 by 2026-10) needs far more than 50 pages.
MAX_PAGES = 500


def summarize_releases(rows: list[dict], window: Window) -> dict:
    stable = [r for r in rows if not r["prerelease"]]
    summary = {
        **coverage_fields(window),
        **band_counts(rows, window, "band", "releases"),
        **band_counts(stable, window, "band", "stable_releases"),
    }
    for name in BANDS:
        times = sorted(r["published_at"] for r in stable if r["band"] == name)
        # Gaps need two releases in the band; fewer leaves the gap undefined.
        gaps = [
            (parse_time(b) - parse_time(a)).total_seconds() / 86400
            for a, b in zip(times, times[1:])
        ]
        summary[f"{name}_mean_days_between_stable"] = (
            round(sum(gaps) / len(gaps), 3) if gaps else None
        )
    return summary


def collect_repository_releases(
    client: GitHubClient,
    candidate: dict,
    anchor: date | None,
    anchor_source: str,
    horizons: HorizonConfig,
    today: date,
) -> dict:
    record, details = begin_record(client, candidate, anchor, anchor_source)
    if details is None:
        return record
    window = Window.around(anchor, horizons, today)
    try:
        listed, complete = paginate(
            client, f"/repos/{record['repository']}/releases", max_pages=MAX_PAGES
        )
    except APIError as error:
        return fail_component(record, "releases", error)
    if not complete:
        return fail_component(
            record, "releases", APIError(200, f"stopped at {MAX_PAGES} pages")
        )
    # Drafts are visible only with push access; an unpublished release has no date.
    published = [r for r in listed if r.get("published_at") and not r.get("draft")]
    record["releases_total"] = len(published)
    record["first_release_at"] = min(
        (r["published_at"] for r in published), default=None
    )
    rows = [
        {
            "tag_name": r.get("tag_name"),
            "name": r.get("name"),
            "prerelease": bool(r.get("prerelease")),
            "created_at": r.get("created_at"),
            "published_at": r["published_at"],
            "author": (r.get("author") or {}).get("login"),
            "band": band,
        }
        for r in published
        if (band := window.band(parse_time(r["published_at"])))
    ]
    record["releases"] = sorted(rows, key=lambda r: r["published_at"])
    record["releases_in_window"] = len(rows)
    record.update(summarize_releases(rows, window))
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
    "releases_total",
    "first_release_at",
    "releases_in_window",
    *[
        f"{b}_{m}"
        for b in BANDS
        for m in (
            "days_observed",
            "releases",
            "stable_releases",
            "mean_days_between_stable",
        )
    ],
]
RELEASE_COLUMNS = [
    "repository",
    "tag_name",
    "name",
    "prerelease",
    "created_at",
    "published_at",
    "author",
    "band",
]


def write_outputs(interim: Path, records: list[dict]) -> None:
    write_csv(interim / "summary.csv", SUMMARY_COLUMNS, records)
    write_csv(
        interim / "releases.csv",
        RELEASE_COLUMNS,
        [
            {"repository": r.get("repository", r["requested_repository"]), **row}
            for r in records
            for row in r.get("releases", [])
        ],
    )


def collect_releases(
    study: StudyConfig, candidates_path: Path, **options
) -> tuple[Path, dict]:
    return run_banded_collection(
        study,
        candidates_path,
        kind="releases",
        measurement="published_releases_around_unverified_anchor",
        collector=collect_repository_releases,
        write_outputs=write_outputs,
        describe=lambda r: (
            f"releases in window: {r.get('releases_in_window')}; "
            f"all time: {r.get('releases_total')}"
        ),
        **options,
    )
