# Running data collection

How to collect every dataset for the study, for the pilot and the full sample,
for repositories with an AI policy ("policy repos") and the matched repositories
without one ("comparison repos"). What each output contains is in the
[data dictionary](data-dictionary.md); this page is the order and the commands.

Commands are for bash, run from the repository root.

## 1. Before you start

```bash
uv sync
uv run aip doctor
uv run aip config validate
uv run python -m unittest discover -s tests
```

A GitHub token is required (GraphQL collectors fail without one). Use an existing
`gh auth login` session or set `GH_TOKEN` / `GITHUB_TOKEN`. The token is read in
memory and never written to any output.

Set the variables used below once per shell:

```bash
# The policy-history run whose detected dates anchor every window
P=data/interim/policy_history/<run_id>
# Comparison repos (no AI policy), proposed by `aip select comparison`
C=data/repositories/sampled_comparison_repos.csv
# Pilot only; leave empty for the full study (all 120 registry repos)
SEL="--priority pilot"     # full study: SEL=""
```

## 2. The steps in order

| Step | What | Command(s) | Section |
| --- | --- | --- | --- |
| 1 | Policy repos listed in `data/repositories/sampled_policy_repos.csv` | manual; `scripts/find_ai_policy_repos.py` helps find more | [8](#8-finding-more-candidates) |
| 2 | Propose policy dates | `aip collect policy-history` | [3](#3-policy-dates) |
| 3 | Verify policy dates by hand | manual (all are `needs_review`) | [3](#3-policy-dates) |
| 4 | Code policy types | manual, `policy_type` column of `data/repositories/sampled_policy_repos.csv` | [3](#3-policy-dates) |
| 5 | Propose and review comparison repos | `aip select comparison`, then review | [4](#4-comparison-repos) |
| 6 | Collect both groups | `aip collect ...` in two terminals | [5](#5-collect) |
| 7 | Build the weekly table | `aip build weekly ... --comparison` | [6](#6-build-the-weekly-table) |

Steps 3 and 4 come before collection for a reason: every collector except stars,
forks, first-seen and profiles depends on the policy date, so correcting a date
later means collecting that repository (and its comparison repo) again.

## 3. Policy dates

```bash
uv run aip collect policy-history $SEL
```

This proposes a date per repository (the first commit adding AI wording to a
policy file) in `data/interim/policy_history/<run_id>/policy_events.csv`. Point
`P` at that run. Dates are unverified: check each against its commit and the
project's announcements before collecting outcomes. (Using hand-verified dates in
place of detected ones is not yet automated.)

Policy types are coded by hand in the `policy_type` column of
`data/repositories/sampled_policy_repos.csv`. They only feed
the weekly build, so they can be filled in at any time and the build re-run.

## 4. Comparison repos

```bash
uv run aip select comparison $SEL --policy-run $P --seed <number> \
    --skip owner/inactive-repo
```

- Proposes one comparison repo per policy repo (same language, similar stars and
  PR volume, no AI policy), with a stand-in date drawn from the policy dates'
  early / middle / late clusters. The seed makes the draw repeatable; record it.
- `--skip` leaves a policy repo unmatched (the pilot skipped
  `wamp-proto/wamp-ai`, which is inactive). Repeat it for several repos.
- Writes `data/repositories/comparison_repos.csv`; refuses to replace an existing file
  unless `--overwrite` is given. Takes about 1–2 minutes per policy repo (GitHub
  allows 30 searches a minute).
- **Review before collecting.** Check `match_quality = weak` rows and any match
  that is a different kind of project. To swap in one of the `alternates`, change
  the repository (and its stars/PR figures) but keep the row's `stand_in_date`,
  `date_cluster` and `matched_policy_repo`. Mark rows `review_status = accepted`.
- The tool proposes one comparison repo per policy repo. The full study's
  recommended three per policy repo is not yet supported.
- Redraw (new file) if verified dates move policy repos between clusters.

## 5. Collect

GraphQL collectors share one budget of 5,000 points an hour, so run them one
after another; the REST collectors use a separate budget and can run at the same
time in a second terminal. Both wait for rate-limit resets rather than fail.

Every collector writes a new run under `data/raw/<collector>/<run_id>/` and
`data/interim/<collector>/<run_id>/`; nothing is overwritten. Each prints a
status per repository and exits with code 2 if any repository is partial or
failed (see [section 7](#7-re-running-and-staleness)).

For the full evaluation, `scripts/run_eval.sh` runs both terminals' steps for the
102 policy and 102 comparison repos, slowest collectors first, logging to
`logs/eval/`: `bash scripts/run_eval.sh graphql` in one terminal and
`bash scripts/run_eval.sh rest` in the other. It uses the verified
`reported_policy_date`, so no `--policy-run`. The commands below are the pilot's.

### Terminal 1: GraphQL (run in this order)

```bash
# Policy repos
for c in pulls issues churn ci discussions; do
  uv run aip collect $c $SEL --policy-run $P
done
uv run aip collect first-seen $SEL

# Comparison repos (after the list is reviewed)
for c in pulls issues churn ci discussions; do
  uv run aip collect $c --candidates $C $SEL --policy-run $P
done
uv run aip collect first-seen --candidates $C $SEL
```

### Terminal 2: REST (alongside Terminal 1)

```bash
# Policy repos
for c in releases comments context; do
  uv run aip collect $c $SEL --policy-run $P
done
for c in stars forks profiles; do
  uv run aip collect $c $SEL
done

# Comparison repos (after the list is reviewed)
for c in releases comments context; do
  uv run aip collect $c --candidates $C $SEL --policy-run $P
done
for c in stars forks profiles; do
  uv run aip collect $c --candidates $C $SEL
done
```

Notes:

- Comparison repos take `--policy-run` too (the configuration requires it), but
  their own `stand_in_date` is used as the date.
- `issues` is a GraphQL collector; `releases`, `comments`, `context`, `stars`,
  `forks` and `profiles` are REST.
- `aip collect contributors` (bands of contributors) is not needed for the weekly
  table; churn and first-seen cover it.
- `aip collect pulls --no-reviews` is much cheaper but leaves the reviewer and
  review-round columns blank.
- A run started before a code change keeps the old behaviour; collect again after
  pulling changes that add fields.

### How long it takes

Time is dominated by the largest repositories (pytorch, llama.cpp, react-native,
opencv): their PR, CI and first-seen histories need thousands of GraphQL pages.
For the pilot (25 + 25 repos) expect Terminal 1 to take most of a day or longer;
Terminal 2 finishes sooner. The full study scales roughly with the number and
size of repositories; consider collecting the largest repositories separately
(section 7) so a failure there does not hold up the rest.

## 6. Build the weekly table

```bash
uv run aip build weekly $SEL --policy-run $P --comparison $C
```

Takes seconds and uses the newest usable run of each collector per repository.
Writes `data/panels/<run_id>/`: `panel.csv` (one row per repository and
week, with `group` = `policy` or `comparison`), `characteristics.csv` (one row per
repository) and `repositories.csv` (which run fed each source, and anything
missing). Exits with code 2 if any repository lacks a source; `repositories.csv`
names it. Reviewer counts need the local raw PR responses, so build on the
machine that collected the PRs.

For the full evaluation:

```bash
uv run aip build weekly --priority evaluation \
    --comparison data/repositories/sampled_comparison_repos.csv
```

### Analyze

```bash
uv run --group analysis aip analyze      # newest weekly panel; or --panel <dir>
```

Needs the `analysis` dependency group (pandas, pyfixest, matplotlib). Writes
`data/results/<run_id>/` (effects, event studies, pre-trend tests,
policy types, sample; CSV and LaTeX) with tables and figures in its `tables/` and `figures/` subfolders. Pass
`--no-paper`; without it the command also copies them into a `paper/` folder. The design is in
the docstring of `src/ai_policy_impact/analysis.py`. Takes about a minute;
`--bootstrap-reps 0` skips the wild bootstrap for a quicker look.

## 7. Re-running and staleness

**A repository came back partial or failed.** Re-run that collector for just
those repositories with a one-row candidates file (keep the header):

```bash
head -1 data/repositories/sampled_policy_repos.csv > one.csv
grep '^pytorch/pytorch,' data/repositories/sampled_policy_repos.csv >> one.csv
uv run aip collect pulls --candidates one.csv --policy-run $P
```

(Use `$C` instead of the registry for a comparison repo.) The weekly build picks
the newest complete run per repository, so the earlier run's other repositories
are still used.

**When collected data goes stale.**

| Change | Collect again |
| --- | --- |
| A policy date is corrected | That repo: pulls, issues, releases, churn, ci, comments, discussions, context. Its comparison repo too if the date changes its cluster (redraw) |
| A comparison repo is swapped | The new repo: everything in section 5 |
| Code adds new fields to a collector | That collector, for every repository |
| More time has passed | Nothing required; collecting again extends the observed weeks |
| Policy types coded | Nothing; re-run the build |

Stars, forks, first-seen and profiles do not depend on the date and never need
collecting again for a date change.

## 8. Finding more candidates

Two standalone scripts search GitHub (they need `requests`):

```bash
GITHUB_TOKEN=... uv run --with requests python scripts/find_ai_policy_repos.py
GITHUB_TOKEN=... uv run --with requests python scripts/find_no_ai_policy_repos.py
```

- `find_ai_policy_repos.py` finds repositories with an AI policy signal
  (`AI_POLICY.md`, or a `CONTRIBUTING.md` mentioning an AI term). New finds are
  added to `candidate_policy_repos.csv`, and to `sampled_policy_repos.csv`
  (with a `priority`) once chosen for collection.
- `find_no_ai_policy_repos.py` finds active repositories with none of those
  signals. It is for browsing; the comparison sample itself comes from
  `aip select comparison`, which applies the same rule.
- Both share the AI term list in `src/ai_policy_impact/ai_terms.py` (a test keeps
  the three copies identical). Settings are in each script's `CONFIG` block.
