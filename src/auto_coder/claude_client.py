"""
Claude CLI client for Auto-Coder.
"""

import os
import re
import subprocess
from typing import List, Optional

from .exceptions import AutoCoderTimeoutError, AutoCoderUsageLimitError
from .llm_backend_config import get_llm_config
from .llm_client_base import LLMClientBase
from .logger_config import get_logger
from .utils import CommandExecutor

logger = get_logger(__name__)


class ClaudeClient(LLMClientBase):
    """Claude CLI client for analyzing issues and generating solutions."""

    def __init__(
        self,
        backend_name: Optional[str] = None,
    ) -> None:
        """Initialize Claude CLI client.

        Args:
            backend_name: Backend name to use for configuration lookup (optional).
                         If provided, will use config for this backend.
        """
        super().__init__()
        config = get_llm_config()

        # If backend_name is provided, get config from that backend
        if backend_name:
            config_backend = config.get_backend_config(backend_name)
            # Use backend config, fall back to default "sonnet"
            self.model_name = (config_backend and config_backend.model) or "sonnet"
            self.api_key = config_backend and config_backend.api_key
            self.base_url = config_backend and config_backend.base_url
            self.openai_api_key = config_backend and config_backend.openai_api_key
            self.openai_base_url = config_backend and config_backend.openai_base_url
            self.settings = config_backend and config_backend.settings
            # Store usage_markers from config
            self.usage_markers = (config_backend and config_backend.usage_markers) or []
            # Store options from config
            self.options = (config_backend and config_backend.options) or []
            # Store options_for_noedit from config
            self.options_for_noedit = (config_backend and config_backend.options_for_noedit) or []
        else:
            # Fall back to default claude config
            config_backend = config.get_backend_config("claude")
            self.model_name = (config_backend and config_backend.model) or "sonnet"
            self.api_key = config_backend and config_backend.api_key
            self.base_url = config_backend and config_backend.base_url
            self.openai_api_key = config_backend and config_backend.openai_api_key
            self.openai_base_url = config_backend and config_backend.openai_base_url
            self.settings = config_backend and config_backend.settings
            # Store usage_markers from config
            self.usage_markers = (config_backend and config_backend.usage_markers) or []
            # Store options from config
            self.options = (config_backend and config_backend.options) or []
            # Store options_for_noedit from config
            self.options_for_noedit = (config_backend and config_backend.options_for_noedit) or []

        self.default_model = self.model_name
        self.conflict_model = "sonnet"
        self.timeout = None

        # Initialize extra args and session tracking
        self._extra_args: List[str] = []
        self._last_session_id: Optional[str] = None
        self._last_output: Optional[str] = None

        # Validate required options for this backend
        if config_backend:
            required_errors = config_backend.validate_required_options()
            if required_errors:
                for error in required_errors:
                    logger.warning(error)

        try:
            result = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                raise RuntimeError("claude CLI not available or not working")
        except Exception as e:
            raise RuntimeError(f"claude CLI not available: {e}")

    def switch_to_conflict_model(self) -> None:
        """Switch to faster model for conflict resolution."""
        if self.model_name != self.conflict_model:
            logger.info(f"Switching from {self.model_name} to {self.conflict_model} for conflict resolution")
            self.model_name = self.conflict_model

    def switch_to_default_model(self) -> None:
        """Switch back to default model."""
        if self.model_name != self.default_model:
            logger.info(f"Switching back to default model: {self.default_model}")
            self.model_name = self.default_model

    def _escape_prompt(self, prompt: str) -> str:
        """Escape special characters that may confuse shell/CLI."""
        return prompt.replace("@", "\\@").strip()

    def _run_llm_cli(self, prompt: str, is_noedit: bool = False) -> str:
        """Run claude CLI with the given prompt and show real-time output."""
        try:
            escaped_prompt = self._escape_prompt(prompt)
            cmd = [
                "claude",
                "--print",
                "--model",
                self.model_name,
            ]

            # Add configurable options from config
            # Use options_for_noedit for no-edit operations if available
            options_to_use = self.options_for_noedit if is_noedit and self.options_for_noedit else self.options
            cmd.extend(options_to_use)

            if self.settings:
                cmd.extend(["--settings", self.settings])

            # Append extra args if any (e.g., --resume <session_id>)
            extra_args = self.consume_extra_args()
            if extra_args:
                cmd.extend(extra_args)
                logger.debug(f"Using extra args: {extra_args}")

            cmd.append(escaped_prompt)

            # Prepare environment variables for subprocess
            env = os.environ.copy()
            if self.api_key:
                env["CLAUDE_API_KEY"] = self.api_key
            if self.base_url:
                env["CLAUDE_BASE_URL"] = self.base_url
            if self.openai_api_key:
                env["OPENAI_API_KEY"] = self.openai_api_key
            if self.openai_base_url:
                env["OPENAI_BASE_URL"] = self.openai_base_url

            logger.warning("LLM invocation: claude CLI is being called. Keep LLM calls minimized.")
            logger.debug(f"Running claude CLI with prompt length: {len(prompt)} characters")
            # Build command string for logging
            options_str = " ".join(self.options) if self.options else ""
            logger.info(f"🤖 Running: claude --print {options_str} --model {self.model_name} [prompt]")
            logger.info("=" * 60)

            # Use configured usage_markers if available, otherwise fall back to defaults
            if self.usage_markers:
                usage_markers = self.usage_markers
            else:
                # Default hardcoded usage markers
                usage_markers = ('api error: 429 {"type":"error","error":{"type":"rate_limit_error","message":"usage limit exceeded', "5-hour limit reached · resets")

            result = CommandExecutor.run_command(
                cmd,
                stream_output=True,
                env=env if len(env) > len(os.environ) else None,
            )
            logger.info("=" * 60)
            stdout = (result.stdout or "").strip()
            stderr = (result.stderr or "").strip()
            combined_parts = [part for part in (stdout, stderr) if part]
            full_output = "\n".join(combined_parts) if combined_parts else (result.stderr or result.stdout or "")
            full_output = full_output.strip()
            low = full_output.lower()

            # Store output for session ID extraction
            self._last_output = full_output

            # Extract and store session ID from output
            self._extract_and_store_session_id(full_output)

            # Check for timeout (returncode -1 and "timed out" in stderr)
            if result.returncode == -1 and "timed out" in low:
                raise AutoCoderTimeoutError(full_output)

            if result.returncode != 0:
                if any(marker in low for marker in usage_markers):
                    raise AutoCoderUsageLimitError(full_output)
                raise RuntimeError(f"claude CLI failed with return code {result.returncode}\n{full_output}")

            return full_output
        except AutoCoderUsageLimitError:
            raise
        except Exception as e:
            raise RuntimeError(f"Failed to run claude CLI: {e}")

    def check_mcp_server_configured(self, server_name: str) -> bool:
        """Check if a specific MCP server is configured for Claude CLI.

        Args:
            server_name: Name of the MCP server to check (e.g., 'graphrag', 'mcp-pdb')

        Returns:
            True if the MCP server is configured, False otherwise
        """
        try:
            result = subprocess.run(
                ["claude", "mcp"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                output = result.stdout.lower()
                if server_name.lower() in output:
                    logger.info(f"Found MCP server '{server_name}' via 'claude mcp'")
                    return True
                logger.debug(f"MCP server '{server_name}' not found via 'claude mcp'")
                return False
            else:
                logger.debug(f"'claude mcp' command failed with return code {result.returncode}")
                return False
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.debug(f"Failed to check Claude MCP config: {e}")
            return False

    def add_mcp_server_config(self, server_name: str, command: str, args: list[str]) -> bool:
        """Add MCP server configuration to Claude CLI config.

        Args:
            server_name: Name of the MCP server (e.g., 'graphrag', 'mcp-pdb')
            command: Command to run the MCP server (e.g., 'uv', '/path/to/script.sh')
            args: Arguments for the command (e.g., ['run', 'main.py'] or [])

        Returns:
            True if configuration was added successfully, False otherwise
        """
        try:
            import json
            from pathlib import Path

            # Use ~/.claude/config.json as primary location
            config_dir = Path.home() / ".claude"
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
            logger.error(f"Failed to add Claude MCP config: {e}")
            return False

    def set_extra_args(self, args: List[str]) -> None:
        """Store extra arguments to be used in the next execution.

        Args:
            args: List of extra arguments to store for the next execution (e.g., ['--resume', 'session_id'])
        """
        self._extra_args = args
        logger.debug(f"Set extra args for next execution: {args}")

    def get_last_session_id(self) -> Optional[str]:
        """Get the last session ID for session resumption.

        Extracts session ID from the last command output using regex patterns.

        Returns:
            The last session ID if available, None otherwise
        """
        return self._last_session_id

    def _extract_and_store_session_id(self, output: str) -> None:
        """Extract session ID from Claude CLI output and store it.

        Looks for patterns like:
        - Session ID: 550e8400-e29b-41d4-a716-446655440000
        - Session: 550e8400-e29b-41d4-a716-446655440000
        - session_id=550e8400-e29b-41d4-a716-446655440000
        - /sessions/550e8400-e29b-41d4-a716-446655440000

        Only matches valid UUID format (8-4-4-4-12 hexadecimal digits).

        Args:
            output: The output from Claude CLI
        """
        if not output:
            return

        # UUID pattern: 8-4-4-4-12 hexadecimal digits
        uuid_pattern = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"

        # Pattern 1: Look for "Session ID:" or "Session:" followed by UUID
        session_pattern = rf"(?:session\s*id:|session:)\s*({uuid_pattern})"
        match = re.search(session_pattern, output, re.IGNORECASE)
        if match:
            self._last_session_id = match.group(1)
            logger.debug(f"Extracted session ID from output: {self._last_session_id}")
            return

        # Pattern 2: Look for session_id= or session= in URLs/parameters
        session_param_pattern = rf"(?:session_id|session)=({uuid_pattern})"
        match = re.search(session_param_pattern, output, re.IGNORECASE)
        if match:
            self._last_session_id = match.group(1)
            logger.debug(f"Extracted session ID from URL parameter: {self._last_session_id}")
            return

        # Pattern 3: Look for /sessions/<uuid> in URLs
        session_path_pattern = rf"/sessions/({uuid_pattern})"
        match = re.search(session_path_pattern, output, re.IGNORECASE)
        if match:
            self._last_session_id = match.group(1)
            logger.debug(f"Extracted session ID from path: {self._last_session_id}")
            return

        # If no valid UUID session ID found, keep previous value
        logger.debug("No valid UUID session ID found in output")
