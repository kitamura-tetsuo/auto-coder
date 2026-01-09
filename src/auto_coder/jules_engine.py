"""
Jules engine module for managing Jules sessions.
"""

import json
import os
from typing import Dict

from .github_client import GitHubClient
from .jules_client import JulesClient
from .logger_config import get_logger

logger = get_logger(__name__)

STATE_FILE = os.path.join(os.getcwd(), ".auto-coder", "jules_session_state.json")


def _load_state() -> Dict[str, int]:
    """Load Jules session state from file."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load Jules session state: {e}")
    return {}


def _save_state(state: Dict[str, int]) -> None:
    """Save Jules session state to file."""
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        logger.warning(f"Failed to save Jules session state: {e}")


def check_and_resume_or_archive_sessions() -> None:
    """Check for Jules sessions to resume or archive.

    - If state is FAILED: Resume with "ok".
    - If state is COMPLETED and no "outputs"/"pullRequest":
        - If retried < 2 times: Resume with "ok".
        - If retried >= 2 times: Request to create PR.
    - If state is COMPLETED and has "outputs"/"pullRequest":
        - Check if PR is closed or merged.
        - If so, archive the session.
    """
    try:
        jules_client = JulesClient()
        sessions = jules_client.list_sessions()

        # Load retry state
        retry_state = _load_state()
        state_changed = False

        # Get GitHub client instance (should be initialized by AutomationEngine)
        try:
            github_client = GitHubClient.get_instance()
        except ValueError:
            # If not initialized (e.g. running in isolation), we can't check PR status
            logger.warning("GitHubClient not initialized, skipping PR status checks")
            github_client = None

        for session in sessions:
            if isinstance(session, list):
                try:
                    session = dict(session)
                except Exception as e:
                    logger.warning(f"Failed to convert list session to dict: {session} - {e}")
                    continue

            if not isinstance(session, dict):
                logger.warning(f"Skipping invalid session object (expected dict, got {type(session)}): {session}")
                continue

            try:
                if jules_client.process_session_status(session, retry_state, github_client):
                    state_changed = True
            except Exception as e:
                logger.error(f"Error processing session status: {e}")

        # Save state if changed
        if state_changed:
            _save_state(retry_state)

    except Exception as e:
        logger.warning(f"Failed to check/resume/archive Jules sessions: {e}")
