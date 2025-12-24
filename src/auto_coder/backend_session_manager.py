"""
BackendSessionManager: Persist session metadata for LLM backends.

This module stores the last-used backend and session ID so we can resume
stateful sessions when the same backend is invoked consecutively.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from .logger_config import get_logger

logger = get_logger(__name__)

# Default session state file path in user's home directory
DEFAULT_SESSION_STATE_FILE = "~/.auto-coder/backend_session_state.json"


@dataclass
class BackendSessionState:
    """Session state persisted between runs."""

    last_backend: Optional[str] = None
    last_session_id: Optional[str] = None
    last_used_timestamp: float = 0.0


class BackendSessionManager:
    """
    Manages persistence of backend session information.

    The session state is stored separately from backend rotation state so that
    we can resume sessions across process restarts without altering backend
    auto-reset behavior.
    """

    def __init__(self, state_file_path: str | None = None):
        self._state_file_path = state_file_path or DEFAULT_SESSION_STATE_FILE
        self._lock = threading.Lock()

    def get_state_file_path(self) -> str:
        """Return the absolute path for the session state file."""
        expanded_path = Path(self._state_file_path).expanduser()
        return str(expanded_path.resolve())

    def save_state(self, state: BackendSessionState) -> bool:
        """
        Persist the given session state atomically.

        Args:
            state: BackendSessionState instance to save

        Returns:
            True on success, False otherwise.
        """
        with self._lock:
            try:
                state_file_path = Path(self.get_state_file_path())
                state_file_path.parent.mkdir(parents=True, exist_ok=True)

                payload = asdict(state)
                temp_file_path = state_file_path.with_suffix(".tmp")

                # Use os.open to ensure file is created with 600 permissions
                try:
                    fd = os.open(str(temp_file_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                except OSError:
                    # Fallback to standard open if os.open fails
                    with open(temp_file_path, "w", encoding="utf-8") as f:
                        try:
                            os.chmod(temp_file_path, 0o600)
                        except OSError:
                            pass  # Ignore permission errors on systems that don't support it
                        json.dump(payload, f, indent=2)
                else:
                    # File opened successfully with os.open
                    try:
                        f = os.fdopen(fd, "w", encoding="utf-8")
                    except Exception:
                        os.close(fd)
                        raise

                    with f:
                        # Ensure permissions are correct even if file already existed
                        try:
                            os.chmod(temp_file_path, 0o600)
                        except OSError:
                            pass  # Ignore permission errors on systems that don't support it
                        json.dump(payload, f, indent=2)

                temp_file_path.replace(state_file_path)
                logger.debug(
                    "Saved backend session state: backend=%s session_id_set=%s timestamp=%.0f file=%s",
                    state.last_backend,
                    bool(state.last_session_id),
                    state.last_used_timestamp,
                    state_file_path,
                )
                return True
            except (OSError, IOError, PermissionError, TypeError) as exc:  # pragma: no cover - defensive
                logger.error("Failed to save backend session state to %s: %s", self.get_state_file_path(), exc)
                return False

    def load_state(self) -> BackendSessionState:
        """
        Load persisted session state.

        Returns:
            BackendSessionState with stored values or defaults when unavailable.
        """
        with self._lock:
            try:
                state_file_path = Path(self.get_state_file_path())

                if not state_file_path.exists():
                    logger.debug("Session state file does not exist: %s", state_file_path)
                    return BackendSessionState()

                with open(state_file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                if not isinstance(data, dict):
                    logger.warning("Session state file contains invalid data format (not a dict): %s", state_file_path)
                    return BackendSessionState()

                last_backend = data.get("last_backend")
                last_session_id = data.get("last_session_id")
                last_used_timestamp_raw = data.get("last_used_timestamp", 0.0)
                try:
                    last_used_timestamp = float(last_used_timestamp_raw)
                except (TypeError, ValueError):
                    last_used_timestamp = 0.0

                return BackendSessionState(
                    last_backend=last_backend if isinstance(last_backend, str) else None,
                    last_session_id=last_session_id if isinstance(last_session_id, str) else None,
                    last_used_timestamp=last_used_timestamp,
                )
            except (OSError, IOError, PermissionError, json.JSONDecodeError) as exc:  # pragma: no cover - defensive
                logger.error("Failed to load backend session state from %s: %s", self.get_state_file_path(), exc)
                return BackendSessionState()


def create_session_state(backend: Optional[str], session_id: Optional[str]) -> BackendSessionState:
    """Helper to build a session state with the current timestamp."""
    return BackendSessionState(
        last_backend=backend,
        last_session_id=session_id,
        last_used_timestamp=time.time(),
    )
