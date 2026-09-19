"""
Jules engine module for managing Jules sessions.
"""

import glob
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import yaml
from dateutil import parser

from .invocation_admission import InvocationHandle, InvocationStateError, current_invocation_gate
from .jules_client import JulesClient, JulesSessionRejectedError
from .llm_backend_config import get_jules_session_expiration_days_from_config
from .logger_config import get_logger
from .shutdown_context import new_work_allowed
from .util.gh_cache import GitHubClient

if TYPE_CHECKING:
    from .implementation_slots import ImplementationOwner, ImplementationSlotRepository

logger = get_logger(__name__)

STATE_FILE = os.path.join(os.getcwd(), ".auto-coder", "jules_session_state.json")

# Sentinel retry-state values (regular values are non-negative retry counts)
SESSION_STATE_NOT_FOUND = -1  # Session returned 404 on the server
SESSION_STATE_STOPPED = -2  # Session was stopped because it timed out without creating a PR


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


def normalize_session_outputs(outputs: Any) -> Dict[str, Any]:
    """Normalize the ``outputs`` field of a Jules session into a dict.

    The API returns a mapping, but some responses deliver it as a list of
    single-entry dicts or key/value pairs.
    """
    if isinstance(outputs, dict):
        return outputs

    if isinstance(outputs, list):
        normalized: Dict[str, Any] = {}
        for item in outputs:
            if isinstance(item, dict):
                normalized.update(item)
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                normalized[item[0]] = item[1]
        return normalized

    return {}


def get_session_pull_request(session: Dict[str, Any]) -> Any:
    """Return the pull request recorded in a Jules session's outputs, if any."""
    outputs = normalize_session_outputs(session.get("outputs", {}))
    return outputs.get("pullRequest") or outputs.get("pull_request")


def is_session_stopped(session_id: str) -> bool:
    """Return True if the session was stopped after failing to create a PR in time."""
    return _load_state().get(session_id) == SESSION_STATE_STOPPED


def mark_session_stopped(session_id: str) -> None:
    """Persist that a session was stopped so it is never resumed again."""
    state = _load_state()
    state[session_id] = SESSION_STATE_STOPPED
    _save_state(state)


def _admit_outbound_jules_send(
    session_id: str,
    issue_number: Optional[int],
    implementation_slots: Optional["ImplementationSlotRepository"],
) -> bool:
    """Guard + durably admit one outbound Jules mutation (Issue #2147, REQ-007/REQ-009).

    Must be called immediately before sending any outbound Jules mutation
    (resume, feedback, plan approval, replacement session, or publication
    request) that targets an ordinary Issue-owned implementation slot.

    Returns True when the caller may proceed with the outbound mutation.
    Returns False when the caller MUST NOT send it, either because:
    - retirement has already been durably committed for this session
      (``guard_retired_session_reuse``), or
    - the durable admission itself failed because the owner's slot has
      already been retired concurrently (``register_outbound_jules_activity``
      returning False), or
    - the admission write itself failed (raised), which must also block the
      send rather than be silently treated as permission to proceed.

    When ``implementation_slots`` is None (e.g. no repository context is
    available) or *issue_number* cannot be resolved to an ordinary Issue
    owner, this is a no-op that returns True — non-Jules-retirement-tracked
    callers and code paths untouched by Issue #2147 must behave exactly as
    before.
    """
    from .implementation_retirement_observer import admit_or_block_outbound_jules_send

    return admit_or_block_outbound_jules_send(session_id, issue_number, implementation_slots)


def check_and_resume_or_archive_sessions(
    repo_name: Optional[str] = None,
    implementation_slots: Optional["ImplementationSlotRepository"] = None,
) -> None:
    """Check for Jules sessions to resume or archive.

    - If state is FAILED: Resume with "ok" (only if automationMode is AUTO_CREATE_PR).
    - If state is AWAITING_USER_FEEDBACK, AWAITING_COMMENT, or AWAITING_COMMENTS:
        - Resume with "ok" (only if automationMode is AUTO_CREATE_PR).
    - If state is COMPLETED and no "outputs"/"pullRequest":
        - If retried < 5 times: Resume with "ok" (only if automationMode is AUTO_CREATE_PR).
        - If retried >= 5 times: Request to create PR.
    - If state is COMPLETED and has "outputs"/"pullRequest":
        - Check if PR is closed or merged.
        - If so, archive the session.

    implementation_slots: When provided, retired sessions are skipped before any
        resume or continuation attempt (REQ-009).
    """
    try:
        jules_client = JulesClient()
        sessions = jules_client.list_sessions(repo_name=repo_name)

        # Load retry state
        retry_state = _load_state()
        state_changed = False

        # Get GitHub client instance (should be initialized by AutomationEngine)
        try:
            github_client = GitHubClient.get_instance()
        except ValueError:
            # If not initialized (e.g. running in isolation), try to initialize with env token
            from .auth_utils import get_github_token

            token = get_github_token()
            if token:
                github_client = GitHubClient.get_instance(token=token)
            else:
                logger.warning("GitHubClient not initialized and no token found, skipping PR status checks")
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

            try:
                # Skip sessions that are known to be not found (404) on the server
                if retry_state.get(session_id) == SESSION_STATE_NOT_FOUND:
                    logger.debug(f"Skipping session {session_id} as it was previously not found (404) on the server.")
                    continue

                # Skip sessions that were stopped because they timed out without creating a PR
                if retry_state.get(session_id) == SESSION_STATE_STOPPED:
                    logger.debug(f"Skipping session {session_id} as it was stopped after failing to create a PR in time.")
                    continue

                # REQ-009: Do not automatically resume or continue a session that belongs to a
                # durably retired implementation slot. Retired sessions must not be revived by
                # stale maintenance or rediscovery scans.
                if implementation_slots is not None:
                    try:
                        from .implementation_retirement_observer import guard_retired_session_reuse

                        if guard_retired_session_reuse(session_id, implementation_slots):
                            logger.info(f"Skipping Jules session {session_id}: belongs to a durably retired " "implementation slot (REQ-009)")
                            continue
                    except Exception as guard_exc:
                        logger.warning(f"Could not check retirement guard for session {session_id}: {guard_exc}")

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

                # Check if session was created more than 7 days ago
                create_time_str = session.get("createTime")
                if create_time_str:
                    try:
                        create_time = parser.parse(create_time_str)
                        if (now - create_time) >= timedelta(days=7):
                            logger.debug(f"Ignoring Jules session older than 7 days: {session_id} (Created: {create_time})")
                            continue
                    except Exception as e:
                        logger.error(f"Failed to check creation time for session {session_id}: {e}")

                state = session.get("state")
                outputs = normalize_session_outputs(session.get("outputs", {}))
                pull_request = outputs.get("pullRequest") or outputs.get("pull_request")
                automation_mode = session.get("automationMode") or session.get("automation_mode")
                if automation_mode is None:
                    automation_mode = "AUTO_CREATE_PR"

                # Check if the associated issue or PR is closed or merged
                is_target_closed = False
                target_num: Optional[int] = None
                if github_client and repo_name:
                    try:
                        from .cloud_manager import CloudManager

                        cloud_manager = CloudManager(repo_name)
                        target_num = cloud_manager.get_issue_by_session(session_id)

                        if not target_num:
                            from .pr_processor import _find_issue_by_session_id_in_comments

                            target_num = _find_issue_by_session_id_in_comments(repo_name, session_id, github_client)

                        if not target_num and pull_request:
                            if isinstance(pull_request, dict):
                                target_num = pull_request.get("number")
                            elif isinstance(pull_request, str) and "github.com" in pull_request:
                                parts = pull_request.split("/")
                                if "pull" in parts:
                                    pull_idx = parts.index("pull")
                                    if pull_idx + 1 < len(parts):
                                        try:
                                            target_num = int(parts[pull_idx + 1])
                                        except ValueError:
                                            pass

                        if target_num:
                            issue = github_client.get_issue(repo_name, target_num)
                            if issue and issue.get("state") == "closed":
                                is_target_closed = True
                                logger.info(f"Target PR/Issue #{target_num} for session {session_id} is already closed/merged. Skipping resume.")
                    except Exception as e:
                        logger.warning(f"Failed to check target status for session {session_id}: {e}")

                # Check for specific error message
                error_msg = outputs.get("error", "")
                if error_msg and "Jules encountered an error" in str(error_msg):
                    if is_target_closed:
                        logger.info(f"Session {session_id} encountered an error but target is closed. Ignoring.")
                        continue

                    logger.info(f"Session {session_id} encountered an error: {error_msg}. Processing as a failed session requiring restart.")

                    try:
                        github_client = GitHubClient.get_instance()
                        from .cloud_manager import CloudManager

                        error_cloud_manager = CloudManager(repo_name) if repo_name else None
                        issue_num = error_cloud_manager.get_issue_by_session(session_id) if error_cloud_manager else None

                        pr_number = None
                        if not issue_num and repo_name:
                            from .pr_processor import _find_issue_by_session_id_in_comments

                            pr_number = _find_issue_by_session_id_in_comments(repo_name, session_id, github_client)

                        # If we found an issue or PR associated with this session
                        target_num = issue_num or pr_number
                        logger.info(f"Restarting session because Jules encountered an error.")
                        session_details = jules_client.get_session(session_id)
                        session_prompt = session_details.get("prompt")
                        if session_prompt:
                            from .managed_prompts import ManagedPromptRecoveryError, get_managed_prompt, recover_original_task

                            effective_repo = repo_name or "unknown"
                            # The saved session prompt may carry a managed
                            # instruction component (cloud_provider_instructions);
                            # recover the exact original task so the
                            # replacement session is rebuilt with exactly one
                            # current component rather than accumulating one
                            # or resending an opaque decorated payload
                            # (Issue #2091, REQ-004).
                            try:
                                original_task = recover_original_task(session_prompt, effective_repo, session_id)
                            except ManagedPromptRecoveryError as recovery_error:
                                logger.error(f"Refusing to restart session {session_id}: {recovery_error}")
                                raise

                            managed_record = get_managed_prompt(effective_repo, session_id)
                            recovered_no_edit = managed_record.no_edit if managed_record is not None else False

                            source_ctx = session_details.get("sourceContext", {})
                            base_branch = source_ctx.get("githubRepoContext", {}).get("startingBranch", "main")
                            if target_num:
                                title = session_details.get("title", f"Restarted session for issue/PR #{target_num}")
                            else:
                                title = session_details.get("title", f"Restarted session from {session_id}")

                            # REQ-007/REQ-009: a replacement session is new
                            # implementation-mutating provider responsibility;
                            # do not create it for an owner whose slot has
                            # already been durably retired, and durably admit
                            # the old session id before starting the replacement
                            # so a concurrent retirement observation is staled.
                            if not _admit_outbound_jules_send(session_id, target_num, implementation_slots):
                                logger.info(f"Skipping replacement session for retired Jules session {session_id} (REQ-007/REQ-009)")
                                retry_state[session_id] = -1
                                state_changed = True
                                state = "FAILED"
                                continue

                            new_session_id = jules_client.start_session(prompt=original_task, repo_name=effective_repo, base_branch=base_branch, is_noedit=recovered_no_edit, title=title)
                            logger.info(f"Started new session {new_session_id}")
                            if target_num:
                                if error_cloud_manager:
                                    error_cloud_manager.add_session(target_num, new_session_id, provider="jules")
                                if repo_name:
                                    comments = github_client.get_issue_comments(repo_name, target_num)
                                    comment_updated = False
                                    for comment in comments:
                                        body = comment.get("body")
                                        if isinstance(body, str) and session_id in body:
                                            new_body = body.replace(session_id, new_session_id)
                                            comment_id = comment.get("id")
                                            if isinstance(comment_id, int):
                                                github_client.update_comment_for_issue(repo_name, comment_id, new_body)
                                            comment_updated = True
                                            logger.info(f"Updated comment {comment_id} to reference new session {new_session_id}")
                                            break
                                    if not comment_updated:
                                        comment_body = f"I started a new Jules session to work on this issue because the previous one encountered an error. New Session ID: {new_session_id}\n\nhttps://jules.google.com/session/{new_session_id}"
                                        github_client.add_comment_to_issue(repo_name, target_num, comment_body)
                            logger.info(f"Ignoring failed session {session_id} as archive API is not available.")
                            retry_state[session_id] = -1
                            state_changed = True
                            state = "FAILED"
                            continue
                        else:
                            logger.warning(f"Could not get prompt for session {session_id} to restart it.")
                    except Exception as inner_e:
                        logger.error(f"Failed to handle error for session {session_id}: {inner_e}")

                    state = "FAILED"

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

                # Case 1: Failed session or Timeout -> Resume (only if automationMode is AUTO_CREATE_PR)
                if (state == "FAILED" or is_timeout) and automation_mode == "AUTO_CREATE_PR" and not is_target_closed:
                    if not _admit_outbound_jules_send(session_id, target_num, implementation_slots):
                        logger.info(f"Skipping resume of Jules session {session_id}: retirement guard blocked the outbound mutation (REQ-007/REQ-009)")
                        continue
                    logger.info(f"Resuming failed/timed-out Jules session: {session_id}")
                    try:
                        jules_client.send_message(session_id, "ok")
                        logger.info(f"Successfully sent resume message to session {session_id}")
                        # Reset retry count if exists
                        if session_id in retry_state:
                            del retry_state[session_id]
                            state_changed = True
                    except Exception as e:
                        if "HTTP 404" in str(e) or "NOT_FOUND" in str(e) or "404" in str(e):
                            logger.warning(f"Jules session {session_id} not found on server (404) during resume. Mark as NOT_FOUND.")
                            retry_state[session_id] = -1
                            state_changed = True
                        else:
                            logger.error(f"Failed to resume session {session_id}: {e}")

                # Case 4: Awaiting Plan Approval -> Approve Plan
                elif state == "AWAITING_PLAN_APPROVAL" and not is_target_closed:
                    if not _admit_outbound_jules_send(session_id, target_num, implementation_slots):
                        logger.info(f"Skipping plan approval for Jules session {session_id}: retirement guard blocked the outbound mutation (REQ-007/REQ-009)")
                        continue
                    logger.info(f"Approving plan for Jules session: {session_id}")
                    try:
                        if jules_client.approve_plan(session_id):
                            logger.info(f"Successfully approved plan for session {session_id}")
                            state_changed = True
                        else:
                            logger.error(f"Failed to approve plan for session {session_id}")
                    except Exception as e:
                        if "HTTP 404" in str(e) or "NOT_FOUND" in str(e) or "404" in str(e):
                            logger.warning(f"Jules session {session_id} not found on server (404) during plan approval. Mark as NOT_FOUND.")
                            retry_state[session_id] = -1
                            state_changed = True
                        else:
                            logger.error(f"Failed to approve plan for session {session_id}: {e}")
                # Case 2: Awaiting User Feedback, Comments, or Completed session without PR -> Resume with retry logic (only if automationMode is AUTO_CREATE_PR)
                elif (
                    ((state in ("AWAITING_USER_FEEDBACK", "AWAITING_COMMENT", "AWAITING_COMMENTS") or (isinstance(state, str) and state.startswith("AWAITING_") and state != "AWAITING_PLAN_APPROVAL")) or (state == "COMPLETED" and not pull_request))
                    and automation_mode == "AUTO_CREATE_PR"
                    and not is_target_closed
                ):
                    if not _admit_outbound_jules_send(session_id, target_num, implementation_slots):
                        logger.info(f"Skipping continuation of Jules session {session_id}: retirement guard blocked the outbound mutation (REQ-007/REQ-009)")
                        continue
                    retry_count = retry_state.get(session_id, 0)

                    if retry_count < 5:
                        logger.info(f"Resuming completed Jules session (no PR) [Attempt {retry_count + 1}]: {session_id}")
                        try:
                            jules_client.send_message(session_id, "ok")
                            logger.info(f"Successfully sent resume message to session {session_id}")
                            retry_state[session_id] = retry_count + 1
                            state_changed = True
                        except Exception as e:
                            if "HTTP 404" in str(e) or "NOT_FOUND" in str(e) or "404" in str(e):
                                logger.warning(f"Jules session {session_id} not found on server (404) during resume. Mark as NOT_FOUND.")
                                retry_state[session_id] = -1
                                state_changed = True
                            else:
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
                            if "HTTP 404" in str(e) or "NOT_FOUND" in str(e) or "404" in str(e):
                                logger.warning(f"Jules session {session_id} not found on server (404) during force PR. Mark as NOT_FOUND.")
                                retry_state[session_id] = -1
                                state_changed = True
                            else:
                                logger.error(f"Failed to send force PR message to session {session_id}: {e}")

                # Case 3: Completed session with PR -> Check PR status and Archive if closed/merged
                elif state == "COMPLETED" and pull_request and github_client:
                    # Clear retry state if exists (success case)
                    if session_id in retry_state:
                        del retry_state[session_id]
                        state_changed = True

                    try:
                        # Extract PR info
                        repo_name_pr = None
                        pr_number = None

                        if isinstance(pull_request, dict):
                            pr_number = pull_request.get("number")
                            # Try to get repo name from PR data or session context
                            # Assuming pullRequest dict might have repo info
                            if "repository" in pull_request:
                                repo_name_pr = pull_request["repository"].get("name")  # format owner/repo?
                                if not repo_name_pr and "full_name" in pull_request["repository"]:
                                    repo_name_pr = pull_request["repository"]["full_name"]
                            # Try to parse repository and PR number from URL if repository is missing but URL is present
                            if not repo_name_pr and "url" in pull_request:
                                url = pull_request["url"]
                                if isinstance(url, str) and "github.com" in url:
                                    parts = url.split("/")
                                    if "pull" in parts:
                                        pull_idx = parts.index("pull")
                                        if pull_idx > 2 and pull_idx + 1 < len(parts):
                                            repo_name_pr = f"{parts[pull_idx-2]}/{parts[pull_idx-1]}"
                                            if not pr_number:
                                                try:
                                                    pr_number = int(parts[pull_idx + 1])
                                                except ValueError:
                                                    pass

                        elif isinstance(pull_request, str) and "github.com" in pull_request:
                            # Parse URL: https://github.com/owner/repo/pull/123
                            parts = pull_request.split("/")
                            if "pull" in parts:
                                pull_idx = parts.index("pull")
                                if pull_idx > 2 and pull_idx + 1 < len(parts):
                                    repo_name_pr = f"{parts[pull_idx-2]}/{parts[pull_idx-1]}"
                                    try:
                                        pr_number = int(parts[pull_idx + 1])
                                    except ValueError:
                                        pass

                        if repo_name_pr and pr_number:
                            # Check PR status
                            pr = github_client.get_pull_request(repo_name_pr, pr_number)

                            if pr and pr.get("state") == "closed":
                                action = "merged" if pr.get("merged") else "closed"
                                logger.info(f"PR #{pr_number} is {action}. Archiving Jules session: {session_id}")
                                if jules_client.archive_session(session_id):
                                    logger.info(f"Successfully archived session {session_id}")
                                else:
                                    logger.error(f"Failed to archive session {session_id}")
                        else:
                            logger.debug(f"Could not extract PR info from session {session_id} outputs: {pull_request}")

                    except Exception as e:
                        if "HTTP 404" in str(e) or "NOT_FOUND" in str(e) or "404" in str(e):
                            logger.warning(f"Jules session {session_id} not found on server (404) during check PR. Mark as NOT_FOUND.")
                            retry_state[session_id] = -1
                            state_changed = True
                        else:
                            logger.warning(f"Failed to check PR status or archive session {session_id}: {e}")
            except Exception as e:
                logger.error(f"Unexpected error processing session {session_id}: {e}")

        # Save state if changed
        if state_changed:
            _save_state(retry_state)

    except Exception as e:
        logger.warning(f"Failed to check/resume/archive Jules sessions: {e}")


def _parse_prompt_file_content(content: str) -> tuple[dict, str]:
    """Parse YAML frontmatter and prompt content from prompt string."""
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", content, re.DOTALL)
    if match:
        frontmatter_str = match.group(1)
        prompt_text = match.group(2)
        try:
            metadata = yaml.safe_load(frontmatter_str) or {}
            if not isinstance(metadata, dict):
                metadata = {}
        except Exception as e:
            logger.warning(f"Failed to parse YAML frontmatter: {e}")
            metadata = {}
        return metadata, prompt_text
    else:
        return {}, content


def _parse_prompt_file(file_path: str) -> tuple[dict, str]:
    """Read a prompt file and parse its frontmatter and full content."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        logger.warning(f"Failed to read prompt file {file_path}: {e}")
        return {}, ""
    metadata, _ = _parse_prompt_file_content(content)
    return metadata, content


def _normalize_tags(tags: Any) -> List[str]:
    """Normalize tags from frontmatter metadata into a list of lowercase strings."""
    if not tags:
        return []
    if isinstance(tags, str):
        parts = tags.split(",")
        res = []
        for part in parts:
            res.extend([p.strip().lower() for p in part.split() if p.strip()])
        return res
    elif isinstance(tags, list):
        res = []
        for t in tags:
            res.extend(_normalize_tags(t))
        return res
    return [str(tags).strip().lower()]


def _recurrent_implementation_owner(repo_name: str, file_path: str) -> "ImplementationOwner":
    """Build a stable logical owner for a repository recurrent prompt."""
    from .implementation_slots import ImplementationOwner

    identity = f"{repo_name}:{os.path.basename(file_path)}"
    number = int.from_bytes(hashlib.sha256(identity.encode("utf-8")).digest()[:8], "big")
    return ImplementationOwner("recurrent", number)


def _session_pull_request_number(pull_request: object) -> Optional[int]:
    """Extract a PR number from Jules session metadata."""
    if isinstance(pull_request, dict):
        number = pull_request.get("number")
        if isinstance(number, int) and not isinstance(number, bool):
            return number
        pull_request = pull_request.get("url", "")
    if isinstance(pull_request, str):
        match = re.search(r"/pull/(\d+)(?:\D|$)", pull_request)
        if match:
            return int(match.group(1))
    return None


def _settle_unresolved_jules_invocation(handle: Optional[InvocationHandle]) -> None:
    """Settle a Jules submission invocation that failed before its receipt was durably confirmed.

    A rejection or transport failure leaves nothing reusable to protect: the
    invocation is moved to CHECKPOINTING (if it had not already reached that
    state) and settled immediately, mirroring how backend_manager settles a
    terminal provider failure (Issue #2009, REQ-004/REQ-006).
    """
    if handle is None:
        return
    try:
        handle.begin_checkpointing("terminal_failure")
    except InvocationStateError:
        pass
    handle.confirm_settled()


def check_and_start_recurrent_jules_tasks(
    repo_name: str,
    implementation_slots: Optional["ImplementationSlotRepository"] = None,
) -> None:
    """Scan .auto-coder/prompts/*.md files and start recurrent Jules tasks if not already running."""
    try:
        prompts_dir = os.path.join(os.getcwd(), ".auto-coder", "prompts")
        if not os.path.isdir(prompts_dir):
            logger.debug(f"Prompts directory {prompts_dir} does not exist. Skipping.")
            return

        md_files = glob.glob(os.path.join(prompts_dir, "*.md"))
        if not md_files:
            logger.debug(f"No prompt files (*.md) found in {prompts_dir}")
            return

        jules_client = JulesClient()
        try:
            sessions = jules_client.list_sessions(repo_name=repo_name)
        except Exception as e:
            logger.error(f"Failed to list Jules sessions: {e}")
            return

        for file_path in md_files:
            metadata, full_prompt = _parse_prompt_file(file_path)
            tags = metadata.get("tags", [])
            name_val = metadata.get("name", [])

            # Normalize tags and name
            tag_list = _normalize_tags(tags)

            if isinstance(name_val, str):
                names = [name_val.strip()]
            elif isinstance(name_val, list):
                names = [str(n).strip() for n in name_val]
            else:
                names = []

            if not ("jules" in tag_list and "recurrent" in tag_list):
                continue

            if not names:
                logger.warning(f"Prompt file {file_path} has jules and recurrent tags but no valid name. Skipping.")
                continue

            owner = _recurrent_implementation_owner(repo_name, file_path)
            terminal_owner = False
            discovered_prs: set[int] = set()

            is_running = False
            for session in sessions:
                session_id = session.get("name", "").split("/")[-1]
                if not session_id:
                    session_id = session.get("id")
                if not session_id:
                    continue

                session_prompt = session.get("prompt")
                if not session_prompt:
                    try:
                        full_session = jules_client.get_session(session_id)
                        session_prompt = full_session.get("prompt")
                        session = full_session
                    except Exception as e:
                        logger.warning(f"Failed to get full session for {session_id} to check prompt: {e}")

                if not session_prompt:
                    continue

                # Composition (cloud_provider_instructions) only ever appends
                # after the complete original task, so frontmatter anchored
                # at position 0 survives decoration and this matches
                # correctly straight off the saved/decorated prompt. A
                # best-effort recovery is still attempted so a future
                # composition change couldn't silently break identity
                # matching; a missing/corrupt managed record must never turn
                # into "no matching session" (Issue #2091, REQ-005), so any
                # recovery failure just falls back to the raw session prompt.
                from .managed_prompts import ManagedPromptRecoveryError, recover_original_task

                try:
                    matched_prompt = recover_original_task(session_prompt, repo_name, session_id)
                except ManagedPromptRecoveryError:
                    matched_prompt = session_prompt

                session_metadata, _ = _parse_prompt_file_content(matched_prompt)
                session_names_val = session_metadata.get("name", [])
                if isinstance(session_names_val, str):
                    session_names = [session_names_val.strip()]
                elif isinstance(session_names_val, list):
                    session_names = [str(n).strip() for n in session_names_val]
                else:
                    session_names = []

                match_found = False
                for n in names:
                    for sn in session_names:
                        if n.strip().lower() == sn.strip().lower():
                            match_found = True
                            break
                    if match_found:
                        break

                if match_found:
                    if implementation_slots is not None:
                        if not implementation_slots.record_provider_session(owner, str(session_id)):
                            raise RuntimeError(f"Lost recurrent implementation ownership for {owner.key}")
                    # Check if the session is completed and merged/closed on GitHub
                    state = session.get("state")
                    pull_request = get_session_pull_request(session)
                    pr_number = _session_pull_request_number(pull_request)
                    if implementation_slots is not None and pr_number is not None:
                        discovered_prs.add(pr_number)
                        implementation_slots.record_implementation_pr(owner, pr_number)
                    if state == "COMPLETED" and pull_request:
                        try:
                            github_client = GitHubClient.get_instance()
                        except ValueError:
                            from .auth_utils import get_github_token

                            token = get_github_token()
                            if token:
                                github_client = GitHubClient.get_instance(token=token)
                            else:
                                github_client = None

                        if github_client:
                            repo_name_pr = None
                            pr_number = None

                            if isinstance(pull_request, dict):
                                pr_number = pull_request.get("number")
                                if "repository" in pull_request:
                                    repo_name_pr = pull_request["repository"].get("name")
                                    if not repo_name_pr and "full_name" in pull_request["repository"]:
                                        repo_name_pr = pull_request["repository"]["full_name"]
                                if not repo_name_pr and "url" in pull_request:
                                    url = pull_request["url"]
                                    if isinstance(url, str) and "github.com" in url:
                                        parts = url.split("/")
                                        if "pull" in parts:
                                            pull_idx = parts.index("pull")
                                            if pull_idx > 2 and pull_idx + 1 < len(parts):
                                                repo_name_pr = f"{parts[pull_idx-2]}/{parts[pull_idx-1]}"
                                                if not pr_number:
                                                    try:
                                                        pr_number = int(parts[pull_idx + 1])
                                                    except ValueError:
                                                        pass
                            elif isinstance(pull_request, str) and "github.com" in pull_request:
                                parts = pull_request.split("/")
                                if "pull" in parts:
                                    pull_idx = parts.index("pull")
                                    if pull_idx > 2 and pull_idx + 1 < len(parts):
                                        repo_name_pr = f"{parts[pull_idx-2]}/{parts[pull_idx-1]}"
                                        try:
                                            pr_number = int(parts[pull_idx + 1])
                                        except ValueError:
                                            pass

                            if repo_name_pr and pr_number:
                                try:
                                    pr = github_client.get_pull_request(repo_name_pr, pr_number)
                                    if pr and pr.get("state") == "closed":
                                        logger.info(f"Session {session_id} has a closed/merged PR #{pr_number}. Not considering it as running.")
                                        terminal_owner = True
                                        continue
                                except Exception as e:
                                    logger.warning(f"Failed to check PR status for session {session_id}: {e}")

                    logger.info(f"Found active Jules session '{session_id}' matching name: {names}")
                    is_running = True
                    break

            if not is_running:
                if not new_work_allowed():
                    logger.info(f"Deferring recurrent Jules prompt {names}: graceful shutdown is draining")
                    continue
                logger.info(f"No active Jules session found for recurrent prompt: {names}. Starting a new Jules session...")
                if implementation_slots is not None and terminal_owner:
                    implementation_slots.release(owner)
                if implementation_slots is not None and not implementation_slots.reserve_new(owner):
                    logger.info(f"Deferring recurrent Jules implementation {owner.key}: no implementation slot available")
                    continue
                if implementation_slots is not None:
                    for pr_number in discovered_prs:
                        if not implementation_slots.record_implementation_pr(owner, pr_number):
                            raise RuntimeError(f"Lost recurrent implementation ownership for {owner.key}")
                submission_attempted = False
                # Local submission of this asynchronous remote task is itself a
                # qualifying invocation (Issue #2009, REQ-006): protect it from
                # invocation start through the durable receipt write in
                # `record_provider_session`, not through Jules' own completion.
                gate = current_invocation_gate()
                invocation_handle = gate.try_admit(repository=repo_name, target=owner.key, stage="jules_remote_dispatch") if gate is not None else None
                if gate is not None and invocation_handle is None:
                    if implementation_slots is not None:
                        implementation_slots.release(owner)
                    logger.info(f"Deferring recurrent Jules submission for {owner.key}: graceful shutdown is draining")
                    continue
                try:
                    from .automation_config import AutomationConfig

                    config = AutomationConfig()
                    base_branch = config.MAIN_BRANCH

                    session_title = names[0]
                    if implementation_slots is None:
                        new_session_id = jules_client.start_session(prompt=full_prompt, repo_name=repo_name, base_branch=base_branch, title=session_title)
                        if invocation_handle is not None:
                            invocation_handle.begin_checkpointing("remote_handoff")
                            invocation_handle.confirm_settled(confirmation_id=str(new_session_id))
                    else:
                        with implementation_slots.serialize(owner):
                            # Once the provider request begins, an exception can
                            # mean an ambiguous transport outcome rather than a
                            # definite rejection. Retain ownership until a later
                            # authoritative provider scan recovers the session.
                            submission_attempted = True
                            new_session_id = jules_client.start_session(prompt=full_prompt, repo_name=repo_name, base_branch=base_branch, title=session_title)
                        # The local submission returned; its receipt still needs a
                        # durable write before this invocation may settle.
                        if invocation_handle is not None:
                            invocation_handle.begin_checkpointing("remote_handoff")
                        if not implementation_slots.record_provider_session(owner, str(new_session_id)):
                            if invocation_handle is not None:
                                invocation_handle.record_checkpoint_attempt_failed("lost recurrent implementation ownership before receipt was recorded")
                            raise RuntimeError(f"Lost recurrent implementation ownership for {owner.key}")
                        if invocation_handle is not None:
                            invocation_handle.confirm_settled(confirmation_id=str(new_session_id))
                    logger.info(f"Successfully started new recurrent Jules session '{new_session_id}' for {names}")
                except JulesSessionRejectedError as e:
                    if implementation_slots is not None:
                        implementation_slots.release(owner)
                    _settle_unresolved_jules_invocation(invocation_handle)
                    logger.error(f"Jules rejected recurrent session creation for {names}: {e}")
                except Exception as e:
                    # Submission failure means no external implementation was
                    # created.  Once Jules accepts the task, however, uncertain
                    # metadata persistence must fail closed and retain capacity.
                    if implementation_slots is not None and not submission_attempted:
                        implementation_slots.release(owner)
                    _settle_unresolved_jules_invocation(invocation_handle)
                    logger.error(f"Failed to start new recurrent Jules session for {names}: {e}")

    except Exception as e:
        logger.error(f"Error checking/starting recurrent Jules tasks: {e}")


def check_and_restart_recurrent_jules_task_for_pr(repo_name: str, pr_number: int, session_id: str) -> None:
    """Check if the merged PR's Jules session has matching recurrent prompt and restart it if so."""
    try:
        jules_client = JulesClient()
        logger.info(f"Checking if merged PR #{pr_number} (session: {session_id}) was a recurrent task...")

        try:
            session = jules_client.get_session(session_id)
        except Exception as e:
            logger.warning(f"Failed to get session details for {session_id}: {e}")
            return

        session_prompt = session.get("prompt")
        if not session_prompt:
            logger.info(f"No startup prompt found in session {session_id}")
            return

        # See the matching comment in check_and_start_recurrent_jules_tasks:
        # frontmatter survives decoration, so matching off the raw saved
        # prompt is already correct; best-effort recovery is attempted but
        # never allowed to turn a missing/corrupt managed record into "no
        # match" (Issue #2091, REQ-005).
        from .managed_prompts import ManagedPromptRecoveryError, recover_original_task

        try:
            matched_prompt = recover_original_task(session_prompt, repo_name, session_id)
        except ManagedPromptRecoveryError:
            matched_prompt = session_prompt

        session_metadata, _ = _parse_prompt_file_content(matched_prompt)
        session_names_val = session_metadata.get("name", [])
        if isinstance(session_names_val, str):
            session_names = [session_names_val.strip()]
        elif isinstance(session_names_val, list):
            session_names = [str(n).strip() for n in session_names_val]
        else:
            session_names = []

        if not session_names:
            logger.info(f"No names found in frontmatter of session {session_id}'s prompt")
            return

        logger.info(f"Merged session names: {session_names}")

        prompts_dir = os.path.join(os.getcwd(), ".auto-coder", "prompts")
        if not os.path.isdir(prompts_dir):
            logger.debug(f"Prompts directory {prompts_dir} does not exist.")
            return

        md_files = glob.glob(os.path.join(prompts_dir, "*.md"))
        if not md_files:
            return

        for file_path in md_files:
            metadata, full_prompt = _parse_prompt_file(file_path)
            tags = metadata.get("tags", [])
            name_val = metadata.get("name", [])

            # Normalize tags and name
            tag_list = _normalize_tags(tags)

            if isinstance(name_val, str):
                names = [name_val.strip()]
            elif isinstance(name_val, list):
                names = [str(n).strip() for n in name_val]
            else:
                names = []

            if not ("jules" in tag_list and "recurrent" in tag_list):
                continue

            match_found = False
            for n in names:
                for sn in session_names:
                    if n.strip().lower() == sn.strip().lower():
                        match_found = True
                        break
                if match_found:
                    break

            if match_found:
                logger.info(f"Found matching recurrent prompt file: {file_path} for merged session {session_id}")
                try:
                    from .automation_config import AutomationConfig

                    config = AutomationConfig()
                    base_branch = config.MAIN_BRANCH

                    session_title = names[0]
                    new_session_id = jules_client.start_session(prompt=full_prompt, repo_name=repo_name, base_branch=base_branch, title=session_title)
                    logger.info(f"Successfully started new recurrent Jules session '{new_session_id}' after merge of PR #{pr_number}")
                except Exception as e:
                    logger.error(f"Failed to start new recurrent Jules session after merge: {e}")

    except Exception as e:
        logger.error(f"Error in check_and_restart_recurrent_jules_task_for_pr: {e}")
