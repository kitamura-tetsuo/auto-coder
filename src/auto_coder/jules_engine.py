"""
Jules engine module for managing Jules sessions.
"""

import glob
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import yaml
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


def check_and_resume_or_archive_sessions(repo_name: Optional[str] = None) -> None:
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
                if retry_state.get(session_id) == -1:
                    logger.debug(f"Skipping session {session_id} as it was previously not found (404) on the server.")
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

                pull_request = outputs.get("pullRequest") or outputs.get("pull_request")
                automation_mode = session.get("automationMode") or session.get("automation_mode")
                if automation_mode is None:
                    automation_mode = "AUTO_CREATE_PR"

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
                if (state == "FAILED" or is_timeout) and automation_mode == "AUTO_CREATE_PR":
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
                elif state == "AWAITING_PLAN_APPROVAL":
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
                elif ((state in ("AWAITING_USER_FEEDBACK", "AWAITING_COMMENT", "AWAITING_COMMENTS") or (isinstance(state, str) and state.startswith("AWAITING_") and state != "AWAITING_PLAN_APPROVAL")) or (state == "COMPLETED" and not pull_request)) and automation_mode == "AUTO_CREATE_PR":
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


def check_and_start_recurrent_jules_tasks(repo_name: str) -> None:
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

                session_metadata, _ = _parse_prompt_file_content(session_prompt)
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
                    # Check if the session is completed and merged/closed on GitHub
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
                            logger.warning(f"Failed to convert list outputs to dict: {e}")
                            outputs = {}

                    pull_request = outputs.get("pullRequest") or outputs.get("pull_request")
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
                                        continue
                                except Exception as e:
                                    logger.warning(f"Failed to check PR status for session {session_id}: {e}")

                    logger.info(f"Found active Jules session '{session_id}' matching name: {names}")
                    is_running = True
                    break

            if not is_running:
                logger.info(f"No active Jules session found for recurrent prompt: {names}. Starting a new Jules session...")
                try:
                    from .automation_config import AutomationConfig

                    config = AutomationConfig()
                    base_branch = config.MAIN_BRANCH

                    session_title = names[0]
                    new_session_id = jules_client.start_session(prompt=full_prompt, repo_name=repo_name, base_branch=base_branch, title=session_title)
                    logger.info(f"Successfully started new recurrent Jules session '{new_session_id}' for {names}")
                except Exception as e:
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

        session_metadata, _ = _parse_prompt_file_content(session_prompt)
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
