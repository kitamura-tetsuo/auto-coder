"""
Jules engine module for managing Jules sessions.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Dict

from dateutil import parser

from .jules_client import JulesClient
from .llm_backend_config import get_jules_session_expiration_days_from_config
from .logger_config import get_logger
from .util.gh_cache import GitHubClient

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
        - If retried < 5 times: Resume with "ok".
        - If retried >= 5 times: Request to create PR.
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

        now = datetime.now(timezone.utc)
        expiration_days = get_jules_session_expiration_days_from_config()
        expiration_date_threshold = now - timedelta(days=expiration_days)
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

            # Check if session is expired
            update_time_str = session.get("updateTime")
            if update_time_str:
                try:
                    update_time = parser.parse(update_time_str)
                    if update_time < expiration_date_threshold:
                        logger.debug(f"Ignoring expired Jules session: {session_id} (Last updated: {update_time}, Expiration: {expiration_days} days)")
                        continue
                except Exception as e:
                    logger.error(f"Failed to check expiration for session {session_id}: {e}")

            state = session.get("state")
            outputs = session.get("outputs", {})
            if isinstance(outputs, list):
                try:
                    new_outputs = {}
                    for item in outputs:
                        if isinstance(item, dict):
                            new_outputs.update(item)
                        elif isinstance(item, (list, tuple)) and len(item) == 2:
                            new_outputs[item[0]] = item[1]
                    outputs = new_outputs
                except Exception as e:
                    logger.warning(f"Failed to convert list outputs to dict: {outputs} - {e}")
                    outputs = {}

            pull_request = outputs.get("pullRequest")

            # Check for timeout if IN_PROGRESS
            is_timeout = False
            if state == "IN_PROGRESS":
                update_time_str = session.get("updateTime")
                if update_time_str:
                    try:
                        update_time = parser.parse(update_time_str)
                        now = datetime.now(timezone.utc)
                        if (now - update_time) > timedelta(minutes=5):
                            logger.info(f"Session {session_id} is IN_PROGRESS but timed out (> 5 mins). Treating as FAILED.")
                            is_timeout = True
                    except Exception as e:
                        logger.warning(f"Failed to parse updateTime for session {session_id}: {e}")

            # Case 1: Failed session or Timeout -> Resume
            if state == "FAILED" or is_timeout:
                logger.info(f"Resuming failed/timed-out Jules session: {session_id}")
                try:
                    jules_client.send_message(session_id, "ok")
                    logger.info(f"Successfully sent resume message to session {session_id}")
                    # Reset retry count if exists
                    if session_id in retry_state:
                        del retry_state[session_id]
                        state_changed = True
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
            # Case 2: Awaiting User Feedback or Completed session without PR -> Resume with retry logic
            elif state == "AWAITING_USER_FEEDBACK" or (state == "COMPLETED" and not pull_request):
                retry_count = retry_state.get(session_id, 0)

                if retry_count < 5:
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
                        pr = github_client.get_pull_request(repo_name, pr_number)

                        if pr is not None and pr.get("state") == "closed":
                            action = "merged" if pr.get("merged") else "closed"
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
