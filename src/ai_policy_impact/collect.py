"""Collect dated repository profiles and source documents for institutional coding."""

from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from ai_policy_impact.config import StudyConfig
from ai_policy_impact.github import (
    API_VERSION,
    APIError,
    GitHubClient,
    github_token,
    utc_now,
    write_json,
)
from ai_policy_impact.projects import collect_projects

REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+\Z")
GOVERNANCE = re.compile(
    r"(?:maintainers?|governance|codeowners|owners|committers|team|contributing)(?:\.(?:md|rst|txt|ya?ml|json))?\Z",
    re.I,
)
EXCLUDED_PARTS = {
    "vendor",
    "vendored",
    "third_party",
    "node_modules",
    "fixtures",
    "test",
    "tests",
}
MAX_GOVERNANCE_FILES = 20
# Instructions for AI coding tools. They show a project uses such tools, not that
# it has a contribution policy; recorded for follow-up only, never fetched.
AI_TOOL_FILENAMES = {
    "agents.md",
    "claude.md",
    "gemini.md",
    ".cursorrules",
    ".windsurfrules",
}
AI_TOOL_PATHS = re.compile(
    r"(?:^|/)(?:\.github/copilot-instructions\.md|\.github/instructions/[^/]+\.md"
    r"|\.cursor/rules/[^/]+|\.claude/[^/]+)\Z",
    re.I,
)


def ai_tool_files(paths: list[str]) -> list[str]:
    """Paths of AI coding-tool instruction files, vendored directories excluded."""
    return sorted(
        path
        for path in paths
        if (
            PurePosixPath(path).name.lower() in AI_TOOL_FILENAMES
            or AI_TOOL_PATHS.search(path)
        )
        and not {p.lower() for p in PurePosixPath(path).parts[:-1]} & EXCLUDED_PARTS
    )


def read_candidates(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if not {"repository", "priority", "policy_paths"} <= set(
            reader.fieldnames or []
        ):
            raise ValueError(
                "Candidate CSV requires repository, priority, and policy_paths columns"
            )
        rows = list(reader)
    seen = set()
    for row in rows:
        if None in row or any(
            row.get(key) is None for key in ("repository", "priority", "policy_paths")
        ):
            raise ValueError("Candidate CSV contains a row with missing or extra cells")
        repo = row["repository"]
        if not REPOSITORY.fullmatch(repo) or any(
            part in {".", ".."} for part in repo.split("/")
        ):
            raise ValueError(f"Invalid repository: {repo!r}")
        if repo.lower() in seen:
            raise ValueError(f"Duplicate repository: {repo}")
        seen.add(repo.lower())
        for path in filter(None, row["policy_paths"].split("|")):
            if (
                path.startswith("/")
                or ".." in PurePosixPath(path).parts
                or "\\" in path
            ):
                raise ValueError(f"Invalid policy path: {path!r}")
    if not rows:
        raise ValueError("Candidate CSV is empty")
    return rows


def metadata_fields(data: dict) -> dict:
    license_data = data.get("license") or {}
    return {
        "repository_id": data["id"],
        "repository": data["full_name"],
        "url": data["html_url"],
        "description": data.get("description"),
        "owner_type": data["owner"]["type"],
        "created_at": data["created_at"],
        "pushed_at": data.get("pushed_at"),
        "default_branch": data["default_branch"],
        "fork": data["fork"],
        "archived": data["archived"],
        "disabled": data.get("disabled"),
        "stars": data["stargazers_count"],
        # REST watchers_count is an alias for stars, not subscriptions.
        "watchers": data.get("subscribers_count"),
        "forks": data["forks_count"],
        "primary_language": data.get("language"),
        "topics": data.get("topics", []),
        "license_spdx_id": license_data.get("spdx_id"),
        "license_name": license_data.get("name"),
        "license_status": "detected"
        if license_data.get("spdx_id") not in {None, "NOASSERTION"}
        else "needs_review",
    }


def save_document(response, run_raw: Path, repo: str, ref: str, kind: str) -> dict:
    data = response.data
    evidence = {
        "kind": kind,
        "path": data["path"],
        "blob_sha": data.get("sha"),
        "ref": ref,
        "retrieved_at": response.retrieved_at,
        "source_url": f"https://github.com/{repo}/blob/{ref}/{quote(data['path'], safe='/')}",
        "raw_response": response.raw_file,
    }
    if data.get("encoding") != "base64" or "content" not in data:
        return {**evidence, "status": "content_unavailable"}
    try:
        content = base64.b64decode("".join(data["content"].split()), validate=True)
    except (ValueError, binascii.Error):
        return {**evidence, "status": "invalid_encoding"}
    name = hashlib.sha256(data["path"].encode()).hexdigest()[:16] + ".txt"
    target = run_raw / "files" / repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return {
        **evidence,
        "status": "collected",
        "local_path": str(target),
        "sha256": hashlib.sha256(content).hexdigest(),
        "bytes": len(content),
    }


def collect_repository(
    client: GitHubClient, candidate: dict, run_raw: Path, include_projects: bool
) -> dict:
    repo = candidate["repository"]
    profile = {
        "requested_repository": repo,
        "candidate": candidate,
        "started_at": utc_now(),
        "status": "complete",
        "errors": [],
        "documents": [],
        "maintainers_present": None,
        "maintainer_count": None,
        "institutional_review_status": "unreviewed",
    }

    def fetch(label: str, path: str, *, missing_ok: bool = False, **params):
        try:
            return client.get(path, **params)
        except APIError as error:
            if not (missing_ok and error.status == 404):
                profile["errors"].append(
                    {
                        "component": label,
                        "status": error.status,
                        "error": str(error),
                        "raw_file": error.raw_file,
                    }
                )
            return None

    details = fetch("metadata", f"/repos/{repo}")
    if details is None:
        profile.update(status="failed", completed_at=utc_now())
        return profile
    if details.data.get("private"):
        profile.update(status="skipped_private", completed_at=utc_now())
        return profile
    profile.update(metadata_fields(details.data))
    repo = profile["repository"]
    profile["metadata_retrieved_at"] = details.retrieved_at
    profile["metadata_raw_response"] = details.raw_file
    languages = fetch("languages", f"/repos/{repo}/languages")
    profile["languages_bytes"] = languages.data if languages else None
    commits = fetch(
        "default_branch_head",
        f"/repos/{repo}/commits",
        sha=profile["default_branch"],
        per_page=1,
    )
    ref = commits.data[0]["sha"] if commits and commits.data else None
    profile["source_commit"] = ref
    if ref:
        readme = fetch("readme", f"/repos/{repo}/readme", ref=ref, missing_ok=True)
        profile["readme_status"] = "unavailable_or_missing"
        if readme:
            document = save_document(readme, run_raw, repo, ref, "readme")
            profile["documents"].append(document)
            profile["readme_status"] = document["status"]
        tree = fetch("tree", f"/repos/{repo}/git/trees/{ref}", recursive=1)
        paths = []
        if tree:
            paths = sorted(
                {
                    entry["path"]
                    for entry in tree.data["tree"]
                    if entry["type"] == "blob"
                    and GOVERNANCE.fullmatch(PurePosixPath(entry["path"]).name)
                    and not (
                        set(p.lower() for p in PurePosixPath(entry["path"]).parts[:-1])
                        & EXCLUDED_PARTS
                    )
                },
                key=lambda p: (len(PurePosixPath(p).parts), p.lower()),
            )
        profile["governance_scan_status"] = (
            "unavailable"
            if not tree
            else "truncated"
            if tree.data.get("truncated")
            else "complete"
        )
        profile["governance_candidate_paths"] = paths
        profile["ai_tool_files"] = (
            ai_tool_files([e["path"] for e in tree.data["tree"] if e["type"] == "blob"])
            if tree
            else None
        )
        # Absence is only known when the whole tree was listed.
        profile["has_ai_tool_files"] = (
            None
            if not tree or (tree.data.get("truncated") and not profile["ai_tool_files"])
            else bool(profile["ai_tool_files"])
        )
        profile["governance_files_capped"] = len(paths) > MAX_GOVERNANCE_FILES
        policy_paths = list(filter(None, candidate["policy_paths"].split("|")))
        selected = list(dict.fromkeys(policy_paths + paths[:MAX_GOVERNANCE_FILES]))
        for path in selected:
            kind = (
                "policy_candidate" if path in policy_paths else "governance_candidate"
            )
            response = fetch(
                kind,
                f"/repos/{repo}/contents/{quote(path, safe='/')}",
                ref=ref,
                missing_ok=True,
            )
            if response:
                profile["documents"].append(
                    save_document(response, run_raw, repo, ref, kind)
                )
            else:
                profile["documents"].append(
                    {
                        "kind": kind,
                        "path": path,
                        "ref": ref,
                        "status": "unavailable_or_missing",
                    }
                )
    else:
        profile["errors"].append(
            {"component": "source_commit", "error": "No default-branch head resolved"}
        )
    profile["projects"] = (
        collect_projects(client, repo)
        if include_projects
        else {"status": "not_requested"}
    )
    if profile["errors"] or profile["projects"]["status"] in {"partial", "unavailable"}:
        profile["status"] = "partial"
    profile["completed_at"] = utc_now()
    return profile


PROFILE_COLUMNS = [
    "requested_repository",
    "repository",
    "repository_id",
    "status",
    "metadata_retrieved_at",
    "source_commit",
    "description",
    "owner_type",
    "stars",
    "watchers",
    "forks",
    "primary_language",
    "license_spdx_id",
    "license_status",
    "fork",
    "archived",
    "readme_status",
    "governance_scan_status",
    "has_ai_tool_files",
    "ai_tool_files",
    "maintainers_present",
    "maintainer_count",
    "institutional_review_status",
    "projects_status",
]


def write_profile_csv(path: Path, profiles: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=PROFILE_COLUMNS, extrasaction="ignore"
        )
        writer.writeheader()
        for profile in profiles:
            writer.writerow(
                {
                    **profile,
                    "projects_status": profile.get("projects", {}).get("status"),
                    "ai_tool_files": ";".join(profile.get("ai_tool_files") or []),
                }
            )


def collect_profiles(
    study: StudyConfig,
    candidates_path: Path,
    *,
    priority: str | None = None,
    limit: int | None = None,
    include_projects: bool = True,
    progress=print,
) -> tuple[Path, dict]:
    candidates = read_candidates(candidates_path)
    if priority:
        candidates = [c for c in candidates if c["priority"] == priority]
    if limit is not None:
        candidates = candidates[:limit]
    if not candidates:
        raise ValueError("No candidates match the requested selection")
    run_id = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid.uuid4().hex[:8]
    )
    raw = study.data.raw / "repository_profiles" / run_id
    interim = study.data.interim / "repository_profiles" / run_id
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
        "include_projects": include_projects,
        "measurement": "current_snapshot_not_pre_policy_covariates",
    }
    write_json(raw / "manifest.json", manifest)
    client = GitHubClient(raw / "requests", github_token())
    profiles = []
    for index, candidate in enumerate(candidates, 1):
        progress(f"[{index}/{len(candidates)}] {candidate['repository']}")
        profile = collect_repository(client, candidate, raw, include_projects)
        profiles.append(profile)
        write_json(
            interim
            / "repositories"
            / (candidate["repository"].replace("/", "__") + ".json"),
            profile,
        )
        # Persist each completed repository even if a later request is interrupted.
        write_profile_csv(interim / "profiles.csv", profiles)
        progress(
            f"  {profile['status']}; Projects: {profile.get('projects', {}).get('status', 'not_collected')}"
        )
    manifest.update(
        completed_at=utc_now(),
        status="completed",
        repositories=len(profiles),
        requests=client.sequence,
        complete=sum(p["status"] == "complete" for p in profiles),
        partial=sum(p["status"] == "partial" for p in profiles),
        failed=sum(p["status"] in {"failed", "skipped_private"} for p in profiles),
        readmes=sum(p.get("readme_status") == "collected" for p in profiles),
        documents=sum(
            d["status"] == "collected" for p in profiles for d in p["documents"]
        ),
        interim_directory=str(interim),
    )
    write_json(raw / "manifest.json", manifest)
    write_json(interim / "summary.json", manifest)
    return interim, manifest
