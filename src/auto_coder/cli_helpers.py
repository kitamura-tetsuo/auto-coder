"""CLI helper functions for backend management and validation."""

import os
import shlex
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import click
import toml

from .auggie_client import AuggieClient
from .automation_config import AutomationConfig
from .backend_manager import BackendManager
from .claude_client import ClaudeClient
from .codex_client import CodexClient
from .codex_mcp_client import CodexMCPClient
from .gemini_client import GeminiClient
from .llm_backend_config import BackendConfig, LLMBackendConfiguration, get_llm_config
from .qwen_client import QwenClient


def ensure_test_script_or_fail() -> None:
    """Ensure TEST_SCRIPT_PATH exists; error early if missing.
    This check runs at CLI startup for commands that may run tests.
    """
    cfg = AutomationConfig()
    script_path = cfg.TEST_SCRIPT_PATH
    if not os.path.exists(script_path):
        raise click.ClickException(f"Required test script not found: {script_path}. This tool requires a target-repo-provided test script.")


def initialize_graphrag(force_reindex: bool = False) -> None:
    """Initialize GraphRAG integration (always enabled).

    This function ensures GraphRAG environment is ready:
    - Ensures GraphRAG MCP server is installed and configured
    - Starts Docker containers if not running
    - Updates index if outdated (or forces update if force_reindex=True)
    - Starts MCP server if configured

    Args:
        force_reindex: Force reindexing even if index is up to date

    Raises:
        click.ClickException: If GraphRAG initialization fails
    """
    from pathlib import Path

    from .graphrag_mcp_integration import GraphRAGMCPIntegration
    from .logger_config import get_logger

    logger = get_logger(__name__)
    logger.info("Initializing GraphRAG integration...")
    if force_reindex:
        click.echo("GraphRAG integration: enabled (always) - forcing reindex")
    else:
        click.echo("GraphRAG integration: enabled (always)")

    # 1. Ensure GraphRAG MCP server is installed
    default_mcp_dir = Path.home() / "graphrag_mcp"
    if not default_mcp_dir.exists():
        logger.info(f"GraphRAG MCP server directory not found at {default_mcp_dir}")
        logger.info("Automatically setting up GraphRAG MCP server...")
        click.echo()
        click.echo("⚠️  GraphRAG MCP server not installed")
        click.echo("   Automatically setting up GraphRAG MCP server...")
        click.echo()

        # Import here to avoid circular dependency
        from .cli_commands_graphrag import run_graphrag_setup_mcp_programmatically

        success = run_graphrag_setup_mcp_programmatically(
            install_dir=None,  # Use default ~/graphrag_mcp
            neo4j_uri="bolt://localhost:7687",
            neo4j_user="neo4j",
            neo4j_password="password",
            qdrant_url="http://localhost:6333",
            skip_clone=False,
            silent=True,  # Suppress verbose output during auto-setup
        )

        if not success:
            logger.error("Failed to automatically set up GraphRAG MCP server")
            click.echo("❌ GraphRAG MCP server setup failed")
            click.echo("   Please run 'auto-coder graphrag setup-mcp' manually")
            raise click.ClickException("Failed to set up GraphRAG MCP server. " "Run 'auto-coder graphrag setup-mcp' manually.")

        logger.info("✅ GraphRAG MCP server setup completed successfully")
        click.echo("✅ GraphRAG MCP server setup completed successfully")

    # 2. Initialize GraphRAG environment (Docker, indexing, MCP server)
    try:
        graphrag_integration = GraphRAGMCPIntegration()
        if not graphrag_integration.ensure_ready(force_reindex=force_reindex):
            click.echo()
            click.echo("❌ Failed to initialize GraphRAG environment")
            click.echo()
            click.echo("Troubleshooting tips:")
            click.echo("   1. Start containers manually: auto-coder graphrag start")
            click.echo("   2. Check container status: auto-coder graphrag status")
            click.echo("   3. Check Docker logs: docker-compose -f docker-compose.graphrag.yml logs")
            raise click.ClickException("Failed to initialize GraphRAG environment. " "Run 'auto-coder graphrag start' to start containers.")

        logger.info("GraphRAG environment ready")
        click.echo("✅ GraphRAG environment ready")

        # Optionally run snapshot cleanup after successful initialization
        cleanup_raw = os.environ.get("GRAPHRAG_CLEANUP_ON_INIT", "1")
        cleanup_enabled = cleanup_raw.strip().lower() not in {"0", "false", "no", "off", ""}

        if cleanup_enabled:
            try:
                logger.info("Running GraphRAG snapshot cleanup after initialization...")
                graphrag_integration.run_cleanup(dry_run=False)
            except Exception as cleanup_error:
                logger.warning(f"GraphRAG cleanup during initialization failed: {cleanup_error}")
    except click.ClickException:
        raise
    except Exception as e:
        click.echo()
        click.echo(f"❌ Error initializing GraphRAG: {e}")
        click.echo()
        click.echo("Troubleshooting tips:")
        click.echo("   1. Ensure Docker is running: docker ps")
        click.echo("   2. Check docker-compose.graphrag.yml exists")
        click.echo("   3. Verify GRAPHRAG_MCP_SERVER_PATH if using MCP server")
        raise click.ClickException(f"Error initializing GraphRAG: {e}")


def check_graphrag_mcp_for_backends(backends: list[str], client: Any = None) -> None:
    """Ensure GraphRAG MCP is configured for all selected backends.

    This function checks if graphrag MCP server is configured for each backend,
    and if not, automatically adds the configuration using the client's MCP methods.

    Note: This function assumes that the MCP server is already installed by initialize_graphrag().
    It only adds backend configurations, not the full setup.

    Args:
        backends: List of backend names to check
        client: Optional LLM client instance to use for MCP configuration
    """
    from pathlib import Path

    from .logger_config import get_logger

    logger = get_logger(__name__)
    logger.info("Ensuring GraphRAG MCP configuration for backends...")

    # GraphRAG MCP server configuration
    server_name = "graphrag"
    default_mcp_dir = Path.home() / "graphrag_mcp"

    # Verify MCP server is installed (should be done by initialize_graphrag)
    if not default_mcp_dir.exists():
        logger.warning(f"GraphRAG MCP server directory not found at {default_mcp_dir}. " f"This should have been installed by initialize_graphrag().")
        click.echo()
        click.echo("⚠️  GraphRAG MCP server not found")
        click.echo("   Please run 'auto-coder graphrag setup-mcp' manually")
        click.echo()
        return

    # If client is provided, check if already configured
    if client is not None:
        logger.info(f"Checking GraphRAG MCP configuration for client...")
        if not client.check_mcp_server_configured(server_name):
            logger.info(f"GraphRAG MCP server not configured for client. " f"Adding configuration...")
            click.echo()
            click.echo("⚠️  GraphRAG MCP server not configured for client")
            click.echo("   Adding configuration...")
            click.echo()

            # Add configuration only (server is already installed)
            from .mcp_manager import get_mcp_manager

            manager = get_mcp_manager()
            success = True
            for backend in backends:
                if not manager.add_backend_config(server_name, backend, default_mcp_dir):
                    logger.warning(f"Failed to configure {backend} backend for {server_name}")
                    success = False

            if success:
                logger.info("✅ GraphRAG MCP server configuration added successfully")
                click.echo("✅ GraphRAG MCP server configuration added successfully")
            else:
                logger.error("❌ GraphRAG MCP server configuration failed")
                click.echo("❌ GraphRAG MCP server configuration failed")
                click.echo("   Please run 'auto-coder graphrag setup-mcp' manually")
        else:
            logger.info(f"✅ GraphRAG MCP server configured for client")
    else:
        # Fallback to file-based configuration for each backend
        from .mcp_checker import ensure_graphrag_mcp_configured

        for backend in backends:
            if not ensure_graphrag_mcp_configured(backend, auto_setup=False):
                logger.info(f"GraphRAG MCP server not configured for {backend}. " f"Adding configuration...")
                click.echo()
                click.echo(f"⚠️  GraphRAG MCP server not configured for {backend}")
                click.echo("   Adding configuration...")
                click.echo()

                # Add configuration only (server is already installed)
                from .mcp_manager import get_mcp_manager

                manager = get_mcp_manager()
                success = manager.add_backend_config(server_name, backend, default_mcp_dir)

                if success:
                    logger.info(f"✅ GraphRAG MCP server configuration added successfully for {backend}")
                    click.echo(f"✅ GraphRAG MCP server configuration added successfully for {backend}")
                else:
                    logger.error(f"❌ GraphRAG MCP server configuration failed for {backend}")
                    click.echo(f"❌ GraphRAG MCP server configuration failed for {backend}")
                    click.echo("   Please run 'auto-coder graphrag setup-mcp' manually")
            else:
                logger.info(f"✅ GraphRAG MCP server already configured for {backend}")


def check_gemini_cli_or_fail() -> None:
    """Check if gemini CLI is available and working."""
    check_cli_tool(tool_name="gemini", install_url="https://github.com/google-gemini/gemini-cli\nOr use: npm install -g @google/generative-ai-cli", version_flag="--version")


def check_codex_cli_or_fail() -> None:
    """Check if Codex (or override) CLI is available and working.

    For testing or custom environments, you can override the codex CLI binary
    via environment variable AUTOCODER_CODEX_CLI. When set, we will try to
    execute the command with `--version` first; if that fails, we will run the
    command without arguments as a liveness check.
    """
    check_cli_tool(
        tool_name="codex",
        install_url="https://github.com/openai/codex",
        version_flag="--version",
        cmd_override_env="AUTOCODER_CODEX_CLI",
        fallback_without_args=True,
    )


def check_qwen_cli_or_fail() -> None:
    """Check if qwen CLI is available and working."""
    check_cli_tool(tool_name="qwen", install_url="https://github.com/QwenLM/qwen-code\nOr use: npm install -g @qwen-code/qwen-code", version_flag="--version")


def check_auggie_cli_or_fail() -> None:
    """Check if auggie CLI is available and working."""
    check_cli_tool(tool_name="auggie", install_url="npm install -g @augmentcode/auggie", version_flag="--version")


def check_claude_cli_or_fail() -> None:
    """Check if claude CLI is available and working."""
    check_cli_tool(tool_name="claude", install_url="https://claude.ai/download", version_flag="--version")


def check_cli_tool(
    tool_name: str,
    install_url: str,
    version_flag: str = "--version",
    cmd_override_env: Optional[str] = None,
    fallback_without_args: bool = False,
) -> None:
    """Generic CLI tool checker.

    Args:
        tool_name: Name of the CLI tool to check
        install_url: URL with installation instructions for the tool
        version_flag: Flag to use for version check (default: "--version")
        cmd_override_env: Optional environment variable name that, if set, contains
                         an override command to use instead of the tool name
        fallback_without_args: If True and version check fails, try running without args
                              (useful for some CLIs that don't support --version)

    Raises:
        click.ClickException: If the CLI tool is not available or not working
    """
    # Check if override env var is set
    override = os.environ.get(cmd_override_env) if cmd_override_env else None
    if override:
        cmd = shlex.split(override)
        try:
            result = subprocess.run(cmd + [version_flag], capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                click.echo(f"Using {tool_name} CLI")
                return
        except Exception:
            pass

        # Fallback: try without args if version check fails
        if fallback_without_args:
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                if result.returncode == 0:
                    click.echo(f"Using {tool_name} CLI")
                    return
            except Exception:
                pass

        raise click.ClickException(f"{tool_name} CLI override ({cmd_override_env}) is set but not working")

    # Default: check the actual CLI tool
    try:
        result = subprocess.run([tool_name, version_flag], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            click.echo(f"Using {tool_name} CLI")
            return
    except Exception:
        pass

    raise click.ClickException(f"{tool_name} CLI is required. Please install it from:\n{install_url}")


def build_models_map() -> Dict[str, str]:
    """Compute per-backend model map with sensible defaults.

    Uses configuration file settings with sensible defaults:
      - gemini: gemini-2.5-pro
      - qwen: qwen3-coder-plus
      - auggie: GPT-5
      - claude: sonnet
      - codex/codex-mcp: placeholders (unused by CLI but kept for uniformity)
    """
    config = get_llm_config()

    models: Dict[str, str] = {}
    # codex backends (accepted for compatibility, not actually used by CLI)
    models["codex"] = "codex"
    models["codex-mcp"] = "codex-mcp"
    # gemini - Check config, then default
    models["gemini"] = config.get_model_for_backend("gemini") or "gemini-2.5-pro"
    # qwen
    models["qwen"] = config.get_model_for_backend("qwen") or "qwen3-coder-plus"
    # auggie
    models["auggie"] = config.get_model_for_backend("auggie") or "GPT-5"
    # claude
    models["claude"] = config.get_model_for_backend("claude") or "sonnet"
    return models


def normalize_backends(backends: tuple[str, ...]) -> list[str]:
    """Preserve order, drop duplicates, and ensure at least one backend (default from config)."""
    config = get_llm_config()

    seen: set[str] = set()
    normalized: list[str] = []
    for backend_name in backends:
        if backend_name not in seen:
            normalized.append(backend_name)
            seen.add(backend_name)
    if not normalized:
        normalized.append(config.default_backend)
    return normalized


def check_backend_prerequisites(backends: list[str]) -> None:
    """Verify CLI prerequisites for all requested backends.

    Supports both known backend types (codex, gemini, qwen, auggie, claude)
    and custom backend names that reference a backend_type in configuration.

    Args:
        backends: List of backend names to check

    Raises:
        click.ClickException: If a backend is unsupported or misconfigured
    """
    config = get_llm_config()

    for backend_name in backends:
        # Known backend types
        if backend_name in ("codex", "codex-mcp"):
            check_codex_cli_or_fail()
        elif backend_name == "gemini":
            check_gemini_cli_or_fail()
        elif backend_name == "qwen":
            check_qwen_cli_or_fail()
        elif backend_name == "auggie":
            check_auggie_cli_or_fail()
        elif backend_name == "claude":
            check_claude_cli_or_fail()
        else:
            # Check if it's a custom backend with backend_type
            backend_config = config.get_backend_config(backend_name)
            if backend_config and backend_config.backend_type:
                # Recursively check the backend_type
                check_backend_prerequisites([backend_config.backend_type])
            else:
                raise click.ClickException(f"Unsupported backend specified: {backend_name}. " f"Either use a known backend type (codex, gemini, qwen, auggie, claude) " f"or configure backend_type in llm_config.toml")


def build_backend_manager(
    selected_backends: list[str],
    primary_backend: str,
    models: dict[str, str],
    enable_graphrag: bool = True,
    use_noedit_options: bool = False,
) -> BackendManager:
    """Construct BackendManager with per-backend model selection.

    models: mapping backend -> model_name (all backends respect this configuration).
    enable_graphrag: Enable GraphRAG integration for CodexMCPClient (always True).
    use_noedit_options: If True, use options_for_noedit instead of options for clients.
    """
    config = get_llm_config()

    # Get API keys and base URLs from configuration
    gemini_config = config.get_backend_config("gemini")

    effective_gemini_api_key = gemini_config.api_key if gemini_config else None

    def _gm() -> str:
        return models.get("gemini", "gemini-2.5-pro")

    def _qm() -> str:
        return models.get("qwen", "qwen3-coder-plus")

    def _am() -> str:
        return models.get("auggie", "GPT-5")

    def _cm() -> str:
        return models.get("claude", "sonnet")

    # Create factory functions that support both direct backend names and aliases
    def _create_qwen_client(backend_name: str):
        """Create a QwenClient with options from config."""
        return QwenClient(
            backend_name=backend_name,
            use_env_vars=True,
            preserve_existing_env=False,
        )

    def _create_gemini_client(backend_name: str):
        """Create a GeminiClient."""
        return GeminiClient(backend_name=backend_name)

    def _create_claude_client(backend_name: str):
        """Create a ClaudeClient with optional configuration for aliases."""
        return ClaudeClient(
            backend_name=backend_name,
        )

    def _create_auggie_client(backend_name: str):
        """Create an AuggieClient."""
        return AuggieClient(backend_name=backend_name)

    def _create_codex_client(backend_name: str):
        """Create a CodexClient with optional configuration for aliases."""
        backend_config = config.get_backend_config(backend_name)
        return CodexClient(
            backend_name=backend_name,
            api_key=backend_config.api_key if backend_config else None,
            base_url=backend_config.base_url if backend_config else None,
            openai_api_key=backend_config.openai_api_key if backend_config else None,
            openai_base_url=backend_config.openai_base_url if backend_config else None,
            use_noedit_options=use_noedit_options,
        )

    def _create_codex_mcp_client(backend_name: str):
        """Create a CodexMCPClient."""
        return CodexMCPClient(backend_name=backend_name, enable_graphrag=enable_graphrag)

    # Mapping of backend types to factory functions
    backend_type_factories = {
        "qwen": _create_qwen_client,
        "gemini": _create_gemini_client,
        "claude": _create_claude_client,
        "auggie": _create_auggie_client,
        "codex": _create_codex_client,
        "codex-mcp": _create_codex_mcp_client,
    }

    # Build factory dictionary with support for aliases
    selected_factories = {}
    for backend_name in selected_backends:
        # Check if it's a direct match first
        if backend_name in ["codex", "codex-mcp", "gemini", "qwen", "auggie", "claude"]:
            # Use the appropriate factory based on backend name
            if backend_name == "codex":
                selected_factories[backend_name] = lambda bn=backend_name: _create_codex_client(bn)
            elif backend_name == "codex-mcp":
                selected_factories[backend_name] = lambda bn=backend_name: _create_codex_mcp_client(bn)
            elif backend_name == "gemini":
                selected_factories[backend_name] = lambda bn=backend_name: _create_gemini_client(bn)
            elif backend_name == "qwen":
                selected_factories[backend_name] = lambda bn=backend_name: _create_qwen_client(bn)
            elif backend_name == "auggie":
                selected_factories[backend_name] = lambda bn=backend_name: _create_auggie_client(bn)
            elif backend_name == "claude":
                selected_factories[backend_name] = lambda bn=backend_name: _create_claude_client(bn)
        else:
            # Check if it's an alias (custom backend name)
            backend_config = config.get_backend_config(backend_name)
            if not backend_config:
                raise click.ClickException(f"Backend '{backend_name}' not found in configuration")

            # Get the backend type from config
            backend_type = backend_config.backend_type
            if not backend_type:
                raise click.ClickException(f"Backend '{backend_name}' does not have a 'backend_type' specified in configuration")

            # Check if the backend type has a factory
            if backend_type not in backend_type_factories:
                raise click.ClickException(f"Backend type '{backend_type}' (for alias '{backend_name}') is not supported")

            # Create factory for this alias
            factory_func = backend_type_factories[backend_type]
            selected_factories[backend_name] = lambda bn=backend_name, ff=factory_func: ff(bn)

    # Create default client
    if primary_backend not in selected_factories:
        raise click.ClickException(f"Primary backend '{primary_backend}' is not in selected backends")
    default_client = selected_factories[primary_backend]()

    return BackendManager(
        default_backend=primary_backend,
        default_client=default_client,
        factories=selected_factories,
        order=selected_backends,
    )


def check_github_sub_issue_or_setup() -> None:
    """Check if github-sub-issue tool is available, auto-setup if missing.

    This function checks if the github-sub-issue CLI tool is installed and working.
    If not available, it automatically installs the tool from utils/github-sub-issue.
    """
    try:
        result = subprocess.run(
            ["github-sub-issue", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            click.echo("Using github-sub-issue CLI")
            return
    except Exception:
        pass

    # Try alternative version check
    try:
        result = subprocess.run(
            ["github-sub-issue", "list", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            click.echo("Using github-sub-issue CLI")
            return
    except Exception:
        pass

    # Auto-setup github-sub-issue tool
    click.echo()
    click.echo("⚠️  github-sub-issue tool not found")
    click.echo("   Automatically installing github-sub-issue tool...")
    click.echo()

    utils_dir = Path(__file__).parent.parent.parent / "utils" / "github-sub-issue"
    if not utils_dir.exists():
        raise click.ClickException(f"github-sub-issue source directory not found at {utils_dir}. " "Cannot auto-install.")

    try:
        # Install the tool in editable mode
        result = subprocess.run(
            ["pip", "install", "-e", str(utils_dir)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            click.echo("✅ github-sub-issue tool installed successfully")
            # Verify the tool is actually available after installation
            try:
                verify_result = subprocess.run(
                    ["github-sub-issue", "--version"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if verify_result.returncode == 0:
                    click.echo("✅ github-sub-issue CLI is now available and working")
                    return
                else:
                    click.echo("⚠️  Installation completed but verification failed")
                    raise click.ClickException("github-sub-issue tool installation completed but verification failed")
            except Exception as e:
                raise click.ClickException(f"github-sub-issue tool installation completed but verification failed: {e}")
        else:
            click.echo(f"❌ Installation failed: {result.stderr}")
            raise click.ClickException(f"Failed to install github-sub-issue tool: {result.stderr}")
    except Exception as e:
        click.echo(f"❌ Installation error: {e}")
        raise click.ClickException(f"Failed to install github-sub-issue tool: {e}")


def build_backend_manager_from_config(
    enable_graphrag: bool = True,
    cli_models: Optional[Dict[str, str]] = None,
    cli_backends: Optional[List[str]] = None,
) -> BackendManager:
    """Construct BackendManager using configuration from the TOML file.

    This function creates a BackendManager instance using the configuration
    specified in the TOML configuration file, with optional CLI overrides.

    Args:
        enable_graphrag: Enable GraphRAG integration for CodexMCPClient (always True)
        cli_models: Dictionary mapping backend names to models specified via CLI, which will
                   override both config file and default models (optional)
        cli_backends: List of backend names specified via CLI. If provided, only these
                     backends will be included in the manager. If None, uses all active
                     backends from the configuration file.

    Returns:
        BackendManager: The configured backend manager instance
    """
    config = get_llm_config()

    # Get active backends from configuration, filtered by CLI backends if provided
    all_configured_backends = config.get_active_backends()
    if cli_backends:
        # Filter to only CLI-specified backends that are also enabled in config
        selected_backends = [backend for backend in cli_backends if backend in all_configured_backends]
        # Primary backend should be the first CLI-specified backend that's enabled
        primary_backend = next((backend for backend in cli_backends if backend in all_configured_backends), config.default_backend)
    else:
        # Use all configured backends
        selected_backends = all_configured_backends
        primary_backend = config.default_backend

    # Build models map from configuration
    models = {}
    for backend_name in selected_backends:
        # CLI models take precedence over config file models
        if cli_models and backend_name in cli_models:
            model_value = cli_models[backend_name] or backend_name  # Ensure non-None value
        else:
            model_value = config.get_model_for_backend(backend_name) or backend_name
        models[backend_name] = model_value

    return build_backend_manager(
        selected_backends=selected_backends,
        primary_backend=primary_backend,
        models=models,
        enable_graphrag=enable_graphrag,
    )


def qwen_help_has_flags(required_flags: list[str]) -> bool:
    """Lightweight probe for qwen --help to verify presence of required flags.

    Tolerates short/long form equivalence, e.g. "-p" <-> "--prompt", "-m" <-> "--model".
    Returns False on any error; intended for tests and optional diagnostics. Fully mocked in CI.
    """
    try:
        import re as _re

        res = subprocess.run(["qwen", "--help"], capture_output=True, text=True, timeout=10)
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


def build_message_backend_manager(
    selected_backends: Optional[list[str]] = None,
    primary_backend: Optional[str] = None,
    models: Optional[dict[str, str]] = None,
) -> BackendManager:
    """Construct and initialize LLMBackendManager singleton with selected backends.

    This function creates a LLMBackendManager singleton instance dedicated to message
    generation (commit messages, PR messages, etc.) using the specified backend configuration.

    Args:
        selected_backends: List of backend names in priority order (defaults to config)
        primary_backend: Primary backend name (defaults to config)
        models: mapping backend -> model_name (defaults to config)

    Returns:
        BackendManager: The singleton instance for message generation operations

    Raises:
        click.ClickException: If backend configuration is invalid
    """
    from .backend_manager import LLMBackendManager

    config = get_llm_config()

    # Log if dual configuration is detected
    if config.has_dual_configuration():
        from .logger_config import get_logger

        logger = get_logger(__name__)
        logger.info("Dual backend configuration detected - using separate settings for message generation")

    # Use configuration values as defaults if not provided
    if selected_backends is None:
        selected_backends = config.get_active_noedit_backends()
    if primary_backend is None:
        primary_backend = config.get_noedit_default_backend()
    if models is None:
        # Build models map using configuration
        models = {}
        for backend_name in selected_backends:
            models[backend_name] = config.get_model_for_backend(backend_name) or backend_name

    # Create a backend manager with the appropriate configuration
    # This will be used to initialize the singleton
    temp_backend_manager = build_backend_manager(
        selected_backends=selected_backends,
        primary_backend=primary_backend,
        models=models,
        enable_graphrag=False,  # GraphRAG not needed for messages
        use_noedit_options=True,  # Use noedit options for message generation
    )

    # Get the default client and factories to initialize the singleton
    default_client = temp_backend_manager._clients[primary_backend]
    factories = temp_backend_manager._factories

    # Initialize the noedit singleton instance
    LLMBackendManager.get_noedit_instance(
        default_backend=primary_backend,
        default_client=default_client,
        factories=factories,
        order=selected_backends,
    )

    # Get and return the already initialized noedit instance
    return LLMBackendManager.get_noedit_instance()
