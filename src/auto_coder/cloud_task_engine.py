"""
Cloud Task Engine: Unified orchestration for asynchronous cloud coding tasks.

Manages cloud tasks from Jules, Claude Routine, Codex Cloud, and other
CloudTaskClientBase providers uniformly at the orchestration layer.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from dateutil import parser

from .cloud_manager import CloudManager
from .cloud_task_client_base import CloudTask, CloudTaskClientBase, CloudTaskState
from .llm_backend_config import active_repo_context
from .logger_config import get_logger
from .util.gh_cache import GitHubClient

logger = get_logger(__name__)

STATE_FILE = os.path.join(os.getcwd(), ".auto-coder", "cloud_task_state.json")


def _load_cloud_task_state(state_file: str = STATE_FILE) -> Dict[str, int]:
    """Load cloud task retry / status state from file."""
    if os.path.exists(state_file):
        try:
            with open(state_file, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load cloud task state: {e}")
    return {}


def _save_cloud_task_state(state: Dict[str, int], state_file: str = STATE_FILE) -> None:
    """Save cloud task retry / status state to file."""
    try:
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        with open(state_file, "w") as f:
            json.dump(state, f)
    except Exception as e:
        logger.warning(f"Failed to save cloud task state: {e}")


class CloudTaskEngine:
    """Orchestrates cloud tasks across multiple CloudTaskClientBase implementations."""

    def __init__(
        self,
        clients: Optional[List[CloudTaskClientBase]] = None,
        state_file: str = STATE_FILE,
    ) -> None:
        """Initialize CloudTaskEngine.

        Args:
            clients: Optional list of CloudTaskClientBase instances.
            state_file: Optional path to state file.
        """
        self.clients = clients or []
        self.state_file = state_file

    def get_registered_clients(self, repo_name: Optional[str] = None) -> List[CloudTaskClientBase]:
        """Return list of active cloud task clients."""
        if self.clients:
            return self.clients

        # Automatically instantiate available cloud clients
        discovered: List[CloudTaskClientBase] = []
        try:
            from .jules_client import JulesClient

            discovered.append(JulesClient())
        except Exception as e:
            logger.debug(f"Could not load JulesClient in CloudTaskEngine: {e}")

        try:
            from .claude_routine_client import ClaudeRoutineClient

            discovered.append(ClaudeRoutineClient(repo_name=repo_name))
        except Exception as e:
            logger.debug(f"Could not load ClaudeRoutineClient in CloudTaskEngine: {e}")

        try:
            from .codex_cloud_client import CodexCloudClient

            discovered.append(CodexCloudClient(repo_name=repo_name))
        except Exception as e:
            logger.debug(f"Could not load CodexCloudClient in CloudTaskEngine: {e}")

        return discovered

    def check_and_resume_tasks(self, repo_name: Optional[str] = None) -> List[str]:
        """Check all cloud tasks across registered providers and resume paused tasks.

        Args:
            repo_name: Optional repository name to filter tasks by.

        Returns:
            List of actions performed.
        """
        with active_repo_context(repo_name):
            actions: List[str] = []
            clients = self.get_registered_clients(repo_name=repo_name)
            retry_state = _load_cloud_task_state(self.state_file)
            state_changed = False

            # Get GitHub client if available
            github_client = None
            try:
                github_client = GitHubClient.get_instance()
            except Exception:
                try:
                    from .auth_utils import get_github_token

                    token = get_github_token()
                    if token:
                        github_client = GitHubClient.get_instance(token=token)
                except Exception:
                    pass

            now = datetime.now(timezone.utc)

            for client in clients:
                try:
                    tasks = client.list_tasks(repo_name=repo_name)
                except Exception as e:
                    logger.warning(f"Failed to list tasks for client {client.__class__.__name__}: {e}")
                    continue

                for task in tasks:
                    task_id = task.task_id
                    if not task_id:
                        continue

                    try:
                        # Skip if task was stopped or marked not found (-1 / -2)
                        if retry_state.get(task_id, 0) < 0:
                            continue

                        # Check expiration (older than 7 days)
                        if task.created_at:
                            created = task.created_at if task.created_at.tzinfo else task.created_at.replace(tzinfo=timezone.utc)
                            if (now - created) >= timedelta(days=7):
                                logger.debug(f"Ignoring cloud task older than 7 days: {task_id}")
                                continue

                        # Check if associated target is closed or merged
                        is_target_closed = False
                        if github_client and repo_name:
                            cloud_manager = CloudManager(repo_name)
                            target_num = cloud_manager.get_issue_by_session(task_id)

                            if not target_num and task.pull_request:
                                if isinstance(task.pull_request, dict):
                                    target_num = task.pull_request.get("number")
                                elif isinstance(task.pull_request, str) and "github.com" in task.pull_request:
                                    parts = task.pull_request.split("/")
                                    if "pull" in parts:
                                        p_idx = parts.index("pull")
                                        if p_idx + 1 < len(parts):
                                            try:
                                                target_num = int(parts[p_idx + 1])
                                            except ValueError:
                                                pass

                            if target_num:
                                try:
                                    issue = github_client.get_issue(repo_name, target_num)
                                    if issue and issue.get("state") == "closed":
                                        is_target_closed = True
                                        logger.info(f"Target PR/Issue #{target_num} for task {task_id} is closed/merged.")
                                except Exception:
                                    pass

                        if is_target_closed:
                            continue

                        # Call client-specific continue_if_paused abstraction
                        resumed = client.continue_if_paused(task_id)
                        if resumed:
                            action_msg = f"Resumed paused cloud task '{task_id}' ({client.__class__.__name__})"
                            logger.info(action_msg)
                            actions.append(action_msg)
                            retry_state[task_id] = retry_state.get(task_id, 0) + 1
                            state_changed = True

                    except Exception as e:
                        logger.error(f"Error processing cloud task {task_id}: {e}")

            if state_changed:
                _save_cloud_task_state(retry_state, self.state_file)

            return actions
