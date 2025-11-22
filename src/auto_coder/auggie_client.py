"""
Auggie CLI client for Auto-Coder.

Design: mimic GeminiClient/CodexClient interface so the automation engine can
swap backends transparently. The Auggie CLI is assumed to be installed via
`npm install -g @augmentcode/auggie` and supports non-interactive invocation
using `--print` together with `--model` and the prompt as a positional
argument.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, List, Optional

from .exceptions import AutoCoderUsageLimitError
from .llm_backend_config import get_llm_config
from .llm_client_base import LLMClientBase
from .llm_output_logger import get_llm_output_logger
from .logger_config import get_logger

logger = get_logger(__name__)


_USAGE_STATE_ENV = "AUTO_CODER_AUGGIE_USAGE_DIR"
_USAGE_FILENAME = "auggie_usage.json"
_DAILY_LIMIT = 20000


class AuggieClient(LLMClientBase):
    """Auggie CLI client wrapper."""

    DAILY_CALL_LIMIT = _DAILY_LIMIT

    def __init__(
        self,
        model_name: Optional[str] = None,
        realtime_feedback: bool = False,
        realtime_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        config = get_llm_config()
        config_backend = config.get_backend_config("auggie")

        # Use provided value, fall back to config, then to default
        self.model_name = model_name or (config_backend and config_backend.model) or "GPT-5"
        self.default_model = self.model_name
        self.conflict_model = self.model_name
        self.timeout = None
        self.realtime_feedback = realtime_feedback
        self.realtime_callback = realtime_callback
        self._usage_state_path = self._compute_usage_state_path()
        self._usage_date_cache: Optional[str] = None
        self._usage_count_cache: int = 0

        # Initialize LLMOutputLogger
        self._llm_logger = get_llm_output_logger()
        if self.realtime_callback:
            # Update the logger's callback for real-time feedback
            self._llm_logger.realtime_callback = self.realtime_callback

        # Verify Auggie CLI availability early for deterministic failures.
        try:
            result = subprocess.run(["auggie", "--version"], capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                raise RuntimeError("auggie CLI not available or not working")
        except Exception as exc:  # pragma: no cover - defensive; raised in init
            raise RuntimeError(f"auggie CLI not available: {exc}")

    def switch_to_conflict_model(self) -> None:
        """No-op placeholder for compatibility with BackendManager."""
        logger.debug("AuggieClient.switch_to_conflict_model: no-op (single model)")

    def switch_to_default_model(self) -> None:
        """No-op placeholder for compatibility with BackendManager."""
        logger.debug("AuggieClient.switch_to_default_model: no-op (single model)")

    def _compute_usage_state_path(self) -> Path:
        """Return path storing Auggie daily usage information."""

        override_dir = os.environ.get(_USAGE_STATE_ENV)
        base_dir = Path(override_dir).expanduser() if override_dir else Path.home() / ".cache" / "auto-coder"
        return base_dir / _USAGE_FILENAME

    def _update_usage_cache(self, date_str: Optional[str], count: int) -> None:
        self._usage_date_cache = date_str
        self._usage_count_cache = max(count, 0)

    def _load_usage_state(self) -> tuple[Optional[str], int]:
        """Load cached usage state, falling back to disk."""

        if self._usage_date_cache is not None:
            return self._usage_date_cache, self._usage_count_cache

        path = self._usage_state_path
        if not path.exists():
            self._update_usage_cache(None, 0)
            return None, 0

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            date_raw = data.get("date")
            count_raw = data.get("count", 0)
            date_str = str(date_raw) if date_raw else None
            count_val = int(count_raw)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.warning(
                "Failed to load Auggie usage state at %s: %s. Resetting counter.",
                path,
                exc,
            )
            self._update_usage_cache(None, 0)
            return None, 0

        self._update_usage_cache(date_str, count_val)
        return date_str, count_val

    def _persist_usage_state(self, date_str: str, count: int) -> None:
        """Persist usage state to disk (best effort)."""

        path = self._usage_state_path
        payload = {"date": date_str, "count": count}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.warning("Unable to persist Auggie usage state at %s: %s", path, exc)

    def _check_and_increment_usage(self) -> None:
        """Ensure the daily usage limit allows another Auggie invocation."""

        today_str = datetime.datetime.now().date().isoformat()
        stored_date, count = self._load_usage_state()

        if stored_date != today_str:
            count = 0

        if count >= _DAILY_LIMIT:
            message = f"Auggie daily invocation limit ({_DAILY_LIMIT}) reached. " "Please wait until tomorrow before using Auggie again."
            logger.warning(message)
            raise AutoCoderUsageLimitError(message)

        new_count = count + 1
        self._update_usage_cache(today_str, new_count)
        self._persist_usage_state(today_str, new_count)
        logger.debug("Auggie daily usage incremented to %s for %s", new_count, today_str)

    def _escape_prompt(self, prompt: str) -> str:
        """Escape characters that can confuse shell commands."""
        return prompt.replace("@", "\\@").strip()

    def _run_auggie_cli(self, prompt: str) -> str:
        """Execute Auggie CLI and log output as JSON."""
        import inspect

        self._check_and_increment_usage()
        escaped_prompt = self._escape_prompt(prompt)
        cmd = [
            "auggie",
            "--print",
            "--model",
            self.model_name,
            escaped_prompt,
        ]

        logger.warning("LLM invocation: auggie CLI is being called. Keep LLM calls minimized.")
        logger.debug(f"Running auggie CLI with prompt length: {len(prompt)} characters")
        logger.info("🤖 Running: auggie --print --model %s [prompt]" % self.model_name)
        logger.info("=" * 60)

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True,
        )

        output_lines: List[str] = []
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip("\n")
            if not line:
                continue

            # Real-time feedback: log each line if enabled
            if self.realtime_feedback:
                logger.info(line)

            # Store line for buffering
            output_lines.append(line)

            # Real-time callback
            if self.realtime_callback:
                try:
                    self.realtime_callback(line)
                except Exception as e:
                    logger.warning(f"Failed to call realtime callback: {e}")

        return_code = process.wait()
        logger.info("=" * 60)
        full_output = "\n".join(output_lines).strip()

        # Get caller information for logging
        frame = inspect.currentframe()
        caller_file = "__main__"
        caller_line = 0
        if frame and frame.f_back:
            caller_frame = frame.f_back
            caller_file = caller_frame.f_code.co_filename
            caller_line = caller_frame.f_lineno

        # Log the complete output as one JSON object
        self._llm_logger.log_output(
            model=self.model_name,
            prompt=prompt,
            output=full_output,
            caller_file=caller_file,
            caller_line=caller_line,
            metadata={"return_code": return_code},
        )

        low = full_output.lower()
        if return_code != 0:
            if ("rate limit" in low) or ("quota" in low) or ("429" in low):
                raise AutoCoderUsageLimitError(full_output)
            raise RuntimeError(f"auggie CLI failed with return code {return_code}\n{full_output}")
        if ("rate limit" in low) or ("quota" in low):
            raise AutoCoderUsageLimitError(full_output)
        return full_output

    def _run_llm_cli(self, prompt: str) -> str:
        """Execute LLM with the given prompt.

        Args:
            prompt: The prompt to send to the LLM

        Returns:
            The LLM's response as a string
        """
        self._check_and_increment_usage()
        escaped_prompt = self._escape_prompt(prompt)
        cmd = [
            "auggie",
            "--print",
            "--model",
            self.model_name,
            escaped_prompt,
        ]

        usage_markers = (
            "rate limit",
            "quota",
            "429",
        )

        # Capture output without streaming to logger
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True,
        )

        output_lines: List[str] = []
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip("\n")
            if not line:
                continue
            output_lines.append(line)

        return_code = process.wait()
        full_output = "\n".join(output_lines).strip()
        low = full_output.lower()

        # Prepare structured JSON log entry
        log_entry = {
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
            "client": "auggie",
            "model": self.model_name,
            "prompt_length": len(prompt),
            "return_code": return_code,
            "success": return_code == 0,
            "usage_limit_hit": any(marker in low for marker in usage_markers),
            "output": full_output,
        }

        # Log as single-line JSON
        logger.info(json.dumps(log_entry, ensure_ascii=False))

        # Print summary to stdout
        summary = f"[Auggie] Model: {self.model_name}, Prompt: {len(prompt)} chars, Output: {len(full_output)} chars"
        print(summary)

        # Handle errors
        if return_code != 0:
            if log_entry["usage_limit_hit"]:
                raise AutoCoderUsageLimitError(full_output)
            raise RuntimeError(f"auggie CLI failed with return code {return_code}\n{full_output}")

        if log_entry["usage_limit_hit"]:
            raise AutoCoderUsageLimitError(full_output)

        return full_output

    def check_mcp_server_configured(self, server_name: str) -> bool:
        """Check if a specific MCP server is configured for Auggie CLI.

        Args:
            server_name: Name of the MCP server to check (e.g., 'graphrag', 'mcp-pdb')

        Returns:
            True if the MCP server is configured, False otherwise
        """
        try:
            result = subprocess.run(
                ["auggie", "mcp", "list"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                output = result.stdout.lower()
                if server_name.lower() in output:
                    logger.info(f"Found MCP server '{server_name}' via 'auggie mcp list'")
                    return True
                logger.debug(f"MCP server '{server_name}' not found via 'auggie mcp list'")
                return False
            else:
                logger.debug(f"'auggie mcp list' command failed with return code {result.returncode}")
                return False
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.debug(f"Failed to check Auggie MCP config: {e}")
            return False

    def add_mcp_server_config(self, server_name: str, command: str, args: list[str]) -> bool:
        """Add MCP server configuration to Auggie CLI config (Windsurf).

        Args:
            server_name: Name of the MCP server (e.g., 'graphrag', 'mcp-pdb')
            command: Command to run the MCP server (e.g., 'uv', '/path/to/script.sh')
            args: Arguments for the command (e.g., ['run', 'main.py'] or [])

        Returns:
            True if configuration was added successfully, False otherwise
        """
        try:
            # Use auggie mcp add command
            # Format: auggie mcp add <name> --command <command> --args "<args>"
            args_str = " ".join(args)
            cmd = [
                "auggie",
                "mcp",
                "add",
                server_name,
                "--command",
                command,
                "--args",
                args_str,
                "--replace",  # Overwrite existing entry without prompt
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode == 0:
                logger.info(f"Added MCP server '{server_name}' to Auggie config")
                return True
            else:
                logger.error(f"Failed to add Auggie MCP config: {result.stderr}")
                return False
        except Exception as e:
            logger.error(f"Failed to add Auggie MCP config: {e}")
            return False
