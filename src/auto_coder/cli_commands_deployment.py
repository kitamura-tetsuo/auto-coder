"""Operator commands for release/beta repository routing."""

import os
import subprocess
from pathlib import Path

import click

from .deployment_channel import VALID_CHANNELS, DeploymentChannelError, assign_repository
from .release_catalog import ReleaseCatalog, ReleaseCatalogError, new_record
from .release_promotion import Registry, ReleasePromotion, ReleasePromotionError, write_summary
from .util.gh_cache import GitHubGitDataClient


@click.group("deployment")
def deployment_group() -> None:
    """Manage external deployment repository ownership."""


@deployment_group.command("assign")
@click.argument("repo")
@click.option("--channel", required=True, type=click.Choice(VALID_CHANNELS))
@click.option("--ownership-file", required=True, type=click.Path(path_type=Path))
@click.option("--runtime-parent", required=True, type=click.Path(path_type=Path))
def assign(repo: str, channel: str, ownership_file: Path, runtime_parent: Path) -> None:
    """Assign REPO after proving its current channel has no active work."""
    try:
        assign_repository(repo, channel, ownership_file, runtime_parent)
    except DeploymentChannelError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Assigned {repo} to {channel}")


@deployment_group.group("release-catalog")
def release_catalog_group() -> None:
    """Prepare and read immutable annotated-tag release records."""


def _catalog(repository: str, token: str | None, api_url: str) -> ReleaseCatalog:
    credential = token or os.environ.get("GITHUB_TOKEN")
    if not credential:
        raise click.ClickException("GITHUB_TOKEN or --github-token is required")
    try:
        return ReleaseCatalog(GitHubGitDataClient(credential, repository, api_url), repository)
    except ReleaseCatalogError as exc:
        raise click.ClickException(str(exc)) from exc


@release_catalog_group.command("prepare")
@click.option("--repository", required=True)
@click.option("--run-id", required=True, type=int)
@click.option("--requested-at", required=True)
@click.option("--source-sha", required=True)
@click.option("--digest", required=True)
@click.option("--memo", default="", show_default=True)
@click.option("--github-token", envvar="GITHUB_TOKEN", hidden=True)
@click.option("--api-url", envvar="GITHUB_API_URL", default="https://api.github.com", hidden=True)
def prepare_release_catalog(repository: str, run_id: int, requested_at: str, source_sha: str, digest: str, memo: str, github_token: str | None, api_url: str) -> None:
    """Create or confirm one immutable release catalog record."""
    try:
        proposed = new_record(repository, run_id, requested_at, source_sha, digest, memo)
        click.echo(_catalog(repository, github_token, api_url).prepare(proposed).to_json())
    except (ReleaseCatalogError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc


@release_catalog_group.command("read")
@click.option("--repository", required=True)
@click.option("--release-tag", required=True)
@click.option("--github-token", envvar="GITHUB_TOKEN", hidden=True)
@click.option("--api-url", envvar="GITHUB_API_URL", default="https://api.github.com", hidden=True)
def read_release_catalog(repository: str, release_tag: str, github_token: str | None, api_url: str) -> None:
    """Read and strictly validate an exact catalog ref."""
    try:
        click.echo(_catalog(repository, github_token, api_url).read(release_tag).to_json())
    except (ReleaseCatalogError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc


@deployment_group.command("promote-release")
@click.option("--repository", required=True)
@click.option("--run-id", required=True, type=int)
@click.option("--run-attempt", required=True, type=int)
@click.option("--memo-env", default="PROMOTION_MEMO", hidden=True)
@click.option("--github-token", envvar="GITHUB_TOKEN", hidden=True)
@click.option("--api-url", envvar="GITHUB_API_URL", default="https://api.github.com", hidden=True)
def promote_release(repository: str, run_id: int, run_attempt: int, memo_env: str, github_token: str | None, api_url: str) -> None:
    """Promote once or conservatively reconcile an existing operation."""
    if not github_token:
        raise click.ClickException("catalog token is required")
    try:
        outcome = ReleasePromotion(GitHubGitDataClient(github_token, repository, api_url), Registry(), repository, run_id, run_attempt, os.environ.get(memo_env, "")).execute()
        write_summary(outcome)
        click.echo(f"{outcome.result}: {outcome.record.release_tag} {outcome.release_url}")
    except (ReleasePromotionError, ReleaseCatalogError, RuntimeError, subprocess.SubprocessError) as exc:
        raise click.ClickException(str(exc)) from exc
