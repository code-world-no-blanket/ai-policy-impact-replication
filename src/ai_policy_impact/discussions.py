"""GitHub Discussions threads and their comments around a policy date.

Discussions on other platforms (mailing lists, chat, forums) are out of scope.
A repository with Discussions turned off has none on GitHub; that is recorded as
`discussions_enabled: false` with empty lists, using the current setting only.
"""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path

from ai_policy_impact.activity import (
    begin_record,
    coverage_fields,
    fail_component,
    is_bot_actor,
    run_banded_collection,
    sweep_updated,
)
from ai_policy_impact.config import HorizonConfig, StudyConfig
from ai_policy_impact.github import APIError, GitHubClient, utc_now
from ai_policy_impact.runs import write_csv
from ai_policy_impact.windows import BANDS, Window, parse_time

PAGE_SIZE = 20
MAX_PAGES = 2000
COMMENTS_PER_DISCUSSION = 50
REPLIES_PER_COMMENT = 30

QUERY = """query($owner:String!,$name:String!,$first:Int!,$cursor:String) {
  rateLimit { cost remaining resetAt }
  repository(owner:$owner,name:$name) {
    discussions(first:$first,after:$cursor,orderBy:{field:UPDATED_AT,direction:DESC}) {
      nodes { number createdAt updatedAt answerChosenAt category { name }
        author { __typename login }
        comments(first:%d) { totalCount
          nodes { createdAt author { __typename login }
            replies(first:%d) { totalCount nodes { createdAt author { __typename login } } }
          }
        }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}""" % (COMMENTS_PER_DISCUSSION, REPLIES_PER_COMMENT)


def post(node: dict, number: int, kind: str) -> dict:
    author = node.get("author") or {}
    return {
        "number": number,
        "kind": kind,
        "author": author.get("login"),
        "author_is_bot": is_bot_actor(author),
        "created_at": node["createdAt"],
    }


def discussion_posts(node: dict, window: Window) -> tuple[list[dict], dict]:
    """The opening post, comments and replies created in the window, and truncation."""
    number = node["number"]
    posts = [post(node, number, "discussion")]
    comments = node.get("comments") or {}
    truncated = {
        "comments_truncated": comments.get("totalCount", 0)
        > len(comments.get("nodes") or []),
        "replies_truncated": False,
    }
    for comment in comments.get("nodes") or []:
        posts.append(post(comment, number, "comment"))
        replies = comment.get("replies") or {}
        nodes = replies.get("nodes") or []
        truncated["replies_truncated"] |= replies.get("totalCount", 0) > len(nodes)
        posts += [post(reply, number, "reply") for reply in nodes]
    kept = []
    for row in posts:
        created = parse_time(row["created_at"])
        band = window.band(created)
        if band is not None and created >= window.start_time:
            kept.append({**row, "band": band})
    return kept, truncated


def summarize_discussions(rows: list[dict], window: Window) -> dict:
    humans = [r for r in rows if not r["author_is_bot"]]
    summary = {"discussion_posts_in_window": len(rows), **coverage_fields(window)}
    for name in BANDS:
        band = [r for r in humans if r["band"] == name]
        observed = window.observed(name)
        summary[f"{name}_discussions_opened"] = (
            sum(r["kind"] == "discussion" for r in band) if observed else None
        )
        summary[f"{name}_discussion_comments"] = (
            sum(r["kind"] != "discussion" for r in band) if observed else None
        )
        summary[f"{name}_discussion_participants"] = (
            len({(r["author"] or "").lower() for r in band}) if observed else None
        )
    return summary


def collect_repository_discussions(
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
    window = Window.around(anchor, horizons, today)
    record["discussions_enabled"] = bool(details.get("has_discussions"))
    rows: list[dict] = []
    truncated = {"comments_truncated": 0, "replies_truncated": 0}
    if record["discussions_enabled"]:
        owner, name = record["repository"].split("/")
        try:
            # A new comment or reply updates its discussion, so the sweep finds it.
            nodes = sweep_updated(
                client,
                QUERY,
                {"owner": owner, "name": name},
                lambda data: (data.get("repository") or {}).get("discussions"),
                window.start_time,
                max_pages=MAX_PAGES,
                page_size=PAGE_SIZE,
                sleep=sleep,
            )
        except APIError as error:
            return fail_component(record, "discussions", error)
        for node in nodes.values():
            posts, flags = discussion_posts(node, window)
            rows += posts
            for key, flag in flags.items():
                truncated[key] += flag
    record["posts"] = sorted(rows, key=lambda r: r["created_at"])
    record.update(
        discussions_with_comments_truncated=truncated["comments_truncated"],
        discussions_with_replies_truncated=truncated["replies_truncated"],
    )
    record.update(summarize_discussions(rows, window))
    record["completed_at"] = utc_now()
    return record


SUMMARY_COLUMNS = [
    "requested_repository",
    "repository",
    "status",
    "discussions_enabled",
    "anchor_date",
    "anchor_source",
    "reported_policy_date",
    "window_start",
    "window_end",
    "discussion_posts_in_window",
    "discussions_with_comments_truncated",
    "discussions_with_replies_truncated",
    *[
        f"{b}_{m}"
        for b in BANDS
        for m in (
            "days_observed",
            "discussions_opened",
            "discussion_comments",
            "discussion_participants",
        )
    ],
]
POST_COLUMNS = [
    "repository",
    "number",
    "kind",
    "author",
    "author_is_bot",
    "created_at",
    "band",
]


def write_outputs(interim: Path, records: list[dict]) -> None:
    write_csv(interim / "summary.csv", SUMMARY_COLUMNS, records)
    write_csv(
        interim / "posts.csv",
        POST_COLUMNS,
        [
            {"repository": r.get("repository", r["requested_repository"]), **row}
            for r in records
            for row in r.get("posts", [])
        ],
    )


def collect_discussions(
    study: StudyConfig, candidates_path: Path, **options
) -> tuple[Path, dict]:
    return run_banded_collection(
        study,
        candidates_path,
        kind="discussions",
        measurement="github_discussions_around_unverified_anchor",
        collector=collect_repository_discussions,
        write_outputs=write_outputs,
        describe=lambda r: (
            f"enabled: {r.get('discussions_enabled')}; "
            f"posts in window: {r.get('discussion_posts_in_window')}"
        ),
        **options,
    )
