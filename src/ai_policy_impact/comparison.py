"""Propose comparison repositories (no AI policy) for the policy repositories.

Each policy repository gets one comparison repository resembling it: same
primary language, similar stars, and similar pull-request volume in the year
before the comparison repository's stand-in date. Stand-in dates follow the
policy dates' timing: policy dates fall into early / middle / late clusters, and
each comparison repository takes the date of a policy repository in its
partner's cluster, drawn without replacement (a random shuffle within the
cluster), shifted by up to JITTER_DAYS. The seed is recorded so
the draw can be repeated.

A candidate has no AI policy when its default branch has no AI_POLICY.md, no
.ai/AI_POLICY.md, and no CONTRIBUTING.md mentioning an AI term (ai_terms), the
rule of scripts/find_no_ai_policy_repos.py. The output is a proposal for review;
nothing is collected for it until the team accepts it.
"""

from __future__ import annotations

import base64
import csv
import math
import random
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from ai_policy_impact.ai_terms import (
    CONTRIBUTING_FILENAME,
    MENTIONS_AI,
    POLICY_FILENAME,
)
from ai_policy_impact.collect import read_candidates
from ai_policy_impact.config import StudyConfig
from ai_policy_impact.github import APIError, GitHubClient, utc_now
from ai_policy_impact.runs import select_candidates, start_run, write_csv
from ai_policy_impact.windows import (
    anchor_dates,
    read_detected_dates,
    require_policy_run,
)

# Early / middle / late: policy dates before the first bound, before the second,
# and from the second on.
DEFAULT_BOUNDS = (date(2026, 1, 1), date(2026, 5, 1))
JITTER_DAYS = 14
# Star ranges tried around the policy repository's stars, narrowest first.
STAR_FACTORS = (1.5, 3.0)
MIN_POOL = 20
CHECKED_PER_REPO = 15
# A "good" match is within these factors of the policy repository.
GOOD_PR_FACTOR = 2.0
GOOD_STAR_FACTOR = 3.0
ACTIVE_DAYS = 60
# GitHub allows 30 search requests a minute.
SEARCH_PAUSE_SECONDS = 2.2

PROPOSAL_COLUMNS = [
    "repository",
    "priority",
    "policy_paths",
    "group",
    "stand_in_date",
    "date_cluster",
    "drawn_from_policy_date",
    "matched_policy_repo",
    "policy_date",
    "language",
    "owner_type",
    "stars",
    "policy_repo_stars",
    "prs_year_before",
    "policy_repo_prs_year_before",
    "match_quality",
    "alternates",
    "no_policy_checked_at",
    "seed",
    "review_status",
    "reviewer",
    "notes",
]


def cluster_of(day: date, bounds: tuple[date, date]) -> str:
    if day < bounds[0]:
        return "early"
    if day < bounds[1]:
        return "middle"
    return "late"


def draw_dates(
    policy_dates: dict[str, date],
    bounds: tuple[date, date],
    rng: random.Random,
    today: date,
) -> dict[str, tuple[str, date, date]]:
    """Per policy repository: its cluster, the drawn policy date, the stand-in date.

    Within each cluster the policy dates are shuffled and handed out one each,
    so the comparison group reproduces the policy dates' distribution exactly.
    """
    by_cluster: dict[str, list[str]] = {}
    for repo in sorted(policy_dates):
        by_cluster.setdefault(cluster_of(policy_dates[repo], bounds), []).append(repo)
    drawn = {}
    for cluster, repos in sorted(by_cluster.items()):
        donors = repos[:]
        rng.shuffle(donors)
        for repo, donor in zip(repos, donors):
            drawn[repo] = (cluster, policy_dates[donor])
    result = {}
    for repo in sorted(policy_dates):
        cluster, base = drawn[repo]
        stand_in = min(
            base + timedelta(days=rng.randint(-JITTER_DAYS, JITTER_DAYS)), today
        )
        result[repo] = (cluster, base, stand_in)
    return result


class Search:
    """Paced GitHub search calls."""

    def __init__(self, client: GitHubClient, sleep=time.sleep):
        self.client = client
        self.sleep = sleep

    def get(self, path: str, **params) -> dict:
        self.sleep(SEARCH_PAUSE_SECONDS)
        return self.client.get(path, **params).data

    def pr_count(self, repo: str, start: date, end: date) -> int:
        query = f"repo:{repo} is:pr created:{start.isoformat()}..{end.isoformat()}"
        return self.get("/search/issues", q=query, per_page=1)["total_count"]

    def repositories(self, query: str) -> list[dict]:
        return self.get(
            "/search/repositories", q=query, sort="stars", order="desc", per_page=100
        )["items"]


def policy_signal(client: GitHubClient, repo: str, branch: str) -> str | None:
    """The first AI-policy signal on the default branch, "" for none, None if unknown."""
    try:
        tree = client.get(
            f"/repos/{repo}/git/trees/{quote(branch, safe='')}", recursive=1
        ).data
    except APIError:
        return None
    if tree.get("truncated"):
        return None
    paths = [e["path"] for e in tree.get("tree", []) if e.get("type") == "blob"]
    for path in paths:
        if path.rsplit("/", 1)[-1] == POLICY_FILENAME:
            return path
    for path in paths:
        if path.rsplit("/", 1)[-1] != CONTRIBUTING_FILENAME:
            continue
        try:
            content = client.get(
                f"/repos/{repo}/contents/{quote(path, safe='/')}", ref=branch
            ).data
        except APIError:
            return None
        text = base64.b64decode("".join(content.get("content", "").split()))
        found = MENTIONS_AI.search(text.decode("utf-8", errors="replace"))
        if found:
            return f"{path} mentions {found.group(0)!r}"
    return ""


def distance(candidate: dict, policy: dict) -> float:
    """Smaller is closer: PR volume first, then stars, then owner type."""
    return (
        abs(math.log((candidate["prs"] + 1) / (policy["prs"] + 1)))
        + 0.5 * abs(math.log(max(candidate["stars"], 1) / max(policy["stars"], 1)))
        + (0.25 if candidate["owner_type"] != policy["owner_type"] else 0.0)
    )


def quality(candidate: dict, policy: dict) -> str:
    pr_ratio = (candidate["prs"] + 1) / (policy["prs"] + 1)
    star_ratio = max(candidate["stars"], 1) / max(policy["stars"], 1)
    good = (
        1 / GOOD_PR_FACTOR <= pr_ratio <= GOOD_PR_FACTOR
        and 1 / GOOD_STAR_FACTOR <= star_ratio <= GOOD_STAR_FACTOR
    )
    return "good" if good else "weak"


def match_one(
    client: GitHubClient,
    search: Search,
    policy: dict,
    stand_in: date,
    excluded: set[str],
    today: date,
    log: list[str],
) -> list[dict]:
    """Checked candidates for one policy repository, closest first."""
    created_before = stand_in - timedelta(days=365)
    pushed_since = today - timedelta(days=ACTIVE_DAYS)
    language = f'language:"{policy["language"]}" ' if policy["language"] else ""
    pool: list[dict] = []
    for factor in STAR_FACTORS:
        low = max(1, int(policy["stars"] / factor))
        high = int(policy["stars"] * factor)
        query = (
            f"{language}stars:{low}..{high} fork:false archived:false "
            f"created:<{created_before.isoformat()} pushed:>={pushed_since.isoformat()}"
        )
        pool = [
            item
            for item in search.repositories(query)
            if item["full_name"].lower() not in excluded
            and not item.get("mirror_url")
            and not item.get("is_template")
        ]
        if len(pool) >= MIN_POOL:
            break
    pool.sort(
        key=lambda i: abs(
            math.log(max(i["stargazers_count"], 1) / max(policy["stars"], 1))
        )
    )
    checked = []
    for item in pool[:CHECKED_PER_REPO]:
        name = item["full_name"]
        signal = policy_signal(client, name, item["default_branch"])
        if signal is None:
            log.append(f"    ? {name}: could not confirm no AI policy")
            continue
        if signal:
            log.append(f"    x {name}: {signal}")
            continue
        try:
            prs = search.pr_count(
                name, stand_in - timedelta(days=365), stand_in - timedelta(days=1)
            )
        except APIError as error:
            log.append(f"    ? {name}: PR count failed ({error})")
            continue
        candidate = {
            "repository": name,
            "language": item.get("language"),
            "owner_type": (item.get("owner") or {}).get("type"),
            "stars": item["stargazers_count"],
            "prs": prs,
        }
        candidate["distance"] = distance(candidate, policy)
        checked.append(candidate)
    return sorted(checked, key=lambda c: c["distance"])


def select_comparisons(
    study: StudyConfig,
    candidates_path: Path,
    *,
    policy_run: Path | None,
    priority: str | None,
    seed: int,
    skip: list[str],
    bounds: tuple[date, date] = DEFAULT_BOUNDS,
    output: Path | None = None,
    overwrite: bool = False,
    progress=print,
    sleep=time.sleep,
) -> tuple[Path, dict]:
    event = study.treatment.anchor_event
    require_policy_run(event, policy_run)
    output = output or study.data.manual / "comparison_repos.csv"
    if output.exists() and not overwrite:
        raise ValueError(f"{output} exists; pass --overwrite to replace it")
    skipped = {s.lower() for s in skip}
    policies = [
        c
        for c in select_candidates(candidates_path, priority, None)
        if c["repository"].lower() not in skipped
    ]
    detected = read_detected_dates(policy_run)
    policy_dates = {}
    for candidate in policies:
        anchor, _ = anchor_dates(candidate, detected, event)
        if anchor is None:
            raise ValueError(f"No policy date for {candidate['repository']}")
        policy_dates[candidate["repository"]] = anchor
    today = datetime.now(timezone.utc).date()
    rng = random.Random(seed)
    drawn = draw_dates(policy_dates, bounds, rng, today)
    run = start_run(
        study,
        "comparison_selection",
        candidates_path,
        policies,
        measurement="proposed_comparison_repositories",
        seed=seed,
        cluster_bounds=[b.isoformat() for b in bounds],
        skipped=sorted(skipped),
    )
    client = run.client
    search = Search(client, sleep)
    excluded = {c["repository"].lower() for c in read_candidates(candidates_path)}
    proposals = []
    # The largest repositories have the fewest look-alikes; they choose first.
    metadata = {}
    for repo in policy_dates:
        details = client.get(f"/repos/{repo}").data
        metadata[repo] = {
            "language": details.get("language"),
            "owner_type": (details.get("owner") or {}).get("type"),
            "stars": details["stargazers_count"],
        }
    order = sorted(policy_dates, key=lambda r: -metadata[r]["stars"])
    for index, repo in enumerate(order, 1):
        cluster, base, stand_in = drawn[repo]
        progress(f"[{index}/{len(order)}] {repo} ({cluster}; stand-in {stand_in})")
        record = {
            "requested_repository": repo,
            "status": "complete",
            "errors": [],
            "started_at": utc_now(),
            "date_cluster": cluster,
            "stand_in_date": stand_in.isoformat(),
        }
        log: list[str] = []
        try:
            policy = {
                **metadata[repo],
                "prs": search.pr_count(
                    repo,
                    policy_dates[repo] - timedelta(days=365),
                    policy_dates[repo] - timedelta(days=1),
                ),
            }
            checked = match_one(client, search, policy, stand_in, excluded, today, log)
            if not checked and policy["language"]:
                # Too few repositories in this language; any language will do.
                log.append(f"    no match in {policy['language']}; trying any language")
                checked = match_one(
                    client,
                    search,
                    {**policy, "language": None},
                    stand_in,
                    excluded,
                    today,
                    log,
                )
        except APIError as error:
            record["errors"].append({"component": "matching", "error": str(error)})
            record["status"] = "failed"
            checked, policy = [], None
        record.update(policy=policy, checked=checked, log=log)
        for line in log:
            progress(line)
        if not checked:
            if record["status"] == "complete":
                record["status"] = "partial"
                record["errors"].append(
                    {"component": "matching", "error": "no candidate passed"}
                )
            progress("  no match found")
        else:
            best = checked[0]
            excluded.add(best["repository"].lower())
            proposals.append(
                {
                    "repository": best["repository"],
                    "priority": priority or "",
                    "policy_paths": "",
                    "group": "comparison",
                    "stand_in_date": stand_in.isoformat(),
                    "date_cluster": cluster,
                    "drawn_from_policy_date": base.isoformat(),
                    "matched_policy_repo": repo,
                    "policy_date": policy_dates[repo].isoformat(),
                    "language": best["language"],
                    "owner_type": best["owner_type"],
                    "stars": best["stars"],
                    "policy_repo_stars": policy["stars"],
                    "prs_year_before": best["prs"],
                    "policy_repo_prs_year_before": policy["prs"],
                    "match_quality": quality(best, policy),
                    "alternates": "|".join(c["repository"] for c in checked[1:3]),
                    "no_policy_checked_at": today.isoformat(),
                    "seed": seed,
                    "review_status": "proposed",
                    "reviewer": "",
                    "notes": "",
                }
            )
            progress(
                f"  -> {best['repository']} (stars {best['stars']} vs "
                f"{policy['stars']}; PRs {best['prs']} vs {policy['prs']}; "
                f"{quality(best, policy)})"
            )
        record["completed_at"] = utc_now()
        run.save_record({"repository": repo}, record)
    proposals.sort(key=lambda r: r["matched_policy_repo"].lower())
    write_csv(run.interim / "proposals.csv", PROPOSAL_COLUMNS, proposals)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=PROPOSAL_COLUMNS)
        writer.writeheader()
        writer.writerows(proposals)
    summary = run.finish()
    summary.update(proposals=len(proposals), output=str(output))
    return output, summary
