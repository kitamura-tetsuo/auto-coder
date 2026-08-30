"""
Claude Routine HTTP API client for Auto-Coder.

Claude Routine is an asynchronous cloud coding agent that executes tasks on
Anthropic-managed cloud infrastructure via routine trigger endpoints.
Reference: https://code.claude.com/docs/en/routines
"""

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests  # type: ignore
from requests.adapters import HTTPAdapter  # type: ignore
from urllib3.util.retry import Retry

from .claude_usage_checker import check_claude_usage, check_claude_usage_or_raise
from .cloud_task_client_base import CloudTask, CloudTaskClientBase, CloudTaskState
from .exceptions import AutoCoderUsageLimitError
from .llm_backend_config import get_llm_config
from .logger_config import get_logger
from .usage_marker_utils import has_usage_marker_match
from .utils import CommandExecutor

logger = get_logger(__name__)

STATE_FILE = os.path.join(os.getcwd(), ".auto-coder", "claude_routine_state.json")


def _load_claude_routine_state(state_file: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Load Claude Routine session state from file."""
    path = state_file or STATE_FILE
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load Claude Routine state: {e}")
    return {}


def _save_claude_routine_state(state: Dict[str, Dict[str, Any]], state_file: Optional[str] = None) -> None:
    """Save Claude Routine session state to file."""
    path = state_file or STATE_FILE
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.warning(f"Failed to save Claude Routine state: {e}")


class ClaudeRoutineClient(CloudTaskClientBase):
    """Claude Routine HTTP API client for asynchronous cloud routine execution."""

    def __init__(self, backend_name: Optional[str] = None, repo_name: Optional[str] = None) -> None:
        """Initialize Claude Routine HTTP API client.

        Args:
            backend_name: Backend name to use for configuration lookup (optional).
            repo_name: Repository name to resolve repository-specific overrides (optional).
        """
        self.backend_name = backend_name or "claude-routine"
        self.repo_name = repo_name
        self.timeout = 30
        self.active_sessions: Dict[str, str] = {}  # session_id -> prompt
        self.token: Optional[str] = None
        self.url: Optional[str] = None

        # Load configuration for this backend
        config = get_llm_config(repo_name=self.repo_name)
        config_backend = config.get_backend_config(self.backend_name)

        self.options = (config_backend and config_backend.options) or []
        self.options_for_noedit = (config_backend and config_backend.options_for_noedit) or []
        self.options_for_resume = (config_backend and config_backend.options_for_resume) or []
        self.token = (config_backend and (config_backend.claude_code_routine_token or config_backend.claude_code_oauth_token or config_backend.api_key)) or None
        self.url = (config_backend and (config_backend.url or config_backend.base_url)) or None

        if not self.token:
            logger.warning("No token configured for Claude Routine. API calls may fail with 401 Unauthorized.")
        if not self.url:
            logger.warning("No trigger URL configured for Claude Routine. API calls may fail.")

        # Create HTTP session with retry strategy
        self.session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        # Set default headers
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "User-Agent": "auto-coder/1.0",
                "anthropic-beta": "experimental-cc-routine-2026-04-01",
                "anthropic-version": "2023-06-01",
            }
        )
        if self.token:
            self.session.headers["Authorization"] = f"Bearer {self.token}"

    def fire_routine(
        self,
        prompt: str,
        repo_name: str = "",
        base_branch: str = "",
        title: Optional[str] = None,
    ) -> Tuple[str, Optional[str]]:
        """Fire a Claude routine via HTTP POST to its trigger endpoint.

        Args:
            prompt: The task prompt to send in the fire text payload
            repo_name: Repository name (optional)
            base_branch: Base branch name (optional)
            title: Optional title/description for logging

        Returns:
            Tuple of (session_id, session_url)
        """
        check_claude_usage_or_raise(token=self.token, backend_name=self.backend_name)

        if not self.url:
            raise ValueError(f"No URL configured for Claude Routine backend '{self.backend_name}'. " "Please configure 'url' in llm_config.toml.")

        payload: Dict[str, Any] = {"text": prompt}

        logger.info(f"Firing Claude Routine on {self.url} (title={title or 'N/A'})")
        logger.info(f"🤖 POST {self.url}")

        try:
            response = self.session.post(self.url, json=payload, timeout=self.timeout)

            if response.status_code not in [200, 201, 202]:
                error_msg = f"HTTP {response.status_code}: {response.text}"
                logger.error(f"Failed to fire Claude Routine: {error_msg}")
                if response.status_code == 429 or has_usage_marker_match(response.text, self.options) or "rate_limit" in response.text.lower():
                    raise AutoCoderUsageLimitError(f"Claude Routine rate limit exceeded: {error_msg}")
                raise RuntimeError(f"Failed to fire Claude Routine: {error_msg}")

            try:
                response_data = response.json()
            except json.JSONDecodeError:
                logger.warning(f"Could not parse JSON response from Claude Routine fire: {response.text}")
                response_data = {}

            session_id = response_data.get("claude_code_session_id") or response_data.get("sessionId") or response_data.get("session_id") or response_data.get("id")
            session_url = response_data.get("claude_code_session_url") or response_data.get("sessionUrl") or response_data.get("url")

            if not session_id:
                session_id = f"session_{int(time.time())}"
                logger.warning(f"Could not extract session ID from response, using generated ID: {session_id}")

            self.active_sessions[session_id] = prompt
            state = _load_claude_routine_state()
            state[session_id] = {
                "created_at": time.time(),
                "last_continued_at": 0.0,
                "continue_count": 0,
                "prompt": prompt,
                "pull_request": None,
            }
            _save_claude_routine_state(state)

            logger.info(f"Successfully fired Claude Routine: session_id={session_id}, session_url={session_url}")
            return session_id, session_url

        except AutoCoderUsageLimitError:
            raise
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fire Claude Routine: {e}")
            raise RuntimeError(f"Failed to fire Claude Routine: {e}")

    def start_session(
        self,
        prompt: str,
        repo_name: str,
        base_branch: str,
        is_noedit: bool = False,
        title: Optional[str] = None,
    ) -> str:
        """Start a new session by firing the Claude Routine.

        Args:
            prompt: The prompt to send
            repo_name: Repository name
            base_branch: Base branch name
            is_noedit: Whether this is a noedit run
            title: Optional session title

        Returns:
            Session ID of the routine session
        """
        session_id, _ = self.fire_routine(prompt, repo_name=repo_name, base_branch=base_branch, title=title)
        return session_id

    def _run_llm_cli(self, prompt: str, is_noedit: bool = False) -> str:
        """Run Claude Routine with given prompt.

        Implements LLMClientBase interface.
        """
        session_id, session_url = self.fire_routine(prompt)
        return f"Claude Routine fired successfully. Session ID: {session_id}, URL: {session_url}"

    def close(self) -> None:
        """Close HTTP session."""
        if self.session:
            self.session.close()

    def check_mcp_server_configured(self, server_name: str) -> bool:
        """Check if MCP server is configured."""
        return False

    def add_mcp_server_config(self, server_name: str, command: str, args: list[str]) -> bool:
        """Add MCP server configuration."""
        return False

    def start_task(
        self,
        prompt: str,
        repo_name: str = "",
        base_branch: str = "",
        title: Optional[str] = None,
    ) -> str:
        """Start a new Claude Routine task.

        Args:
            prompt: The task prompt
            repo_name: Repository name (optional)
            base_branch: Base branch name (optional)
            title: Optional title

        Returns:
            Session ID string
        """
        session_id, _ = self.fire_routine(prompt, repo_name=repo_name, base_branch=base_branch, title=title)
        return session_id

    def _get_session_url(self, session_id: str) -> str:
        """Construct the session URL or API endpoint for a session ID."""
        if self.url and "/routines/" in self.url:
            base_api = self.url.split("/routines/")[0]
            return f"{base_api}/sessions/{session_id}"
        return f"https://api.anthropic.com/v1/claude_code/sessions/{session_id}"

    def get_task(self, task_id: str) -> Optional[CloudTask]:
        """Get the current normalized task state for a Claude Routine session."""
        state = _load_claude_routine_state()
        session_info = state.get(task_id, {})
        pr = session_info.get("pull_request")
        created_at_ts = session_info.get("created_at")
        created_at_dt = datetime.fromtimestamp(created_at_ts, tz=timezone.utc) if created_at_ts else None

        session_api_url = self._get_session_url(task_id)
        try:
            response = self.session.get(session_api_url, timeout=self.timeout)
            if response.status_code == 200:
                data = response.json()
                raw_state = (data.get("status") or data.get("state") or "").lower()

                pr = pr or data.get("pull_request") or data.get("pullRequest") or data.get("pr_url")

                if raw_state in ("completed", "finished", "success") or pr:
                    task_state = CloudTaskState.COMPLETED
                elif raw_state in ("failed", "error", "cancelled"):
                    task_state = CloudTaskState.FAILED
                elif raw_state in ("paused", "waiting_for_input", "idle", "stopped", "awaiting_user_input"):
                    task_state = CloudTaskState.PAUSED
                elif raw_state in ("running", "in_progress", "active"):
                    # Absence of PR serves as substitute pause indicator
                    task_state = CloudTaskState.PAUSED if not pr else CloudTaskState.RUNNING
                elif raw_state in ("queued", "pending"):
                    task_state = CloudTaskState.QUEUED
                else:
                    task_state = CloudTaskState.PAUSED if not pr else CloudTaskState.UNKNOWN

                return CloudTask(
                    task_id=task_id,
                    state=task_state,
                    raw_state=raw_state,
                    title=data.get("title"),
                    pull_request=pr,
                    prompt=data.get("prompt") or session_info.get("prompt") or self.active_sessions.get(task_id),
                    created_at=created_at_dt,
                    url=data.get("url") or f"https://claude.ai/code/{task_id}",
                    error=data.get("error"),
                    raw_data=data,
                )
        except Exception as e:
            logger.debug(f"Could not fetch Claude routine task details via API for {task_id}: {e}")

        # Fallback to local tracking: Absence of created PR indicates paused state
        if task_id in self.active_sessions or session_info:
            task_state = CloudTaskState.COMPLETED if pr else CloudTaskState.PAUSED
            return CloudTask(
                task_id=task_id,
                state=task_state,
                pull_request=pr,
                created_at=created_at_dt,
                prompt=session_info.get("prompt") or self.active_sessions.get(task_id),
                url=f"https://claude.ai/code/{task_id}",
            )

        return None

    def continue_if_paused(self, task_id: str, message: str = "continue") -> bool:
        """Resume an existing Claude routine session if no PR has been created.

        Criteria:
        - Absence of a created PR for the session serves as the substitute indication of paused state.
        - Continues every 1 hour up to 5 hours from session start (up to 5 continuation attempts).
        - Sends continuation message using `claude -p --cloud <task_id> <message>`.

        Args:
            task_id: The Claude session ID to resume.
            message: Message to send (default: "continue").

        Returns:
            True if continuation message was sent, False otherwise.
        """
        state = _load_claude_routine_state()
        session_info = state.get(task_id, {})
        now = time.time()

        # Check if PR is already created
        task = self.get_task(task_id)
        pr = (task and task.pull_request) or session_info.get("pull_request")
        if pr:
            logger.debug(f"Claude Routine session {task_id} already has a PR ({pr}); skipping continuation")
            return False

        created_at = session_info.get("created_at")
        if created_at is None:
            # If not tracked yet, initialize tracking starting now
            created_at = now
            session_info["created_at"] = created_at
            session_info["last_continued_at"] = 0.0
            session_info["continue_count"] = 0
            session_info["prompt"] = self.active_sessions.get(task_id, "")
            state[task_id] = session_info
            _save_claude_routine_state(state)

        elapsed_since_start = now - created_at

        # Session start to 5 hours limit
        if elapsed_since_start > 5 * 3600:
            logger.info(f"Claude Routine session {task_id} exceeded 5-hour window without PR; no more continuation")
            return False

        continue_count = session_info.get("continue_count", 0)
        if continue_count >= 5:
            logger.info(f"Claude Routine session {task_id} reached maximum continuation attempts (5)")
            return False

        last_continued_at = session_info.get("last_continued_at", 0.0)

        # Check if 1 hour has elapsed since start (for 1st attempt) or since last continuation
        time_since_last = (now - last_continued_at) if last_continued_at > 0 else elapsed_since_start
        if time_since_last < 3600:
            logger.debug(f"Claude Routine session {task_id} not yet due for continuation " f"(elapsed since last: {time_since_last / 60:.1f}m < 60m, attempts: {continue_count})")
            return False

        # Check Claude usage before sending continuation
        quota = check_claude_usage(token=self.token)
        if quota.is_quota_insufficient:
            logger.warning(f"Claude usage limit reached before continuation for session {task_id}: {quota.reason}")
            return False

        # Send continue message via `claude -p --cloud <session_id> <message>`
        cmd = ["claude", "-p", f"--cloud={task_id}", message]
        env = os.environ.copy()
        if self.token:
            env["CLAUDE_CODE_ROUTINE_TOKEN"] = self.token
            env["CLAUDE_CODE_OAUTH_TOKEN"] = self.token

        logger.info(f"Sending '{message}' to Claude Routine session {task_id} via claude CLI (attempt {continue_count + 1}/5)")
        try:
            result = CommandExecutor.run_command(cmd, env=env if len(env) > len(os.environ) else None)
            if result.returncode == 0:
                session_info["continue_count"] = continue_count + 1
                session_info["last_continued_at"] = now
                state[task_id] = session_info
                _save_claude_routine_state(state)
                logger.info(f"Successfully sent '{message}' to Claude Routine session {task_id}")
                return True
            else:
                logger.warning(f"Failed to send continuation to Claude Routine session {task_id} (code {result.returncode}): {result.stderr or result.stdout}")
                return False
        except Exception as e:
            logger.warning(f"Error sending continuation to Claude Routine session {task_id}: {e}")
            return False

    def list_tasks(self, repo_name: Optional[str] = None) -> List[CloudTask]:
        """List active or recent Claude Routine sessions."""
        tasks: List[CloudTask] = []
        if self.url and "/routines/" in self.url:
            base_api = self.url.split("/routines/")[0]
            sessions_url = f"{base_api}/sessions"
            try:
                response = self.session.get(sessions_url, timeout=self.timeout)
                if response.status_code == 200:
                    data = response.json()
                    sessions_list = data.get("sessions", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
                    for s in sessions_list:
                        if isinstance(s, dict):
                            s_id = s.get("id") or s.get("session_id") or s.get("claude_code_session_id")
                            if s_id:
                                task = self.get_task(s_id)
                                if task:
                                    tasks.append(task)
                    if tasks:
                        return tasks
            except Exception as e:
                logger.debug(f"Could not list Claude sessions from API: {e}")

        # Return tracked active sessions as fallback
        for session_id, prompt in self.active_sessions.items():
            tasks.append(
                CloudTask(
                    task_id=session_id,
                    prompt=prompt,
                    state=CloudTaskState.UNKNOWN,
                    url=f"https://claude.ai/code/{session_id}",
                )
            )
        return tasks

    def stop_task(self, task_id: str) -> bool:
        """Stop or cancel a Claude Routine session."""
        session_url = self._get_session_url(task_id)
        stopped = False
        try:
            response = self.session.post(f"{session_url}/cancel", json={}, timeout=self.timeout)
            if response.status_code in (200, 204):
                stopped = True
        except Exception:
            pass

        if task_id in self.active_sessions:
            del self.active_sessions[task_id]
            stopped = True

        return stopped
