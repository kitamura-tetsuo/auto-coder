"""
BackendStateManager: Handles persistence of LLM backend state.

This module provides a helper class to manage the persistence of backend state
information including the current backend and the timestamp of the last switch.
This state is saved to a JSON file for persistence across application restarts.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Dict

from .logger_config import get_logger

logger = get_logger(__name__)

# Default state file path in user's home directory
DEFAULT_STATE_FILE = "~/.auto-coder/backend_state.json"


class BackendStateManager:
    """
    Manages persistence of LLM backend state.

    This class handles saving and loading the current backend state to/from
    a JSON file, providing thread-safe operations for state persistence.

    State Schema:
    ------------
    {
        "current_backend": "string",
        "last_switch_timestamp": float
    }

    Thread Safety:
    -------------
    This class uses a lock to ensure thread-safe file operations.
    """

    def __init__(self, state_file_path: str | None = None):
        """
        Initialize the BackendStateManager.

        Args:
            state_file_path: Optional custom path for the state file.
                            If not provided, uses the default: ~/.auto-coder/backend_state.json
        """
        self._state_file_path = state_file_path or DEFAULT_STATE_FILE
        self._lock = threading.Lock()

    def get_state_file_path(self) -> str:
        """
        Get the absolute path to the state file.

        Returns:
            Absolute path to the state file as a string
        """
        expanded_path = Path(self._state_file_path).expanduser()
        return str(expanded_path.resolve())

    def save_state(self, current_backend: str, timestamp: float) -> bool:
        """
        Save the current backend state to the state file.

        This method is thread-safe and will create the necessary directories
        if they don't exist.

        Args:
            current_backend: Name of the current backend
            timestamp: Timestamp of the last backend switch

        Returns:
            True if state was saved successfully, False otherwise
        """
        with self._lock:
            try:
                state_data = {
                    "current_backend": current_backend,
                    "last_switch_timestamp": timestamp,
                }

                state_file_path = Path(self.get_state_file_path())

                # Ensure the parent directory exists
                state_file_path.parent.mkdir(parents=True, exist_ok=True)

                # Write state to temporary file first for atomic operation
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
                        json.dump(state_data, f, indent=2)
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
                        json.dump(state_data, f, indent=2)

                # Atomically replace the old file with the new one
                temp_file_path.replace(state_file_path)

                logger.debug(f"Saved backend state: backend={current_backend}, " f"timestamp={timestamp}, file={state_file_path}")
                return True

            except (OSError, IOError, PermissionError, TypeError) as e:
                logger.error(f"Failed to save backend state to {self.get_state_file_path()}: {e}")
                return False

    def load_state(self) -> Dict[str, str | float]:
        """
        Load the backend state from the state file.

        This method is thread-safe and handles missing or corrupted state files gracefully.

        Returns:
            Dictionary containing the state with keys:
                - "current_backend": str
                - "last_switch_timestamp": float
            Returns empty dict if file doesn't exist or cannot be read
        """
        with self._lock:
            try:
                state_file_path = Path(self.get_state_file_path())

                if not state_file_path.exists():
                    logger.debug(f"State file does not exist: {state_file_path}. " "Returning empty state.")
                    return {}

                with open(state_file_path, "r") as f:
                    state_data = json.load(f)

                # Validate the loaded data
                if not isinstance(state_data, dict):
                    logger.warning(f"State file contains invalid data format (not a dict). " f"File: {state_file_path}. Returning empty state.")
                    return {}

                # Ensure required fields are present
                if "current_backend" not in state_data:
                    logger.warning(f"State file missing 'current_backend' field. " f"File: {state_file_path}. Returning empty state.")
                    return {}

                if "last_switch_timestamp" not in state_data:
                    logger.warning(f"State file missing 'last_switch_timestamp' field. " f"File: {state_file_path}. Returning empty state.")
                    return {}

                logger.debug(f"Loaded backend state: backend={state_data.get('current_backend')}, " f"timestamp={state_data.get('last_switch_timestamp')}")
                return state_data

            except (OSError, IOError, PermissionError, json.JSONDecodeError) as e:
                logger.error(f"Failed to load backend state from {self.get_state_file_path()}: {e}")
                return {}
