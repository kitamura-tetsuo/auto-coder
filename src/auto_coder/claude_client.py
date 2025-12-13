"""
Claude CLI client for Auto-Coder.
"""

import os
import re
import subprocess
from typing import Any, List, Optional

from .exceptions import AutoCoderTimeoutError, AutoCoderUsageLimitError
from .llm_backend_config import get_llm_config
from .llm_client_base import LLMClientBase
from .logger_config import get_logger
from .usage_marker_utils import has_usage_marker_match
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
            self.config_backend = config.get_backend_config(backend_name)
            self.model_name = (self.config_backend and self.config_backend.model) or "sonnet"
            self.api_key = self.config_backend and self.config_backend.api_key
            self.base_url = self.config_backend and self.config_backend.base_url
            self.openai_api_key = self.config_backend and self.config_backend.openai_api_key
            self.openai_base_url = self.config_backend and self.config_backend.openai_base_url
            if self.config_backend:
                self.settings = self.config_backend.settings
            else:
                self.settings = None
            # Store usage_markers from config
            self.usage_markers = (self.config_backend and self.config_backend.usage_markers) or []
            # Store options from config
            self.options = (self.config_backend and self.config_backend.options) or []
            # Store options_for_noedit from config
            self.options_for_noedit = (self.config_backend and self.config_backend.options_for_noedit) or []
        else:
            # Fall back to default claude config
            self.config_backend = config.get_backend_config("claude")
            self.model_name = (self.config_backend and self.config_backend.model) or "sonnet"
            self.api_key = self.config_backend and self.config_backend.api_key
            self.base_url = self.config_backend and self.config_backend.base_url
            self.openai_api_key = self.config_backend and self.config_backend.openai_api_key
            self.openai_base_url = self.config_backend and self.config_backend.openai_base_url
            if self.config_backend:
                self.settings = self.config_backend.settings
            else:
                self.settings = None
            # Store usage_markers from config
            self.usage_markers = (self.config_backend and self.config_backend.usage_markers) or []
            # Store options from config
            self.options = (self.config_backend and self.config_backend.options) or []
            # Store options_for_noedit from config
            self.options_for_noedit = (self.config_backend and self.config_backend.options_for_noedit) or []

        self.default_model = self.model_name
        self.conflict_model = "sonnet"
        self.timeout = None

        # Initialize extra args and session tracking
        self._extra_args: List[str] = []
        self._last_session_id: Optional[str] = None
        self._last_output: Optional[str] = None

        # Validate required options for this backend
        if self.config_backend:
            required_errors = self.config_backend.validate_required_options()
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
            base_cmd = ["claude"]

            # Get processed options with placeholders replaced
            # Use options_for_noedit for no-edit operations if available
            options_to_use = None
            try:
                # Try to call replace_placeholders if available
                if self.config_backend:
                    options_dict = self.config_backend.replace_placeholders(
                        model_name=self.model_name,
                        settings=self.settings,
                    )

                    # Check if options_dict is a valid dict (not a MagicMock)
                    is_dict = isinstance(options_dict, dict)
                    logger.info(f"options_dict type: {type(options_dict)}, is_dict: {is_dict}")
                    if is_dict:
                        # Use the appropriate options based on is_noedit flag
                        if is_noedit and self.options_for_noedit:
                            options_to_use = options_dict.get("options_for_noedit", [])
                            logger.info(f"Using options_for_noedit: {options_to_use}")
                        else:
                            options_to_use = options_dict.get("options", [])
                            logger.info(f"Using options: {options_to_use}")
                    else:
                        # options_dict is not a dict (probably a MagicMock), use fallback logic
                        raise TypeError("options_dict is not a dict")
            except (AttributeError, TypeError):
                # Fallback to direct options if replace_placeholders is not available or not properly configured
                if is_noedit:
                    options_to_use = self.options_for_noedit
                else:
                    options_to_use = self.options

            # Check if options already contain essential flags
            has_print = False
            has_model = False
            has_settings = False
            # Check for list, MagicMock, or other sequence types
            if options_to_use:
                try:
                    # Try to iterate over options_to_use
                    has_print = any("--print" in str(opt) for opt in options_to_use)
                    has_model = any("--model" in str(opt) for opt in options_to_use)
                    has_settings = any("--settings" in str(opt) for opt in options_to_use)
                    logger.info(f"has_print: {has_print}, has_model: {has_model}, has_settings: {has_settings}")
                except (TypeError, AttributeError):
                    # Not iterable, treat as empty
                    pass

            # Filter out --print, --model, and --settings from options to avoid duplication
            # Only filter if we plan to add them separately (i.e., if any is missing)
            filtered_options = options_to_use
            need_to_add_flags = not has_print or not has_model or not has_settings
            logger.info(f"need_to_add_flags: {need_to_add_flags}")
            if options_to_use and need_to_add_flags:
                # We're adding missing flags, so filter them out from options
                try:
                    filtered_options = []
                    i = 0
                    while i < len(options_to_use):
                        opt = options_to_use[i]
                        if opt == "--print" and not has_print:
                            # Skip this flag
                            i += 1
                            continue
                        if opt == "--model" and not has_model:
                            # Skip this flag and the next argument (the model name)
                            i += 2
                            continue
                        if opt == "--settings" and not has_settings:
                            # Skip this flag and the next argument (the settings path)
                            i += 2
                            continue
                        filtered_options.append(opt)
                        i += 1
                except (TypeError, AttributeError):
                    # Can't iterate, use empty list
                    filtered_options = []

            logger.info(f"filtered_options: {filtered_options}")

            # Add --print flag if not already present
            if not has_print:
                try:
                    base_cmd.append("--print")
                except (TypeError, AttributeError):
                    pass
            logger.info(f"Adding --print: {not has_print}")

            # Add --model flag with the model name if not already present
            if not has_model:
                try:
                    base_cmd.extend(["--model", self.model_name])
                except (TypeError, AttributeError):
                    pass
            logger.info(f"Adding --model: {not has_model}")

            # Add --settings flag if settings are configured and not already in options
            logger.info(f"self.settings: {self.settings}")
            # Check if settings is a valid string (not a MagicMock)
            is_settings_valid = isinstance(self.settings, str) and self.settings.strip()
            if is_settings_valid and not has_settings and self.settings:
                try:
                    base_cmd.extend(["--settings", self.settings])
                except (TypeError, AttributeError):
                    pass
            logger.info(f"Adding --settings: {is_settings_valid and not has_settings}")

            # Add additional options from config if available (filtered)
            if filtered_options:
                try:
                    base_cmd.extend(filtered_options)
                except (TypeError, AttributeError):
                    # Can't extend, skip adding options
                    pass

            # Append extra args if any (e.g., --resume <session_id>)
            cmd = base_cmd.copy()
            extra_args = self.consume_extra_args()
            if extra_args:
                try:
                    cmd.extend(extra_args)
                    logger.debug(f"Using extra args: {extra_args}")
                except (TypeError, AttributeError):
                    # Can't extend, skip extra args
                    pass

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
            # Build command string for logging (convert all elements to strings)
            cmd_str = " ".join(str(item) for item in cmd)
            logger.info(f"🤖 Running: {cmd_str}")
            logger.info("=" * 60)

            # Use configured usage_markers if available, otherwise fall back to defaults
            if self.usage_markers:
                usage_markers = self.usage_markers
            else:
                # Default hardcoded usage markers
                usage_markers = ['{\\"type\\":\\"error\\",\\"error\\":{\\"type\\":\\"rate_limit_error\\",', "5-hour limit reached · resets"]

            def run_cli(command: list[str]) -> tuple[Any, str, str, bool]:
                display_cmd = " ".join(command)
                logger.info(f"🤖 Running: {display_cmd}")
                logger.info("=" * 60)
                result = CommandExecutor.run_command(
                    command,
                    stream_output=True,
                    env=env if len(env) > len(os.environ) else None,
                    dot_format=True,
                    idle_timeout=1800,
                )
                logger.info("=" * 60)
                stdout = (result.stdout or "").strip()
                stderr = (result.stderr or "").strip()
                combined_parts = [part for part in (stdout, stderr) if part]
                full_output = "\n".join(combined_parts) if combined_parts else (result.stderr or result.stdout or "")
                full_output = full_output.strip()
                low_output = full_output.lower()
                usage_limit_detected = has_usage_marker_match(full_output, usage_markers)
                return result, full_output, low_output, usage_limit_detected

            result, full_output, low, usage_limit_detected = run_cli(cmd)

            if result.returncode != 0 and extra_args:
                logger.info("claude CLI failed with extra args; retrying without them")
                retry_cmd = base_cmd + [escaped_prompt]
                result, full_output, low, usage_limit_detected = run_cli(retry_cmd)

            # Store output for session ID extraction
            self._last_output = full_output

            # Extract and store session ID from output
            self._extract_and_store_session_id(full_output)

            # Check for timeout (returncode -1 and "timed out" in stderr)
            if result.returncode == -1 and "timed out" in low:
                raise AutoCoderTimeoutError(full_output)

            if result.returncode != 0:
                if usage_limit_detected:
                    raise AutoCoderUsageLimitError(full_output)
                raise RuntimeError(f"claude CLI failed with return code {result.returncode}\n{full_output}")

            if usage_limit_detected:
                raise AutoCoderUsageLimitError(full_output)

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

    def _is_valid_uuid(self, session_id: str) -> bool:
        """Validate that a string is a valid UUID format.

        Args:
            session_id: String to validate

        Returns:
            True if the string is a valid UUID, False otherwise
        """
        import uuid

        try:
            uuid.UUID(session_id)
            return True
        except (ValueError, AttributeError):
            return False

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
            extracted_id = match.group(1)
            if self._is_valid_uuid(extracted_id):
                self._last_session_id = extracted_id
                logger.debug(f"Extracted session ID from output: {self._last_session_id}")
            else:
                logger.debug(f"Extracted session ID failed UUID validation: {extracted_id}")
            return

        # Pattern 2: Look for session_id= or session= in URLs/parameters
        session_param_pattern = rf"(?:session_id|session)=({uuid_pattern})"
        match = re.search(session_param_pattern, output, re.IGNORECASE)
        if match:
            extracted_id = match.group(1)
            if self._is_valid_uuid(extracted_id):
                self._last_session_id = extracted_id
                logger.debug(f"Extracted session ID from URL parameter: {self._last_session_id}")
            else:
                logger.debug(f"Extracted session ID failed UUID validation: {extracted_id}")
            return

        # Pattern 3: Look for /sessions/<uuid> in URLs
        session_path_pattern = rf"/sessions/({uuid_pattern})"
        match = re.search(session_path_pattern, output, re.IGNORECASE)
        if match:
            extracted_id = match.group(1)
            if self._is_valid_uuid(extracted_id):
                self._last_session_id = extracted_id
                logger.debug(f"Extracted session ID from path: {self._last_session_id}")
            else:
                logger.debug(f"Extracted session ID failed UUID validation: {extracted_id}")
            return

        # If no valid UUID session ID found, keep previous value
        logger.debug("No valid UUID session ID found in output")
