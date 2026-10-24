"""Offline tests for the repository-week panel builder."""

import csv
import json
import tempfile
import unittest
from pathlib import Path

from ai_policy_impact.config import DataPathConfig, StudyConfig
from ai_policy_impact.activity import is_bot_login
from ai_policy_impact.panel import build_panel, read_record

ANCHOR = "2025-11-20"  # a Thursday; event weeks start on Thursdays


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def record(**fields):
    return {
        "requested_repository": "a/b",
        "repository": "a/b",
        "status": "complete",
        "errors": [],
        "anchor_date": ANCHOR,
        "window_end": "2025-12-10",
        **fields,
    }


def pr(number, created, merged=None, author="dev", bot=False, review=None, **extra):
    return {
        "number": number,
        "author": author,
        "author_is_bot": bot,
        "created_at": created,
        "merged_at": merged,
        "closed_at": merged,
        "first_review_at": review,
        "merged_by": "Maint" if merged else None,
        **extra,
    }


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.interim, self.raw = root / "interim", root / "raw"
        manual = root / "manual"
        self.study = StudyConfig(
            data=DataPathConfig(
                manual=manual,
                interim=self.interim,
                raw=self.raw,
                processed=root / "processed",
            )
        )
        manual.mkdir()
        (manual / "institutional_reviews.csv").write_text(
            "repository,maintainer_count\na/b,3\n", encoding="utf-8"
        )
        self.candidates = root / "candidates.csv"
        with self.candidates.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(
                ["repository", "priority", "policy_paths", "reported_policy_date",
                 "policy_type"]
            )
            writer.writerow(["a/b", "pilot", "AI.md", ANCHOR, "restrictive"])
        self.policy_run = self.interim / "policy_history" / "run"
        self.policy_run.mkdir(parents=True)
        (self.policy_run / "policy_events.csv").write_text(
            f"requested_repository,detected_policy_date\na/b,{ANCHOR}\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, kind, run_id, rec):
        run = self.interim / kind / run_id
        write(run / "summary.json", {"run_id": run_id})
        write(run / "repositories" / "a__b.json", rec)

    def add_review_pages(self, run_id, nodes):
        requests = self.raw / "pull_requests" / run_id / "requests"
        write(
            requests / "000001.json",
            {"data": {"repository": {"pullRequests": {"nodes": nodes}}}},
        )
        entry = {
            "status": 200,
            "payload": {"variables": {"owner": "a", "name": "b"}},
            "file": "000001.json",
        }
        (requests / "requests.jsonl").write_text(json.dumps(entry) + "\n")

    def build(self):
        output, summary = build_panel(
            self.study,
            self.candidates,
            policy_run=self.policy_run,
            progress=lambda _: None,
        )
        with (output / "panel.csv").open(encoding="utf-8") as stream:
            rows = {int(r["event_week"]): r for r in csv.DictReader(stream)}
        with (output / "characteristics.csv").open(encoding="utf-8") as stream:
            self.characteristics = list(csv.DictReader(stream))
        return rows, summary

    def add_all_sources(self, pr_run="p2"):
        self.add(
            "pull_requests",
            pr_run,
            record(
                reviews_collected=True,
                open_prs_collected=True,
                pulls=[
                    pr(
                        1,
                        "2025-11-20T10:00:00Z",
                        "2025-11-21T10:00:00Z",
                        review="2025-11-20T12:00:00Z",
                        additions=10,
                        deletions=4,
                        changed_files=2,
                        commits=3,
                        reviews_by_others=2,
                        changes_requested=1,
                        ci_state="SUCCESS",
                    ),
                    pr(
                        2,
                        "2025-11-21T00:00:00Z",
                        author="Bot[bot]",
                        bot=True,
                        ci_state="FAILURE",
                    ),
                    # Merged after the panel end: unobserved, not merged.
                    pr(3, "2025-12-01T00:00:00Z", "2025-12-20T00:00:00Z"),
                    # Still open: part of the backlog from week -8 on.
                    pr(6, "2025-10-01T00:00:00Z", ci_state="ERROR"),
                    # Merged in week -12 by a maintainer.
                    pr(7, "2025-09-01T00:00:00Z", "2025-09-02T00:00:00Z"),
                ],
            ),
        )
        self.add_review_pages(
            pr_run,
            [
                {
                    "number": 1,
                    "author": {"login": "dev"},
                    "reviews": {
                        "nodes": [
                            {
                                "author": {"__typename": "User", "login": "Rev"},
                                "submittedAt": "2025-11-20T12:00:00Z",
                            },
                            {
                                "author": {"__typename": "Bot", "login": "ci"},
                                "submittedAt": "2025-11-20T12:00:00Z",
                            },
                            {
                                "author": {"__typename": "User", "login": "dev"},
                                "submittedAt": "2025-11-20T13:00:00Z",
                            },
                        ]
                    },
                }
            ],
        )
        self.add(
            "issues",
            "i1",
            record(
                issues=[
                    {
                        "author": "ann",
                        "author_is_bot": False,
                        "state_reason": "COMPLETED",
                        "created_at": "2025-11-14T00:00:00Z",
                        "closed_at": "2025-11-22T00:00:00Z",
                    },
                    {
                        "author": "x",
                        "author_is_bot": False,
                        "state_reason": "DUPLICATE",
                        "created_at": "2025-11-20T00:00:00Z",
                        "closed_at": "2025-11-20T05:00:00Z",
                    },
                ]
            ),
        )
        self.add(
            "releases",
            "r1",
            record(
                releases=[
                    {
                        "published_at": "2025-11-24T00:00:00Z",
                        "prerelease": False,
                        "author": "github-actions[bot]",
                    },
                    {
                        "published_at": "2025-11-25T00:00:00Z",
                        "prerelease": True,
                        "author": "github-actions[bot]",
                    },
                ]
            ),
        )
        self.add(
            "churn",
            "c1",
            record(
                commits=[
                    {
                        "committed_at": "2025-11-20T00:00:00Z",
                        "contributor_id": "Dev",
                        "is_bot": False,
                        "is_merge": False,
                        "additions": 5,
                        "deletions": 1,
                    },
                    {
                        "committed_at": "2025-11-20T00:00:00Z",
                        "contributor_id": "dev",
                        "is_bot": False,
                        "is_merge": True,
                        "additions": 50,
                        "deletions": 50,
                    },
                    {
                        "committed_at": "2025-11-20T00:00:00Z",
                        "contributor_id": "ci[bot]",
                        "is_bot": True,
                        "is_merge": False,
                        "additions": 9,
                        "deletions": 9,
                    },
                ]
            ),
        )
        self.add(
            "star_history",
            "s1",
            {
                "requested_repository": "a/b",
                "repository": "a/b",
                "status": "partial",
                "errors": [],
                "reconciliation": "mismatch",
                "stars_after_retrieved_at": "2025-12-08T04:00:00+00:00",
                "daily": [
                    {"date": "2020-01-01", "new_stars": 100, "cumulative_stars": 100},
                    {"date": "2025-11-21", "new_stars": 3, "cumulative_stars": 103},
                ],
            },
        )

    def add_engagement_sources(self):
        self.add(
            "comments",
            "m1",
            record(
                comments=[
                    {
                        "on_pull_request": True,
                        "author": "Carol",
                        "author_is_bot": False,
                        "created_at": "2025-11-21T00:00:00Z",
                    },
                    {
                        "on_pull_request": True,
                        "author": "ci[bot]",
                        "author_is_bot": True,
                        "created_at": "2025-11-21T00:00:00Z",
                    },
                    {
                        "on_pull_request": False,
                        "author": "ann",
                        "author_is_bot": False,
                        "created_at": "2025-11-22T00:00:00Z",
                    },
                ]
            ),
        )
        self.add(
            "discussions",
            "d1",
            record(
                discussions_enabled=True,
                posts=[
                    {
                        "kind": "discussion",
                        "author": "dana",
                        "author_is_bot": False,
                        "created_at": "2025-11-23T00:00:00Z",
                    },
                    {
                        "kind": "reply",
                        "author": "ann",
                        "author_is_bot": False,
                        "created_at": "2025-11-24T00:00:00Z",
                    },
                ],
            ),
        )
        history = {
            "requested_repository": "a/b",
            "repository": "a/b",
            "status": "complete",
            "errors": [],
        }
        self.add(
            "first_seen",
            "f1",
            history
            | {
                "started_at": "2025-12-15T00:00:00+00:00",
                "people": [
                    {
                        "person": "dev",
                        "is_bot": False,
                        "commit": "2020-01-01T00:00:00+00:00",
                        "pull_request": "2021-01-01T00:00:00+00:00",
                    },
                    {
                        "person": "x",
                        "is_bot": False,
                        "issue": "2025-11-20T00:00:00+00:00",
                    },
                    {
                        "person": "newbie",
                        "is_bot": False,
                        "pull_request": "2025-11-21T00:00:00+00:00",
                    },
                    {
                        "person": "ci[bot]",
                        "is_bot": True,
                        "commit": "2025-11-20T00:00:00+00:00",
                    },
                ],
            },
        )
        self.add(
            "fork_history",
            "k1",
            history
            | {
                "forks_retrieved_at": "2025-12-20T00:00:00+00:00",
                "daily": [
                    {"date": "2019-01-01", "new_forks": 5, "cumulative_forks": 5},
                    {"date": "2025-11-24", "new_forks": 2, "cumulative_forks": 7},
                ],
            },
        )
        self.add(
            "ci",
            "c1",
            record(
                pulls=[
                    {
                        "number": 1,
                        "author_is_bot": False,
                        "created_at": "2025-11-20T10:00:00Z",
                        "ci_results": 3,
                        "ci_failures": 2,
                        "first_ci_state": "FAILURE",
                        "ci_fixed": True,
                    },
                    {
                        "number": 3,
                        "author_is_bot": False,
                        "created_at": "2025-11-21T00:00:00Z",
                        "ci_results": 1,
                        "ci_failures": 0,
                        "first_ci_state": "SUCCESS",
                        "ci_fixed": False,
                    },
                    {
                        "number": 2,
                        "author_is_bot": True,
                        "created_at": "2025-11-21T00:00:00Z",
                        "ci_results": 1,
                        "ci_failures": 1,
                        "first_ci_state": "FAILURE",
                        "ci_fixed": False,
                    },
                ]
            ),
        )
        self.add(
            "context",
            "x1",
            record(
                repository_created_at="2015-11-20T00:00:00Z",
                primary_language="Rust",
                owner_login="a",
                owner_type="Organization",
                owner_created_at="2010-11-20T00:00:00Z",
                owner_public_repos=12,
                maintainer_file_user_count=4,
                maintainer_file_team_count=1,
            ),
        )

    def test_rq1_rq2_columns_and_characteristics(self):
        self.add_all_sources()
        self.add_engagement_sources()
        rows, summary = self.build()
        self.assertEqual(summary["complete"], 1)
        self.assertEqual(summary["policy_types_coded"], 1)
        week0, week1 = rows[0], rows[1]
        # RQ1: size, review rounds, CI (bots excluded), backlog.
        self.assertEqual(week0["median_pr_lines_by_opened"], "14")
        self.assertEqual(week0["median_pr_commits_by_opened"], "3")
        self.assertEqual(week0["median_reviews_per_pr_by_opened"], "2")
        self.assertEqual(week0["prs_changes_requested"], "1")
        self.assertEqual(week0["prs_ci_success"], "1")
        self.assertEqual(week0["prs_ci_failure"], "0")
        self.assertEqual(rows[-8]["prs_ci_failure"], "1")  # ERROR counts as failure
        self.assertEqual(rows[-9]["open_prs"], "0")
        self.assertEqual(week0["open_prs"], "1")  # PR 6
        self.assertEqual(week1["open_prs"], "2")  # PRs 3 and 6
        # CI across each PR's pushes; the bot PR is excluded.
        self.assertEqual(week0["prs_with_ci"], "2")
        self.assertEqual(week0["prs_first_ci_failed"], "1")
        self.assertEqual(week0["prs_ci_fixed"], "1")
        self.assertEqual(week0["median_ci_failures_per_pr_by_opened"], "1.0")
        # RQ2: comments, discussions, newcomers, engagement.
        self.assertEqual(week0["pr_comments"], "1")
        self.assertEqual(week0["issue_comments"], "1")
        self.assertEqual(week0["commenters"], "2")
        self.assertEqual(week0["discussions_opened"], "1")
        self.assertEqual(week0["discussion_comments"], "1")
        self.assertEqual(week0["discussion_participants"], "2")
        self.assertEqual(week0["newcomers"], "2")  # x and newbie, not the bot
        self.assertEqual(week0["first_time_pr_authors"], "1")
        self.assertEqual(week0["active_contributors"], "3")
        self.assertEqual(week0["engaged_people"], "6")  # + carol, ann, dana
        self.assertEqual(week0["returning_contributors"], "1")  # dev, from week -8
        self.assertEqual(rows[-41]["returning_contributors"], "")
        self.assertEqual(rows[-40]["returning_contributors"], "0")
        self.assertEqual(week0["new_forks"], "2")
        self.assertEqual(week0["cumulative_forks"], "7")
        (row,) = self.characteristics
        self.assertEqual(row["policy_type"], "restrictive")
        self.assertEqual(row["stars_at_policy"], "100")
        self.assertEqual(row["forks_at_policy"], "5")
        self.assertEqual(row["mergers_pre_policy"], "1")  # PR 7's merger
        self.assertEqual(row["maintainer_count_manual"], "3")
        self.assertEqual(row["maintainer_file_user_count"], "4")
        self.assertEqual(row["age_at_policy_years"], "10.0")
        self.assertEqual(row["owner_type"], "Organization")

    def test_weeks_bots_merges_and_panel_end(self):
        self.add_all_sources()
        rows, summary = self.build()
        self.assertEqual(summary["partial"], 1)  # no engagement sources yet
        self.assertEqual(rows[0]["pr_comments"], "")
        self.assertEqual(rows[0]["engaged_people"], "")
        self.assertEqual(rows[0]["newcomers"], "")
        # Stars end 2025-12-08, earlier than the outcome runs: weeks 0..2 only.
        self.assertEqual(min(rows), -52)
        self.assertEqual(max(rows), 2)
        self.assertEqual(rows[2]["days_observed"], "5")
        self.assertEqual(rows[0]["post"], "1")
        self.assertEqual(rows[-1]["post"], "0")
        week0 = rows[0]
        self.assertEqual(week0["prs_opened"], "1")  # bot PR excluded
        self.assertEqual(week0["prs_merged"], "1")
        self.assertEqual(week0["median_hours_to_merge_by_opened"], "24.0")
        self.assertEqual(week0["median_hours_to_first_review_by_opened"], "2.0")
        self.assertEqual(rows[1]["prs_merged"], "0")
        self.assertEqual(rows[1]["median_hours_to_merge_by_opened"], "")
        self.assertEqual(rows[-1]["issues_opened"], "1")
        self.assertEqual(week0["issues_closed"], "2")
        self.assertEqual(week0["issues_closed_completed"], "1")
        self.assertEqual(week0["issues_closed_not_planned"], "1")
        # Bot-published releases count.
        self.assertEqual(week0["releases"], "2")
        self.assertEqual(week0["stable_releases"], "1")
        self.assertEqual(week0["commits"], "1")
        self.assertEqual(week0["lines_added"], "5")
        self.assertEqual(week0["new_stars"], "3")
        self.assertEqual(rows[-52]["cumulative_stars"], "100")
        self.assertEqual(week0["cumulative_stars"], "103")
        # Reviewers exclude bots and self-reviews; logins are case-insensitive.
        self.assertEqual(week0["reviewers"], "1")
        self.assertEqual(week0["commit_authors"], "1")
        self.assertEqual(week0["pr_authors"], "1")
        self.assertEqual(week0["issue_authors"], "1")
        self.assertEqual(week0["active_contributors"], "3")  # dev, x, rev
        self.assertEqual(rows[-30]["prs_opened"], "0")

    def test_newest_run_with_same_anchor_and_missing_raw_reviews(self):
        self.add_all_sources(pr_run="p1")
        # A newer run made with a different anchor must not be used.
        self.add("pull_requests", "p3", record(anchor_date="2025-01-01", pulls=[]))
        # A newer partial run must not be used either.
        self.add("pull_requests", "p2", record(status="partial", pulls=[]))
        rows, _ = self.build()
        self.assertEqual(rows[0]["prs_opened"], "1")
        self.assertEqual(rows[0]["reviewers"], "1")

        # Without the raw pages, reviewers (and so the broad count) are blank.
        (self.raw / "pull_requests" / "p1" / "requests" / "requests.jsonl").unlink()
        rows, _ = self.build()
        self.assertEqual(rows[0]["reviewers"], "")
        self.assertEqual(rows[0]["active_contributors"], "")
        self.assertEqual(rows[0]["pr_authors"], "1")

    def test_prs_land_by_merge_or_by_a_commit_naming_them(self):
        self.add_all_sources()
        pulls = self.interim / "pull_requests" / "p2" / "repositories" / "a__b.json"
        record = json.loads(pulls.read_text())
        record["pulls"] += [
            # Closed unmerged, landed by a commit within the grace period.
            pr(4, "2025-11-20T00:00:00Z", review=None)
            | {"closed_at": "2025-11-21T00:00:00Z"},
            # Named only by a commit long after it closed: not landed.
            pr(5, "2025-11-20T00:00:00Z") | {"closed_at": "2025-11-21T00:00:00Z"},
        ]
        pulls.write_text(json.dumps(record))
        churn = self.interim / "churn" / "c1" / "repositories" / "a__b.json"
        record = json.loads(churn.read_text())
        for commit in record["commits"]:
            commit["referenced_prs"] = []
        record["commits"] += [
            {
                "committed_at": "2025-11-27T12:00:00Z",
                "contributor_id": "dev",
                "is_bot": True,
                "is_merge": False,
                "additions": 0,
                "deletions": 0,
                "referenced_prs": [4],
            },
            {
                "committed_at": "2025-12-05T00:00:00Z",
                "contributor_id": "dev",
                "is_bot": False,
                "is_merge": False,
                "additions": 0,
                "deletions": 0,
                "referenced_prs": [5],
            },
        ]
        churn.write_text(json.dumps(record))
        rows, _ = self.build()
        # PR 1 merged in week 0; PR 4 landed in week 1 by a (bot-committed) commit.
        self.assertEqual(rows[0]["prs_landed"], "1")
        self.assertEqual(rows[1]["prs_landed"], "1")
        self.assertEqual(rows[0]["prs_closed_unlanded"], "1")  # PR 5
        self.assertEqual(rows[0]["prs_closed_unmerged"], "2")  # PRs 4 and 5
        self.assertEqual(rows[1]["median_hours_to_land_by_landed"], "180.0")
        self.assertEqual(rows[0]["median_hours_to_land_by_opened"], "102.0")

    def test_landing_is_blank_for_churn_runs_without_references(self):
        self.add_all_sources()
        rows, _ = self.build()
        self.assertEqual(rows[0]["prs_landed"], "")
        self.assertEqual(rows[0]["prs_merged"], "1")

    def test_comparison_repositories_use_their_stand_in_date(self):
        self.add_all_sources()
        comparison = self.candidates.parent / "comparison.csv"
        comparison.write_text(
            "repository,priority,policy_paths,stand_in_date,matched_policy_repo,"
            "date_cluster\nc/d,pilot,,2025-12-04,a/b,middle\n",
            encoding="utf-8",
        )
        run = self.interim / "pull_requests" / "q1"
        write(run / "summary.json", {"run_id": "q1"})
        write(
            run / "repositories" / "c__d.json",
            record(
                requested_repository="c/d",
                repository="c/d",
                anchor_date="2025-12-04",
                pulls=[pr(1, "2025-12-05T00:00:00Z")],
            ),
        )
        output, summary = build_panel(
            self.study,
            self.candidates,
            policy_run=self.policy_run,
            comparison_path=comparison,
            progress=lambda _: None,
        )
        with (output / "panel.csv").open(encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
        groups = {(r["repository"], r["group"]) for r in rows}
        self.assertEqual(groups, {("a/b", "policy"), ("c/d", "comparison")})
        week0 = next(
            r for r in rows if r["repository"] == "c/d" and r["event_week"] == "0"
        )
        self.assertEqual(week0["anchor_source"], "stand_in")
        self.assertEqual(week0["week_start"], "2025-12-04")
        self.assertEqual(week0["prs_opened"], "1")
        with (output / "characteristics.csv").open(encoding="utf-8") as stream:
            found = {r["repository"]: r for r in csv.DictReader(stream)}
        self.assertEqual(found["c/d"]["matched_policy_repo"], "a/b")
        self.assertEqual(found["c/d"]["date_cluster"], "middle")
        self.assertEqual(found["a/b"]["group"], "policy")

    def test_missing_source_is_blank_not_zero(self):
        self.add_all_sources()
        (self.interim / "issues" / "i1" / "repositories" / "a__b.json").unlink()
        rows, summary = self.build()
        self.assertEqual(summary["partial"], 1)
        self.assertEqual(rows[0]["issues_opened"], "")
        self.assertEqual(rows[0]["active_contributors"], "")
        self.assertEqual(rows[0]["prs_opened"], "1")

    def test_issues_disabled_keeps_contributor_counts(self):
        self.add_all_sources()
        self.add_engagement_sources()
        disabled = {"errors": [{"component": "issue", "error": "issues disabled"}]}
        self.add(
            "issues",
            "i1",
            record(status="partial", has_issues=False, **disabled),
        )
        path = self.interim / "first_seen" / "f1" / "repositories" / "a__b.json"
        first_seen = json.loads(path.read_text(encoding="utf-8"))
        first_seen["people"] = [p for p in first_seen["people"] if "issue" not in p]
        self.add("first_seen", "f1", first_seen | {"status": "partial"} | disabled)
        context = self.interim / "context" / "x1" / "repositories" / "a__b.json"
        self.add(
            "context",
            "x1",
            json.loads(context.read_text(encoding="utf-8")) | {"has_issues": False},
        )
        rows, _ = self.build()
        week0 = rows[0]
        self.assertEqual(week0["issues_opened"], "")
        self.assertEqual(week0["newcomers"], "1")  # newbie; x only filed issues
        self.assertEqual(week0["active_contributors"], "2")  # dev and rev
        self.assertNotEqual(week0["engaged_people"], "")
        self.assertNotEqual(week0["returning_contributors"], "")

    def test_other_partial_first_seen_is_not_used(self):
        self.add_all_sources()
        self.add_engagement_sources()
        path = self.interim / "first_seen" / "f1" / "repositories" / "a__b.json"
        first_seen = json.loads(path.read_text(encoding="utf-8"))
        error = {"component": "issue", "error": "HTTP 502: Bad Gateway"}
        self.add(
            "first_seen", "f1", first_seen | {"status": "partial", "errors": [error]}
        )
        rows, _ = self.build()
        self.assertEqual(rows[0]["newcomers"], "")


class BotLoginTests(unittest.TestCase):
    def test_bot_logins(self):
        for login in ("dependabot[bot]", "qiskit-bot", "macportsbot", "JuliaTagBot"):
            self.assertTrue(is_bot_login(login), login)
        for login in (None, "", "miabbott", "botanicvelious", "oss-fuzz-robot"):
            self.assertFalse(is_bot_login(login), login)

    def test_read_record_flags_bot_logins_from_older_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "repositories").mkdir()
            record = {
                "pulls": [
                    {
                        "author": "babel-bot",
                        "author_is_bot": False,
                        "merged_by": "alice",
                        "merged_by_is_bot": False,
                    },
                ],
                "commits": [{"login": "xntbot", "is_bot": False}],
            }
            (run / "repositories" / "o__r.json").write_text(json.dumps(record))
            loaded = read_record(run, "o/r")
            self.assertTrue(loaded["pulls"][0]["author_is_bot"])
            self.assertFalse(loaded["pulls"][0]["merged_by_is_bot"])
            self.assertTrue(loaded["commits"][0]["is_bot"])


if __name__ == "__main__":
    unittest.main()
