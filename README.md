# Replication package: Ban, Restrict, or Permit? How AI Contribution Policies Shape Open-Source Workflows and Contributor Engagement

This package contains the repository lists, the data-collection and analysis code, the repository-week panels, and the estimates behind the paper. The panels hold weekly counts only, with no user names, comments, or code.

## Layout

```
data/
  repositories/                    repository lists
    candidate_policy_repos.csv       all repositories found with an AI-policy signal (695)
    candidate_comparison_repos.csv   all repositories found without one (209)
    sampled_policy_repos.csv         the policy sample: 102 evaluation + 26 pilot repositories
    sampled_comparison_repos.csv     the comparison sample: 102 matched evaluation + 25 pilot repositories
    random_comparison_repos.csv      102 randomly drawn, unmatched comparison repositories (robustness check v)
  panels/                          repository-week panels (input to the analysis)
    matched-comparison/              102 policy + 102 matched comparison repositories (main design)
    random-comparison/               102 policy + 102 random comparison repositories (robustness check v)
  results/                         estimates, tables, and figures
    matched-comparison/              all results reported in the paper except robustness check v
    random-comparison/               robustness check v
src/ai_policy_impact/              collection, panel, matching, and analysis code (the `aip` command)
scripts/
  find_ai_policy_repos.py          GitHub search that produced candidate_policy_repos.csv
  find_no_ai_policy_repos.py       GitHub search that produced candidate_comparison_repos.csv
  run_eval.sh                      runs every collector for the evaluation sample
  matching/                        scripts that turned the matcher's proposals into the final matched sample
  sample_table.py                  Table I
configs/study.yaml                 study settings (adoption date, data folders)
docs/                              data dictionary and collection guide
tests/                             unit tests
```

### Repository lists

`sampled_policy_repos.csv` and `sampled_comparison_repos.csv` share one format. `priority` is `evaluation` for the 102 + 102 repositories analyzed in the paper and `pilot` for the pilot used to develop and test the collection, which shares no repository with the evaluation sample.

| Column | Policy list | Comparison list |
|---|---|---|
| `repository`, `priority`, `group` | owner/name, `evaluation` or `pilot`, `policy` | same, `comparison` |
| `policy_paths` | files that hold the policy (`\|`-separated) | empty |
| reference date | `reported_policy_date`: hand-verified adoption date | `stand_in_date`: date drawn from a policy date in the partner's period |
| partner | `matched_comparison_repo` | `matched_policy_repo` |
| `language`, `owner_type` | primary language and owner type | same |
| policy-only | `policy_type` (banned, restrictive, permissive), `policy_rules` (coded rules), `source_url` (policy at the commit we read) | |
| comparison-only | | `match_quality`: `good` if within 2x in PR volume and 3x in stars when matched, else `weak` |

Most matched comparison repositories come from the matcher's GitHub search (`aip select comparison`, seed 20261023), not from `candidate_comparison_repos.csv`. The scripts in `scripts/matching/` record how we screened the matcher's proposals (removing AI-tool projects and automated feeds), reused 13 comparison repositories from the random sample, and re-matched the policy repositories whose first match was screened out. They read working files from the original run and are included as a record; they do not run on this package alone.

### Panels

Each panel folder holds:
- `panel.csv`: one row per repository and event week, from 52 weeks before to 51 weeks after the reference date.
- `characteristics.csv`: repository traits at the reference date.
- `repositories.csv`: collection coverage per repository.
- `summary.json`

`docs/data-dictionary.md` defines every column.

### Paper tables and figures

| Paper | File |
|---|---|
| Table I (sample at the reference date) | `scripts/sample_table.py data/panels/matched-comparison` |
| Table III (analyzed sample) | `data/results/matched-comparison/tables/sample.tex` |
| Table IV (effects) | `.../tables/effects.tex`, all outcomes in `effects.csv` |
| Figure 1 (event study) | `.../figures/event_study.tex`, values in `event_study.csv` |
| Table V and Figure 2 (policy types) | `.../tables/policy_types.tex`, `.../figures/policy_types.tex`, values in `policy_types.csv` |
| Table VI (three designs and pre-trend tests) | `.../tables/designs.tex`, values in `effects.csv` and `pretrends.csv` |
| Balance before and after reweighting (alternative design 1; not shown in the paper) | `.../tables/balance.tex`, `balance.csv` |
| Robustness checks i to iv | `effects.csv` (`model` = `log1p_ols` and `p_wild_bootstrap`; `design` = `comparison_weighted` and `stacked`) |
| Robustness check v | `data/results/random-comparison/effects.csv`, rows with `design` = `comparison` |

In the CSV files, `design` is `comparison` (main design), `comparison_weighted` (alternative design 1), or `stacked` (alternative design 2), and `pct` is the percentage change.

## Reproducing the results

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --group analysis
uv run --group analysis python -m unittest discover -s tests

# Tables III to VI and Figures 1 and 2 (the wild bootstrap takes a while; --bootstrap-reps 0 skips it)
uv run --group analysis aip analyze --panel data/panels/matched-comparison \
    --main-design comparison --no-paper

# Robustness check v
uv run --group analysis aip analyze --panel data/panels/random-comparison \
    --main-design comparison --no-paper

# Table I
uv run --group analysis python scripts/sample_table.py data/panels/matched-comparison
```

Each `aip analyze` run writes a new folder under `data/results/`.

## Recollecting the data

Recollection queries the GitHub API, needs a token in `GH_TOKEN`, and takes several days for the full sample because of rate limits. GitHub data change over time, so a new collection will differ slightly from ours.

```bash
bash scripts/run_eval.sh graphql   # terminal 1
bash scripts/run_eval.sh rest      # terminal 2, at the same time
uv run aip build weekly --priority evaluation \
    --candidates data/repositories/sampled_policy_repos.csv \
    --comparison data/repositories/sampled_comparison_repos.csv
```

`docs/running-collection.md` describes each collector. Collected data go to `data/raw` and `data/interim`, which this package does not include.

## License

Code: MIT (`LICENSE`). Data: CC BY 4.0 (`LICENSE-DATA`).
