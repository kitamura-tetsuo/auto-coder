"""Main CLI commands (process_issues, create_feature_issues, fix_to_pass_tests)."""

import os
import re
from pathlib import Path
from typing import Any, Optional

import click

from .automation_config import AutomationConfig
from .automation_engine import AutomationEngine
from .cli_commands_utils import get_github_token_or_fail, get_repo_or_detect
from .cli_helpers import build_models_map, check_backend_prerequisites, check_github_sub_issue_or_setup, check_graphrag_mcp_for_backends, ensure_test_script_or_fail, initialize_graphrag
from .git_utils import extract_number_from_branch, get_current_branch
from .github_client import GitHubClient
from .llm_backend_config import get_llm_config
from .logger_config import get_logger, setup_logger
from .progress_footer import setup_progress_footer_logging
from .utils import VERBOSE_ENV_FLAG, CommandExecutor

logger = get_logger(__name__)


def _resolve_backends_from_config(config: Any) -> list[str]:
    """Return enabled backends honoring configured order, falling back to default backend."""
    if getattr(config, "backend_order", None):
        candidates = list(config.backend_order)
    else:
        candidates = [config.default_backend]

    resolved: list[str] = []
    for name in candidates:
        backend_cfg = config.get_backend_config(name)
        if backend_cfg is not None and not backend_cfg.enabled:
            continue
        if name not in resolved:
            resolved.append(name)

    if not resolved:
        resolved.append("codex")
    return resolved


@click.command()
@click.option(
    "--repo",
    help="GitHub repository (owner/repo). If not specified, auto-detects from current Git repository.",
)
@click.option("--github-token", envvar="GITHUB_TOKEN", help="GitHub API token")
@click.option(
    "--jules-mode/--no-jules-mode",
    default=True,
    help='Run in jules mode - only add "jules" label to issues without AI analysis (default: on)',
)
@click.option(
    "--disable-labels/--no-disable-labels",
    default=False,
    help="Disable GitHub label operations (@auto-coder label) - affects LabelManager context manager behavior",
)
@click.option(
    "--check-labels/--no-check-labels",
    default=True,
    help="Enable checking for existing @auto-coder label before processing (default: enabled)",
)
@click.option(
    "--skip-main-update/--no-skip-main-update",
    default=True,
    help="When PR checks fail, skip merging the PR base branch into the PR before attempting fixes (default: skip)",
)
@click.option(
    "--ignore-dependabot-prs/--no-ignore-dependabot-prs",
    default=False,
    help="Skip non-ready dependency-bot PRs (Dependabot/Renovate/[bot]); still auto-merge when checks pass and PR is mergeable.",
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
    jules_mode: bool,
    disable_labels: Optional[bool],
    check_labels: bool,
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

    config = get_llm_config()
    selected_backends = _resolve_backends_from_config(config)
    primary_backend = config.default_backend
    models = build_models_map()
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

    backend_list_str = ", ".join(selected_backends)
    logger.info(f"Processing repository: {repo_name}")
    logger.info(f"Using backends: {backend_list_str} (default: {primary_backend})")
    if primary_backend in ("gemini", "qwen", "auggie", "claude"):
        logger.info(f"Using model: {primary_model}")
    logger.info(f"Jules mode: {jules_mode}")
    logger.info(f"Disable labels: {disable_labels}")
    logger.info(f"Check labels: {check_labels}")
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
    click.echo(f"Disable labels: {disable_labels}")
    click.echo(f"Check labels: {check_labels}")
    click.echo(f"Main update before fixes when PR checks fail: {policy_str}")
    click.echo(f"Ignore Dependabot PRs: {ignore_dependabot_prs}")
    click.echo(f"Force clean before checkout: {force_clean_before_checkout}")
    click.echo(f"Force reindex: {force_reindex}")
    click.echo(f"Verbose logging: {verbose}")

    # Initialize GraphRAG (conditionally enabled)
    if enable_graphrag:
        initialize_graphrag(force_reindex=force_reindex)

    # Initialize clients
    github_client = GitHubClient.get_instance(github_token_final, disable_labels=bool(disable_labels))
    # Use global LLMBackendManager for main backend
    from auto_coder.backend_manager import get_llm_backend_manager

    from .cli_helpers import build_backend_manager_from_config

    # Create manager using configuration from TOML file with CLI parameter overrides
    manager = build_backend_manager_from_config(enable_graphrag=enable_graphrag, cli_models=models, cli_backends=selected_backends)

    # Get actual backends and primary backend from the manager
    selected_backends = manager._all_backends[:]
    primary_backend = manager._default_backend
    primary_model = None
    if primary_backend in ("gemini", "qwen", "auggie", "claude"):
        # Get the actual model from the client
        client = manager._clients[primary_backend]
        if client is not None:
            try:
                primary_model = client.model_name
            except AttributeError:
                primary_model = None  # Will be resolved from config

    # Check GraphRAG MCP configuration for selected backends using client
    check_graphrag_mcp_for_backends(selected_backends, client=manager)

    # Initialize message backend manager using configuration from TOML file
    from .cli_helpers import build_message_backend_manager

    message_manager = build_message_backend_manager(selected_backends, selected_backends[0], models)
    message_backend_list = message_manager._all_backends[:]
    message_primary_backend = message_manager._default_backend
    message_backend_str = ", ".join(message_backend_list)
    logger.info(f"Using message backends: {message_backend_str} (default: {message_primary_backend})")
    click.echo(f"Using message backends: {message_backend_str} (default: {message_primary_backend})")

    # Configure engine behavior flags
    engine_config = AutomationConfig()

    # When --only is specified, set CHECK_LABELS to False
    effective_check_labels = check_labels
    if only_target:
        effective_check_labels = False

    engine_config.CHECK_LABELS = effective_check_labels
    engine_config.SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL = bool(skip_main_update)
    engine_config.IGNORE_DEPENDABOT_PRS = bool(ignore_dependabot_prs)
    engine_config.DISABLE_LABELS = bool(disable_labels)
    engine_config.FORCE_CLEAN_BEFORE_CHECKOUT = bool(force_clean_before_checkout)

    automation_engine = AutomationEngine(
        github_client,
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
                engine_config.CHECK_LABELS = False

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
        number = None
        target_type = None
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
            # Try plain number - auto-detect type (try PR first, then issue)
            try:
                number = int(only_target.strip())
            except ValueError:
                raise click.ClickException("--only must be a PR/Issue URL or a number")
        # Run single-item processing with auto-detection if needed
        if target_type is None:
            # Auto-detect: try PR first, then issue
            try:
                _ = automation_engine.process_single(repo_name, "pr", number, jules_mode=jules_mode)
                target_type = "pr"
            except Exception:
                # Fall back to issue
                _ = automation_engine.process_single(repo_name, "issue", number, jules_mode=jules_mode)
                target_type = "issue"
        else:
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
    gemini_config = config.get_backend_config("gemini")
    if primary_backend == "gemini" and gemini_config is not None and gemini_config.api_key is not None:
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
    "--disable-labels/--no-disable-labels",
    default=False,
    help="Disable GitHub label operations (@auto-coder label) - affects LabelManager context manager behavior",
)
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
    disable_labels: Optional[bool],
    enable_graphrag: bool,
    force_reindex: bool,
    log_level: str,
    log_file: Optional[str],
    verbose: bool,
) -> None:
    """Analyze repository and create feature enhancement issues."""

    config = get_llm_config()
    selected_backends = _resolve_backends_from_config(config)
    primary_backend = config.default_backend
    models = build_models_map()
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
    logger.info(f"Disable labels: {disable_labels}")

    click.echo(f"Analyzing repository for feature opportunities: {repo_name}")
    click.echo(f"Using backends: {backend_list_str} (default: {primary_backend})")
    if primary_backend in ("gemini", "qwen", "auggie", "claude"):
        click.echo(f"Using model: {primary_model}")
    click.echo(f"Disable labels: {disable_labels}")
    click.echo(f"Force reindex: {force_reindex}")
    click.echo(f"Verbose logging: {verbose}")

    # Initialize GraphRAG (conditionally enabled)
    if enable_graphrag:
        initialize_graphrag(force_reindex=force_reindex)

    # Initialize clients
    github_client = GitHubClient.get_instance(github_token_final, disable_labels=bool(disable_labels))
    from .cli_helpers import build_backend_manager_from_config

    # Create manager using configuration from TOML file with CLI parameter overrides
    manager = build_backend_manager_from_config(enable_graphrag=enable_graphrag, cli_models=models, cli_backends=selected_backends)

    # Get actual backends and primary backend from the manager
    selected_backends = manager._all_backends[:]
    primary_backend = manager._default_backend
    primary_model = None
    if primary_backend in ("gemini", "qwen", "auggie", "claude"):
        # Get the actual model from the client
        client = manager._clients[primary_backend]
        if client is not None:
            try:
                primary_model = client.model_name
            except AttributeError:
                primary_model = None  # Will be resolved from config

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
    "--disable-labels/--no-disable-labels",
    default=False,
    help="Disable GitHub label operations (@auto-coder label) - affects LabelManager context manager behavior",
)
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
    disable_labels: Optional[bool],
    max_attempts: Optional[int],
    enable_graphrag: bool,
    force_reindex: bool,
    log_level: str,
    log_file: Optional[str],
    verbose: bool,
) -> None:
    """Run local tests and repeatedly request LLM fixes until tests pass.

    If the LLM makes no edits in an iteration, error and stop.
    """
    config = get_llm_config()
    selected_backends = _resolve_backends_from_config(config)
    primary_backend = config.default_backend
    models = build_models_map()
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
    click.echo(f"Disable labels: {disable_labels}")
    click.echo(f"Force reindex: {force_reindex}")
    click.echo(f"Verbose logging: {verbose}")

    # Initialize GraphRAG (conditionally enabled)
    if enable_graphrag:
        initialize_graphrag(force_reindex=force_reindex)

    # Initialize minimal clients (GitHub not used here, but engine expects a client)
    try:
        from .github_client import GitHubClient as _GH

        github_client = _GH("", disable_labels=bool(disable_labels))
    except Exception:
        # Fallback to a minimal stand-in (never used)
        class _Dummy:
            token = ""

        github_client = _Dummy()  # type: ignore

    from .cli_helpers import build_backend_manager_from_config

    # Create manager using configuration from TOML file with CLI parameter overrides
    manager = build_backend_manager_from_config(enable_graphrag=enable_graphrag, cli_models=models, cli_backends=selected_backends)

    # Get actual backends and primary backend from the manager
    selected_backends = manager._all_backends[:]
    primary_backend = manager._default_backend
    primary_model = None
    if primary_backend in ("gemini", "qwen", "auggie", "claude"):
        # Get the actual model from the client
        client = manager._clients[primary_backend]
        if client is not None:
            try:
                primary_model = client.model_name
            except AttributeError:
                primary_model = None  # Will be resolved from config

    # Check GraphRAG MCP configuration for selected backends using client
    check_graphrag_mcp_for_backends(selected_backends, client=manager)

    # Initialize message backend manager using configuration from TOML file
    from .cli_helpers import build_message_backend_manager

    message_manager = build_message_backend_manager(selected_backends, selected_backends[0], models)
    message_backend_list = message_manager._all_backends[:]
    message_primary_backend = message_manager._default_backend
    message_backend_str = ", ".join(message_backend_list)
    logger.info(f"Using message backends: {message_backend_str} (default: {message_primary_backend})")
    click.echo(f"Using message backends: {message_backend_str} (default: {message_primary_backend})")

    engine_config = AutomationConfig()
    engine = AutomationEngine(github_client, config=engine_config)

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
