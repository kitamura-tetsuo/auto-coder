"""
Claude CLI client for Auto-Coder.
"""

import subprocess

from .exceptions import AutoCoderUsageLimitError
from .llm_client_base import LLMClientBase
from .logger_config import get_logger
from .utils import CommandExecutor

logger = get_logger(__name__)


class ClaudeClient(LLMClientBase):
    """Claude CLI client for analyzing issues and generating solutions.

    Note: Provides a GeminiClient-compatible surface for integration.
    """

    def __init__(self, model_name: str = "sonnet"):
        """Initialize Claude CLI client.

        Args:
            model_name: Model to use (e.g., 'sonnet', 'opus', or full model name)
        """
        self.model_name = model_name or "sonnet"
        self.default_model = self.model_name
        self.conflict_model = "sonnet"  # Use sonnet for faster conflict resolution
        self.timeout = None

        # Check if claude CLI is available
        try:
            result = subprocess.run(
                ["claude", "--version"], capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                raise RuntimeError("claude CLI not available or not working")
        except Exception as e:
            raise RuntimeError(f"claude CLI not available: {e}")

    def switch_to_conflict_model(self) -> None:
        """Switch to faster model for conflict resolution."""
        if self.model_name != self.conflict_model:
            logger.info(
                f"Switching from {self.model_name} to {self.conflict_model} for conflict resolution"
            )
            self.model_name = self.conflict_model

    def switch_to_default_model(self) -> None:
        """Switch back to default model."""
        if self.model_name != self.default_model:
            logger.info(f"Switching back to default model: {self.default_model}")
            self.model_name = self.default_model

    def _escape_prompt(self, prompt: str) -> str:
        """Escape special characters that may confuse shell/CLI.
        Keep behavior aligned with other clients for consistency.
        """
        return prompt.replace("@", "\\@").strip()

    def _run_gemini_cli(self, prompt: str) -> str:
        """Run claude CLI with the given prompt and show real-time output.
        The method name matches GeminiClient for compatibility with AutomationEngine.
        """
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

            # Warn that we are invoking an LLM (minimize calls)
            logger.warning(
                "LLM invocation: claude CLI is being called. Keep LLM calls minimized."
            )
            logger.debug(
                f"Running claude CLI with prompt length: {len(prompt)} characters"
            )
            logger.info(
                f"🤖 Running: claude --print --dangerously-skip-permissions "
                f"--allow-dangerously-skip-permissions --model {self.model_name} [prompt]"
            )
            logger.info("=" * 60)

            usage_markers = (
                "rate limit",
                "quota",
                "429",
                "usage limit",
                "upgrade to pro",
                "overloaded",
            )

            def _on_stream(stream_name: str, chunk: str) -> None:
                low_chunk = chunk.lower()
                if any(marker in low_chunk for marker in usage_markers):
                    raise AutoCoderUsageLimitError(chunk.strip())

            result = CommandExecutor.run_command(
                cmd,
                stream_output=True,
                on_stream=_on_stream,
            )
            logger.info("=" * 60)
            stdout = (result.stdout or "").strip()
            stderr = (result.stderr or "").strip()
            combined_parts = [part for part in (stdout, stderr) if part]
            full_output = (
                "\n".join(combined_parts)
                if combined_parts
                else (result.stderr or result.stdout or "")
            )
            full_output = full_output.strip()
            low = full_output.lower()
            if result.returncode != 0:
                # Detect usage/rate limit patterns
                if any(marker in low for marker in usage_markers):
                    raise AutoCoderUsageLimitError(full_output)
                raise RuntimeError(
                    f"claude CLI failed with return code {result.returncode}\n{full_output}"
                )

            # Even with 0, some CLIs may print limit messages
            if any(marker in low for marker in usage_markers):
                raise AutoCoderUsageLimitError(full_output)
            return full_output
        except AutoCoderUsageLimitError:
            raise
        except Exception as e:
            raise RuntimeError(f"Failed to run claude CLI: {e}")

    def _run_llm_cli(self, prompt: str) -> str:
        """Neutral alias: delegate to _run_gemini_cli (migration helper)."""
        return self._run_gemini_cli(prompt)

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
                logger.debug(
                    f"'claude mcp' command failed with return code {result.returncode}"
                )
                return False
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.debug(f"Failed to check Claude MCP config: {e}")
            return False

    def add_mcp_server_config(
        self, server_name: str, command: str, args: list[str]
    ) -> bool:
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
