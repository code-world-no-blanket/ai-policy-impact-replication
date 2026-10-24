"""Policy-effect estimates from the repository-week panel.

Every repository is aligned on its own event week: the policy date for policy
repositories, the assigned stand-in date for comparison repositories. Effects
are Poisson pseudo-maximum-likelihood (PPML) estimates of `treated_post` (a
policy repository in week 0 or later), clustered by repository, under three
designs:

- stacked (main): policy repositories only. Each adoption-month cohort is
  compared, in calendar time, with policy repositories that adopt after the
  cohort's post window ends, using only their pre-adoption weeks. Weeks -26 to
  11 around adoption; fixed effects for repository and calendar week, each
  within its stack. Controls are projects that will soon adopt a policy, so the
  design does not rely on comparison repositories at all.
- comparison_weighted: policy repositories against the comparison repositories,
  reweighted by entropy balancing to match the policy repositories' pre-period
  activity, growth, stars, age and owner type. Repository, event-week and
  calendar-month fixed effects; all observed weeks.
- comparison: the same without weights, for reference.

log(1 + y) OLS is the robustness check, with wild cluster bootstrap p-values
for the primary outcomes of the unweighted designs (blank for the stacked design,
whose stack-specific fixed effects the bootstrap cannot handle). Partly observed weeks are dropped.
Event-study coefficients use four-week bins, relative to the four weeks before
the anchor; their pre-adoption bins give a joint pre-trend test per design.

RQ3 compares policy types: one effect per type (pairwise differences, Holm
corrected) and a trend across strictness (permissive 0, restrictive 1,
banned 2).

Needs the `analysis` dependency group: `uv run --group analysis aip analyze`.
"""

from __future__ import annotations

import math
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import pyfixest as pf
from scipy import optimize, stats

from ai_policy_impact.github import utc_now, write_json
from ai_policy_impact.runs import new_run_id

# Outcome column -> (research question, label). The first four are primary.
OUTCOMES = {
    "prs_opened": ("RQ1", "PRs opened"),
    "median_hours_to_first_review_by_opened": ("RQ1", "Hours to first review"),
    "active_contributors": ("RQ2", "Active contributors"),
    "first_time_pr_authors": ("RQ2", "First-time PR authors"),
    "prs_merged": ("RQ1", "PRs merged"),
    "prs_closed_unmerged": ("RQ1", "PRs closed unmerged"),
    "open_prs": ("RQ1", "Open-PR backlog"),
    "median_hours_to_merge_by_opened": ("RQ1", "Hours to merge"),
    "median_reviews_per_pr_by_opened": ("RQ1", "Reviews per PR"),
    "prs_changes_requested": ("RQ1", "PRs with changes requested"),
    "prs_ci_failure": ("RQ1", "PRs with CI failure"),
    "median_pr_lines_by_opened": ("RQ1", "PR size (lines)"),
    "commits": ("RQ1", "Commits"),
    "lines_added": ("RQ1", "Lines added"),
    "issues_opened": ("RQ1", "Issues opened"),
    "issues_closed": ("RQ1", "Issues closed"),
    "releases": ("RQ1", "Releases"),
    "newcomers": ("RQ2", "Newcomers"),
    "returning_contributors": ("RQ2", "Returning contributors"),
    "pr_authors": ("RQ2", "PR authors"),
    "pr_comments": ("RQ2", "PR comments"),
    "issue_comments": ("RQ2", "Issue comments"),
    "discussions_opened": ("RQ2", "Discussions opened"),
    "new_stars": ("RQ2", "New stars"),
    "new_forks": ("RQ2", "New forks"),
}
PRIMARY = list(OUTCOMES)[:4]
STRICTNESS = {"permissive": 0, "restrictive": 1, "banned": 2}
POLICY_TYPES = list(STRICTNESS)
BIN_WEEKS = 4
REFERENCE_BIN = -1
FIXED_EFFECTS = "repository + event_week + month"
STACKED_EFFECTS = "repo_stack + week_stack"
# Stacked design: weeks around adoption kept for a cohort, and the fewest
# not-yet-treated repositories a cohort needs to be estimated.
PRE_WEEKS = 26
POST_WEEKS = 12
MIN_CONTROLS = 3
DESIGNS = ("stacked", "comparison_weighted", "comparison")
# Pre-period repository traits the comparison weights balance.
BALANCE = {
    "log_prs": "log(1 + PRs/week), pre-period",
    "log_pr_authors": "log(1 + PR authors/week), pre-period",
    "prs_growth": "Change in log(1 + PRs/week), weeks -26..-1 vs -52..-27",
    "log_stars": "log(1 + stars) at reference date",
    "age_years": "Age at reference date (years)",
    "organization": "Owned by an organization",
}
VCOV = {"CRV1": "repository_id"}  # numeric: the wild bootstrap needs it
BOOTSTRAP_REPS = 9999
SEED = 20261019
# Light-mode categorical slots 1-3 (dataviz reference palette) and a neutral.
COLORS = {"permissive": "#1baf7a", "restrictive": "#eb6834", "banned": "#2a78d6"}
EFFECT_COLOR = "#2a78d6"
NEUTRAL = "#6b6a63"
GRID = "#e4e3dd"


def load_panel(panel_dir: Path) -> pd.DataFrame:
    """Fully observed repository-weeks, with treatment and policy-type columns."""
    panel = pd.read_csv(panel_dir / "panel.csv", low_memory=False)
    traits = pd.read_csv(panel_dir / "characteristics.csv")
    keep = ["repository", "policy_type", "stars_at_policy", "age_at_policy_years",
            "owner_type"]
    panel = panel.merge(
        traits[[c for c in keep if c in traits]], on="repository", how="left"
    )
    panel = panel[panel["days_observed"] == 7].copy()
    treated = panel["group"].eq("policy")
    coded = panel["policy_type"].isin(POLICY_TYPES)
    unknown = sorted(set(panel.loc[treated & ~coded, "repository"]))
    if unknown and (treated & coded).any():
        # All or nothing: RQ3 is skipped only when no policy type is coded.
        raise ValueError(f"Policy repositories without a policy type: {unknown}")
    panel["repository_id"] = pd.factorize(panel["repository"])[0]
    panel["treated"] = treated.astype(int)
    panel["post"] = (panel["event_week"] >= 0).astype(int)
    panel["treated_post"] = panel["treated"] * panel["post"]
    for kind in POLICY_TYPES:
        panel[f"post_{kind}"] = (
            panel["treated_post"] * panel["policy_type"].eq(kind)
        ).astype(int)
    panel["treated_post_strictness"] = panel["treated_post"] * panel[
        "policy_type"
    ].map(STRICTNESS).fillna(0)
    panel["event_bin"] = panel["event_week"] // BIN_WEEKS
    panel["month"] = panel["week_start"].str[:7]
    start = pd.to_datetime(panel["week_start"])
    panel["anchor"] = start - pd.to_timedelta(7 * panel["event_week"], unit="D")
    monday = start - pd.to_timedelta(start.dt.weekday, unit="D")
    panel["calendar_week"] = monday.dt.strftime("%Y-%m-%d")
    panel["weight"] = 1.0
    for column in OUTCOMES:
        if column in panel:
            panel[f"log1p_{column}"] = np.log1p(panel[column])
    return panel


def fit(formula: str, data: pd.DataFrame, poisson: bool, weighted: bool = False):
    """A fitted model, or the error message when it cannot be estimated."""
    weights = "weight" if weighted else None
    try:
        if poisson:
            return pf.fepois(formula, data=data, vcov=VCOV, weights=weights)
        return pf.feols(formula, data=data, vcov=VCOV, weights=weights)
    except Exception as error:  # pyfixest raises many types; keep the run going.
        return f"{type(error).__name__}: {error}"


def coefficient(model, name: str) -> dict:
    estimate = float(model.coef()[name])
    se = float(model.se()[name])
    return {
        "estimate": estimate,
        "se": se,
        "p": float(model.pvalue()[name]),
        "ci_low": estimate - 1.96 * se,
        "ci_high": estimate + 1.96 * se,
        "pct": math.expm1(estimate),
        "n_obs": int(model._N),
    }


def linear_combination(model, weights: dict[str, float]) -> dict:
    """A weighted sum of coefficients with its cluster-robust standard error."""
    names = list(model._coefnames)
    vector = np.array([weights.get(name, 0.0) for name in names])
    estimate = float(vector @ np.asarray(model.coef()))
    se = float(math.sqrt(vector @ model._vcov @ vector))
    return {
        "estimate": estimate,
        "se": se,
        "p": float(2 * stats.norm.sf(abs(estimate / se))),
        "ci_low": estimate - 1.96 * se,
        "ci_high": estimate + 1.96 * se,
        "pct": math.expm1(estimate),
    }


def holm(pvalues: list[float]) -> list[float]:
    """Holm-adjusted p-values, in the input order."""
    order = sorted(range(len(pvalues)), key=lambda i: pvalues[i])
    adjusted = [0.0] * len(pvalues)
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (len(pvalues) - rank) * pvalues[i]))
        adjusted[i] = running
    return adjusted


def average_effects(
    panel: pd.DataFrame,
    bootstrap_reps: int,
    fixed_effects: str = FIXED_EFFECTS,
    weighted: bool = False,
) -> pd.DataFrame:
    """RQ1/RQ2: one PPML and one log(1 + y) OLS effect per outcome."""
    rows = []
    for outcome, (rq, label) in OUTCOMES.items():
        if outcome not in panel:
            continue
        base = {"outcome": outcome, "label": label, "rq": rq,
                "primary": outcome in PRIMARY}
        for model_name, formula, poisson in (
            ("ppml", f"{outcome} ~ treated_post | {fixed_effects}", True),
            ("log1p_ols", f"log1p_{outcome} ~ treated_post | {fixed_effects}", False),
        ):
            model = fit(formula, panel, poisson, weighted)
            if isinstance(model, str):
                rows.append({**base, "model": model_name, "error": model})
                continue
            row = {**base, "model": model_name, **coefficient(model, "treated_post")}
            if model_name == "log1p_ols" and base["primary"] and bootstrap_reps:
                boot = model.wildboottest(
                    param="treated_post", reps=bootstrap_reps, seed=SEED
                )
                # The bootstrap gives a NaN t (and p = 0) with the stacked
                # design's stack-specific fixed effects; report it as missing.
                t_value = float(boot["t value"])
                row["p_wild_bootstrap"] = (
                    float(boot["Pr(>|t|)"]) if np.isfinite(t_value) else np.nan
                )
            rows.append(row)
    table = pd.DataFrame(rows)
    main = table["model"].eq("ppml") & table["primary"] & table["p"].notna()
    table.loc[main, "p_holm"] = holm(table.loc[main, "p"].tolist())
    return table


def event_study(
    panel: pd.DataFrame,
    outcomes: list[str],
    fixed_effects: str = FIXED_EFFECTS,
    weighted: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Four-week-bin PPML coefficients and a joint pre-trend test per outcome."""
    rows, pretrends = [], []
    for outcome in outcomes:
        model = fit(
            f"{outcome} ~ i(event_bin, treated, ref={REFERENCE_BIN}) | {fixed_effects}",
            panel,
            poisson=True,
            weighted=weighted,
        )
        if isinstance(model, str):
            pretrends.append({"outcome": outcome, "error": model})
            continue
        pre = []
        for i, name in enumerate(model._coefnames):
            event_bin = int(str(name).split("::")[1].split(":")[0])
            rows.append({"outcome": outcome, "event_bin": event_bin,
                         "week_start": event_bin * BIN_WEEKS,
                         **coefficient(model, name)})
            if event_bin < REFERENCE_BIN:
                pre.append(i)
        rows.append({"outcome": outcome, "event_bin": REFERENCE_BIN,
                     "week_start": REFERENCE_BIN * BIN_WEEKS, "estimate": 0.0,
                     "se": 0.0, "ci_low": 0.0, "ci_high": 0.0, "pct": 0.0})
        b = np.asarray(model.coef())[pre]
        v = model._vcov[np.ix_(pre, pre)]
        wald = float(b @ np.linalg.pinv(v) @ b)
        pretrends.append({"outcome": outcome, "pre_bins": len(pre), "wald": wald,
                          "p": float(stats.chi2.sf(wald, len(pre)))})
    return pd.DataFrame(rows), pd.DataFrame(pretrends)


def policy_types(
    panel: pd.DataFrame,
    outcomes: list[str],
    fixed_effects: str = FIXED_EFFECTS,
    weighted: bool = False,
) -> pd.DataFrame:
    """RQ3: effect per policy type, pairwise differences, and strictness trend."""
    rows = []
    terms = " + ".join(f"post_{kind}" for kind in POLICY_TYPES)
    pairs = [("banned", "permissive"), ("banned", "restrictive"),
             ("restrictive", "permissive")]
    for outcome in outcomes:
        model = fit(f"{outcome} ~ {terms} | {fixed_effects}", panel, poisson=True,
                    weighted=weighted)
        if isinstance(model, str):
            rows.append({"outcome": outcome, "error": model})
            continue
        for kind in POLICY_TYPES:
            rows.append({"outcome": outcome, "term": kind,
                         **coefficient(model, f"post_{kind}")})
        contrasts = [
            {"outcome": outcome, "term": f"{a} - {b}",
             **linear_combination(model, {f"post_{a}": 1.0, f"post_{b}": -1.0})}
            for a, b in pairs
        ]
        for row, p in zip(contrasts, holm([c["p"] for c in contrasts])):
            row["p_holm"] = p
        rows.extend(contrasts)
        trend = fit(
            f"{outcome} ~ treated_post + treated_post_strictness | {fixed_effects}",
            panel,
            poisson=True,
            weighted=weighted,
        )
        if not isinstance(trend, str):
            rows.append({"outcome": outcome, "term": "strictness trend",
                         **coefficient(trend, "treated_post_strictness")})
    return pd.DataFrame(rows)


def stack_panel(
    panel: pd.DataFrame, pre_weeks: int = PRE_WEEKS, post_weeks: int = POST_WEEKS
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Policy repositories stacked by adoption month with not-yet-treated controls.

    A cohort's treated rows are its weeks -pre_weeks..post_weeks-1. Its controls
    are policy repositories adopting after the cohort's last post week, observed
    over the cohort's calendar span and before their own adoption. A repository
    can be a control in early stacks and treated in its own. Cohorts with fewer
    than MIN_CONTROLS controls are left out. Returns the stacked rows and one row
    per cohort.
    """
    policy = panel[panel["treated"].eq(1)].copy()
    policy["week_date"] = pd.to_datetime(policy["week_start"])
    policy["cohort"] = policy["anchor"].dt.to_period("M")
    stacks, cohorts = [], []
    for cohort in sorted(policy["cohort"].unique()):
        first = cohort.start_time - pd.Timedelta(weeks=pre_weeks)
        last = cohort.end_time.normalize() + pd.Timedelta(weeks=post_weeks)
        treated = policy[
            policy["cohort"].eq(cohort)
            & policy["event_week"].between(-pre_weeks, post_weeks - 1)
        ]
        controls = policy[
            (policy["anchor"] > last)
            & policy["week_date"].between(first, last)
            & (policy["event_week"] < 0)
        ].copy()
        row = {
            "cohort": str(cohort),
            "treated": treated["repository"].nunique(),
            "controls": controls["repository"].nunique(),
        }
        row["used"] = bool(row["treated"]) and row["controls"] >= MIN_CONTROLS
        cohorts.append(row)
        if not row["used"]:
            continue
        controls["treated"] = 0
        for column in ["treated_post", "treated_post_strictness",
                       *(f"post_{kind}" for kind in POLICY_TYPES)]:
            controls[column] = 0
        stacks.append(pd.concat([treated, controls]).assign(stack=str(cohort)))
    if not stacks:
        return pd.DataFrame(columns=panel.columns), pd.DataFrame(cohorts)
    stacked = pd.concat(stacks, ignore_index=True)
    stacked["repo_stack"] = stacked["repository"] + "|" + stacked["stack"]
    stacked["week_stack"] = stacked["calendar_week"] + "|" + stacked["stack"]
    return stacked.drop(columns=["week_date", "cohort"]), pd.DataFrame(cohorts)


def repository_traits(panel: pd.DataFrame) -> pd.DataFrame:
    """Per repository, the pre-period traits the comparison weights balance."""
    pre = panel[panel["post"].eq(0)]
    by = pre.groupby("repository")
    traits = pd.DataFrame({
        "treated": by["treated"].first(),
        "log_prs": np.log1p(by["prs_opened"].mean()),
    })
    if "pr_authors" in pre:
        traits["log_pr_authors"] = np.log1p(by["pr_authors"].mean())
    late = pre[pre["event_week"] >= -26].groupby("repository")["prs_opened"].mean()
    early = pre[pre["event_week"] < -26].groupby("repository")["prs_opened"].mean()
    traits["prs_growth"] = (np.log1p(late) - np.log1p(early)).reindex(traits.index)
    first = panel.groupby("repository").first()
    if "stars_at_policy" in first:
        traits["log_stars"] = np.log1p(first["stars_at_policy"])
    if "age_at_policy_years" in first:
        traits["age_years"] = first["age_at_policy_years"]
    if "owner_type" in first:
        traits["organization"] = first["owner_type"].eq("Organization").astype(float)
    # A trait missing for a repository takes its group's mean, so it neither
    # drops the repository nor pulls the weights.
    for column in traits.columns.drop("treated"):
        traits[column] = traits[column].astype(float).fillna(
            traits.groupby("treated")[column].transform("mean")
        )
    return traits.dropna(axis=1, how="all")


def entropy_weights(controls: np.ndarray, target: np.ndarray) -> np.ndarray:
    """Entropy-balancing weights (mean 1) whose weighted means match `target`."""
    scale = controls.std(axis=0)
    scale[scale == 0] = 1.0
    centered = (controls - target) / scale

    def dual(lam):
        z = centered @ lam
        top = z.max()
        weights = np.exp(z - top)
        total = weights.sum()
        return top + np.log(total), centered.T @ weights / total

    lam = optimize.minimize(dual, np.zeros(centered.shape[1]), jac=True,
                            method="BFGS").x
    z = centered @ lam
    weights = np.exp(z - z.max())
    return weights / weights.mean()


def balance_weights(panel: pd.DataFrame) -> tuple[pd.Series, pd.DataFrame]:
    """Comparison weights by repository (policy repositories weigh 1) and a
    balance table: trait means for policy, comparison, and weighted comparison."""
    traits = repository_traits(panel)
    columns = [c for c in BALANCE if c in traits]
    policy = traits[traits["treated"].eq(1)]
    comparison = traits[traits["treated"].eq(0)]
    weights = pd.Series(1.0, index=traits.index)
    weights[comparison.index] = entropy_weights(
        comparison[columns].to_numpy(), policy[columns].mean().to_numpy()
    )
    w = weights[comparison.index]
    pooled = np.sqrt((policy[columns].var() + comparison[columns].var()) / 2)
    weighted_mean = comparison[columns].mul(w, axis=0).sum() / w.sum()
    balance = pd.DataFrame({
        "trait": columns,
        "label": [BALANCE[c] for c in columns],
        "policy": policy[columns].mean().to_numpy(),
        "comparison": comparison[columns].mean().to_numpy(),
        "comparison_weighted": weighted_mean.to_numpy(),
        "std_diff_before": ((comparison[columns].mean() - policy[columns].mean())
                            / pooled).to_numpy(),
        "std_diff_after": ((weighted_mean - policy[columns].mean()) / pooled).to_numpy(),
    })
    return weights, balance


def effective_size(weights: pd.Series) -> float:
    """Kish's effective number of repositories for a set of weights."""
    return float(weights.sum() ** 2 / (weights**2).sum())


def describe(panel: pd.DataFrame) -> pd.DataFrame:
    """Sample by group: repositories, weeks observed, and pre-period activity."""
    sample = panel["policy_type"].where(panel["policy_type"].isin(POLICY_TYPES), "policy")
    panel = panel.assign(sample=sample.where(panel["treated"].eq(1), "comparison"))
    rows = []
    for sample in ["comparison", "policy", *POLICY_TYPES]:
        part = panel[panel["sample"] == sample]
        if part.empty:
            continue
        pre = part[part["post"] == 0].groupby("repository")
        post_weeks = part[part["post"] == 1].groupby("repository").size()
        rows.append({
            "sample": sample,
            "repositories": part["repository"].nunique(),
            "median_post_weeks": float(post_weeks.median()) if len(post_weeks) else 0,
            "median_weekly_prs_pre": float(pre["prs_opened"].mean().median()),
            "median_weekly_contributors_pre": float(
                pre["active_contributors"].mean().median()
            ),
        })
    return pd.DataFrame(rows)


def nice_ticks(low: float, high: float, count: int = 4) -> list[float]:
    """Round tick values spanning [low, high], about `count` of them."""
    if high <= low:
        high = low + 1.0
    raw = (high - low) / count
    power = 10 ** math.floor(math.log10(raw))
    step = next(m * power for m in (1, 2, 2.5, 5, 10) if m * power >= raw)
    first = math.floor(low / step) * step
    ticks, value = [], first
    while value <= high + step * 1e-9:
        ticks.append(round(value, 10))
        value += step
    if ticks[-1] < high:
        ticks.append(round(value, 10))
    return ticks


def tick_label(value: float) -> str:
    text = f"{value:g}"
    return f"${text}$" if value < 0 else text


def tikz_colors() -> list[str]:
    """xcolor definitions for the figure colors, local to the picture."""
    named = {"effect": EFFECT_COLOR, "neutral": NEUTRAL, "grid": GRID,
             **{f"type{kind}": color for kind, color in COLORS.items()}}
    return [rf"\definecolor{{{name}}}{{HTML}}{{{color.lstrip('#').upper()}}}"
            for name, color in named.items()]


def tikz_event_studies(events: pd.DataFrame) -> str:
    """The primary outcomes' event studies as a 2 x 2 TikZ figure (plain TikZ,
    no pgfplots): 95% band, estimates, zero line, and adoption marker."""
    width, height, gap_x, gap_y = 7.4, 2.5, 1.0, 1.25
    outcomes = [o for o in PRIMARY if o in set(events["outcome"])]
    x_ticks = nice_ticks(events["week_start"].min(), events["week_start"].max(), 5)
    x_low, x_high = x_ticks[0], x_ticks[-1]
    lines = [r"\begin{tikzpicture}[font=\scriptsize]", *tikz_colors()]
    for index, outcome in enumerate(outcomes):
        part = events[events["outcome"] == outcome].sort_values("week_start")
        column, row = index % 2, index // 2
        left, bottom = column * (width + gap_x), -row * (height + gap_y)
        y_ticks = nice_ticks(min(part["ci_low"].min(), 0), max(part["ci_high"].max(), 0))
        y_low, y_high = y_ticks[0], y_ticks[-1]

        def x(v):
            return left + (v - x_low) / (x_high - x_low) * width

        def y(v):
            return bottom + (v - y_low) / (y_high - y_low) * height

        for tick in y_ticks:
            lines.append(rf"\draw[grid, line width=0.3pt] ({x(x_low):.3f},{y(tick):.3f})"
                         rf" -- ({x(x_high):.3f},{y(tick):.3f});")
            lines.append(rf"\node[anchor=east, text=neutral] at ({x(x_low) - 0.05:.3f},"
                         rf"{y(tick):.3f}) {{{tick_label(tick)}}};")
        band = [(r.week_start, r.ci_high) for r in part.itertuples()]
        band += [(r.week_start, r.ci_low) for r in part[::-1].itertuples()]
        lines.append(r"\fill[effect, opacity=0.18] "
                     + " -- ".join(f"({x(a):.3f},{y(b):.3f})" for a, b in band)
                     + " -- cycle;")
        lines.append(rf"\draw[neutral, line width=0.5pt] ({x(x_low):.3f},{y(0):.3f})"
                     rf" -- ({x(x_high):.3f},{y(0):.3f});")
        lines.append(rf"\draw[neutral, line width=0.5pt, dashed] ({x(0):.3f},{y(y_low):.3f})"
                     rf" -- ({x(0):.3f},{y(y_high):.3f});")
        lines.append(r"\draw[effect, line width=1pt] "
                     + " -- ".join(f"({x(r.week_start):.3f},{y(r.estimate):.3f})"
                                   for r in part.itertuples()) + ";")
        for r in part.itertuples():
            lines.append(rf"\fill[effect] ({x(r.week_start):.3f},{y(r.estimate):.3f})"
                         r" circle (1.1pt);")
        lines.append(rf"\draw[neutral, line width=0.4pt] ({x(x_low):.3f},{y(y_low):.3f})"
                     rf" -- ({x(x_high):.3f},{y(y_low):.3f});")
        for tick in x_ticks:
            lines.append(rf"\draw[neutral, line width=0.4pt] ({x(tick):.3f},{y(y_low):.3f})"
                         rf" -- ({x(tick):.3f},{y(y_low) - 0.06:.3f});")
            lines.append(rf"\node[anchor=north, text=neutral] at ({x(tick):.3f},"
                         rf"{y(y_low) - 0.08:.3f}) {{{tick_label(tick)}}};")
        lines.append(rf"\node[anchor=base, font=\footnotesize] at "
                     rf"({left + width / 2:.3f},{bottom + height + 0.15:.3f}) "
                     rf"{{{OUTCOMES[outcome][1]}}};")
        if column == 0:
            lines.append(rf"\node[rotate=90, anchor=south] at ({left - 0.75:.3f},"
                         rf"{bottom + height / 2:.3f}) {{Effect (log points)}};")
        if row == (len(outcomes) - 1) // 2:
            lines.append(rf"\node[anchor=north] at ({left + width / 2:.3f},"
                         rf"{bottom - 0.45:.3f}) {{Weeks relative to policy adoption}};")
    lines.append(r"\end{tikzpicture}")
    return "\n".join(lines) + "\n"


def tikz_policy_types(types: pd.DataFrame) -> str:
    """Per-type effects for the primary outcomes as a row of TikZ panels: one
    95% interval and estimate per policy type, strictest at the bottom."""
    width, height, gap = 3.3, 1.5, 1.0
    effects = types[types["term"].isin(POLICY_TYPES)]
    outcomes = [o for o in PRIMARY if o in set(effects["outcome"])]
    rows = {kind: len(POLICY_TYPES) - 1 - i for i, kind in enumerate(POLICY_TYPES)}
    lines = [r"\begin{tikzpicture}[font=\scriptsize]", *tikz_colors()]
    for index, outcome in enumerate(outcomes):
        part = effects[effects["outcome"] == outcome].set_index("term")
        left = 1.6 + index * (width + gap)
        x_ticks = nice_ticks(min(part["ci_low"].min(), 0), max(part["ci_high"].max(), 0), 3)
        x_low, x_high = x_ticks[0], x_ticks[-1]

        def x(v):
            return left + (v - x_low) / (x_high - x_low) * width

        def y(row):
            return (row + 0.5) / len(POLICY_TYPES) * height

        for tick in x_ticks:
            lines.append(rf"\draw[grid, line width=0.3pt] ({x(tick):.3f},0) -- "
                         rf"({x(tick):.3f},{height:.3f});")
            lines.append(rf"\node[anchor=north, text=neutral] at ({x(tick):.3f},-0.04) "
                         rf"{{{tick_label(tick)}}};")
        lines.append(rf"\draw[neutral, line width=0.5pt] ({x(0):.3f},0) -- "
                     rf"({x(0):.3f},{height:.3f});")
        for kind in POLICY_TYPES:
            if kind not in part.index:
                continue
            r = part.loc[kind]
            lines.append(rf"\draw[type{kind}, line width=1pt] ({x(r['ci_low']):.3f},"
                         rf"{y(rows[kind]):.3f}) -- ({x(r['ci_high']):.3f},{y(rows[kind]):.3f});")
            lines.append(rf"\fill[type{kind}] ({x(r['estimate']):.3f},{y(rows[kind]):.3f})"
                         r" circle (1.6pt);")
            if index == 0:
                lines.append(rf"\node[anchor=east] at ({left - 0.1:.3f},{y(rows[kind]):.3f})"
                             rf" {{{kind.capitalize()}}};")
        lines.append(rf"\node[anchor=base, font=\footnotesize] at "
                     rf"({left + width / 2:.3f},{height + 0.15:.3f}) "
                     rf"{{{OUTCOMES[outcome][1]}}};")
        lines.append(rf"\node[anchor=north] at ({left + width / 2:.3f},-0.4) "
                     r"{Effect (log points)};")
    lines.append(r"\end{tikzpicture}")
    return "\n".join(lines) + "\n"


def stars(p: float) -> str:
    return "***" if p < 0.001 else "**" if p < 0.01 else "*" if p < 0.05 else ""


def signed(value: float, digits: int = 1) -> str:
    """A signed number in math mode, so negatives get a true minus sign."""
    return f"${value:+.{digits}f}$"


def latex_effects(effects: pd.DataFrame) -> str:
    """Main PPML effects, grouped as primary, secondary workflow (RQ1) and
    secondary engagement (RQ2); Holm p in parentheses for primary outcomes."""
    lines = [
        r"\begin{tabular}{@{}lrcl@{}}", r"\toprule",
        r"Outcome & Change (\%) & 95\% CI (\%) & $p$ \\",
    ]
    main = effects[effects["model"].eq("ppml") & effects["estimate"].notna()]
    groups = [
        ("Primary ($p_{\\text{Holm}}$ in parentheses)", main["primary"]),
        ("Secondary: workflow (RQ1)", ~main["primary"] & main["rq"].eq("RQ1")),
        ("Secondary: engagement (RQ2)", ~main["primary"] & main["rq"].eq("RQ2")),
    ]
    for title, mask in groups:
        part = main[mask]
        if part.empty:
            continue
        lines += [r"\midrule", rf"\multicolumn{{4}}{{@{{}}l}}{{\textit{{{title}}}}} \\"]
        for row in part.itertuples():
            low, high = math.expm1(row.ci_low) * 100, math.expm1(row.ci_high) * 100
            p_text = f"{row.p:.3f}"
            if row.primary and pd.notna(row.p_holm):
                p_text += f" ({row.p_holm:.3f})"
            lines.append(
                rf"\quad {row.label} & {signed(row.pct * 100)} & "
                rf"[{signed(low, 0)}, {signed(high, 0)}] & {p_text} \\"
            )
    lines += [r"\bottomrule", r"\end{tabular}"]
    return "\n".join(lines) + "\n"


def latex_policy_types(types: pd.DataFrame) -> str:
    lines = [
        r"\begin{tabular}{lrrrr}", r"\toprule",
        r"Outcome & Permissive & Restrictive & Banned & Trend \\", r"\midrule",
    ]
    for outcome in [o for o in PRIMARY if o in set(types["outcome"])]:
        part = types[types["outcome"] == outcome].set_index("term")
        cells = []
        for term in [*POLICY_TYPES, "strictness trend"]:
            if term in part.index and pd.notna(part.loc[term, "estimate"]):
                row = part.loc[term]
                cells.append(f"{row['pct']:+.1%}{stars(row['p'])}".replace("%", r"\%"))
            else:
                cells.append("--")
        lines.append(f"{OUTCOMES[outcome][1]} & " + " & ".join(cells) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}"]
    return "\n".join(lines) + "\n"


def latex_sample(sample: pd.DataFrame) -> str:
    lines = [
        r"\begin{tabular}{lrrrr}", r"\toprule",
        r"Group & Repos & Post weeks & PRs/week & Contributors/week \\", r"\midrule",
    ]
    for row in sample.itertuples():
        lines.append(
            f"{row.sample.capitalize()} & {row.repositories} & "
            f"{row.median_post_weeks:.0f} & {row.median_weekly_prs_pre:.1f} & "
            f"{row.median_weekly_contributors_pre:.1f} \\\\"
        )
    lines += [r"\bottomrule", r"\end{tabular}"]
    return "\n".join(lines) + "\n"


def latex_designs(
    effects: pd.DataFrame, pretrends: pd.DataFrame, main: str | None = None
) -> str:
    """Primary PPML effects under each design, with the pre-trend test p-value;
    the main design comes first and is labelled as such."""
    names = {"stacked": "Later adopters", "comparison_weighted": "Weighted comparison",
             "comparison": "Unweighted comparison"}
    if main in names:
        names[main] += " (main)"
    designs = [d for d in DESIGNS if d in set(effects["design"])]
    if main in designs:
        designs = [main] + [d for d in designs if d != main]
    lines = [
        r"\begin{tabular}{l" + "rr" * len(designs) + "}", r"\toprule",
        " & " + " & ".join(rf"\multicolumn{{2}}{{c}}{{{names[d]}}}" for d in designs)
        + r" \\",
        "Outcome & " + " & ".join([r"Effect & $p_{\text{pre}}$"] * len(designs))
        + r" \\",
        r"\midrule",
    ]
    main = effects[effects["model"].eq("ppml") & effects["estimate"].notna()]
    for outcome in PRIMARY:
        cells = []
        for design in designs:
            row = main[(main["design"] == design) & (main["outcome"] == outcome)]
            pre = pretrends[(pretrends["design"] == design)
                            & (pretrends["outcome"] == outcome)]
            effect = (f"{row['pct'].item():+.1%}{stars(row['p'].item())}"
                      if len(row) else "--")
            pre_p = (f"{pre['p'].item():.3f}"
                     if len(pre) and "p" in pre and pd.notna(pre["p"].item()) else "--")
            cells += [effect.replace("%", r"\%"), pre_p]
        lines.append(f"{OUTCOMES[outcome][1]} & " + " & ".join(cells) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}"]
    return "\n".join(lines) + "\n"


# Short row labels for the balance table; BALANCE holds the full definitions.
BALANCE_SHORT = {
    "log_prs": "PRs/week (log)",
    "log_pr_authors": "PR authors/week (log)",
    "prs_growth": "PR growth (log)",
    "log_stars": "Stars (log)",
    "age_years": "Age (years)",
    "organization": "Org.-owned (share)",
}


def latex_balance(balance: pd.DataFrame) -> str:
    """Trait means and standardized differences before and after weighting."""
    lines = [
        r"\begin{tabular}{@{}lrrrrr@{}}", r"\toprule",
        r" & & \multicolumn{2}{c}{Comparison} & \multicolumn{2}{c}{Std.\ diff.} \\",
        r"\cmidrule(lr){3-4}\cmidrule(l){5-6}",
        r"Trait & Policy & Raw & Weighted & Before & After \\", r"\midrule",
    ]
    for row in balance.itertuples():
        lines.append(
            f"{BALANCE_SHORT.get(row.trait, row.label)} & {row.policy:.2f} & "
            f"{row.comparison:.2f} & {row.comparison_weighted:.2f} & "
            f"{signed(row.std_diff_before + 0.0, 2)} & "
            f"{signed(round(row.std_diff_after, 2) + 0.0, 2)} \\\\"
        )
    lines += [r"\bottomrule", r"\end{tabular}"]
    return "\n".join(lines) + "\n"


def analyze(
    panel_dir: Path,
    output_root: Path,
    *,
    paper_dir: Path | None = None,
    bootstrap_reps: int = BOOTSTRAP_REPS,
    main_design: str | None = None,
    progress=print,
) -> tuple[Path, dict]:
    """Run every design, write CSVs and a summary, and the paper's tables/figures.

    The paper's effects, event-study and policy-type outputs come from the main
    design: `main_design` if given, else stacked when any cohort has enough
    not-yet-treated controls, else the weighted comparison.
    """
    if main_design is not None and main_design not in DESIGNS:
        raise ValueError(f"Unknown design {main_design!r}; one of {DESIGNS}")
    # Separation and singleton notices are expected (repositories with all-zero
    # outcomes); the observation counts in the outputs show what was used.
    warnings.filterwarnings("ignore", category=UserWarning, module="pyfixest")
    panel = load_panel(panel_dir)
    has_types = panel.loc[panel["treated"].eq(1), "policy_type"].isin(POLICY_TYPES).all()
    output = output_root / new_run_id()
    output.mkdir(parents=True)
    progress(f"Panel: {panel['repository'].nunique()} repositories, {len(panel)} weeks")

    sample = describe(panel)
    stacked, cohorts = stack_panel(panel)
    weights, balance = balance_weights(panel)
    weighted = panel.assign(weight=panel["repository"].map(weights))
    designs = {
        "stacked": (stacked, STACKED_EFFECTS, False),
        "comparison_weighted": (weighted, FIXED_EFFECTS, True),
        "comparison": (panel, FIXED_EFFECTS, False),
    }
    if stacked.empty:
        progress("Stacked design: no cohort has enough not-yet-treated controls")
        del designs["stacked"]
    main = main_design if main_design in designs else next(iter(designs))
    tables = {name: [] for name in ("effects", "event_study", "pretrends",
                                    "policy_types")}
    for design, (data, fixed_effects, use_weights) in designs.items():
        progress(f"Design: {design}")
        # The bootstrap supports neither weights nor the stacked design's
        # stack-specific fixed effects (it returns NaN t or p = 0 there).
        reps = 0 if use_weights or design == "stacked" else bootstrap_reps
        found = {
            "effects": average_effects(data, reps, fixed_effects, use_weights),
            **dict(zip(("event_study", "pretrends"),
                       event_study(data, PRIMARY, fixed_effects, use_weights))),
            "policy_types": (policy_types(data, PRIMARY, fixed_effects, use_weights)
                             if has_types else pd.DataFrame()),
        }
        for name, table in found.items():
            tables[name].append(table.assign(design=design))
    results = {
        name: pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        for name, parts in tables.items()
    }
    for name, table in (("sample", sample), ("cohorts", cohorts), ("balance", balance),
                        *results.items()):
        table.to_csv(output / f"{name}.csv", index=False)

    def of(name: str) -> pd.DataFrame:
        table = results[name]
        return table[table["design"] == main] if not table.empty else table

    effects, events, types = of("effects"), of("event_study"), of("policy_types")
    figures = {"event_study.tex": tikz_event_studies(events)}
    if not types.empty:
        figures["policy_types.tex"] = tikz_policy_types(types)
    (output / "figures").mkdir()
    for name, text in figures.items():
        (output / "figures" / name).write_text(text, encoding="utf-8")
    written = {
        "sample.tex": latex_sample(sample),
        "effects.tex": latex_effects(effects),
        "designs.tex": latex_designs(results["effects"], results["pretrends"], main),
        "balance.tex": latex_balance(balance),
    }
    if not types.empty:
        written["policy_types.tex"] = latex_policy_types(types)
    (output / "tables").mkdir()
    for name, text in written.items():
        (output / "tables" / name).write_text(text, encoding="utf-8")
    if paper_dir is not None:
        (paper_dir / "figures").mkdir(parents=True, exist_ok=True)
        (paper_dir / "tables").mkdir(parents=True, exist_ok=True)
        for name, text in figures.items():
            (paper_dir / "figures" / name).write_text(text, encoding="utf-8")
        for name, text in written.items():
            (paper_dir / "tables" / name).write_text(text, encoding="utf-8")

    used = cohorts[cohorts["used"]] if not cohorts.empty else cohorts
    comparison = weights[weights.index.isin(
        panel.loc[panel["treated"].eq(0), "repository"])]
    summary = {
        "schema_version": 2,
        "run_id": output.name,
        "analyzed_at": utc_now(),
        "panel": str(panel_dir),
        "repositories": int(panel["repository"].nunique()),
        "policy_repositories": int(panel.loc[panel["treated"].eq(1), "repository"].nunique()),
        "weeks": int(len(panel)),
        "primary_outcomes": PRIMARY,
        "main_design": main,
        "designs": list(designs),
        "stacked_window_weeks": [-PRE_WEEKS, POST_WEEKS - 1],
        "stacked_cohorts": int(len(used)),
        "stacked_treated_repositories": int(used["treated"].sum()) if len(used) else 0,
        "comparison_effective_size": effective_size(comparison),
        "comparison_max_std_diff_after": float(balance["std_diff_after"].abs().max()),
        "bootstrap_reps": bootstrap_reps,
        "policy_types_estimated": not types.empty,
        "model_errors": int(sum(
            table.get("error", pd.Series(dtype=str)).notna().sum()
            for table in results.values() if not table.empty
        )),
        "output_directory": str(output),
    }
    write_json(output / "summary.json", summary)
    return output, summary
