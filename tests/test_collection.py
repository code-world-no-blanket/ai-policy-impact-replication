"""Offline tests for research data semantics and failure handling."""

import base64
import hashlib
import io
import json
import tempfile
import unittest
from http.client import IncompleteRead
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from typer.testing import CliRunner

from ai_policy_impact.cli import app
from ai_policy_impact.collect import metadata_fields, read_candidates, save_document
from ai_policy_impact.github import APIError, GitHubClient, Response
from ai_policy_impact.runs import guarded
from ai_policy_impact.projects import collect_projects, pages, summarize_items


class FakeHTTP:
    def __init__(self, data, headers=None):
        self.body = json.dumps(data).encode()
        self.status = 200
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


class ClientTests(unittest.TestCase):
    def test_invalid_json_success_is_archived_and_reported_as_error(self):
        response = FakeHTTP({})
        response.body = b"<html>upstream error</html>"
        with tempfile.TemporaryDirectory() as tmp:
            with patch("ai_policy_impact.github.urlopen", return_value=response):
                with self.assertRaisesRegex(APIError, "Non-JSON"):
                    GitHubClient(Path(tmp), None).get("/example")
            self.assertEqual((Path(tmp) / "000001.json").read_bytes(), response.body)

    def test_existing_raw_requests_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "000001.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "new or empty"):
                GitHubClient(Path(tmp), None)

    def test_provenance_checksums_and_no_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = GitHubClient(Path(tmp), "secret-token-for-test")
            with patch(
                "ai_policy_impact.github.urlopen",
                return_value=FakeHTTP({"ok": True}, {"ETag": "tag"}),
            ):
                response = client.get("/repos/example/repo", ref="branch name")
            journal_text = (Path(tmp) / "requests.jsonl").read_text()
            journal = json.loads(journal_text)
            self.assertNotIn("secret-token-for-test", journal_text)
            self.assertNotIn("Authorization", journal_text)
            self.assertEqual(
                journal["sha256"],
                hashlib.sha256(Path(response.raw_file).read_bytes()).hexdigest(),
            )
            self.assertEqual(journal["headers"], {"etag": "tag"})
            self.assertTrue(journal["url"].endswith("ref=branch+name"))

    def test_http_404_recorded_without_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            error = HTTPError(
                "https://api.github.com/example",
                404,
                "Not found",
                {},
                io.BytesIO(b'{"message":"Not found"}'),
            )
            with patch("ai_policy_impact.github.urlopen", side_effect=error) as opener:
                with self.assertRaises(APIError) as raised:
                    GitHubClient(Path(tmp), None).get("/example")
            self.assertEqual(raised.exception.status, 404)
            self.assertEqual(opener.call_count, 1)
            self.assertEqual(
                json.loads((Path(tmp) / "requests.jsonl").read_text())["status"], 404
            )

    def test_network_retries_are_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch(
                    "ai_policy_impact.github.urlopen", side_effect=URLError("offline")
                ) as opener,
                patch("ai_policy_impact.github.time.sleep"),
            ):
                with self.assertRaises(APIError):
                    GitHubClient(Path(tmp), None).get("/example")
            self.assertEqual(opener.call_count, 3)

    def test_truncated_response_body_is_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch(
                    "ai_policy_impact.github.urlopen",
                    side_effect=[IncompleteRead(b"{", 10), FakeHTTP({"ok": True})],
                ),
                patch("ai_policy_impact.github.time.sleep"),
            ):
                response = GitHubClient(Path(tmp), None).get("/example")
            self.assertEqual(response.data, {"ok": True})

    def test_unexpected_error_fails_only_that_repository(self):
        def broken(client, candidate):
            raise KeyError("missing field")

        record = guarded(broken, {"repository": "o/r"}, None, {"repository": "o/r"})
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["repository"], "o/r")
        self.assertIn("KeyError", record["errors"][0]["error"])

    def test_exhausted_hourly_budget_waits_for_reset(self):
        limited = HTTPError(
            "https://api.github.com/repos/o/r",
            403,
            "Forbidden",
            {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1000"},
            io.BytesIO(b'{"message":"API rate limit exceeded for user ID 1."}'),
        )
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch(
                    "ai_policy_impact.github.urlopen",
                    side_effect=[limited, FakeHTTP({"ok": True})],
                ),
                patch("ai_policy_impact.github.time.time", return_value=900),
                patch("ai_policy_impact.github.time.sleep") as sleep,
            ):
                response = GitHubClient(Path(tmp), None).get("/repos/o/r")
            self.assertEqual(response.data, {"ok": True})
            sleep.assert_called_once_with(105)

    def test_secondary_rate_limit_retry_after_is_honoured(self):
        limited = HTTPError(
            "https://api.github.com/graphql",
            403,
            "Forbidden",
            {"Retry-After": "60"},
            io.BytesIO(b'{"message":"You have exceeded a secondary rate limit."}'),
        )
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch(
                    "ai_policy_impact.github.urlopen",
                    side_effect=[limited, FakeHTTP({"ok": True})],
                ),
                patch("ai_policy_impact.github.time.sleep") as sleep,
            ):
                response = GitHubClient(Path(tmp), None).get("/example")
            self.assertEqual(response.data, {"ok": True})
            sleep.assert_called_once_with(60)

    def test_graphql_errors_on_http_200_are_not_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "ai_policy_impact.github.urlopen",
                return_value=FakeHTTP(
                    {"data": None, "errors": [{"message": "missing read:project"}]}
                ),
            ):
                with self.assertRaisesRegex(APIError, "read:project"):
                    GitHubClient(Path(tmp), None).graphql(
                        "query { viewer { login } }", {}
                    )


class DataTests(unittest.TestCase):
    def test_watchers_are_subscribers_not_stars(self):
        data = dict(
            id=1,
            full_name="a/b",
            html_url="https://github.com/a/b",
            owner={"type": "Organization"},
            created_at="2020-01-01T00:00:00Z",
            default_branch="main",
            fork=False,
            archived=False,
            stargazers_count=100,
            watchers_count=100,
            subscribers_count=7,
            forks_count=9,
            license={"spdx_id": "NOASSERTION"},
        )
        fields = metadata_fields(data)
        self.assertEqual(fields["watchers"], 7)
        self.assertEqual(fields["stars"], 100)
        self.assertEqual(fields["license_status"], "needs_review")
        data.pop("subscribers_count")
        self.assertIsNone(metadata_fields(data)["watchers"])

    def test_case_insensitive_duplicate_repos_and_path_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "candidates.csv"
            for rows in [
                "a/b,pilot,AI_POLICY.md\nA/B,pilot,AI_POLICY.md\n",
                "a/b,pilot,../secret\n",
                "../repo,pilot,file\n",
            ]:
                path.write_text("repository,priority,policy_paths\n" + rows)
                with self.assertRaises(ValueError):
                    read_candidates(path)

    def test_readme_retains_bytes_and_commit_permalink(self):
        with tempfile.TemporaryDirectory() as tmp:
            content = "# Project\n研究\n".encode()
            response = Response(
                {
                    "path": "README.md",
                    "sha": "blob",
                    "encoding": "base64",
                    "content": base64.b64encode(content).decode(),
                },
                "date",
                "raw.json",
            )
            result = save_document(
                response, Path(tmp), "owner/repo", "commit-sha", "readme"
            )
            self.assertEqual(Path(result["local_path"]).read_bytes(), content)
            self.assertEqual(
                result["source_url"],
                "https://github.com/owner/repo/blob/commit-sha/README.md",
            )

    def test_unsupported_content_is_missing_not_empty(self):
        response = Response(
            {"path": "README.md", "sha": "blob", "encoding": "none", "content": ""},
            "date",
            "raw.json",
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = save_document(response, Path(tmp), "a/b", "sha", "readme")
            self.assertEqual(result["status"], "content_unavailable")
            self.assertNotIn("local_path", result)

    def test_cli_rejects_zero_limit_before_network(self):
        result = CliRunner().invoke(app, ["collect", "profiles", "--limit", "0"])
        self.assertNotEqual(result.exit_code, 0)

    def test_ragged_candidate_csv_is_a_validation_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "candidates.csv"
            path.write_text("repository,priority,policy_paths\na/b,pilot\n")
            with self.assertRaisesRegex(ValueError, "missing or extra cells"):
                read_candidates(path)


class FakeGraphQL:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.variables = []

    def graphql(self, query, variables):
        self.variables.append(variables)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def connection(nodes, more=False, cursor=None, **extra):
    return {
        "nodes": nodes,
        "pageInfo": {"hasNextPage": more, "endCursor": cursor},
        **extra,
    }


class ProjectTests(unittest.TestCase):
    def test_multiple_pages_and_cursor(self):
        client = FakeGraphQL(
            [
                {"node": {"items": connection([1], True, "next")}},
                {"node": {"items": connection([2])}},
            ]
        )
        results = list(pages(client, "query", {"id": "project"}, "node", "items"))
        self.assertEqual([p["nodes"] for p in results], [[1], [2]])
        self.assertEqual(client.variables[1]["cursor"], "next")

    def test_repeated_cursor_is_incomplete(self):
        client = FakeGraphQL([{"node": {"items": connection([], True, "same")}}] * 2)
        with self.assertRaises(APIError):
            list(pages(client, "query", {}, "node", "items"))

    def test_empty_columns_drafts_prs_unassigned_and_archived(self):
        options = [{"id": "todo", "name": "Todo"}, {"id": "done", "name": "Done"}]
        items = [
            {"type": kind, "isArchived": archived, "fieldValueByName": value}
            for kind, archived, value in [
                ("DRAFT_ISSUE", False, {"optionId": "todo"}),
                ("PULL_REQUEST", True, {"optionId": "todo"}),
                ("ISSUE", False, None),
            ]
        ]
        rows = summarize_items(options, items)
        self.assertEqual((rows[0]["active_items"], rows[0]["archived_items"]), (1, 1))
        self.assertEqual(rows[0]["by_type"]["DRAFT_ISSUE"], 1)
        self.assertEqual(rows[1]["active_items"], 0)
        self.assertEqual(rows[2]["active_items"], 1)

    def test_permission_failure_does_not_become_zero_projects(self):
        result = collect_projects(
            FakeGraphQL([APIError(200, "missing read:project")]), "a/b"
        )
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("total_items", result)

    def test_zero_projects_requires_successful_query(self):
        result = collect_projects(
            FakeGraphQL([{"repository": {"projectsV2": connection([])}}]), "a/b"
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["projects"], [])

    def test_partial_item_pagination_withholds_all_counts(self):
        board = {"id": "p1", "public": True}
        field = {
            "id": "s1",
            "name": "Status",
            "options": [{"id": "todo", "name": "Todo"}],
        }
        client = FakeGraphQL(
            [
                {"repository": {"projectsV2": connection([board])}},
                {"node": {"fields": connection([field])}},
                {
                    "node": {
                        "items": connection(
                            [{"id": "item"}], True, "next", totalCount=2
                        )
                    }
                },
                APIError(403, "rate limited"),
            ]
        )
        result = collect_projects(client, "a/b")
        self.assertEqual(result["status"], "partial")
        self.assertNotIn("columns", result["projects"][0])

    def test_private_projects_are_not_queried(self):
        client = FakeGraphQL(
            [
                {
                    "repository": {
                        "projectsV2": connection([{"id": "private", "public": False}])
                    }
                }
            ]
        )
        result = collect_projects(client, "a/b")
        self.assertEqual(result["projects"], [])
        self.assertEqual(result["private_projects_skipped"], 1)
        self.assertEqual(len(client.variables), 1)

    def test_complete_board_counts_across_item_pages(self):
        board = {"id": "p1", "public": True}
        field = {
            "id": "s1",
            "name": "Status",
            "options": [{"id": "todo", "name": "Todo"}],
        }
        item1 = {
            "id": "i1",
            "type": "ISSUE",
            "isArchived": False,
            "fieldValueByName": {"optionId": "todo"},
        }
        item2 = {
            "id": "i2",
            "type": "DRAFT_ISSUE",
            "isArchived": False,
            "fieldValueByName": None,
        }
        client = FakeGraphQL(
            [
                {"repository": {"projectsV2": connection([board])}},
                {"node": {"fields": connection([field])}},
                {"node": {"items": connection([item1], True, "next", totalCount=2)}},
                {"node": {"items": connection([item2], totalCount=2)}},
            ]
        )
        result = collect_projects(client, "a/b")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["projects"][0]["total_items"], 2)
        self.assertEqual(
            [c["active_items"] for c in result["projects"][0]["columns"]], [1, 1]
        )

    def test_changed_board_total_withholds_counts(self):
        client = FakeGraphQL(
            [
                {
                    "repository": {
                        "projectsV2": connection([{"id": "p1", "public": True}])
                    }
                },
                {"node": {"fields": connection([{"name": "Status", "options": []}])}},
                {"node": {"items": connection([], totalCount=1)}},
            ]
        )
        result = collect_projects(client, "a/b")
        self.assertEqual(result["status"], "partial")
        self.assertNotIn("total_items", result["projects"][0])


SUNDAY = 1788048000  # 2026-08-30 00:00 UTC
WEEK = 7 * 86400


def week(offset, days):
    return {"week": SUNDAY + offset * WEEK, "total": sum(days), "days": days}


class FakeREST:
    """Serves canned responses by path, recording each request."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, path, /, **params):
        self.calls.append((path, params))
        if callable(self.routes):
            value = self.routes(path, params)
        else:
            key = (path, params.get("page"))
            value = self.routes.get(key, self.routes.get((path, None)))
        if isinstance(value, Exception):
            raise value
        return Response(value, "2026-09-02T12:00:00+00:00", f"{len(self.calls)}.json")


def star_routes(history_pages, before=5, after=5):
    history = "/repos/a/b/stargazers/history"
    routes = {
        ("/repos/a/b", None): {
            "id": 1,
            "full_name": "a/b",
            "private": False,
            "created_at": "2026-08-31T10:00:00Z",
            "stargazers_count": before,
        },
        ("/repos/a/b/stargazers/count", None): {"count": after},
    }
    for number, page in enumerate(history_pages, 1):
        routes[(history, number)] = page
    return routes


class StarHistoryTests(unittest.TestCase):
    def test_weekly_and_daily_series_are_cumulative_and_trimmed(self):
        from ai_policy_impact.stars import collect_star_history

        # Newest first: the second week has stars through Wednesday 2026-09-09.
        client = FakeREST(
            star_routes(
                [[week(1, [0, 0, 0, 1, 0, 0, 0]), week(0, [0, 2, 1, 1, 0, 0, 0])]]
            )
        )
        record = collect_star_history(client, {"repository": "a/b"})
        self.assertEqual(record["status"], "complete")
        self.assertEqual(record["reconciliation"], "matched")
        self.assertEqual(
            [
                (w["week_start"], w["new_stars"], w["cumulative_stars"])
                for w in record["weekly"]
            ],
            [("2026-08-30", 4, 4), ("2026-09-06", 1, 5)],
        )
        # Days before creation (08-30) and after retrieval (09-02) are not observations.
        self.assertEqual(record["daily"][0]["date"], "2026-08-31")
        self.assertEqual(record["daily"][-1]["date"], "2026-09-02")
        self.assertEqual(record["daily"][0]["cumulative_stars"], 2)

    def test_full_pages_continue_and_short_page_stops(self):
        from ai_policy_impact.stars import WEEKS_PER_PAGE, collect_star_history

        first = [week(-i, [1, 0, 0, 0, 0, 0, 0]) for i in range(WEEKS_PER_PAGE)]
        second = [week(-WEEKS_PER_PAGE, [1, 0, 0, 0, 0, 0, 0])]
        total = WEEKS_PER_PAGE + 1
        routes = star_routes([first, second], before=total, after=total)
        routes[("/repos/a/b", None)]["created_at"] = "2025-01-01T00:00:00Z"
        client = FakeREST(routes)
        record = collect_star_history(client, {"repository": "a/b"})
        self.assertEqual(record["history_pages"], 2)
        self.assertEqual(record["history_total"], total)
        self.assertEqual(record["reconciliation"], "matched")

    def test_gap_between_weeks_fails_instead_of_filling_zeros(self):
        from ai_policy_impact.stars import collect_star_history

        client = FakeREST(
            star_routes([[week(2, [1] + [0] * 6), week(0, [0] * 7)]], 1, 1)
        )
        record = collect_star_history(client, {"repository": "a/b"})
        self.assertEqual(record["status"], "failed")
        self.assertNotIn("weekly", record)
        self.assertIn("not consecutive", record["errors"][0]["error"])

    def test_days_must_sum_to_total(self):
        from ai_policy_impact.stars import validate_weeks

        bad = {"week": SUNDAY, "total": 3, "days": [1, 0, 0, 0, 0, 0, 0]}
        self.assertTrue(validate_weeks([bad]))
        self.assertTrue(
            validate_weeks([{"week": SUNDAY + 3600, "total": 0, "days": [0] * 7}])
        )

    def test_count_mismatch_and_changes_during_run_are_partial(self):
        from ai_policy_impact.stars import collect_star_history

        pages = [[week(0, [0, 1, 0, 0, 0, 0, 0])]]
        mismatch = collect_star_history(
            FakeREST(star_routes(pages, 2, 2)), {"repository": "a/b"}
        )
        self.assertEqual(
            (mismatch["status"], mismatch["reconciliation"]), ("partial", "mismatch")
        )
        moved = collect_star_history(
            FakeREST(star_routes(pages, 1, 2)), {"repository": "a/b"}
        )
        self.assertEqual(moved["reconciliation"], "changed_during_run")

    def test_history_error_is_failure_not_zero_stars(self):
        from ai_policy_impact.stars import collect_star_history

        routes = star_routes([])
        routes[("/repos/a/b/stargazers/history", 1)] = APIError(
            404, "HTTP 404", "x.json"
        )
        record = collect_star_history(FakeREST(routes), {"repository": "a/b"})
        self.assertEqual(record["status"], "failed")
        self.assertNotIn("history_total", record)
        self.assertEqual(record["stars_before"], 5)

    def test_cli_rejects_zero_limit_before_network(self):
        result = CliRunner().invoke(app, ["collect", "stars", "--limit", "0"])
        self.assertNotEqual(result.exit_code, 0)


def listed_commit(sha, committed_at, login="dev", parents=1, email="dev@example.org"):
    return {
        "sha": sha,
        "author": {"login": login, "type": "User"} if login else None,
        "parents": [{}] * parents,
        "commit": {
            "committer": {"date": committed_at},
            "author": {"date": committed_at, "email": email},
            "message": f"commit {sha}",
        },
    }


def commit_detail(sha, committed_at, path, patch, status="modified"):
    detail = listed_commit(sha, committed_at)
    detail["files"] = [{"filename": path, "status": status, "patch": patch}]
    return detail


REPO_METADATA = {
    "id": 1,
    "full_name": "a/b",
    "private": False,
    "default_branch": "main",
}


class PolicyHistoryTests(unittest.TestCase):
    def test_ai_terms_match_policy_language_not_ordinary_words(self):
        from ai_policy_impact.policy_history import AI_TERMS

        for text in ["No AI-generated code", "LLMs may assist", "using Copilot"]:
            self.assertTrue(AI_TERMS.search(text), text)
        for text in ["maintainers said", "AIM of the project", "email the team"]:
            self.assertFalse(AI_TERMS.search(text), text)

    def test_earliest_ai_addition_across_paths_is_the_proposed_date(self):
        from ai_policy_impact.policy_history import collect_policy_history

        contributing = [
            listed_commit("c3", "2026-02-01T00:00:00Z"),
            listed_commit("c2", "2025-11-20T00:00:00Z"),
            listed_commit("c1", "2020-01-01T00:00:00Z"),
        ]
        details = {
            "c3": commit_detail(
                "c3", "2026-02-01T00:00:00Z", "CONTRIBUTING.md", "+Be nice"
            ),
            "c2": commit_detail(
                "c2",
                "2025-11-20T00:00:00Z",
                "CONTRIBUTING.md",
                "+Disclose AI use\n-old",
            ),
            "p1": commit_detail(
                "p1", "2026-01-05T00:00:00Z", "AI_POLICY.md", "+# AI policy", "added"
            ),
        }

        def routes(path, params):
            if path == "/repos/a/b":
                return REPO_METADATA
            if path == "/repos/a/b/commits":
                return {
                    "CONTRIBUTING.md": contributing,
                    "AI_POLICY.md": [listed_commit("p1", "2026-01-05T00:00:00Z")],
                }[params["path"]]
            if path.startswith("/repos/a/b/commits/"):
                return details[path.rsplit("/", 1)[1]]
            if path == "/repos/a/b/contents/CONTRIBUTING.md":
                return {"content": base64.b64encode(b"Please test.").decode()}
            raise AssertionError(path)

        record = collect_policy_history(
            FakeREST(routes),
            {
                "repository": "a/b",
                "policy_paths": "CONTRIBUTING.md|AI_POLICY.md",
                "reported_policy_date": "2025-11-14",
            },
        )
        self.assertEqual(record["status"], "complete")
        self.assertEqual(record["review_status"], "needs_review")
        self.assertEqual(record["detected_policy_date"], "2025-11-20")
        self.assertEqual(record["detected_sha"], "c2")
        self.assertEqual(record["detected_minus_reported_days"], 6)
        self.assertEqual(record["ai_commits"], 2)
        # c1 predates the inspection window; the file then had no AI text.
        self.assertFalse(record["paths"][0]["ai_text_before_inspection_window"])
        self.assertEqual(
            [c["sha"] for c in record["paths"][0]["commits"]], ["c2", "c3"]
        )

    def test_missing_path_is_flagged_not_dated(self):
        from ai_policy_impact.policy_history import collect_policy_history

        def routes(path, params):
            return REPO_METADATA if path == "/repos/a/b" else []

        record = collect_policy_history(
            FakeREST(routes), {"repository": "a/b", "policy_paths": "AI_POLICY.md"}
        )
        self.assertNotIn("detected_policy_date", record)
        self.assertEqual(record["flags"], ["AI_POLICY.md: no_history_at_path"])


class ContributorTests(unittest.TestCase):
    def setUp(self):
        from ai_policy_impact.config import HorizonConfig

        self.horizons = HorizonConfig(medium_run_days=180, long_run_days=365)

    def test_band_edges_are_half_open(self):
        from datetime import date, timedelta

        from ai_policy_impact.contributors import band_bounds, band_for

        anchor = date(2025, 11, 20)
        bounds = band_bounds(anchor, self.horizons)
        self.assertEqual(band_for(anchor, bounds), "post_medium")
        self.assertEqual(band_for(anchor - timedelta(days=1), bounds), "pre_medium")
        self.assertEqual(band_for(anchor - timedelta(days=180), bounds), "pre_medium")
        self.assertEqual(band_for(anchor - timedelta(days=181), bounds), "pre_long")
        self.assertEqual(band_for(anchor + timedelta(days=180), bounds), "post_long")
        self.assertIsNone(band_for(anchor + timedelta(days=365), bounds))
        self.assertIsNone(band_for(anchor - timedelta(days=366), bounds))

    def test_unlinked_authors_are_hashed_and_bots_flagged(self):
        from ai_policy_impact.contributors import contributor_id

        ident, login, bot = contributor_id(
            listed_commit(
                "x", "2026-01-01T00:00:00Z", login=None, email="Me@Example.org"
            )
        )
        self.assertTrue(ident.startswith("email:"))
        self.assertNotIn("example", ident.lower())
        self.assertIsNone(login)
        self.assertFalse(bot)
        commit = listed_commit("y", "2026-01-01T00:00:00Z", login="dependabot[bot]")
        self.assertTrue(contributor_id(commit)[2])

    def test_presence_merges_bots_and_censored_bands(self):
        from datetime import date

        from ai_policy_impact.contributors import collect_repository_contributors

        commits = [
            listed_commit("1", "2025-03-01T00:00:00Z", "stayer"),
            listed_commit("2", "2025-12-01T00:00:00Z", "stayer"),
            listed_commit("3", "2025-10-01T00:00:00Z", "leaver"),
            listed_commit("4", "2025-12-15T00:00:00Z", "newcomer"),
            listed_commit("5", "2025-12-16T00:00:00Z", "maintainer", parents=2),
            listed_commit("6", "2025-12-17T00:00:00Z", "renovate[bot]"),
        ]

        def routes(path, params):
            if path == "/repos/a/b":
                return REPO_METADATA
            if path == "/repos/a/b/contributors":
                return [{"login": "stayer", "type": "User", "contributions": 9}]
            if path == "/repos/a/b/commits":
                return commits
            raise AssertionError(path)

        client = FakeREST(routes)
        record = collect_repository_contributors(
            client,
            {"repository": "a/b"},
            date(2025, 11, 20),
            "detected",
            self.horizons,
            today=date(2026, 1, 31),
        )
        self.assertEqual(record["status"], "complete")
        self.assertEqual(
            (record["retained"], record["pre_only"], record["post_only"]), (1, 1, 1)
        )
        self.assertEqual(
            (record["contributors_pre"], record["contributors_post"]), (2, 2)
        )
        self.assertEqual(record["bot_accounts"], 1)
        self.assertEqual(record["merge_commits"], 1)
        self.assertNotIn("maintainer", [c["login"] for c in record["contributors"]])
        # post_long starts 2026-05-19, after "today": missing, not zero.
        self.assertEqual(record["post_long_days_observed"], 0)
        self.assertIsNone(record["post_long_contributors"])
        self.assertEqual(record["post_medium_days_observed"], 73)
        self.assertEqual(record["window_end"], "2026-01-31")
        commit_params = next(
            p for path, p in client.calls if path == "/repos/a/b/commits"
        )
        self.assertEqual(commit_params["since"], "2024-11-20T00:00:00Z")

    def test_too_large_contributor_list_is_not_zero(self):
        from datetime import date

        from ai_policy_impact.contributors import collect_repository_contributors

        def routes(path, params):
            if path == "/repos/a/b":
                return REPO_METADATA
            if path == "/repos/a/b/contributors":
                return APIError(403, "HTTP 403: too large", "x.json")
            return []

        record = collect_repository_contributors(
            FakeREST(routes),
            {"repository": "a/b"},
            None,
            "none",
            self.horizons,
            date.today(),
        )
        self.assertEqual(record["overall_status"], "too_large_for_api")
        self.assertNotIn("overall_linked_accounts", record)
        self.assertEqual(record["status"], "partial")

    def test_anchor_follows_configured_event_without_fallback(self):
        from datetime import date

        from ai_policy_impact.config import AnchorEvent
        from ai_policy_impact.windows import anchor_dates, require_policy_run

        first, reported = AnchorEvent.FIRST_AI_MENTION, AnchorEvent.REPORTED_POLICY_DATE
        candidate = {"repository": "A/B", "reported_policy_date": "2025-11-14"}
        self.assertEqual(
            anchor_dates(candidate, {"a/b": "2025-11-20"}, first),
            (date(2025, 11, 20), "detected"),
        )
        # No detected mention: no anchor, not the reported date.
        self.assertEqual(anchor_dates(candidate, {}, first), (None, "none"))
        self.assertEqual(
            anchor_dates(candidate, {"a/b": "2025-11-20"}, reported),
            (date(2025, 11, 14), "reported"),
        )
        self.assertEqual(
            anchor_dates({"repository": "c/d"}, {}, reported), (None, "none")
        )
        with self.assertRaisesRegex(ValueError, "--policy-run"):
            require_policy_run(first, None)
        require_policy_run(reported, None)

    def test_new_commands_reject_zero_limit_before_network(self):
        for command in ["policy-history", "contributors"]:
            result = CliRunner().invoke(app, ["collect", command, "--limit", "0"])
            self.assertNotEqual(result.exit_code, 0, command)


class AIToolFileTests(unittest.TestCase):
    def test_tool_instruction_files_found_and_vendored_skipped(self):
        from ai_policy_impact.collect import ai_tool_files

        paths = [
            "AGENTS.md",
            "src/web/AGENTS.md",
            "CLAUDE.md",
            ".claude/settings.json",
            ".github/copilot-instructions.md",
            ".github/instructions/python.instructions.md",
            ".cursor/rules/style.mdc",
            ".cursorrules",
            "vendor/lib/AGENTS.md",
            "docs/agents.txt",
            "CONTRIBUTING.md",
        ]
        self.assertEqual(
            ai_tool_files(paths),
            [
                ".claude/settings.json",
                ".cursor/rules/style.mdc",
                ".cursorrules",
                ".github/copilot-instructions.md",
                ".github/instructions/python.instructions.md",
                "AGENTS.md",
                "CLAUDE.md",
                "src/web/AGENTS.md",
            ],
        )


if __name__ == "__main__":
    unittest.main()
