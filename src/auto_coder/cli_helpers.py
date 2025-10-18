"""CLI helper functions for backend management and validation."""

import os
import shlex
import subprocess
from typing import Any, Optional

import click

from .auggie_client import AuggieClient
from .automation_config import AutomationConfig
from .backend_manager import BackendManager
from .codex_client import CodexClient
from .codex_mcp_client import CodexMCPClient
from .gemini_client import GeminiClient
from .qwen_client import QwenClient


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


def initialize_graphrag() -> None:
    """Initialize GraphRAG integration (always enabled).

    This function ensures GraphRAG environment is ready:
    - Starts Docker containers if not running
    - Updates index if outdated
    - Starts MCP server if configured

    Raises:
        click.ClickException: If GraphRAG initialization fails
    """
    from .graphrag_mcp_integration import GraphRAGMCPIntegration
    from .logger_config import get_logger

    logger = get_logger(__name__)
    logger.info("Initializing GraphRAG integration...")
    click.echo("GraphRAG integration: enabled (always)")
    try:
        graphrag_integration = GraphRAGMCPIntegration()
        if not graphrag_integration.ensure_ready():
            click.echo()
            click.echo("❌ Failed to initialize GraphRAG environment")
            click.echo()
            click.echo("Troubleshooting tips:")
            click.echo("   1. Start containers manually: auto-coder graphrag start")
            click.echo("   2. Check container status: auto-coder graphrag status")
            click.echo("   3. Check Docker logs: docker-compose -f docker-compose.graphrag.yml logs")
            raise click.ClickException(
                "Failed to initialize GraphRAG environment. "
                "Run 'auto-coder graphrag start' to start containers."
            )
        logger.info("GraphRAG environment ready")
        click.echo("✅ GraphRAG environment ready")
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

    Args:
        backends: List of backend names to check
        client: Optional LLM client instance to use for MCP configuration
    """
    from .logger_config import get_logger

    logger = get_logger(__name__)
    logger.info("Ensuring GraphRAG MCP configuration for backends...")

    # GraphRAG MCP server configuration
    server_name = "graphrag"
    command = "npx"
    args = ["-y", "@modelcontextprotocol/server-graphrag"]

    # If client is provided, use its MCP methods
    if client is not None:
        logger.info(f"Checking GraphRAG MCP configuration for client...")
        if not client.ensure_mcp_server_configured(server_name, command, args):
            logger.error(
                f"Failed to ensure GraphRAG MCP configuration for client. "
                f"Please configure manually."
            )
            exit(1)
        else:
            logger.info(f"✅ GraphRAG MCP server configured for client")
    else:
        # Fallback to file-based configuration for each backend
        from .mcp_checker import ensure_graphrag_mcp_configured

        for backend in backends:
            ensure_graphrag_mcp_configured(backend)


def check_gemini_cli_or_fail() -> None:
    """Check if gemini CLI is available and working."""
    try:
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


def build_models_map(
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


def normalize_backends(backends: tuple[str, ...]) -> list[str]:
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


def check_backend_prerequisites(backends: list[str]) -> None:
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


def build_backend_manager(
    selected_backends: list[str],
    primary_backend: str,
    models: dict[str, str],
    gemini_api_key: Optional[str],
    openai_api_key: Optional[str],
    openai_base_url: Optional[str],
    enable_graphrag: bool = True,
) -> BackendManager:
    """Construct BackendManager with per-backend model selection.

    models: mapping backend -> model_name (codex backends ignored but accepted).
    enable_graphrag: Enable GraphRAG integration for CodexMCPClient (always True).
    """

    def _gm() -> str:
        return models.get("gemini", "gemini-2.5-pro")

    def _qm() -> str:
        return models.get("qwen", "qwen3-coder-plus")

    def _am() -> str:
        return models.get("auggie", "GPT-5")

    factories_all = {
        "codex": lambda: CodexClient(model_name="codex"),
        "codex-mcp": lambda: CodexMCPClient(
            model_name="codex-mcp", enable_graphrag=enable_graphrag
        ),
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

