import os
import re
import sys

import click

from .auth_utils import get_github_token
from .util.gh_cache import GitHubClient
from .logger_config import setup_logger
from .util.github_action import _get_playwright_artifact_logs


@click.command()
@click.option("--github-action-log-summary", help="GitHub Action Run URL to summarize", required=False)
def debug(github_action_log_summary: str) -> None:
    """Debug utilities for Auto-Coder."""
    # Route logs to stderr
    setup_logger(stream=sys.stderr)

    if github_action_log_summary:
        # Ensure GitHub token is available and client is initialized
        token = get_github_token()
        if not token:
            click.echo("Error: GitHub token not found. Please set GITHUB_TOKEN or run 'gh auth login'.", err=True)
            sys.exit(1)

        # Initialize GitHubClient
        GitHubClient.get_instance(token=token)

        url = github_action_log_summary
        # Parse URL
        # Pattern: github.com/{owner}/{repo}/actions/runs/{run_id}
        match = re.search(r"github\.com/([^/]+)/([^/]+)/actions/runs/(\d+)", url)
        if not match:
            click.echo("Invalid GitHub Action Run URL format. Expected: https://github.com/owner/repo/actions/runs/run_id", err=True)
            sys.exit(1)

        owner, repo, run_id = match.groups()
        repo_name = f"{owner}/{repo}"

        click.echo(f"Fetching logs for Run ID: {run_id} in {repo_name}...", err=True)

        try:
            from .automation_config import AutomationConfig
            from .util.github_action import _create_github_action_log_summary

            # Create a dummy config and failed_checks to pass to _get_github_actions_logs
            config = AutomationConfig()  # Default config
            failed_checks = [{"details_url": url}]

            logs, artifacts = _create_github_action_log_summary(repo_name, config, failed_checks)

            if logs and logs != "No detailed logs available":
                click.echo("--- LLM Summary Start ---")
                click.echo(logs)
                click.echo("--- LLM Summary End ---")
            else:
                click.echo("No logs found.", err=True)
                sys.exit(1)

        except Exception as e:
            click.echo(f"Error fetching logs: {e}", err=True)
            sys.exit(1)
    else:
        click.echo(click.get_current_context().get_help())
