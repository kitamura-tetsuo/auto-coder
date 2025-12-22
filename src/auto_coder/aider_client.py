"""
Aider CLI client for Auto-Coder.
"""

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
        try:
            result = subprocess.run(["aider", "--version"], capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                raise RuntimeError("aider CLI not available or not working")
        except Exception as e:
            raise RuntimeError(f"aider CLI not available: {e}")

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

    def _run_llm_cli(self, prompt: str, is_noedit: bool = False) -> str:
        """Run aider CLI with the given prompt and show real-time output."""
        start_time = time.time()
        status = "success"
        error_message = None
        full_output = ""

        try:
            escaped_prompt = self._escape_prompt(prompt)
            cmd = ["aider"]

            # Explicitly pass model if configured and not the internal default
            if self.model_name and self.model_name != "aider":
                cmd.extend(["--model", self.model_name])
            
            # Always run in non-interactive mode
            cmd.append("--yes")

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

            # Add configured options from config
            if options_to_use:
                cmd.extend(options_to_use)

            # Append any one-time extra arguments (e.g., resume flags)
            extra_args = self.consume_extra_args()
            if extra_args:
                cmd.extend(extra_args)

            # Aider specific: use --message to pass the prompt
            cmd.extend(["--message", escaped_prompt])

            # Use configured usage_markers if available, otherwise fall back to defaults
            if self.usage_markers and isinstance(self.usage_markers, (list, tuple)):
                usage_markers = self.usage_markers
            else:
                # Default hardcoded usage markers
                usage_markers = [
                    "rate limit",
                    "usage limit",
                    "upgrade to pro",
                    "too many requests",
                ]

            # Prepare environment variables for subprocess
            env = os.environ.copy()
            if self.api_key:
                env["AIDER_API_KEY"] = self.api_key  # Aider might use different env vars depending on provider
            if self.base_url:
                env["AIDER_BASE_URL"] = self.base_url
            if self.openai_api_key:
                env["OPENAI_API_KEY"] = self.openai_api_key
            if self.openai_base_url:
                env["OPENAI_BASE_URL"] = self.openai_base_url
            if self.openrouter_api_key:
                env["OPENROUTER_API_KEY"] = self.openrouter_api_key
            if self.openrouter_base_url:
                env["OPENROUTER_BASE_URL"] = self.openrouter_base_url

            # Ensure Aider doesn't try to open a GUI or browser or check for updates
            env["AIDER_GUI"] = "false"
            env["AIDER_BROWSER"] = "false"
            env["AIDER_CHECK_UPDATE"] = "false"
            env["AIDER_ANALYTICS"] = "false"
            env["AIDER_NO_STREAM"] = "true"  # Ensure no streaming characters that might confuse parsing

            result = CommandExecutor.run_command(
                cmd,
                stream_output=True,
                env=env if len(env) > len(os.environ) else None,
                dot_format=True,
                idle_timeout=1800,
            )

            stdout = (result.stdout or "").strip()
            stderr = (result.stderr or "").strip()
            combined_parts = [part for part in (stdout, stderr) if part]
            full_output = "\n".join(combined_parts) if combined_parts else (result.stderr or result.stdout or "")
            full_output = full_output.strip()
            low = full_output.lower()

            # Check for timeout (returncode -1 and "timed out" in stderr)
            if result.returncode == -1 and "timed out" in low:
                raise AutoCoderTimeoutError(full_output)

            usage_limit_detected = has_usage_marker_match(full_output, usage_markers)

            if result.returncode != 0:
                if usage_limit_detected:
                    status = "error"
                    error_message = full_output
                    raise AutoCoderUsageLimitError(full_output)
                status = "error"
                error_message = f"aider CLI failed with return code {result.returncode}\n{full_output}"
                raise RuntimeError(error_message)

            if usage_limit_detected:
                status = "error"
                error_message = full_output
                raise AutoCoderUsageLimitError(full_output)

            return full_output
        except AutoCoderUsageLimitError:
            # Re-raise without catching
            raise
        except AutoCoderTimeoutError:
            # Re-raise timeout errors
            raise
        except Exception as e:
            raise RuntimeError(f"Failed to run aider CLI: {e}")
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
