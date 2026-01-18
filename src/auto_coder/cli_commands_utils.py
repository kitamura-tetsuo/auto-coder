"""Utility CLI commands (auth_status, get_actions_logs)."""

import sys
from typing import Optional

import click

from .auth_utils import get_auth_status, get_github_token
from .automation_config import AutomationConfig
from .automation_engine import AutomationEngine
from .git_utils import get_current_repo_name, is_git_repository, migrate_pr_branches
from .logger_config import setup_logger
from .util.gh_cache import GitHubClient
from .util.github_action import get_github_actions_logs_from_url


def get_github_token_or_fail(provided_token: Optional[str]) -> str:
    """Get GitHub token from parameter or auto-detect from gh CLI.

    Note: Do not print to stdout to avoid polluting CLI outputs that pipe to files.
    """
    from .logger_config import get_logger

    logger = get_logger(__name__)

    if provided_token:
        return provided_token

    # Try to auto-detect token
    detected_token = get_github_token()
    if detected_token:
        # Use logger instead of stdout to avoid prelude noise in CLI outputs
        logger.info("Using GitHub token from gh CLI authentication")
        return detected_token

    # Show helpful error with authentication instructions
    raise click.ClickException("GitHub token is required. Please either:\n" "1. Set GITHUB_TOKEN environment variable, or\n" "2. Login with gh CLI: 'gh auth login', or\n" "3. Use --github-token option")


def get_repo_or_detect(repo: Optional[str]) -> str:
    """Get repository name from parameter or auto-detect from current directory."""
    if repo:
        return repo

    # Try to auto-detect repository
    detected_repo = get_current_repo_name()
    if detected_repo:
        click.echo(f"Auto-detected repository: {detected_repo}")
        return detected_repo

    # If not in a git repository or can't detect, show helpful error
    if not is_git_repository():
        raise click.ClickException("Not in a Git repository. Please specify --repo option or run from within a Git repository.")
    else:
        raise click.ClickException("Could not auto-detect GitHub repository. Please specify --repo option.")


@click.command()
@click.option("--url", "actions_url", required=True, help="GitHub Actions job URL")
@click.option("--github-token", envvar="GITHUB_TOKEN", help="GitHub API token")
def get_actions_logs(actions_url: str, github_token: Optional[str]) -> None:
    """Fetch error logs from a GitHub Actions job URL for debugging."""
    # Route log output to stderr to avoid polluting stdout which is piped to file
    setup_logger(stream=sys.stderr)
    github_token_final = get_github_token_or_fail(github_token)
    # Initialize GitHubClient with the token so get_github_actions_logs_from_url can access it
    GitHubClient.get_instance(token=github_token_final)
    logs = get_github_actions_logs_from_url(actions_url)
    click.echo(logs)


@click.command()
def auth_status() -> None:
    """Check authentication status for GitHub and Gemini."""
    click.echo("Checking authentication status...")
    click.echo()

    status = get_auth_status()

    # GitHub status
    github_status = status["github"]
    click.echo("🐙 GitHub:")
    if github_status["token_available"]:
        click.echo("  ✅ Token available")
        if github_status["authenticated"]:
            click.echo("  ✅ gh CLI authenticated")
        else:
            click.echo("  ⚠️  gh CLI not authenticated (but token available)")
    else:
        click.echo("  ❌ No token found")
        click.echo("     Please run 'gh auth login' or set GITHUB_TOKEN")

    click.echo()

    # Gemini CLI status
    click.echo("🤖 Gemini CLI:")
    try:
        import subprocess

        result = subprocess.run(["gemini", "--version"], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            click.echo("  ✅ gemini CLI available")
            version_info = result.stdout.strip()
            if version_info:
                click.echo(f"     Version: {version_info}")
        else:
            click.echo("  ❌ gemini CLI not working")
    except Exception:
        click.echo("  ❌ gemini CLI not found")
        click.echo("     Please install from: https://github.com/google-gemini/gemini-cli")

    click.echo()

    # Qwen Code CLI status
    click.echo("🤖 Qwen Code CLI:")
    try:
        import subprocess as _sp

        res = _sp.run(["qwen", "--version"], capture_output=True, text=True, timeout=10)
        if res.returncode == 0:
            click.echo("  ✅ qwen CLI available")
            ver = (res.stdout or "").strip()
            if ver:
                click.echo(f"     Version: {ver}")
        else:
            click.echo("  ❌ qwen CLI not working")
    except Exception:
        click.echo("  ❌ qwen CLI not found")
        click.echo("     Please install from: https://github.com/QwenLM/qwen-code")

    click.echo()

    # Auggie CLI status
    click.echo("🤖 Auggie CLI:")
    try:
        import subprocess as _sp

        res = _sp.run(["auggie", "--version"], capture_output=True, text=True, timeout=10)
        if res.returncode == 0:
            click.echo("  ✅ auggie CLI available")
            ver = (res.stdout or "").strip()
            if ver:
                click.echo(f"     Version: {ver}")
        else:
            click.echo("  ❌ auggie CLI not working")
    except Exception:
        click.echo("  ❌ auggie CLI not found")
        click.echo("     Please install via: npm install -g @augmentcode/auggie")

    click.echo()

    # Repository detection
    repo_name = get_current_repo_name()
    if repo_name:
        click.echo(f"📁 Repository: {repo_name} (auto-detected)")
    else:
        if is_git_repository():
            click.echo("📁 Repository: Git repository detected but not GitHub")
        else:
            click.echo("📁 Repository: Not in a Git repository")


@click.command()
@click.option(
    "--execute",
    is_flag=True,
    help="Actually perform the migration (default: False, use --execute to run)",
)
@click.option(
    "--no-delete",
    is_flag=True,
    default=False,
    help="Do not delete pr-<number> branches after successful migration",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Auto-resolve merge conflicts (use with caution)",
)
def migrate_branches(
    execute: bool,
    no_delete: bool,
    force: bool,
) -> None:
    """Migrate existing pr-<number> branches to their corresponding issue-<number> branches.

    This command will:
    1. Scan for all branches matching the pr-<number> pattern
    2. For each pr-xx branch, check if an issue-xx branch exists
    3. If issue-xx exists, merge pr-xx into issue-xx
    4. If issue-xx doesn't exist, create it from pr-xx
    5. Delete the pr-xx branch after successful merge (unless --no-delete is used)

    Examples:
        # Preview what would be migrated
        auto-coder migrate-branches

        # Execute the migration
        auto-coder migrate-branches --execute

        # Execute without deleting pr-* branches
        auto-coder migrate-branches --execute --no-delete

        # Auto-resolve merge conflicts
        auto-coder migrate-branches --execute --force
    """
    # Setup logger to show detailed output
    setup_logger(stream=sys.stderr)

    # Check if we're in a git repository
    if not is_git_repository():
        raise click.ClickException("Not in a Git repository. Please run from within a Git repository.")

    # Create config
    config = AutomationConfig()

    # Perform the migration
    results = migrate_pr_branches(
        config,  # type: ignore[arg-type]
        delete_after_merge=not no_delete,
        force=force,
        execute=execute,
    )

    # Display results
    click.echo()
    click.echo("=" * 80)
    click.echo("MIGRATION RESULTS")
    click.echo("=" * 80)

    if results["migrated"]:
        click.echo(f"\n✅ Successfully migrated: {len(results['migrated'])}")
        for item in results["migrated"]:
            click.echo(f"  - {item['from']} -> {item['to']}")

    if results["skipped"]:
        click.echo(f"\n⚠️  Skipped: {len(results['skipped'])}")
        for item in results["skipped"]:
            reason = item.get("reason", item.get("error", "Unknown reason"))
            click.echo(f"  - {item.get('branch', item.get('from', 'Unknown'))}: {reason}")

    if results["conflicts"]:
        click.echo(f"\n❌ Merge conflicts detected: {len(results['conflicts'])}")
        for item in results["conflicts"]:
            click.echo(f"  - {item['from']} -> {item['to']}: {item['error']}")

    if results["failed"]:
        click.echo(f"\n❌ Failed: {len(results['failed'])}")
        for item in results["failed"]:
            click.echo(f"  - {item['from']} -> {item['to']}: {item['error']}")

    click.echo()
    click.echo("=" * 80)

    # Provide guidance based on results
    if results["conflicts"] or results["failed"]:
        click.echo("\n⚠️  Some issues occurred during migration.")
        if results["conflicts"]:
            click.echo("   Use --force flag to auto-resolve merge conflicts, or resolve manually.")
        if results["failed"]:
            click.echo("   Please check the error messages above and resolve manually.")
    else:
        click.echo("✅ Migration completed successfully!")

    # Exit with non-zero code if there were failures
    if results["failed"] or results["conflicts"]:
        sys.exit(1)
