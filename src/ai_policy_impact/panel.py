"""Repository-week panel built from the interim outcome runs.

Weeks are counted from each repository's anchor date: event week 0 is the seven
days starting on the anchor, so weeks -52..-1 are before the policy and 0..51 are
after it. Every row ends at the repository's panel end, the earliest date through
which all of its sources were observed; weeks after it are omitted and a partly
observed week is kept with its `days_observed`.

Bot activity is excluded everywhere except releases, which projects often publish
from CI accounts, and default-branch CI results, which describe the branch rather
than a person. A bot is an account GitHub types as a bot or whose login names one
(`is_bot_login`); the login rule is re-applied here, so it covers runs collected
before it was added. Merge commits are excluded from commit counts and churn, as
in the churn and contributor collectors.

Alongside the weekly table, `characteristics.csv` holds one row per repository:
the manually coded policy type, repository and owner context, maintainer counts,
and stars and forks at the policy date.
"""

from __future__ import annotations

import csv
import json
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from ai_policy_impact.activity import (
    hours_between,
    is_bot_actor,
    is_bot_login,
    median,
)
from ai_policy_impact.config import StudyConfig
from ai_policy_impact.github import utc_now, write_json
from ai_policy_impact.runs import new_run_id, select_candidates, write_csv
from ai_policy_impact.windows import (
    anchor_dates,
    parse_time,
    read_detected_dates,
    require_policy_run,
)

WEEKS_BEFORE = 52
WEEKS_AFTER = 52
OUTCOME_KINDS = (
    "pull_requests",
    "issues",
    "releases",
    "churn",
    "comments",
    "discussions",
    "ci",
)
# Not tied to an anchor: dated histories reused whatever the policy date.
HISTORY_KINDS = ("star_history", "fork_history", "first_seen")
# Tied to the anchor but not a time series; it does not limit the panel end.
CONTEXT_KIND = "context"
ALL_KINDS = (*OUTCOME_KINDS, *HISTORY_KINDS, CONTEXT_KIND)
CI_SUCCESS = {"SUCCESS"}
CI_FAILURE = {"FAILURE", "ERROR"}
# A returning contributor was also active in this many preceding weeks.
RETURN_WEEKS = 12
# Mergers are counted over this many weeks before the policy.
MERGER_WEEKS = 26
# Committer accounts that stand for GitHub itself, not a maintainer.
SERVICE_COMMITTERS = {"web-flow", "github"}
# Issues closed as not planned, including duplicates; other reasons count only
# toward the total.
NOT_PLANNED_REASONS = {"NOT_PLANNED", "DUPLICATE"}
# How long after a PR closes a commit naming it still counts as landing it.
LANDING_GRACE = timedelta(days=7)
# A star history that only disagrees with the live count is still usable.
STAR_RECONCILIATIONS = {"matched", "mismatch", "changed_during_run"}


@dataclass
class Source:
    """One repository's record from the run chosen for one collector."""

    run_id: str
    record: dict
    # None for context, which is not a time series.
    observed_through: date | None


def run_dirs(interim: Path, kind: str) -> list[Path]:
    """Completed runs of one collector, newest first (run IDs sort by time)."""
    root = interim / kind
    if not root.is_dir():
        return []
    return sorted(
        (d for d in root.iterdir() if (d / "summary.json").is_file()),
        key=lambda d: d.name,
        reverse=True,
    )


# Per record list: (login field, bot flag) pairs to re-flag by login.
BOT_FIELDS = {
    "pulls": [("author", "author_is_bot"), ("merged_by", "merged_by_is_bot")],
    "issues": [("author", "author_is_bot")],
    "comments": [("author", "author_is_bot")],
    "posts": [("author", "author_is_bot")],
    "commits": [("login", "is_bot")],
    "people": [("person", "is_bot")],
}


def read_record(run: Path, repository: str) -> dict | None:
    path = run / "repositories" / (repository.replace("/", "__") + ".json")
    if not path.is_file():
        return None
    record = json.loads(path.read_text(encoding="utf-8"))
    for key, fields in BOT_FIELDS.items():
        for row in record.get(key) or []:
            for login, flag in fields:
                if flag in row and is_bot_login(row.get(login)):
                    row[flag] = True
    return record


def issues_disabled_only(record: dict) -> bool:
    """Partial only because the repository has no issue tracker."""
    errors = record.get("errors") or []
    return bool(errors) and all(e.get("error") == "issues disabled" for e in errors)


def usable(kind: str, record: dict, anchor: date) -> bool:
    if kind == "star_history":
        return not record.get("errors") and (
            record.get("status") == "complete"
            or record.get("reconciliation") in STAR_RECONCILIATIONS
        )
    if kind == "first_seen" and record.get("status") == "partial":
        # Commits and PRs were read in full; there were no issues to read.
        return issues_disabled_only(record)
    if kind in HISTORY_KINDS:
        return record.get("status") == "complete"
    # A run made with another anchor has different windows; never mix them.
    return (
        record.get("status") == "complete"
        and record.get("anchor_date") == anchor.isoformat()
    )


def newest_source(
    interim: Path, kind: str, repository: str, anchor: date
) -> Source | None:
    """The newest run in which this repository is usable for this anchor."""
    for run in run_dirs(interim, kind):
        record = read_record(run, repository)
        if record is None or not usable(kind, record, anchor):
            continue
        if kind == "star_history":
            through = parse_time(record["stars_after_retrieved_at"]).date()
        elif kind == "fork_history":
            through = parse_time(record["forks_retrieved_at"]).date()
        elif kind == "first_seen":
            # First contributions after the run started were not read.
            through = parse_time(record["started_at"]).date() - timedelta(days=1)
        elif kind == CONTEXT_KIND:
            through = None
        else:
            through = date.fromisoformat(record["window_end"])
        return Source(run.name, record, through)
    return None


def pull_reviews(raw_requests: Path, repository: str) -> list[dict] | None:
    """Reviews by people other than the PR author, from a pulls run's raw pages.

    Reviewer logins are kept only in the raw GraphQL responses. Pages are read in
    request order so the sweep's re-read of the list head replaces older copies.
    Returns None when the raw responses are not on this machine.
    """
    journal = raw_requests / "requests.jsonl"
    if not journal.is_file():
        return None
    owner, name = repository.lower().split("/")
    nodes: dict[int, dict] = {}
    with journal.open(encoding="utf-8") as stream:
        for line in stream:
            entry = json.loads(line)
            variables = (entry.get("payload") or {}).get("variables") or {}
            if (
                entry.get("status") != 200
                or str(variables.get("owner", "")).lower() != owner
                or str(variables.get("name", "")).lower() != name
            ):
                continue
            data = json.loads((raw_requests / entry["file"]).read_text("utf-8"))
            connection = (
                ((data.get("data") or {}).get("repository") or {}).get("pullRequests")
            ) or {}
            for node in connection.get("nodes") or []:
                nodes[node["number"]] = node
    reviews = []
    for node in nodes.values():
        author = ((node.get("author") or {}).get("login") or "").lower()
        for review in (node.get("reviews") or {}).get("nodes") or []:
            login = ((review.get("author") or {}).get("login") or "").lower()
            if (
                review.get("submittedAt")
                and login
                and login != author
                and not is_bot_actor(review.get("author"))
            ):
                reviews.append({"login": login, "submitted_at": review["submittedAt"]})
    return reviews


class Weeks:
    """Maps timestamps to event weeks within one repository's observed panel."""

    def __init__(self, anchor: date, end: date):
        self.anchor = anchor
        self.end = end
        last = min(WEEKS_AFTER - 1, (end - anchor).days // 7)
        self.indexes = range(-WEEKS_BEFORE, last + 1)

    def start(self, week: int) -> date:
        return self.anchor + timedelta(days=7 * week)

    def of(self, value: str | None) -> int | None:
        moment = parse_time(value)
        if moment is None or moment.date() > self.end:
            return None
        week = (moment.date() - self.anchor).days // 7
        return week if week in self.indexes else None

    def days_observed(self, week: int) -> int:
        return min(7, (self.end - self.start(week)).days + 1)


def count_by(rows, week_of) -> dict[int, int]:
    counts: dict[int, int] = defaultdict(int)
    for row in rows:
        week = week_of(row)
        if week is not None:
            counts[week] += 1
    return counts


def medians_by(rows, week_of, value_of) -> dict[int, float | None]:
    values: dict[int, list[float]] = defaultdict(list)
    for row in rows:
        week, value = week_of(row), value_of(row)
        if week is not None and value is not None:
            values[week].append(value)
    return {week: median(v) for week, v in values.items()}


def people_by(rows, week_of, who) -> dict[int, set[str]]:
    people: dict[int, set[str]] = defaultdict(set)
    for row in rows:
        week = week_of(row)
        if week is not None and who(row):
            people[week].add(who(row))
    return people


def pull_columns(record: dict, weeks: Weeks) -> tuple[dict, dict]:
    humans = [r for r in record["pulls"] if not r["author_is_bot"]]

    def opened(r):
        return weeks.of(r["created_at"])

    def merged(r):
        return weeks.of(r["merged_at"])

    def unmerged_close(r):
        return None if r["merged_at"] else weeks.of(r["closed_at"])

    def first_review(r):
        return weeks.of(r.get("first_review_at"))

    def merge_hours(r):
        # Merges after the panel end were not observed for this panel.
        if merged(r) is None:
            return None
        return hours_between(parse_time(r["created_at"]), parse_time(r["merged_at"]))

    def review_hours(r):
        if first_review(r) is None:
            return None
        return hours_between(
            parse_time(r["created_at"]), parse_time(r["first_review_at"])
        )

    def size(r):
        if r.get("additions") is None or r.get("deletions") is None:
            return None
        return r["additions"] + r["deletions"]

    columns = {
        "prs_opened": count_by(humans, opened),
        "prs_merged": count_by(humans, merged),
        "prs_closed_unmerged": count_by(humans, unmerged_close),
        "median_hours_to_merge_by_opened": medians_by(humans, opened, merge_hours),
        "median_hours_to_merge_by_merged": medians_by(humans, merged, merge_hours),
        "median_pr_lines_by_opened": medians_by(humans, opened, size),
        "median_pr_files_by_opened": medians_by(
            humans, opened, lambda r: r.get("changed_files")
        ),
        "median_pr_commits_by_opened": medians_by(
            humans, opened, lambda r: r.get("commits")
        ),
        **ci_counts(humans, opened, "prs"),
    }
    if record.get("reviews_collected"):
        columns["median_hours_to_first_review_by_opened"] = medians_by(
            humans, opened, review_hours
        )
        columns["median_hours_to_first_review_by_reviewed"] = medians_by(
            humans, first_review, review_hours
        )
        columns["median_reviews_per_pr_by_opened"] = medians_by(
            humans, opened, lambda r: r.get("reviews_by_others")
        )
        columns["prs_changes_requested"] = count_by(
            [r for r in humans if r.get("changes_requested")], opened
        )
    if record.get("open_prs_collected"):
        columns["open_prs"] = open_backlog(humans, weeks)
    authors = people_by(humans, opened, lambda r: (r["author"] or "").lower())
    return columns, authors


def ci_counts(rows: list[dict], week_of, prefix: str) -> dict:
    """Passing and failing CI results; none when no item reports CI at all."""
    if not any(r.get("ci_state") for r in rows):
        return {}
    return {
        f"{prefix}_ci_success": count_by(
            [r for r in rows if r.get("ci_state") in CI_SUCCESS], week_of
        ),
        f"{prefix}_ci_failure": count_by(
            [r for r in rows if r.get("ci_state") in CI_FAILURE], week_of
        ),
    }


def open_backlog(pulls: list[dict], weeks: Weeks) -> dict[int, int]:
    """PRs open at the end of each week: opened by then and not yet closed."""
    spans = [
        (
            parse_time(r["created_at"]).date(),
            parse_time(r["closed_at"]).date() if r["closed_at"] else None,
        )
        for r in pulls
    ]
    backlog = {}
    for week in weeks.indexes:
        last = min(weeks.start(week) + timedelta(days=6), weeks.end)
        backlog[week] = sum(
            opened <= last and (closed is None or closed > last)
            for opened, closed in spans
        )
    return backlog


def is_person(login: str | None) -> bool:
    login = (login or "").lower()
    return bool(login) and not is_bot_login(login) and login not in SERVICE_COMMITTERS


def landing_times(pulls: dict, churn: dict) -> dict[int, dict] | None:
    """When and by whom each closed human PR landed: merged, or by a commit naming it.

    A commit on the default branch that names a closed, unmerged PR lands it when
    committed between the PR's opening and LANDING_GRACE after its closing; the
    earliest such commit is the landing time and its committer lands it. None
    when the churn run predates the collection of PR references.
    """
    commits = churn["commits"]
    if any("referenced_prs" not in c for c in commits):
        return None
    named: dict[int, list] = defaultdict(list)
    for commit in commits:
        for number in commit["referenced_prs"]:
            named[number].append(
                (parse_time(commit["committed_at"]), commit.get("committer_login"))
            )
    landed = {}
    for pr in pulls["pulls"]:
        if pr["merged_at"]:
            landed[pr["number"]] = {"at": pr["merged_at"], "by": pr.get("merged_by")}
        elif pr["closed_at"]:
            opened, closed = parse_time(pr["created_at"]), parse_time(pr["closed_at"])
            times = [
                (t, by)
                for t, by in named[pr["number"]]
                if opened <= t <= closed + LANDING_GRACE
            ]
            if times:
                moment, by = min(times, key=lambda pair: pair[0])
                landed[pr["number"]] = {"at": moment.isoformat(), "by": by}
    return landed


def landing_columns(pulls: dict, landed: dict[int, dict], weeks: Weeks) -> dict:
    humans = [r for r in pulls["pulls"] if not r["author_is_bot"]]

    def opened(r):
        return weeks.of(r["created_at"])

    def landed_week(r):
        return weeks.of((landed.get(r["number"]) or {}).get("at"))

    def unlanded_close(r):
        return None if r["number"] in landed else weeks.of(r["closed_at"])

    def land_hours(r):
        if landed_week(r) is None:
            return None
        return hours_between(
            parse_time(r["created_at"]), parse_time(landed[r["number"]]["at"])
        )

    return {
        "prs_landed": count_by(humans, landed_week),
        "prs_closed_unlanded": count_by(humans, unlanded_close),
        "median_hours_to_land_by_opened": medians_by(humans, opened, land_hours),
        "median_hours_to_land_by_landed": medians_by(humans, landed_week, land_hours),
    }


def mergers_before(landings: dict[int, dict], weeks: Weeks) -> int:
    """People who merged or landed PRs in the MERGER_WEEKS before the policy."""
    people = set()
    for entry in landings.values():
        week = weeks.of(entry["at"])
        if week is not None and -MERGER_WEEKS <= week < 0 and is_person(entry["by"]):
            people.add(entry["by"].lower())
    return len(people)


def issue_columns(record: dict, weeks: Weeks) -> tuple[dict, dict]:
    humans = [r for r in record["issues"] if not r["author_is_bot"]]

    def opened(r):
        return weeks.of(r["created_at"])

    def closed(r):
        return weeks.of(r["closed_at"])

    def close_hours(r):
        if closed(r) is None:
            return None
        return hours_between(parse_time(r["created_at"]), parse_time(r["closed_at"]))

    columns = {
        "issues_opened": count_by(humans, opened),
        "issues_closed": count_by(humans, closed),
        "issues_closed_completed": count_by(
            [r for r in humans if r["state_reason"] == "COMPLETED"], closed
        ),
        "issues_closed_not_planned": count_by(
            [r for r in humans if r["state_reason"] in NOT_PLANNED_REASONS], closed
        ),
        "median_hours_to_close_by_opened": medians_by(humans, opened, close_hours),
        "median_hours_to_close_by_closed": medians_by(humans, closed, close_hours),
    }
    authors = people_by(humans, opened, lambda r: (r["author"] or "").lower())
    return columns, authors


def release_columns(record: dict, weeks: Weeks) -> dict:
    def published(r):
        return weeks.of(r["published_at"])

    return {
        "releases": count_by(record["releases"], published),
        "stable_releases": count_by(
            [r for r in record["releases"] if not r["prerelease"]], published
        ),
    }


def churn_columns(record: dict, weeks: Weeks) -> tuple[dict, dict]:
    counted = [c for c in record["commits"] if not c["is_bot"] and not c["is_merge"]]

    def committed(c):
        return weeks.of(c["committed_at"])

    def lines(c):
        if c["additions"] is None or c["deletions"] is None:
            return None
        return c["additions"] + c["deletions"]

    added: dict[int, int] = defaultdict(int)
    deleted: dict[int, int] = defaultdict(int)
    files: dict[int, int] = defaultdict(int)
    for commit in counted:
        week = committed(commit)
        if week is not None:
            added[week] += commit["additions"] or 0
            deleted[week] += commit["deletions"] or 0
            files[week] += commit.get("changed_files") or 0
    columns = {
        "commits": count_by(counted, committed),
        "lines_added": added,
        "lines_deleted": deleted,
        "files_changed": files,
        "median_lines_per_commit": medians_by(counted, committed, lines),
        # The branch's CI state concerns every commit on it, merges and bots too.
        **ci_counts(record["commits"], committed, "commits"),
    }
    # Logins are case-insensitive; unlinked authors keep their hashed-email ID.
    authors = people_by(counted, committed, lambda c: c["contributor_id"].lower())
    return columns, authors


def ci_lifecycle_columns(record: dict, weeks: Weeks) -> dict:
    """CI across each human PR's pushes, by the week the PR was opened."""
    tested = [r for r in record["pulls"] if not r["author_is_bot"] and r["ci_results"]]
    if not tested:
        return {}

    def opened(r):
        return weeks.of(r["created_at"])

    return {
        "prs_with_ci": count_by(tested, opened),
        "prs_first_ci_failed": count_by(
            [r for r in tested if r["first_ci_state"] in CI_FAILURE], opened
        ),
        "prs_ci_fixed": count_by([r for r in tested if r["ci_fixed"]], opened),
        "median_ci_failures_per_pr_by_opened": medians_by(
            tested, opened, lambda r: r["ci_failures"]
        ),
    }


def comment_columns(record: dict, weeks: Weeks) -> tuple[dict, dict]:
    humans = [c for c in record["comments"] if not c["author_is_bot"]]

    def posted(c):
        return weeks.of(c["created_at"])

    columns = {
        "pr_comments": count_by([c for c in humans if c["on_pull_request"]], posted),
        "issue_comments": count_by(
            [c for c in humans if not c["on_pull_request"]], posted
        ),
    }
    return columns, people_by(humans, posted, lambda c: (c["author"] or "").lower())


def discussion_columns(record: dict, weeks: Weeks) -> tuple[dict, dict]:
    humans = [p for p in record["posts"] if not p["author_is_bot"]]

    def posted(p):
        return weeks.of(p["created_at"])

    columns = {
        "discussions_opened": count_by(
            [p for p in humans if p["kind"] == "discussion"], posted
        ),
        "discussion_comments": count_by(
            [p for p in humans if p["kind"] != "discussion"], posted
        ),
    }
    return columns, people_by(humans, posted, lambda p: (p["author"] or "").lower())


def newcomer_columns(record: dict, weeks: Weeks) -> dict:
    """People whose first-ever authored commit, PR, or issue falls in each week."""
    newcomers: dict[int, int] = defaultdict(int)
    first_pr: dict[int, int] = defaultdict(int)
    for person in record["people"]:
        if person["is_bot"]:
            continue
        dates = [
            person[k] for k in ("commit", "pull_request", "issue") if person.get(k)
        ]
        week = weeks.of(min(dates)) if dates else None
        if week is not None:
            newcomers[week] += 1
        week = weeks.of(person.get("pull_request"))
        if week is not None:
            first_pr[week] += 1
    return {"newcomers": newcomers, "first_time_pr_authors": first_pr}


def cumulative_on(daily: list[tuple[date, int, int]], day: date) -> int:
    """The cumulative count on a day, from oldest-first (date, new, cumulative)."""
    position = bisect_right([on for on, _, _ in daily], day)
    return daily[position - 1][2] if position else 0


def history_columns(record: dict, weeks: Weeks, new_key: str, total_key: str) -> dict:
    days = sorted(
        (date.fromisoformat(d["date"]), d[new_key], d[total_key])
        for d in record["daily"]
    )
    new: dict[int, int] = defaultdict(int)
    for on, count, _ in days:
        week = weeks.of(on.isoformat())
        if week is not None:
            new[week] += count
    # The count at the last observed day of each week; earlier days carry forward.
    total = {
        week: cumulative_on(days, min(weeks.start(week) + timedelta(days=6), weeks.end))
        for week in weeks.indexes
    }
    return {new_key: new, total_key: total}


def star_columns(record: dict, weeks: Weeks) -> dict:
    return history_columns(record, weeks, "new_stars", "cumulative_stars")


def fork_columns(record: dict, weeks: Weeks) -> dict:
    return history_columns(record, weeks, "new_forks", "cumulative_forks")


COUNT_COLUMNS = [
    # Throughput and backlog
    "prs_opened",
    "prs_merged",
    "prs_closed_unmerged",
    "prs_landed",
    "prs_closed_unlanded",
    "issues_opened",
    "issues_closed",
    "issues_closed_completed",
    "issues_closed_not_planned",
    "releases",
    "stable_releases",
    # Code churn
    "commits",
    "lines_added",
    "lines_deleted",
    "files_changed",
    # Review and CI
    "prs_changes_requested",
    "prs_ci_success",
    "prs_ci_failure",
    "commits_ci_success",
    "commits_ci_failure",
    "prs_with_ci",
    "prs_first_ci_failed",
    "prs_ci_fixed",
    # Conversation
    "pr_comments",
    "issue_comments",
    "discussions_opened",
    "discussion_comments",
    # Popularity
    "new_stars",
    "new_forks",
    # Newcomers
    "newcomers",
    "first_time_pr_authors",
]
VALUE_COLUMNS = [
    "open_prs",
    "median_pr_lines_by_opened",
    "median_pr_files_by_opened",
    "median_pr_commits_by_opened",
    "median_reviews_per_pr_by_opened",
    "median_ci_failures_per_pr_by_opened",
    "median_lines_per_commit",
    "median_hours_to_merge_by_opened",
    "median_hours_to_merge_by_merged",
    "median_hours_to_land_by_opened",
    "median_hours_to_land_by_landed",
    "median_hours_to_first_review_by_opened",
    "median_hours_to_first_review_by_reviewed",
    "median_hours_to_close_by_opened",
    "median_hours_to_close_by_closed",
    "cumulative_stars",
    "cumulative_forks",
]
# The components of active_contributors, and the source each needs.
PEOPLE_COLUMNS = {
    "commit_authors": "churn",
    "pr_authors": "pull_requests",
    "issue_authors": "issues",
    "reviewers": "pull_requests",
}
# Further people counted by engaged_people only.
ENGAGED_COLUMNS = {"commenters": "comments", "discussion_participants": "discussions"}
PANEL_COLUMNS = [
    "repository",
    "group",
    "anchor_date",
    "anchor_source",
    "event_week",
    "week_start",
    "week_end",
    "post",
    "days_observed",
    *COUNT_COLUMNS,
    *VALUE_COLUMNS,
    "active_contributors",
    *PEOPLE_COLUMNS,
    "returning_contributors",
    "engaged_people",
    *ENGAGED_COLUMNS,
]


def repository_rows(
    repository: str,
    anchor: date,
    anchor_source: str,
    sources: dict[str, Source],
    reviews: list[dict] | None,
) -> tuple[list[dict], date, dict]:
    """All observed weeks for one repository, the panel end, and per-repo facts."""
    end = min(
        min(s.observed_through for s in sources.values() if s.observed_through),
        anchor + timedelta(days=7 * WEEKS_AFTER - 1),
    )
    weeks = Weeks(anchor, end)
    columns: dict[str, dict] = {}
    people: dict[str, dict[int, set[str]]] = {}
    if "pull_requests" in sources:
        found, people["pr_authors"] = pull_columns(
            sources["pull_requests"].record, weeks
        )
        columns.update(found)
        if reviews is not None:
            people["reviewers"] = people_by(
                reviews, lambda r: weeks.of(r["submitted_at"]), lambda r: r["login"]
            )
    if "issues" in sources:
        found, people["issue_authors"] = issue_columns(sources["issues"].record, weeks)
        columns.update(found)
    elif (
        CONTEXT_KIND in sources
        and sources[CONTEXT_KIND].record.get("has_issues") is False
    ):
        # No tracker: nobody authored issues, so the contributor counts stay
        # whole, while the issue outcomes themselves stay blank.
        people["issue_authors"] = {}
    if "releases" in sources:
        columns.update(release_columns(sources["releases"].record, weeks))
    if "churn" in sources:
        found, people["commit_authors"] = churn_columns(sources["churn"].record, weeks)
        columns.update(found)
    if "comments" in sources:
        found, people["commenters"] = comment_columns(sources["comments"].record, weeks)
        columns.update(found)
    if "discussions" in sources:
        found, people["discussion_participants"] = discussion_columns(
            sources["discussions"].record, weeks
        )
        columns.update(found)
    if "ci" in sources:
        columns.update(ci_lifecycle_columns(sources["ci"].record, weeks))
    if "first_seen" in sources:
        columns.update(newcomer_columns(sources["first_seen"].record, weeks))
    if "star_history" in sources:
        columns.update(star_columns(sources["star_history"].record, weeks))
    if "fork_history" in sources:
        columns.update(fork_columns(sources["fork_history"].record, weeks))
    facts: dict = {"landing": False, "prs_landed_by_commit": None}
    if "pull_requests" in sources:
        pulls = sources["pull_requests"].record
        landed = (
            landing_times(pulls, sources["churn"].record)
            if "churn" in sources
            else None
        )
        if landed is not None:
            columns.update(landing_columns(pulls, landed, weeks))
            facts.update(
                landing=True,
                prs_landed_by_commit=sum(
                    not r["merged_at"] and r["number"] in landed
                    for r in pulls["pulls"]
                    if not r["author_is_bot"]
                ),
            )
        if all("merged_by" in r for r in pulls["pulls"]):
            merges = {
                r["number"]: {"at": r["merged_at"], "by": r["merged_by"]}
                for r in pulls["pulls"]
                if r["merged_at"]
            }
            facts["mergers_pre_policy"] = mergers_before(landed or merges, weeks)
            facts["mergers_include_landing"] = landed is not None

    # A source that ran counts unobserved weeks as zero; absent ones stay blank.
    zero_filled = set(COUNT_COLUMNS) & set(columns)
    active = all(name in people for name in PEOPLE_COLUMNS)
    engaged = active and all(name in people for name in ENGAGED_COLUMNS)

    def union(names, week):
        return set().union(*(people[name].get(week, set()) for name in names))

    rows = []
    for week in weeks.indexes:
        start = weeks.start(week)
        row = {
            "repository": repository,
            "anchor_date": anchor.isoformat(),
            "anchor_source": anchor_source,
            "event_week": week,
            "week_start": start.isoformat(),
            "week_end": (start + timedelta(days=6)).isoformat(),
            "post": int(week >= 0),
            "days_observed": weeks.days_observed(week),
        }
        for name, by_week in columns.items():
            row[name] = by_week.get(week, 0 if name in zero_filled else None)
        for name in (*PEOPLE_COLUMNS, *ENGAGED_COLUMNS):
            if name in people:
                row[name] = len(people[name].get(week, ()))
        # The broad counts need every component, or they would quietly undercount.
        if active:
            current = union(PEOPLE_COLUMNS, week)
            row["active_contributors"] = len(current)
            # Weeks without RETURN_WEEKS observed weeks before them stay blank.
            if week - RETURN_WEEKS >= weeks.indexes[0]:
                earlier = set().union(
                    *(
                        union(PEOPLE_COLUMNS, w)
                        for w in range(week - RETURN_WEEKS, week)
                    )
                )
                row["returning_contributors"] = len(current & earlier)
        if engaged:
            row["engaged_people"] = len(
                union((*PEOPLE_COLUMNS, *ENGAGED_COLUMNS), week)
            )
        rows.append(row)
    return rows, end, facts


def read_manual(path: Path) -> dict[str, dict]:
    """A manual CSV keyed by lower-case repository; empty when the file is absent."""
    if not path.is_file():
        return {}
    with path.open(newline="", encoding="utf-8-sig") as stream:
        return {row["repository"].lower(): row for row in csv.DictReader(stream)}


def years_between(start: str | None, end: date) -> float | None:
    moment = parse_time(start)
    return round((end - moment.date()).days / 365.25, 2) if moment else None


def daily_series(record: dict, new_key: str, total_key: str) -> list[tuple]:
    return sorted(
        (date.fromisoformat(d["date"]), d[new_key], d[total_key])
        for d in record["daily"]
    )


CHARACTERISTIC_COLUMNS = [
    "repository",
    "group",
    "matched_policy_repo",
    "date_cluster",
    "anchor_date",
    "policy_type",
    "policy_type_coded_by",
    "repository_created_at",
    "age_at_policy_years",
    "primary_language",
    "license_spdx_id",
    "has_discussions",
    "stars_at_policy",
    "forks_at_policy",
    "owner_login",
    "owner_type",
    "owner_created_at",
    "owner_age_at_policy_years",
    "owner_public_repos",
    "owner_followers",
    "owner_public_members",
    "owner_is_verified",
    "maintainer_file_user_count",
    "maintainer_file_team_count",
    "mergers_pre_policy",
    "mergers_include_landing",
    "maintainer_count_manual",
    "context_run",
]


def characteristics_row(
    repository: str,
    anchor: date,
    sources: dict[str, Source],
    facts: dict,
    coding: dict,
    institutional: dict,
) -> dict:
    """Repository-level characteristics as of the policy date, where possible."""
    before = anchor - timedelta(days=1)
    row = {
        "repository": repository,
        "anchor_date": anchor.isoformat(),
        "policy_type": coding.get("policy_type") or None,
        "policy_type_coded_by": coding.get("coded_by") or None,
        "mergers_pre_policy": facts.get("mergers_pre_policy"),
        "mergers_include_landing": facts.get("mergers_include_landing"),
        "maintainer_count_manual": institutional.get("maintainer_count") or None,
    }
    if "star_history" in sources:
        row["stars_at_policy"] = cumulative_on(
            daily_series(
                sources["star_history"].record, "new_stars", "cumulative_stars"
            ),
            before,
        )
    if "fork_history" in sources:
        row["forks_at_policy"] = cumulative_on(
            daily_series(
                sources["fork_history"].record, "new_forks", "cumulative_forks"
            ),
            before,
        )
    if CONTEXT_KIND in sources:
        context = sources[CONTEXT_KIND].record
        row.update(
            {
                key: context.get(key)
                for key in CHARACTERISTIC_COLUMNS
                if key in context and key not in row
            },
            age_at_policy_years=years_between(
                context.get("repository_created_at"), anchor
            ),
            owner_age_at_policy_years=years_between(
                context.get("owner_created_at"), anchor
            ),
            context_run=sources[CONTEXT_KIND].run_id,
        )
    return row


def build_panel(
    study: StudyConfig,
    candidates_path: Path,
    *,
    policy_run: Path | None = None,
    priority: str | None = None,
    comparison_path: Path | None = None,
    progress=print,
) -> tuple[Path, dict]:
    event = study.treatment.anchor_event
    require_policy_run(event, policy_run)
    candidates = [
        {**c, "group": "policy"}
        for c in select_candidates(candidates_path, priority, None)
    ]
    if comparison_path is not None:
        policy_names = {c["repository"].lower() for c in candidates}
        for row in select_candidates(comparison_path, priority, None):
            if not row.get("stand_in_date"):
                raise ValueError(
                    f"{row['repository']}: comparison row lacks stand_in_date"
                )
            if row["repository"].lower() in policy_names:
                raise ValueError(f"{row['repository']} is in both files")
            candidates.append({**row, "group": "comparison"})
    detected = read_detected_dates(policy_run)
    # Policy types are coded in the policy list's policy_type column.
    coding = {c["repository"].lower(): c for c in candidates if c.get("policy_type")}
    institutional = read_manual(study.data.manual / "institutional_reviews.csv")
    run_id = new_run_id()
    output = study.data.processed / "panels" / run_id
    rows: list[dict] = []
    repositories: list[dict] = []
    characteristics: list[dict] = []
    for index, candidate in enumerate(candidates, 1):
        name = candidate["repository"]
        progress(f"[{index}/{len(candidates)}] {name}")
        anchor, anchor_source = anchor_dates(candidate, detected, event)
        entry = {
            "requested_repository": name,
            "group": candidate["group"],
            "anchor_date": None,
            "weeks": 0,
        }
        repositories.append(entry)
        if anchor is None:
            entry["status"] = "no_anchor"
            progress("  skipped: no anchor date")
            continue
        sources = {
            kind: source
            for kind in ALL_KINDS
            if (source := newest_source(study.data.interim, kind, name, anchor))
        }
        if not any(s.observed_through for s in sources.values()):
            entry["status"] = "no_data"
            progress("  skipped: no usable runs")
            continue
        reviews = None
        if "pull_requests" in sources:
            pulls = sources["pull_requests"]
            reviews = pull_reviews(
                study.data.raw / "pull_requests" / pulls.run_id / "requests",
                pulls.record["repository"],
            )
        repository = next(iter(sources.values())).record["repository"]
        found, end, facts = repository_rows(
            repository, anchor, anchor_source, sources, reviews
        )
        rows.extend({**row, "group": candidate["group"]} for row in found)
        characteristics.append(
            characteristics_row(
                repository,
                anchor,
                sources,
                facts,
                coding.get(name.lower(), {}),
                institutional.get(name.lower(), {}),
            )
            | {
                "group": candidate["group"],
                "matched_policy_repo": candidate.get("matched_policy_repo") or None,
                "date_cluster": candidate.get("date_cluster") or None,
            }
        )
        entry.update(
            repository=repository,
            group=candidate["group"],
            status="complete" if len(sources) == len(ALL_KINDS) else "partial",
            anchor_date=anchor.isoformat(),
            anchor_source=anchor_source,
            panel_end=end.isoformat(),
            weeks=len(found),
            missing_sources=sorted(set(ALL_KINDS) - sources.keys()),
            reviewers=reviews is not None,
            landing=facts["landing"],
            prs_landed_by_commit=facts["prs_landed_by_commit"],
            **{f"{kind}_run": s.run_id for kind, s in sources.items()},
            **{
                f"{kind}_through": s.observed_through.isoformat()
                for kind, s in sources.items()
                if s.observed_through
            },
        )
        progress(
            f"  {entry['status']}; anchor {entry['anchor_date']}; "
            f"through {entry['panel_end']}; {len(found)} weeks"
            + ("" if reviews is not None else "; reviewers unavailable (no raw data)")
            + (
                f"; missing: {', '.join(entry['missing_sources'])}"
                if entry["missing_sources"]
                else ""
            )
        )
    output.mkdir(parents=True, exist_ok=False)
    write_csv(output / "panel.csv", PANEL_COLUMNS, rows)
    write_csv(
        output / "repositories.csv",
        list(dict.fromkeys(k for r in repositories for k in r)),
        [
            {k: ";".join(v) if isinstance(v, list) else v for k, v in r.items()}
            for r in repositories
        ],
    )
    write_csv(output / "characteristics.csv", CHARACTERISTIC_COLUMNS, characteristics)
    manifest = {
        "schema_version": 2,
        "run_id": run_id,
        "built_at": utc_now(),
        "candidates_file": str(candidates_path),
        "priority": priority,
        "anchor_event": event.value,
        "policy_run": str(policy_run) if policy_run else None,
        "weeks_before": WEEKS_BEFORE,
        "weeks_after": WEEKS_AFTER,
        "rows": len(rows),
        "repositories": len(repositories),
        "complete": sum(r.get("status") == "complete" for r in repositories),
        "partial": sum(r.get("status") == "partial" for r in repositories),
        "skipped": sum(
            r.get("status") in {"no_anchor", "no_data"} for r in repositories
        ),
        "policy_types_coded": sum(bool(r["policy_type"]) for r in characteristics),
        "output_directory": str(output),
    }
    write_json(output / "summary.json", manifest)
    return output, manifest
