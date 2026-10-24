"""Command-line interface for the research pipeline."""

import platform
import sys
from pathlib import Path

import typer
import yaml
from pydantic import ValidationError

from ai_policy_impact import __version__
from ai_policy_impact.config import StudyConfig, load_study_config

DEFAULT_CONFIG = Path("configs/study.yaml")

app = typer.Typer(
    name="aip",
    help="AI policy impact research pipeline.",
    no_args_is_help=True,
)
config_app = typer.Typer(help="Inspect and validate study configuration.")
app.add_typer(config_app, name="config")
collect_app = typer.Typer(help="Collect dated GitHub research snapshots.")
app.add_typer(collect_app, name="collect")
build_app = typer.Typer(help="Build analysis datasets from collected runs.")
app.add_typer(build_app, name="build")
select_app = typer.Typer(help="Propose samples for review.")
app.add_typer(select_app, name="select")


def _load_or_exit(path: Path) -> StudyConfig:
    try:
        return load_study_config(path)
    except (OSError, TypeError, ValueError, ValidationError, yaml.YAMLError) as error:
        typer.secho(f"Configuration invalid: {error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from error


@app.callback(invoke_without_command=True)
def application(
    context: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        help="Show the installed package version and exit.",
        is_eager=True,
    ),
) -> None:
    """Run the AI policy impact research pipeline."""

    if version:
        typer.echo(__version__)
        raise typer.Exit()

    if context.invoked_subcommand is None:
        typer.echo(context.get_help())


@app.command()
def doctor(
    config: Path = typer.Option(
        DEFAULT_CONFIG,
        "--config",
        help="Path to the study configuration file.",
        dir_okay=False,
    ),
) -> None:
    """Check the local runtime and configuration without using the network."""

    study = _load_or_exit(config)
    typer.echo(f"ai-policy-impact: {__version__}")
    typer.echo(f"python: {platform.python_version()}")
    typer.echo(f"config: valid ({config})")
    typer.echo(f"primary panel: {study.primary_panel.value}")
    typer.echo("network: not accessed")

    if sys.version_info[:2] != (3, 12):
        typer.secho("Expected Python 3.12.", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=1)


@config_app.command("validate")
def validate_config(
    path: Path = typer.Option(
        DEFAULT_CONFIG,
        "--path",
        help="Path to the study configuration file.",
        dir_okay=False,
    ),
) -> None:
    """Validate the study configuration and print its approved design choices."""

    study = _load_or_exit(path)
    typer.echo(f"Configuration valid: {path}")
    typer.echo(f"schema version: {study.schema_version}")
    typer.echo(f"primary panel: {study.primary_panel.value}")
    typer.echo(f"medium run: {study.horizons.medium_run_days} days")
    typer.echo(f"long run: {study.horizons.long_run_days} days")
    typer.echo(f"eligibility activity basis: {study.eligibility.activity_basis.value}")
    typer.echo(f"policy anchor event: {study.treatment.anchor_event.value}")


@collect_app.command("profiles")
def collect_repository_profiles(
    candidates: Path = typer.Option(
        Path("data/repositories/sampled_policy_repos.csv"), dir_okay=False
    ),
    config: Path = typer.Option(DEFAULT_CONFIG, dir_okay=False),
    priority: str | None = typer.Option(
        None, help="Filter the candidate priority, e.g. pilot."
    ),
    limit: int | None = typer.Option(
        None, min=1, help="Collect at most this many repositories."
    ),
    projects: bool = typer.Option(
        True,
        "--projects/--no-projects",
        help="Attempt public Projects v2 Status counts.",
    ),
) -> None:
    """Collect metadata, README, languages, policy and governance evidence."""
    from ai_policy_impact.collect import collect_profiles

    study = _load_or_exit(config)
    try:
        output, summary = collect_profiles(
            study,
            candidates,
            priority=priority,
            limit=limit,
            include_projects=projects,
            progress=typer.echo,
        )
    except (OSError, ValueError) as error:
        typer.secho(f"Collection failed: {error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Profiles: {output}")
    typer.echo(
        f"Repositories: {summary['repositories']}; README files: {summary['readmes']}; "
        f"complete: {summary['complete']}; partial: {summary['partial']}; failed: {summary['failed']}"
    )
    if summary["partial"] or summary["failed"]:
        typer.echo(
            "Some components are unavailable; see each profile's status and errors."
        )
        raise typer.Exit(code=2)


CANDIDATES_OPTION = typer.Option(
    Path("data/repositories/sampled_policy_repos.csv"), dir_okay=False
)
CONFIG_OPTION = typer.Option(DEFAULT_CONFIG, dir_okay=False)
PRIORITY_OPTION = typer.Option(None, help="Filter the candidate priority, e.g. pilot.")
LIMIT_OPTION = typer.Option(None, min=1, help="Collect at most this many repositories.")


def _run_collection(label: str, collector, config: Path, **kwargs) -> None:
    study = _load_or_exit(config)
    try:
        output, summary = collector(study, progress=typer.echo, **kwargs)
    except (OSError, ValueError) as error:
        typer.secho(f"Collection failed: {error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"{label}: {output}")
    typer.echo(
        f"Repositories: {summary['repositories']}; complete: {summary['complete']}; "
        f"partial: {summary['partial']}; failed: {summary['failed']}"
    )
    if summary["partial"] or summary["failed"]:
        typer.echo("Some records are incomplete; see each record's status and errors.")
        raise typer.Exit(code=2)


@collect_app.command("stars")
def collect_star_history(
    candidates: Path = CANDIDATES_OPTION,
    config: Path = CONFIG_OPTION,
    priority: str | None = PRIORITY_OPTION,
    limit: int | None = LIMIT_OPTION,
) -> None:
    """Collect current star counts and weekly/daily star history."""
    from ai_policy_impact.stars import collect_star_histories

    _run_collection(
        "Star history",
        collect_star_histories,
        config,
        candidates_path=candidates,
        priority=priority,
        limit=limit,
    )


@collect_app.command("policy-history")
def collect_policy_history(
    candidates: Path = CANDIDATES_OPTION,
    config: Path = CONFIG_OPTION,
    priority: str | None = PRIORITY_OPTION,
    limit: int | None = LIMIT_OPTION,
) -> None:
    """Trace policy-file commits and propose (unverified) adoption dates."""
    from ai_policy_impact.policy_history import collect_policy_histories

    _run_collection(
        "Policy history",
        collect_policy_histories,
        config,
        candidates_path=candidates,
        priority=priority,
        limit=limit,
    )


@collect_app.command("contributors")
def collect_contributor_bands(
    candidates: Path = CANDIDATES_OPTION,
    config: Path = CONFIG_OPTION,
    priority: str | None = PRIORITY_OPTION,
    limit: int | None = LIMIT_OPTION,
    policy_run: Path | None = typer.Option(
        None,
        file_okay=False,
        exists=True,
        help="A policy-history interim run; its detected dates are the band "
        "anchor when treatment.anchor_event is first_ai_mention (required then).",
    ),
) -> None:
    """Collect overall contributors and commit authors in bands around the policy date."""
    from ai_policy_impact.contributors import collect_contributors

    _run_collection(
        "Contributors",
        collect_contributors,
        config,
        candidates_path=candidates,
        policy_run=policy_run,
        priority=priority,
        limit=limit,
    )


POLICY_RUN_OPTION = typer.Option(
    None,
    file_okay=False,
    exists=True,
    help="A policy-history interim run; its detected dates are the band "
    "anchor when treatment.anchor_event is first_ai_mention (required then).",
)


@collect_app.command("pulls")
def collect_pull_requests(
    candidates: Path = CANDIDATES_OPTION,
    config: Path = CONFIG_OPTION,
    priority: str | None = PRIORITY_OPTION,
    limit: int | None = LIMIT_OPTION,
    policy_run: Path | None = POLICY_RUN_OPTION,
    reviews: bool = typer.Option(
        True, "--reviews/--no-reviews", help="Also collect each PR's reviews."
    ),
) -> None:
    """Collect pull requests, reviews, and PR size in bands around the policy date."""
    from ai_policy_impact.pulls import collect_pulls

    _run_collection(
        "Pull requests",
        collect_pulls,
        config,
        candidates_path=candidates,
        policy_run=policy_run,
        priority=priority,
        limit=limit,
        reviews=reviews,
    )


@collect_app.command("issues")
def collect_issue_activity(
    candidates: Path = CANDIDATES_OPTION,
    config: Path = CONFIG_OPTION,
    priority: str | None = PRIORITY_OPTION,
    limit: int | None = LIMIT_OPTION,
    policy_run: Path | None = POLICY_RUN_OPTION,
) -> None:
    """Collect issues opened and closed in bands around the policy date."""
    from ai_policy_impact.issues import collect_issues

    _run_collection(
        "Issues",
        collect_issues,
        config,
        candidates_path=candidates,
        policy_run=policy_run,
        priority=priority,
        limit=limit,
    )


@collect_app.command("releases")
def collect_release_activity(
    candidates: Path = CANDIDATES_OPTION,
    config: Path = CONFIG_OPTION,
    priority: str | None = PRIORITY_OPTION,
    limit: int | None = LIMIT_OPTION,
    policy_run: Path | None = POLICY_RUN_OPTION,
) -> None:
    """Collect published releases in bands around the policy date."""
    from ai_policy_impact.releases import collect_releases

    _run_collection(
        "Releases",
        collect_releases,
        config,
        candidates_path=candidates,
        policy_run=policy_run,
        priority=priority,
        limit=limit,
    )


@collect_app.command("churn")
def collect_code_churn(
    candidates: Path = CANDIDATES_OPTION,
    config: Path = CONFIG_OPTION,
    priority: str | None = PRIORITY_OPTION,
    limit: int | None = LIMIT_OPTION,
    policy_run: Path | None = POLICY_RUN_OPTION,
) -> None:
    """Collect lines added/deleted by default-branch commits around the policy date."""
    from ai_policy_impact.churn import collect_churn

    _run_collection(
        "Code churn",
        collect_churn,
        config,
        candidates_path=candidates,
        policy_run=policy_run,
        priority=priority,
        limit=limit,
    )


@collect_app.command("ci")
def collect_pull_request_ci(
    candidates: Path = CANDIDATES_OPTION,
    config: Path = CONFIG_OPTION,
    priority: str | None = PRIORITY_OPTION,
    limit: int | None = LIMIT_OPTION,
    policy_run: Path | None = POLICY_RUN_OPTION,
) -> None:
    """Collect CI results for every push to each PR around the policy date."""
    from ai_policy_impact.ci import collect_ci

    _run_collection(
        "Pull-request CI",
        collect_ci,
        config,
        candidates_path=candidates,
        policy_run=policy_run,
        priority=priority,
        limit=limit,
    )


@collect_app.command("comments")
def collect_comment_activity(
    candidates: Path = CANDIDATES_OPTION,
    config: Path = CONFIG_OPTION,
    priority: str | None = PRIORITY_OPTION,
    limit: int | None = LIMIT_OPTION,
    policy_run: Path | None = POLICY_RUN_OPTION,
) -> None:
    """Collect PR and issue comments (who and when, no text) around the policy date."""
    from ai_policy_impact.comments import collect_comments

    _run_collection(
        "Comments",
        collect_comments,
        config,
        candidates_path=candidates,
        policy_run=policy_run,
        priority=priority,
        limit=limit,
    )


@collect_app.command("discussions")
def collect_discussion_activity(
    candidates: Path = CANDIDATES_OPTION,
    config: Path = CONFIG_OPTION,
    priority: str | None = PRIORITY_OPTION,
    limit: int | None = LIMIT_OPTION,
    policy_run: Path | None = POLICY_RUN_OPTION,
) -> None:
    """Collect GitHub Discussions posts around the policy date."""
    from ai_policy_impact.discussions import collect_discussions

    _run_collection(
        "Discussions",
        collect_discussions,
        config,
        candidates_path=candidates,
        policy_run=policy_run,
        priority=priority,
        limit=limit,
    )


@collect_app.command("first-seen")
def collect_first_contributions(
    candidates: Path = CANDIDATES_OPTION,
    config: Path = CONFIG_OPTION,
    priority: str | None = PRIORITY_OPTION,
    limit: int | None = LIMIT_OPTION,
) -> None:
    """Collect each person's first-ever commit, PR, and issue (full history)."""
    from ai_policy_impact.first_seen import collect_first_seen

    _run_collection(
        "First seen",
        collect_first_seen,
        config,
        candidates_path=candidates,
        priority=priority,
        limit=limit,
    )


@collect_app.command("forks")
def collect_fork_history(
    candidates: Path = CANDIDATES_OPTION,
    config: Path = CONFIG_OPTION,
    priority: str | None = PRIORITY_OPTION,
    limit: int | None = LIMIT_OPTION,
) -> None:
    """Collect dated fork histories (surviving public forks)."""
    from ai_policy_impact.forks import collect_fork_histories

    _run_collection(
        "Fork history",
        collect_fork_histories,
        config,
        candidates_path=candidates,
        priority=priority,
        limit=limit,
    )


@collect_app.command("context")
def collect_repository_context(
    candidates: Path = CANDIDATES_OPTION,
    config: Path = CONFIG_OPTION,
    priority: str | None = PRIORITY_OPTION,
    limit: int | None = LIMIT_OPTION,
    policy_run: Path | None = POLICY_RUN_OPTION,
) -> None:
    """Collect owner context and maintainer files as of the policy date."""
    from ai_policy_impact.context import collect_context

    _run_collection(
        "Context",
        collect_context,
        config,
        candidates_path=candidates,
        policy_run=policy_run,
        priority=priority,
        limit=limit,
    )


@build_app.command("weekly")
def build_repository_week_panel(
    candidates: Path = CANDIDATES_OPTION,
    config: Path = CONFIG_OPTION,
    priority: str | None = PRIORITY_OPTION,
    policy_run: Path | None = POLICY_RUN_OPTION,
    comparison: Path | None = typer.Option(
        None,
        dir_okay=False,
        exists=True,
        help="Comparison repositories (no AI policy), e.g. "
        "data/repositories/sampled_comparison_repos.csv; added with group 'comparison'.",
    ),
) -> None:
    """Build the repository-week panel from the newest usable run per repository."""
    from ai_policy_impact.panel import build_panel

    study = _load_or_exit(config)
    try:
        output, summary = build_panel(
            study,
            candidates,
            policy_run=policy_run,
            priority=priority,
            comparison_path=comparison,
            progress=typer.echo,
        )
    except (OSError, ValueError) as error:
        typer.secho(f"Weekly build failed: {error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Weekly panel: {output}")
    typer.echo(
        f"Repositories: {summary['repositories']}; complete: {summary['complete']}; "
        f"partial: {summary['partial']}; skipped: {summary['skipped']}; "
        f"rows: {summary['rows']}"
    )
    if summary["partial"] or summary["skipped"]:
        typer.echo("Some repositories lack sources; see repositories.csv.")
        raise typer.Exit(code=2)


@app.command("analyze")
def analyze_panel(
    panel: Path | None = typer.Option(
        None,
        file_okay=False,
        exists=True,
        help="A weekly panel run. Default: the newest under data/panels.",
    ),
    config: Path = CONFIG_OPTION,
    paper: bool = typer.Option(
        True, help="Also write tables and figures into paper/tables and paper/figures."
    ),
    bootstrap_reps: int = typer.Option(
        9999, min=0, help="Wild cluster bootstrap replications (0 skips them)."
    ),
    main_design: str | None = typer.Option(
        None,
        help="Design for the paper's tables and figures: stacked, "
        "comparison_weighted, or comparison. Default: stacked when available.",
    ),
) -> None:
    """Estimate policy effects (RQ1-RQ3) from a weekly panel."""
    try:
        from ai_policy_impact.analysis import analyze
    except ImportError as error:
        typer.secho(
            f"Analysis libraries missing ({error.name}); run with "
            "`uv run --group analysis aip analyze`.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from error

    study = _load_or_exit(config)
    if panel is None:
        runs = sorted((study.data.processed / "panels").glob("*/panel.csv"))
        if not runs:
            typer.secho("No weekly panel found.", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)
        panel = runs[-1].parent
    try:
        output, summary = analyze(
            panel,
            study.data.processed / "results",
            paper_dir=Path("paper") if paper else None,
            bootstrap_reps=bootstrap_reps,
            main_design=main_design,
            progress=typer.echo,
        )
    except (OSError, ValueError) as error:
        typer.secho(f"Analysis failed: {error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Analysis: {output}")
    if not summary["policy_types_estimated"]:
        typer.echo("Policy types not coded in this panel; RQ3 skipped.")
    if summary["model_errors"]:
        typer.echo(f"{summary['model_errors']} models failed; see effects.csv.")


@select_app.command("comparison")
def select_comparison_repositories(
    candidates: Path = CANDIDATES_OPTION,
    config: Path = CONFIG_OPTION,
    priority: str | None = PRIORITY_OPTION,
    policy_run: Path | None = POLICY_RUN_OPTION,
    seed: int = typer.Option(..., help="Random seed for the stand-in date draw."),
    skip: list[str] = typer.Option(
        [], help="Policy repository to leave unmatched (repeatable), e.g. inactive."
    ),
    output: Path | None = typer.Option(
        None, dir_okay=False, help="Default: data/repositories/comparison_repos.csv."
    ),
    overwrite: bool = typer.Option(False, help="Replace an existing proposal file."),
) -> None:
    """Propose one comparison repository (no AI policy) per policy repository."""
    from ai_policy_impact.comparison import select_comparisons

    study = _load_or_exit(config)
    try:
        path, summary = select_comparisons(
            study,
            candidates,
            policy_run=policy_run,
            priority=priority,
            seed=seed,
            skip=skip,
            output=output,
            overwrite=overwrite,
            progress=typer.echo,
        )
    except (OSError, ValueError) as error:
        typer.secho(f"Selection failed: {error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Proposals: {path} ({summary['proposals']} repositories)")
    if summary["partial"] or summary["failed"]:
        typer.echo("Some policy repositories have no match; see the run records.")
        raise typer.Exit(code=2)


def main() -> None:
    """Console-script entry point."""

    app()
