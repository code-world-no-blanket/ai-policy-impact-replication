# Institutional profile data dictionary

Profiles are **current observations**, not historical baselines or policy effects.
Times are UTC. Each run spans multiple requests and is not an atomic snapshot.
Code, curated inputs, and interim outputs are tracked; raw responses remain local.

## Repository lists (`data/repositories/`)

| File | Contents |
| --- | --- |
| `candidate_policy_repos.csv` | Every policy-repository candidate found by `scripts/find_ai_policy_repos.py` (695). `policy_file` is the policy or CONTRIBUTING file (pipe-delimited if several) |
| `candidate_comparison_repos.csv` | Repositories with no AI-policy signal, from `scripts/find_no_ai_policy_repos.py` (209) |
| `sampled_policy_repos.csv` | The policy sample: 26 `pilot` and 102 `evaluation` repositories |
| `sampled_comparison_repos.csv` | The comparison sample: 25 `pilot` and 102 `evaluation` repositories, each matched to one policy repository |
| `random_comparison_repos.csv` | 102 randomly drawn, unmatched comparison repositories (robustness check) |

Both candidate lists share the same GitHub metadata, retrieved together:
`language`, `stars`, `owner_type`, `created_at`, `pushed_at`, `prs_past_year`
(PRs opened in the preceding 365 days) and `description`.

`sampled_policy_repos.csv` and `sampled_comparison_repos.csv` share one format
(see the README). The policy list is the collectors' default `--candidates`.
`policy_paths` is pipe-delimited; `source_url` is the policy file at a pinned
commit. `reported_policy_date` was checked by hand. `policy_type` holds the
analysis group (`banned`, `restrictive` or `permissive`; 34 each among the
evaluation rows; blank for pilot rows) and `policy_rules` the coded rules.
`language` and `owner_type` are reference-date values from the panel for the
evaluation rows and listing-time values for the pilot rows.

The comparison sample was proposed by `aip select comparison` (seed 20261023)
and screened by the scripts in `scripts/matching/`: AI-tool projects and
automated feeds were removed, 13 repositories from the random sample that met
the `good` rule were reused, and policy repositories whose first match was
screened out took the next candidate that passed. `match_quality` is the
matcher's label.

The random sample (`random_comparison_repos.csv`) was drawn with
`random.Random(20261019)` from the candidate comparison list, excluding pilot
comparisons: 96 drawn uniformly, 6 more from candidates with 50 to 1,000 PRs in
the past year, and replacements for repositories created less than a year
before the latest policy date.

`stand_in_date` gives each comparison one of the 102 evaluation policy dates, one
to one: taking repositories youngest first, each gets a random remaining date at
least 365 days after its creation (same seed).

## Repository profile outputs

Location: `data/interim/repository_profiles/<run_id>/profiles.csv`, with full nested
records in `repositories/*.json` and run totals in `summary.json`.

| Field | Definition / interpretation |
| --- | --- |
| `requested_repository` | Candidate name before a GitHub rename/redirect |
| `repository_id`, `repository` | Stable GitHub ID and returned canonical full name |
| `metadata_retrieved_at` | UTC time of the repository metadata request |
| `source_commit` | Default-branch head resolved during this run; README, policy and governance files use this immutable ref |
| `description` | GitHub description; evidence for manual goal coding, not the completed coding itself |
| `owner_type` | GitHub User/Organization account type, not governance or incorporation status |
| `stars` | REST `stargazers_count` |
| `watchers` | REST `subscribers_count`; `watchers_count` aliases stars and is deliberately unused |
| `forks` | REST `forks_count` at retrieval |
| `primary_language` | GitHub's primary language; nullable |
| `languages_bytes` | Full language-to-byte map from `/languages`; current, not pinned to the source commit |
| `license_spdx_id`, `license_name` | GitHub's detected license; not a complete audit of all licenses |
| `license_status` | `needs_review` when absent or `NOASSERTION`; neither proves no license |
| `fork`, `archived`, `disabled` | Current screening signals; no automatic exclusion |
| `maintainers_present`, `maintainer_count` | Null until reviewed with evidence; no count inferred from contributors or CODEOWNERS |
| `institutional_review_status` | `unreviewed`; manual reviews are recorded separately |
| `status` | `complete`, `partial`, `failed`, or `skipped_private`; component statuses must also be checked |

No blank numeric field is zero. The CSV is a compact index; consult JSON for
documents, language bytes, Projects counts, provenance, and errors.

### README, policy, and governance evidence

`documents` contains the source path, kind, commit and blob SHAs, immutable URL,
retrieval time, raw response path, local decoded-file path, content SHA-256, byte
count, and status. Files are stored as bytes even if not UTF-8. A content API result
without base64 content is explicitly unavailable, not silently saved as empty.

The collector scans the recursive tree for basenames such as MAINTAINERS,
GOVERNANCE, CODEOWNERS, OWNERS, COMMITTERS, TEAM, and CONTRIBUTING (including common
extensions). Test/fixture/vendor directories are skipped for governance discovery.
At most 20 governance candidates are fetched, shallowest first; the complete list
of matched paths and any cap are recorded. Explicit candidate policy paths are
also fetched. A truncated Git tree is marked `truncated`. A completed scan only
means this filename-based scan completed; it does not establish the absence of
external, organization-wide, or differently named governance documents.

`has_ai_tool_files` / `ai_tool_files` record instruction files for AI coding
tools found in the same tree: `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`,
`.cursorrules`, `.windsurfrules` (anywhere), `.github/copilot-instructions.md`,
`.github/instructions/*.md`, `.cursor/rules/*` and `.claude/*`, vendored paths
excluded. They show that a project uses such tools, not that it has a
contribution policy, and are kept for follow-up only; their content is not
fetched. `has_ai_tool_files` is blank when the tree was unavailable, or truncated
with none found.

`unavailable_or_missing` requires consulting the raw request/status and errors:
404 can mean missing or not accessible. Do not code "no maintainers" from missing
files. The collector does not execute README, policy, AGENTS, or governance content.

### GitHub Projects

`projects.status` distinguishes `complete`, `partial`, `unavailable`, and
`not_requested`. The collector paginates **repository-linked public Projects v2**,
their fields, and all returned items. Private projects are skipped. Unlinked
organization boards, classic Projects, historical states, and custom board-view
filters are outside this first collector's scope.

For each board, `columns` groups items by the single-select field named `Status`.
Every configured option is included even if empty; unassigned items receive
`(no Status)`. `active_items` excludes archived items; `archived_items` is separate.
`by_type` includes both active and archived items by API item type (including
Issue, PullRequest and DraftIssue equivalents). Counts cover the **whole board**,
which can contain multiple repositories, and are not repository issue counts.
The same board may be linked from multiple repositories; deduplicate by project
ID before aggregating across repositories.

The query explicitly requests `archivedStates: [ARCHIVED, NOT_ARCHIVED]`; the live
GraphQL schema checked on 2026-09-17 defaults to nonarchived items when this argument
is omitted. Both states are needed for the separate archived counts above.

A board without a `Status` field gets `status_field_not_found`, not invented columns.
Pagination failures withhold that board's counts. Changed totals, duplicate items,
or options changed during retrieval also withhold counts. Changes that preserve
totals cannot be detected, so treat counts as observations over the run interval.
Only a successful, fully paginated list returning no public boards supports an
empty visible-board list; permission errors never establish zero boards or items.

## Star history outputs

Command: `aip collect stars`. Location: `data/interim/star_history/<run_id>/`, with
full nested records in `repositories/*.json` and run totals in `summary.json`.
Source: `GET /repos/{owner}/{repo}/stargazers/history` (weekly, newest first,
30 weeks per page, at most 100 pages) and `GET /repos/{owner}/{repo}/stargazers/count`.

`summary.csv` has one row per repository:

| Field | Definition / interpretation |
| --- | --- |
| `stars_before`, `stars_before_retrieved_at` | REST `stargazers_count` from repository metadata, fetched before the history |
| `stars_after`, `stars_after_retrieved_at` | `/stargazers/count`, fetched after the last history page |
| `history_total` | Sum of all weekly totals |
| `reconciliation` | `matched` (before = after = history total); `mismatch` (counts stable but differ from the history); `changed_during_run`; `count_unavailable` |
| `weeks`, `first_week`, `last_week` | Number and range of consecutive weeks returned |
| `history_pages` | History pages fetched |
| `status` | `complete` only when the history is valid and `matched`; otherwise `partial`, `failed`, or `skipped_private` |

`weekly.csv` (`repository, week_start, new_stars, cumulative_stars`) has one row per
returned week. `week_start` is a Sunday; weeks are UTC. `daily.csv`
(`repository, date, new_stars, cumulative_stars`) comes from each week's per-day
counts and is limited to dates from repository creation through retrieval, so days
outside that range are not reported as observed zeros. The daily series lets
analysis windows (for example the configured 180/365-day horizons) start on any
day, not only on GitHub's Sunday week boundaries.

**The history counts surviving stars by the date they were given.** On the
2026-09-25 pilot run, the weekly totals equalled the current count for 23 of 26
repositories and no week or day was negative. So an unstar removes a star from
the week it was originally given; it is not recorded as a loss when it happens.
Unstarring after a policy therefore lowers *pre-policy* counts and can make
post-policy growth look larger. Detecting this requires comparing weekly counts
across dated runs. Three repositories created in 2012-2013 (RIOT-OS/RIOT,
openssl/openssl, samtools/samtools) had histories exactly one star below stable
current counts; they are left `partial`, not adjusted.

Validation rejects (`failed`) any week that is not a Sunday 00:00 UTC, whose daily
counts do not sum to its total, or that is not consecutive with its neighbours.
The endpoint returns empty weeks as zeros, so a gap is missing data and is never
filled. A history request error never yields zero stars.

## Policy history outputs

Command: `aip collect policy-history`. Location:
`data/interim/policy_history/<run_id>/`. For each path in a candidate's
`policy_paths`, the collector lists every default-branch commit that touched the
path (`GET /commits?path=`) and fetches each commit's diff (`GET /commits/{sha}`).
Added and removed lines are matched against a list of AI terms: `AI`, `LLM`,
`GenAI`, artificial intelligence, large language model, generative, ChatGPT,
Copilot, Claude, Gemini, OpenAI, Anthropic, machine-generated, and vibe coding. The
acronyms are case-sensitive.

Diffs are inspected only for commits on or after 2021-06-29 (the start of the
GitHub Copilot technical preview). The file as it stood at the last earlier commit
is checked for AI terms, and the flag `ai_text_before_inspection_window` is set when
it already had them.

`policy_events.csv` has one row per repository:

| Field | Definition / interpretation |
| --- | --- |
| `detected_policy_date` | UTC committer date of the earliest commit, across all candidate paths, that **added** a line with an AI term. Proposed, not verified |
| `detected_path`, `detected_sha`, `detected_file_status` | Where that happened. `added` means the file was created with AI text; that can be a move or copy the path history does not follow |
| `detected_first_line` | The first matching added line, for quick review |
| `detected_minus_reported_days` | Detected minus the registry's `reported_policy_date`; blank when either is missing |
| `ai_commits` | Commits that added AI-term lines to any candidate path |
| `flags` | `no_history_at_path`, `ai_text_before_inspection_window`, `history_starts_with_rename`, `patch_unavailable` (GitHub omits very large diffs) |
| `review_status` | Always `needs_review` |

`policy_commits.csv` has one row per inspected commit and path. It includes line
counts, AI-term line counts, the first AI line added, and a link to the commit.

**Detected dates measure the first AI wording, not necessarily a formal policy.**
In the 2026-09-25 pilot, 10 of 13 reported dates matched exactly. In the other
three (JabRef, ghostty, modelcontextprotocol), CONTRIBUTING.md carried AI guidance
5-13 months before the reported date. There, the reported date is when a
standalone policy file appeared. Which event counts as treatment is a
research-team decision. `policy_commits.csv` has every AI-related edit, so either
definition can be applied. A path's history does not follow renames, and the
candidate paths come from the registry, so wording elsewhere in the repository is
not searched.

## Contributor outputs

Command: `aip collect contributors [--policy-run <policy_history interim dir>]`.
Location: `data/interim/contributors/<run_id>/`.

**Anchor date.** `treatment.anchor_event` in `configs/study.yaml` chooses it.
The current, provisional setting is `first_ai_mention`: the policy-history
`detected_policy_date`, i.e. the first commit adding AI wording to a policy path.
`--policy-run` is then required. A repository without a detected date gets **no**
anchor; it does not fall back to the reported date, since that is a different
event. The alternative, `reported_policy_date`, uses the registry's transcribed
date. The manifest records `anchor_event`, and `anchor_source` records `detected`,
`reported`, or `none`. Neither date is verified. A detected date can be later than
the true first mention when a policy path's history starts with a rename (flag
`history_starts_with_rename`; llama.cpp and qdrant in the pilot) or when the
wording first appeared in a file not listed in `policy_paths`. With no anchor, only
the overall list is collected.

**Bands** come from `configs/study.yaml`, in days relative to the anchor, as
half-open intervals: `pre_long` [-365, -180), `pre_medium` [-180, 0),
`post_medium` [0, 180), and `post_long` [180, 365). The anchor day is post-policy.
The pre-period mirrors the configured long-run horizon.

**Source.** The collector reads default-branch commits dated from the start of
`pre_long` to the earlier of the end of `post_long` and the collection date. Dates
are UTC committer dates, which is when a commit landed on the branch. Author dates
are kept in `commits.csv`.

- **Identity.** A contributor is the linked GitHub login. An unlinked author is
  keyed as `email:` plus 16 hex characters of the SHA-256 of the lowercased
  address; addresses are not written to outputs. One person with several
  unlinked addresses counts more than once.
- **Bots.** Accounts of type `Bot`, or logins or emails containing `[bot]`, are
  flagged. They are excluded from contributor counts, and `bot_accounts` counts
  them.
- **Merge commits.** Commits with more than one parent are excluded from
  contributor counts, since they credit whoever merged, not the author.
  `merge_commits` counts them.
- **What commit authorship misses.** It covers work that landed on the default
  branch. It misses reviewers, issue authors, unmerged PRs, and co-authors in
  `Co-authored-by` trailers. A squash merge credits the PR author.

`summary.csv` has one row per repository:

| Field | Definition / interpretation |
| --- | --- |
| `overall_status` | All-time `/contributors?anon=true` (default-branch commits). `too_large_for_api` when GitHub refuses the list with 403; that is missing, not zero. `truncated` after 100 pages |
| `overall_linked_accounts`, `overall_unlinked_emails` | **Not headcounts.** GitHub links only the first 500 author email addresses to accounts; every other address is a separate anonymous entry. Linked accounts therefore stop growing near 500 emails (pytorch: 327 accounts, 6,737 unlinked emails), and one person can appear under several emails. For people over time, use the band counts, which come from commit authors |
| `window_start`, `window_end` | Commit window actually requested |
| `contributors_pre`, `contributors_post` | Non-bot contributors with any non-merge commit before or after the anchor within the window |
| `retained`, `pre_only`, `post_only` | Non-bot contributors active on both sides, only before, or only after |
| `<band>_days_observed` | Days of the band inside the window up to the collection date. A band with 0 observed days has **blank**, not zero, counts |
| `<band>_contributors`, `<band>_commits` | Non-bot contributors and non-merge commits in the band |

`post_only` means not seen in the pre-period window, not "first-ever
contribution". Post-policy bands for recent anchors are right-censored; compare
rates per observed day, not raw counts. `contributor_bands.csv` has one row per
repository and contributor, with commits per band, first and last commit time,
`presence`, and `is_bot`. `commits.csv` has one row per commit in the window.
`overall_contributors.csv` holds the all-time list.

## Outcome outputs: pull requests, issues, releases, churn

Commands: `aip collect pulls [--no-reviews]`, `aip collect issues`,
`aip collect releases`, `aip collect churn`, each taking `--policy-run` like
`contributors`. Locations: `data/interim/{pull_requests,issues,releases,churn}/<run_id>/`.

All four share the contributor collector's **anchor** rule (`treatment.anchor_event`;
not verified) and **bands**. The observed window runs from the start
of `pre_long` to the earlier of the end of `post_long` and the collection date.
The *cutoff* is the first instant after that window. Events after the cutoff are
treated as unobserved. A band with 0 `<band>_days_observed` has **blank** counts,
not zero, and a repository with no anchor is `partial` and is not queried.
Bot authors (GraphQL `Bot` type or a `[bot]` login/email) are excluded from
band counts and counted separately. Medians are blank when no event qualifies.

**Pull requests and issues** come from the GraphQL `pullRequests`/`issues`
connections ordered by `UPDATED_AT` descending. Anything created, merged, or
closed in the window was updated in it, so the sweep stops at the first item last
updated before the window. A second pass re-reads the head of the list down to the
sweep's start time, which catches items that moved past the cursor because they
were updated during the run. The sweep reads every item updated since the window
start, including items touched after the window ended, so old anchors cost more
than their window suggests. The GraphQL `issues` connection never includes pull
requests (unlike REST `/issues`). If a sweep fails part-way, the record is `partial`
and **all** counts are withheld, because a partial sweep would undercount every band.

`pull_requests.csv` has one row per PR that was created, merged, or closed
without merging in the window:

| Field | Definition / interpretation |
| --- | --- |
| `created_band`, `merged_band`, `closed_unmerged_band` | Band of each event; blank when outside the window or after the cutoff |
| `hours_to_merge`, `hours_to_close_unmerged` | From creation; blank when resolution falls after the cutoff |
| `open_at_cutoff` | Unresolved at the cutoff. Pre-policy PRs have had longer to resolve than post-policy ones, so compare durations with a censoring-aware method, not raw medians alone |
| `author_association` | GitHub's **current** relationship, not the relationship when the PR was opened |
| `targets_default_branch` | Whether the base branch is the default branch; summaries include all bases |
| `is_draft`, `state` | Current values |
| `additions`, `deletions`, `changed_files`, `commits`, `comments` | PR totals as of collection |
| `reviews_*`, `reviewers`, `approvals`, `changes_requested`, `first_review_at`, `hours_to_first_review` | Submitted reviews before the cutoff by someone other than the PR author; bots excluded. `reviews_truncated` is set when a PR has more than 30 reviews and only the first 30 were read. Absent with `--no-reviews` |

PR `summary.csv` per band: `prs_opened`, `prs_merged`, `prs_closed_unmerged`
(counted in the band the event happened), `prs_open_at_cutoff` and
`median_hours_to_merge` (over PRs opened in the band), `merged_additions` and
`merged_deletions` (PRs merged in the band), and, with reviews,
`prs_without_review` and `median_hours_to_first_review`. **Review rounds are not
defined yet.** `changes_requested` per PR is the raw material; the definition is
a research-team decision.

`issues.csv` and the issue `summary.csv` follow the same pattern, with
`issues_opened`, `issues_closed`, `issues_closed_completed` (`stateReason`
`COMPLETED`, as opposed to `NOT_PLANNED` or `DUPLICATE`), `issues_open_at_cutoff`,
and `median_hours_to_close`. A repository whose issue tracker is disabled is
`partial` with no counts. That is missing, not zero, and `has_issues` is the
current setting only. Deleted and transferred issues are absent.

**Releases** come from REST `/releases`. Only published, non-draft releases
count. `releases.csv` has one row per release in the window; the summary has
`releases`, `stable_releases` (not `prerelease`), and
`mean_days_between_stable`, which is blank with fewer than two stable releases in
the band. `releases_total` and `first_release_at` are all-time. **Releases are not
tags.** A project that only pushes tags looks release-less here.

**Churn** comes from the GraphQL default-branch commit history between the window
start and the cutoff, with GitHub's per-commit `additions`/`deletions`. `commits.csv`
has one row per commit, using the contributor collector's identity and hashed-email
rule. Band totals exclude merge commits (they repeat their branch's changes) and
bot commits. Lines include generated, vendored, and lock files, since per-file
paths are not collected; `changed_files` is blank when GitHub does not compute it.

`referenced_prs` lists the pull requests a commit says it lands (`;`-separated in
the CSV). Only two places count: the title (GitHub's squash suffix `(#N)`,
`Merge pull request #N`, `Merge owner/repo#N`), and a line that *starts* with a
landing keyword followed directly by `#N`, `owner/repo#N`, or a link to the PR
(`Pull Request resolved:`, `(Merged from …)`, `Merged-from:`, `PR #N`, `Closes`,
`Fixes`, `Resolves`). Reverts, mentions elsewhere ("supersedes …", "see also PR
#N", `ghstack dependencies:`), comment links, and other repositories are ignored.
Issues share the numbering, so a number may be an issue; the panel only matches
known PRs. Commit messages themselves are not stored (they contain co-author
emails). 

**Rate limits.** GraphQL responses report their remaining budget. When fewer than
100 points remain, the collector sleeps until the reset, or stops if the reset is
more than about an hour away. REST requests still follow the client's bounded
retries.

## Additional outputs for RQ1-RQ3

See each item for the rule.

**Pull requests (`aip collect pulls`)** now also record, per PR:

- `merged_by`, `merged_by_is_bot`: who pressed merge (used for maintainer counts).
- `ci_state`: GitHub's combined check-and-status result on the PR's last commit
  as reported now (`SUCCESS`, `FAILURE`, `ERROR`, `PENDING`, `EXPECTED`, or blank
  when no CI reports to GitHub). It is the PR's final CI result, not its first run.
- A second pass lists every PR open today, so PRs opened before the window and
  untouched since are kept (with blank bands) for the open-PR backlog. Records
  have `open_prs_collected: true`; `prs_in_window` now includes them.

**Churn (`aip collect churn`)** adds `committer_login` (who put the commit on the
branch) and `ci_state` (as above, for each default-branch commit).

**CI across the PR lifecycle (`aip collect ci --policy-run <run>`)**:
`pull_request_ci.csv`, one row per PR opened in the window. For each PR, the
combined CI result of every commit pushed to it, oldest first; only the first 50
and last 50 commits are read (`commits_truncated` flags longer PRs, whose middle
pushes are skipped). CI usually runs on the head of each push, so commits with a
result (`ci_results`) stand for pushes. Per PR: `ci_failures` (results that are
`FAILURE` or `ERROR`), `first_ci_state`, `final_ci_state`, and `ci_fixed`
(failed at least once, final result `SUCCESS`). A failing run that was re-run on
the same commit and passed shows only the pass. Results are as GitHub reports
them now; projects whose CI does not report to GitHub have none.

**Comments (`aip collect comments --policy-run <run>`)**:
`data/interim/comments/<run_id>/` with `comments.csv`. Two repository-wide REST
listings: conversation comments on issues and PRs, and review comments on PR
diffs. Each row keeps the comment ID, kind, item number, whether the item is a
PR, author, bot flag and creation time; never the text. Comments created before
the window but edited in it are dropped.

**Discussions (`aip collect discussions --policy-run <run>`)**: GitHub
Discussions only; mailing lists, chat and forums are a stated limitation.
`posts.csv` has one row per opening post, comment or reply created in the window.
The first 50 comments per discussion and 30 replies per comment are read;
`discussions_with_comments_truncated` / `_replies_truncated` count where more
exist. A repository with Discussions turned off (current setting) is `complete`
with `discussions_enabled: false` and no posts.

**First contributions (`aip collect first-seen`)**: not tied to the policy date.
Reads the full history of default-branch commits, PRs and issues and keeps, per
person, the earliest date of each (`first_seen.csv`: `person`, `is_bot`,
`commit`, `pull_request`, `issue`). Commit authors are identified as in churn
(login, else hashed email). Reviews and comments are not covered, so someone who
only commented before counts as new at their first authored contribution. GitHub's
`authorAssociation` cannot replace this: it reflects each author's status today
(no pilot PR is tagged as a first-time contribution).

**Fork history (`aip collect forks`)**: each surviving public fork by its creation
day (`daily.csv`, days with new forks only). Deleted forks are not listed, so past
counts are surviving forks; `reconciliation` compares the listed total with the
repository's `forks_count`, which usually differs slightly (it is accepted).

**Context (`aip collect context --policy-run <run>`)**: repository metadata
(creation date, language, license, Discussions/issues/wiki settings), owner
figures (type, creation date, public repositories, followers; for organizations
public members and verification), all as of collection. Maintainer files
(`MAINTAINERS`, `CODEOWNERS`, `OWNERS`, `COMMITTERS`, `GOVERNANCE`, any
extension, vendored paths excluded) are read at the last default-branch commit
before the policy date. `@user` handles and GitHub profile links are counted
(`maintainer_file_user_count`; logins kept in `maintainer_file_users`);
`@org/team` handles are counted separately (`maintainer_file_team_count`) and not
resolved, because team membership is not visible from outside the organization.
Blank when no such file exists.

**Policy type** is coded by hand in the `policy_type` column of
`data/repositories/sampled_policy_repos.csv`; the weekly build copies it as written.

## Comparison repositories (no AI policy)

Command: `aip select comparison --priority pilot --policy-run <run> --seed <n>
[--skip owner/repo]`. One comparison repository per policy repository, written
to `data/repositories/comparison_repos.csv` for review before collecting. The run's records, with every candidate checked and
why it was rejected, are in `data/interim/comparison_selection/<run_id>/`.

**Stand-in dates.** A comparison repository has no policy, so it gets a date to
count weeks from. Policy dates are grouped into clusters: early (before
2026-01-01), middle (before 2026-05-01) and late. Each comparison repository
takes the date of a policy repository in its partner's cluster, the dates being
shuffled within the cluster and handed out once each, shifted by up to 14 days
either way. The comparison group therefore reproduces the policy dates'
distribution, so calendar effects
(Hacktoberfest, holidays, GitHub-wide trends) fall on both groups alike. The
seed is recorded in each row. If verification moves a policy date into another
cluster, redraw. `windows.anchor_dates` uses `stand_in_date` whenever a
candidate row has one (`anchor_source = stand_in`).

**Matching.** Candidates come from repository search: same primary language,
stars within 1.5x of the policy repository's (3x when fewer than 20 are found),
not a fork, archive, mirror or template, created more than a year before the
stand-in date, pushed within 60 days, and not in the policy registry. The 15
closest by stars are checked for the no-policy rule and their PRs counted for
the year before the stand-in date (search counts; bots included, as for the
policy repository). The closest by PR volume, then stars, then owner type is
proposed, with the next two as `alternates`. `match_quality` is `good` when PRs
are within 2x and stars within 3x. Stars are current counts for both sides.
Largest policy repositories choose first. When no candidate in the language
passes, the search is repeated in any language (the row's `language` shows it).

**No-policy rule.** At the default branch's head: no `AI_POLICY.md` anywhere and
no `CONTRIBUTING.md` mentioning an AI term (`ai_terms.CONTRIBUTING_TERMS`, the
same list as both finder scripts). A tree too large to list is "unconfirmed" and
skipped. The head is checked because the comparison window ends on or before
today; a policy added and later removed would be missed.

Collect comparison repositories with the same commands as the pilot, using
`--candidates data/repositories/sampled_comparison_repos.csv`. Build them into
the same table with `aip build weekly ... --comparison
data/repositories/sampled_comparison_repos.csv`; rows then carry `group` (`policy` or
`comparison`), and `characteristics.csv` adds `matched_policy_repo` and
`date_cluster`.

## Repository-week panel

Command: `aip build weekly --priority pilot --policy-run <policy-history run>`.
Location: `data/panels/<run_id>/` with `panel.csv` (one row per
repository and event week), `characteristics.csv` (one row per repository, below),
`repositories.csv` (per repository: anchor, panel end, the run used for each
source and how far it was observed, missing sources), and `summary.json`. This
folder is committed; it holds weekly totals only, no logins.

**Inputs.** Eleven sources. For pull requests, issues, releases, churn, CI,
comments, discussions and context: the newest run where the repository is `complete` *and*
its `anchor_date` equals the panel's anchor. For star history, fork history and
first contributions (not tied to the anchor): the newest `complete` run; a star
history whose only problem is a reconciliation mismatch is accepted. Reviewer
logins are read from that pull-request run's raw responses in `data/raw/`, which
stay local. A repository is `complete` when all eleven are present.

**Weeks.** `event_week` counts 7-day weeks from the anchor date: week 0 starts on
the anchor, `-52..-1` are before (`post=0`) and `0..51` after (`post=1`); the one
remaining day of each 365-day band is not used. Events are assigned by UTC date.
The **panel end** is the earliest date through which all of the repository's
time-series sources were observed (capped at week 51); for first contributions
that is the day before its run started. Context does not limit it. Weeks after
the end are omitted, events after it are ignored, and `days_observed` (1-7) flags
a partly observed last week.

**Bots** are excluded from every column except releases, which projects often
publish from CI accounts, and default-branch CI results, which describe the
branch. Merge commits are excluded from commit counts, lines, files and commit
authors.

### RQ1: workflow

| Column | Definition |
| --- | --- |
| `prs_opened`, `prs_merged`, `prs_closed_unmerged` | Human PRs (all branches, drafts included) by week created / merged / closed without merging |
| `prs_landed`, `prs_closed_unlanded` | Human PRs by week landed / closed without landing. A PR lands when GitHub merged it, or, if closed unmerged, when a default-branch commit names it in `referenced_prs` between the PR's opening and 7 days after its closing (earliest such commit). Use these instead of the merge columns for projects that land changes outside GitHub's merge button (pytorch, openssl, ripgrep). Blank when the churn run lacks `referenced_prs` |
| `open_prs` | Backlog: human PRs opened by the week's last observed day and not closed by then. Blank for PR runs without `open_prs_collected` |
| `issues_opened`, `issues_closed` | Human-authored issues by week created / closed (any reason) |
| `issues_closed_completed`, `issues_closed_not_planned` | Closed as `COMPLETED`; closed as `NOT_PLANNED` or `DUPLICATE`. Other or blank reasons count only in `issues_closed` |
| `releases`, `stable_releases` | Published, non-draft releases (any author) / excluding prereleases |
| `commits`, `lines_added`, `lines_deleted`, `files_changed` | Code churn: non-bot, non-merge default-branch commits by committed date. `files_changed` sums files per commit where GitHub reports it (very large commits may lack it) |
| `median_lines_per_commit` | Lines added plus deleted, median over that week's commits |
| `median_pr_lines_by_opened`, `median_pr_files_by_opened`, `median_pr_commits_by_opened` | PR size: lines added plus deleted, files changed, and commits, median over PRs opened that week (size as of collection) |
| `median_reviews_per_pr_by_opened` | Review rounds: reviews by people other than the author (bots excluded), median over PRs opened that week; recent weeks are low because later reviews are not yet in. First 30 reviews per PR |
| `prs_changes_requested` | PRs opened that week with at least one "changes requested" review by someone else |
| `prs_ci_success`, `prs_ci_failure` | PRs opened that week whose final CI result is `SUCCESS` / `FAILURE` or `ERROR`; pending and missing results count in neither. Blank when no PR in the repository reports CI to GitHub |
| `commits_ci_success`, `commits_ci_failure` | The same for default-branch commits (all commits, merges and bots included) by committed week |
| `prs_with_ci` | PRs opened that week with at least one CI result on any push (denominator for the next three). Blank when no PR in the repository reports CI |
| `prs_first_ci_failed` | ...whose first CI result failed (`FAILURE` or `ERROR`) |
| `prs_ci_fixed` | ...that failed CI at least once and ended passing |
| `median_ci_failures_per_pr_by_opened` | Failing pushes per PR, median over PRs with CI opened that week |
| `median_hours_to_merge_by_opened` | PRs opened this week and merged by the panel end; recent weeks are biased low because slow PRs are not yet merged |
| `median_hours_to_merge_by_merged` | PRs merged this week; no end-of-panel bias, but mixes PRs opened at different times |
| `median_hours_to_land_by_opened` / `_by_landed` | Opening to landing (merge time, or the landing commit's committed date), split as for merges |
| `median_hours_to_first_review_by_opened` / `_by_reviewed` | Same split for the first non-bot review by someone other than the author |
| `median_hours_to_close_by_opened` / `_by_closed` | As for PRs, for issues |

### RQ2: engagement

| Column | Definition |
| --- | --- |
| `pr_comments`, `issue_comments` | Human comments posted that week on PRs (conversation and review comments) / on issues |
| `discussions_opened`, `discussion_comments` | Human GitHub Discussions opened / comments and replies posted that week |
| `commit_authors`, `pr_authors`, `issue_authors`, `reviewers` | Distinct people who committed, opened a PR, opened an issue, or reviewed someone else's PR that week |
| `active_contributors` | Distinct people across those four (unchanged from the pilot); blank when any is unavailable |
| `commenters`, `discussion_participants` | Distinct people who commented on a PR or issue / posted in Discussions |
| `engaged_people` | Distinct people across all six; blank when any is unavailable |
| `newcomers` | People whose first-ever authored commit, PR or issue in the repository (full history) falls in the week |
| `first_time_pr_authors` | People whose first-ever PR in the repository falls in the week |
| `returning_contributors` | Active contributors this week who were also active contributors in any of the 12 weeks before. Blank for weeks -52..-41, which lack 12 observed weeks before them |
| `new_stars`, `cumulative_stars` | Stars gained in the week; total at the week's last observed day |
| `new_forks`, `cumulative_forks` | The same for surviving public forks |

`repositories.csv` adds `landing` (whether landing columns were computed) and
`prs_landed_by_commit` (closed-unmerged human PRs reclassified as landed). In the
pilot check against commit messages to 2026-09-25, the rule reclassified 0.4% of
closed-unmerged PRs in repositories that use GitHub's merge button (42 of 11,450)
and about 60% in pytorch, openssl, and dart-lang/sdk. dart-lang/sdk reviews most
changes in Gerrit, so its PR columns cover only the few changes made via GitHub.

People are matched by lower-cased GitHub login. Commit authors with no linked
account keep their hashed-email ID, so they cannot be matched to their PRs or
issues and may be counted twice. Reviews come from the first 30 per PR.

Counts are 0 for weeks without events; medians are blank when no event
qualifies. A source with no usable run (for example, a disabled issue tracker)
leaves its columns blank rather than zero, and the repository is `partial`.

### RQ3: `characteristics.csv`

One row per repository, as of the policy date where GitHub allows.

| Column | Definition |
| --- | --- |
| `policy_type`, `policy_type_coded_by` | `policy_type` from `sampled_policy_repos.csv`; `policy_type_coded_by` is blank |
| `repository_created_at`, `age_at_policy_years` | Creation date; years from creation to the policy date |
| `primary_language`, `license_spdx_id`, `has_discussions` | Current values |
| `stars_at_policy`, `forks_at_policy` | Surviving stars / forks on the day before the policy date |
| `owner_login`, `owner_type`, `owner_created_at`, `owner_age_at_policy_years` | Organization or user that owns the repository |
| `owner_public_repos`, `owner_followers`, `owner_public_members`, `owner_is_verified` | Owner figures as of collection (members and verification for organizations only; public members only) |
| `maintainer_file_user_count`, `maintainer_file_team_count` | People / teams named in maintainer files at the policy date (see Context) |
| `mergers_pre_policy` | Maintainer proxy: distinct people who merged a PR, or committed the commit that landed one, in the 26 weeks before the policy; bots and GitHub's `web-flow` excluded. `mergers_include_landing` is false when only GitHub merges could be used |
| `maintainer_count_manual` | Manual maintainer counts were not used in the paper; blank |

The three maintainer measures answer different questions (who is listed, who
merges, who the project names in its governance) and are kept side by side for
checking one against another.

## Provenance and limits

`data/raw/repository_profiles/<run_id>/manifest.json` records the input checksum,
selection, API version, start/end times, request count and result totals.
`candidates.csv` retains the exact input bytes so later registry edits do not erase
the run's input. The manifest checksum applies to this retained snapshot.
`requests/requests.jsonl` records each attempt and its response file/checksum.
The journal stores no Authorization header or credential. File snapshots are
under `files/`; normalized profiles can be audited against the retained raw data.
Runs use fresh IDs and never overwrite earlier snapshots. An interrupted run
retains completed profiles and a `running` manifest; it is not a completed sample.

Retries are bounded (three attempts, 45-second request timeout, backoff waits at
most 30 seconds). A `retry-after` that GitHub sends, as with secondary rate limits,
is honoured up to 5 minutes. Rate-limit windows requiring longer waits are recorded as errors,
except that the GraphQL outcome collectors wait for a reset (see above).
The CLI exits 2 for partial/failed collection, 1 for invalid input/local failure,
and 0 for a fully successful run. Component-specific limitations still apply.

## API references

- [Repository metadata](https://docs.github.com/en/rest/repos/repos#get-a-repository)
- [Language bytes](https://docs.github.com/en/rest/repos/repos#list-repository-languages)
- [README/content API](https://docs.github.com/en/rest/repos/contents#get-a-repository-readme)
- [Watching versus starring](https://docs.github.com/en/rest/activity/watching)
- [Projects API and read:project authentication](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-api-to-manage-projects)

Checked 2026-09-17. The new REST client pins API version `2026-03-10`.
