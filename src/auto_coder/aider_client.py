"""
Aider CLI client for Auto-Coder.
"""

import contextlib
import io
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from .exceptions import AutoCoderTimeoutError, AutoCoderUsageLimitError
from .llm_backend_config import get_llm_config
from .llm_client_base import LLMClientBase
from .llm_output_logger import LLMOutputLogger
from .logger_config import get_logger
from .usage_marker_utils import has_usage_marker_match
from .utils import CommandExecutor

try:
    from aider.coders import Coder
    from aider.io import InputOutput
    from aider.models import Model
except ImportError:
    Coder = None
    Model = None
    InputOutput = None

logger = get_logger(__name__)


class AiderClient(LLMClientBase):
    """Aider CLI client for analyzing issues and generating solutions."""

    def __init__(
        self,
        backend_name: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        openai_api_key: Optional[str] = None,
        openai_base_url: Optional[str] = None,
        openrouter_api_key: Optional[str] = None,
        openrouter_base_url: Optional[str] = None,
        use_noedit_options: bool = False,
    ) -> None:
        """Initialize Aider CLI client.

        Args:
            backend_name: Backend name to use for configuration lookup (optional).
                         If provided, will use config for this backend.
            api_key: API key for the backend (optional, for custom backends).
            base_url: Base URL for the backend (optional, for custom backends).
            openai_api_key: OpenAI API key (optional, for OpenAI-compatible backends).
            openai_base_url: O  penAI base URL (optional, for OpenAI-compatible backends).
            openrouter_api_key: OpenRouter API key (optional, for OpenRouter-compatible backends).
            openrouter_base_url: OpenRouter base URL (optional, for OpenRouter-compatible backends).
            use_noedit_options: If True, use options_for_noedit instead of options.
        """
        super().__init__()
        config = get_llm_config()

        # If backend_name is provided, get config from that backend
        if backend_name:
            self.config_backend = config.get_backend_config(backend_name)
            # Use backend config model, fall back to default "aider"
            self.model_name = (self.config_backend and self.config_backend.model) or "aider"
            # Use options_for_noedit if use_noedit_options is True
            if use_noedit_options and self.config_backend and self.config_backend.options_for_noedit:
                self.options = self.config_backend.options_for_noedit
            else:
                self.options = (self.config_backend and self.config_backend.options) or []
            self.options_for_noedit = (self.config_backend and self.config_backend.options_for_noedit) or []
            self.api_key = api_key or (self.config_backend and self.config_backend.api_key)
            self.base_url = base_url or (self.config_backend and self.config_backend.base_url)
            self.openai_api_key = openai_api_key or (self.config_backend and self.config_backend.openai_api_key)
            self.openai_base_url = openai_base_url or (self.config_backend and self.config_backend.openai_base_url)
            self.openrouter_api_key = openrouter_api_key or (self.config_backend and self.config_backend.openrouter_api_key)
            self.openrouter_base_url = openrouter_base_url or (self.config_backend and self.config_backend.openrouter_base_url)
            self.model_provider = self.config_backend and self.config_backend.model_provider
            # Store usage_markers from config
            self.usage_markers = (self.config_backend and self.config_backend.usage_markers) or []
        else:
            # Fall back to default aider config
            self.config_backend = config.get_backend_config("aider")
            self.model_name = (self.config_backend and self.config_backend.model) or "aider"
            # Use options_for_noedit if use_noedit_options is True
            if use_noedit_options and self.config_backend and self.config_backend.options_for_noedit:
                self.options = self.config_backend.options_for_noedit
            else:
                self.options = (self.config_backend and self.config_backend.options) or []
            self.options_for_noedit = (self.config_backend and self.config_backend.options_for_noedit) or []
            self.api_key = api_key
            self.base_url = base_url
            self.openai_api_key = openai_api_key
            self.openai_base_url = openai_base_url
            self.openrouter_api_key = openrouter_api_key
            self.openrouter_base_url = openrouter_base_url
            self.model_provider = None
            # Store usage_markers from config
            self.usage_markers = (self.config_backend and self.config_backend.usage_markers) or []

        self.default_model = self.model_name
        self.conflict_model = self.model_name
        self.timeout = None

        # Validate required options for this backend
        if self.config_backend:
            required_errors = self.config_backend.validate_required_options()
            if required_errors:
                for error in required_errors:
                    logger.warning(error)

        # Initialize LLM output logger
        self.output_logger = LLMOutputLogger()

        # Check if aider CLI is available
        # Check if aider library is available
        if Coder is None:
            raise RuntimeError("aider-chat library not installed. Please install it to use AiderClient.")

    def switch_to_conflict_model(self) -> None:
        """No-op; aider has no model switching."""
        logger.info("AiderClient: switch_to_conflict_model noop")

    def switch_to_default_model(self) -> None:
        """No-op; aider has no model switching."""
        logger.info("AiderClient: switch_to_default_model noop")

    def _escape_prompt(self, prompt: str) -> str:
        """Escape special characters that may confuse shell/CLI."""
        # Aider handles its own escaping usually, but we might need some basic ones if passing via shell
        # For now, we'll just return as is or do minimal escaping if needed.
        # Codex client escapes @, let's do the same just in case.
        return prompt.replace("@", "\\@").strip()

    def _apply_options_to_env(self, options: list[str], env_vars: dict[str, str]) -> None:
        """Map CLI options to Aider environment variables."""
        i = 0
        while i < len(options):
            opt = options[i]
            if not opt.startswith("--"):
                i += 1
                continue

            key = opt.lstrip("-").replace("-", "_").upper()
            env_key = f"AIDER_{key}"

            # Check if this option is a flag or takes a value
            # Heuristic: if next element exists and doesn't start with -, it's a value.
            # Exception: --no-foo is always a boolean flag (false).

            if opt.startswith("--no-"):
                # Handle --no-foo -> AIDER_FOO=false
                # key is NO_FOO. We want FOO.
                real_key = key.replace("NO_", "", 1)
                env_vars[f"AIDER_{real_key}"] = "false"
                i += 1
            else:
                # Check for value
                if i + 1 < len(options) and not options[i + 1].startswith("-"):
                    val = options[i + 1]
                    env_vars[env_key] = val
                    i += 2
                else:
                    # Boolean flag
                    env_vars[env_key] = "true"
                    i += 1

    def _run_llm_cli(self, prompt: str, is_noedit: bool = False) -> str:
        """Run aider CLI with the given prompt and show real-time output."""
        start_time = time.time()
        status = "success"
        error_message = None
        full_output = ""

        try:
            escaped_prompt = self._escape_prompt(prompt)
            # cmd = ["aider"] # We don't need cmd list anymore

            # Prepare environment variables
            env_vars = {}
            if self.api_key:
                env_vars["AIDER_API_KEY"] = self.api_key
            if self.base_url:
                env_vars["AIDER_BASE_URL"] = self.base_url
            if self.openai_api_key:
                env_vars["OPENAI_API_KEY"] = self.openai_api_key
            if self.openai_base_url:
                env_vars["OPENAI_BASE_URL"] = self.openai_base_url
            if self.openrouter_api_key:
                env_vars["OPENROUTER_API_KEY"] = self.openrouter_api_key
            if self.openrouter_base_url:
                env_vars["OPENROUTER_BASE_URL"] = self.openrouter_base_url

            # Set aider env vars for non-interactive mode
            env_vars["AIDER_GUI"] = "false"
            env_vars["AIDER_BROWSER"] = "false"
            env_vars["AIDER_CHECK_UPDATE"] = "false"
            env_vars["AIDER_ANALYTICS"] = "false"
            env_vars["AIDER_NO_STREAM"] = "true"

            # Apply usage markers (handled by capturing output)
            if self.usage_markers and isinstance(self.usage_markers, (list, tuple)):
                usage_markers = self.usage_markers
            else:
                usage_markers = [
                    "rate limit",
                    "usage limit",
                    "upgrade to pro",
                    "too many requests",
                ]

            # Run aider library
            # We need to temporarily set environment variables
            original_env = os.environ.copy()
            os.environ.update(env_vars)

            try:
                io_obj = InputOutput(yes=True)

                # Model selection
                model_name = self.model_name
                if self.config_backend:
                    # Logic to determine model name if using backend config
                    # Already set in __init__ as self.model_name
                    pass

                # Get processed options with placeholders replaced
                # Use options_for_noedit for no-edit operations if available
                if self.config_backend:
                    processed_options = self.config_backend.replace_placeholders(model_name=self.model_name, session_id=None)
                    if is_noedit and self.options_for_noedit:
                        options_to_use = processed_options["options_for_noedit"]
                    else:
                        options_to_use = processed_options["options"]
                else:
                    # Fallback if config_backend is not available
                    options_to_use = self.options_for_noedit if is_noedit and self.options_for_noedit else self.options

                # Add configured options from config to env vars
                if options_to_use:
                    self._apply_options_to_env(options_to_use, env_vars)

                # Append any one-time extra arguments (e.g., resume flags)
                extra_args = self.consume_extra_args()
                if extra_args:
                    self._apply_options_to_env(extra_args, env_vars)

                # is_noedit_run = is_noedit # Unused

                # fnames needs to be passed?
                # CLI usually infers fnames or expects them in args.
                # If the user prompt adds files, aider handles it.
                # But initial fnames might be needed if provided in options?
                fnames: list[str] = []

                # Initialize Model
                # aider.models.Model(model_name)
                # If model_name is "aider", it might defaults.
                if model_name == "aider":
                    main_model = Model("gpt-4-turbo")  # Default fallback if 'aider' backend is generic?
                    # Actually, users usually set specific model.
                    # If model_name is literally "aider", we might want to check what default aider uses.
                    # But let's use self.model_name.
                    pass

                main_model = Model(model_name)

                # Capture output
                stdout_capture = io.StringIO()

                with contextlib.redirect_stdout(stdout_capture), contextlib.redirect_stderr(stdout_capture):
                    # Create coder
                    # We pass empty fnames initially; Aider might pick up git repo files or we can let it.
                    # CLI usually runs in a git repo.
                    coder = Coder.create(main_model=main_model, fnames=fnames, io=io_obj)

                    if coder:
                        run_result = coder.run(escaped_prompt)

                full_output = stdout_capture.getvalue()

                # If coder.run returns a string, use it as the main response content
                # This avoids parsing CLI headers which are in stdout
                if run_result is not None and isinstance(run_result, str) and run_result.strip():
                    # However, aider sometimes prints changes to files or other info to stdout/stderr
                    # that might be relevant?
                    # The user specifically wants to avoid "headers" (Tokens, etc).
                    # So we should prefer run_result for the "response content".
                    # But if we need logs of what happened (files edited), that might be in stdout.
                    # For now, let's prioritize run_result but maybe log stdout to debug?
                    # Or we can return run_result.
                    full_output = run_result

            finally:
                # Restore environment
                os.environ.clear()
                os.environ.update(original_env)

            full_output = full_output.strip()
            low = full_output.lower()

            # Check for generic errors or timeout indicators in output
            # (Aider library doesn't timeout in the same way subprocess does,
            # unless we add wrapping or configure it)

            usage_limit_detected = has_usage_marker_match(full_output, usage_markers)

            if usage_limit_detected:
                status = "error"
                error_message = full_output
                raise AutoCoderUsageLimitError(full_output)

            # We assume success if no exception raised by coder.run
            # But we should check for errors in output if possible.

            return full_output

        except AutoCoderUsageLimitError:
            raise
        except Exception as e:
            raise RuntimeError(f"Failed to run aider library: {e}")
        finally:
            # Always log the interaction and print summary
            duration_ms = (time.time() - start_time) * 1000

            # Log to JSON file
            self.output_logger.log_interaction(
                backend="aider",
                model=self.model_name,
                prompt=prompt,
                response=full_output,
                duration_ms=duration_ms,
                status=status,
                error=error_message,
            )

            # Print user-friendly summary to stdout
            print("\n" + "=" * 60)
            print("🤖 Aider CLI Execution Summary")
            print("=" * 60)
            print(f"Backend: aider")
            print(f"Model: {self.model_name}")
            print(f"Prompt Length: {len(prompt)} characters")
            print(f"Response Length: {len(full_output)} characters")
            print(f"Duration: {duration_ms:.0f}ms")
            print(f"Status: {status.upper()}")
            if error_message:
                print(f"Error: {error_message[:200]}..." if len(error_message) > 200 else f"Error: {error_message}")
            print("=" * 60 + "\n")

    def check_mcp_server_configured(self, server_name: str) -> bool:
        """Check if a specific MCP server is configured for Aider CLI.

        Aider doesn't seem to have native MCP support in the same way Codex might,
        or at least not documented in the help output I saw.
        For now, we'll return False or implement if Aider adds support.
        """
        return False

    def add_mcp_server_config(self, server_name: str, command: str, args: list[str]) -> bool:
        """Add MCP server configuration to Aider CLI config.

        Not currently supported for Aider.
        """
        logger.warning("MCP server configuration not supported for Aider backend yet.")
        return False
