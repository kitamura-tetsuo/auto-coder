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

from .durable_repair_allowance import (
    CompletionAvailability,
    CorrectiveGenerationBundle,
    DeliveryOutcome,
    GenerationLifecycleState,
    RepairAllowanceLedger,
    ValidationObservation,
)
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
    attempt_id: str = ""
    head_sha: str = ""
    head_ref: str = ""
    phase: str = ""
    result_sha: str = ""
    reason: str = ""
    incarnation: int = 0
    workspace_path: str = ""


@dataclass(frozen=True)
class LocalReviewRepairClaim:
    admitted: bool
    phase: str
    attempt_id: str
    incarnation: int = 0


class LocalBackendUnavailableError(RuntimeError):
    """Raised before invocation when no configured local candidate can run."""


class LocalRepairValidationRequired(RuntimeError):
    """A completed generation needs independent validation before another repair."""


def settle_local_review_repair_validation(
    repository: str,
    pr_number: int,
    head_sha: str,
    observations: tuple[ValidationObservation, ...],
    *,
    ledger: Optional[RepairAllowanceLedger] = None,
    store: Optional[LocalReviewRepairStore] = None,
    expected_generation_id: Optional[str] = None,
) -> None:
    """Settle only independently adjudicated feedback after the published result."""
    ledger = ledger or RepairAllowanceLedger()
    snapshot = ledger.get_snapshot("https://api.github.com", repository, pr_number)
    generation = snapshot.get_outstanding_generation()
    if generation is None or generation.owning_identity != "local-review-repair" or generation.lifecycle_state != GenerationLifecycleState.PENDING_REVALIDATION:
        return
    if expected_generation_id is not None and generation.generation_id != expected_generation_id:
        raise RuntimeError("pending local repair generation changed before verification settlement")
    store = store or LocalReviewRepairStore(local_review_repair_db_path(repository))
    request = LocalReviewRepairRequest(repository, pr_number, repository, "", head_sha, (), "")
    record = store.get(request, generation.bundle_reference)
    if record is None or not record.result_sha:
        return
    ancestry = subprocess.run(["git", "merge-base", "--is-ancestor", record.result_sha, head_sha], capture_output=True)
    if ancestry.returncode != 0:
        return
    covered = tuple(observation for observation in observations if observation.blocker_id in generation.covered_blocker_ids)
    if covered:
        ledger.record_validation_results(
            "https://api.github.com",
            repository,
            pr_number,
            f"local-validation-{generation.generation_id}-{time.time_ns()}",
            snapshot.epoch,
            generation.generation_id,
            covered,
        )


@dataclass
class LocalRepairAllowanceAuthority:
    ledger: RepairAllowanceLedger
    request: LocalReviewRepairRequest
    generation_id: str
    epoch: int

    def mark_invocation(self) -> None:
        snapshot = self.ledger.record_delivery_outcome(
            "https://api.github.com",
            self.request.repository,
            self.request.pr_number,
            f"local-delivery-{self.request.attempt_id}",
            self.epoch,
            self.generation_id,
            DeliveryOutcome.CONFIRMED,
            self.request.attempt_id,
            evidence="local mutating invocation entered",
        )
        self.epoch = snapshot.epoch

    def mark_completion(self, *, code_changed: bool, evidence: str) -> None:
        snapshot = self.ledger.record_completion_observation(
            "https://api.github.com",
            self.request.repository,
            self.request.pr_number,
            f"local-completion-{self.request.attempt_id}",
            self.epoch,
            self.generation_id,
            CompletionAvailability.KNOWN,
            completion_seq=time.time_ns(),
            code_changed=code_changed,
            evidence=evidence,
        )
        self.epoch = snapshot.epoch


def admit_local_repair_allowance(
    request: LocalReviewRepairRequest,
    ledger: Optional[RepairAllowanceLedger] = None,
) -> tuple[Optional[LocalRepairAllowanceAuthority], str]:
    """Acquire or recover the production corrective-generation authority."""
    ledger = ledger or RepairAllowanceLedger()
    snapshot = ledger.initialize_namespace("https://api.github.com", request.repository, request.pr_number)
    outstanding = snapshot.get_outstanding_generation()
    if outstanding is not None:
        if outstanding.owning_identity == "local-review-repair":
            if outstanding.lifecycle_state == GenerationLifecycleState.PENDING_REVALIDATION:
                raise LocalRepairValidationRequired(f"local correction {outstanding.bundle_reference} awaits independent validation")
            if outstanding.bundle_reference != request.attempt_id:
                return None, f"another local corrective attempt is outstanding: {outstanding.bundle_reference}"
            return LocalRepairAllowanceAuthority(ledger, request, outstanding.generation_id, snapshot.epoch), ""
        return None, f"another corrective generation is outstanding: {outstanding.generation_id}"
    bundle = CorrectiveGenerationBundle(
        bundle_reference=request.attempt_id,
        covered_blocker_ids=request.feedback_identities,
        owning_identity="local-review-repair",
        observed_baseline=request.head_sha,
    )
    admission = ledger.admit_generation(
        "https://api.github.com",
        request.repository,
        request.pr_number,
        f"local-admit-{request.attempt_id}",
        snapshot.epoch,
        bundle,
        open_blocker_ids=request.feedback_identities,
    )
    if not admission.admitted or not admission.generation_id or admission.snapshot is None:
        return None, admission.denial_reason or "repair allowance admission was refused"
    return LocalRepairAllowanceAuthority(ledger, request, admission.generation_id, admission.snapshot.epoch), ""


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
                attempt_id TEXT NOT NULL, head_sha TEXT NOT NULL, head_ref TEXT NOT NULL,
                feedback_json TEXT NOT NULL, phase TEXT NOT NULL,
                result_sha TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL, incarnation INTEGER NOT NULL,
                workspace_path TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(repository, pr_number, attempt_id))"""
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def admit(self, request: LocalReviewRepairRequest) -> LocalReviewRepairClaim:
        """Atomically claim the PR, or describe the retained recovery phase."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            exact = connection.execute(
                "SELECT phase, incarnation FROM local_review_repair_attempts " "WHERE repository=? AND pr_number=? AND attempt_id=?",
                (request.repository, request.pr_number, request.attempt_id),
            ).fetchone()
            if exact is not None:
                phase, previous_incarnation = str(exact[0]), int(exact[1])
                if phase == "not_started":
                    active = connection.execute(
                        "SELECT attempt_id, phase FROM local_review_repair_attempts " "WHERE repository=? AND pr_number=? AND attempt_id<>? " "AND phase IN ('executing', 'indeterminate', 'publication_pending') " "ORDER BY updated_at DESC LIMIT 1",
                        (request.repository, request.pr_number, request.attempt_id),
                    ).fetchone()
                    if active is not None:
                        connection.commit()
                        return LocalReviewRepairClaim(False, str(active[1]), str(active[0]))
                    incarnation = time.time_ns()
                    connection.execute(
                        "UPDATE local_review_repair_attempts SET phase='executing', reason='', updated_at=?, incarnation=? " "WHERE repository=? AND pr_number=? AND attempt_id=?",
                        (time.time(), incarnation, request.repository, request.pr_number, request.attempt_id),
                    )
                    connection.commit()
                    return LocalReviewRepairClaim(True, "executing", request.attempt_id, incarnation)
                connection.commit()
                return LocalReviewRepairClaim(False, phase, request.attempt_id, previous_incarnation)
            active = connection.execute(
                "SELECT attempt_id, phase, incarnation FROM local_review_repair_attempts " "WHERE repository=? AND pr_number=? " "AND phase IN ('executing', 'indeterminate', 'publication_pending') " "ORDER BY updated_at DESC LIMIT 1",
                (request.repository, request.pr_number),
            ).fetchone()
            if active is not None:
                connection.commit()
                return LocalReviewRepairClaim(False, str(active[1]), str(active[0]), int(active[2]))
            incarnation = int(time.time_ns())
            connection.execute(
                "INSERT INTO local_review_repair_attempts VALUES (?, ?, ?, ?, ?, ?, 'executing', '', '', ?, ?, '')",
                (
                    request.repository,
                    request.pr_number,
                    request.attempt_id,
                    request.head_sha,
                    request.head_ref,
                    json.dumps(request.feedback_identities),
                    time.time(),
                    incarnation,
                ),
            )
            connection.commit()
            return LocalReviewRepairClaim(True, "executing", request.attempt_id, incarnation)

    def transition(
        self,
        request: LocalReviewRepairRequest,
        claim: LocalReviewRepairClaim,
        phase: str,
        *,
        result_sha: str = "",
        reason: str = "",
        workspace_path: str = "",
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE local_review_repair_attempts SET phase=?, result_sha=?, reason=?, workspace_path=?, updated_at=? " "WHERE repository=? AND pr_number=? AND attempt_id=? AND incarnation=?",
                (phase, result_sha, reason, workspace_path, time.time(), request.repository, request.pr_number, claim.attempt_id, claim.incarnation),
            )
            return cursor.rowcount == 1

    def get(self, request: LocalReviewRepairRequest, attempt_id: Optional[str] = None) -> Optional[LocalReviewRepairRecord]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT attempt_id, head_sha, head_ref, phase, result_sha, reason, incarnation, workspace_path " "FROM local_review_repair_attempts WHERE repository=? AND pr_number=? AND attempt_id=?",
                (request.repository, request.pr_number, attempt_id or request.attempt_id),
            ).fetchone()
        if row is None:
            return None
        return LocalReviewRepairRecord(
            attempt_id=str(row[0]),
            head_sha=str(row[1]),
            head_ref=str(row[2]),
            phase=str(row[3]),
            result_sha=str(row[4]),
            reason=str(row[5]),
            incarnation=int(row[6]),
            workspace_path=str(row[7]),
        )


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


def _prepare_default_executor(request: LocalReviewRepairRequest) -> Callable[[LocalReviewRepairRequest, str], str]:
    from .cli_helpers import build_backend_manager

    with active_repo_context(request.repository):
        candidates = select_local_review_repair_candidates(request.repository)
        if not candidates:
            raise LocalBackendUnavailableError("no configured synchronous local backend is available")
        config = get_llm_config(repo_name=request.repository)
        manager = build_backend_manager(
            selected_backends=candidates,
            primary_backend=candidates[0],
            models={name: config.get_model_for_backend(name) or name for name in candidates},
            automatic_session_resume=False,
        )

    def invoke(_request: LocalReviewRepairRequest, worktree: str) -> str:
        with active_repo_context(request.repository):
            token = bind_command_execution_cwd(worktree)
            try:
                return manager.run_prompt(request.prompt)
            finally:
                reset_command_execution_cwd(token)

    return invoke


def _default_executor(request: LocalReviewRepairRequest, worktree: str) -> str:
    invoke = _prepare_default_executor(request)
    return invoke(request, worktree)


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
        claim = LocalReviewRepairClaim(False, record.phase, record.attempt_id, record.incarnation)
        store.transition(request, claim, "awaiting_validation", result_sha=record.result_sha)
        return LocalReviewRepairOutcome(
            "awaiting_validation",
            f"reconciled published {record.result_sha} on {request.head_ref}; independent validation is required",
            executed=True,
            published=True,
        )
    if remote_head != record.head_sha:
        return LocalReviewRepairOutcome(
            "publication_pending",
            f"publication conflict: {request.head_ref} advanced to {remote_head or 'an unavailable ref'}",
            executed=True,
        )
    push = git_push(
        cwd=str(root),
        remote="origin",
        branch=f"{record.result_sha}:{request.head_ref}",
        expected_remote_sha=record.head_sha,
    )
    if not push.success:
        claim = LocalReviewRepairClaim(False, record.phase, record.attempt_id, record.incarnation)
        store.transition(request, claim, "publication_pending", result_sha=record.result_sha, reason=push.stderr, workspace_path=record.workspace_path)
        return LocalReviewRepairOutcome("publication_pending", f"publication incomplete: {push.stderr}", executed=True)
    claim = LocalReviewRepairClaim(False, record.phase, record.attempt_id, record.incarnation)
    store.transition(request, claim, "awaiting_validation", result_sha=record.result_sha)
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
    allowance_authority: Optional[LocalRepairAllowanceAuthority] = None,
) -> LocalReviewRepairOutcome:
    """Run one fenced correction in a detached exact-head checkout and publish it."""
    if request.head_repository != request.repository:
        return LocalReviewRepairOutcome("not_admitted", "foreign-head pull requests are not eligible")
    if executor is None:
        try:
            executor = _prepare_default_executor(request)
        except LocalBackendUnavailableError as exc:
            return LocalReviewRepairOutcome("backend_unavailable", str(exc))
    store = store or LocalReviewRepairStore(local_review_repair_db_path(request.repository))
    claim = store.admit(request)
    if not claim.admitted:
        if claim.phase == "publication_pending":
            record = store.get(request, claim.attempt_id)
            if record is not None:
                return _resume_publication(request, store, record, Path.cwd())
        return LocalReviewRepairOutcome(claim.phase, f"retained local correction phase: {claim.phase}")

    root = Path.cwd()
    worktree = tempfile.mkdtemp(prefix=f"auto_coder_review_{request.pr_number}_")
    preserve_worktree = False
    try:
        added = subprocess.run(
            ["git", "worktree", "add", "--detach", worktree, request.head_sha],
            cwd=root,
            capture_output=True,
            text=True,
        )
        if added.returncode != 0:
            store.transition(request, claim, "not_started", reason=added.stderr.strip())
            return LocalReviewRepairOutcome("not_started", f"protected checkout failed: {added.stderr.strip()}")
        if allowance_authority is not None:
            try:
                allowance_authority.mark_invocation()
            except Exception as exc:
                store.transition(request, claim, "not_started", reason=str(exc))
                return LocalReviewRepairOutcome("not_started", f"local invocation was not admitted: {exc}")
        try:
            response = executor(request, worktree)
        except Exception as exc:
            store.transition(request, claim, "indeterminate", reason=str(exc), workspace_path=worktree)
            preserve_worktree = True
            return LocalReviewRepairOutcome("indeterminate", f"local backend outcome is indeterminate: {exc}", executed=True)

        status = subprocess.run(["git", "status", "--porcelain"], cwd=worktree, capture_output=True, text=True)
        if status.returncode != 0:
            store.transition(request, claim, "indeterminate", reason=status.stderr.strip(), workspace_path=worktree)
            preserve_worktree = True
            return LocalReviewRepairOutcome("indeterminate", "could not inspect corrective output", executed=True)
        if response.strip() == "CANNOT_FIX":
            store.transition(request, claim, "terminal_failure", reason="local backend returned CANNOT_FIX")
            if allowance_authority is not None:
                allowance_authority.mark_completion(code_changed=False, evidence="local backend returned CANNOT_FIX")
            return LocalReviewRepairOutcome("terminal_failure", "local backend could not correct the finding", executed=True)
        if not status.stdout.strip():
            store.transition(request, claim, "completed_no_change", result_sha=request.head_sha)
            if allowance_authority is not None:
                allowance_authority.mark_completion(code_changed=False, evidence="local correction completed with no change")
            return LocalReviewRepairOutcome("completed_no_change", "local correction completed with no change; independent validation is required", executed=True)

        stage = subprocess.run(["git", "add", "-A"], cwd=worktree, capture_output=True, text=True)
        if stage.returncode != 0:
            store.transition(request, claim, "indeterminate", reason=stage.stderr, workspace_path=worktree)
            preserve_worktree = True
            return LocalReviewRepairOutcome("indeterminate", f"corrective output could not be staged: {stage.stderr}", executed=True)
        commit = git_commit_with_retry(f"Fix unresolved review findings for PR #{request.pr_number}", cwd=worktree)
        if not commit.success:
            store.transition(request, claim, "indeterminate", reason=commit.stderr, workspace_path=worktree)
            preserve_worktree = True
            return LocalReviewRepairOutcome("indeterminate", f"corrective commit failed after execution: {commit.stderr}", executed=True)
        sha_result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=worktree, capture_output=True, text=True)
        result_sha = sha_result.stdout.strip()
        store.transition(request, claim, "publication_pending", result_sha=result_sha)
        push = git_push(cwd=worktree, remote="origin", branch=f"HEAD:{request.head_ref}", expected_remote_sha=request.head_sha)
        if not push.success:
            store.transition(request, claim, "publication_pending", result_sha=result_sha, reason=push.stderr)
            return LocalReviewRepairOutcome("publication_pending", f"publication incomplete: {push.stderr}", executed=True)
        store.transition(request, claim, "awaiting_validation", result_sha=result_sha)
        if allowance_authority is not None:
            allowance_authority.mark_completion(code_changed=True, evidence=f"local correction published as {result_sha}")
        return LocalReviewRepairOutcome("awaiting_validation", f"published {result_sha} to {request.head_ref}; independent validation is required", executed=True, published=True)
    finally:
        if not preserve_worktree:
            subprocess.run(["git", "worktree", "remove", "--force", worktree], cwd=root, capture_output=True)
            try:
                os.rmdir(worktree)
            except OSError:
                pass
