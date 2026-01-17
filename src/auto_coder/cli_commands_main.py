"""Main CLI commands (process_issues, create_feature_issues, fix_to_pass_tests)."""

import os
import re
from typing import Dict, Optional

import click

from .automation_config import AutomationConfig
from .automation_engine import AutomationEngine
from .cli_commands_utils import get_github_token_or_fail, get_repo_or_detect
from .cli_helpers import build_backend_manager_from_config, build_message_backend_manager, build_models_map, check_backend_prerequisites, check_github_sub_issue_or_setup, check_graphrag_mcp_for_backends, ensure_test_script_or_fail, initialize_graphrag
from .cli_ui import print_configuration_summary, sleep_with_countdown
from .git_utils import extract_number_from_branch, get_current_branch
from .llm_backend_config import get_llm_config
from .logger_config import get_logger, setup_logger
from .progress_footer import setup_progress_footer_logging
from .util.gh_cache import GitHubClient
from .utils import VERBOSE_ENV_FLAG

logger = get_logger(__name__)


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
    "--auto-merge/--no-auto-merge",
    default=True,
    help="Enable auto-merge of PRs when checks pass and PR is mergeable (default: enabled)",
)
@click.option(
    "--auto-merge-dependabot-prs/--no-auto-merge-dependabot-prs",
    default=True,
    help="Enable auto-merge of Dependabot PRs when checks pass and PR is mergeable (default: enabled)",
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
    disable_labels: Optional[bool],
    check_labels: bool,
    skip_main_update: bool,
    ignore_dependabot_prs: bool,
    auto_merge: bool,
    auto_merge_dependabot_prs: bool,
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

    active_backends = config.get_active_backends()
    ordered_backends = [backend for backend in (config.backend_order or []) if backend in active_backends]
    selected_backends = ordered_backends or [config.default_backend or "codex"]
    primary_backend = selected_backends[0]
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
    logger.info(f"Disable labels: {disable_labels}")
    logger.info(f"Check labels: {check_labels}")
    logger.info(f"Log level: {effective_log_level}")
    logger.info(f"Verbose logging: {verbose}")
    logger.info(f"Ignore Dependabot PRs: {ignore_dependabot_prs}")
    logger.info(f"Auto-merge: {auto_merge}")
    logger.info(f"Auto-merge Dependabot PRs: {auto_merge_dependabot_prs}")
    logger.info(f"Force clean before checkout: {force_clean_before_checkout}")

    # Explicitly show base branch update policy for PR checks failure
    policy_str = "SKIP (default)" if skip_main_update else "ENABLED (--no-skip-main-update)"
    logger.info(f"Base branch update before fixes when PR checks fail: {policy_str}")

    summary: Dict[str, str] = {
        "Repository": repo_name,
        "Backends": f"{backend_list_str} (default: {primary_backend})",
    }
    if primary_backend in ("gemini", "qwen", "auggie", "claude"):
        summary["Model"] = primary_model or ""
    summary.update(
        {
            "Disable labels": str(disable_labels),
            "Check labels": str(check_labels),
            "Main update before fixes": policy_str,
            "Ignore Dependabot PRs": str(ignore_dependabot_prs),
            "Auto-merge": str(auto_merge),
            "Auto-merge Dependabot PRs": str(auto_merge_dependabot_prs),
            "Force clean before checkout": str(force_clean_before_checkout),
            "Force reindex": str(force_reindex),
            "Verbose logging": str(verbose),
        }
    )
    print_configuration_summary("Processing Configuration", summary)

    # Initialize GraphRAG (conditionally enabled)
    if enable_graphrag:
        initialize_graphrag(force_reindex=force_reindex)

    # Initialize clients
    github_client = GitHubClient.get_instance(github_token_final, disable_labels=bool(disable_labels))
    manager = build_backend_manager_from_config(
        enable_graphrag=enable_graphrag,
        cli_models=models,
        cli_backends=selected_backends,
    )

    # Initialize LLM backend manager singleton
    from .backend_manager import LLMBackendManager

    LLMBackendManager.get_llm_instance(
        default_backend=manager._default_backend,
        default_client=manager._clients[manager._default_backend],
        factories=manager._factories,
        order=manager._all_backends,
    )

    selected_backends = manager._all_backends[:]
    primary_backend = manager._default_backend
    primary_model = None
    if primary_backend in ("gemini", "qwen", "auggie", "claude"):
        client = manager._clients.get(primary_backend)
        if client is not None:
            primary_model = getattr(client, "model_name", None)

    check_graphrag_mcp_for_backends(selected_backends, client=manager)

    message_manager = build_message_backend_manager(models=models)
    message_backend_list = message_manager._all_backends[:]
    message_primary_backend = message_manager._default_backend
    message_backend_str = ", ".join(message_backend_list)
    logger.info(f"Message backends: {message_backend_str} (default: {message_primary_backend})")

    # Configure engine behavior flags
    engine_config = AutomationConfig()

    # When --only is specified, set CHECK_LABELS to False
    effective_check_labels = check_labels
    if only_target:
        effective_check_labels = False

    engine_config.CHECK_LABELS = effective_check_labels
    engine_config.SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL = bool(skip_main_update)
    engine_config.IGNORE_DEPENDABOT_PRS = bool(ignore_dependabot_prs)
    engine_config.AUTO_MERGE = bool(auto_merge)
    engine_config.AUTO_MERGE_DEPENDABOT_PRS = bool(auto_merge_dependabot_prs)
    engine_config.DISABLE_LABELS = bool(disable_labels)
    engine_config.FORCE_CLEAN_BEFORE_CHECKOUT = bool(force_clean_before_checkout)

    automation_engine = AutomationEngine(
        github_client,  # type: ignore[arg-type]
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
                        # gh_repo = github_client.get_repository(repo_name)
                        # issue = gh_repo.get_issue(number)
                        issue = github_client.get_issue(repo_name, number)
                        issue_data = github_client.get_issue_details(issue)
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
                _ = automation_engine.process_single(repo_name, target_type, number)
                # Print brief summary to stdout
                click.echo(f"Processed {target_type} #{number}")
                # Close MCP session if present
                try:
                    manager.close()
                    message_manager.close()
                except Exception:
                    pass
                # After resuming and processing the current branch item,
                # continue to the main loop to process other items.
                pass
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
                _ = automation_engine.process_single(repo_name, "pr", number)
                target_type = "pr"
            except Exception:
                # Fall back to issue
                _ = automation_engine.process_single(repo_name, "issue", number)
                target_type = "issue"
        else:
            _ = automation_engine.process_single(repo_name, target_type, number)
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

    import time

    from .llm_backend_config import get_process_issues_empty_sleep_time_from_config, get_process_issues_sleep_time_from_config

    while True:
        if primary_backend == "gemini" and gemini_config and gemini_config.api_key:
            result = automation_engine.run(repo_name)
        else:
            result = automation_engine.run(repo_name)

        # Determine sleep time based on OPEN issues/PRs (not just processed ones)
        # We check if there are any open issues or PRs to decide if we should sleep short or long.
        # Use limit=1 to minimize API cost just to check existence.
        open_issues_exist = len(github_client.get_open_issues(repo_name, limit=1)) > 0
        open_prs_exist = len(github_client.get_open_pull_requests(repo_name, limit=1)) > 0

        processed_issues = len(result.get("issues_processed", []))
        processed_prs = len(result.get("prs_processed", []))

        if not open_issues_exist and not open_prs_exist:
            sleep_time = get_process_issues_empty_sleep_time_from_config()
            logger.info(f"No open issues or PRs found. Sleeping for extended time: {sleep_time} seconds...")
        else:
            sleep_time = get_process_issues_sleep_time_from_config()
            logger.info(f"Processed {processed_issues} issues and {processed_prs} PRs (Open items detected). Sleeping for {sleep_time} seconds...")

        sleep_with_countdown(sleep_time)

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

    active_backends = config.get_active_backends()
    ordered_backends = [backend for backend in (config.backend_order or []) if backend in active_backends]
    selected_backends = ordered_backends or [config.default_backend or "codex"]
    primary_backend = selected_backends[0]
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

    summary: Dict[str, str] = {
        "Repository": repo_name,
        "Backends": f"{backend_list_str} (default: {primary_backend})",
    }
    if primary_backend in ("gemini", "qwen", "auggie", "claude"):
        summary["Model"] = primary_model or ""
    summary.update(
        {
            "Disable labels": str(disable_labels),
            "Force reindex": str(force_reindex),
            "Verbose logging": str(verbose),
        }
    )
    print_configuration_summary("Feature Analysis Configuration", summary)

    # Initialize GraphRAG (conditionally enabled)
    if enable_graphrag:
        initialize_graphrag(force_reindex=force_reindex)

    # Initialize clients
    github_client = GitHubClient.get_instance(github_token_final, disable_labels=bool(disable_labels))
    manager = build_backend_manager_from_config(
        enable_graphrag=enable_graphrag,
        cli_models=models,
        cli_backends=selected_backends,
    )

    # Initialize LLM backend manager singleton
    from .backend_manager import LLMBackendManager

    LLMBackendManager.get_llm_instance(
        default_backend=manager._default_backend,
        default_client=manager._clients[manager._default_backend],
        factories=manager._factories,
        order=manager._all_backends,
    )

    selected_backends = manager._all_backends[:]
    primary_backend = manager._default_backend
    if primary_backend in ("gemini", "qwen", "auggie", "claude"):
        client = manager._clients.get(primary_backend)
        if client is not None:
            primary_model = getattr(client, "model_name", None)

    check_graphrag_mcp_for_backends(selected_backends, client=manager)

    # Configure engine behavior flags
    engine_config = AutomationConfig()
    engine_config.DISABLE_LABELS = bool(disable_labels)

    automation_engine = AutomationEngine(github_client, config=engine_config)  # type: ignore[arg-type]

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

    active_backends = config.get_active_backends()
    ordered_backends = [backend for backend in (config.backend_order or []) if backend in active_backends]
    selected_backends = ordered_backends or [config.default_backend or "codex"]
    primary_backend = selected_backends[0]
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

    summary: Dict[str, str] = {
        "Backends": f"{backend_list_str} (default: {primary_backend})",
    }
    if primary_backend in ("gemini", "qwen", "auggie", "claude"):
        summary["Model"] = primary_model or ""
    summary.update(
        {
            "Disable labels": str(disable_labels),
            "Force reindex": str(force_reindex),
            "Verbose logging": str(verbose),
        }
    )
    print_configuration_summary("Fix Tests Configuration", summary)

    # Initialize GraphRAG (conditionally enabled)
    if enable_graphrag:
        initialize_graphrag(force_reindex=force_reindex)

    # Initialize minimal clients (GitHub not used here, but engine expects a client)
    try:
        from .util.gh_cache import GitHubClient as _GH

        github_client = _GH("", disable_labels=bool(disable_labels))
    except Exception:
        # Fallback to a minimal stand-in (never used)
        class _Dummy:
            token = ""

        github_client = _Dummy()  # type: ignore

    manager = build_backend_manager_from_config(
        enable_graphrag=enable_graphrag,
        cli_models=models,
        cli_backends=selected_backends,
    )

    # Initialize LLM backend manager singleton
    from .backend_manager import LLMBackendManager

    LLMBackendManager.get_llm_instance(
        default_backend=manager._default_backend,
        default_client=manager._clients[manager._default_backend],
        factories=manager._factories,
        order=manager._all_backends,
    )

    selected_backends = manager._all_backends[:]
    primary_backend = manager._default_backend
    if primary_backend in ("gemini", "qwen", "auggie", "claude"):
        client = manager._clients.get(primary_backend)
        if client is not None:
            primary_model = getattr(client, "model_name", None)

    check_graphrag_mcp_for_backends(selected_backends, client=manager)

    message_manager = build_message_backend_manager(models=models)
    message_backend_list = message_manager._all_backends[:]
    message_primary_backend = message_manager._default_backend
    logger.info(f"Message backends: {', '.join(message_backend_list)} (default: {message_primary_backend})")
    engine_config = AutomationConfig()
    engine = AutomationEngine(github_client, config=engine_config)  # type: ignore[arg-type]

    try:
        result = engine.fix_to_pass_tests(
            llm_backend_manager=manager,
            max_attempts=max_attempts,
            message_backend_manager=message_manager,
        )
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
