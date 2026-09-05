"""
Codex Cloud client for Auto-Coder.

Manages asynchronous Codex Cloud task execution and lifecycle management via the Codex CLI.
Reference: https://github.com/openai/codex
"""

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .cloud_task_client_base import CloudTask, CloudTaskClientBase, CloudTaskState
from .codex_cloud_task import extract_codex_cloud_task_id, is_valid_codex_cloud_task_id
from .codex_usage_checker import codex_cloud_quota_allows_task
from .codex_wham_client import CodexWhamClient, FollowUpDeliveryOutcome
from .exceptions import AutoCoderUsageLimitError
from .llm_backend_config import get_llm_config
from .logger_config import get_logger
from .prompt_loader import render_prompt
from .utils import CommandExecutor

logger = get_logger(__name__)

_followup_state_lock = threading.Lock()


@dataclass
class PendingCodexFollowUp:
    """Durable evidence needed to reconcile an ambiguous WHAM POST."""

    task_id: str = ""
    message_fingerprint: str = ""
    pre_send_turn_id: str = ""
    status: str = "indeterminate"


def _codex_followup_state_path(repo_name: Optional[str]) -> Path:
    repository = repo_name or "default"
    return Path.home() / ".auto-coder" / repository / "codex_followups.json"


def _load_pending_followups(path: Path) -> dict[str, PendingCodexFollowUp]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Codex follow-up state is not an object")
    return {key: PendingCodexFollowUp(**value) for key, value in data.items() if isinstance(key, str) and isinstance(value, dict)}


def _save_pending_followups(path: Path, records: dict[str, PendingCodexFollowUp]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps({key: asdict(value) for key, value in records.items()}, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


class CodexCloudClient(CloudTaskClientBase):
    """Codex Cloud client for asynchronous cloud task execution and lifecycle management."""

    def __init__(self, backend_name: Optional[str] = None, repo_name: Optional[str] = None) -> None:
        """Initialize Codex Cloud client.

        Args:
            backend_name: Backend name to use for configuration lookup (optional).
            repo_name: Repository name to resolve repository-specific overrides (optional).
        """
        super().__init__()
        self.backend_name = backend_name or "codex-cloud"
        self.repo_name = repo_name
        self.active_tasks: Dict[str, str] = {}  # task_id -> prompt
        self.task_urls: Dict[str, str] = {}  # task_id -> url
        self._warned_unsupported_models: set[str] = set()

        config = get_llm_config(repo_name=self.repo_name)
        self.quota_selection_strategy = config.quota_selection_strategy
        self.config_backend = config.get_backend_config(self.backend_name)
        self._warn_unsupported_model(self.config_backend.model if self.config_backend else None)
        self.options = (self.config_backend and self.config_backend.options) or []
        self.options_for_noedit = (self.config_backend and self.config_backend.options_for_noedit) or []
        self.api_key = self.config_backend and self.config_backend.api_key
        self.base_url = self.config_backend and self.config_backend.base_url
        self.usage_markers = (self.config_backend and self.config_backend.usage_markers) or []

        # Optional environment ID for Codex Cloud executions
        self.environment_id: Optional[str] = (self.config_backend and (self.config_backend.environment_id or getattr(self.config_backend, "environment", None))) or os.environ.get("CODEX_CLOUD_ENV_ID") or os.environ.get("CODEX_ENVIRONMENT_ID")
        self.attempts = (self.config_backend and self.config_backend.attempts) or 1
        self.wham_client: Optional[CodexWhamClient] = None
        self.last_continued_at: Dict[str, float] = {}

    def _warn_unsupported_model(self, model_name: Optional[str]) -> None:
        """Warn when configuration requests unsupported Cloud model selection."""
        if model_name and model_name not in self._warned_unsupported_models:
            logger.warning(f"Codex Cloud does not support user-selected models; configured model " f"'{model_name}' for backend '{self.backend_name}' is being ignored.")
            self._warned_unsupported_models.add(model_name)

    def _extract_task_id(self, output: str) -> Optional[str]:
        """Extract a task ID or task URL from Codex Cloud CLI output.

        Task IDs typically follow the shape: `task_e_6a26c19ac8a88326af83ebfb44b89fe2`
        Task URLs typically follow: `https://chatgpt.com/codex/tasks/task_e_...`
        """
        if not output:
            return None

        # Check for JSON output first
        try:
            data = json.loads(output)
            if isinstance(data, dict):
                tid = data.get("task_id") or data.get("taskId") or data.get("id")
                if is_valid_codex_cloud_task_id(tid):
                    return str(tid).strip()
                url = data.get("url") or data.get("task_url")
                if url:
                    task_id = extract_codex_cloud_task_id(url)
                    if task_id:
                        return task_id
        except Exception:
            pass

        # Check for task URL in text output
        task_id = extract_codex_cloud_task_id(output)
        if task_id:
            return task_id

        # Generic task_id pattern
        generic_match = re.search(r"(?:task\s*id|task_id)[:=]\s*([a-zA-Z0-9_-]+)", output, re.IGNORECASE)
        if generic_match and is_valid_codex_cloud_task_id(generic_match.group(1)):
            return generic_match.group(1)

        return None

    def _extract_task_url(self, output: str) -> Optional[str]:
        """Extract a task URL from Codex Cloud CLI output."""
        if not output:
            return None

        url_match = re.search(r"(https?://[^\s]+/codex/tasks/[a-zA-Z0-9_-]+)", output)
        if url_match:
            return url_match.group(1)
        return None

    def start_task(
        self,
        prompt: str,
        repo_name: str = "",
        base_branch: str = "",
        title: Optional[str] = None,
    ) -> str:
        """Start a new asynchronous cloud task on Codex Cloud.

        Executes:
            codex cloud exec [--env <ENVIRONMENT_ID>] [--branch <BRANCH>] "<PROMPT>"

        Args:
            prompt: The task prompt / instruction.
            repo_name: Repository name (optional).
            base_branch: Base branch name (optional).
            title: Optional task title (for logging).

        Returns:
            The created Task ID.
        """
        logger.info(f"Starting Codex Cloud task (title={title or 'N/A'}, branch={base_branch or 'N/A'})")

        effective_repo = repo_name or self.repo_name
        if effective_repo:
            config = get_llm_config(repo_name=effective_repo)
            self.quota_selection_strategy = config.quota_selection_strategy
            backend_cfg = config.get_backend_config(self.backend_name)
            if backend_cfg:
                env_id = backend_cfg.environment_id or getattr(backend_cfg, "environment", None)
                if env_id:
                    self.environment_id = env_id
                self._warn_unsupported_model(backend_cfg.model)
                if backend_cfg.options:
                    self.options = backend_cfg.options
                if backend_cfg.api_key:
                    self.api_key = backend_cfg.api_key
                if backend_cfg.base_url:
                    self.base_url = backend_cfg.base_url
                if backend_cfg.attempts:
                    self.attempts = backend_cfg.attempts

        if not codex_cloud_quota_allows_task(strategy=self.quota_selection_strategy):
            raise AutoCoderUsageLimitError("Codex weekly quota is unavailable under the configured quota strategy")

        cmd = ["codex", "cloud", "exec"]

        if not self.environment_id:
            raise ValueError(f"No environment_id configured for Codex Cloud backend '{self.backend_name}'. " "Set environment_id in llm_config.toml or CODEX_CLOUD_ENV_ID.")
        cmd.extend(["--env", self.environment_id])

        if self.attempts != 1:
            cmd.extend(["--attempts", str(self.attempts)])

        if base_branch:
            cmd.extend(["--branch", base_branch])

        if self.options:
            cmd.extend(self.options)

        cmd.append(prompt)

        env = os.environ.copy()
        if self.api_key:
            env["CODEX_API_KEY"] = self.api_key
        if self.base_url:
            env["CODEX_BASE_URL"] = self.base_url

        logger.info(f"🤖 Running: {' '.join(cmd)}")
        result = CommandExecutor.run_command(cmd, env=env if len(env) > len(os.environ) else None)
        output = (result.stdout or result.stderr or "").strip()

        if result.returncode != 0:
            raise RuntimeError(f"Failed to start Codex Cloud task: {output or 'unknown CLI error'}")

        task_id = self._extract_task_id(output)
        task_url = self._extract_task_url(output)

        if not task_id:
            raise RuntimeError(f"Codex Cloud did not return a task ID: {output or 'empty output'}")

        if task_url:
            self.task_urls[task_id] = task_url

        self.active_tasks[task_id] = prompt
        logger.info(f"Started Codex Cloud task: {task_id} (url={task_url or 'N/A'})")
        return task_id

    def list_tasks(self, repo_name: Optional[str] = None) -> List[CloudTask]:
        """List active or recent Codex Cloud tasks.

        Executes:
            codex cloud list --json [--env <ENVIRONMENT_ID>]

        Args:
            repo_name: Optional repository name filter (unused directly by CLI list).

        Returns:
            List of CloudTask instances.
        """
        cmd = ["codex", "cloud", "list", "--json"]
        if self.environment_id:
            cmd.extend(["--env", self.environment_id])

        env = os.environ.copy()
        if self.api_key:
            env["CODEX_API_KEY"] = self.api_key
        if self.base_url:
            env["CODEX_BASE_URL"] = self.base_url

        tasks: List[CloudTask] = []
        try:
            result = CommandExecutor.run_command(cmd, env=env if len(env) > len(os.environ) else None)
            output = (result.stdout or "").strip()
            if result.returncode == 0 and output:
                data = json.loads(output)
                task_list = data if isinstance(data, list) else data.get("tasks", [])
                for item in task_list:
                    if isinstance(item, dict):
                        tid = item.get("task_id") or item.get("id")
                        if tid:
                            raw_state = (item.get("status") or item.get("state") or "").upper()
                            # Normalize state: READY means completed/ready in Codex Cloud list
                            if raw_state in ("READY", "COMPLETED", "FINISHED", "SUCCESS"):
                                state = CloudTaskState.COMPLETED
                            elif raw_state in ("RUNNING", "IN_PROGRESS", "ACTIVE"):
                                state = CloudTaskState.RUNNING
                            elif raw_state in ("FAILED", "ERROR", "CANCELLED"):
                                state = CloudTaskState.FAILED
                            elif raw_state in ("PAUSED", "WAITING_FOR_INPUT", "IDLE", "STOPPED"):
                                state = CloudTaskState.PAUSED
                            elif raw_state in ("QUEUED", "PENDING"):
                                state = CloudTaskState.QUEUED
                            else:
                                state = CloudTaskState.UNKNOWN

                            tasks.append(
                                CloudTask(
                                    task_id=str(tid),
                                    state=state,
                                    raw_state=raw_state,
                                    title=item.get("title"),
                                    url=item.get("url") or self.task_urls.get(str(tid)),
                                    error=item.get("error"),
                                    raw_data=item,
                                )
                            )
                if tasks:
                    return tasks
        except Exception as e:
            logger.debug(f"Failed to list Codex Cloud tasks: {e}")

        # Fallback to locally tracked tasks
        for task_id, prompt in self.active_tasks.items():
            tasks.append(
                CloudTask(
                    task_id=task_id,
                    prompt=prompt,
                    state=CloudTaskState.UNKNOWN,
                    url=self.task_urls.get(task_id),
                )
            )
        return tasks

    def get_task(self, task_id: str) -> Optional[CloudTask]:
        """Retrieve current status of a Codex Cloud task.

        Executes:
            codex cloud status <TASK_ID>

        Args:
            task_id: The cloud task ID.

        Returns:
            CloudTask instance with normalized state.
        """
        cmd = ["codex", "cloud", "status", task_id]
        env = os.environ.copy()
        if self.api_key:
            env["CODEX_API_KEY"] = self.api_key
        if self.base_url:
            env["CODEX_BASE_URL"] = self.base_url

        try:
            result = CommandExecutor.run_command(cmd, env=env if len(env) > len(os.environ) else None)
            output = (result.stdout or result.stderr or "").strip()

            if result.returncode == 0 and output:
                # Try parsing as JSON if JSON output is returned
                try:
                    data = json.loads(output)
                    if isinstance(data, dict):
                        raw_state = (data.get("status") or data.get("state") or "").upper()
                        if raw_state in ("READY", "COMPLETED", "FINISHED", "SUCCESS"):
                            state = CloudTaskState.COMPLETED
                        elif raw_state in ("RUNNING", "IN_PROGRESS", "ACTIVE"):
                            state = CloudTaskState.RUNNING
                        elif raw_state in ("FAILED", "ERROR", "CANCELLED"):
                            state = CloudTaskState.FAILED
                        elif raw_state in ("PAUSED", "WAITING_FOR_INPUT", "IDLE", "STOPPED"):
                            state = CloudTaskState.PAUSED
                        elif raw_state in ("QUEUED", "PENDING"):
                            state = CloudTaskState.QUEUED
                        else:
                            state = CloudTaskState.UNKNOWN

                        return CloudTask(
                            task_id=task_id,
                            state=state,
                            raw_state=raw_state,
                            title=data.get("title"),
                            url=data.get("url") or self.task_urls.get(task_id),
                            error=data.get("error"),
                            raw_data=data,
                        )
                except json.JSONDecodeError:
                    pass

                # Parse plain text output
                raw_upper = output.upper()
                if "READY" in raw_upper or "COMPLETED" in raw_upper or "SUCCESS" in raw_upper:
                    state = CloudTaskState.COMPLETED
                elif "RUNNING" in raw_upper or "IN_PROGRESS" in raw_upper:
                    state = CloudTaskState.RUNNING
                elif "FAILED" in raw_upper or "ERROR" in raw_upper:
                    state = CloudTaskState.FAILED
                elif "PAUSED" in raw_upper or "WAITING" in raw_upper:
                    state = CloudTaskState.PAUSED
                else:
                    state = CloudTaskState.UNKNOWN

                raw_data = None
                if state in {CloudTaskState.COMPLETED, CloudTaskState.FAILED, CloudTaskState.PAUSED}:
                    latest_turn_id = (self.wham_client or CodexWhamClient()).resolve_latest_assistant_turn(task_id)
                    if latest_turn_id:
                        raw_data = {"latest_turn_id": latest_turn_id}

                return CloudTask(
                    task_id=task_id,
                    state=state,
                    raw_state=output,
                    prompt=self.active_tasks.get(task_id),
                    url=self.task_urls.get(task_id),
                    raw_data=raw_data,
                )
        except Exception as e:
            logger.debug(f"Failed to check Codex Cloud task status for {task_id}: {e}")

        if task_id in self.active_tasks:
            return CloudTask(
                task_id=task_id,
                prompt=self.active_tasks[task_id],
                state=CloudTaskState.UNKNOWN,
                url=self.task_urls.get(task_id),
            )

        return None

    def get_diff(self, task_id: str) -> str:
        """Inspect the changes/diff produced by a Codex Cloud task.

        Executes:
            codex cloud diff <TASK_ID>

        Args:
            task_id: The cloud task ID.

        Returns:
            Diff output string.
        """
        cmd = ["codex", "cloud", "diff", task_id]
        env = os.environ.copy()
        if self.api_key:
            env["CODEX_API_KEY"] = self.api_key
        if self.base_url:
            env["CODEX_BASE_URL"] = self.base_url

        try:
            result = CommandExecutor.run_command(cmd, env=env if len(env) > len(os.environ) else None)
            return (result.stdout or "").strip()
        except Exception as e:
            logger.warning(f"Failed to get diff for Codex Cloud task {task_id}: {e}")
            return ""

    def apply_changes(self, task_id: str) -> bool:
        """Apply changes from a Codex Cloud task locally.

        Executes:
            codex cloud apply <TASK_ID>

        Args:
            task_id: The cloud task ID.

        Returns:
            True if applied successfully, False otherwise.
        """
        cmd = ["codex", "cloud", "apply", task_id]
        env = os.environ.copy()
        if self.api_key:
            env["CODEX_API_KEY"] = self.api_key
        if self.base_url:
            env["CODEX_BASE_URL"] = self.base_url

        try:
            result = CommandExecutor.run_command(cmd, env=env if len(env) > len(os.environ) else None)
            return result.returncode == 0
        except Exception as e:
            logger.warning(f"Failed to apply changes for Codex Cloud task {task_id}: {e}")
            return False

    def continue_if_paused(self, task_id: str, prompt: Optional[str] = None) -> bool:
        """Attempt to continue a paused or waiting Codex Cloud task via WHAM follow-up.

        Resolves the latest usable assistant turn ID and sends a continuation prompt
        using the internal WHAM tasks backend API.

        Args:
            task_id: The Codex Cloud task ID.
            prompt: Optional custom continuation prompt. If None, renders the default
                    prompt from prompts.yaml (codex_cloud.continuation).

        Returns:
            True if the continuation request was accepted, False otherwise.
        """
        if not is_valid_codex_cloud_task_id(task_id):
            logger.warning("continue_if_paused called with invalid task_id")
            return False

        # Anti-tight-loop cooldown (60 seconds)
        now = time.time()
        last_time = self.last_continued_at.get(task_id, 0.0)
        if now - last_time < 60.0:
            logger.info(f"Codex Cloud task '{task_id}' was continued {int(now - last_time)}s ago; skipping to avoid tight loops")
            return False

        if not prompt:
            continuation_prompt = render_prompt("codex_cloud.continuation")
        else:
            continuation_prompt = prompt

        if self.send_followup(task_id, continuation_prompt):
            self.last_continued_at[task_id] = now
            logger.info(f"Successfully sent continuation follow-up to Codex Cloud task '{task_id}'")
            return True

        logger.warning(f"Failed to send continuation follow-up to Codex Cloud task '{task_id}'")
        return False

    def get_followup_delivery(self, task_id: str, logical_identity: str) -> FollowUpDeliveryOutcome:
        """Return durable delivery state, reconciling an ambiguous record."""
        state_path = _codex_followup_state_path(self.repo_name)
        key = hashlib.sha256(f"{task_id}\0{logical_identity}".encode("utf-8")).hexdigest()
        with _followup_state_lock:
            try:
                records = _load_pending_followups(state_path)
            except (OSError, ValueError, TypeError) as exc:
                logger.warning(f"Cannot read Codex follow-up delivery state; failing closed: {exc}")
                return FollowUpDeliveryOutcome.INDETERMINATE
            record = records.get(key)
        if record is None:
            return FollowUpDeliveryOutcome.NOT_DELIVERED
        if record.status == FollowUpDeliveryOutcome.DELIVERED.value:
            return FollowUpDeliveryOutcome.DELIVERED

        wham = self.wham_client or CodexWhamClient()
        if wham.reconcile_follow_up(task_id, record.pre_send_turn_id, record.message_fingerprint) is not True:
            return FollowUpDeliveryOutcome.INDETERMINATE
        record.status = FollowUpDeliveryOutcome.DELIVERED.value
        with _followup_state_lock:
            records = _load_pending_followups(state_path)
            records[key] = record
            _save_pending_followups(state_path, records)
        logger.info(f"Reconciled previously ambiguous Codex follow-up '{logical_identity}' as delivered")
        return FollowUpDeliveryOutcome.DELIVERED

    def get_followup_remediation_baseline(self, task_id: str, logical_identities: tuple[str, ...]) -> str:
        """Return the pre-send assistant turn durably captured for a follow-up."""
        state_path = _codex_followup_state_path(self.repo_name)
        keys = [hashlib.sha256(f"{task_id}\0{identity}".encode("utf-8")).hexdigest() for identity in logical_identities]
        with _followup_state_lock:
            try:
                records = _load_pending_followups(state_path)
            except (OSError, ValueError, TypeError):
                return ""
        turns = {records[key].pre_send_turn_id for key in keys if key in records and records[key].pre_send_turn_id}
        return f"latest_turn_id:{turns.pop()}" if len(turns) == 1 else ""

    def send_followup(self, task_id: str, message: str, logical_identities: tuple[str, ...] = ()) -> bool:
        """Send work once, reconciling any prior ambiguous POST before retrying."""
        if not is_valid_codex_cloud_task_id(task_id) or not message:
            logger.warning("Codex Cloud follow-up requires a task ID and message")
            return False

        wham = self.wham_client or CodexWhamClient()
        message_fingerprint = hashlib.sha256(message.encode("utf-8")).hexdigest()
        identities = logical_identities or (message_fingerprint,)
        keys = {hashlib.sha256(f"{task_id}\0{identity}".encode("utf-8")).hexdigest() for identity in identities}
        state_path = _codex_followup_state_path(self.repo_name)
        states = [self.get_followup_delivery(task_id, identity) for identity in identities]
        if states and all(state is FollowUpDeliveryOutcome.DELIVERED for state in states):
            return True
        if any(state is not FollowUpDeliveryOutcome.NOT_DELIVERED for state in states):
            logger.warning("Codex follow-up contains an indeterminate or partially delivered logical identity; duplicate POST deferred")
            return False

        turn_id = wham.resolve_latest_assistant_turn(task_id)
        if not turn_id:
            logger.warning(f"Codex Cloud task '{task_id}' has no usable assistant turn for follow-up")
            return False

        record = PendingCodexFollowUp(task_id=task_id, message_fingerprint=message_fingerprint, pre_send_turn_id=turn_id)
        with _followup_state_lock:
            try:
                pending = _load_pending_followups(state_path)
                for key in keys:
                    pending[key] = record
                _save_pending_followups(state_path, pending)
            except (OSError, ValueError, TypeError) as exc:
                logger.warning(f"Cannot durably reserve Codex follow-up; request was not sent: {exc}")
                return False

        result = wham.send_follow_up(task_id=task_id, turn_id=turn_id, prompt=message)
        with _followup_state_lock:
            pending = _load_pending_followups(state_path)
            if result.delivered:
                confirmed = PendingCodexFollowUp(task_id=task_id, message_fingerprint=message_fingerprint, pre_send_turn_id=turn_id, status=FollowUpDeliveryOutcome.DELIVERED.value)
                for key in keys:
                    pending[key] = confirmed
            elif result.outcome is FollowUpDeliveryOutcome.NOT_DELIVERED:
                for key in keys:
                    pending.pop(key, None)
            _save_pending_followups(state_path, pending)
        if result.delivered:
            self.active_tasks[task_id] = message
            logger.info(f"Assigned follow-up work to Codex Cloud task '{task_id}'")
            return True
        if result.outcome is FollowUpDeliveryOutcome.INDETERMINATE:
            logger.warning(f"Codex follow-up delivery for task '{task_id}' is indeterminate; automatic retry is deferred")
        return False

    def stop_task(self, task_id: str) -> bool:
        """Stop or clean up a Codex Cloud task tracking."""
        if task_id in self.active_tasks:
            del self.active_tasks[task_id]
            return True
        return False

    def _run_llm_cli(self, prompt: str, is_noedit: bool = False) -> str:
        """Execute LLM with prompt via Codex Cloud."""
        task_id = self.start_task(prompt)
        return f"Codex Cloud task started successfully. Task ID: {task_id}"

    def check_mcp_server_configured(self, server_name: str) -> bool:
        """Check if MCP server is configured."""
        return False

    def add_mcp_server_config(self, server_name: str, command: str, args: list[str]) -> bool:
        """Add MCP server configuration."""
        return False
