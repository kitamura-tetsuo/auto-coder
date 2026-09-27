"""Durable execution of explicit-local pull-request review corrections."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .git_branch import git_commit_with_retry
from .git_commit import git_push
from .llm_backend_config import TASK_ONLY_BACKEND_TYPES, active_repo_context, get_llm_config
from .logger_config import get_logger
from .utils import bind_command_execution_cwd, reset_command_execution_cwd

logger = get_logger(__name__)


@dataclass(frozen=True)
class LocalReviewRepairRequest:
    repository: str
    pr_number: int
    head_repository: str
    head_ref: str
    head_sha: str
    feedback_identities: tuple[str, ...]
    prompt: str

    @property
    def attempt_id(self) -> str:
        material = "\n".join((self.repository, str(self.pr_number), self.head_sha, *sorted(self.feedback_identities)))
        return hashlib.sha256(material.encode()).hexdigest()


@dataclass(frozen=True)
class LocalReviewRepairOutcome:
    phase: str
    reason: str
    executed: bool = False
    published: bool = False


@dataclass(frozen=True)
class LocalReviewRepairRecord:
    phase: str
    result_sha: str = ""
    reason: str = ""


def local_review_repair_db_path(repository: str) -> Path:
    return Path.home() / ".auto-coder" / repository / "local_review_repairs.sqlite3"


class LocalReviewRepairStore:
    """SQLite claim store that serializes all corrective work for one PR."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS local_review_repair_attempts (
                repository TEXT NOT NULL, pr_number INTEGER NOT NULL,
                attempt_id TEXT NOT NULL, head_sha TEXT NOT NULL,
                feedback_json TEXT NOT NULL, phase TEXT NOT NULL,
                result_sha TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL, incarnation INTEGER NOT NULL,
                PRIMARY KEY(repository, pr_number, attempt_id))"""
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def admit(self, request: LocalReviewRepairRequest) -> tuple[bool, str]:
        """Atomically claim the PR, or describe the retained recovery phase."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            exact = connection.execute(
                "SELECT phase FROM local_review_repair_attempts " "WHERE repository=? AND pr_number=? AND attempt_id=?",
                (request.repository, request.pr_number, request.attempt_id),
            ).fetchone()
            if exact is not None:
                phase = str(exact[0])
                if phase == "not_started":
                    connection.execute(
                        "UPDATE local_review_repair_attempts SET phase='executing', reason='', updated_at=?, incarnation=? " "WHERE repository=? AND pr_number=? AND attempt_id=?",
                        (time.time(), time.time_ns(), request.repository, request.pr_number, request.attempt_id),
                    )
                    connection.commit()
                    return True, "executing"
                connection.commit()
                return False, phase
            active = connection.execute(
                "SELECT phase FROM local_review_repair_attempts " "WHERE repository=? AND pr_number=? " "AND phase IN ('executing', 'indeterminate', 'publication_pending') " "ORDER BY updated_at DESC LIMIT 1",
                (request.repository, request.pr_number),
            ).fetchone()
            if active is not None:
                connection.commit()
                return False, str(active[0])
            incarnation = int(time.time_ns())
            connection.execute(
                "INSERT INTO local_review_repair_attempts VALUES (?, ?, ?, ?, ?, 'executing', '', '', ?, ?)",
                (
                    request.repository,
                    request.pr_number,
                    request.attempt_id,
                    request.head_sha,
                    json.dumps(request.feedback_identities),
                    time.time(),
                    incarnation,
                ),
            )
            connection.commit()
            return True, "executing"

    def transition(self, request: LocalReviewRepairRequest, phase: str, *, result_sha: str = "", reason: str = "") -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE local_review_repair_attempts SET phase=?, result_sha=?, reason=?, updated_at=? " "WHERE repository=? AND pr_number=? AND attempt_id=?",
                (phase, result_sha, reason, time.time(), request.repository, request.pr_number, request.attempt_id),
            )
            return cursor.rowcount == 1

    def get(self, request: LocalReviewRepairRequest) -> Optional[LocalReviewRepairRecord]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT phase, result_sha, reason FROM local_review_repair_attempts " "WHERE repository=? AND pr_number=? AND attempt_id=?",
                (request.repository, request.pr_number, request.attempt_id),
            ).fetchone()
        if row is None:
            return None
        return LocalReviewRepairRecord(phase=str(row[0]), result_sha=str(row[1]), reason=str(row[2]))


def select_local_review_repair_candidates(repository: str) -> list[str]:
    """Return quota-ranked synchronous candidates from the ordinary policy."""
    config = get_llm_config(repo_name=repository)
    groups = config.get_ordinary_priority_groups()
    local_groups = [[name for name in group if config.resolve_backend_type(name) not in TASK_ONLY_BACKEND_TYPES] for group in groups]
    local_groups = [group for group in local_groups if group]
    if not local_groups:
        return []
    from .quota_selector import rank_high_score_backends_by_quota

    return rank_high_score_backends_by_quota(local_groups, config)


def _default_executor(request: LocalReviewRepairRequest, worktree: str) -> str:
    from .cli_helpers import build_backend_manager

    with active_repo_context(request.repository):
        candidates = select_local_review_repair_candidates(request.repository)
        if not candidates:
            raise RuntimeError("no configured synchronous local backend is available")
        config = get_llm_config(repo_name=request.repository)
        manager = build_backend_manager(
            selected_backends=candidates,
            primary_backend=candidates[0],
            models={name: config.get_model_for_backend(name) or name for name in candidates},
            automatic_session_resume=False,
        )
        token = bind_command_execution_cwd(worktree)
        try:
            return manager.run_prompt(request.prompt)
        finally:
            reset_command_execution_cwd(token)


def _remote_head(root: Path, head_ref: str) -> tuple[bool, str]:
    result = subprocess.run(
        ["git", "ls-remote", "origin", f"refs/heads/{head_ref}"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False, result.stderr.strip()
    fields = result.stdout.split()
    return True, fields[0] if fields else ""


def _resume_publication(
    request: LocalReviewRepairRequest,
    store: LocalReviewRepairStore,
    record: LocalReviewRepairRecord,
    root: Path,
) -> LocalReviewRepairOutcome:
    """Reconcile or publish retained output without rerunning implementation."""
    if not record.result_sha:
        return LocalReviewRepairOutcome("indeterminate", "retained correction has no recoverable commit", executed=True)
    readable, remote_head = _remote_head(root, request.head_ref)
    if not readable:
        return LocalReviewRepairOutcome("publication_pending", f"remote branch could not be reconciled: {remote_head}", executed=True)
    if remote_head == record.result_sha:
        store.transition(request, "awaiting_validation", result_sha=record.result_sha)
        return LocalReviewRepairOutcome(
            "awaiting_validation",
            f"reconciled published {record.result_sha} on {request.head_ref}; independent validation is required",
            executed=True,
            published=True,
        )
    if remote_head != request.head_sha:
        return LocalReviewRepairOutcome(
            "publication_pending",
            f"publication conflict: {request.head_ref} advanced to {remote_head or 'an unavailable ref'}",
            executed=True,
        )
    push = git_push(
        cwd=str(root),
        remote="origin",
        branch=f"{record.result_sha}:{request.head_ref}",
        expected_remote_sha=request.head_sha,
    )
    if not push.success:
        store.transition(request, "publication_pending", result_sha=record.result_sha, reason=push.stderr)
        return LocalReviewRepairOutcome("publication_pending", f"publication incomplete: {push.stderr}", executed=True)
    store.transition(request, "awaiting_validation", result_sha=record.result_sha)
    return LocalReviewRepairOutcome(
        "awaiting_validation",
        f"published retained {record.result_sha} to {request.head_ref}; independent validation is required",
        executed=True,
        published=True,
    )


def execute_local_review_repair(
    request: LocalReviewRepairRequest,
    *,
    store: Optional[LocalReviewRepairStore] = None,
    executor: Optional[Callable[[LocalReviewRepairRequest, str], str]] = None,
) -> LocalReviewRepairOutcome:
    """Run one fenced correction in a detached exact-head checkout and publish it."""
    if request.head_repository != request.repository:
        return LocalReviewRepairOutcome("not_admitted", "foreign-head pull requests are not eligible")
    store = store or LocalReviewRepairStore(local_review_repair_db_path(request.repository))
    admitted, retained_phase = store.admit(request)
    if not admitted:
        if retained_phase == "publication_pending":
            record = store.get(request)
            if record is not None:
                return _resume_publication(request, store, record, Path.cwd())
        return LocalReviewRepairOutcome(retained_phase, f"retained local correction phase: {retained_phase}")

    root = Path.cwd()
    worktree = tempfile.mkdtemp(prefix=f"auto_coder_review_{request.pr_number}_")
    try:
        added = subprocess.run(
            ["git", "worktree", "add", "--detach", worktree, request.head_sha],
            cwd=root,
            capture_output=True,
            text=True,
        )
        if added.returncode != 0:
            store.transition(request, "not_started", reason=added.stderr.strip())
            return LocalReviewRepairOutcome("not_started", f"protected checkout failed: {added.stderr.strip()}")
        try:
            (executor or _default_executor)(request, worktree)
        except Exception as exc:
            store.transition(request, "indeterminate", reason=str(exc))
            return LocalReviewRepairOutcome("indeterminate", f"local backend outcome is indeterminate: {exc}", executed=True)

        status = subprocess.run(["git", "status", "--porcelain"], cwd=worktree, capture_output=True, text=True)
        if status.returncode != 0:
            store.transition(request, "indeterminate", reason=status.stderr.strip())
            return LocalReviewRepairOutcome("indeterminate", "could not inspect corrective output", executed=True)
        if not status.stdout.strip():
            store.transition(request, "completed_no_change", result_sha=request.head_sha)
            return LocalReviewRepairOutcome("completed_no_change", "local correction completed with no change; independent validation is required", executed=True)

        commit = git_commit_with_retry(f"Fix unresolved review findings for PR #{request.pr_number}", cwd=worktree)
        if not commit.success:
            store.transition(request, "indeterminate", reason=commit.stderr)
            return LocalReviewRepairOutcome("indeterminate", f"corrective commit failed after execution: {commit.stderr}", executed=True)
        sha_result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=worktree, capture_output=True, text=True)
        result_sha = sha_result.stdout.strip()
        store.transition(request, "publication_pending", result_sha=result_sha)
        push = git_push(cwd=worktree, remote="origin", branch=f"HEAD:{request.head_ref}", expected_remote_sha=request.head_sha)
        if not push.success:
            store.transition(request, "publication_pending", result_sha=result_sha, reason=push.stderr)
            return LocalReviewRepairOutcome("publication_pending", f"publication incomplete: {push.stderr}", executed=True)
        store.transition(request, "awaiting_validation", result_sha=result_sha)
        return LocalReviewRepairOutcome("awaiting_validation", f"published {result_sha} to {request.head_ref}; independent validation is required", executed=True, published=True)
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", worktree], cwd=root, capture_output=True)
        try:
            os.rmdir(worktree)
        except OSError:
            pass
