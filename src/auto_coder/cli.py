"""Command Line Interface for Auto-Coder."""

import os
import sys
from typing import Optional

import click
from dotenv import load_dotenv

from . import __version__ as AUTO_CODER_VERSION
from .auggie_client import AuggieClient
from .auth_utils import get_auth_status, get_github_token
from .automation_config import AutomationConfig
from .automation_engine import AutomationEngine
from .backend_manager import BackendManager
from .codex_client import CodexClient
from .codex_mcp_client import CodexMCPClient
from .gemini_client import GeminiClient
from .git_utils import get_current_repo_name, is_git_repository
from .github_client import GitHubClient
from .logger_config import get_logger, setup_logger
from .qwen_client import QwenClient
from .update_manager import maybe_run_auto_update, record_startup_options
from .utils import VERBOSE_ENV_FLAG

# Load environment variables
load_dotenv()

# Initialize logger
logger = get_logger(__name__)


def ensure_test_script_or_fail() -> None:
    """Ensure TEST_SCRIPT_PATH exists; error early if missing.
    This check runs at CLI startup for commands that may run tests.
    """
    cfg = AutomationConfig()
    script_path = cfg.TEST_SCRIPT_PATH
    if not os.path.exists(script_path):
        raise click.ClickException(
            f"Required test script not found: {script_path}. This tool requires a target-repo-provided test script."
        )


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
    """Get GitHub token from parameter or auto-detect from gh CLI.

    Note: Do not print to stdout to avoid polluting CLI outputs that pipe to files.
    """
    if provided_token:
        return provided_token

    # Try to auto-detect token
    detected_token = get_github_token()
    if detected_token:
        # Use logger instead of stdout to avoid prelude noise in CLI outputs
        logger.info("Using GitHub token from gh CLI authentication")
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
            ["gemini", "--version"], capture_output=True, text=True, timeout=10
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
    """Check if Codex (or override) CLI is available and working.

    For testing or custom environments, you can override the codex CLI binary
    via environment variable AUTOCODER_CODEX_CLI. When set, we will try to
    execute the command with `--version` first; if that fails, we will run the
    command without arguments as a liveness check.
    """
    import shlex
    import subprocess

    # Allow override for CI/e2e tests
    override = os.environ.get("AUTOCODER_CODEX_CLI")
    if override:
        cmd = shlex.split(override)
        # Try with --version
        try:
            result = subprocess.run(
                cmd + ["--version"], capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                click.echo(f"Using codex CLI (override): {cmd[0]}")
                return
        except Exception:
            pass
        # Fallback: run without args
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                click.echo(f"Using codex CLI (override): {cmd[0]}")
                return
        except Exception:
            pass
        raise click.ClickException(
            "Codex CLI override (AUTOCODER_CODEX_CLI) is set but not working"
        )

    # Default: check real codex CLI
    try:
        result = subprocess.run(
            ["codex", "--version"], capture_output=True, text=True, timeout=10
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


def check_qwen_cli_or_fail() -> None:
    """Check if qwen CLI is available and working."""
    try:
        import subprocess

        result = subprocess.run(
            ["qwen", "--version"], capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            click.echo("Using qwen CLI")
            return
    except Exception:
        pass
    raise click.ClickException(
        "Qwen Code CLI is required. Please install it from:\n"
        "https://github.com/QwenLM/qwen-code\n"
        "Or use: npm install -g @qwen-code/qwen-code"
    )


def check_auggie_cli_or_fail() -> None:
    """Check if auggie CLI is available and working."""
    try:
        import subprocess

        result = subprocess.run(
            ["auggie", "--version"], capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            click.echo("Using auggie CLI")
            return
    except Exception:
        pass
    raise click.ClickException(
        "Auggie CLI is required. Please install it via:\n"
        "npm install -g @augmentcode/auggie"
    )


def _build_models_map(
    model_gemini: Optional[str] = None,
    model_qwen: Optional[str] = None,
    model_auggie: Optional[str] = None,
) -> dict[str, str]:
    """Compute per-backend model map with sensible defaults.

    Priority per backend: specific flag (--model-<backend>) > backend default.
    Backend defaults:
      - gemini: gemini-2.5-pro
      - qwen: qwen3-coder-plus
      - auggie: GPT-5
      - codex/codex-mcp: placeholders (unused by CLI but kept for uniformity)
    """
    models: dict[str, str] = {}
    # codex backends (accepted for compatibility, not actually used by CLI)
    models["codex"] = "codex"
    models["codex-mcp"] = "codex-mcp"
    # gemini
    models["gemini"] = model_gemini or "gemini-2.5-pro"
    # qwen
    models["qwen"] = model_qwen or "qwen3-coder-plus"
    # auggie
    models["auggie"] = model_auggie or "GPT-5"
    return models


def _normalize_backends(backends: tuple[str, ...]) -> list[str]:
    """Preserve order, drop duplicates, and ensure at least one backend (default codex)."""
    seen: set[str] = set()
    normalized: list[str] = []
    for backend_name in backends:
        if backend_name not in seen:
            normalized.append(backend_name)
            seen.add(backend_name)
    if not normalized:
        normalized.append("codex")
    return normalized


def _check_backend_prerequisites(backends: list[str]) -> None:
    """Verify CLI prerequisites for all requested backends."""
    for backend_name in backends:
        if backend_name in ("codex", "codex-mcp"):
            check_codex_cli_or_fail()
        elif backend_name == "gemini":
            check_gemini_cli_or_fail()
        elif backend_name == "qwen":
            check_qwen_cli_or_fail()
        elif backend_name == "auggie":
            check_auggie_cli_or_fail()
        else:
            raise click.ClickException(f"Unsupported backend specified: {backend_name}")


def _build_backend_manager(
    selected_backends: list[str],
    primary_backend: str,
    models: dict[str, str],
    gemini_api_key: Optional[str],
    openai_api_key: Optional[str],
    openai_base_url: Optional[str],
) -> BackendManager:
    """Construct BackendManager with per-backend model selection.

    models: mapping backend -> model_name (codex backends ignored but accepted).
    """

    def _gm() -> str:
        return models.get("gemini", "gemini-2.5-pro")

    def _qm() -> str:
        return models.get("qwen", "qwen3-coder-plus")

    def _am() -> str:
        return models.get("auggie", "GPT-5")

    factories_all = {
        "codex": lambda: CodexClient(model_name="codex"),
        "codex-mcp": lambda: CodexMCPClient(model_name="codex-mcp"),
        "gemini": lambda: (
            GeminiClient(gemini_api_key, model_name=_gm())
            if gemini_api_key
            else GeminiClient(model_name=_gm())
        ),
        "qwen": lambda: QwenClient(
            model_name=_qm(),
            openai_api_key=openai_api_key,
            openai_base_url=openai_base_url,
        ),
        "auggie": lambda: AuggieClient(model_name=_am()),
    }

    missing_factories = [
        name for name in selected_backends if name not in factories_all
    ]
    if missing_factories:
        raise click.ClickException(
            f"Unsupported backend(s) specified: {', '.join(missing_factories)}"
        )

    default_client = factories_all[primary_backend]()
    selected_factories = {name: factories_all[name] for name in selected_backends}

    return BackendManager(
        default_backend=primary_backend,
        default_client=default_client,
        factories=selected_factories,
        order=selected_backends,
    )


def qwen_help_has_flags(required_flags: list[str]) -> bool:
    """Lightweight probe for qwen --help to verify presence of required flags.

    Tolerates short/long form equivalence, e.g. "-p" <-> "--prompt", "-m" <-> "--model".
    Returns False on any error; intended for tests and optional diagnostics. Fully mocked in CI.
    """
    try:
        import re as _re
        import subprocess

        res = subprocess.run(
            ["qwen", "--help"], capture_output=True, text=True, timeout=10
        )
        if res.returncode != 0:
            return False
        help_text_raw = (res.stdout or "") + (res.stderr or "")

        # Normalize help text: strip ANSI, unify dashes, collapse whitespace
        ansi_re = _re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
        help_text = ansi_re.sub("", help_text_raw)
        help_text = help_text.replace("\u2013", "-").replace("\u2014", "-")
        help_text = " ".join(help_text.split())

        # Map equivalent flags so either form satisfies the requirement
        equivalents = {
            "-p": ["-p", "--prompt"],
            "--prompt": ["--prompt", "-p"],
            "-m": ["-m", "--model"],
            "--model": ["--model", "-m"],
        }

        def flag_present(flag: str) -> bool:
            options = equivalents.get(flag, [flag])
            return any(opt in help_text for opt in options)

        return all(flag_present(f) for f in required_flags)
    except Exception:
        return False


@click.group()
@click.version_option(version=AUTO_CODER_VERSION, package_name="auto-coder")
def main() -> None:
    """Auto-Coder: Automated application development using Gemini CLI and GitHub integration."""
    record_startup_options(sys.argv, os.environ)
    maybe_run_auto_update()


@main.command()
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

    selected_backends = _normalize_backends(backends)
    primary_backend = selected_backends[0]
    models = _build_models_map(model_gemini, model_qwen, model_auggie)
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
    _check_backend_prerequisites(selected_backends)

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

    # Initialize clients
    github_client = GitHubClient(github_token_final)
    manager = _build_backend_manager(
        selected_backends,
        primary_backend,
        models,
        gemini_api_key,
        openai_api_key,
        openai_base_url,
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
        import re

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


@main.command()
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

    selected_backends = _normalize_backends(backends)
    primary_backend = selected_backends[0]
    models = _build_models_map(model_gemini, model_qwen, model_auggie)
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
    _check_backend_prerequisites(selected_backends)

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

    # Initialize clients
    github_client = GitHubClient(github_token_final)
    manager = _build_backend_manager(
        selected_backends,
        primary_backend,
        models,
        gemini_api_key,
        openai_api_key,
        openai_base_url,
    )

    automation_engine = AutomationEngine(github_client, manager)

    # Analyze and create feature issues
    automation_engine.create_feature_issues(repo_name)

    # Close MCP session if present
    try:
        manager.close()
    except Exception:
        pass


@main.command(name="fix-to-pass-tests")
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
    selected_backends = _normalize_backends(backends)
    primary_backend = selected_backends[0]
    models = _build_models_map(model_gemini, model_qwen, model_auggie)
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
    _check_backend_prerequisites(selected_backends)

    # Initialize minimal clients (GitHub not used here, but engine expects a client)
    try:
        from .github_client import GitHubClient as _GH

        github_client = _GH("")
    except Exception:
        # Fallback to a minimal stand-in (never used)
        class _Dummy:
            token = ""

        github_client = _Dummy()  # type: ignore

    manager = _build_backend_manager(
        selected_backends,
        primary_backend,
        models,
        gemini_api_key,
        openai_api_key,
        openai_base_url,
    )

    engine = AutomationEngine(github_client, manager, dry_run=dry_run)

    backend_list_str = ", ".join(selected_backends)
    click.echo(f"Using backends: {backend_list_str} (default: {primary_backend})")
    if primary_backend in ("gemini", "qwen", "auggie"):
        click.echo(f"Using model: {primary_model}")
    click.echo(f"Dry run mode: {dry_run}")
    click.echo(f"Verbose logging: {verbose}")

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


@main.command()
@click.option("--url", "actions_url", required=True, help="GitHub Actions job URL")
@click.option("--github-token", envvar="GITHUB_TOKEN", help="GitHub API token")
def get_actions_logs(actions_url: str, github_token: Optional[str]) -> None:
    """Fetch error logs from a GitHub Actions job URL for debugging."""
    # Route log output to stderr to avoid polluting stdout which is piped to file
    setup_logger(stream=sys.stderr)
    github_token_final = get_github_token_or_fail(github_token)
    github_client = GitHubClient(github_token_final)
    engine = AutomationEngine(github_client, None, dry_run=True)
    logs = engine.get_github_actions_logs_from_url(actions_url)
    click.echo(logs)


@main.command()
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

        result = subprocess.run(
            ["gemini", "--version"], capture_output=True, text=True, timeout=10
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
        click.echo(
            "     Please install from: https://github.com/google-gemini/gemini-cli"
        )

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

        res = _sp.run(
            ["auggie", "--version"], capture_output=True, text=True, timeout=10
        )
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


@main.group(name="mcp-pdb")
def mcp_pdb_group() -> None:
    """MCP-PDB のセットアップ支援ユーティリティ。

    - print-config: Windsurf/Claude 用の設定スニペットを出力
    - status: 必要な前提コマンドの存在を確認
    """
    pass


def _windsurf_mcp_config_snippet() -> str:
    import json as _json

    return _json.dumps(
        {
            "mcpServers": {
                "mcp-pdb": {
                    "command": "uv",
                    "args": ["run", "--with", "mcp-pdb", "mcp-pdb"],
                }
            }
        },
        indent=2,
        ensure_ascii=False,
    )


@mcp_pdb_group.command("print-config")
@click.option(
    "--target",
    type=click.Choice(["windsurf", "claude"], case_sensitive=False),
    default="windsurf",
    help="出力先ツールの種類 (windsurf|claude)",
)
@click.option(
    "--write-to",
    type=click.Path(dir_okay=False, resolve_path=True),
    help="出力内容をファイルにも保存するパス (任意)",
)
def mcp_pdb_print_config(target: str, write_to: Optional[str]) -> None:
    """mcp-pdb の設定を出力（必要に応じてファイル保存）。"""
    setup_logger()  # 標準設定
    if target.lower() == "windsurf":
        content = _windsurf_mcp_config_snippet()
    else:
        # Claude Code 用はコマンドラインをそのまま提示
        content = (
            "# Install the MCP server\n"
            "claude mcp add mcp-pdb -- uv run --with mcp-pdb mcp-pdb\n\n"
            "# Alternative: Install with specific Python version\n"
            "claude mcp add mcp-pdb -- uv run --python 3.13 --with mcp-pdb mcp-pdb\n\n"
            "# Note: The -- separator is required for Claude Code CLI\n"
        )

    click.echo(content)
    if write_to:
        try:
            with open(write_to, "w", encoding="utf-8") as f:
                f.write(content)
            logger.info(f"Wrote mcp-pdb config to: {write_to}")
        except Exception as e:
            raise click.ClickException(f"Failed to write file: {e}")


@mcp_pdb_group.command("status")
def mcp_pdb_status() -> None:
    """mcp-pdb 利用に必要な前提コマンドの存在確認を行う。"""
    setup_logger()
    click.echo("Checking MCP-PDB prerequisites...\n")

    # uv の存在確認
    try:
        import subprocess as _sp

        res = _sp.run(["uv", "--version"], capture_output=True, text=True, timeout=10)
        if res.returncode == 0:
            ver = (res.stdout or "").strip()
            click.echo("✅ uv available")
            if ver:
                click.echo(f"   {ver}")
        else:
            click.echo("❌ uv not working")
    except Exception:
        click.echo("❌ uv not found")
        click.echo("   Install uv: https://docs.astral.sh/uv/")

    click.echo()
    click.echo("Setup tips:")
    click.echo("  - Windsurf: settings.json に mcpServers を追加")
    click.echo(
        "  - Claude Code: 'claude mcp add mcp-pdb -- uv run --with mcp-pdb mcp-pdb'"
    )


if __name__ == "__main__":
    main()
