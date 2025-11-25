"""
Claude CLI client for Auto-Coder.
"""

import subprocess
from typing import Optional

from .exceptions import AutoCoderUsageLimitError
from .llm_backend_config import get_llm_config
from .llm_client_base import LLMClientBase
from .logger_config import get_logger
from .utils import CommandExecutor

logger = get_logger(__name__)


class ClaudeClient(LLMClientBase):
    """Claude CLI client for analyzing issues and generating solutions."""

    def __init__(
        self,
        model_name: Optional[str] = None,
        backend_name: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        openai_api_key: Optional[str] = None,
        openai_base_url: Optional[str] = None,
    ) -> None:
        """Initialize Claude CLI client.

        Args:
            model_name: Model to use (e.g., 'sonnet', 'opus', or full model name).
                      If None and backend_name is provided, will use config for backend_name.
            backend_name: Backend name to use for configuration lookup (optional).
                         If provided along with model_name=None, will use config for this backend.
            api_key: API key for the backend (optional, for custom backends).
            base_url: Base URL for the backend (optional, for custom backends).
            openai_api_key: OpenAI API key (optional, for OpenAI-compatible backends).
            openai_base_url: OpenAI base URL (optional, for OpenAI-compatible backends).
        """
        config = get_llm_config()

        # If backend_name is provided, get config from that backend
        if backend_name:
            config_backend = config.get_backend_config(backend_name)
            # Use provided values, fall back to config, then to default
            self.model_name = model_name or (config_backend and config_backend.model) or "sonnet"
            self.api_key = api_key or (config_backend and config_backend.api_key)
            self.base_url = base_url or (config_backend and config_backend.base_url)
            self.openai_api_key = openai_api_key or (config_backend and config_backend.openai_api_key)
            self.openai_base_url = openai_base_url or (config_backend and config_backend.openai_base_url)
        else:
            # Fall back to default claude config
            config_backend = config.get_backend_config("claude")
            self.model_name = model_name or (config_backend and config_backend.model) or "sonnet"
            self.api_key = api_key
            self.base_url = base_url
            self.openai_api_key = openai_api_key
            self.openai_base_url = openai_base_url

        self.default_model = self.model_name
        self.conflict_model = "sonnet"
        self.timeout = None

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

    def _run_llm_cli(self, prompt: str) -> str:
        """Run claude CLI with the given prompt and show real-time output."""
        try:
            escaped_prompt = self._escape_prompt(prompt)
            cmd = [
                "claude",
                "--print",
                "--dangerously-skip-permissions",
                "--allow-dangerously-skip-permissions",
                "--model",
                self.model_name,
                escaped_prompt,
            ]

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
            logger.info(f"🤖 Running: claude --print --dangerously-skip-permissions " f"--allow-dangerously-skip-permissions --model {self.model_name} [prompt]")
            logger.info("=" * 60)

            usage_markers = ('api error: 429 {"type":"error","error":{"type":"rate_limit_error","message":"usage limit exceeded',)

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
