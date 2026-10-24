"""Offline tests for comparison-repository selection and stand-in dates."""

import ast
import base64
import random
import unittest
from collections import Counter
from datetime import date
from pathlib import Path

from ai_policy_impact.ai_terms import CONTRIBUTING_TERMS, MENTIONS_AI
from ai_policy_impact.comparison import (
    DEFAULT_BOUNDS,
    JITTER_DAYS,
    cluster_of,
    distance,
    draw_dates,
    policy_signal,
    quality,
)
from ai_policy_impact.config import AnchorEvent
from ai_policy_impact.windows import anchor_dates
from test_outcomes import FakeClient

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def script_terms(name: str) -> list[str]:
    tree = ast.parse((SCRIPTS / name).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "CONTRIBUTING_TERMS" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"no CONTRIBUTING_TERMS in {name}")


class TermTests(unittest.TestCase):
    def test_scripts_and_package_use_the_same_terms(self):
        for name in ("find_ai_policy_repos.py", "find_no_ai_policy_repos.py"):
            self.assertEqual(script_terms(name), CONTRIBUTING_TERMS, name)

    def test_phrases_and_noise(self):
        for text in ["AI-assisted code", "large  language\nmodels", "a coding-agent"]:
            self.assertTrue(MENTIONS_AI.search(text), text)
        for text in ["user-agent header", "maintain", "claudette"]:
            self.assertFalse(MENTIONS_AI.search(text), text)


class DateTests(unittest.TestCase):
    def test_stand_in_date_overrides_the_policy_event(self):
        anchor, source = anchor_dates(
            {"repository": "x/y", "stand_in_date": "2026-02-03"},
            {},
            AnchorEvent.FIRST_AI_MENTION,
        )
        self.assertEqual((anchor, source), (date(2026, 2, 3), "stand_in"))

    def test_draw_keeps_cluster_shares_jitters_and_repeats(self):
        dates = {
            "a/early1": date(2024, 10, 14),
            "a/early2": date(2025, 8, 19),
            "a/mid": date(2026, 2, 13),
            "a/late1": date(2026, 6, 1),
            "a/late2": date(2026, 7, 15),
        }
        today = date(2026, 7, 20)
        drawn = draw_dates(dates, DEFAULT_BOUNDS, random.Random(7), today)
        self.assertEqual(
            Counter(c for c, _, _ in drawn.values()),
            Counter(cluster_of(d, DEFAULT_BOUNDS) for d in dates.values()),
        )
        for repo, (cluster, base, stand_in) in drawn.items():
            self.assertEqual(cluster, cluster_of(dates[repo], DEFAULT_BOUNDS))
            self.assertEqual(cluster_of(base, DEFAULT_BOUNDS), cluster)
            self.assertLessEqual(abs((stand_in - base).days), JITTER_DAYS)
            self.assertLessEqual(stand_in, today)
        self.assertEqual(
            drawn, draw_dates(dates, DEFAULT_BOUNDS, random.Random(7), today)
        )
        # Without replacement: each policy date is used exactly once.
        self.assertEqual(
            sorted(base for _, base, _ in drawn.values()), sorted(dates.values())
        )


def contents(text: str) -> dict:
    return {"content": base64.b64encode(text.encode()).decode()}


class SignalTests(unittest.TestCase):
    def tree(self, *paths, truncated=False):
        return {
            "truncated": truncated,
            "tree": [{"type": "blob", "path": p} for p in paths],
        }

    def test_policy_file_contributing_terms_and_unknown(self):
        cases = [
            (
                {"/repos/x/y/git/trees/main": self.tree("docs/AI_POLICY.md")},
                "docs/AI_POLICY.md",
            ),
            (
                {
                    "/repos/x/y/git/trees/main": self.tree("CONTRIBUTING.md"),
                    "/repos/x/y/contents/CONTRIBUTING.md": contents("No LLM output."),
                },
                "CONTRIBUTING.md mentions 'LLM'",
            ),
            (
                {
                    "/repos/x/y/git/trees/main": self.tree("CONTRIBUTING.md"),
                    "/repos/x/y/contents/CONTRIBUTING.md": contents(
                        "Set the user-agent."
                    ),
                },
                "",
            ),
            ({"/repos/x/y/git/trees/main": self.tree("a", truncated=True)}, None),
        ]
        for rest, expected in cases:
            self.assertEqual(
                policy_signal(FakeClient(rest=rest), "x/y", "main"), expected
            )


class MatchTests(unittest.TestCase):
    def test_distance_prefers_activity_then_stars(self):
        policy = {"prs": 100, "stars": 10000, "owner_type": "Organization"}
        busy = {"prs": 110, "stars": 4000, "owner_type": "Organization"}
        quiet = {"prs": 5, "stars": 10000, "owner_type": "Organization"}
        self.assertLess(distance(busy, policy), distance(quiet, policy))
        self.assertEqual(quality(busy, policy), "good")
        self.assertEqual(quality(quiet, policy), "weak")


if __name__ == "__main__":
    unittest.main()
