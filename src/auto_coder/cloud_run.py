"""
CloudRun: durable lifecycle abstraction for non-Jules cloud coding runs.

This module models the lifecycle of a single Issue attempt processed by an
asynchronous cloud provider (e.g. Codex Cloud, Claude Routine, or a future
Google Cloud backend), separately from the low-level provider transport
operations represented by `CloudTaskClientBase` (starting/querying/stopping
provider tasks).

Scope note: this module intentionally does not change Jules behavior. Jules
continues to persist its own state and use its existing lifecycle paths;
it must not be migrated to this abstraction here.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .logger_config import get_logger

logger = get_logger(__name__)


def _parse_stored_int(value: object, field_name: str) -> int:
    """Parse an integer stored in the JSON repository."""
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    raise TypeError(f"{field_name} must be an integer")


def _parse_stored_int_list(value: object, field_name: str) -> List[int]:
    """Parse an integer list stored in the JSON repository."""
    if not isinstance(value, list):
        raise TypeError(f"{field_name} must be a list")
    return [_parse_stored_int(item, field_name) for item in value]


@dataclass
class CloudRun:
    """A durable record of one cloud provider run for a single Issue attempt.

    A `CloudRun` is identified by (repo_name, issue_number, attempt) and is
    associated with exactly one provider task/session id. It may accumulate
    zero, one, or multiple pull requests over its lifetime; recording a new
    pull request preserves every previously associated pull request.
    """

    repo_name: str
    issue_number: int
    attempt: int
    provider: str
    task_id: str = ""
    backend_name: str = ""
    environment_id: str = ""
    base_branch: str = ""
    submission_outcome: str = "accepted"
    task_url: str = ""
    pull_request_numbers: List[int] = field(default_factory=list)

    def add_pull_request(self, pr_number: int) -> None:
        """Associate a pull request with this run, preserving prior associations."""
        if pr_number not in self.pull_request_numbers:
            self.pull_request_numbers.append(pr_number)

    def to_dict(self) -> Dict[str, object]:
        return {
            "repo_name": self.repo_name,
            "issue_number": self.issue_number,
            "attempt": self.attempt,
            "provider": self.provider,
            "task_id": self.task_id,
            "backend_name": self.backend_name,
            "environment_id": self.environment_id,
            "base_branch": self.base_branch,
            "submission_outcome": self.submission_outcome,
            "task_url": self.task_url,
            "pull_request_numbers": list(self.pull_request_numbers),
        }

    @staticmethod
    def from_dict(data: Dict[str, object]) -> "CloudRun":
        return CloudRun(
            repo_name=str(data["repo_name"]),
            issue_number=_parse_stored_int(data["issue_number"], "issue_number"),
            attempt=_parse_stored_int(data["attempt"], "attempt"),
            provider=str(data["provider"]),
            task_id=str(data["task_id"]),
            backend_name=str(data.get("backend_name", "")),
            environment_id=str(data.get("environment_id", "")),
            base_branch=str(data.get("base_branch", "")),
            submission_outcome=str(data.get("submission_outcome", "accepted")),
            task_url=str(data.get("task_url", "")),
            pull_request_numbers=_parse_stored_int_list(data.get("pull_request_numbers", []), "pull_request_numbers"),
        )


def _run_key(issue_number: int, attempt: int) -> str:
    return f"{issue_number}:{attempt}"


class CloudRunRepository:
    """Durable store for `CloudRun` records, keyed by (issue_number, attempt).

    State is persisted as JSON under `~/.auto-coder/<repo>/cloud_runs.json`
    (or a custom path) so it can be recovered after an Auto-Coder process
    restart, independent of any in-memory manager instance. This durability
    is what duplicate-dispatch protection relies on: a fresh
    `CloudRunRepository` instance pointed at the same storage path sees
    every run persisted by a previous process.

    Thread Safety:
    -------------
    A lock guards read-modify-write file operations.
    """

    def __init__(self, repo_name: str, storage_path: Optional[Path] = None):
        self.repo_name = repo_name
        self._lock = threading.Lock()

        if storage_path:
            self.storage_path = storage_path
        else:
            self.storage_path = Path.home() / ".auto-coder" / repo_name / "cloud_runs.json"
        self.lock_path = self.storage_path.with_suffix(self.storage_path.suffix + ".lock")

    def _ensure_dir(self) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)

    def _read_all(self) -> Dict[str, Dict[str, object]]:
        self._ensure_dir()

        if not self.storage_path.exists():
            return {}

        with open(self.storage_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError(f"Cloud run state at {self.storage_path} is not an object")
        return raw

    def _write_all(self, data: Dict[str, Dict[str, object]]) -> bool:
        self._ensure_dir()

        temporary = self.storage_path.with_suffix(f"{self.storage_path.suffix}.{os.getpid()}.{threading.get_ident()}.tmp")
        fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, self.storage_path)
            directory_fd = os.open(str(self.storage_path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return True
        finally:
            if temporary.exists():
                temporary.unlink()

    def _file_lock(self):
        """Return an opened, exclusively locked cross-process lock file."""
        self._ensure_dir()
        lock_file = open(self.lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except Exception:
            lock_file.close()
            raise
        return lock_file

    def acquire_submission_claim(self, run: CloudRun) -> Tuple[CloudRun, bool]:
        """Atomically publish a suppressing claim, or return the existing owner."""
        with self._lock:
            lock_file = self._file_lock()
            try:
                data = self._read_all()
                key = _run_key(run.issue_number, run.attempt)
                raw = data.get(key)
                if raw is not None:
                    existing = CloudRun.from_dict(raw)
                    if existing.repo_name != self.repo_name or existing.provider != run.provider:
                        raise ValueError(f"Contradictory cloud ownership at {key}")
                    return existing, False
                data[key] = run.to_dict()
                self._write_all(data)
                return run, True
            finally:
                lock_file.close()

    def update_claim(self, run: CloudRun) -> None:
        """Update a claim without permitting its provider task identity to change."""
        with self._lock:
            lock_file = self._file_lock()
            try:
                data = self._read_all()
                key = _run_key(run.issue_number, run.attempt)
                current_raw = data.get(key)
                if current_raw is None:
                    raise ValueError("Submission claim disappeared")
                current = CloudRun.from_dict(current_raw)
                if current.provider != run.provider or (current.task_id and current.task_id != run.task_id):
                    raise ValueError("Contradictory cloud task binding")
                data[key] = run.to_dict()
                self._write_all(data)
            finally:
                lock_file.close()

    def release_definitely_not_submitted(self, issue_number: int, attempt: int) -> None:
        """Release only a claim explicitly proven not to have crossed the boundary."""
        with self._lock:
            lock_file = self._file_lock()
            try:
                data = self._read_all()
                key = _run_key(issue_number, attempt)
                current = CloudRun.from_dict(data[key])
                if current.submission_outcome != "definitely-not-submitted":
                    raise ValueError("Only definitely-not-submitted claims may be released")
                del data[key]
                self._write_all(data)
            finally:
                lock_file.close()

    def list_all(self) -> List[CloudRun]:
        """Enumerate accepted tasks and unresolved claims for this repository."""
        with self._lock:
            return [CloudRun.from_dict(raw) for raw in self._read_all().values()]

    def save(self, run: CloudRun) -> bool:
        """Persist (create or update) a cloud run durably."""
        with self._lock:
            lock_file = self._file_lock()
            try:
                data = self._read_all()
                key = _run_key(run.issue_number, run.attempt)
                old_raw = data.get(key)
                if old_raw is not None:
                    old = CloudRun.from_dict(old_raw)
                    if old.provider != run.provider or (old.task_id and run.task_id and old.task_id != run.task_id):
                        raise ValueError("Contradictory cloud task binding")
                data[key] = run.to_dict()
                success = self._write_all(data)
            finally:
                lock_file.close()
            if success:
                logger.info(f"Persisted cloud run for issue #{run.issue_number} attempt {run.attempt} " f"(provider={run.provider}, task_id={run.task_id})")
            return success

    def get(self, issue_number: int, attempt: int) -> Optional[CloudRun]:
        """Load a cloud run for the given issue/attempt, if any."""
        with self._lock:
            data = self._read_all()
            raw = data.get(_run_key(issue_number, attempt))
            if raw is None:
                return None
            return CloudRun.from_dict(raw)

    def get_by_task_id(self, task_id: str) -> Optional[CloudRun]:
        """Reverse lookup a cloud run by its provider task/session id.

        Intended for duplicate-dispatch protection: callers can check
        whether a task id is already durably tracked before starting a new
        provider task for the same Issue attempt.
        """
        with self._lock:
            data = self._read_all()
            for raw in data.values():
                if raw.get("task_id") == task_id:
                    return CloudRun.from_dict(raw)
            return None

    def list_for_issue(self, issue_number: int) -> List[CloudRun]:
        """List all persisted runs (across attempts) for an issue, ordered by attempt."""
        with self._lock:
            data = self._read_all()
            runs = [CloudRun.from_dict(raw) for raw in data.values() if _parse_stored_int(raw.get("issue_number", -1), "issue_number") == issue_number]
            runs.sort(key=lambda r: r.attempt)
            return runs

    def add_pull_request(self, issue_number: int, attempt: int, pr_number: int) -> Optional[CloudRun]:
        """Associate a pull request with an existing run, preserving prior PRs.

        Returns the updated `CloudRun`, or None if no run exists for the
        given issue/attempt.
        """
        with self._lock:
            data = self._read_all()
            key = _run_key(issue_number, attempt)
            raw = data.get(key)
            if raw is None:
                return None
            run = CloudRun.from_dict(raw)
            run.add_pull_request(pr_number)
            data[key] = run.to_dict()
            self._write_all(data)
            return run


@dataclass
class CloudRunEvent:
    """A lifecycle event proposing that a `CloudRun` transition to a new attempt.

    Generic lifecycle code constructs this event and asks a `CloudRunPolicy`
    whether the transition is allowed, without needing to know which
    provider produced the run.
    """

    run: CloudRun
    reason: str
    proposed_attempt: int


class CloudRunPolicy(ABC):
    """Provider-supplied policy for cloud-run lifecycle decisions.

    Implementations encode provider-specific rules (for example, "this
    provider may auto-retry on an empty pull request" vs. "this provider
    never auto-retries") so that generic lifecycle code can ask the policy
    for a decision without branching on a concrete provider name such as
    `codex-cloud`, `claude-routine`, or a future Google backend.
    """

    @abstractmethod
    def allow_new_attempt(self, event: CloudRunEvent) -> bool:
        """Return True if `event` may transition the run to a new attempt."""
        raise NotImplementedError


def evaluate_new_attempt(event: CloudRunEvent, policy: CloudRunPolicy) -> bool:
    """Ask `policy` whether `event` may start a new attempt for its run.

    This is the single generic entry point lifecycle code should use to make
    this decision; it never inspects `event.run.provider` itself, so adding
    a new provider only requires supplying a new `CloudRunPolicy`.
    """
    return policy.allow_new_attempt(event)
