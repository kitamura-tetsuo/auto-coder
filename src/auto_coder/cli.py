"""
Command Line Interface for Auto-Coder.
"""

import click
from typing import Optional
import os
from dotenv import load_dotenv

from .github_client import GitHubClient
from .gemini_client import GeminiClient
from .codex_client import CodexClient
from .automation_engine import AutomationEngine, AutomationConfig
from .git_utils import get_current_repo_name, is_git_repository
from .auth_utils import get_github_token, get_gemini_api_key, get_auth_status
from .logger_config import setup_logger, get_logger

# Load environment variables
load_dotenv()

# Initialize logger
logger = get_logger(__name__)


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
        raise click.ClickException(
            "Not in a Git repository. Please specify --repo option or run from within a Git repository."
        )
    else:
        raise click.ClickException(
            "Could not auto-detect GitHub repository. Please specify --repo option."
        )


def get_github_token_or_fail(provided_token: Optional[str]) -> str:
    """Get GitHub token from parameter or auto-detect from gh CLI."""
    if provided_token:
        return provided_token

    # Try to auto-detect token
    detected_token = get_github_token()
    if detected_token:
        click.echo("Using GitHub token from gh CLI authentication")
        return detected_token

    # Show helpful error with authentication instructions
    raise click.ClickException(
        "GitHub token is required. Please either:\n"
        "1. Set GITHUB_TOKEN environment variable, or\n"
        "2. Login with gh CLI: 'gh auth login', or\n"
        "3. Use --github-token option"
    )


def check_gemini_cli_or_fail() -> None:
    """Check if gemini CLI is available and working."""
    try:
        import subprocess
        result = subprocess.run(
            ['gemini', '--version'],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            click.echo("Using gemini CLI")
            return
    except Exception:
        pass

    # Show helpful error with installation instructions
    raise click.ClickException(
        "Gemini CLI is required. Please install it from:\n"
        "https://github.com/google-gemini/gemini-cli\n"
        "Or use: npm install -g @google/generative-ai-cli"
    )


def check_codex_cli_or_fail() -> None:
    """Check if codex CLI is available and working."""
    try:
        import subprocess
        result = subprocess.run(
            ['codex', '--version'],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            click.echo("Using codex CLI")
            return
    except Exception:
        pass

    raise click.ClickException(
        "Codex CLI is required. Please install it from:\n"
        "https://github.com/openai/codex"
    )


@click.group()
@click.version_option(version="0.1.0", package_name="auto-coder")
def main() -> None:
    """Auto-Coder: Automated application development using Gemini CLI and GitHub integration."""
    pass


@main.command()
@click.option('--repo', help='GitHub repository (owner/repo). If not specified, auto-detects from current Git repository.')
@click.option('--github-token', envvar='GITHUB_TOKEN', help='GitHub API token')
@click.option('--backend', default='codex', type=click.Choice(['codex', 'gemini']), help='AI backend to use (default: codex)')
@click.option('--gemini-api-key', envvar='GEMINI_API_KEY', help='Gemini API key (optional, used when backend=gemini)')
@click.option('--model', default='gemini-2.5-pro', help='Model to use (Gemini only; ignored when backend=codex)')
@click.option('--dry-run', is_flag=True, help='Run in dry-run mode without making changes')
@click.option('--jules-mode/--no-jules-mode', default=True, help='Run in jules mode - only add "jules" label to issues without AI analysis (default: on)')
@click.option('--skip-main-update/--no-skip-main-update', default=True, help='When PR checks fail, skip merging main into the PR before attempting fixes (default: skip)')
@click.option('--log-level', default='INFO', type=click.Choice(['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']), help='Set logging level')
@click.option('--log-file', help='Log file path (optional)')
def process_issues(
    repo: Optional[str],
    github_token: Optional[str],
    backend: str,
    gemini_api_key: Optional[str],
    model: str,
    dry_run: bool,
    jules_mode: bool,
    skip_main_update: bool,
    log_level: str,
    log_file: Optional[str]
) -> None:
    """Process GitHub issues and PRs using AI CLI (codex or gemini)."""
    # Setup logger with specified options
    setup_logger(log_level=log_level, log_file=log_file)

    # Check prerequisites
    github_token_final = get_github_token_or_fail(github_token)
    if backend == 'codex':
        check_codex_cli_or_fail()
    else:
        check_gemini_cli_or_fail()

    # Get repository name (from parameter or auto-detect)
    repo_name = get_repo_or_detect(repo)

    # Warn if model is specified but backend is codex (model will be ignored)
    try:
        ctx = click.get_current_context(silent=True)
        if ctx is not None and backend == 'codex':
            source = ctx.get_parameter_source('model')
            if source in (click.core.ParameterSource.COMMANDLINE, click.core.ParameterSource.ENVIRONMENT, click.core.ParameterSource.PROMPT):
                logger.warning("--model is ignored when backend=codex")
                click.echo("Warning: --model is ignored when backend=codex")
    except Exception:
        # Non-fatal: proceed without warning if context/source not available
        pass

    logger.info(f"Processing repository: {repo_name}")
    logger.info(f"Using backend: {backend}")
    if backend == 'gemini':
        logger.info(f"Using model: {model}")
    logger.info(f"Jules mode: {jules_mode}")
    logger.info(f"Dry run mode: {dry_run}")
    logger.info(f"Log level: {log_level}")

    # Explicitly show main update policy for PR checks failure
    policy_str = "SKIP (default)" if skip_main_update else "ENABLED (--no-skip-main-update)"
    logger.info(f"Main update before fixes when PR checks fail: {policy_str}")

    click.echo(f"Processing repository: {repo_name}")
    click.echo(f"Using backend: {backend}")
    if backend == 'gemini':
        click.echo(f"Using model: {model}")
    click.echo(f"Jules mode: {jules_mode}")
    click.echo(f"Dry run mode: {dry_run}")
    click.echo(f"Main update before fixes when PR checks fail: {policy_str}")

    # Initialize clients
    github_client = GitHubClient(github_token_final)
    ai_client = None
    if backend == 'gemini':
        ai_client = GeminiClient(gemini_api_key, model_name=model) if gemini_api_key else GeminiClient(model_name=model)
    else:
        ai_client = CodexClient(model_name='codex')

    # Configure engine behavior flags
    engine_config = AutomationConfig()
    engine_config.SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL = bool(skip_main_update)

    automation_engine = AutomationEngine(github_client, ai_client, dry_run=dry_run, config=engine_config)

    # Run automation
    if backend == 'gemini' and gemini_api_key is not None:
        automation_engine.run(repo_name)
    else:
        automation_engine.run(repo_name, jules_mode=jules_mode)


@main.command()
@click.option('--repo', help='GitHub repository (owner/repo). If not specified, auto-detects from current Git repository.')
@click.option('--github-token', envvar='GITHUB_TOKEN', help='GitHub API token')
@click.option('--backend', default='codex', type=click.Choice(['codex', 'gemini']), help='AI backend to use (default: codex)')
@click.option('--gemini-api-key', envvar='GEMINI_API_KEY', help='Gemini API key (optional, used when backend=gemini)')
@click.option('--model', default='gemini-2.5-pro', help='Model to use (Gemini only; ignored when backend=codex)')
@click.option('--log-level', default='INFO', type=click.Choice(['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']), help='Set logging level')
@click.option('--log-file', help='Log file path (optional)')
def create_feature_issues(
    repo: Optional[str],
    github_token: Optional[str],
    backend: str,
    gemini_api_key: Optional[str],
    model: str,
    log_level: str,
    log_file: Optional[str]
) -> None:
    """Analyze repository and create feature enhancement issues."""
    # Setup logger with specified options
    setup_logger(log_level=log_level, log_file=log_file)

    # Check prerequisites
    github_token_final = get_github_token_or_fail(github_token)
    if backend == 'codex':
        check_codex_cli_or_fail()
    else:
        check_gemini_cli_or_fail()

    # Get repository name (from parameter or auto-detect)
    repo_name = get_repo_or_detect(repo)

    # Warn if model is specified but backend is codex (model will be ignored)
    try:
        ctx = click.get_current_context(silent=True)
        if ctx is not None and backend == 'codex':
            source = ctx.get_parameter_source('model')
            if source in (click.core.ParameterSource.COMMANDLINE, click.core.ParameterSource.ENVIRONMENT, click.core.ParameterSource.PROMPT):
                logger.warning("--model is ignored when backend=codex")
                click.echo("Warning: --model is ignored when backend=codex")
    except Exception:
        pass

    logger.info(f"Analyzing repository for feature opportunities: {repo_name}")
    logger.info(f"Using backend: {backend}")
    if backend == 'gemini':
        logger.info(f"Using model: {model}")
    logger.info(f"Log level: {log_level}")

    click.echo(f"Analyzing repository for feature opportunities: {repo_name}")
    click.echo(f"Using backend: {backend}")
    if backend == 'gemini':
        click.echo(f"Using model: {model}")

    # Initialize clients
    github_client = GitHubClient(github_token_final)
    if backend == 'gemini':
        ai_client = GeminiClient(gemini_api_key, model_name=model) if gemini_api_key else GeminiClient(model_name=model)
    else:
        ai_client = CodexClient(model_name='codex')
    automation_engine = AutomationEngine(github_client, ai_client)

    # Analyze and create feature issues
    automation_engine.create_feature_issues(repo_name)


@main.command()
def auth_status() -> None:
    """Check authentication status for GitHub and Gemini."""
    click.echo("Checking authentication status...")
    click.echo()

    status = get_auth_status()

    # GitHub status
    github_status = status['github']
    click.echo("🐙 GitHub:")
    if github_status['token_available']:
        click.echo("  ✅ Token available")
        if github_status['authenticated']:
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
        result = subprocess.run(
            ['gemini', '--version'],
            capture_output=True,
            text=True,
            timeout=10
        )
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

    # Repository detection
    repo_name = get_current_repo_name()
    if repo_name:
        click.echo(f"📁 Repository: {repo_name} (auto-detected)")
    else:
        if is_git_repository():
            click.echo("📁 Repository: Git repository detected but not GitHub")
        else:
            click.echo("📁 Repository: Not in a Git repository")


if __name__ == '__main__':
    main()
