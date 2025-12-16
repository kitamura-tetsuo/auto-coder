"""
GitHub CLI command logging infrastructure for Auto-Coder.

This module provides the GHCommandLogger class for logging GitHub CLI commands
to CSV files with metadata including timestamp, caller location, command details,
repository, and hostname.
"""

import csv
import json
import os
import re
import socket
import subprocess
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Protocol, Union, cast

# Environment variable to disable logging
GH_LOGGING_DISABLED = os.environ.get("GH_LOGGING_DISABLED", "").lower() in (
    "1",
    "true",
    "yes",
    "on",
)

# Default log directory
DEFAULT_LOG_DIR = Path.home() / ".auto-coder" / "log"

# CSV field names
CSV_FIELDS = [
    "timestamp",
    "caller_file",
    "caller_line",
    "command",
    "args",
    "repo",
    "hostname",
]

# Patterns for sensitive data redaction
REDACTION_PATTERNS = [
    r"gh[pousr]_[a-zA-Z0-9]+",  # GitHub tokens
    r"github_pat_[a-zA-Z0-9_]+",  # GitHub PATs
    r"AIza[0-9A-Za-z-_]{35}",  # Google API keys
]


class GHCommandLogger:
    """
    Logger for GitHub CLI commands.

    Logs GitHub CLI command executions to CSV files with metadata including
    timestamp, caller file and line number, command, arguments, repository, and hostname.

    Features:
    - Automatic log file creation with rotation
    - CSV format for easy analysis
    - Context manager support for automatic logging
    - Environment variable to disable logging
    """

    def __init__(
        self,
        log_dir: Optional[Path] = None,
        logger: Optional[Any] = None,
    ):
        """
        Initialize GHCommandLogger.

        Args:
            log_dir: Directory to store log files. Defaults to ~/.auto-coder/log
            logger: Optional loguru logger instance to use
        """
        self.log_dir = log_dir or DEFAULT_LOG_DIR
        self.logger = logger
        self._hostname = socket.gethostname()
        self._ensure_log_dir()

    def _ensure_log_dir(self) -> None:
        """Ensure the log directory exists."""
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def _get_log_file_path(self) -> Path:
        """
        Get the path to the current log file.

        Returns:
            Path to the log file (gh_commands_YYYY-MM-DD.csv)
        """
        today = datetime.now().strftime("%Y-%m-%d")
        return self.log_dir / f"gh_commands_{today}.csv"

    def _compress_json_string(self, text: str) -> str:
        """
        Compress JSON strings by removing whitespace and newlines.

        Attempts to parse the input as JSON and return a compact version.
        If parsing fails, attempts to extract and compress JSON from within
        strings containing key=value patterns (e.g., "query={...}", "variables={...}").
        If all attempts fail, returns the original string unchanged.

        Args:
            text: String that may contain JSON

        Returns:
            Compressed JSON string if valid JSON, otherwise original string
        """
        # First, try to parse the entire string as JSON
        try:
            parsed = json.loads(text)
            return json.dumps(parsed, separators=(",", ":"))
        except (json.JSONDecodeError, TypeError):
            pass

        # Try to extract and compress JSON from key=value patterns
        # Match patterns like "query={...}", "variables={...}", etc.
        # Using [\s\S]* to match any character including newlines
        match = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*=)({[\s\S]*})$", text)
        if match:
            prefix = match.group(1)
            json_part = match.group(2)
            try:
                # Try to parse the JSON part
                parsed = json.loads(json_part)
                compressed_json = json.dumps(parsed, separators=(",", ":"))
                return prefix + compressed_json
            except (json.JSONDecodeError, TypeError):
                pass

        # If all else fails, return original string unchanged
        return text

    def _redact_string(self, text: str) -> str:
        """
        Redact sensitive information from a string.

        Args:
            text: String to redact

        Returns:
            Redacted string
        """
        redacted = text
        for pattern in REDACTION_PATTERNS:
            redacted = re.sub(pattern, "[REDACTED]", redacted)
        return redacted

    def _format_csv_row(
        self,
        caller_file: str,
        caller_line: int,
        command: str,
        args: List[str],
        repo: Optional[str] = None,
    ) -> Dict[str, str]:
        """
        Format a single CSV row with command metadata.

        Args:
            caller_file: Path to the file calling the command
            caller_line: Line number where the command was called
            command: The command that was executed (e.g., "gh")
            args: List of command arguments
            repo: Optional repository name (owner/repo)

        Returns:
            Dictionary with CSV field names as keys
        """
        formatted_args = []
        if args:
            for arg in args:
                compressed = self._compress_json_string(arg)
                redacted = self._redact_string(compressed)
                formatted_args.append(redacted)

        return {
            "timestamp": datetime.now().isoformat(),
            "caller_file": caller_file,
            "caller_line": str(caller_line),
            "command": command,
            "args": " ".join(formatted_args),
            "repo": repo or "",
            "hostname": self._hostname,
        }

    def log_command(
        self,
        command_list: List[str],
        caller_file: str,
        caller_line: int,
        repo: Optional[str] = None,
    ) -> None:
        """
        Log a GitHub CLI command execution.

        Args:
            command_list: Full command list (e.g., ["gh", "api", "graphql", ...])
            caller_file: Path to the file calling the command
            caller_line: Line number where the command was called
            repo: Optional repository name (owner/repo)
        """
        if GH_LOGGING_DISABLED:
            return

        if not command_list:
            return

        # Extract command and args
        command = command_list[0]
        args = command_list[1:]

        # Format the row
        row = self._format_csv_row(
            caller_file=caller_file,
            caller_line=caller_line,
            command=command,
            args=args,
            repo=repo,
        )

        # Write to CSV file
        log_file = self._get_log_file_path()
        try:
            with open(log_file, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
                # Write header if file is empty
                if f.tell() == 0:
                    writer.writeheader()
                writer.writerow(row)
        except Exception as e:
            # Don't fail the command if logging fails
            if self.logger:
                self.logger.warning(f"Failed to log GitHub command: {e}")

    @contextmanager
    def logged_subprocess(
        self,
        command: List[str],
        repo: Optional[str] = None,
        capture_output: bool = False,
        **kwargs: Any,
    ) -> Iterator[subprocess.CompletedProcess]:
        """
        Context manager for executing a subprocess with automatic logging.

        Automatically extracts caller information (file and line) and logs the command.

        Args:
            command: Command to execute as a list of strings
            repo: Optional repository name (owner/repo)
            capture_output: Whether to capture output (default: False)
            **kwargs: Additional arguments to pass to subprocess.run

        Yields:
            CompletedProcess object from subprocess.run

        Example:
            with gh_logger.logged_subprocess(["gh", "auth", "status"], repo="owner/repo") as result:
                print(result.stdout)
        """
        import inspect

        # Get caller information
        frame = inspect.currentframe()
        if frame and frame.f_back:
            caller_frame = frame.f_back
            caller_file = caller_frame.f_code.co_filename
            caller_line = caller_frame.f_lineno
        else:
            caller_file = "unknown"
            caller_line = 0

        # Log the command
        self.log_command(
            command_list=command,
            caller_file=caller_file,
            caller_line=caller_line,
            repo=repo,
        )

        # Execute the command
        try:
            result = subprocess.run(
                command,
                capture_output=capture_output,
                **kwargs,
            )
            yield result
        except Exception as e:
            if self.logger:
                self.logger.error(f"Error executing command {command}: {e}")
            raise

    def execute_with_logging(
        self,
        command: List[str],
        repo: Optional[str] = None,
        **kwargs: Any,
    ) -> "GHCommandResult":
        """
        Execute a subprocess command and log it.

        This is a convenience method that combines execution and logging.

        Args:
            command: Command to execute as a list of strings
            repo: Optional repository name (owner/repo)
            **kwargs: Additional arguments to pass to subprocess.run

        Returns:
            CompletedProcess object from subprocess.run

        Example:
            result = gh_logger.execute_with_logging(
                ["gh", "api", "graphql", "-f", "query=..."],
                repo="owner/repo"
            )
        """
        import inspect

        # Get caller information
        frame = inspect.currentframe()
        if frame and frame.f_back:
            caller_frame = frame.f_back
            caller_file = caller_frame.f_code.co_filename
            caller_line = caller_frame.f_lineno
        else:
            caller_file = "unknown"
            caller_line = 0

        # Log the command
        self.log_command(
            command_list=command,
            caller_file=caller_file,
            caller_line=caller_line,
            repo=repo,
        )

        # Execute the command using subprocess.run directly
        # The tests will mock subprocess.run
        timeout = kwargs.get("timeout", 60)
        cwd = kwargs.get("cwd", None)
        capture_output = kwargs.get("capture_output", True)

        # Execute the command
        result = subprocess.run(
            command,
            timeout=timeout,
            cwd=cwd,
            capture_output=capture_output,
            text=True,
        )

        # Add success attribute for compatibility
        if not hasattr(result, "success"):
            result.success = result.returncode == 0  # type: ignore[attr-defined]

        return cast(GHCommandResult, result)


class GHCommandResult(Protocol):
    returncode: int
    stdout: str
    stderr: str
    success: bool


# Global logger instance
_gh_logger: Optional[GHCommandLogger] = None


def get_gh_logger() -> GHCommandLogger:
    """
    Get the global GHCommandLogger instance.

    Returns:
        Global GHCommandLogger instance
    """
    global _gh_logger
    if _gh_logger is None:
        _gh_logger = GHCommandLogger()
    return _gh_logger


def set_gh_logger(logger: GHCommandLogger) -> None:
    """
    Set the global GHCommandLogger instance.

    Args:
        logger: GHCommandLogger instance to use globally
    """
    global _gh_logger
    _gh_logger = logger
