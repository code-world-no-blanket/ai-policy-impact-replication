"""Offline tests for the RQ1/RQ2/RQ3 additions: CI, backlog, comments, discussions,
first contributions, forks, and repository context."""

import base64
import unittest
from datetime import date

from typer.testing import CliRunner

from ai_policy_impact.cli import app
from ai_policy_impact.github import Response
from test_outcomes import ANCHOR, HORIZONS, REPO, TODAY, FakeClient, page, pr


NO_REVIEWS = {"totalCount": 0, "nodes": []}


def rollup(state):
    return {"nodes": [{"commit": {"statusCheckRollup": {"state": state}}}]}


class PullAdditionTests(unittest.TestCase):
    def test_open_prs_ci_and_merger(self):
        from ai_policy_impact.pulls import collect_repository_pulls

        swept = [
            pr(
                1,
                "2025-12-01T00:00:00Z",
                merged="2025-12-02T00:00:00Z",
                mergedBy={"__typename": "User", "login": "Lead"},
                headCommit=rollup("FAILURE"),
                reviews=NO_REVIEWS,
            ),
        ]
        # Opened before the window, untouched since: only the open-PR list has it.
        stale = pr(
            9, "2023-01-01T00:00:00Z", headCommit={"nodes": []}, reviews=NO_REVIEWS
        )
        client = FakeClient(
            [
                page("pullRequests", swept),
                page("pullRequests", swept),
                page("pullRequests", [stale]),
            ]
        )
        record = collect_repository_pulls(
            client, {"repository": "a/b"}, ANCHOR, "detected", HORIZONS, TODAY
        )
        self.assertTrue(record["open_prs_collected"])
        merged, old = record["pulls"]
        self.assertEqual(merged["merged_by"], "Lead")
        self.assertFalse(merged["merged_by_is_bot"])
        self.assertEqual(merged["ci_state"], "FAILURE")
        self.assertEqual(old["number"], 9)
        self.assertIsNone(old["created_band"])
        self.assertIsNone(old["ci_state"])
        # It is not counted as opened in any band.
        self.assertEqual(record["pre_long_prs_opened"], 0)


class ChurnAdditionTests(unittest.TestCase):
    def test_commit_ci_and_committer(self):
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
            "committer": {"user": {"login": "lead"}},
            "statusCheckRollup": {"state": "SUCCESS"},
            "message": "x",
        }
        history = {"nodes": [node], "pageInfo": {"hasNextPage": False}}
        client = FakeClient([{"repository": {"ref": {"target": {"history": history}}}}])
        record = collect_repository_churn(
            client, {"repository": "a/b"}, ANCHOR, "detected", HORIZONS, TODAY
        )
        commit = record["commits"][0]
        self.assertEqual(commit["committer_login"], "lead")
        self.assertEqual(commit["ci_state"], "SUCCESS")


def ci_commit(oid, at, state):
    rollup = {"state": state} if state else None
    return {"commit": {"oid": oid, "committedDate": at, "statusCheckRollup": rollup}}


class PullCiTests(unittest.TestCase):
    def test_lifecycle_first_final_fixed_and_truncation(self):
        from ai_policy_impact.ci import collect_repository_ci

        early = [
            ci_commit("a", "2025-12-01T00:00:00Z", None),  # pushed with b
            ci_commit("b", "2025-12-01T00:01:00Z", "FAILURE"),
            ci_commit("c", "2025-12-02T00:00:00Z", "ERROR"),
        ]
        late = [early[2], ci_commit("d", "2025-12-03T00:00:00Z", "SUCCESS")]
        node = {
            "number": 4,
            "createdAt": "2025-12-01T00:00:00Z",
            "updatedAt": "2025-12-03T00:00:00Z",
            "author": {"__typename": "User", "login": "dev"},
            "early": {"totalCount": 120, "nodes": early},
            "late": {"totalCount": 120, "nodes": late},
        }
        old = dict(node, number=1, createdAt="2023-01-01T00:00:00Z")
        client = FakeClient([page("pullRequests", [node, old])])
        record = collect_repository_ci(
            client, {"repository": "a/b"}, ANCHOR, "detected", HORIZONS, TODAY
        )
        (row,) = record["pulls"]
        self.assertEqual(row["ci_results"], 3)
        self.assertEqual(row["ci_failures"], 2)
        self.assertEqual(row["first_ci_state"], "FAILURE")
        self.assertEqual(row["final_ci_state"], "SUCCESS")
        self.assertTrue(row["ci_fixed"])
        self.assertTrue(row["commits_truncated"])
        self.assertEqual(record["post_medium_prs_first_ci_failed"], 1)
        self.assertEqual(record["post_medium_prs_ci_fixed"], 1)


def comment(cid, created, login="dev", kind="User", **urls):
    return {
        "id": cid,
        "created_at": created,
        "user": {"login": login, "type": kind},
        **urls,
    }


class CommentTests(unittest.TestCase):
    def test_both_listings_window_and_bots(self):
        from ai_policy_impact.comments import collect_repository_comments

        conversation = [
            comment(
                1,
                "2025-12-01T00:00:00Z",
                issue_url="https://api.github.com/repos/a/b/issues/5",
                html_url="https://github.com/a/b/pull/5#issuecomment-1",
            ),
            comment(
                2,
                "2025-12-02T00:00:00Z",
                login="Ann",
                issue_url="https://api.github.com/repos/a/b/issues/6",
                html_url="https://github.com/a/b/issues/6#issuecomment-2",
            ),
            # Created before the window, edited inside it.
            comment(
                3,
                "2023-01-01T00:00:00Z",
                issue_url="https://api.github.com/repos/a/b/issues/1",
                html_url="https://github.com/a/b/issues/1#issuecomment-3",
            ),
            comment(
                4,
                "2025-12-03T00:00:00Z",
                login="ci-bot",
                kind="Bot",
                issue_url="https://api.github.com/repos/a/b/issues/5",
                html_url="https://github.com/a/b/pull/5#issuecomment-4",
            ),
        ]
        review = [
            comment(
                7,
                "2025-12-04T00:00:00Z",
                login="lead",
                pull_request_url="https://api.github.com/repos/a/b/pulls/5",
            )
        ]
        client = FakeClient(
            rest={
                "/repos/a/b/issues/comments": conversation,
                "/repos/a/b/pulls/comments": review,
            }
        )
        record = collect_repository_comments(
            client, {"repository": "a/b"}, ANCHOR, "detected", HORIZONS, TODAY
        )
        self.assertEqual(record["status"], "complete")
        rows = {r["id"]: r for r in record["comments"]}
        self.assertEqual(sorted(rows), [1, 2, 4, 7])
        self.assertTrue(rows[1]["on_pull_request"])
        self.assertFalse(rows[2]["on_pull_request"])
        self.assertEqual(rows[7]["number"], 5)
        self.assertTrue(rows[4]["author_is_bot"])
        self.assertEqual(record["post_medium_pr_comments"], 2)
        self.assertEqual(record["post_medium_issue_comments"], 1)
        self.assertEqual(record["post_medium_commenters"], 3)
        params = client.rest_calls[1][1]
        self.assertEqual(params["since"], "2024-11-20T00:00:00Z")

    def listing(self, fail):
        """A 450-comment listing that raises fail(params)'s error, if any."""
        from ai_policy_impact import comments

        rows = [
            {"id": i, "created_at": f"2025-12-01T{i // 60:02d}:{i % 60:02d}:00Z"}
            for i in range(450)
        ]
        calls = []

        class Client:
            def get(self, path, /, **params):
                calls.append(params)
                if error := fail(params):
                    raise error
                kept = [r for r in rows if r["created_at"] >= params["since"]]
                first = (params["page"] - 1) * params["per_page"]
                return Response(kept[first : first + params["per_page"]], "t", "x.json")

        listed, finished = comments.list_comments(
            Client(), "/repos/a/b/issues/comments", "2025-11-01T00:00:00Z"
        )
        self.assertTrue(finished)
        self.assertEqual({r["id"] for r in listed}, set(range(450)))
        return listed, calls

    def test_restarts_from_newest_when_pagination_is_limited(self):
        from ai_policy_impact.github import APIError

        # Page 3 of any listing is refused, like GitHub's depth limit.
        listed, calls = self.listing(
            lambda params: params["page"] == 3
            and APIError(422, "pagination is limited for this resource")
        )
        self.assertEqual(calls[2]["page"], 3)
        self.assertEqual(
            (calls[3]["since"], calls[3]["page"]), (listed[199]["created_at"], 1)
        )

    def test_server_timeout_retries_with_smaller_pages(self):
        from ai_policy_impact import comments
        from ai_policy_impact.github import APIError

        # Full pages time out on GitHub's side; smaller ones succeed.
        listed, calls = self.listing(
            lambda params: params["per_page"] == comments.PER_PAGE
            and params["page"] == 2
            and APIError(502, "HTTP 502: Server Error")
        )
        self.assertEqual(
            [(c["per_page"], c["page"]) for c in calls[:4]],
            [(100, 1), (100, 2), (comments.SMALL_PAGE, 1), (comments.SMALL_PAGE, 2)],
        )
        self.assertEqual(calls[2]["since"], listed[99]["created_at"])

    def test_slow_since_filter_falls_back_to_newest_first(self):
        from ai_policy_impact import comments
        from ai_policy_impact.github import APIError

        rows = [
            {"id": i, "created_at": f"2025-{10 + i // 300:02d}-{1 + i % 300 // 12:02d}"
             f"T{i % 12:02d}:00:00Z"}
            for i in range(450)
        ]
        calls = []

        class Client:
            def get(self, path, /, **params):
                calls.append(params)
                if "since" in params:  # the filter times out at every page size
                    raise APIError(500, "HTTP 500: Non-JSON API response")
                ordered = sorted(rows, key=lambda r: r["created_at"], reverse=True)
                first = (params["page"] - 1) * params["per_page"]
                return Response(ordered[first : first + params["per_page"]], "t", "x")

        since = "2025-11-01T00:00:00Z"
        listed, finished = comments.list_comments(
            Client(), "/repos/a/b/pulls/comments", since
        )
        self.assertTrue(finished)
        wanted = {r["id"] for r in rows if r["created_at"] >= since}
        self.assertLessEqual(wanted, {r["id"] for r in listed})
        self.assertEqual(calls[2]["direction"], "desc")
        # It stops at the first page reaching past `since`, not at the end.
        self.assertLess(len(calls), 2 + 5)

    def test_pagination_limit_without_progress_fails(self):
        from ai_policy_impact.comments import collect_repository_comments
        from ai_policy_impact.github import APIError

        client = FakeClient(
            rest={
                "/repos/a/b/issues/comments": APIError(
                    422, "pagination is limited for this resource"
                )
            }
        )
        record = collect_repository_comments(
            client, {"repository": "a/b"}, ANCHOR, "detected", HORIZONS, TODAY
        )
        self.assertEqual(record["status"], "partial")


def author(login):
    return {"__typename": "Bot" if "[bot]" in login else "User", "login": login}


class DiscussionTests(unittest.TestCase):
    def collect(self, client):
        from ai_policy_impact.discussions import collect_repository_discussions

        return collect_repository_discussions(
            client, {"repository": "a/b"}, ANCHOR, "detected", HORIZONS, TODAY
        )

    def test_disabled_is_empty_and_complete(self):
        client = FakeClient(rest={"/repos/a/b": dict(REPO, has_discussions=False)})
        record = self.collect(client)
        self.assertEqual(record["status"], "complete")
        self.assertFalse(record["discussions_enabled"])
        self.assertEqual(record["posts"], [])
        self.assertEqual(client.graphql_calls, [])

    def test_posts_replies_truncation_and_window(self):
        node = {
            "number": 3,
            "createdAt": "2023-06-01T00:00:00Z",  # opened before the window
            "updatedAt": "2025-12-05T00:00:00Z",
            "author": author("op"),
            "comments": {
                "totalCount": 2,
                "nodes": [
                    {
                        "createdAt": "2025-12-01T00:00:00Z",
                        "author": author("ann"),
                        "replies": {
                            "totalCount": 3,
                            "nodes": [
                                {
                                    "createdAt": "2025-12-02T00:00:00Z",
                                    "author": author("helper[bot]"),
                                }
                            ],
                        },
                    }
                ],
            },
        }
        client = FakeClient(
            [page("discussions", [node])],
            rest={"/repos/a/b": dict(REPO, has_discussions=True)},
        )
        record = self.collect(client)
        self.assertTrue(record["discussions_enabled"])
        self.assertEqual([p["kind"] for p in record["posts"]], ["comment", "reply"])
        self.assertEqual(record["discussions_with_comments_truncated"], 1)
        self.assertEqual(record["discussions_with_replies_truncated"], 1)
        self.assertEqual(record["post_medium_discussion_comments"], 1)  # bot excluded
        self.assertEqual(record["pre_long_discussions_opened"], 0)


def connection(nodes):
    return {"nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}


class FirstSeenTests(unittest.TestCase):
    def test_earliest_per_kind_bots_and_deleted_accounts(self):
        from ai_policy_impact.first_seen import collect_repository_first_seen

        commits = connection(
            [
                {
                    "committedDate": "2024-05-01T00:00:00Z",
                    "author": {"email": "a@x.org", "user": {"login": "Dev"}},
                },
                {
                    "committedDate": "2022-01-01T00:00:00Z",
                    "author": {"email": "a@x.org", "user": {"login": "dev"}},
                },
                {
                    "committedDate": "2023-01-01T00:00:00Z",
                    "author": {"email": "r[bot]@x.org", "user": None},
                },
            ]
        )
        pulls = connection(
            [
                {"createdAt": "2021-03-01T00:00:00Z", "author": author("dev")},
                {"createdAt": "2025-12-01T00:00:00Z", "author": author("newbie")},
                {"createdAt": "2025-12-02T00:00:00Z", "author": None},
            ]
        )
        issues = connection(
            [{"createdAt": "2020-01-01T00:00:00Z", "author": author("dependabot[bot]")}]
        )
        client = FakeClient(
            [
                {"repository": {"ref": {"target": {"history": commits}}}},
                {"repository": {"pullRequests": pulls}},
                {"repository": {"issues": issues}},
            ]
        )
        record = collect_repository_first_seen(client, {"repository": "a/b"})
        self.assertEqual(record["status"], "complete")
        people = {p["person"]: p for p in record["people"]}
        self.assertEqual(people["dev"]["commit"], "2022-01-01T00:00:00+00:00")
        self.assertEqual(people["dev"]["pull_request"], "2021-03-01T00:00:00+00:00")
        self.assertIn("newbie", people)
        self.assertTrue(people["dependabot[bot]"]["is_bot"])
        bots = [p for p in people.values() if p["person"].startswith("email:")]
        self.assertTrue(bots and bots[0]["is_bot"])
        self.assertEqual(record["items_read"]["pull_request"], 2)

    def test_failed_history_is_failed_not_partial(self):
        from ai_policy_impact.first_seen import collect_repository_first_seen
        from ai_policy_impact.github import APIError

        client = FakeClient([APIError(500, "HTTP 500: boom")])
        record = collect_repository_first_seen(client, {"repository": "a/b"})
        self.assertEqual(record["status"], "failed")
        self.assertNotIn("people", record)


class ForkTests(unittest.TestCase):
    def test_daily_cumulative_and_reconciliation(self):
        from ai_policy_impact.forks import collect_fork_history

        meta = dict(REPO, created_at="2025-01-01T00:00:00Z", forks_count=4)
        forks = [
            {"created_at": "2025-01-03T00:00:00Z"},
            {"created_at": "2025-01-03T05:00:00Z"},
            {"created_at": "2025-01-05T00:00:00Z"},
        ]
        client = FakeClient(rest={"/repos/a/b": meta, "/repos/a/b/forks": forks})
        record = collect_fork_history(client, {"repository": "a/b"})
        self.assertEqual(record["status"], "complete")
        self.assertEqual(record["reconciliation"], "mismatch")
        self.assertEqual(
            record["daily"],
            [
                {"date": "2025-01-03", "new_forks": 2, "cumulative_forks": 2},
                {"date": "2025-01-05", "new_forks": 1, "cumulative_forks": 3},
            ],
        )
        self.assertEqual(client.rest_calls[1][1]["sort"], "oldest")


class ContextTests(unittest.TestCase):
    def test_handles(self):
        from ai_policy_impact.context import handles

        text = (
            "* @Alice (lead)\n* @bob, mail carol@example.org\n"
            "/src/ @a/core-team @a\n"
            "[Dan](https://github.com/dan) see https://github.com/a/b/pull/1\n"
        )
        users, teams = handles(text, "a")
        self.assertEqual(users, {"alice", "bob", "dan"})
        self.assertEqual(teams, {"a/core-team"})

    def test_owner_and_maintainer_files_at_policy_date(self):
        from ai_policy_impact.context import collect_repository_context

        meta = dict(
            REPO,
            created_at="2015-01-01T00:00:00Z",
            language="Rust",
            license={"spdx_id": "MIT"},
            owner={"login": "a", "type": "Organization"},
        )
        content = base64.b64encode(b"Maintainers: @alice @bob @a/team").decode()
        client = FakeClient(
            rest={
                "/repos/a/b": meta,
                "/orgs/a": {"public_repos": 7, "followers": 3, "is_verified": True},
                "/orgs/a/public_members": [{"login": "x"}, {"login": "y"}],
                "/repos/a/b/commits": [{"sha": "abc"}],
                "/repos/a/b/git/trees/abc": {
                    "tree": [
                        {"type": "blob", "path": "MAINTAINERS.md"},
                        {"type": "blob", "path": "vendor/x/CODEOWNERS"},
                        {"type": "blob", "path": "src/main.rs"},
                    ]
                },
                "/repos/a/b/contents/MAINTAINERS.md": {
                    "encoding": "base64",
                    "content": content,
                },
            }
        )
        record = collect_repository_context(
            client, {"repository": "a/b"}, ANCHOR, "detected", HORIZONS, TODAY
        )
        self.assertEqual(record["status"], "complete")
        self.assertEqual(record["owner_public_repos"], 7)
        self.assertEqual(record["owner_public_members"], 2)
        self.assertEqual(record["maintainer_file_user_count"], 2)
        self.assertEqual(record["maintainer_file_team_count"], 1)
        self.assertEqual(
            [f["path"] for f in record["maintainer_files"]], ["MAINTAINERS.md"]
        )
        commits_call = next(
            c for c in client.rest_calls if c[0] == "/repos/a/b/commits"
        )
        self.assertEqual(commits_call[1]["until"], "2025-11-20T00:00:00Z")
        self.assertEqual(record["primary_language"], "Rust")
        self.assertEqual(date.fromisoformat(record["anchor_date"]), ANCHOR)


class CommandTests(unittest.TestCase):
    def test_new_commands_reject_zero_limit_before_network(self):
        for command in [
            "ci",
            "comments",
            "discussions",
            "first-seen",
            "forks",
            "context",
        ]:
            result = CliRunner().invoke(app, ["collect", command, "--limit", "0"])
            self.assertNotEqual(result.exit_code, 0, command)


if __name__ == "__main__":
    unittest.main()
