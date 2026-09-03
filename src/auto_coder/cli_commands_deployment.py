"""Operator commands for release/beta repository routing."""

from pathlib import Path

import click

from .deployment_channel import VALID_CHANNELS, DeploymentChannelError, assign_repository


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
