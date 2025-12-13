"""LLM Output Logger - JSON Lines format logging for LLM interactions.

This module provides a specialized logger for recording LLM interactions in
JSON Lines format (one JSON object per line). This format is ideal for:
- Easy parsing and analysis
- Streaming and tailing log files
- Integration with data processing tools

The logger is designed to be compatible with loguru's multi-sink setup and
can be enabled/disabled via environment variables.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Union


class LLMOutputLogger:
    """Logger for LLM outputs in JSON Lines format.

    This class provides structured logging for LLM interactions, writing
    one JSON object per line to a log file. It's designed to work alongside
    loguru's multi-sink setup for comprehensive logging coverage.

    Example JSON log entry:
    {
        "timestamp": "2025-11-24T10:30:45.123Z",
        "level": "INFO",
        "event_type": "llm_request",
        "backend": "codex",
        "model": "codex",
        "prompt_length": 1500,
        "response_length": 850,
        "duration_ms": 1234,
        "status": "success"
    }

    Attributes:
        log_path: Path to the JSON log file
        enabled: Whether logging is enabled (can be overridden by env var)
        env_var: Environment variable name for enabling/disabling
    """

    def __init__(
        self,
        log_path: Optional[Union[str, Path]] = None,
        enabled: Optional[bool] = None,
        environment_variable: str = "AUTO_CODER_LLM_OUTPUT_LOG_ENABLED",
    ):
        """Initialize LLMOutputLogger.

        Args:
            log_path: Path for JSON log file. If None, uses default path in
                     ~/.auto-coder/logs/llm_output.jsonl
            enabled: Whether logging is enabled. If None, checks environment
                    variable first, then uses True as default
            environment_variable: Environment variable to check for enabling/disabling
        """
        self.env_var = environment_variable

        # Determine if logging is enabled
        # Priority: explicit enabled parameter > environment variable > default (True)
        if enabled is not None:
            # If explicitly set, use it unless env var is also set
            env_enabled = os.environ.get(self.env_var, "").strip().lower()
            if env_enabled:
                self.enabled = env_enabled in {"1", "true", "yes", "on"}
            else:
                self.enabled = enabled
        else:
            # No explicit setting, check environment variable
            env_enabled = os.environ.get(self.env_var, "").strip().lower()
            if env_enabled:
                self.enabled = env_enabled in {"1", "true", "yes", "on"}
            else:
                # Default to enabled if no environment variable is set
                self.enabled = True

        # Set up log path
        if log_path is not None:
            self.log_path = Path(log_path)
        else:
            # Compute default path at runtime
            self.log_path = Path.home() / ".auto-coder" / "logs" / "llm_output.jsonl"

        # Ensure log directory exists
        if self.enabled and self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)

        # Track if file is open
        self._file_handle: Optional[Any] = None

    def _is_enabled(self) -> bool:
        """Check if logging is currently enabled.

        Returns:
            True if logging is enabled, False otherwise
        """
        # Re-check environment variable on each call to allow runtime changes
        env_enabled = os.environ.get(self.env_var, "").strip().lower()
        if env_enabled:
            return env_enabled in {"1", "true", "yes", "on"}

        return self.enabled

    def _get_timestamp(self) -> str:
        """Get current timestamp in ISO format with timezone.

        Returns:
            ISO format timestamp with timezone info
        """
        return datetime.now(timezone.utc).isoformat()

    def _open_file(self) -> None:
        """Open the log file for writing."""
        if self._file_handle is None:
            try:
                self._file_handle = open(
                    self.log_path,
                    "a",
                    encoding="utf-8",
                )
            except Exception as e:
                # Fallback to stderr if file can't be opened
                print(f"Warning: Could not open LLM output log file {self.log_path}: {e}", file=sys.stderr)
                self._file_handle = None

    def _close_file(self) -> None:
        """Close the log file if it's open."""
        if self._file_handle is not None:
            try:
                self._file_handle.close()
            except Exception:
                pass
            self._file_handle = None

    def _write_json_line(self, data: Dict[str, Any]) -> None:
        """Write a JSON line to the log.

        Args:
            data: Dictionary to write as JSON
        """
        if not self._is_enabled():
            return

        # Add timestamp if not present
        if "timestamp" not in data:
            data["timestamp"] = self._get_timestamp()

        # Write to file or stderr as fallback
        try:
            if self._file_handle is None:
                # Try to open file for append
                self._open_file()

            if self._file_handle is not None:
                json_line = json.dumps(data, ensure_ascii=False)
                self._file_handle.write(json_line + "\n")
                self._file_handle.flush()
            else:
                # Fallback to stderr
                json_line = json.dumps(data, ensure_ascii=False)
                print(json_line, file=sys.stderr)
        except Exception as e:
            # Don't let logging errors break the application
            print(f"Warning: Failed to write LLM output log: {e}", file=sys.stderr)

    def log_request(
        self,
        backend: str,
        model: Optional[str] = None,
        prompt: Optional[str] = None,
        prompt_length: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Log an LLM request.

        Args:
            backend: Name of the backend (e.g., 'codex', 'gemini', 'qwen')
            model: Model name (optional)
            prompt: The prompt sent to LLM (optional)
            prompt_length: Length of prompt in characters (optional, can be auto-calculated)
            metadata: Additional metadata (optional)
        """
        data: Dict[str, Any] = {
            "event_type": "llm_request",
            "backend": backend,
        }

        if model is not None:
            data["model"] = model

        if prompt is not None:
            data["prompt_length"] = len(prompt)
        elif prompt_length is not None:
            data["prompt_length"] = prompt_length

        if metadata:
            data.update(metadata)

        self._write_json_line(data)

    def log_response(
        self,
        backend: str,
        model: Optional[str] = None,
        response: Optional[str] = None,
        response_length: Optional[int] = None,
        duration_ms: Optional[float] = None,
        status: str = "success",
        error: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Log an LLM response.

        Args:
            backend: Name of the backend (e.g., 'codex', 'gemini', 'qwen')
            model: Model name (optional)
            response: The response from LLM (optional)
            response_length: Length of response in characters (optional, can be auto-calculated)
            duration_ms: Duration of request in milliseconds (optional)
            status: Status of the request ('success', 'error', etc.)
            error: Error message if request failed (optional)
            metadata: Additional metadata (optional)
        """
        data: Dict[str, Any] = {
            "event_type": "llm_response",
            "backend": backend,
            "status": status,
        }

        if model is not None:
            data["model"] = model

        if response is not None:
            data["response_length"] = len(response)
        elif response_length is not None:
            data["response_length"] = response_length

        if duration_ms is not None:
            data["duration_ms"] = duration_ms

        if error is not None:
            data["error"] = error

        if metadata:
            data.update(metadata)

        self._write_json_line(data)

    def log_interaction(
        self,
        backend: str,
        model: Optional[str] = None,
        prompt: Optional[str] = None,
        response: Optional[str] = None,
        duration_ms: Optional[float] = None,
        status: str = "success",
        error: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Log a complete LLM interaction (request + response).

        This is a convenience method that combines log_request and log_response
        into a single entry.

        Args:
            backend: Name of the backend (e.g., 'codex', 'gemini', 'qwen')
            model: Model name (optional)
            prompt: The prompt sent to LLM (optional)
            response: The response from LLM (optional)
            duration_ms: Duration of request in milliseconds (optional)
            status: Status of the request ('success', 'error', etc.)
            error: Error message if request failed (optional)
            metadata: Additional metadata (optional)
        """
        data: Dict[str, Any] = {
            "event_type": "llm_interaction",
            "backend": backend,
            "status": status,
        }

        if model is not None:
            data["model"] = model

        if prompt is not None:
            data["prompt_length"] = len(prompt)

        if response is not None:
            data["response_length"] = len(response)

        if duration_ms is not None:
            data["duration_ms"] = duration_ms

        if error is not None:
            data["error"] = error

        if metadata:
            data.update(metadata)

        self._write_json_line(data)

    def flush(self) -> None:
        """Flush the log file to ensure all writes are committed."""
        if self._file_handle is not None:
            try:
                self._file_handle.flush()
            except Exception:
                pass

    def close(self) -> None:
        """Close the log file and release resources."""
        self._close_file()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()

    def __repr__(self) -> str:
        """String representation of the logger."""
        return f"LLMOutputLogger(" f"enabled={self.enabled}, " f"path={self.log_path}, " f"env_var={self.env_var})"
