"""
This module provides utilities for logging test results.
"""
import os
import json
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any
import datetime

from .git_info import get_current_repo_name

@dataclass
class LogEntry:
    """Represents a single log entry."""
    test_file: str
    stdout: str
    stderr: str
    exit_code: int
    success: bool
    timestamp: str

def get_log_dir() -> Optional[str]:
    """
    Returns the path to the log directory.
    Creates the directory if it doesn't exist.
    """
    repo_name = get_current_repo_name()
    if not repo_name:
        return None

    # Sanitize repo_name to be used as a directory name
    sanitized_repo_name = repo_name.replace('/', '_')

    log_dir = os.path.expanduser(f"~/.auto-coder/{sanitized_repo_name}/test_log")
    os.makedirs(log_dir, exist_ok=True)
    return log_dir

def get_raw_log_dir() -> Optional[str]:
    """
    Returns the path to the raw log directory.
    Creates the directory if it doesn't exist.
    """
    log_dir = get_log_dir()
    if not log_dir:
        return None

    raw_log_dir = os.path.join(log_dir, "raw")
    os.makedirs(raw_log_dir, exist_ok=True)
    return raw_log_dir

def write_log_entry(log_entry: LogEntry):
    """Writes a log entry to a file."""
    raw_log_dir = get_raw_log_dir()
    if not raw_log_dir:
        return

    log_file_path = os.path.join(raw_log_dir, f"{log_entry.timestamp}.json")
    with open(log_file_path, "w") as f:
        json.dump(asdict(log_entry), f, indent=2)

def create_log_entry(test_file: str, stdout: str, stderr: str, exit_code: int, success: bool) -> LogEntry:
    """Creates a LogEntry with the current timestamp."""
    timestamp = datetime.datetime.now().isoformat()
    return LogEntry(
        test_file=test_file,
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        success=success,
        timestamp=timestamp,
    )
