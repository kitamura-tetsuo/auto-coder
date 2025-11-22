"""
Codex CLI client for Auto-Coder.
"""

import json
import subprocess
from pathlib import Path
from typing import Optional

from .exceptions import AutoCoderUsageLimitError
from .llm_backend_config import get_llm_config
from .llm_client_base import LLMClientBase
from .logger_config import get_logger
from .utils import CommandExecutor

logger = get_logger(__name__)


class CodexClient(LLMClientBase):
    """Codex CLI client for analyzing issues and generating solutions."""

    def __init__(self, model_name: Optional[str] = None) -> None:
        """Initialize Codex CLI client.

        Args:
            model_name: Model name (not used by codex CLI, accepted for interface compatibility).
                        If None, will use the configuration value, then fall back to default.
        """
        config = get_llm_config()
        config_backend = config.get_backend_config("codex")

        # Use provided value, fall back to config, then to default
        self.model_name = model_name or (config_backend and config_backend.model) or "codex"
        self.default_model = self.model_name
        self.conflict_model = self.model_name
        self.timeout = None

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

    def _run_llm_cli(self, prompt: str) -> str:
        """Run codex CLI with the given prompt and show real-time output."""
        import datetime

        try:
            escaped_prompt = self._escape_prompt(prompt)
            cmd = [
                "codex",
                "exec",
                "-s",
                "workspace-write",
                "--dangerously-bypass-approvals-and-sandbox",
                escaped_prompt,
            ]

            usage_markers = (
                "rate limit",
                "usage limit",
                "upgrade to pro",
                "too many requests",
            )

            # Capture output without streaming to logger
            result = CommandExecutor.run_command(
                cmd,
                stream_output=False,
            )

            stdout = (result.stdout or "").strip()
            stderr = (result.stderr or "").strip()
            combined_parts = [part for part in (stdout, stderr) if part]
            full_output = "\n".join(combined_parts) if combined_parts else (result.stderr or result.stdout or "")
            full_output = full_output.strip()
            low = full_output.lower()

            # Prepare structured JSON log entry
            log_entry = {
                "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
                "client": "codex",
                "model": self.model_name,
                "prompt_length": len(prompt),
                "return_code": result.returncode,
                "success": result.returncode == 0,
                "usage_limit_hit": any(marker in low for marker in usage_markers),
                "output": full_output,
            }

            # Log as single-line JSON
            logger.info(json.dumps(log_entry, ensure_ascii=False))

            # Print summary to stdout
            summary = f"[Codex] Model: {self.model_name}, Prompt: {len(prompt)} chars, Output: {len(full_output)} chars"
            print(summary)

            # Handle errors
            if result.returncode != 0:
                if log_entry["usage_limit_hit"]:
                    raise AutoCoderUsageLimitError(full_output)
                raise RuntimeError(f"codex CLI failed with return code {result.returncode}\n{full_output}")

            if log_entry["usage_limit_hit"]:
                raise AutoCoderUsageLimitError(full_output)
            return full_output
        except AutoCoderUsageLimitError:
            raise
        except Exception as e:
            raise RuntimeError(f"Failed to run codex CLI: {e}")

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
