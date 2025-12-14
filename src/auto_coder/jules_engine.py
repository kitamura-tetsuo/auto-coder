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

            session_id = session.get("name", "").split("/")[-1]
            if not session_id:
                session_id = session.get("id")

            if not session_id:
                continue

            state = session.get("state")
            outputs = session.get("outputs", {})
            pull_request = outputs.get("pullRequest")

            # Case 1: Failed session -> Resume
            if state == "FAILED":
                logger.info(f"Resuming failed Jules session: {session_id}")
                try:
                    jules_client.send_message(session_id, "ok")
                    logger.info(f"Successfully sent resume message to session {session_id}")
                    # Reset retry count if exists
                    if session_id in retry_state:
                        del retry_state[session_id]
                        state_changed = True
                except Exception as e:
                    logger.error(f"Failed to resume session {session_id}: {e}")
                except Exception as e:
                    logger.error(f"Failed to resume session {session_id}: {e}")

            # Case 4: Awaiting Plan Approval -> Approve Plan
            elif state == "AWAITING_PLAN_APPROVAL":
                logger.info(f"Approving plan for Jules session: {session_id}")
                try:
                    if jules_client.approve_plan(session_id):
                        logger.info(f"Successfully approved plan for session {session_id}")
                        state_changed = True
                    else:
                        logger.error(f"Failed to approve plan for session {session_id}")
                except Exception as e:
                    logger.error(f"Failed to approve plan for session {session_id}: {e}")
            # Case 2: Completed session without PR -> Resume with retry logic
            elif state == "COMPLETED" and not pull_request:
                retry_count = retry_state.get(session_id, 0)

                if retry_count < 2:
                    logger.info(f"Resuming completed Jules session (no PR) [Attempt {retry_count + 1}]: {session_id}")
                    try:
                        jules_client.send_message(session_id, "ok")
                        logger.info(f"Successfully sent resume message to session {session_id}")
                        retry_state[session_id] = retry_count + 1
                        state_changed = True
                    except Exception as e:
                        logger.error(f"Failed to resume session {session_id}: {e}")
                else:
                    logger.info(f"Resuming completed Jules session (no PR) [Force PR]: {session_id}")
                    try:
                        jules_client.send_message(session_id, "Please create a PR with the current code")
                        logger.info(f"Successfully sent force PR message to session {session_id}")
                        # Reset count to 0 to restart cycle if needed
                        retry_state[session_id] = 0
                        state_changed = True
                    except Exception as e:
                        logger.error(f"Failed to send force PR message to session {session_id}: {e}")

            # Case 3: Completed session with PR -> Check PR status and Archive if closed/merged
            elif state == "COMPLETED" and pull_request and github_client:
                # Clear retry state if exists (success case)
                if session_id in retry_state:
                    del retry_state[session_id]
                    state_changed = True

                try:
                    # Extract PR info
                    repo_name = None
                    pr_number = None

                    if isinstance(pull_request, dict):
                        pr_number = pull_request.get("number")
                        # Try to get repo name from PR data or session context
                        # Assuming pullRequest dict might have repo info
                        if "repository" in pull_request:
                            repo_name = pull_request["repository"].get("name")  # format owner/repo?
                            if not repo_name and "full_name" in pull_request["repository"]:
                                repo_name = pull_request["repository"]["full_name"]

                    elif isinstance(pull_request, str) and "github.com" in pull_request:
                        # Parse URL: https://github.com/owner/repo/pull/123
                        parts = pull_request.split("/")
                        if "pull" in parts:
                            pull_idx = parts.index("pull")
                            if pull_idx > 2 and pull_idx + 1 < len(parts):
                                repo_name = f"{parts[pull_idx-2]}/{parts[pull_idx-1]}"
                                try:
                                    pr_number = int(parts[pull_idx + 1])
                                except ValueError:
                                    pass

                    if repo_name and pr_number:
                        # Check PR status
                        repo = github_client.get_repository(repo_name)
                        pr = repo.get_pull(pr_number)

                        if pr.state == "closed":
                            action = "merged" if pr.merged else "closed"
                            logger.info(f"PR #{pr_number} is {action}. Archiving Jules session: {session_id}")
                            if jules_client.archive_session(session_id):
                                logger.info(f"Successfully archived session {session_id}")
                            else:
                                logger.error(f"Failed to archive session {session_id}")
                    else:
                        logger.debug(f"Could not extract PR info from session {session_id} outputs: {pull_request}")

                except Exception as e:
                    logger.warning(f"Failed to check PR status or archive session {session_id}: {e}")

        # Save state if changed
        if state_changed:
            _save_state(retry_state)

    except Exception as e:
        logger.warning(f"Failed to check/resume/archive Jules sessions: {e}")
