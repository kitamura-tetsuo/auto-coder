"""
Codex CLI client for Auto-Coder.
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


class CodexClient(LLMClientBase):
    """Codex CLI client for analyzing issues and generating solutions."""

    def __init__(
        self,
        backend_name: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        openai_api_key: Optional[str] = None,
        openai_base_url: Optional[str] = None,
        use_noedit_options: bool = False,
    ) -> None:
        """Initialize Codex CLI client.

        Args:
            backend_name: Backend name to use for configuration lookup (optional).
                         If provided, will use config for this backend.
            api_key: API key for the backend (optional, for custom backends).
            base_url: Base URL for the backend (optional, for custom backends).
            openai_api_key: OpenAI API key (optional, for OpenAI-compatible backends).
            openai_base_url: OpenAI base URL (optional, for OpenAI-compatible backends).
            use_noedit_options: If True, use options_for_noedit instead of options.
        """
        super().__init__()
        config = get_llm_config()

        # If backend_name is provided, get config from that backend
        if backend_name:
            self.config_backend = config.get_backend_config(backend_name)
            # Use backend config model, fall back to default "codex"
            self.model_name = (self.config_backend and self.config_backend.model) or "codex"
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
            self.model_provider = self.config_backend and self.config_backend.model_provider
            # Store usage_markers from config
            self.usage_markers = (self.config_backend and self.config_backend.usage_markers) or []
        else:
            # Fall back to default codex config
            self.config_backend = config.get_backend_config("codex")
            self.model_name = (self.config_backend and self.config_backend.model) or "codex"
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

        # Check if codex CLI is available
        try:
            result = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                raise RuntimeError("codex CLI not available or not working")
        except Exception as e:
            raise RuntimeError(f"codex CLI not available: {e}")

    def switch_to_conflict_model(self) -> None:
        """No-op; codex has no model switching."""
        logger.info("CodexClient: switch_to_conflict_model noop")

    def switch_to_default_model(self) -> None:
        """No-op; codex has no model switching."""
        logger.info("CodexClient: switch_to_default_model noop")

    def _escape_prompt(self, prompt: str) -> str:
        """Escape special characters that may confuse shell/CLI."""
        return prompt.replace("@", "\\@").strip()

    def _run_llm_cli(self, prompt: str, is_noedit: bool = False) -> str:
        """Run codex CLI with the given prompt and show real-time output."""
        start_time = time.time()
        status = "success"
        error_message = None
        full_output = ""

        try:
            escaped_prompt = self._escape_prompt(prompt)
            cmd = ["codex"]

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

            cmd.append(escaped_prompt)

            # Use configured usage_markers if available, otherwise fall back to defaults
            if self.usage_markers and isinstance(self.usage_markers, (list, tuple)):
                usage_markers = self.usage_markers
            else:
                # Default hardcoded usage markers
                usage_markers = (
                    "rate limit",
                    "usage limit",
                    "upgrade to pro",
                    "too many requests",
                )

            # Prepare environment variables for subprocess
            env = os.environ.copy()
            if self.api_key:
                env["CODEX_API_KEY"] = self.api_key
            if self.base_url:
                env["CODEX_BASE_URL"] = self.base_url
            if self.openai_api_key:
                env["OPENAI_API_KEY"] = self.openai_api_key
            if self.openai_base_url:
                env["OPENAI_BASE_URL"] = self.openai_base_url

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
                error_message = f"codex CLI failed with return code {result.returncode}\n{full_output}"
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
            raise RuntimeError(f"Failed to run codex CLI: {e}")
        finally:
            # Always log the interaction and print summary
            duration_ms = (time.time() - start_time) * 1000

            # Log to JSON file
            self.output_logger.log_interaction(
                backend="codex",
                model=self.model_name,
                prompt=prompt,
                response=full_output,
                duration_ms=duration_ms,
                status=status,
                error=error_message,
            )

            # Print user-friendly summary to stdout
            print("\n" + "=" * 60)
            print("🤖 Codex CLI Execution Summary")
            print("=" * 60)
            print(f"Backend: codex")
            print(f"Model: {self.model_name}")
            print(f"Prompt Length: {len(prompt)} characters")
            print(f"Response Length: {len(full_output)} characters")
            print(f"Duration: {duration_ms:.0f}ms")
            print(f"Status: {status.upper()}")
            if error_message:
                print(f"Error: {error_message[:200]}..." if len(error_message) > 200 else f"Error: {error_message}")
            print("=" * 60 + "\n")

    def check_mcp_server_configured(self, server_name: str) -> bool:
        """Check if a specific MCP server is configured for Codex CLI.

        Args:
            server_name: Name of the MCP server to check (e.g., 'graphrag', 'mcp-pdb')

        Returns:
            True if the MCP server is configured, False otherwise
        """
        try:
            result = subprocess.run(
                ["codex", "mcp", "list"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                output = result.stdout.lower()
                if server_name.lower() in output:
                    logger.info(f"Found MCP server '{server_name}' via 'codex mcp list'")
                    return True
                logger.debug(f"MCP server '{server_name}' not found via 'codex mcp list'")
                return False
            else:
                logger.debug(f"'codex mcp list' command failed with return code {result.returncode}")
                return False
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.debug(f"Failed to check Codex MCP config: {e}")
            return False

    def add_mcp_server_config(self, server_name: str, command: str, args: list[str]) -> bool:
        """Add MCP server configuration to Codex CLI config.

        Args:
            server_name: Name of the MCP server (e.g., 'graphrag', 'mcp-pdb')
            command: Command to run the MCP server (e.g., 'uv', '/path/to/script.sh')
            args: Arguments for the command (e.g., ['run', 'main.py'] or [])

        Returns:
            True if configuration was added successfully, False otherwise
        """
        try:
            # Use ~/.codex/config.json as primary location
            config_dir = Path.home() / ".codex"
            config_path = config_dir / "config.json"

            # Create directory if it doesn't exist
            config_dir.mkdir(parents=True, exist_ok=True)

            # Read existing config or create new one
            if config_path.exists():
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
            else:
                config = {}

            # Add MCP server
            if "mcpServers" not in config:
                config["mcpServers"] = {}

            config["mcpServers"][server_name] = {"command": command, "args": args}

            # Write config
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)

            logger.info(f"Added MCP server '{server_name}' to {config_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to add Codex MCP config: {e}")
            return False
