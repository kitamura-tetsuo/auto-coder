"""
CloudManager: Manages session tracking for issues in cloud.csv files.

This module provides functionality to track and manage sessions for GitHub issues
by storing issue number, provider, and session ID mappings in CSV files.
"""

import csv
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from .logger_config import get_logger

logger = get_logger(__name__)

# Keep the provider family separate from the named configuration used by it.
CSV_FIELDS = ["issue_number", "provider", "backend_name", "session_id"]


@dataclass(frozen=True)
class CloudTaskBinding:
    """Durable ownership information for a provider cloud task."""

    provider: str = ""
    task_id: str = ""
    backend_name: str = ""


class CloudManager:
    """
    Manages session tracking for issues using cloud.csv files.

    This class provides methods to add, retrieve, and check sessions for GitHub issues
    by storing mappings in CSV files under ~/.auto-coder/<repo>/cloud.csv.

    Thread Safety:
    -------------
    This class uses a lock to ensure thread-safe file operations.
    """

    def __init__(self, repo_name: str, cloud_file_path: Optional[Path] = None):
        """
        Initialize the CloudManager.

        Args:
            repo_name: Repository name in format 'owner/repo'
            cloud_file_path: Optional custom path for the cloud CSV file.
                            If not provided, uses ~/.auto-coder/<repo>/cloud.csv
        """
        self.repo_name = repo_name
        self._lock = threading.Lock()

        # Set default cloud file path if not provided
        if cloud_file_path:
            self.cloud_file_path = cloud_file_path
        else:
            auto_coder_dir = Path.home() / ".auto-coder" / repo_name
            self.cloud_file_path = auto_coder_dir / "cloud.csv"

    def _ensure_cloud_dir(self) -> None:
        """Ensure the cloud directory exists."""
        self.cloud_file_path.parent.mkdir(parents=True, exist_ok=True)

    def _read_bindings(self) -> Dict[str, CloudTaskBinding]:
        """
        Read all sessions from the cloud CSV file.

        Returns:
            Dictionary mapping issue numbers (as strings) to session IDs
        """
        self._ensure_cloud_dir()

        if not self.cloud_file_path.exists():
            return {}

        sessions: Dict[str, CloudTaskBinding] = {}
        try:
            with open(self.cloud_file_path, "r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    issue_number = row.get("issue_number", "")
                    session_id = row.get("session_id", "")
                    provider = row.get("provider", "")
                    backend_name = row.get("backend_name", "")
                    if provider == "claude-routine" and not backend_name:
                        backend_name = "claude-routine"
                    if issue_number and session_id:
                        sessions[issue_number] = CloudTaskBinding(provider=provider, task_id=session_id, backend_name=backend_name)
        except Exception as e:
            logger.error(f"Failed to read cloud sessions from {self.cloud_file_path}: {e}")

        return sessions

    def _read_sessions(self) -> Dict[str, str]:
        """Read the historical session-id view used by lifecycle callers."""
        return {number: binding.task_id for number, binding in self._read_bindings().items()}

    def _write_bindings(self, sessions: Dict[str, CloudTaskBinding]) -> bool:
        """
        Write sessions to the cloud CSV file.

        Args:
            sessions: Dictionary mapping issue numbers to session IDs

        Returns:
            True if sessions were written successfully, False otherwise
        """
        self._ensure_cloud_dir()

        try:
            # Secure file opening with restricted permissions (600)
            fd = os.open(str(self.cloud_file_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)

            # Ensure permissions are correct even if file already existed
            os.chmod(self.cloud_file_path, 0o600)

            with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
                writer.writeheader()

                # Sort by issue number for consistent output
                for issue_number in sorted(sessions.keys()):
                    binding = sessions[issue_number]
                    writer.writerow(
                        {
                            "issue_number": issue_number,
                            "provider": binding.provider,
                            "backend_name": binding.backend_name,
                            "session_id": binding.task_id,
                        }
                    )

            logger.debug(f"Successfully wrote {len(sessions)} sessions to {self.cloud_file_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to write cloud sessions to {self.cloud_file_path}: {e}")
            return False

    def _write_sessions(self, sessions: Dict[str, str]) -> bool:
        """Preserve provider values while supporting the historical private API."""
        current = self._read_bindings()
        bindings = {
            number: CloudTaskBinding(
                provider=current.get(number, CloudTaskBinding()).provider,
                task_id=task_id,
                backend_name=current.get(number, CloudTaskBinding()).backend_name,
            )
            for number, task_id in sessions.items()
        }
        return self._write_bindings(bindings)

    def add_session(self, issue_number: int, session_id: str, provider: str = "", backend_name: str = "") -> bool:
        """
        Add a session for an issue number.

        Args:
            issue_number: GitHub issue number
            session_id: Session ID to associate with the issue

        Returns:
            True if session was added successfully, False otherwise
        """
        with self._lock:
            try:
                # Read existing sessions
                sessions = self._read_bindings()

                # Add or update the session
                issue_key = str(issue_number)
                sessions[issue_key] = CloudTaskBinding(provider=provider, task_id=session_id, backend_name=backend_name)

                # Write back to file
                success = self._write_bindings(sessions)

                if success:
                    logger.info(f"Added session for issue #{issue_number}: session_id={session_id}")

                return success
            except Exception as e:
                logger.error(f"Failed to add session for issue #{issue_number}: {e}")
                return False

    def get_binding(self, issue_number: int) -> Optional[CloudTaskBinding]:
        """Return provider ownership, or ``None`` for unsafe legacy rows."""
        binding = self._read_bindings().get(str(issue_number))
        if binding and binding.provider and binding.task_id:
            return binding
        return None

    def get_session_id(self, issue_number: int) -> Optional[str]:
        """
        Get the session ID for an issue number.

        Args:
            issue_number: GitHub issue number

        Returns:
            Session ID if found, None otherwise
        """
        try:
            sessions = self._read_sessions()
            issue_key = str(issue_number)
            session_id = sessions.get(issue_key)

            if session_id:
                logger.debug(f"Found session for issue #{issue_number}: session_id={session_id}")
            else:
                logger.debug(f"No session found for issue #{issue_number}")

            return session_id
        except Exception as e:
            logger.error(f"Failed to get session for issue #{issue_number}: {e}")
            return None

    def is_managed(self, issue_number: int) -> bool:
        """
        Check if an issue number has a session.

        Args:
            issue_number: GitHub issue number

        Returns:
            True if the issue has a session, False otherwise
        """
        try:
            sessions = self._read_sessions()
            issue_key = str(issue_number)
            has_session = issue_key in sessions

            logger.debug(f"Issue #{issue_number} {'has' if has_session else 'does not have'} a session")

            return has_session
        except Exception as e:
            logger.error(f"Failed to check if issue #{issue_number} is managed: {e}")
            return False

    def get_issue_by_session(self, session_id: str) -> Optional[int]:
        """
        Get the issue number for a given session ID (reverse lookup).

        Args:
            session_id: Session ID to look up

        Returns:
            Issue number if found, None otherwise
        """
        try:
            sessions = self._read_sessions()
            # Reverse lookup: find issue number for given session_id
            for issue_number_str, stored_session_id in sessions.items():
                if stored_session_id == session_id:
                    issue_number = int(issue_number_str)
                    logger.debug(f"Found issue #{issue_number} for session_id={session_id}")
                    return issue_number

            logger.debug(f"No issue found for session_id={session_id}")
            return None
        except Exception as e:
            logger.error(f"Failed to lookup issue by session {session_id}: {e}")
            return None
