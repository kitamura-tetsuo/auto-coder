"""CLI helper functions for backend management and validation."""

import os
import shlex
import shutil
import subprocess
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, cast

import click

from .automation_config import AutomationConfig
from .backend_manager import BackendManager
from .llm_backend_config import get_llm_config

# Backend client modules are imported lazily inside their factory functions.
# Importing them eagerly pulls in heavy optional dependencies (e.g. aider and
# google.generativeai) on every CLI invocation, even though a single run only
# ever instantiates one backend.


def ensure_test_script_or_fail() -> None:
    """Ensure TEST_SCRIPT_PATH exists; error early if missing.
    This check runs at CLI startup for commands that may run tests.
    """
    cfg = AutomationConfig()
    script_path = cfg.TEST_SCRIPT_PATH
    if not os.path.exists(script_path):
        click.echo(f"⚠️  Required test script not found: {script_path}. Entering Jules dedicated mode (remote execution only).")
        # Update singleton if it exists, or just set it on the next instance
        cfg.JULES_ONLY_MODE = True
        # Also ensure the environment variable is set for any subprocesses or re-initializations
        os.environ["AUTOCODER_JULES_ONLY_MODE"] = "true"


def check_gemini_cli_or_fail() -> None:
    """Check if antigravity CLI is available and working."""
    check_cli_tool(
        tool_name="agy",
        install_url="https://antigravity.google/download#antigravity-cli\nOr use: curl -fsSL https://antigravity.google/cli/install.sh | bash",
        version_flag="--version",
        cmd_override_env="AUTOCODER_GEMINI_CLI",
    )


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
    check_cli_tool(tool_name="claude", install_url="https://claude.ai/download\nOr use: npm install -g @anthropic-ai/claude-code", version_flag="--version")


def check_aider_cli_or_fail() -> None:
    """Check if aider CLI is available and working."""
    # Note: aider is typically a python library but it does have a CLI
    check_cli_tool(tool_name="aider", install_url="pip install aider-chat", version_flag="--version")


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
    if not cmd_override_env:
        cmd_override_env = f"AUTOCODER_{tool_name.upper()}_CLI"

    # Handle docker execution if AM_I_AUTOCODER_CONTAINER=true
    if os.environ.get("AM_I_AUTOCODER_CONTAINER") == "true":
        from .utils import get_target_container

        target_container = get_target_container(None)
        if target_container:
            override_val = f"docker exec -i {target_container} {tool_name}"
            os.environ[cmd_override_env] = override_val

            # Check if it exists in the container
            cmd = shlex.split(override_val)
            try:
                res = subprocess.run(cmd + [version_flag], capture_output=True, text=True, timeout=60)
                if res.returncode != 0 and fallback_without_args:
                    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

                if res.returncode != 0:
                    click.echo(f"Installing {tool_name} inside {target_container}...")

                    # Extract install command from install_url
                    install_cmd = None
                    if "npm install" in install_url:
                        for line in install_url.split("\n"):
                            if "npm install" in line:
                                install_cmd = shlex.split(line.strip().replace("Or use: ", ""))
                                break
                    elif "pip install" in install_url:
                        for line in install_url.split("\n"):
                            if "pip install" in line:
                                install_cmd = shlex.split(line.strip().replace("Or use: ", ""))
                                break

                    if install_cmd:
                        subprocess.run(["docker", "exec", "-i", target_container] + install_cmd, check=True)
                        # Re-verify post installation
                        verify_res = subprocess.run(cmd + [version_flag], capture_output=True, text=True, timeout=60)
                        if verify_res.returncode != 0:
                            raise RuntimeError(f"Installation succeeded but {tool_name} is still failing in {target_container}")
                    else:
                        raise click.ClickException(f"Cannot auto-install {tool_name} inside container, missing install command in: {install_url}")
            except Exception as e:
                raise click.ClickException(f"Failed to check/install {tool_name} in target container {target_container}: {e}")

    # Check if override env var is set
    override = os.environ.get(cmd_override_env)
    if override:
        cmd = shlex.split(override)
        try:
            result = subprocess.run(cmd + [version_flag], capture_output=True, text=True, timeout=60)
            if result.returncode == 0:
                click.echo(f"Using {tool_name} CLI (override: {override})")
                return
        except Exception:
            pass

        # Fallback: try without args if version check fails
        if fallback_without_args:
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                if result.returncode == 0:
                    click.echo(f"Using {tool_name} CLI (override: {override})")
                    return
            except Exception:
                pass

        raise click.ClickException(f"{tool_name} CLI override ({cmd_override_env}) is set to '{override}' but it is not working.")

    # Default: check the actual CLI tool
    try:
        # Check if the tool exists in PATH first
        if not shutil.which(tool_name):
            raise click.ClickException(f"{tool_name} CLI is not found in PATH. Please install it from:\n{install_url}")

        result = subprocess.run([tool_name, version_flag], capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            click.echo(f"Using {tool_name} CLI")
            return
        else:
            # Tool exists but version check failed - provide diagnostics
            stdout = (result.stdout or "").strip()[:200]
            stderr = (result.stderr or "").strip()[:200]
            diag = []
            if stdout:
                diag.append(f"stdout: {stdout}")
            if stderr:
                diag.append(f"stderr: {stderr}")
            diag_str = " | ".join(diag)
            raise click.ClickException(f"{tool_name} CLI found but '{tool_name} {version_flag}' failed (exit code {result.returncode}).\n" f"Diagnostics: {diag_str}\n" f"Please ensure it is working correctly or reinstall from:\n{install_url}")
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(f"Error checking {tool_name} CLI: {e}\nPlease install it from:\n{install_url}")


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
    models["antigravity"] = config.get_model_for_backend("antigravity") or "gemini-2.5-pro"
    # qwen
    models["qwen"] = config.get_model_for_backend("qwen") or "qwen3-coder-plus"
    # auggie
    models["auggie"] = config.get_model_for_backend("auggie") or "GPT-5"
    # claude
    models["claude"] = config.get_model_for_backend("claude") or "sonnet"
    # aider
    models["aider"] = config.get_model_for_backend("aider") or "aider"
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

    # Handle legacy "gemini" backend name translation
    backends = ["antigravity" if b == "gemini" else b for b in backends]

    for backend_name in backends:
        # Known backend types
        if backend_name in ("codex", "codex-mcp"):
            check_codex_cli_or_fail()
        elif backend_name == "antigravity":
            check_gemini_cli_or_fail()
        elif backend_name == "qwen":
            check_qwen_cli_or_fail()
        elif backend_name == "auggie":
            check_auggie_cli_or_fail()
        elif backend_name == "claude":
            check_claude_cli_or_fail()
        elif backend_name == "aider":
            check_aider_cli_or_fail()
        elif backend_name == "codex-cloud":
            check_codex_cli_or_fail()
        elif backend_name in ("jules", "claude-routine"):
            pass  # Cloud/API based, no local CLI binary needed
        else:
            # Check if it's a custom backend with backend_type
            backend_config = config.get_backend_config(backend_name)
            if backend_config and backend_config.backend_type:
                # Recursively check the backend_type
                check_backend_prerequisites([backend_config.backend_type])
            else:
                raise click.ClickException(f"Unsupported backend specified: {backend_name}. " f"Either use a known backend type (codex, antigravity, qwen, auggie, claude, claude-routine, codex-cloud) " f"or configure backend_type in llm_config.toml")


def build_backend_manager(
    selected_backends: list[str],
    primary_backend: str,
    models: dict[str, str],
    use_noedit_options: bool = False,
) -> BackendManager:
    # Handle legacy "gemini" backend name translation
    selected_backends = ["antigravity" if b == "gemini" else b for b in selected_backends]
    if primary_backend == "gemini":
        primary_backend = "antigravity"

    """Construct BackendManager with per-backend model selection.

    models: mapping backend -> model_name (all backends respect this configuration).
    use_noedit_options: If True, use options_for_noedit instead of options for clients.
    """
    config = get_llm_config()

    # Get API keys and base URLs from configuration
    gemini_config = config.get_backend_config("antigravity")

    effective_gemini_api_key = gemini_config.api_key if gemini_config else None

    def _gm() -> str:
        return models.get("antigravity", "gemini-2.5-pro")

    def _qm() -> str:
        return models.get("qwen", "qwen3-coder-plus")

    def _am() -> str:
        return models.get("auggie", "GPT-5")

    def _cm() -> str:
        return models.get("claude", "sonnet")

    def _aiderm() -> str:
        return models.get("aider", "aider")

    # Create factory functions that support both direct backend names and aliases
    def _create_qwen_client(backend_name: str):
        """Create a QwenClient with options from config."""
        from .qwen_client import QwenClient

        return QwenClient(
            backend_name=backend_name,
            use_env_vars=True,
            preserve_existing_env=False,
        )

    def _create_gemini_client(backend_name: str):
        """Create a GeminiClient."""
        from .gemini_client import GeminiClient

        return GeminiClient(backend_name=backend_name)

    def _create_claude_client(backend_name: str):
        """Create a ClaudeClient with optional configuration for aliases."""
        from .claude_client import ClaudeClient

        return ClaudeClient(
            backend_name=backend_name,
        )

    def _create_auggie_client(backend_name: str):
        """Create an AuggieClient."""
        from .auggie_client import AuggieClient

        return AuggieClient(backend_name=backend_name)

    def _create_codex_client(backend_name: str):
        """Create a CodexClient with optional configuration for aliases."""
        from .codex_client import CodexClient

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
        from .codex_mcp_client import CodexMCPClient

        return CodexMCPClient(backend_name=backend_name)

    def _create_aider_client(backend_name: str):
        """Create an AiderClient."""
        from .aider_client import AiderClient

        backend_config = config.get_backend_config(backend_name)
        return AiderClient(
            backend_name=backend_name,
            api_key=backend_config.api_key if backend_config else None,
            base_url=backend_config.base_url if backend_config else None,
            openai_api_key=backend_config.openai_api_key if backend_config else None,
            openai_base_url=backend_config.openai_base_url if backend_config else None,
            use_noedit_options=use_noedit_options,
        )

    def _create_claude_routine_client(backend_name: str, use_noedit_options: bool = False) -> Any:
        """Create a ClaudeRoutineClient."""
        from .claude_routine_client import ClaudeRoutineClient

        return ClaudeRoutineClient(backend_name=backend_name)

    def _create_codex_cloud_client(backend_name: str, use_noedit_options: bool = False) -> Any:
        """Create a CodexCloudClient."""
        from .codex_cloud_client import CodexCloudClient

        return CodexCloudClient(backend_name=backend_name)

    # Mapping of backend types to factory functions
    backend_type_factories: Dict[str, Callable[..., Any]] = {
        "qwen": _create_qwen_client,
        "antigravity": _create_gemini_client,
        "claude": _create_claude_client,
        "auggie": _create_auggie_client,
        "codex": _create_codex_client,
        "codex-mcp": _create_codex_mcp_client,
        "aider": _create_aider_client,
        "claude-routine": _create_claude_routine_client,
        "codex-cloud": _create_codex_cloud_client,
    }

    # Build factory dictionary with support for aliases
    selected_factories: Dict[str, Callable[[], Any]] = {}
    for backend_name in selected_backends:
        # Check if it's a direct match first
        if backend_name in ["codex", "codex-mcp", "antigravity", "qwen", "auggie", "claude", "aider", "claude-routine", "codex-cloud"]:
            # Use the appropriate factory based on backend name
            if backend_name == "codex":
                selected_factories[backend_name] = cast(Callable[[], Any], partial(_create_codex_client, backend_name))
            elif backend_name == "codex-mcp":
                selected_factories[backend_name] = cast(Callable[[], Any], partial(_create_codex_mcp_client, backend_name))
            elif backend_name == "antigravity":
                selected_factories[backend_name] = cast(Callable[[], Any], partial(_create_gemini_client, backend_name))
            elif backend_name == "qwen":
                selected_factories[backend_name] = cast(Callable[[], Any], partial(_create_qwen_client, backend_name))
            elif backend_name == "auggie":
                selected_factories[backend_name] = cast(Callable[[], Any], partial(_create_auggie_client, backend_name))
            elif backend_name == "claude":
                selected_factories[backend_name] = cast(Callable[[], Any], partial(_create_claude_client, backend_name))
            elif backend_name == "aider":
                selected_factories[backend_name] = cast(Callable[[], Any], partial(_create_aider_client, backend_name))
            elif backend_name == "claude-routine":
                selected_factories[backend_name] = cast(Callable[[], Any], partial(_create_claude_routine_client, backend_name))
            elif backend_name == "codex-cloud":
                selected_factories[backend_name] = cast(Callable[[], Any], partial(_create_codex_cloud_client, backend_name))
        else:
            backend_config = config.get_backend_config(backend_name)
            if backend_config:
                backend_type = backend_config.backend_type
                if not backend_type:
                    raise click.ClickException(f"Backend '{backend_name}' does not have a 'backend_type' specified in configuration")

                # Check if the backend type has a factory
                if backend_type not in backend_type_factories:
                    raise click.ClickException(f"Backend type '{backend_type}' (for alias '{backend_name}') is not supported")

                # Create factory for this alias
                factory_func = backend_type_factories[backend_type]
                selected_factories[backend_name] = cast(Callable[[], Any], partial(factory_func, backend_name))
            else:
                # Fallback or error if config missing?
                raise click.ClickException(f"Backend '{backend_name}' not found in configuration")

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
            timeout=60,
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
            timeout=60,
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
                    timeout=60,
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
    cli_models: Optional[Dict[str, str]] = None,
    cli_backends: Optional[List[str]] = None,
) -> BackendManager:
    """Construct BackendManager using configuration from the TOML file.

    This function creates a BackendManager instance using the configuration
    specified in the TOML configuration file, with optional CLI overrides.

    Args:
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
    # Get active backends from configuration, filtered by CLI backends if provided
    all_configured_backends = config.get_active_backends()
    if cli_backends:
        # Filter to only CLI-specified backends that are also enabled in config OR exist in backends map
        selected_backends = []
        for backend in cli_backends:
            if backend in all_configured_backends or config.get_backend_config(backend):
                selected_backends.append(backend)

        # Primary backend should be the first CLI-specified valid backend
        primary_backend = next((b for b in selected_backends), config.default_backend)
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
    )


def qwen_help_has_flags(required_flags: list[str]) -> bool:
    """Lightweight probe for qwen --help to verify presence of required flags.

    Tolerates short/long form equivalence, e.g. "-p" <-> "--prompt", "-m" <-> "--model".
    Returns False on any error; intended for tests and optional diagnostics. Fully mocked in CI.
    """
    try:
        import re as _re

        res = subprocess.run(["qwen", "--help"], capture_output=True, text=True, timeout=60)
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


def create_high_score_backend_manager() -> Optional[BackendManager]:
    """Create a BackendManager for the backend_with_high_score configuration.

    Returns:
        BackendManager instance if backend_with_high_score is configured, None otherwise.
    """
    config = get_llm_config()

    # Check for order first
    high_score_order = config.backend_with_high_score_order
    high_score_config = config.get_backend_with_high_score()

    if not high_score_order and not high_score_config:
        return None

    # If order is present, rank candidates by quota surplus
    if high_score_order:
        from .quota_selector import rank_high_score_backends_by_quota

        selected_backends = rank_high_score_backends_by_quota(high_score_order, config) or high_score_order
        primary_backend = selected_backends[0]

        # Build models map for these backends
        models = {}
        for backend_name in selected_backends:
            models[backend_name] = config.get_model_for_backend(backend_name) or backend_name

    else:
        # Fallback to single backend config (legacy behavior)
        assert high_score_config is not None
        backend_name = high_score_config.name
        selected_backends = [backend_name]
        primary_backend = backend_name

        # Determine model: use configured model or fallback to name
        model = high_score_config.model or backend_name
        models = {backend_name: model}

    try:
        return build_backend_manager(
            selected_backends=selected_backends,
            primary_backend=primary_backend,
            models=models,
        )
    except Exception as e:
        from .logger_config import get_logger

        logger = get_logger(__name__)
        logger.error(f"Failed to create backend manager for high score backend: {e}")
        return None


def create_high_score_cloud_backend_manager() -> Optional[BackendManager]:
    """Create a BackendManager for the backend_with_high_score_cloud configuration.

    Returns:
        BackendManager instance if backend_with_high_score_cloud is configured, None otherwise.
    """
    config = get_llm_config()

    # Check for order first
    high_score_cloud_order = config.backend_with_high_score_cloud_order
    high_score_cloud_config = config.get_backend_with_high_score_cloud()

    if not high_score_cloud_order and not high_score_cloud_config:
        return None

    # If order is present, rank candidates by quota surplus
    if high_score_cloud_order:
        from .quota_selector import rank_high_score_backends_by_quota

        selected_backends = rank_high_score_backends_by_quota(high_score_cloud_order, config) or high_score_cloud_order
        primary_backend = selected_backends[0]

        # Build models map for these backends
        models = {}
        for backend_name in selected_backends:
            models[backend_name] = config.get_model_for_backend(backend_name) or backend_name

    else:
        # Fallback to single backend config
        assert high_score_cloud_config is not None
        backend_name = high_score_cloud_config.name
        selected_backends = [backend_name]
        primary_backend = backend_name

        # Determine model: use configured model or fallback to name
        model = high_score_cloud_config.model or backend_name
        models = {backend_name: model}

    try:
        return build_backend_manager(
            selected_backends=selected_backends,
            primary_backend=primary_backend,
            models=models,
        )
    except Exception as e:
        from .logger_config import get_logger

        logger = get_logger(__name__)
        logger.error(f"Failed to create backend manager for high score cloud backend: {e}")
        return None


def create_cloud_backend_manager() -> Optional[BackendManager]:
    """Create a BackendManager for the backend_cloud configuration.

    Returns:
        BackendManager instance if backend_cloud is configured, None otherwise.
    """
    config = get_llm_config()

    # Check for order first
    cloud_order = config.backend_cloud_order
    cloud_config = config.get_backend_cloud()

    if not cloud_order and not cloud_config:
        return None

    # If order is present, rank candidates by quota surplus
    if cloud_order:
        from .quota_selector import rank_high_score_backends_by_quota

        selected_backends = rank_high_score_backends_by_quota(cloud_order, config) or cloud_order
        primary_backend = selected_backends[0]

        # Build models map for these backends
        models = {}
        for backend_name in selected_backends:
            models[backend_name] = config.get_model_for_backend(backend_name) or backend_name

    else:
        # Fallback to single backend config
        assert cloud_config is not None
        backend_name = cloud_config.name
        selected_backends = [backend_name]
        primary_backend = backend_name

        # Determine model: use configured model or fallback to name
        model = cloud_config.model or backend_name
        models = {backend_name: model}

    try:
        return build_backend_manager(
            selected_backends=selected_backends,
            primary_backend=primary_backend,
            models=models,
        )
    except Exception as e:
        from .logger_config import get_logger

        logger = get_logger(__name__)
        logger.error(f"Failed to create backend manager for cloud backend: {e}")
        return None


READ_ONLY_REVIEW_CAPABLE_TYPES = {"claude", "codex"}


def get_effective_backend_type(backend_name: Optional[str], config: Optional[Any] = None) -> Optional[str]:
    """Resolve the underlying backend_type for a backend name/alias."""
    if not backend_name or not isinstance(backend_name, str):
        return None
    if config is not None:
        try:
            b_cfg = config.get_backend_config(backend_name)
            if b_cfg and isinstance(getattr(b_cfg, "backend_type", None), str) and b_cfg.backend_type:
                return b_cfg.backend_type
        except Exception:
            pass
    return backend_name


def is_read_only_review_capable_backend(backend_name: Optional[str], config: Optional[Any] = None) -> bool:
    """Check if a backend provides synchronous read-only review execution based on resolved backend_type.

    Only local clients with proven client-level read-only sandboxing (Claude, Codex)
    are permitted for adversarial validation. Cloud agents (CodexCloud, ClaudeRoutine, Jules),
    MCP variants without sandbox sanitization (CodexMCP), and non-enforcing clients are rejected.
    """
    effective_type = get_effective_backend_type(backend_name, config)
    if not effective_type or not isinstance(effective_type, str):
        return False
    normalized = effective_type.strip().lower().replace("-", "_")
    return normalized in READ_ONLY_REVIEW_CAPABLE_TYPES


def create_adversarial_validation_backend_manager() -> Optional[BackendManager]:
    """Create a BackendManager for the adversarial validation configuration.

    Uses dedicated [backend_adversarial_validation] settings if configured,
    or falls back to high-score order, filtering strictly for backends whose effective
    backend_type supports synchronous read-only review capability.
    Cloud coding backends, codex-mcp, and write-capable clients are rejected.

    Returns:
        BackendManager instance configured strictly with read-only capable models,
        or None if no read-only capable backend is available (fail-closed).
    """
    config = get_llm_config()
    if config is None:
        return None

    adv_order = config.get_adversarial_validation_backend_order()
    adv_config = config.get_backend_adversarial_validation()

    candidates: List[str] = []
    if adv_order and isinstance(adv_order, list):
        candidates = adv_order
    elif adv_config and hasattr(adv_config, "name"):
        candidates = [adv_config.name]
    else:
        # Fallback to high score order if defined
        if hasattr(config, "get_high_score_backend_order"):
            candidates = config.get_high_score_backend_order() or []
        elif hasattr(config, "backend_with_high_score_order"):
            high_score_order = getattr(config, "backend_with_high_score_order", None)
            if isinstance(high_score_order, list):
                candidates = high_score_order

    if not candidates:
        return None

    # Strict capability filter on EVERY backend in the candidate list
    capable_backends = [b for b in candidates if is_read_only_review_capable_backend(b, config)]

    if not capable_backends:
        return None

    from .quota_selector import rank_high_score_backends_by_quota

    selected_backends = rank_high_score_backends_by_quota(capable_backends, config) or capable_backends
    primary_backend = selected_backends[0]

    models = {}
    for backend_name in selected_backends:
        models[backend_name] = config.get_model_for_backend(backend_name) or backend_name

    try:
        return build_backend_manager(
            selected_backends=selected_backends,
            primary_backend=primary_backend,
            models=models,
        )
    except Exception as e:
        from .logger_config import get_logger

        logger = get_logger(__name__)
        logger.warning(f"Failed to create backend manager for adversarial validation: {e}")
        return None
