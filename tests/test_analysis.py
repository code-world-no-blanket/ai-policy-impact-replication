import tempfile
import unittest
from pathlib import Path

try:
    import numpy as np
    import pandas as pd

    from ai_policy_impact import analysis
except ImportError:  # the `analysis` dependency group is not installed
    analysis = None

EFFECTS = {"permissive": 0.0, "restrictive": 0.2, "banned": 0.4}


def synthetic_panel(directory: Path, seed: int = 1, stagger: int = 0) -> None:
    """60 comparison and 60 policy repositories; known PPML effects by type.

    With `stagger`, adoption dates spread over that many months.
    """
    rng = np.random.default_rng(seed)
    rows, characteristics = [], []
    for i in range(120):
        repository = f"o/r{i}"
        policy = i >= 60
        kind = analysis.POLICY_TYPES[i % 3] if policy else None
        base = rng.uniform(2, 8)
        shift = (i % stagger) * 4 if stagger else 0
        for week in range(-30, 20):
            start = pd.Timestamp("2026-03-02") + pd.Timedelta(
                weeks=week + i % 5 + shift
            )
            effect = EFFECTS[kind] if policy and week >= 0 else 0.0
            mu = base * np.exp(effect)
            rows.append({
                "repository": repository,
                "group": "policy" if policy else "comparison",
                "event_week": week,
                "week_start": start.date().isoformat(),
                "days_observed": 7,
                **{column: rng.poisson(mu) for column in analysis.PRIMARY},
            })
        characteristics.append({
            "repository": repository,
            "policy_type": kind,
            # Comparison repositories are smaller, as in the study.
            "stars_at_policy": rng.integers(1000, 9000) if policy
            else rng.integers(10, 5000),
            "age_at_policy_years": rng.uniform(2, 10),
            "owner_type": "Organization" if rng.random() < (0.7 if policy else 0.4)
            else "User",
        })
    pd.DataFrame(rows).to_csv(directory / "panel.csv", index=False)
    pd.DataFrame(characteristics).to_csv(directory / "characteristics.csv", index=False)


@unittest.skipIf(analysis is None, "analysis dependency group not installed")
class AnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        synthetic_panel(cls.dir)
        cls.panel = analysis.load_panel(cls.dir)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_average_effect_recovers_mean_of_type_effects(self):
        effects = analysis.average_effects(self.panel, bootstrap_reps=0)
        row = effects[(effects["outcome"] == "prs_opened") & (effects["model"] == "ppml")]
        self.assertAlmostEqual(row["estimate"].item(), 0.2, delta=0.08)
        self.assertTrue(row["primary"].item())
        self.assertTrue(effects.loc[effects["model"].eq("ppml") & effects["primary"],
                                    "p_holm"].notna().all())

    def test_policy_types_recover_ordering_and_trend(self):
        types = analysis.policy_types(self.panel, ["prs_opened"]).set_index("term")
        for kind, effect in EFFECTS.items():
            self.assertAlmostEqual(types.loc[kind, "estimate"], effect, delta=0.1)
        self.assertAlmostEqual(types.loc["strictness trend", "estimate"], 0.2, delta=0.06)
        self.assertLess(types.loc["banned - permissive", "p_holm"], 0.05)

    def test_event_study_is_flat_before_and_includes_reference(self):
        events, pretrends = analysis.event_study(self.panel, ["prs_opened"])
        reference = events[events["event_bin"] == analysis.REFERENCE_BIN]
        self.assertEqual(reference["estimate"].item(), 0.0)
        self.assertGreater(pretrends["p"].item(), 0.01)

    def test_untyped_policy_repository_is_rejected_when_others_are_typed(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            synthetic_panel(directory)
            types = pd.read_csv(directory / "characteristics.csv")
            types.loc[types["repository"] == "o/r60", "policy_type"] = None
            types.to_csv(directory / "characteristics.csv", index=False)
            with self.assertRaises(ValueError):
                analysis.load_panel(directory)

    def test_stacked_design_recovers_effect_with_later_adopters_as_controls(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            synthetic_panel(directory, stagger=8)
            panel = analysis.load_panel(directory)
        stacked, cohorts = analysis.stack_panel(panel)
        self.assertTrue(cohorts["used"].any())
        self.assertFalse(cohorts["used"].all())  # the last cohorts have no controls
        self.assertEqual(set(stacked["group"]), {"policy"})
        # Controls are never observed at or after their own adoption.
        controls = stacked[stacked["treated"].eq(0)]
        self.assertTrue((controls["event_week"] < 0).all())
        treated = stacked[stacked["treated"].eq(1)]
        self.assertTrue(treated["event_week"].between(
            -analysis.PRE_WEEKS, analysis.POST_WEEKS - 1).all())
        effects = analysis.average_effects(
            stacked, 0, analysis.STACKED_EFFECTS
        )
        row = effects[(effects["outcome"] == "prs_opened") & (effects["model"] == "ppml")]
        self.assertAlmostEqual(row["estimate"].item(), 0.2, delta=0.1)

    def test_balance_weights_match_policy_means(self):
        weights, balance = analysis.balance_weights(self.panel)
        self.assertTrue((balance["std_diff_after"].abs() < 0.01).all())
        self.assertGreater(balance["std_diff_before"].abs().max(), 0.2)
        policy = self.panel.loc[self.panel["treated"].eq(1), "repository"].unique()
        self.assertTrue((weights[policy] == 1).all())
        comparison = weights.drop(policy)
        self.assertAlmostEqual(comparison.mean(), 1.0)
        self.assertLess(analysis.effective_size(comparison), len(comparison))

    def test_nice_ticks_span_the_range(self):
        self.assertEqual(analysis.nice_ticks(-0.37, 0.8), [-0.5, 0.0, 0.5, 1.0])
        self.assertEqual(analysis.nice_ticks(-28, 8, 5), [-30, -20, -10, 0, 10])

    def test_main_design_can_be_chosen(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, summary = analysis.analyze(
                self.dir, Path(tmp), bootstrap_reps=0, main_design="comparison",
                progress=lambda _: None,
            )
            self.assertEqual(summary["main_design"], "comparison")
            with self.assertRaises(ValueError):
                analysis.analyze(self.dir, Path(tmp), main_design="matched",
                                 progress=lambda _: None)

    def test_holm(self):
        self.assertEqual(analysis.holm([0.01, 0.04, 0.03]), [0.03, 0.06, 0.06])

    def test_analyze_writes_outputs_and_paper_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, paper = Path(tmp) / "out", Path(tmp) / "paper"
            output, summary = analysis.analyze(
                self.dir, out, paper_dir=paper, bootstrap_reps=0, progress=lambda _: None
            )
            self.assertTrue(summary["policy_types_estimated"])
            self.assertEqual(summary["main_design"], "comparison_weighted")
            for name in ("effects.csv", "policy_types.csv", "summary.json",
                         "balance.csv", "cohorts.csv"):
                self.assertTrue((output / name).is_file())
            figure = (paper / "figures" / "event_study.tex").read_text()
            self.assertIn(r"\begin{tikzpicture}", figure)
            self.assertIn("Weeks relative to policy adoption", figure)
            self.assertIn(r"\begin{tikzpicture}",
                          (paper / "figures" / "policy_types.tex").read_text())
            self.assertIn(r"\toprule", (paper / "tables" / "effects.tex").read_text())
            self.assertIn("Weighted comparison",
                          (paper / "tables" / "designs.tex").read_text())


if __name__ == "__main__":
    unittest.main()
