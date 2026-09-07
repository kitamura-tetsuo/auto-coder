"""Read-only, fail-closed observations for durably tracked Codex Cloud runs."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .cloud_run import CloudRun, CloudRunRepository
from .cloud_task_client_base import CloudTaskState
from .codex_cloud_task import is_valid_codex_cloud_task_id
from .codex_wham_client import CodexWhamClient, WhamTask
from .util.gh_cache import GitHubClient


class PullRequestPresence(str, Enum):
    PR_PRESENT = "pr_present"
    PREVIOUSLY_PUBLISHED = "previously_published"
    NO_MATCHING_PR = "no_matching_pr"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ObservationBinding:
    repository: str
    issue_number: int
    attempt: int
    provider: str
    task_id: str
    backend_name: str
    environment_id: str
    base_branch: str

    @classmethod
    def from_run(cls, run: CloudRun) -> "ObservationBinding":
        return cls(run.repo_name, run.issue_number, run.attempt, run.provider, run.task_id, run.backend_name, run.environment_id, run.base_branch)


@dataclass(frozen=True)
class CodexExecutionEvidence:
    state: CloudTaskState = CloudTaskState.UNKNOWN
    assistant_turn_id: str = ""
    user_turn_id: str = ""
    recovery_eligible: bool = False
    raw_status: str = ""


@dataclass(frozen=True)
class PullRequestEvidence:
    presence: PullRequestPresence = PullRequestPresence.UNKNOWN
    number: Optional[int] = None
    url: str = ""


@dataclass(frozen=True)
class CodexRunObservation:
    binding: ObservationBinding
    generation: int
    activity_generation: int
    execution: CodexExecutionEvidence = field(default_factory=CodexExecutionEvidence)
    pull_request: PullRequestEvidence = field(default_factory=PullRequestEvidence)
    issue_state: str = "unknown"
    errors: tuple[str, ...] = ()


_STATUS = {
    "completed": CloudTaskState.COMPLETED,
    "in_progress": CloudTaskState.RUNNING,
    "pending": CloudTaskState.QUEUED,
    "queued": CloudTaskState.QUEUED,
    "paused": CloudTaskState.PAUSED,
    "waiting_for_input": CloudTaskState.PAUSED,
    "failed": CloudTaskState.FAILED,
    "error": CloudTaskState.FAILED,
    "cancelled": CloudTaskState.CANCELLED,
    "canceled": CloudTaskState.CANCELLED,
}


def execution_evidence(task: Optional[WhamTask], binding: ObservationBinding) -> CodexExecutionEvidence:
    """Normalize only authoritative current-turn fields from task-details."""
    if task is None or task.id != binding.task_id:
        return CodexExecutionEvidence()
    if task.environment_id and task.environment_id != binding.environment_id:
        return CodexExecutionEvidence()
    assistant = task.current_assistant_turn
    if assistant is None or not assistant.id or not assistant.id.startswith(f"{binding.task_id}~"):
        return CodexExecutionEvidence(user_turn_id=task.current_user_turn.id if task.current_user_turn else "")
    current = assistant.status.strip().lower()
    latest = task.latest_turn_status.strip().lower()
    state = _STATUS.get(current, CloudTaskState.UNKNOWN)
    # Latest metadata may corroborate or contradict, but a summary never creates
    # assistant identity. A queued current user means a newer turn is pending.
    user_status = task.current_user_turn.status.strip().lower() if task.current_user_turn else ""
    contradicted = bool(latest and latest != current) or user_status in {"pending", "queued", "in_progress"}
    eligible = state is CloudTaskState.COMPLETED and latest == current and not contradicted
    if contradicted:
        state = CloudTaskState.UNKNOWN
    return CodexExecutionEvidence(state, assistant.id, task.current_user_turn.id if task.current_user_turn else "", eligible, current)


class CodexObservationService:
    """Combines provider and GitHub reads without scheduling or mutations."""

    def __init__(self, github: GitHubClient, runs: CloudRunRepository, wham: Optional[CodexWhamClient] = None) -> None:
        self.github = github
        self.runs = runs
        self.wham = wham or CodexWhamClient()
        self._lock = threading.Lock()
        self._issued = 0
        self._latest: dict[ObservationBinding, CodexRunObservation] = {}
        self._latest_issued: dict[ObservationBinding, int] = {}
        self._activity_key: dict[ObservationBinding, tuple[str, CloudTaskState]] = {}
        self._activity_generation: dict[ObservationBinding, int] = {}

    def observe(self, run: CloudRun) -> CodexRunObservation:
        binding = ObservationBinding.from_run(run)
        with self._lock:
            self._issued += 1
            generation = self._issued
            self._latest_issued[binding] = generation
        errors: list[str] = []
        task: Optional[WhamTask] = None
        if binding.provider != "codex-cloud" or not is_valid_codex_cloud_task_id(binding.task_id):
            errors.append("unsupported or invalid tracked Codex identity")
        else:
            task = self.wham.get_task(binding.task_id)
            if task is None:
                errors.append("Codex task details unavailable")
        execution = execution_evidence(task, binding)
        if task is not None and execution.state is CloudTaskState.UNKNOWN:
            errors.append("Codex current execution evidence is missing or contradictory")
        issue_state = "unknown"
        pr_evidence = PullRequestEvidence()
        try:
            issue = self.github.get_issue_dispatch_snapshot_strict(binding.repository, binding.issue_number)
            issue_state = str(issue.get("state", "unknown"))
            pr_evidence = self._observe_pr(binding)
        except Exception as exc:
            errors.append(f"GitHub observation unavailable: {type(exc).__name__}")
        with self._lock:
            if generation < self._latest_issued[binding] and binding in self._latest:
                return self._latest[binding]
            if execution.state is not CloudTaskState.UNKNOWN:
                key = (execution.assistant_turn_id or execution.user_turn_id, execution.state)
                if key != self._activity_key.get(binding):
                    self._activity_generation[binding] = self._activity_generation.get(binding, 0) + 1
                    self._activity_key[binding] = key
            activity_generation = self._activity_generation.get(binding, 0)
            result = CodexRunObservation(binding, generation, activity_generation, execution, pr_evidence, issue_state, tuple(errors))
            self._latest[binding] = result
            return result

    def _observe_pr(self, binding: ObservationBinding) -> PullRequestEvidence:
        timeline = self.github.get_issue_timeline_strict(binding.repository, binding.issue_number)
        open_prs = self.github.get_open_pull_requests_strict(binding.repository)
        current = self.runs.get(binding.issue_number, binding.attempt)
        if current is None or ObservationBinding.from_run(current) != binding:
            raise RuntimeError("durable run binding changed during observation")
        all_runs = self.runs.list_for_issue(binding.issue_number)
        known = {number for candidate in all_runs for number in candidate.pull_request_numbers}
        candidates = set(known)
        native: set[int] = set()
        for event in timeline:
            source = event.get("source")
            issue = source.get("issue") if isinstance(source, dict) else None
            if isinstance(issue, dict) and isinstance(issue.get("pull_request"), dict) and isinstance(issue.get("number"), int):
                # GitHub timeline URLs establish repository scope, not coincidental numbers.
                html_url = issue.get("html_url", "")
                if isinstance(html_url, str) and html_url.startswith(f"https://github.com/{binding.repository}/pull/"):
                    candidates.add(issue["number"])
                    if event.get("event") == "connected":
                        native.add(issue["number"])
        candidates.update(item["number"] for item in open_prs if isinstance(item.get("number"), int))
        ambiguous = False
        for number in sorted(candidates):
            pr = self.github.get_pull_request_metadata_strict(binding.repository, number)
            match, conflict = self._matches(pr, binding, all_runs, number in native)
            if conflict:
                continue
            state = str(pr.get("state", "")).lower()
            exact_task = f"https://chatgpt.com/codex/tasks/{binding.task_id}" in str(pr.get("body") or "")
            exact_run = number in current.pull_request_numbers
            if match and state != "open" and not (exact_task or exact_run):
                ambiguous = True
                continue
            if match:
                presence = PullRequestPresence.PR_PRESENT if state == "open" else PullRequestPresence.PREVIOUSLY_PUBLISHED
                return PullRequestEvidence(presence, number, str(pr.get("html_url", "")))
            if state_relevant(pr, binding.issue_number, binding.repository) and number not in known:
                ambiguous = True
        if ambiguous:
            return PullRequestEvidence(PullRequestPresence.AMBIGUOUS)
        return PullRequestEvidence(PullRequestPresence.NO_MATCHING_PR)

    @staticmethod
    def _matches(pr: dict[str, object], binding: ObservationBinding, runs: list[CloudRun], native_closing: bool = False) -> tuple[bool, bool]:
        number = pr.get("number")
        owners = [run for run in runs if isinstance(number, int) and number in run.pull_request_numbers]
        if owners:
            current = any(ObservationBinding.from_run(run) == binding for run in owners)
            return (True, False) if current else (False, True)
        raw_body = pr.get("body")
        body: str = raw_body if isinstance(raw_body, str) else ""
        other_task = re.search(r"https://chatgpt\.com/codex/tasks/(task_[A-Za-z0-9_-]+)", body)
        if other_task:
            return other_task.group(1) == binding.task_id, other_task.group(1) != binding.task_id
        return native_closing or state_relevant(pr, binding.issue_number, binding.repository), False


def state_relevant(pr: dict[str, object], issue_number: int, repository: str) -> bool:
    raw_body = pr.get("body")
    body: str = raw_body if isinstance(raw_body, str) else ""
    verb = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
    local = rf"(?i)\b{verb}\s+#{issue_number}(?!\d)"
    full = rf"(?i)\b{verb}\s+https://github\.com/{re.escape(repository)}/issues/{issue_number}(?!\d)"
    return bool(re.search(local, body) or re.search(full, body))
