import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


def get_test_log_dir(repo_name: str) -> Path:
    """Returns the path to the test log directory."""
    return Path.home() / ".auto-coder" / repo_name / "test_log"


def ensure_log_dirs(log_dir: Path):
    """Ensures the log directory and its raw subdirectory exist."""
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "raw").mkdir(exist_ok=True)


@dataclass
class LogEntry:
    """Represents a single test log entry."""

    ts: str
    source: str
    repo: str
    job: Optional[str] = None
    command: Optional[str] = None
    exit_code: Optional[int] = None
    raw: Optional[str] = None
    file: Optional[str] = None
    stderr: Optional[str] = None
    stdout: Optional[str] = None
    fingerprint: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None

    def save(self, log_dir: Path, filename: str):
        """Saves the log entry to a file."""
        ensure_log_dirs(log_dir)
        filepath = log_dir / filename
        fd = os.open(filepath, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        # Ensure permissions are restricted even if file already existed
        os.chmod(filepath, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2)
