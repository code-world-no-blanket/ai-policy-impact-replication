"""Repository and owner context, with maintainer files as of the policy date.

Owner (organization or user) figures and repository metadata are current values:
GitHub keeps no history of them. Maintainer files (MAINTAINERS, CODEOWNERS,
OWNERS, COMMITTERS, GOVERNANCE) are read at the last default-branch commit before
the policy date, and the GitHub handles they name are counted. Handles are an
automatic proxy; team handles (@org/team) are counted separately, unresolved.
"""

from __future__ import annotations

import base64
import re
import time
from datetime import date, datetime, time as clock, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from ai_policy_impact.activity import begin_record, run_banded_collection
from ai_policy_impact.collect import EXCLUDED_PARTS
from ai_policy_impact.config import HorizonConfig, StudyConfig
from ai_policy_impact.github import APIError, GitHubClient, utc_now
from ai_policy_impact.runs import paginate, write_csv

MAINTAINER_FILE = re.compile(
    r"(?:maintainers?|codeowners|owners|committers|governance)"
    r"(?:\.(?:md|rst|txt|ya?ml))?\Z",
    re.I,
)
MAX_FILES = 20
MAX_MEMBER_PAGES = 50
# "@login" or "@org/team", not part of an email address or a path.
HANDLE = re.compile(
    r"(?<![\w.@/-])@([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))(/[A-Za-z0-9_.-]+)?"
)
# A link to a GitHub profile (not to a repository under it).
PROFILE_LINK = re.compile(
    r"github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))(?![\w/.-])", re.I
)
NOT_PEOPLE = {
    "about",
    "apps",
    "features",
    "orgs",
    "sponsors",
    "settings",
    "notifications",
    "marketplace",
    "topics",
    "github",
}


def handles(text: str, owner: str) -> tuple[set[str], set[str]]:
    """GitHub users and teams a maintainer file names; the owner itself excluded."""
    users, teams = set(), set()
    for login, team in HANDLE.findall(text):
        if team:
            teams.add(f"{login}{team}".lower())
        else:
            users.add(login.lower())
    users |= {login.lower() for login in PROFILE_LINK.findall(text)}
    users -= NOT_PEOPLE | {owner.lower()}
    return users, teams


def owner_fields(client: GitHubClient, owner: dict, record: dict) -> None:
    login, kind = owner["login"], owner.get("type")
    record.update(owner_login=login, owner_type=kind)
    try:
        if kind == "Organization":
            data = client.get(f"/orgs/{login}").data
            members, finished = paginate(
                client, f"/orgs/{login}/public_members", max_pages=MAX_MEMBER_PAGES
            )
            record.update(
                owner_public_members=len(members) if finished else None,
                owner_is_verified=data.get("is_verified"),
            )
        else:
            data = client.get(f"/users/{login}").data
        record.update(
            owner_created_at=data.get("created_at"),
            owner_public_repos=data.get("public_repos"),
            owner_followers=data.get("followers"),
        )
    except APIError as error:
        record["errors"].append({"component": "owner", "error": str(error)})


def maintainer_files(
    client: GitHubClient, repo: str, branch: str, anchor: date, record: dict
) -> None:
    before = datetime.combine(anchor, clock.min, timezone.utc).isoformat()
    try:
        commits = client.get(
            f"/repos/{repo}/commits",
            sha=branch,
            until=before.replace("+00:00", "Z"),
            per_page=1,
        ).data
        if not commits:
            record["maintainer_files_ref"] = None
            record["maintainer_files"] = []
            return
        ref = commits[0]["sha"]
        tree = client.get(f"/repos/{repo}/git/trees/{ref}", recursive=1).data
    except APIError as error:
        record["errors"].append({"component": "maintainer_files", "error": str(error)})
        return
    paths = sorted(
        (
            entry["path"]
            for entry in tree["tree"]
            if entry["type"] == "blob"
            and MAINTAINER_FILE.fullmatch(PurePosixPath(entry["path"]).name)
            and not (
                {p.lower() for p in PurePosixPath(entry["path"]).parts[:-1]}
                & EXCLUDED_PARTS
            )
        ),
        key=lambda p: (len(PurePosixPath(p).parts), p.lower()),
    )
    record.update(
        maintainer_files_ref=ref,
        maintainer_files_tree_truncated=bool(tree.get("truncated")),
        maintainer_files_capped=len(paths) > MAX_FILES,
    )
    files, users, teams = [], set(), set()
    for path in paths[:MAX_FILES]:
        try:
            content = client.get(
                f"/repos/{repo}/contents/{quote(path, safe='/')}", ref=ref
            ).data
        except APIError as error:
            record["errors"].append({"component": f"file:{path}", "error": str(error)})
            continue
        if content.get("encoding") != "base64" or not content.get("content"):
            files.append({"path": path, "users": None, "teams": None})
            continue
        text = base64.b64decode(content["content"]).decode("utf-8", "replace")
        found_users, found_teams = handles(text, record["owner_login"])
        users |= found_users
        teams |= found_teams
        files.append(
            {"path": path, "users": len(found_users), "teams": len(found_teams)}
        )
    record.update(
        maintainer_files=files,
        maintainer_file_users=sorted(users),
        maintainer_file_user_count=len(users) if files else None,
        maintainer_file_team_count=len(teams) if files else None,
    )


def collect_repository_context(
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
    license_info = details.get("license") or {}
    record.update(
        repository_created_at=details.get("created_at"),
        primary_language=details.get("language"),
        license_spdx_id=license_info.get("spdx_id"),
        topics=details.get("topics") or [],
        has_issues=details.get("has_issues"),
        has_discussions=details.get("has_discussions"),
        has_wiki=details.get("has_wiki"),
        archived=details.get("archived"),
        metadata_retrieved_at=utc_now(),
    )
    owner_fields(client, details["owner"], record)
    maintainer_files(
        client, record["repository"], record["default_branch"], anchor, record
    )
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
    "repository_created_at",
    "primary_language",
    "license_spdx_id",
    "has_issues",
    "has_discussions",
    "has_wiki",
    "archived",
    "owner_login",
    "owner_type",
    "owner_created_at",
    "owner_public_repos",
    "owner_followers",
    "owner_public_members",
    "owner_is_verified",
    "maintainer_files_ref",
    "maintainer_file_user_count",
    "maintainer_file_team_count",
    "maintainer_files_capped",
    "maintainer_files_tree_truncated",
    "metadata_retrieved_at",
]


def write_outputs(interim: Path, records: list[dict]) -> None:
    write_csv(interim / "summary.csv", SUMMARY_COLUMNS, records)


def collect_context(
    study: StudyConfig, candidates_path: Path, **options
) -> tuple[Path, dict]:
    return run_banded_collection(
        study,
        candidates_path,
        kind="context",
        measurement="owner_context_and_maintainer_files_at_unverified_anchor",
        collector=collect_repository_context,
        write_outputs=write_outputs,
        describe=lambda r: (
            f"owner: {r.get('owner_type')}; maintainer-file users: "
            f"{r.get('maintainer_file_user_count')}"
        ),
        **options,
    )
