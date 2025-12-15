"""Utilities for setting up and managing log directories and structures."""

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional

from .git_info import get_current_repo_name


@dataclass
class LogEntry:
    """Represents a single test log entry."""

    timestamp: float
    test_file: str
    stdout: str
    stderr: str
    exit_code: int
    success: bool
    raw_log_files: List[str]

    def to_dict(self) -> dict:
        """Converts the LogEntry to a dictionary."""
        return asdict(self)


def get_sanitized_repo_name() -> Optional[str]:
    """
    Get the sanitized repository name.

    Returns:
        The sanitized repository name, or None if it cannot be determined.
    """
    repo_name = get_current_repo_name()
    if repo_name:
        return re.sub(r"[^a-zA-Z0-9_\-]", "_", repo_name)
    return None


def setup_test_log_dir() -> Optional[Path]:
    """
    Set up the test log directory.

    Creates `~/.auto-coder/<repo_name>/test_log/` and
    `~/.auto-coder/<repo_name>/test_log/raw` directories.

    Returns:
        The path to the `raw` directory, or None if the repo name
        cannot be determined.
    """
    repo_name = get_sanitized_repo_name()
    if not repo_name:
        return None

    log_dir = Path.home() / ".auto-coder" / repo_name / "test_log"
    raw_dir = log_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    return raw_dir
