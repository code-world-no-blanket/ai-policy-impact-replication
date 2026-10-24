"""Collect overall contributors and commit authors in bands around a policy date.

Bands are defined in ai_policy_impact.windows.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ai_policy_impact.config import HorizonConfig, StudyConfig
from ai_policy_impact.github import APIError, GitHubClient, utc_now
from ai_policy_impact.runs import paginate, select_candidates, start_run, write_csv
from ai_policy_impact.windows import (  # noqa: F401  (re-exported for callers)
    BANDS,
    anchor_dates,
    band_bounds,
    band_coverage,
    band_for,
    read_detected_dates,
    require_policy_run,
)

MAX_CONTRIBUTOR_PAGES = 100
MAX_COMMIT_PAGES = 500


def contributor_id(commit: dict) -> tuple[str, str | None, bool]:
    """Stable ID, GitHub login (if linked), and bot flag for a commit author."""
    author = commit.get("author") or {}
    login = author.get("login")
    if login:
        is_bot = author.get("type") == "Bot" or login.endswith("[bot]")
        return login, login, is_bot
    email = (commit["commit"]["author"].get("email") or "").lower()
    # Unlinked authors are keyed by a hashed email so addresses stay out of outputs.
    digest = hashlib.sha256(email.encode()).hexdigest()[:16]
    return f"email:{digest}", None, "[bot]" in email


def summarize_commits(
    commits: list[dict], coverage: dict[str, int]
) -> tuple[list[dict], dict]:
    people: dict[str, dict] = {}
    for commit in commits:
        if commit["band"] is None or commit["is_merge"]:
            continue
        person = people.setdefault(
            commit["contributor_id"],
            {
                "contributor_id": commit["contributor_id"],
                "login": commit["login"],
                "is_bot": commit["is_bot"],
                **{f"commits_{b}": 0 for b in BANDS},
                "first_commit_at": commit["committed_at"],
                "last_commit_at": commit["committed_at"],
            },
        )
        person[f"commits_{commit['band']}"] += 1
        person["first_commit_at"] = min(
            person["first_commit_at"], commit["committed_at"]
        )
        person["last_commit_at"] = max(person["last_commit_at"], commit["committed_at"])
    for person in people.values():
        pre = person["commits_pre_long"] + person["commits_pre_medium"]
        post = person["commits_post_medium"] + person["commits_post_long"]
        person["presence"] = (
            "both" if pre and post else "pre_only" if pre else "post_only"
        )
    humans = [p for p in people.values() if not p["is_bot"]]
    summary = {
        "contributors_pre": sum(p["presence"] in {"pre_only", "both"} for p in humans),
        "contributors_post": sum(
            p["presence"] in {"post_only", "both"} for p in humans
        ),
        "retained": sum(p["presence"] == "both" for p in humans),
        "pre_only": sum(p["presence"] == "pre_only" for p in humans),
        "post_only": sum(p["presence"] == "post_only" for p in humans),
        "bot_accounts": len(people) - len(humans),
        "merge_commits": sum(c["is_merge"] for c in commits),
    }
    for name in BANDS:
        observed = coverage[name] > 0
        # An unobserved band is missing, not zero.
        summary[f"{name}_days_observed"] = coverage[name]
        summary[f"{name}_contributors"] = (
            sum(p[f"commits_{name}"] > 0 for p in humans) if observed else None
        )
        summary[f"{name}_commits"] = (
            sum(p[f"commits_{name}"] for p in humans) if observed else None
        )
    return sorted(people.values(), key=lambda p: p["contributor_id"]), summary


def collect_repository_contributors(
    client: GitHubClient,
    candidate: dict,
    anchor: date | None,
    anchor_source: str,
    horizons: HorizonConfig,
    today: date,
) -> dict:
    repo = candidate["repository"]
    record = {
        "requested_repository": repo,
        "reported_policy_date": candidate.get("reported_policy_date") or None,
        "anchor_date": anchor.isoformat() if anchor else None,
        "anchor_source": anchor_source,
        "started_at": utc_now(),
        "status": "complete",
        "errors": [],
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

    try:
        listed, complete = paginate(
            client,
            f"/repos/{repo}/contributors",
            max_pages=MAX_CONTRIBUTOR_PAGES,
            anon="true",
        )
        record["overall"] = [
            {
                "login": c.get("login"),
                "type": c.get("type"),
                "contributions": c.get("contributions"),
            }
            for c in listed
        ]
        record["overall_status"] = "complete" if complete else "truncated"
        # GitHub links only the first 500 author emails to accounts; every other
        # email is a separate anonymous entry, so neither count is a headcount.
        record["overall_linked_accounts"] = sum(
            1 for c in listed if c.get("type") != "Anonymous"
        )
        record["overall_unlinked_emails"] = sum(
            1 for c in listed if c.get("type") == "Anonymous"
        )
    except APIError as error:
        # GitHub answers 403 for histories too large to list; that is not zero.
        record["overall_status"] = (
            "too_large_for_api" if error.status == 403 else "unavailable"
        )
        record["errors"].append({"component": "contributors", "error": str(error)})

    if anchor is None:
        record["errors"].append({"component": "commits", "error": "no anchor date"})
        record.update(status="partial", completed_at=utc_now())
        return record
    bounds = band_bounds(anchor, horizons)
    window_start = bounds["pre_long"][0]
    window_end = min(bounds["post_long"][1] - timedelta(days=1), today)
    record.update(
        window_start=window_start.isoformat(), window_end=window_end.isoformat()
    )
    try:
        listed, complete = paginate(
            client,
            f"/repos/{repo}/commits",
            max_pages=MAX_COMMIT_PAGES,
            sha=record["default_branch"],
            since=f"{window_start.isoformat()}T00:00:00Z",
            until=f"{window_end.isoformat()}T23:59:59Z",
        )
    except APIError as error:
        record["errors"].append({"component": "commits", "error": str(error)})
        record.update(status="partial", completed_at=utc_now())
        return record
    if not complete:
        record["errors"].append(
            {"component": "commits", "error": f"stopped at {MAX_COMMIT_PAGES} pages"}
        )
    commits = []
    for item in listed:
        ident, login, is_bot = contributor_id(item)
        committed_at = item["commit"]["committer"]["date"]
        day = (
            datetime.fromisoformat(committed_at.replace("Z", "+00:00"))
            .astimezone(timezone.utc)
            .date()
        )
        commits.append(
            {
                "sha": item["sha"],
                "committed_at": committed_at,
                "authored_at": item["commit"]["author"]["date"],
                "contributor_id": ident,
                "login": login,
                "is_bot": is_bot,
                "is_merge": len(item.get("parents", [])) > 1,
                "band": band_for(day, bounds),
            }
        )
    record["commits"] = commits
    record["commits_in_window"] = len(commits)
    coverage = band_coverage(bounds, window_end)
    record["contributors"], summary = summarize_commits(commits, coverage)
    record.update(summary)
    if record["errors"]:
        record["status"] = "partial"
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
    "overall_status",
    "overall_linked_accounts",
    "overall_unlinked_emails",
    "commits_in_window",
    "merge_commits",
    "contributors_pre",
    "contributors_post",
    "retained",
    "pre_only",
    "post_only",
    "bot_accounts",
    *[f"{b}_{m}" for b in BANDS for m in ("days_observed", "contributors", "commits")],
]
CONTRIBUTOR_COLUMNS = [
    "repository",
    "contributor_id",
    "login",
    "is_bot",
    "presence",
    *[f"commits_{b}" for b in BANDS],
    "first_commit_at",
    "last_commit_at",
]
COMMIT_COLUMNS = [
    "repository",
    "sha",
    "committed_at",
    "authored_at",
    "contributor_id",
    "login",
    "is_bot",
    "is_merge",
    "band",
]


def write_outputs(interim: Path, records: list[dict]) -> None:
    write_csv(interim / "summary.csv", SUMMARY_COLUMNS, records)
    for name, columns, key in (
        ("contributor_bands", CONTRIBUTOR_COLUMNS, "contributors"),
        ("commits", COMMIT_COLUMNS, "commits"),
        (
            "overall_contributors",
            ["repository", "login", "type", "contributions"],
            "overall",
        ),
    ):
        write_csv(
            interim / f"{name}.csv",
            columns,
            [
                {"repository": r.get("repository", r["requested_repository"]), **row}
                for r in records
                for row in r.get(key, [])
            ],
        )


def collect_contributors(
    study: StudyConfig,
    candidates_path: Path,
    *,
    policy_run: Path | None = None,
    priority: str | None = None,
    limit: int | None = None,
    progress=print,
) -> tuple[Path, dict]:
    event = study.treatment.anchor_event
    require_policy_run(event, policy_run)
    candidates = select_candidates(candidates_path, priority, limit)
    detected = read_detected_dates(policy_run)
    run = start_run(
        study,
        "contributors",
        candidates_path,
        candidates,
        measurement="default_branch_commit_authors_around_unverified_anchor",
        policy_run=str(policy_run) if policy_run else None,
        anchor_event=event.value,
        horizons=study.horizons.model_dump(),
    )
    today = datetime.now(timezone.utc).date()
    for index, candidate in enumerate(candidates, 1):
        progress(f"[{index}/{len(candidates)}] {candidate['repository']}")
        anchor, source = anchor_dates(candidate, detected, event)
        record = collect_repository_contributors(
            run.client, candidate, anchor, source, study.horizons, today
        )
        run.save_record(candidate, record)
        write_outputs(run.interim, run.records)
        progress(
            f"  {record['status']}; anchor: {record['anchor_date']} ({source}); "
            f"pre: {record.get('contributors_pre')}; post: {record.get('contributors_post')}; "
            f"retained: {record.get('retained')}"
        )
    return run.interim, run.finish()
