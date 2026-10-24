"""Offline tests for the PR, issue, release, and churn outcome collectors."""

import unittest
from datetime import date, datetime, timedelta, timezone

from typer.testing import CliRunner

from ai_policy_impact.cli import app
from ai_policy_impact.config import HorizonConfig
from ai_policy_impact.github import APIError, Response
from ai_policy_impact.windows import Window

ANCHOR = date(2025, 11, 20)
TODAY = date(2026, 1, 31)
HORIZONS = HorizonConfig(medium_run_days=180, long_run_days=365)
REPO = {
    "id": 1,
    "full_name": "a/b",
    "private": False,
    "default_branch": "main",
    "has_issues": True,
}


class FakeClient:
    """REST metadata by path plus scripted GraphQL responses."""

    def __init__(self, graphql=(), rest=None):
        self.graphql_responses = list(graphql)
        self.rest = rest or {}
        self.graphql_calls = []
        self.rest_calls = []

    def get(self, path, /, **params):
        self.rest_calls.append((path, params))
        value = self.rest.get((path, params.get("page")), self.rest.get(path, REPO))
        if isinstance(value, Exception):
            raise value
        return Response(value, "2026-01-31T00:00:00+00:00", "x.json")

    def graphql(self, query, variables):
        self.graphql_calls.append(variables)
        # The last scripted page repeats, e.g. for the sweep's head re-read.
        if len(self.graphql_responses) > 1:
            response = self.graphql_responses.pop(0)
        else:
            response = self.graphql_responses[0]
        if isinstance(response, Exception):
            raise response
        return response


def page(field, nodes, more=False, cursor=None):
    connection = {
        "nodes": nodes,
        "pageInfo": {"hasNextPage": more, "endCursor": cursor},
    }
    return {"repository": {field: connection}}


def pr(number, created, updated=None, merged=None, closed=None, login="dev", **extra):
    return {
        "number": number,
        "createdAt": created,
        "updatedAt": updated or merged or closed or created,
        "mergedAt": merged,
        "closedAt": closed or merged,
        "isDraft": False,
        "state": "MERGED" if merged else "CLOSED" if closed else "OPEN",
        "baseRefName": "main",
        "authorAssociation": "CONTRIBUTOR",
        "author": {"__typename": "Bot" if "[bot]" in login else "User", "login": login},
        "additions": 10,
        "deletions": 4,
        "changedFiles": 2,
        "commits": {"totalCount": 1},
        "comments": {"totalCount": 0},
        **extra,
    }


def review(login, state, at):
    kind = "Bot" if "[bot]" in login else "User"
    return {
        "author": {"__typename": kind, "login": login},
        "state": state,
        "submittedAt": at,
    }


OLD = "2020-01-01T00:00:00Z"


class WindowTests(unittest.TestCase):
    def test_events_after_cutoff_or_before_start_have_no_band(self):
        window = Window.around(ANCHOR, HORIZONS, TODAY)
        self.assertEqual(window.start, date(2024, 11, 20))
        self.assertEqual(window.end, TODAY)
        self.assertEqual(
            window.band(window.cutoff - timedelta(seconds=1)), "post_medium"
        )
        self.assertIsNone(window.band(window.cutoff))
        self.assertIsNone(window.band(window.start_time - timedelta(seconds=1)))
        self.assertFalse(window.observed("post_long"))


class SweepTests(unittest.TestCase):
    def test_stops_at_older_items_and_rereads_items_updated_mid_run(self):
        from ai_policy_impact.activity import sweep_updated

        since = datetime(2025, 1, 1, tzinfo=timezone.utc)
        stale = pr(7, "2025-03-01T00:00:00Z", updated="2025-06-01T00:00:00Z")
        fresh = dict(stale, updatedAt="2099-01-01T00:00:00Z", state="MERGED")
        client = FakeClient(
            [
                page("pullRequests", [stale], True, "c1"),
                page("pullRequests", [pr(3, OLD)], True, "c2"),
                # Second pass: the item moved to the head of the list.
                page("pullRequests", [fresh, pr(3, OLD)]),
            ]
        )
        nodes = sweep_updated(
            client,
            "q",
            {},
            lambda d: d["repository"]["pullRequests"],
            since,
            max_pages=10,
        )
        self.assertEqual(list(nodes), [7])
        self.assertEqual(nodes[7]["state"], "MERGED")
        self.assertEqual(len(client.graphql_calls), 3)
        self.assertEqual(client.graphql_calls[1]["cursor"], "c1")
        self.assertIsNone(client.graphql_calls[2]["cursor"])

    def test_timed_out_page_retries_same_cursor_with_fewer_nodes(self):
        from ai_policy_impact.activity import sweep_updated

        since = datetime(2025, 1, 1, tzinfo=timezone.utc)
        recent = pr(7, "2025-03-01T00:00:00Z")
        client = FakeClient(
            [
                page("pullRequests", [recent], True, "c1"),
                APIError(504, "HTTP 504: Non-JSON API response"),
                APIError(200, "Something went wrong; a timeout occurred"),
                page("pullRequests", [pr(3, OLD)]),
            ]
        )
        nodes = sweep_updated(
            client,
            "q",
            {},
            lambda d: d["repository"]["pullRequests"],
            since,
            max_pages=10,
            page_size=40,
        )
        self.assertEqual(list(nodes), [7])
        sizes = [(c["cursor"], c["first"]) for c in client.graphql_calls[:4]]
        self.assertEqual(sizes, [(None, 40), ("c1", 40), ("c1", 20), ("c1", 10)])

    def test_resource_limit_errors_shrink_the_page_like_timeouts(self):
        from ai_policy_impact.activity import is_timeout

        self.assertTrue(
            is_timeout(APIError(200, "Resource limits for this query exceeded."))
        )
        self.assertFalse(is_timeout(APIError(200, "Could not resolve to a Repository")))

    def test_timeouts_at_smallest_page_still_fail(self):
        from ai_policy_impact.activity import graphql_pages

        client = FakeClient([APIError(502, "HTTP 502: timeout")])
        with self.assertRaises(APIError):
            list(graphql_pages(client, "q", {}, lambda d: d, max_pages=5, page_size=8))
        self.assertEqual([c["first"] for c in client.graphql_calls], [8, 5])

    def test_skip_steps_over_a_node_that_times_out_alone(self):
        from ai_policy_impact.activity import graphql_pages
        from ai_policy_impact.churn import skip_commit

        timeout = APIError(502, "HTTP 502: timeout")
        first = {"nodes": [{"n": 1}], "pageInfo": {"hasNextPage": True, "endCursor": "h 0"}}
        last = {"nodes": [{"n": 3}], "pageInfo": {"hasNextPage": False, "endCursor": "h 2"}}

        class Client:
            calls = []

            def graphql(self, query, variables):
                self.calls.append((variables["cursor"], variables["first"]))
                if variables["cursor"] == "h 0":  # the second commit never computes
                    raise timeout
                return first if variables["cursor"] is None else last

        skipped = []
        client = Client()
        pages = list(
            graphql_pages(client, "q", {}, lambda d: d, max_pages=9, page_size=4,
                          skip=lambda c: skip_commit(c, skipped))
        )
        self.assertEqual([n["n"] for p in pages for n in p["nodes"]], [1, 3])
        self.assertEqual(skipped, [1])
        self.assertEqual(client.calls[-1], ("h 1", 1))

    def test_unavailable_line_counts_are_skipped_like_timeouts(self):
        from ai_policy_impact.churn import unreadable_commit

        self.assertTrue(unreadable_commit(
            APIError(200, "The additions count for this commit is unavailable.")
        ))
        self.assertTrue(unreadable_commit(APIError(502, "HTTP 502: timeout")))
        self.assertFalse(unreadable_commit(APIError(404, "HTTP 404: Not Found")))

    def test_low_rate_limit_waits_and_far_reset_stops(self):
        from ai_policy_impact.activity import pace

        waits = []
        soon = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
        limit = {"cost": 5, "remaining": 3, "resetAt": soon}
        self.assertGreater(pace({"rateLimit": limit}, waits.append), 0)
        self.assertEqual(len(waits), 1)
        self.assertEqual(
            pace({"rateLimit": dict(limit, remaining=4000)}, waits.append), 0
        )
        later = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        with self.assertRaises(APIError):
            pace({"rateLimit": dict(limit, resetAt=later)}, waits.append)


class PullTests(unittest.TestCase):
    def collect(self, nodes, **options):
        from ai_policy_impact.pulls import collect_repository_pulls

        client = FakeClient([page("pullRequests", nodes)])
        record = collect_repository_pulls(
            client,
            {"repository": "a/b"},
            ANCHOR,
            "detected",
            HORIZONS,
            TODAY,
            **options,
        )
        return record, client

    def test_bands_bots_censoring_and_reviews(self):
        reviews = {
            "totalCount": 4,
            "nodes": [
                review("dev", "COMMENTED", "2025-12-01T01:00:00Z"),  # self-review
                review("ci[bot]", "APPROVED", "2025-12-01T02:00:00Z"),
                review("lead", "CHANGES_REQUESTED", "2025-12-01T06:00:00Z"),
                review("lead", "APPROVED", "2025-12-02T00:00:00Z"),
            ],
        }
        nodes = [
            # Pre-policy, merged in 48 hours.
            pr(
                1,
                "2025-10-01T00:00:00Z",
                merged="2025-10-03T00:00:00Z",
                reviews={"totalCount": 0, "nodes": []},
            ),
            # Post-policy, reviewed and merged.
            pr(
                2,
                "2025-12-01T00:00:00Z",
                merged="2025-12-02T12:00:00Z",
                reviews=reviews,
            ),
            # Post-policy, closed without merging.
            pr(
                3,
                "2025-12-05T00:00:00Z",
                closed="2025-12-06T00:00:00Z",
                reviews={"totalCount": 0, "nodes": []},
            ),
            # Opened before the anchor, merged after the cutoff: censored.
            pr(
                4,
                "2025-11-01T00:00:00Z",
                merged="2026-03-01T00:00:00Z",
                reviews={"totalCount": 0, "nodes": []},
            ),
            pr(
                5,
                "2025-12-10T00:00:00Z",
                login="dependabot[bot]",
                reviews={"totalCount": 0, "nodes": []},
            ),
            # Updated in the window but created and closed long before it.
            pr(
                6,
                OLD,
                updated="2025-12-01T00:00:00Z",
                closed="2021-01-01T00:00:00Z",
                reviews={"totalCount": 0, "nodes": []},
            ),
        ]
        record, _ = self.collect(nodes)
        self.assertEqual(record["status"], "complete")
        self.assertEqual([r["number"] for r in record["pulls"]], [1, 2, 3, 4, 5])
        self.assertEqual(record["bot_prs_in_window"], 1)
        self.assertEqual(record["pre_medium_prs_opened"], 2)
        self.assertEqual(record["post_medium_prs_opened"], 2)
        self.assertEqual(record["post_medium_prs_merged"], 1)
        self.assertEqual(record["post_medium_prs_closed_unmerged"], 1)
        self.assertEqual(record["pre_medium_prs_open_at_cutoff"], 1)
        self.assertEqual(record["pre_medium_median_hours_to_merge"], 48)
        self.assertEqual(record["post_medium_merged_additions"], 10)
        self.assertIsNone(record["post_long_prs_opened"])
        self.assertEqual(record["post_long_days_observed"], 0)
        reviewed = record["pulls"][1]
        self.assertEqual(reviewed["reviews_by_others"], 2)
        self.assertEqual(reviewed["reviewers"], 1)
        self.assertEqual(reviewed["changes_requested"], 1)
        self.assertEqual(reviewed["hours_to_first_review"], 6)
        self.assertFalse(reviewed["reviews_truncated"])
        censored = record["pulls"][3]
        self.assertIsNone(censored["hours_to_merge"])
        self.assertIsNone(censored["merged_band"])
        self.assertTrue(censored["open_at_cutoff"])
        self.assertEqual(record["post_medium_prs_without_review"], 1)

    def test_failed_sweep_withholds_counts(self):
        from ai_policy_impact.pulls import collect_repository_pulls

        client = FakeClient([APIError(502, "HTTP 502: timeout")])
        record = collect_repository_pulls(
            client, {"repository": "a/b"}, ANCHOR, "detected", HORIZONS, TODAY
        )
        self.assertEqual(record["status"], "partial")
        self.assertNotIn("pre_medium_prs_opened", record)
        self.assertNotIn("pulls", record)

    def test_no_anchor_is_partial_without_querying(self):
        from ai_policy_impact.pulls import collect_repository_pulls

        client = FakeClient()
        record = collect_repository_pulls(
            client, {"repository": "a/b"}, None, "none", HORIZONS, TODAY
        )
        self.assertEqual(record["status"], "partial")
        self.assertEqual(client.graphql_calls, [])


class IssueTests(unittest.TestCase):
    def test_disabled_tracker_is_missing_not_zero(self):
        from ai_policy_impact.issues import collect_repository_issues

        client = FakeClient(rest={"/repos/a/b": dict(REPO, has_issues=False)})
        record = collect_repository_issues(
            client, {"repository": "a/b"}, ANCHOR, "detected", HORIZONS, TODAY
        )
        self.assertEqual(record["status"], "partial")
        self.assertNotIn("post_medium_issues_opened", record)

    def test_close_reasons_and_durations(self):
        from ai_policy_impact.issues import collect_repository_issues

        def issue(number, created, closed=None, reason=None):
            return {
                "number": number,
                "createdAt": created,
                "updatedAt": closed or created,
                "closedAt": closed,
                "state": "CLOSED" if closed else "OPEN",
                "stateReason": reason,
                "authorAssociation": "NONE",
                "author": {"__typename": "User", "login": "u"},
                "comments": {"totalCount": 1},
            }

        nodes = [
            issue(1, "2025-12-01T00:00:00Z", "2025-12-01T12:00:00Z", "COMPLETED"),
            issue(2, "2025-12-02T00:00:00Z", "2025-12-03T00:00:00Z", "NOT_PLANNED"),
            issue(3, "2025-12-03T00:00:00Z"),
        ]
        client = FakeClient([page("issues", nodes)])
        record = collect_repository_issues(
            client, {"repository": "a/b"}, ANCHOR, "detected", HORIZONS, TODAY
        )
        self.assertEqual(record["post_medium_issues_opened"], 3)
        self.assertEqual(record["post_medium_issues_closed"], 2)
        self.assertEqual(record["post_medium_issues_closed_completed"], 1)
        self.assertEqual(record["post_medium_issues_open_at_cutoff"], 1)
        self.assertEqual(record["post_medium_median_hours_to_close"], 18)
        self.assertEqual(record["pre_medium_issues_opened"], 0)


class ReleaseTests(unittest.TestCase):
    def test_drafts_prereleases_and_gaps(self):
        from ai_policy_impact.releases import collect_repository_releases

        def release(tag, published, prerelease=False, draft=False):
            return {
                "tag_name": tag,
                "name": tag,
                "draft": draft,
                "prerelease": prerelease,
                "created_at": published,
                "published_at": published,
                "author": {"login": "rel"},
            }

        listed = [
            release("v3", "2025-12-21T00:00:00Z"),
            release("v3-rc1", "2025-12-15T00:00:00Z", prerelease=True),
            release("v2", "2025-12-01T00:00:00Z"),
            release("v1", "2025-06-01T00:00:00Z"),
            release("draft", None, draft=True),
            release("v0", "2019-01-01T00:00:00Z"),
        ]
        client = FakeClient(rest={"/repos/a/b/releases": listed})
        record = collect_repository_releases(
            client, {"repository": "a/b"}, ANCHOR, "detected", HORIZONS, TODAY
        )
        self.assertEqual(record["status"], "complete")
        self.assertEqual(record["releases_total"], 5)
        self.assertEqual(record["first_release_at"], "2019-01-01T00:00:00Z")
        self.assertEqual(record["post_medium_releases"], 3)
        self.assertEqual(record["post_medium_stable_releases"], 2)
        self.assertEqual(record["post_medium_mean_days_between_stable"], 20)
        # One release leaves the gap undefined, not zero.
        self.assertEqual(record["pre_medium_stable_releases"], 1)
        self.assertIsNone(record["pre_medium_mean_days_between_stable"])
        self.assertIsNone(record["post_long_releases"])


class ChurnTests(unittest.TestCase):
    def test_pr_references_count_only_landing_mentions(self):
        from ai_policy_impact.churn import pr_references

        cases = [
            # Merge bot: title suffix plus trailer; stack and issue lines differ.
            (
                "x (#198410)\n\nPull Request resolved: "
                "https://github.com/a/b/pull/198410\nghstack dependencies: #7",
                [198410],
            ),
            ("x\n\n(Merged from https://github.com/a/b/pull/32992)", [32992]),
            ("x\n\nMerged-from: https://github.com/a/b/pull/8", [8]),
            ("x\n\nCloses https://github.com/a/b/pull/64347\nBug: #12", [64347]),
            ("x\n\nFixes #2565, PR #3496", [2565, 3496]),
            ("x\n\nFixes #4800, supercedes #5995", [4800]),
            ("Merge pull request #115 from a/topic", [115]),
            ("Merge a/b#33421: wallet: x", [33421]),
            # Mentions that do not land the PR.
            ('Revert "x (#5)"\n\nReverted https://github.com/a/b/pull/5', []),
            ("x\n\nSupersedes https://github.com/a/b/pull/19842", []),
            ("x\n\nSee also PR #27441.", []),
            ("x\n\nPR replaces #22667", []),
            ("x\n\nCloses https://github.com/a/b/pull/9#issuecomment-1", []),
            ("x\n\nFixes: other/project#55", []),
            ("x\n\nFixes https://github.com/other/b/pull/3", []),
            (None, []),
        ]
        for message, expected in cases:
            with self.subTest(message=message):
                self.assertEqual(pr_references(message, "a/b"), expected)

    def test_merges_and_bots_excluded_and_window_requested(self):
        from ai_policy_impact.churn import collect_repository_churn

        def commit(oid, at, adds, dels, login="dev", parents=1, email="d@x.org"):
            return {
                "oid": oid,
                "committedDate": at,
                "authoredDate": at,
                "additions": adds,
                "deletions": dels,
                "changedFilesIfAvailable": 1,
                "parents": {"totalCount": parents},
                "author": {"email": email, "user": {"login": login} if login else None},
                "message": f"commit {oid} (#{len(oid)})",
            }

        nodes = [
            commit("a", "2025-12-01T00:00:00Z", 100, 20),
            commit("b", "2025-12-02T00:00:00Z", 5, 5, login=None, email="Me@X.org"),
            commit("m", "2025-12-03T00:00:00Z", 900, 900, parents=2),
            commit(
                "r",
                "2025-12-04T00:00:00Z",
                50,
                50,
                login=None,
                email="29139614+renovate[bot]@users.noreply.github.com",
            ),
            commit("p", "2025-10-01T00:00:00Z", 7, 3),
        ]
        history = {
            "nodes": nodes,
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
        client = FakeClient([{"repository": {"ref": {"target": {"history": history}}}}])
        record = collect_repository_churn(
            client, {"repository": "a/b"}, ANCHOR, "detected", HORIZONS, TODAY
        )
        self.assertEqual(record["status"], "complete")
        self.assertEqual(record["merge_commits"], 1)
        self.assertEqual(record["bot_commits"], 1)
        self.assertEqual(record["post_medium_commits"], 2)
        self.assertEqual(record["post_medium_additions"], 105)
        self.assertEqual(record["post_medium_deletions"], 25)
        self.assertEqual(record["post_medium_median_lines_per_commit"], 65)
        self.assertEqual(record["pre_medium_additions"], 7)
        self.assertEqual(record["commits"][0]["referenced_prs"], [1])
        self.assertNotIn("message", record["commits"][0])
        self.assertIsNone(record["post_long_commits"])
        unlinked = record["commits"][1]["contributor_id"]
        self.assertTrue(unlinked.startswith("email:"))
        self.assertNotIn("x.org", unlinked)
        variables = client.graphql_calls[0]
        self.assertEqual(variables["branch"], "refs/heads/main")
        self.assertEqual(variables["since"], "2024-11-20T00:00:00+00:00")
        self.assertEqual(variables["until"], "2026-01-31T23:59:59+00:00")

    def test_failed_history_keeps_commits_read_before_it(self):
        from ai_policy_impact.churn import collect_repository_churn

        node = {
            "oid": "a",
            "committedDate": "2025-12-01T00:00:00Z",
            "authoredDate": "2025-12-01T00:00:00Z",
            "additions": 1,
            "deletions": 1,
            "changedFilesIfAvailable": 1,
            "parents": {"totalCount": 1},
            "author": {"email": "d@x.org", "user": {"login": "dev"}},
            "message": "commit a",
        }
        history = {"nodes": [node], "pageInfo": {"hasNextPage": True, "endCursor": "h 0"}}
        client = FakeClient(
            [
                {"repository": {"ref": {"target": {"history": history}}}},
                APIError(500, "HTTP 500: Server Error"),
            ]
        )
        record = collect_repository_churn(
            client, {"repository": "a/b"}, ANCHOR, "detected", HORIZONS, TODAY
        )
        self.assertEqual(record["status"], "partial")
        self.assertEqual([c["sha"] for c in record["commits"]], ["a"])


class CommandTests(unittest.TestCase):
    def test_outcome_commands_reject_zero_limit_before_network(self):
        for command in ["pulls", "issues", "releases", "churn"]:
            result = CliRunner().invoke(app, ["collect", command, "--limit", "0"])
            self.assertNotEqual(result.exit_code, 0, command)


if __name__ == "__main__":
    unittest.main()
