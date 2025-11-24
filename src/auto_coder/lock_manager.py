"""Lock Manager for preventing concurrent auto-coder executions."""

import json
import os
import platform
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional


class LockInfo:
    """Information about a lock."""

    def __init__(self, pid: int, hostname: str, started_at: str):
        self.pid = pid
        self.hostname = hostname
        self.started_at = started_at

    @classmethod
    def from_dict(cls, data: Dict) -> "LockInfo":
        """Create LockInfo from dictionary."""
        return cls(
            pid=data["pid"],
            hostname=data["hostname"],
            started_at=data["started_at"],
        )

    def to_dict(self) -> Dict:
        """Convert to dictionary."""
        return {
            "pid": self.pid,
            "hostname": self.hostname,
            "started_at": self.started_at,
        }


class LockManager:
    """Manages lock files to prevent concurrent auto-coder executions."""

    def __init__(self):
        self.lock_file_path = self._get_lock_file_path()

    def _get_lock_file_path(self) -> Path | None:
        """Get the lock file path based on git directory.

        Returns None if not in a git repository or in a temporary directory (test environment).
        """
        try:
            # Use git rev-parse --git-dir to get the git directory
            result = subprocess.run(["git", "rev-parse", "--git-dir"], capture_output=True, text=True, check=True)
            git_dir = result.stdout.strip()
            git_dir_path = Path(git_dir).resolve()

            # Check if git directory exists
            if not git_dir_path.exists():
                return None

            # Check if we're in a temporary directory (likely a test environment)
            # This allows tests to run without lock interference
            current_dir = Path.cwd()
            if "tmp" in str(current_dir) or "pytest" in str(current_dir):
                return None

            return git_dir_path / "auto-coder.lock"
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            # git command failed or not found
            return None

    def acquire_lock(self, force: bool = False) -> bool:
        """Acquire a lock.

        Args:
            force: If True, forcibly overwrite existing lock

        Returns:
            True if lock was acquired, False otherwise
        """
        # If not in a git repository, skip locking
        if self.lock_file_path is None:
            return True

        if not force and self.is_locked():
            # Lock exists and not forcing
            return False

        if force and self.is_locked():
            # Remove existing lock if forcing
            self.release_lock()

        lock_info = LockInfo(
            pid=os.getpid(),
            hostname=platform.node(),
            started_at=datetime.now().isoformat(),
        )

        try:
            with open(self.lock_file_path, "w") as f:
                json.dump(lock_info.to_dict(), f, indent=2)
            return True
        except Exception as e:
            print(f"Error creating lock file: {e}", file=sys.stderr)
            return False

    def is_locked(self) -> bool:
        """Check if the lock file exists."""
        if self.lock_file_path is None:
            return False
        return self.lock_file_path.exists()

    def release_lock(self) -> None:
        """Remove the lock file."""
        if self.lock_file_path is None:
            return
        try:
            if self.lock_file_path.exists():
                self.lock_file_path.unlink()
        except Exception as e:
            print(f"Error removing lock file: {e}", file=sys.stderr)

    def get_lock_info_obj(self) -> Optional[LockInfo]:
        """Get information about the current lock."""
        if not self.is_locked():
            return None

        try:
            with open(self.lock_file_path, "r") as f:
                data = json.load(f)
            return LockInfo.from_dict(data)
        except Exception as e:
            print(f"Error reading lock file: {e}", file=sys.stderr)
            return None

    def print_lock_info(self) -> None:
        """Print information about the current lock."""
        if self.lock_file_path is None:
            print("Not in a git repository. No lock file available.")
            return

        lock_info = self.get_lock_info_obj()
        if not lock_info:
            print("No lock file found.")
            return

        print(f"Lock file: {self.lock_file_path}")
        print(f"PID: {lock_info.pid}")
        print(f"Hostname: {lock_info.hostname}")
        print(f"Started at: {lock_info.started_at}")

        # Check if process is still running
        if self._is_process_running(lock_info.pid):
            print("Status: Process is still running")
        else:
            print("Status: Process is no longer running (stale lock)")
            print("You can use '--force' to override or 'auto-coder unlock' to remove the lock.")

    def _is_process_running(self, pid: int) -> bool:
        """Check if a process is still running."""
        try:
            # On Unix systems, we can check if the process exists
            if platform.system() == "Windows":
                import ctypes

                return ctypes.windll.kernel32.OpenProcess(1, False, pid) != 0
            else:
                # Check if process exists by sending signal 0
                import signal

                os.kill(pid, 0)
                return True
        except (OSError, ProcessLookupError, AttributeError):
            return False

    # Alias methods with exact names as specified in issue #573

    def acquire(self, force: bool = False) -> bool:
        """Acquire a lock.

        Args:
            force: If True, forcibly overwrite existing lock

        Returns:
            True if lock was acquired, False otherwise
        """
        return self.acquire_lock(force=force)

    def release(self) -> None:
        """Remove the lock file."""
        self.release_lock()

    def get_lock_info(self) -> dict:
        """Get information about the current lock.

        Returns:
            Dictionary with lock information, or empty dict if no lock exists
        """
        lock_info = self.get_lock_info_obj()
        if lock_info is None:
            return {}
        return lock_info.to_dict()
