"""Main CLI commands (process_issues, create_feature_issues, fix_to_pass_tests)."""

import os
import re
from typing import Optional

import click

from .automation_config import AutomationConfig
from .automation_engine import AutomationEngine
from .cli_commands_utils import get_github_token_or_fail, get_repo_or_detect
from .cli_helpers import (
    build_backend_manager,
    build_models_map,
    check_backend_prerequisites,
    ensure_test_script_or_fail,
    initialize_graphrag,
    normalize_backends,
)
from .github_client import GitHubClient
from .logger_config import get_logger, setup_logger
from .utils import VERBOSE_ENV_FLAG

logger = get_logger(__name__)


@click.command()
@click.option(
    "--repo",
    help="GitHub repository (owner/repo). If not specified, auto-detects from current Git repository.",
)
@click.option("--github-token", envvar="GITHUB_TOKEN", help="GitHub API token")
@click.option(
    "--backend",
    "backends",
    multiple=True,
    default=("codex",),
    type=click.Choice(["codex", "codex-mcp", "gemini", "qwen", "auggie"]),
    help="AI backend(s) to use in priority order (default: codex)",
)
@click.option(
    "--gemini-api-key",
    envvar="GEMINI_API_KEY",
    help="Gemini API key (optional, used when backend=gemini)",
)
@click.option(
    "--openai-api-key",
    envvar="OPENAI_API_KEY",
    help="OpenAI-style API key (optional, used when backend=qwen)",
)
@click.option(
    "--openai-base-url",
    envvar="OPENAI_BASE_URL",
    help="OpenAI-style Base URL (optional, used when backend=qwen)",
)
@click.option("--model-gemini", help="Model to use when backend=gemini")
@click.option("--model-qwen", help="Model to use when backend=qwen")
@click.option(
    "--model-auggie", help="Model to use when backend=auggie (defaults to GPT-5)"
)
@click.option(
    "--dry-run", is_flag=True, help="Run in dry-run mode without making changes"
)
@click.option(
    "--jules-mode/--no-jules-mode",
    default=True,
    help='Run in jules mode - only add "jules" label to issues without AI analysis (default: on)',
)
@click.option(
    "--skip-main-update/--no-skip-main-update",
    default=True,
    help="When PR checks fail, skip merging the PR base branch into the PR before attempting fixes (default: skip)",
)
@click.option(
    "--ignore-dependabot-prs/--no-ignore-dependabot-prs",
    default=False,
    help="Ignore PRs opened by Dependabot when processing PRs (default: do not ignore)",
)
@click.option(
    "--only",
    "only_target",
    help="Process only a specific issue/PR by URL or number (e.g., https://github.com/owner/repo/issues/123 or 123)",
)
@click.option(
    "--log-level",
    default="INFO",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]),
    help="Set logging level",
)
@click.option("--log-file", help="Log file path (optional)")
@click.option(
    "--verbose", is_flag=True, help="Enable verbose logging and detailed command traces"
)
def process_issues(
    repo: Optional[str],
    github_token: Optional[str],
    backends: tuple[str, ...],
    gemini_api_key: Optional[str],
    openai_api_key: Optional[str],
    openai_base_url: Optional[str],
    model_gemini: Optional[str],
    model_qwen: Optional[str],
    model_auggie: Optional[str],
    dry_run: bool,
    jules_mode: bool,
    skip_main_update: bool,
    ignore_dependabot_prs: bool,
    only_target: Optional[str],
    log_level: str,
    log_file: Optional[str],
    verbose: bool,
) -> None:
    """Process GitHub issues and PRs using AI CLI (codex or gemini)."""

    selected_backends = normalize_backends(backends)
    primary_backend = selected_backends[0]
    models = build_models_map(model_gemini, model_qwen, model_auggie)
    primary_model = models.get(primary_backend)

    # Configure verbose flag and setup logger with specified options
    if verbose:
        os.environ[VERBOSE_ENV_FLAG] = "1"
        effective_log_level = "DEBUG"
    else:
        os.environ.pop(VERBOSE_ENV_FLAG, None)
        effective_log_level = log_level

    setup_logger(log_level=effective_log_level, log_file=log_file)

    # Check prerequisites
    github_token_final = get_github_token_or_fail(github_token)
    check_backend_prerequisites(selected_backends)

    # Get repository name (from parameter or auto-detect)
    repo_name = get_repo_or_detect(repo)

    # Ensure required test script is present (fail early)
    ensure_test_script_or_fail()

    backend_list_str = ", ".join(selected_backends)
    logger.info(f"Processing repository: {repo_name}")
    logger.info(f"Using backends: {backend_list_str} (default: {primary_backend})")
    if primary_backend in ("gemini", "qwen", "auggie"):
        logger.info(f"Using model: {primary_model}")
    logger.info(f"Jules mode: {jules_mode}")
    logger.info(f"Dry run mode: {dry_run}")
    logger.info(f"Log level: {effective_log_level}")
    logger.info(f"Verbose logging: {verbose}")
    logger.info(f"Ignore Dependabot PRs: {ignore_dependabot_prs}")

    # Explicitly show base branch update policy for PR checks failure
    policy_str = (
        "SKIP (default)" if skip_main_update else "ENABLED (--no-skip-main-update)"
    )
    logger.info(f"Base branch update before fixes when PR checks fail: {policy_str}")

    click.echo(f"Processing repository: {repo_name}")
    click.echo(f"Using backends: {backend_list_str} (default: {primary_backend})")
    if primary_backend in ("gemini", "qwen", "auggie"):
        click.echo(f"Using model: {primary_model}")
    click.echo(f"Jules mode: {jules_mode}")
    click.echo(f"Dry run mode: {dry_run}")
    click.echo(f"Main update before fixes when PR checks fail: {policy_str}")
    click.echo(f"Ignore Dependabot PRs: {ignore_dependabot_prs}")
    click.echo(f"Verbose logging: {verbose}")

    # Initialize GraphRAG (always enabled)
    initialize_graphrag()

    # Initialize clients
    github_client = GitHubClient(github_token_final)
    manager = build_backend_manager(
        selected_backends,
        primary_backend,
        models,
        gemini_api_key,
        openai_api_key,
        openai_base_url,
        enable_graphrag=True,  # Always enable GraphRAG
    )

    # Configure engine behavior flags
    engine_config = AutomationConfig()
    engine_config.SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL = bool(skip_main_update)
    engine_config.IGNORE_DEPENDABOT_PRS = bool(ignore_dependabot_prs)

    automation_engine = AutomationEngine(
        github_client, manager, dry_run=dry_run, config=engine_config
    )

    # If only_target is provided, parse and process a single item
    if only_target:
        target_type = "auto"
        number = None
        # If URL, extract number and type
        m = re.search(r"/issues/(\d+)$", only_target)
        if m:
            target_type = "issue"
            number = int(m.group(1))
        else:
            m = re.search(r"/pull/(\d+)$", only_target)
            if m:
                target_type = "pr"
                number = int(m.group(1))
        if number is None:
            # Try plain number
            try:
                number = int(only_target.strip())
                target_type = "auto"
            except ValueError:
                raise click.ClickException("--only must be a PR/Issue URL or a number")
        # Run single-item processing
        _ = automation_engine.process_single(
            repo_name, target_type, number, jules_mode=jules_mode
        )
        # Print brief summary to stdout
        click.echo(f"Processed single {target_type} #{number}")
        # Close MCP session if present
        try:
            manager.close()
        except Exception:
            pass
        return

    # Run automation
    if primary_backend == "gemini" and gemini_api_key is not None:
        automation_engine.run(repo_name)
    else:
        automation_engine.run(repo_name, jules_mode=jules_mode)

    # Close MCP session if present
    try:
        manager.close()
    except Exception:
        pass


@click.command()
@click.option(
    "--repo",
    help="GitHub repository (owner/repo). If not specified, auto-detects from current Git repository.",
)
@click.option("--github-token", envvar="GITHUB_TOKEN", help="GitHub API token")
@click.option(
    "--backend",
    "backends",
    multiple=True,
    default=("codex",),
    type=click.Choice(["codex", "codex-mcp", "gemini", "qwen", "auggie"]),
    help="AI backend(s) to use in priority order (default: codex)",
)
@click.option(
    "--gemini-api-key",
    envvar="GEMINI_API_KEY",
    help="Gemini API key (optional, used when backend=gemini)",
)
@click.option(
    "--openai-api-key",
    envvar="OPENAI_API_KEY",
    help="OpenAI-style API key (optional, used when backend=qwen)",
)
@click.option(
    "--openai-base-url",
    envvar="OPENAI_BASE_URL",
    help="OpenAI-style Base URL (optional, used when backend=qwen)",
)
@click.option("--model-gemini", help="Model to use when backend=gemini")
@click.option("--model-qwen", help="Model to use when backend=qwen")
@click.option(
    "--model-auggie", help="Model to use when backend=auggie (defaults to GPT-5)"
)
@click.option(
    "--log-level",
    default="INFO",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]),
    help="Set logging level",
)
@click.option("--log-file", help="Log file path (optional)")
@click.option(
    "--verbose", is_flag=True, help="Enable verbose logging and detailed command traces"
)
def create_feature_issues(
    repo: Optional[str],
    github_token: Optional[str],
    backends: tuple[str, ...],
    gemini_api_key: Optional[str],
    openai_api_key: Optional[str],
    openai_base_url: Optional[str],
    model_gemini: Optional[str],
    model_qwen: Optional[str],
    model_auggie: Optional[str],
    log_level: str,
    log_file: Optional[str],
    verbose: bool,
) -> None:
    """Analyze repository and create feature enhancement issues."""

    selected_backends = normalize_backends(backends)
    primary_backend = selected_backends[0]
    models = build_models_map(model_gemini, model_qwen, model_auggie)
    primary_model = models.get(primary_backend)

    # Configure verbose flag and setup logger with specified options
    if verbose:
        os.environ[VERBOSE_ENV_FLAG] = "1"
        effective_log_level = "DEBUG"
    else:
        os.environ.pop(VERBOSE_ENV_FLAG, None)
        effective_log_level = log_level

    setup_logger(log_level=effective_log_level, log_file=log_file)

    # Check prerequisites
    github_token_final = get_github_token_or_fail(github_token)
    check_backend_prerequisites(selected_backends)

    # Get repository name (from parameter or auto-detect)
    repo_name = get_repo_or_detect(repo)

    backend_list_str = ", ".join(selected_backends)
    logger.info(f"Analyzing repository for feature opportunities: {repo_name}")
    logger.info(f"Using backends: {backend_list_str} (default: {primary_backend})")
    if primary_backend in ("gemini", "qwen", "auggie"):
        logger.info(f"Using model: {primary_model}")
    logger.info(f"Log level: {effective_log_level}")
    logger.info(f"Verbose logging: {verbose}")

    click.echo(f"Analyzing repository for feature opportunities: {repo_name}")
    click.echo(f"Using backends: {backend_list_str} (default: {primary_backend})")
    if primary_backend in ("gemini", "qwen", "auggie"):
        click.echo(f"Using model: {primary_model}")
    click.echo(f"Verbose logging: {verbose}")

    # Initialize GraphRAG (always enabled)
    initialize_graphrag()

    # Initialize clients
    github_client = GitHubClient(github_token_final)
    manager = build_backend_manager(
        selected_backends,
        primary_backend,
        models,
        gemini_api_key,
        openai_api_key,
        openai_base_url,
        enable_graphrag=True,  # Always enable GraphRAG
    )

    automation_engine = AutomationEngine(github_client, manager)

    # Analyze and create feature issues
    automation_engine.create_feature_issues(repo_name)

    # Close MCP session if present
    try:
        manager.close()
    except Exception:
        pass


@click.command(name="fix-to-pass-tests")
@click.option(
    "--backend",
    "backends",
    multiple=True,
    default=("codex",),
    type=click.Choice(["codex", "codex-mcp", "gemini", "qwen", "auggie"]),
    help="AI backend(s) to use in priority order (default: codex)",
)
@click.option(
    "--gemini-api-key",
    envvar="GEMINI_API_KEY",
    help="Gemini API key (optional, used when backend=gemini)",
)
@click.option(
    "--openai-api-key",
    envvar="OPENAI_API_KEY",
    help="OpenAI-style API key (optional, used when backend=qwen)",
)
@click.option(
    "--openai-base-url",
    envvar="OPENAI_BASE_URL",
    help="OpenAI-style Base URL (optional, used when backend=qwen)",
)
@click.option("--model-gemini", help="Model to use when backend=gemini")
@click.option("--model-qwen", help="Model to use when backend=qwen")
@click.option(
    "--model-auggie", help="Model to use when backend=auggie (defaults to GPT-5)"
)
@click.option(
    "--max-attempts",
    type=int,
    default=None,
    help="Maximum fix attempts before giving up (defaults to engine config)",
)
@click.option(
    "--dry-run", is_flag=True, help="Run without making changes (LLM edits simulated)"
)
@click.option(
    "--log-level",
    default="INFO",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]),
    help="Set logging level",
)
@click.option("--log-file", help="Log file path (optional)")
@click.option(
    "--verbose", is_flag=True, help="Enable verbose logging and detailed command traces"
)
def fix_to_pass_tests_command(
    backends: tuple[str, ...],
    gemini_api_key: Optional[str],
    openai_api_key: Optional[str],
    openai_base_url: Optional[str],
    model_gemini: Optional[str],
    model_qwen: Optional[str],
    model_auggie: Optional[str],
    max_attempts: Optional[int],
    dry_run: bool,
    log_level: str,
    log_file: Optional[str],
    verbose: bool,
) -> None:
    """Run local tests and repeatedly request LLM fixes until tests pass.

    If the LLM makes no edits in an iteration, error and stop.
    """
    selected_backends = normalize_backends(backends)
    primary_backend = selected_backends[0]
    models = build_models_map(model_gemini, model_qwen, model_auggie)
    primary_model = models.get(primary_backend)

    if verbose:
        os.environ[VERBOSE_ENV_FLAG] = "1"
        effective_log_level = "DEBUG"
    else:
        os.environ.pop(VERBOSE_ENV_FLAG, None)
        effective_log_level = log_level

    setup_logger(log_level=effective_log_level, log_file=log_file)

    # Ensure required test script is present (fail early)
    ensure_test_script_or_fail()

    # Check backend CLI availability
    check_backend_prerequisites(selected_backends)

    backend_list_str = ", ".join(selected_backends)
    click.echo(f"Using backends: {backend_list_str} (default: {primary_backend})")
    if primary_backend in ("gemini", "qwen", "auggie"):
        click.echo(f"Using model: {primary_model}")
    click.echo(f"Dry run mode: {dry_run}")
    click.echo(f"Verbose logging: {verbose}")

    # Initialize GraphRAG (always enabled)
    initialize_graphrag()

    # Initialize minimal clients (GitHub not used here, but engine expects a client)
    try:
        from .github_client import GitHubClient as _GH

        github_client = _GH("")
    except Exception:
        # Fallback to a minimal stand-in (never used)
        class _Dummy:
            token = ""

        github_client = _Dummy()  # type: ignore

    manager = build_backend_manager(
        selected_backends,
        primary_backend,
        models,
        gemini_api_key,
        openai_api_key,
        openai_base_url,
        enable_graphrag=True,  # Always enable GraphRAG
    )

    engine = AutomationEngine(github_client, manager, dry_run=dry_run)

    try:
        result = engine.fix_to_pass_tests(max_attempts=max_attempts)
        if result.get("success"):
            click.echo(f"✅ Tests passed in {result.get('attempts')} attempt(s)")
        else:
            click.echo("❌ Tests still failing after attempts")
            raise click.ClickException("Tests did not pass within the attempt limit")
    except RuntimeError as e:
        # Specific error when LLM made no edits
        raise click.ClickException(str(e))
    finally:
        # Close underlying sessions if present
        try:
            manager.close()
        except Exception:
            pass

