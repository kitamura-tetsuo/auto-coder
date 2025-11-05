"""Main CLI commands (process_issues, create_feature_issues, fix_to_pass_tests)."""

import os
import re
from pathlib import Path
from typing import Optional

import click

from .automation_config import AutomationConfig
from .automation_engine import AutomationEngine
from .cli_commands_utils import get_github_token_or_fail, get_repo_or_detect
from .cli_helpers import build_backend_manager, build_models_map, check_backend_prerequisites, check_github_sub_issue_or_setup, check_graphrag_mcp_for_backends, ensure_test_script_or_fail, initialize_graphrag, normalize_backends
from .git_utils import extract_number_from_branch, get_current_branch
from .github_client import GitHubClient
from .logger_config import get_logger, setup_logger
from .progress_footer import setup_progress_footer_logging
from .utils import VERBOSE_ENV_FLAG, CommandExecutor

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
    type=click.Choice(["codex", "codex-mcp", "gemini", "qwen", "auggie", "claude"]),
    help="AI backend(s) to use in priority order (default: codex)",
)
@click.option(
    "--message-backend",
    "message_backends",
    multiple=True,
    default=None,
    type=click.Choice(["codex", "codex-mcp", "gemini", "qwen", "auggie", "claude"]),
    help="AI backend(s) for message generation in priority order (default: same as --backend)",
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
@click.option(
    "--qwen-use-env-vars/--qwen-use-cli-options",
    default=False,
    help="Pass Qwen credentials via environment variables or CLI options (default)",
)
@click.option(
    "--qwen-preserve-env/--qwen-clear-env",
    default=False,
    help="Preserve existing OPENAI_* environment variables (default: clear before setting)",
)
@click.option("--model-gemini", help="Model to use when backend=gemini")
@click.option("--model-qwen", help="Model to use when backend=qwen")
@click.option("--model-auggie", help="Model to use when backend=auggie (defaults to GPT-5)")
@click.option("--model-claude", help="Model to use when backend=claude (defaults to sonnet)")
@click.option("--dry-run", is_flag=True, help="Run in dry-run mode without making changes")
@click.option(
    "--jules-mode/--no-jules-mode",
    default=True,
    help='Run in jules mode - only add "jules" label to issues without AI analysis (default: on)',
)
@click.option(
    "--disable-labels/--no-disable-labels",
    default=None,
    help="Disable GitHub label operations (@auto-coder label). Auto-detected: disabled in debugger, enabled otherwise",
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
    "--force-clean-before-checkout/--no-force-clean-before-checkout",
    default=False,
    help="Force clean workspace (git reset --hard + git clean -fd) before PR checkout (default: do not force clean)",
)
@click.option(
    "--enable-graphrag/--disable-graphrag",
    default=True,
    help="Enable GraphRAG integration (default: enabled)",
)
@click.option(
    "--only",
    "only_target",
    help="Process only a specific issue/PR by URL or number (e.g., https://github.com/owner/repo/issues/123 or 123)",
)
@click.option(
    "--force-reindex",
    is_flag=True,
    default=False,
    help="Force GraphRAG code analysis reindexing even if index is up to date (default: false)",
)
@click.option(
    "--log-level",
    default="INFO",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]),
    help="Set logging level",
)
@click.option("--log-file", help="Log file path (optional)")
@click.option("--verbose", is_flag=True, help="Enable verbose logging and detailed command traces")
def process_issues(
    repo: Optional[str],
    github_token: Optional[str],
    backends: tuple[str, ...],
    message_backends: Optional[tuple[str, ...]],
    gemini_api_key: Optional[str],
    openai_api_key: Optional[str],
    openai_base_url: Optional[str],
    qwen_use_env_vars: bool,
    qwen_preserve_env: bool,
    model_gemini: Optional[str],
    model_qwen: Optional[str],
    model_auggie: Optional[str],
    model_claude: Optional[str],
    dry_run: bool,
    jules_mode: bool,
    disable_labels: Optional[bool],
    skip_main_update: bool,
    ignore_dependabot_prs: bool,
    force_clean_before_checkout: bool,
    only_target: Optional[str],
    enable_graphrag: bool,
    force_reindex: bool,
    log_level: str,
    log_file: Optional[str],
    verbose: bool,
) -> None:
    """Process GitHub issues and PRs using AI CLI (codex or gemini)."""

    selected_backends = normalize_backends(backends)
    primary_backend = selected_backends[0]
    models = build_models_map(model_gemini, model_qwen, model_auggie, model_claude)
    primary_model = models.get(primary_backend)

    # Configure verbose flag and setup logger with specified options
    if verbose:
        os.environ[VERBOSE_ENV_FLAG] = "1"
        effective_log_level = "DEBUG"
    else:
        os.environ.pop(VERBOSE_ENV_FLAG, None)
        effective_log_level = log_level

    setup_logger(log_level=effective_log_level, log_file=log_file)

    # Setup progress footer logging (re-configures logger with footer sink)
    setup_progress_footer_logging()

    # Check prerequisites
    github_token_final = get_github_token_or_fail(github_token)
    check_backend_prerequisites(selected_backends)

    # Get repository name (from parameter or auto-detect)
    repo_name = get_repo_or_detect(repo)

    # Ensure required test script is present (fail early)
    ensure_test_script_or_fail()

    # Auto-detect disable_labels if not explicitly set
    if disable_labels is None:
        # Disable labels when running in debugger, enable otherwise
        disable_labels = CommandExecutor.is_running_in_debugger()
        logger.debug(f"Auto-detected disable_labels={disable_labels} (debugger={disable_labels})")

    backend_list_str = ", ".join(selected_backends)
    logger.info(f"Processing repository: {repo_name}")
    logger.info(f"Using backends: {backend_list_str} (default: {primary_backend})")
    if primary_backend in ("gemini", "qwen", "auggie", "claude"):
        logger.info(f"Using model: {primary_model}")
    logger.info(f"Jules mode: {jules_mode}")
    logger.info(f"Dry run mode: {dry_run}")
    logger.info(f"Disable labels: {disable_labels}")
    logger.info(f"Log level: {effective_log_level}")
    logger.info(f"Verbose logging: {verbose}")
    logger.info(f"Ignore Dependabot PRs: {ignore_dependabot_prs}")
    logger.info(f"Force clean before checkout: {force_clean_before_checkout}")

    # Explicitly show base branch update policy for PR checks failure
    policy_str = "SKIP (default)" if skip_main_update else "ENABLED (--no-skip-main-update)"
    logger.info(f"Base branch update before fixes when PR checks fail: {policy_str}")

    click.echo(f"Processing repository: {repo_name}")
    click.echo(f"Using backends: {backend_list_str} (default: {primary_backend})")
    if primary_backend in ("gemini", "qwen", "auggie", "claude"):
        click.echo(f"Using model: {primary_model}")
    click.echo(f"Jules mode: {jules_mode}")
    click.echo(f"Dry run mode: {dry_run}")
    click.echo(f"Disable labels: {disable_labels}")
    click.echo(f"Main update before fixes when PR checks fail: {policy_str}")
    click.echo(f"Ignore Dependabot PRs: {ignore_dependabot_prs}")
    click.echo(f"Force clean before checkout: {force_clean_before_checkout}")
    click.echo(f"Force reindex: {force_reindex}")
    click.echo(f"Verbose logging: {verbose}")

    # Initialize GraphRAG (conditionally enabled)
    if enable_graphrag:
        initialize_graphrag(force_reindex=force_reindex)

    # Initialize clients
    github_client = GitHubClient(github_token_final, disable_labels=disable_labels)
    # Use global LLMBackendManager for main backend
    from auto_coder.backend_manager import get_llm_backend_manager

    from .cli_helpers import build_backend_manager

    # First create a temporary manager to get the configuration
    temp_manager = build_backend_manager(
        selected_backends,
        primary_backend,
        models,
        gemini_api_key,
        openai_api_key,
        openai_base_url,
        qwen_use_env_vars=qwen_use_env_vars,
        qwen_preserve_env=qwen_preserve_env,
        enable_graphrag=enable_graphrag,
    )

    # Initialize the global singleton with proper configuration

    manager = get_llm_backend_manager(
        default_backend=primary_backend,
        default_client=temp_manager._clients[primary_backend],
        factories=temp_manager._factories,
        order=selected_backends,
        force_reinitialize=True,
    )

    # Check GraphRAG MCP configuration for selected backends using client
    check_graphrag_mcp_for_backends(selected_backends, client=manager)

    # Initialize message backend manager (use same backends if not specified)
    from .cli_helpers import build_message_backend_manager

    message_backend_list = normalize_backends(message_backends) if message_backends else selected_backends
    message_primary_backend = message_backend_list[0]
    message_manager = build_message_backend_manager(
        message_backend_list,
        message_primary_backend,
        models,
        gemini_api_key,
        openai_api_key,
        openai_base_url,
        qwen_use_env_vars=qwen_use_env_vars,
        qwen_preserve_env=qwen_preserve_env,
    )

    if message_backends:
        message_backend_str = ", ".join(message_backend_list)
        logger.info(f"Using message backends: {message_backend_str} (default: {message_primary_backend})")
        click.echo(f"Using message backends: {message_backend_str} (default: {message_primary_backend})")

    # Configure engine behavior flags
    engine_config = AutomationConfig()
    engine_config.SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL = bool(skip_main_update)
    engine_config.IGNORE_DEPENDABOT_PRS = bool(ignore_dependabot_prs)
    engine_config.DISABLE_LABELS = bool(disable_labels)
    engine_config.FORCE_CLEAN_BEFORE_CHECKOUT = bool(force_clean_before_checkout)

    automation_engine = AutomationEngine(
        github_client,
        dry_run=dry_run,
        config=engine_config,
    )

    # Check if we should resume work on current branch
    # (only if not on main branch and no --only target specified)
    if not only_target:
        current_branch = get_current_branch()
        if current_branch and current_branch != engine_config.MAIN_BRANCH:
            logger.info(f"Detected work in progress on branch '{current_branch}'")

            # Search for an open PR with this head branch
            target_type = None
            target_data = None
            number = None

            # Try to find PR by head branch
            try:
                pr_data = github_client.find_pr_by_head_branch(repo_name, current_branch)
                if pr_data:
                    target_type = "pr"
                    target_data = pr_data
                    number = pr_data.get("number")
                    logger.info(f"Found open PR #{number} for current branch")
            except Exception as e:
                logger.debug(f"No open PR found for branch '{current_branch}': {e}")

            # If no PR found, try to extract number from branch name and look for issue
            if not target_type:
                number = extract_number_from_branch(current_branch)
                if number:
                    try:
                        issue_data = github_client.get_issue_details_by_number(repo_name, number)
                        if issue_data and issue_data.get("state") == "open":
                            target_type = "issue"
                            target_data = issue_data
                            logger.info(f"Found open issue #{number} for current branch")
                    except Exception as e:
                        logger.debug(f"No open issue found for #{number}: {e}")

            # If we found an open PR or issue, process it
            if target_type and target_data and number:
                click.echo(f"Resuming work on {target_type} #{number} (branch: {current_branch})")
                logger.info(f"Resuming work on {target_type} #{number}")

                # Run single-item processing
                _ = automation_engine.process_single(repo_name, target_type, number, jules_mode=jules_mode)
                # Print brief summary to stdout
                click.echo(f"Processed {target_type} #{number}")
                # Close MCP session if present
                try:
                    manager.close()
                    message_manager.close()
                except Exception:
                    pass
                return
            else:
                logger.info(f"No open PR or issue found for branch '{current_branch}', proceeding with normal processing")

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
        _ = automation_engine.process_single(repo_name, target_type, number, jules_mode=jules_mode)
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
    type=click.Choice(["codex", "codex-mcp", "gemini", "qwen", "auggie", "claude"]),
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
@click.option(
    "--qwen-use-env-vars/--qwen-use-cli-options",
    default=True,
    help="Pass Qwen credentials via environment variables (default) or CLI options",
)
@click.option(
    "--qwen-preserve-env/--qwen-clear-env",
    default=False,
    help="Preserve existing OPENAI_* environment variables (default: clear before setting)",
)
@click.option("--model-gemini", help="Model to use when backend=gemini")
@click.option("--model-qwen", help="Model to use when backend=qwen")
@click.option("--model-auggie", help="Model to use when backend=auggie (defaults to GPT-5)")
@click.option("--model-claude", help="Model to use when backend=claude (defaults to sonnet)")
@click.option(
    "--enable-graphrag/--disable-graphrag",
    default=True,
    help="Enable GraphRAG integration (default: enabled)",
)
@click.option(
    "--force-reindex",
    is_flag=True,
    default=False,
    help="Force GraphRAG code analysis reindexing even if index is up to date (default: false)",
)
@click.option(
    "--log-level",
    default="INFO",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]),
    help="Set logging level",
)
@click.option("--log-file", help="Log file path (optional)")
@click.option("--verbose", is_flag=True, help="Enable verbose logging and detailed command traces")
def create_feature_issues(
    repo: Optional[str],
    github_token: Optional[str],
    backends: tuple[str, ...],
    gemini_api_key: Optional[str],
    openai_api_key: Optional[str],
    openai_base_url: Optional[str],
    qwen_use_env_vars: bool,
    qwen_preserve_env: bool,
    model_gemini: Optional[str],
    model_qwen: Optional[str],
    model_auggie: Optional[str],
    model_claude: Optional[str],
    enable_graphrag: bool,
    force_reindex: bool,
    log_level: str,
    log_file: Optional[str],
    verbose: bool,
) -> None:
    """Analyze repository and create feature enhancement issues."""

    selected_backends = normalize_backends(backends)
    primary_backend = selected_backends[0]
    models = build_models_map(model_gemini, model_qwen, model_auggie, model_claude)
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
    check_github_sub_issue_or_setup()

    # Get repository name (from parameter or auto-detect)
    repo_name = get_repo_or_detect(repo)

    backend_list_str = ", ".join(selected_backends)
    logger.info(f"Analyzing repository for feature opportunities: {repo_name}")
    logger.info(f"Using backends: {backend_list_str} (default: {primary_backend})")
    if primary_backend in ("gemini", "qwen", "auggie", "claude"):
        logger.info(f"Using model: {primary_model}")
    logger.info(f"Log level: {effective_log_level}")
    logger.info(f"Verbose logging: {verbose}")

    click.echo(f"Analyzing repository for feature opportunities: {repo_name}")
    click.echo(f"Using backends: {backend_list_str} (default: {primary_backend})")
    if primary_backend in ("gemini", "qwen", "auggie", "claude"):
        click.echo(f"Using model: {primary_model}")
    click.echo(f"Force reindex: {force_reindex}")
    click.echo(f"Verbose logging: {verbose}")

    # Initialize GraphRAG (conditionally enabled)
    if enable_graphrag:
        initialize_graphrag(force_reindex=force_reindex)

    # Initialize clients
    github_client = GitHubClient(github_token_final)
    manager = build_backend_manager(
        selected_backends,
        primary_backend,
        models,
        gemini_api_key,
        openai_api_key,
        openai_base_url,
        qwen_use_env_vars=qwen_use_env_vars,
        qwen_preserve_env=qwen_preserve_env,
        enable_graphrag=enable_graphrag,
    )

    # Check GraphRAG MCP configuration for selected backends using client
    check_graphrag_mcp_for_backends(selected_backends, client=manager)

    automation_engine = AutomationEngine(github_client)

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
    type=click.Choice(["codex", "codex-mcp", "gemini", "qwen", "auggie", "claude"]),
    help="AI backend(s) to use in priority order (default: codex)",
)
@click.option(
    "--message-backend",
    "message_backends",
    multiple=True,
    default=None,
    type=click.Choice(["codex", "codex-mcp", "gemini", "qwen", "auggie", "claude"]),
    help="AI backend(s) for message generation in priority order (default: same as --backend)",
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
@click.option(
    "--qwen-use-env-vars/--qwen-use-cli-options",
    default=True,
    help="Pass Qwen credentials via environment variables (default) or CLI options",
)
@click.option(
    "--qwen-preserve-env/--qwen-clear-env",
    default=False,
    help="Preserve existing OPENAI_* environment variables (default: clear before setting)",
)
@click.option("--model-gemini", help="Model to use when backend=gemini")
@click.option("--model-qwen", help="Model to use when backend=qwen")
@click.option("--model-auggie", help="Model to use when backend=auggie (defaults to GPT-5)")
@click.option("--model-claude", help="Model to use when backend=claude (defaults to sonnet)")
@click.option(
    "--max-attempts",
    type=int,
    default=None,
    help="Maximum fix attempts before giving up (defaults to engine config)",
)
@click.option(
    "--enable-graphrag/--disable-graphrag",
    default=True,
    help="Enable GraphRAG integration (default: enabled)",
)
@click.option("--dry-run", is_flag=True, help="Run without making changes (LLM edits simulated)")
@click.option(
    "--force-reindex",
    is_flag=True,
    default=False,
    help="Force GraphRAG code analysis reindexing even if index is up to date (default: false)",
)
@click.option(
    "--log-level",
    default="INFO",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]),
    help="Set logging level",
)
@click.option("--log-file", help="Log file path (optional)")
@click.option("--verbose", is_flag=True, help="Enable verbose logging and detailed command traces")
def fix_to_pass_tests_command(
    backends: tuple[str, ...],
    message_backends: Optional[tuple[str, ...]],
    gemini_api_key: Optional[str],
    openai_api_key: Optional[str],
    openai_base_url: Optional[str],
    qwen_use_env_vars: bool,
    qwen_preserve_env: bool,
    model_gemini: Optional[str],
    model_qwen: Optional[str],
    model_auggie: Optional[str],
    model_claude: Optional[str],
    max_attempts: Optional[int],
    dry_run: bool,
    enable_graphrag: bool,
    force_reindex: bool,
    log_level: str,
    log_file: Optional[str],
    verbose: bool,
) -> None:
    """Run local tests and repeatedly request LLM fixes until tests pass.

    If the LLM makes no edits in an iteration, error and stop.
    """
    selected_backends = normalize_backends(backends)
    primary_backend = selected_backends[0]
    models = build_models_map(model_gemini, model_qwen, model_auggie, model_claude)
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
    check_github_sub_issue_or_setup()

    backend_list_str = ", ".join(selected_backends)
    click.echo(f"Using backends: {backend_list_str} (default: {primary_backend})")
    if primary_backend in ("gemini", "qwen", "auggie", "claude"):
        click.echo(f"Using model: {primary_model}")
    click.echo(f"Dry run mode: {dry_run}")
    click.echo(f"Force reindex: {force_reindex}")
    click.echo(f"Verbose logging: {verbose}")

    # Initialize GraphRAG (conditionally enabled)
    if enable_graphrag:
        initialize_graphrag(force_reindex=force_reindex)

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
        qwen_use_env_vars=qwen_use_env_vars,
        qwen_preserve_env=qwen_preserve_env,
        enable_graphrag=enable_graphrag,
    )

    # Check GraphRAG MCP configuration for selected backends using client
    check_graphrag_mcp_for_backends(selected_backends, client=manager)

    # Initialize message backend manager (use same backends if not specified)
    from .cli_helpers import build_message_backend_manager

    message_backend_list = normalize_backends(message_backends) if message_backends else selected_backends
    message_primary_backend = message_backend_list[0]
    message_manager = build_message_backend_manager(
        message_backend_list,
        message_primary_backend,
        models,
        gemini_api_key,
        openai_api_key,
        openai_base_url,
        qwen_use_env_vars=qwen_use_env_vars,
        qwen_preserve_env=qwen_preserve_env,
    )

    if message_backends:
        message_backend_str = ", ".join(message_backend_list)
        logger.info(f"Using message backends: {message_backend_str} (default: {message_primary_backend})")
        click.echo(f"Using message backends: {message_backend_str} (default: {message_primary_backend})")

    engine = AutomationEngine(github_client, dry_run=dry_run)

    try:
        result = engine.fix_to_pass_tests(max_attempts=max_attempts, message_backend_manager=message_manager)
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
            message_manager.close()
        except Exception:
            pass
