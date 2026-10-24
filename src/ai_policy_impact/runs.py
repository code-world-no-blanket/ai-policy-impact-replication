"""Shared bookkeeping for dated, never-overwritten collection runs."""

from __future__ import annotations

import csv
import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ai_policy_impact.collect import read_candidates
from ai_policy_impact.config import StudyConfig
from ai_policy_impact.github import (
    API_VERSION,
    APIError,
    GitHubClient,
    github_token,
    utc_now,
    write_json,
)


def select_candidates(
    path: Path, priority: str | None, limit: int | None
) -> list[dict]:
    candidates = read_candidates(path)
    if priority:
        candidates = [c for c in candidates if c["priority"] == priority]
    if limit is not None:
        candidates = candidates[:limit]
    if not candidates:
        raise ValueError("No candidates match the requested selection")
    return candidates


def paginate(
    client: GitHubClient,
    endpoint: str,
    /,
    *,
    max_pages: int,
    per_page: int = 100,
    **params,
) -> tuple[list, bool]:
    """Fetch a REST list page by page; the flag is False when max_pages cut it off."""
    items: list = []
    for page in range(1, max_pages + 1):
        response = client.get(endpoint, per_page=per_page, page=page, **params)
        if not isinstance(response.data, list):
            raise APIError(200, f"Expected a list from {endpoint}", response.raw_file)
        items.extend(response.data)
        if len(response.data) < per_page:
            return items, True
    return items, False


def write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


@dataclass
class Run:
    raw: Path
    interim: Path
    manifest: dict
    client: GitHubClient
    records: list[dict] = field(default_factory=list)

    def save_record(self, candidate: dict, record: dict) -> None:
        self.records.append(record)
        write_json(
            self.interim
            / "repositories"
            / (candidate["repository"].replace("/", "__") + ".json"),
            record,
        )

    def finish(self) -> dict:
        records = self.records
        self.manifest.update(
            completed_at=utc_now(),
            status="completed",
            repositories=len(records),
            requests=self.client.sequence,
            complete=sum(r["status"] == "complete" for r in records),
            partial=sum(r["status"] == "partial" for r in records),
            failed=sum(r["status"] in {"failed", "skipped_private"} for r in records),
            interim_directory=str(self.interim),
        )
        write_json(self.raw / "manifest.json", self.manifest)
        write_json(self.interim / "summary.json", self.manifest)
        return self.manifest


def new_run_id() -> str:
    """A sortable, collision-resistant run ID: UTC start time plus a random suffix."""
    return (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid.uuid4().hex[:8]
    )


def start_run(
    study: StudyConfig,
    kind: str,
    candidates_path: Path,
    candidates: list[dict],
    **manifest_fields,
) -> Run:
    run_id = new_run_id()
    raw = study.data.raw / kind / run_id
    interim = study.data.interim / kind / run_id
    raw.mkdir(parents=True, exist_ok=False)
    interim.mkdir(parents=True, exist_ok=False)
    candidate_bytes = candidates_path.read_bytes()
    (raw / "candidates.csv").write_bytes(candidate_bytes)
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "started_at": utc_now(),
        "status": "running",
        "api_version": API_VERSION,
        "candidates_file": str(candidates_path),
        "candidates_sha256": hashlib.sha256(candidate_bytes).hexdigest(),
        "selected_repositories": [c["repository"] for c in candidates],
        **manifest_fields,
    }
    write_json(raw / "manifest.json", manifest)
    return Run(raw, interim, manifest, GitHubClient(raw / "requests", github_token()))


def guarded(collect, candidate: dict, *args) -> dict:
    """Run one repository's collector; an unexpected error fails only that repository."""
    try:
        return collect(*args)
    except Exception as error:  # noqa: BLE001 - keep a long run going
        return {
            "requested_repository": candidate["repository"],
            "repository": candidate["repository"],
            "status": "failed",
            "errors": [
                {"component": "unexpected", "error": f"{type(error).__name__}: {error}"}
            ],
            "completed_at": utc_now(),
        }
