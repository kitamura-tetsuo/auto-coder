"""Durable, attempt-bound envelopes for accepted local model jobs.

This module is deliberately an evidence producer. It does not start a worker,
invoke a backend, publish changes, or settle the Issue/PR domain owner.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from .durable_repair_allowance import GenerationLifecycleState, RepairAllowanceLedger
from .issue_dispatch import DispatchOutcome, IssueAttemptIdentity, IssueDispatchGuard
from .local_review_repair import LocalReviewRepairRequest, LocalReviewRepairStore, local_review_repair_db_path


class LocalJobKind(str, Enum):
    ISSUE_IMPLEMENTATION = "issue_implementation"
    PR_REVIEW_CORRECTION = "pr_review_correction"
    PR_CI_FAILURE = "pr_ci_failure"
    PR_MERGE_CONFLICT = "pr_merge_conflict"


class LocalJobState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    RESULT_RECORDED = "result_recorded"
    DOWNSTREAM_EFFECTS_PENDING = "downstream_effects_pending"
    SETTLED = "settled"


class InvocationOutcome(str, Enum):
    COMPLETED = "completed"
    CANNOT_FIX = "cannot_fix"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class StartNotificationState(str, Enum):
    """Durable delivery knowledge for a local Issue start notification."""

    PENDING = "pending"
    INDETERMINATE = "indeterminate"
    DEFINITELY_UNSENT = "definitely_unsent"
    CONFIRMED = "confirmed"


class ContinuationState(str, Enum):
    """Provider progress, deliberately independent of job/result settlement."""

    FOREGROUND = "foreground"
    WAITING_BACKGROUND = "waiting_background"
    CONTINUATION_PENDING = "continuation_pending"
    NATIVE_CONTINUATION_ACTIVE = "native_continuation_active"
    CONTROLLER_READY = "controller_ready"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    FINAL_CONFIRMED = "final_confirmed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class ContinuationIntentState(str, Enum):
    NOT_SUBMITTED = "not_submitted"
    ADMITTED = "admitted"
    INDETERMINATE = "indeterminate"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class LocalJobContinuation:
    job_id: str
    execution_incarnation: str
    executor_token: str
    provider_session: str
    state: ContinuationState
    frontier: int = 0
    candidate_frontier: Optional[int] = None
    candidate_output: str = ""
    final_output: str = ""
    provider_position: str = ""
    deadline_at: Optional[float] = None
    reconciliation_reason: str = ""
    provider_workspace: str = ""
    writer_owner: str = ""


@dataclass(frozen=True)
class ContinuationTask:
    job_id: str
    task_id: str
    lifecycle: str
    terminal_outcome: str = ""
    result_reference: str = ""
    provider_position: str = ""
    sequence: int = 0


@dataclass(frozen=True)
class ContinuationIntent:
    intent_id: str
    job_id: str
    provider_session: str
    causal_frontier: int
    command_kind: str
    payload_identity: str
    provider_turn: str
    state: ContinuationIntentState


@dataclass(frozen=True)
class LocalJobOffer:
    kind: LocalJobKind
    repository: str
    target_number: int
    upstream_attempt: str
    backend_name: str
    invocation_input: str
    implementation_execution_id: str = ""

    @property
    def input_identity(self) -> str:
        return hashlib.sha256(self.invocation_input.encode("utf-8")).hexdigest()

    @property
    def job_id(self) -> str:
        material = json.dumps(
            [self.kind.value, self.repository, self.target_number, self.upstream_attempt, self.backend_name, self.input_identity],
            separators=(",", ":"),
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PRRepairAdmission:
    """Authoritative facts retained with one exact-head PR repair offer."""

    head_repository: str
    head_ref: str
    head_sha: str
    base_repository: str
    base_ref: str
    base_identity: str
    repair_kind: str
    source_evidence_identity: str
    upstream_incarnation: str
    failed_check_identities: tuple[str, ...] = ()
    pr_open: bool = False
    pr_unmerged: bool = False
    local_route: bool = False
    owner_allowed: bool = False
    conflict_eligible: bool = False
    ci_complete_failure: bool = False


@dataclass(frozen=True)
class RetainedPRRepairClaim:
    """Immutable source identity persisted for one admitted PR repair job."""

    job_id: str
    repository: str
    pr_number: int
    head_repository: str
    head_ref: str
    head_sha: str
    base_repository: str
    base_ref: str
    base_identity: str
    repair_kind: str
    source_evidence_identity: str
    upstream_incarnation: str
    failed_check_identities: tuple[str, ...] = ()


@dataclass(frozen=True)
class LocalJobRecord:
    job_id: str
    kind: LocalJobKind
    repository: str
    target_number: int
    upstream_attempt: str
    upstream_incarnation: str
    backend_name: str
    input_identity: str
    invocation_input: str
    state: LocalJobState
    execution_incarnation: str = ""
    invocation_outcome: Optional[InvocationOutcome] = None
    result_reference: str = ""
    diagnostic: str = ""
    provider_entered: bool = False
    runner_owner: str = ""
    workspace_path: str = ""
    source_commit: str = ""
    source_ref: str = ""
    work_branch: str = ""
    publication_remote: str = ""
    owner_incarnation: str = ""
    owner_generation: Optional[str] = None
    implementation_execution_id: str = ""


@dataclass(frozen=True)
class LocalJobClaim:
    record: LocalJobRecord
    acquired: bool


@dataclass(frozen=True)
class LocalJobResultArtifact:
    artifact_id: str
    job_id: str
    execution_incarnation: str
    outcome: InvocationOutcome
    output: str


@dataclass(frozen=True)
class LocalJobEffect:
    """Durable evidence for one controller-owned downstream effect."""

    job_id: str
    execution_incarnation: str
    name: str
    state: str
    evidence: str = ""


@dataclass(frozen=True)
class EffectIntentClaim:
    """Result of an atomic first-effect-intent claim."""

    effect: LocalJobEffect
    inserted: bool


@dataclass(frozen=True)
class LocalIssueJobEvidence:
    """Read-only diagnostic projection of one attributable Issue job."""

    job: LocalJobRecord
    effects: tuple[LocalJobEffect, ...] = ()


@dataclass(frozen=True)
class LocalStartNotification:
    job_id: str
    repository: str
    issue_number: int
    backend_name: str
    model_name: str
    marker: str
    state: StartNotificationState
    diagnostic: str = ""


def default_local_job_db_path() -> Path:
    return Path.home() / ".auto-coder" / "local_jobs.sqlite3"


class LocalJobStore:
    """SQLite envelope with atomic ownership transfer and fenced writes."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or default_local_job_db_path()
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS local_jobs (
                job_id TEXT PRIMARY KEY, kind TEXT NOT NULL, repository TEXT NOT NULL,
                target_number INTEGER NOT NULL, upstream_attempt TEXT NOT NULL,
                upstream_incarnation TEXT NOT NULL, backend_name TEXT NOT NULL,
                input_identity TEXT NOT NULL, invocation_input TEXT NOT NULL,
                state TEXT NOT NULL, execution_incarnation TEXT NOT NULL DEFAULT '',
                invocation_outcome TEXT, result_reference TEXT NOT NULL DEFAULT '',
                diagnostic TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
                updated_at REAL NOT NULL, provider_entered INTEGER NOT NULL DEFAULT 0,
                runner_owner TEXT NOT NULL DEFAULT '', workspace_path TEXT NOT NULL DEFAULT '',
                source_commit TEXT NOT NULL DEFAULT '', source_ref TEXT NOT NULL DEFAULT '',
                work_branch TEXT NOT NULL DEFAULT '', publication_remote TEXT NOT NULL DEFAULT '',
                owner_incarnation TEXT NOT NULL DEFAULT '', owner_generation TEXT,
                implementation_execution_id TEXT NOT NULL DEFAULT '',
                UNIQUE(kind, repository, target_number, upstream_attempt))"""
            )
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(local_jobs)")}
            if "provider_entered" not in columns:
                connection.execute("ALTER TABLE local_jobs ADD COLUMN provider_entered INTEGER NOT NULL DEFAULT 0")
            if "runner_owner" not in columns:
                connection.execute("ALTER TABLE local_jobs ADD COLUMN runner_owner TEXT NOT NULL DEFAULT ''")
            for name in ("workspace_path", "source_commit", "source_ref", "work_branch", "publication_remote", "owner_incarnation"):
                if name not in columns:
                    connection.execute(f"ALTER TABLE local_jobs ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")  # nosec B608: fixed names
            if "owner_generation" not in columns:
                connection.execute("ALTER TABLE local_jobs ADD COLUMN owner_generation TEXT")
            if "implementation_execution_id" not in columns:
                connection.execute("ALTER TABLE local_jobs ADD COLUMN implementation_execution_id TEXT NOT NULL DEFAULT ''")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS local_job_results (
                artifact_id TEXT PRIMARY KEY, job_id TEXT NOT NULL,
                execution_incarnation TEXT NOT NULL, outcome TEXT NOT NULL,
                output TEXT NOT NULL, created_at REAL NOT NULL,
                UNIQUE(job_id, execution_incarnation))"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS local_pr_repair_claims (
                job_id TEXT PRIMARY KEY, repository TEXT NOT NULL,
                pr_number INTEGER NOT NULL, head_sha TEXT NOT NULL,
                base_identity TEXT NOT NULL, repair_kind TEXT NOT NULL,
                source_evidence_identity TEXT NOT NULL,
                upstream_incarnation TEXT NOT NULL,
                failed_check_identities_json TEXT NOT NULL,
                created_at REAL NOT NULL)"""
            )
            claim_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(local_pr_repair_claims)")}
            for name in ("head_repository", "head_ref", "base_repository", "base_ref"):
                if name not in claim_columns:
                    connection.execute(f"ALTER TABLE local_pr_repair_claims ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")  # nosec B608: fixed names
            connection.execute(
                """CREATE TABLE IF NOT EXISTS local_job_effects (
                job_id TEXT NOT NULL, execution_incarnation TEXT NOT NULL,
                name TEXT NOT NULL, state TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL,
                PRIMARY KEY(job_id, execution_incarnation, name))"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS local_start_notifications (
                job_id TEXT PRIMARY KEY, repository TEXT NOT NULL,
                issue_number INTEGER NOT NULL, backend_name TEXT NOT NULL,
                model_name TEXT NOT NULL, marker TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL, diagnostic TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL)"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS local_job_continuations (
                job_id TEXT PRIMARY KEY, execution_incarnation TEXT NOT NULL,
                executor_token TEXT NOT NULL, provider_session TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL, frontier INTEGER NOT NULL DEFAULT 0,
                candidate_frontier INTEGER, candidate_output TEXT NOT NULL DEFAULT '',
                final_output TEXT NOT NULL DEFAULT '', provider_position TEXT NOT NULL DEFAULT '',
                observation_json TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL)"""
            )
            columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(local_job_continuations)")}
            if "observation_json" not in columns:
                connection.execute("ALTER TABLE local_job_continuations ADD COLUMN observation_json TEXT NOT NULL DEFAULT ''")
            if "deadline_at" not in columns:
                connection.execute("ALTER TABLE local_job_continuations ADD COLUMN deadline_at REAL")
            for column in ("provider_workspace", "writer_owner"):
                if column not in columns:
                    connection.execute(f"ALTER TABLE local_job_continuations ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
            if "reconciliation_reason" not in columns:
                connection.execute("ALTER TABLE local_job_continuations ADD COLUMN reconciliation_reason TEXT NOT NULL DEFAULT ''")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS local_continuation_tasks (
                job_id TEXT NOT NULL, task_id TEXT NOT NULL, lifecycle TEXT NOT NULL,
                terminal_outcome TEXT NOT NULL DEFAULT '', result_reference TEXT NOT NULL DEFAULT '',
                provider_position TEXT NOT NULL DEFAULT '', sequence INTEGER NOT NULL,
                updated_at REAL NOT NULL, PRIMARY KEY(job_id, task_id))"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS local_continuation_intents (
                intent_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, provider_session TEXT NOT NULL,
                causal_frontier INTEGER NOT NULL, command_kind TEXT NOT NULL,
                payload_identity TEXT NOT NULL, provider_turn TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL, updated_at REAL NOT NULL,
                UNIQUE(job_id, provider_session, causal_frontier, command_kind, payload_identity))"""
            )

    @staticmethod
    def _start_notification(row: sqlite3.Row) -> LocalStartNotification:
        return LocalStartNotification(
            job_id=str(row["job_id"]),
            repository=str(row["repository"]),
            issue_number=int(row["issue_number"]),
            backend_name=str(row["backend_name"]),
            model_name=str(row["model_name"]),
            marker=str(row["marker"]),
            state=StartNotificationState(str(row["state"])),
            diagnostic=str(row["diagnostic"]),
        )

    def observe_issue_start(self, job: LocalJobRecord, *, backend_name: str, model_name: str, marker: str) -> Optional[LocalStartNotification]:
        """Persist the actual client-dispatch fact without depending on job settlement."""
        if job.kind is not LocalJobKind.ISSUE_IMPLEMENTATION or not marker:
            return None
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT OR IGNORE INTO local_start_notifications " "(job_id, repository, issue_number, backend_name, model_name, marker, state, diagnostic, updated_at) " "VALUES (?, ?, ?, ?, ?, ?, ?, '', ?)",
                    (job.job_id, job.repository, job.target_number, backend_name, model_name or "unknown", marker, StartNotificationState.PENDING.value, time.time()),
                )
                row = connection.execute("SELECT * FROM local_start_notifications WHERE job_id=?", (job.job_id,)).fetchone()
                connection.commit()
            if row is None or str(row["marker"]) != marker:
                return None
            return self._start_notification(row)
        except (OSError, sqlite3.Error, ValueError):
            return None

    def claim_start_notification(self, job_id: str) -> Optional[LocalStartNotification]:
        """Atomically claim a notification that is authoritatively safe to POST."""
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "UPDATE local_start_notifications SET state=?, diagnostic='', updated_at=? " "WHERE job_id=? AND state IN (?, ?)",
                    (StartNotificationState.INDETERMINATE.value, time.time(), job_id, StartNotificationState.PENDING.value, StartNotificationState.DEFINITELY_UNSENT.value),
                )
                row = connection.execute("SELECT * FROM local_start_notifications WHERE job_id=?", (job_id,)).fetchone()
                connection.commit()
            return self._start_notification(row) if cursor.rowcount == 1 and row is not None else None
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise RuntimeError(f"local start notification claim failed for {job_id}") from exc

    def set_start_notification_state(self, job_id: str, expected: StartNotificationState, state: StartNotificationState, diagnostic: str = "") -> bool:
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "UPDATE local_start_notifications SET state=?, diagnostic=?, updated_at=? WHERE job_id=? AND state=?",
                    (state.value, diagnostic[:1000], time.time(), job_id, expected.value),
                )
            return cursor.rowcount == 1
        except (OSError, sqlite3.Error) as exc:
            raise RuntimeError(f"local start notification state update failed for {job_id}") from exc

    def recoverable_start_notifications(self) -> tuple[LocalStartNotification, ...]:
        """Return notifications needing safe send or exact-marker reconciliation."""
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT * FROM local_start_notifications WHERE state != ? ORDER BY updated_at",
                    (StartNotificationState.CONFIRMED.value,),
                ).fetchall()
            return tuple(self._start_notification(row) for row in rows)
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise RuntimeError("local start notification recovery enumeration failed") from exc

    def get_start_notification(self, job_id: str) -> Optional[LocalStartNotification]:
        try:
            with self._connect() as connection:
                row = connection.execute("SELECT * FROM local_start_notifications WHERE job_id=?", (job_id,)).fetchone()
            return self._start_notification(row) if row is not None else None
        except (OSError, sqlite3.Error, ValueError):
            return None

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @staticmethod
    def _record(row: sqlite3.Row) -> LocalJobRecord:
        raw_outcome = row["invocation_outcome"]
        return LocalJobRecord(
            job_id=str(row["job_id"]),
            kind=LocalJobKind(row["kind"]),
            repository=str(row["repository"]),
            target_number=int(row["target_number"]),
            upstream_attempt=str(row["upstream_attempt"]),
            upstream_incarnation=str(row["upstream_incarnation"]),
            backend_name=str(row["backend_name"]),
            input_identity=str(row["input_identity"]),
            invocation_input=str(row["invocation_input"]),
            state=LocalJobState(row["state"]),
            execution_incarnation=str(row["execution_incarnation"]),
            invocation_outcome=InvocationOutcome(raw_outcome) if raw_outcome else None,
            result_reference=str(row["result_reference"]),
            diagnostic=str(row["diagnostic"]),
            provider_entered=bool(row["provider_entered"]),
            runner_owner=str(row["runner_owner"]),
            workspace_path=str(row["workspace_path"]),
            source_commit=str(row["source_commit"]),
            source_ref=str(row["source_ref"]),
            work_branch=str(row["work_branch"]),
            publication_remote=str(row["publication_remote"]),
            owner_incarnation=str(row["owner_incarnation"]),
            owner_generation=str(row["owner_generation"]) if row["owner_generation"] is not None else None,
            implementation_execution_id=str(row["implementation_execution_id"]),
        )

    @staticmethod
    def _attach(connection: sqlite3.Connection, path: Path, alias: str) -> None:
        connection.execute(f"ATTACH DATABASE ? AS {alias}", (str(path),))  # nosec B608: internal alias

    @staticmethod
    def _existing(connection: sqlite3.Connection, offer: LocalJobOffer, upstream_incarnation: str) -> Optional[LocalJobRecord]:
        row = connection.execute(
            "SELECT * FROM local_jobs WHERE kind=? AND repository=? AND target_number=? AND upstream_attempt=?",
            (offer.kind.value, offer.repository, offer.target_number, offer.upstream_attempt),
        ).fetchone()
        if row is None:
            return None
        record = LocalJobStore._record(row)
        if record.job_id != offer.job_id or record.upstream_incarnation != upstream_incarnation:
            raise ValueError("conflicting durable local-job offer")
        return record

    @staticmethod
    def _insert(connection: sqlite3.Connection, offer: LocalJobOffer, upstream_incarnation: str) -> None:
        now = time.time()
        connection.execute(
            "INSERT INTO local_jobs "
            "(job_id, kind, repository, target_number, upstream_attempt, upstream_incarnation, "
            "backend_name, input_identity, invocation_input, state, execution_incarnation, "
            "invocation_outcome, result_reference, diagnostic, implementation_execution_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', NULL, '', '', ?, ?, ?)",
            (
                offer.job_id,
                offer.kind.value,
                offer.repository,
                offer.target_number,
                offer.upstream_attempt,
                upstream_incarnation,
                offer.backend_name,
                offer.input_identity,
                offer.invocation_input,
                LocalJobState.PENDING.value,
                offer.implementation_execution_id,
                now,
                now,
            ),
        )

    def offer_issue(self, offer: LocalJobOffer, identity: IssueAttemptIdentity, guard: IssueDispatchGuard) -> Optional[LocalJobRecord]:
        """Atomically transfer an exact, definitely-not-entered Issue claim."""
        if offer.kind is not LocalJobKind.ISSUE_IMPLEMENTATION or offer.repository != identity.full_repository_name or offer.target_number != identity.issue_number or offer.upstream_attempt != identity.implementation_attempt_id:
            return None
        ownership = guard.inspect_local_job_authority(identity)
        if ownership is None or ownership.outcome is not DispatchOutcome.INDETERMINATE or not ownership.claim_incarnation or ownership.backend_name != offer.backend_name:
            return None
        try:
            with self._connect() as connection:
                self._attach(connection, guard.database_path, "upstream")
                connection.execute("BEGIN IMMEDIATE")
                existing = self._existing(connection, offer, ownership.claim_incarnation)
                if existing is not None:
                    connection.commit()
                    return existing
                cursor = connection.execute(
                    "UPDATE upstream.issue_dispatch_handoffs SET state='local_job_handoff', updated_at=? " "WHERE repository_owner=? AND repository_name=? AND issue_number=? AND attempt_id=? " "AND incarnation=? AND state='pending' AND backend_name=?",
                    (
                        time.time(),
                        identity.repository_owner,
                        identity.repository_name,
                        identity.issue_number,
                        identity.implementation_attempt_id,
                        ownership.claim_incarnation,
                        offer.backend_name,
                    ),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    return None
                self._insert(connection, offer, ownership.claim_incarnation)
                connection.commit()
            return self.get(offer.job_id)
        except (OSError, sqlite3.Error, ValueError):
            return None

    def offer_pr_correction(
        self,
        offer: LocalJobOffer,
        request: LocalReviewRepairRequest,
        repair_store: LocalReviewRepairStore,
        allowance_ledger: RepairAllowanceLedger,
    ) -> Optional[LocalJobRecord]:
        """Atomically transfer exact, definitely-not-entered PR authorities."""
        if offer.kind is not LocalJobKind.PR_REVIEW_CORRECTION or offer.repository != request.repository or offer.target_number != request.pr_number or offer.upstream_attempt != request.attempt_id:
            return None
        try:
            repair = repair_store.get(request)
            snapshot = allowance_ledger.get_snapshot("https://api.github.com", request.repository, request.pr_number)
        except Exception:
            return None
        generation = snapshot.get_outstanding_generation()
        if (
            repair is None
            or repair.phase != "executing"
            or repair.invocation_entered
            or (repair.local_job_id and repair.local_job_id != offer.job_id)
            or generation is None
            or generation.lifecycle_state is not GenerationLifecycleState.RESERVED
            or generation.owning_identity != "local-review-repair"
            or generation.bundle_reference != request.attempt_id
        ):
            return None
        upstream_incarnation = f"{repair.incarnation}:{generation.generation_id}"
        try:
            with self._connect() as connection:
                self._attach(connection, repair_store.path, "repair")
                self._attach(connection, allowance_ledger.database_path, "allowance")
                connection.execute("BEGIN IMMEDIATE")
                existing = self._existing(connection, offer, upstream_incarnation)
                if existing is not None:
                    connection.commit()
                    return existing
                competing = connection.execute(
                    "SELECT 1 FROM local_jobs WHERE repository=? AND target_number=? " "AND kind IN (?, ?) AND state<>? LIMIT 1",
                    (
                        offer.repository,
                        offer.target_number,
                        LocalJobKind.PR_CI_FAILURE.value,
                        LocalJobKind.PR_MERGE_CONFLICT.value,
                        LocalJobState.SETTLED.value,
                    ),
                ).fetchone()
                if competing is not None:
                    connection.rollback()
                    return None
                current_generation = connection.execute(
                    "SELECT 1 FROM allowance.generations WHERE generation_id=? AND bundle_reference=? " "AND owning_identity='local-review-repair' AND lifecycle_state='RESERVED'",
                    (generation.generation_id, request.attempt_id),
                ).fetchone()
                if current_generation is None:
                    connection.rollback()
                    return None
                cursor = connection.execute(
                    "UPDATE repair.local_review_repair_attempts SET local_job_id=?, updated_at=? " "WHERE repository=? AND pr_number=? AND attempt_id=? AND incarnation=? " "AND phase='executing' AND invocation_entered=0 AND local_job_id=''",
                    (offer.job_id, time.time(), request.repository, request.pr_number, request.attempt_id, repair.incarnation),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    return None
                self._insert(connection, offer, upstream_incarnation)
                connection.execute(
                    "UPDATE local_jobs SET owner_incarnation=?, owner_generation=? WHERE job_id=?",
                    (str(repair.incarnation), generation.generation_id, offer.job_id),
                )
                connection.commit()
            return self.get(offer.job_id)
        except (OSError, sqlite3.Error, ValueError):
            return None

    def offer_pr_repair(
        self,
        offer: LocalJobOffer,
        admission: PRRepairAdmission,
        repair_store: LocalReviewRepairStore | None = None,
    ) -> Optional[LocalJobRecord]:
        """Durably admit one exact-head local CI/conflict operation.

        The caller performs the final authoritative reads immediately before
        this call.  This transaction retains those immutable facts with the
        envelope and arbitrates all local edit ownership for the PR.
        """
        expected_kind = {
            "ci_failure": LocalJobKind.PR_CI_FAILURE,
            "merge_conflict": LocalJobKind.PR_MERGE_CONFLICT,
        }.get(admission.repair_kind)
        common_valid = (
            expected_kind is offer.kind
            and offer.repository == admission.base_repository
            and admission.head_repository == offer.repository
            and admission.pr_open
            and admission.pr_unmerged
            and admission.local_route
            and admission.owner_allowed
            and all(
                (
                    admission.head_ref,
                    admission.head_sha,
                    admission.base_ref,
                    admission.base_identity,
                    admission.source_evidence_identity,
                    admission.upstream_incarnation,
                )
            )
        )
        kind_valid = admission.ci_complete_failure and bool(admission.failed_check_identities) if expected_kind is LocalJobKind.PR_CI_FAILURE else admission.conflict_eligible
        if not common_valid or not kind_valid:
            return None
        try:
            repair_store = repair_store or LocalReviewRepairStore(local_review_repair_db_path(offer.repository))
            with self._connect() as connection:
                self._attach(connection, repair_store.path, "review_repair")
                self._pr_repair_cas_checkpoint()
                connection.execute("BEGIN IMMEDIATE")
                existing = self._existing(connection, offer, admission.upstream_incarnation)
                if existing is not None:
                    retained_claim = connection.execute(
                        "SELECT 1 FROM local_pr_repair_claims WHERE job_id=? AND head_sha=? "
                        "AND base_identity=? AND repair_kind=? AND source_evidence_identity=? "
                        "AND upstream_incarnation=? AND failed_check_identities_json=? "
                        "AND head_repository=? AND head_ref=? AND base_repository=? AND base_ref=?",
                        (
                            offer.job_id,
                            admission.head_sha,
                            admission.base_identity,
                            admission.repair_kind,
                            admission.source_evidence_identity,
                            admission.upstream_incarnation,
                            json.dumps(admission.failed_check_identities, separators=(",", ":")),
                            admission.head_repository,
                            admission.head_ref,
                            admission.base_repository,
                            admission.base_ref,
                        ),
                    ).fetchone()
                    if retained_claim is None:
                        connection.rollback()
                        return None
                    connection.commit()
                    return existing
                competing = connection.execute(
                    "SELECT 1 FROM local_jobs WHERE repository=? AND target_number=? " "AND kind IN (?, ?, ?) AND state<>? LIMIT 1",
                    (
                        offer.repository,
                        offer.target_number,
                        LocalJobKind.PR_REVIEW_CORRECTION.value,
                        LocalJobKind.PR_CI_FAILURE.value,
                        LocalJobKind.PR_MERGE_CONFLICT.value,
                        LocalJobState.SETTLED.value,
                    ),
                ).fetchone()
                if competing is not None:
                    connection.rollback()
                    return None
                competing_review_claim = connection.execute(
                    "SELECT 1 FROM review_repair.local_review_repair_attempts " "WHERE repository=? AND pr_number=? " "AND phase IN ('executing', 'indeterminate', 'publication_pending') LIMIT 1",
                    (offer.repository, offer.target_number),
                ).fetchone()
                if competing_review_claim is not None:
                    connection.rollback()
                    return None
                self._insert(connection, offer, admission.upstream_incarnation)
                connection.execute(
                    "INSERT INTO local_pr_repair_claims "
                    "(job_id, repository, pr_number, head_sha, base_identity, repair_kind, "
                    "source_evidence_identity, upstream_incarnation, failed_check_identities_json, created_at, "
                    "head_repository, head_ref, base_repository, base_ref) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        offer.job_id,
                        offer.repository,
                        offer.target_number,
                        admission.head_sha,
                        admission.base_identity,
                        admission.repair_kind,
                        admission.source_evidence_identity,
                        admission.upstream_incarnation,
                        json.dumps(admission.failed_check_identities, separators=(",", ":")),
                        time.time(),
                        admission.head_repository,
                        admission.head_ref,
                        admission.base_repository,
                        admission.base_ref,
                    ),
                )
                connection.commit()
            return self.get(offer.job_id)
        except (OSError, sqlite3.Error, ValueError):
            return None

    def _pr_repair_cas_checkpoint(self) -> None:
        """Test seam immediately before the durable repair admission CAS."""

    def get(self, job_id: str) -> Optional[LocalJobRecord]:
        try:
            with self._connect() as connection:
                row = connection.execute("SELECT * FROM local_jobs WHERE job_id=?", (job_id,)).fetchone()
            return self._record(row) if row is not None else None
        except (OSError, sqlite3.Error, ValueError):
            return None

    def get_pr_repair_claim(self, job_id: str) -> Optional[RetainedPRRepairClaim]:
        """Read the exact admitted PR H/B authority; legacy incomplete rows fail closed."""
        try:
            with self._connect() as connection:
                row = connection.execute("SELECT * FROM local_pr_repair_claims WHERE job_id=?", (job_id,)).fetchone()
            if row is None or not all(str(row[name]) for name in ("head_repository", "head_ref", "head_sha", "base_repository", "base_ref", "base_identity")):
                return None
            return RetainedPRRepairClaim(
                str(row["job_id"]),
                str(row["repository"]),
                int(row["pr_number"]),
                str(row["head_repository"]),
                str(row["head_ref"]),
                str(row["head_sha"]),
                str(row["base_repository"]),
                str(row["base_ref"]),
                str(row["base_identity"]),
                str(row["repair_kind"]),
                str(row["source_evidence_identity"]),
                str(row["upstream_incarnation"]),
                tuple(str(value) for value in json.loads(str(row["failed_check_identities_json"]))),
            )
        except (OSError, sqlite3.Error, ValueError):
            return None

    def discover_unsettled(self) -> tuple[LocalJobRecord, ...]:
        """Return recoverable jobs; RUNNING means reconcile, never auto-reissue."""
        try:
            with self._connect() as connection:
                rows = connection.execute("SELECT * FROM local_jobs WHERE state<>? ORDER BY created_at", (LocalJobState.SETTLED.value,)).fetchall()
            return tuple(self._record(row) for row in rows)
        except (OSError, sqlite3.Error, ValueError):
            return ()

    def retained_issue_execution_ids(self, repository: str) -> frozenset[str]:
        """Read durable execution responsibility strictly for slot reclamation."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT implementation_execution_id FROM local_jobs WHERE repository=? AND kind=? AND state<>? AND implementation_execution_id<>''",
                (repository, LocalJobKind.ISSUE_IMPLEMENTATION.value, LocalJobState.SETTLED.value),
            ).fetchall()
        return frozenset(str(row[0]) for row in rows)

    def pr_repair_for_execution(self, repository: str, pr_number: int, execution_id: str) -> Optional[LocalJobRecord]:
        """Strictly find the unsettled PR repair owning an implementation execution."""
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT * FROM local_jobs WHERE repository=? AND target_number=? AND kind IN (?, ?) AND state<>? AND implementation_execution_id=?",
                    (repository, pr_number, LocalJobKind.PR_CI_FAILURE.value, LocalJobKind.PR_MERGE_CONFLICT.value, LocalJobState.SETTLED.value, execution_id),
                ).fetchall()
            if len(rows) > 1:
                raise RuntimeError("multiple local PR repair jobs own one execution")
            return self._record(rows[0]) if rows else None
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise RuntimeError("local PR repair execution lookup failed") from exc

    def issue_job_evidence(self, repository: str, issue_number: int) -> tuple[LocalIssueJobEvidence, ...]:
        """Read all retained jobs and effects for one exact repository/Issue.

        Unlike recovery enumeration, diagnostic callers need to distinguish an
        empty history from an unavailable store, so read errors are surfaced.
        This method performs no lifecycle transitions.
        """
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT * FROM local_jobs WHERE repository=? AND target_number=? AND kind=? ORDER BY created_at, job_id",
                    (repository, issue_number, LocalJobKind.ISSUE_IMPLEMENTATION.value),
                ).fetchall()
                evidence = []
                for row in rows:
                    job = self._record(row)
                    effect_rows = connection.execute(
                        "SELECT job_id, execution_incarnation, name, state, evidence FROM local_job_effects " "WHERE job_id=? AND execution_incarnation=? ORDER BY name",
                        (job.job_id, job.execution_incarnation),
                    ).fetchall()
                    effects = tuple(
                        LocalJobEffect(
                            str(effect["job_id"]),
                            str(effect["execution_incarnation"]),
                            str(effect["name"]),
                            str(effect["state"]),
                            str(effect["evidence"]),
                        )
                        for effect in effect_rows
                    )
                    evidence.append(LocalIssueJobEvidence(job, effects))
            return tuple(evidence)
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise RuntimeError("local Issue job diagnostic read failed") from exc

    def claim(self, job_id: str, runner_owner: str = "") -> Optional[LocalJobClaim]:
        incarnation = str(uuid.uuid4())
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "UPDATE local_jobs SET state=?, execution_incarnation=?, runner_owner=?, updated_at=? WHERE job_id=? AND state=? AND execution_incarnation=''",
                    (LocalJobState.RUNNING.value, incarnation, runner_owner, time.time(), job_id, LocalJobState.PENDING.value),
                )
                row = connection.execute("SELECT * FROM local_jobs WHERE job_id=?", (job_id,)).fetchone()
                connection.commit()
            return LocalJobClaim(self._record(row), cursor.rowcount == 1) if row is not None else None
        except (OSError, sqlite3.Error, ValueError):
            return None

    def mark_provider_entered(self, claim: LocalJobClaim) -> bool:
        """Durably fence the exact incarnation immediately before provider entry."""
        if not claim.acquired:
            return False
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "UPDATE local_jobs SET provider_entered=1, updated_at=? WHERE job_id=? AND state=? " "AND execution_incarnation=? AND provider_entered=0",
                    (time.time(), claim.record.job_id, LocalJobState.RUNNING.value, claim.record.execution_incarnation),
                )
            return cursor.rowcount == 1
        except (OSError, sqlite3.Error):
            return False

    def record_runner_diagnostic(self, claim: LocalJobClaim, diagnostic: str) -> bool:
        """Record a non-result runner failure without implying provider completion."""
        if not claim.acquired:
            return False
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "UPDATE local_jobs SET diagnostic=?, updated_at=? WHERE job_id=? AND state=? AND execution_incarnation=?",
                    (diagnostic, time.time(), claim.record.job_id, LocalJobState.RUNNING.value, claim.record.execution_incarnation),
                )
            return cursor.rowcount == 1
        except (OSError, sqlite3.Error):
            return False

    def release_unentered_claim(self, claim: LocalJobClaim, diagnostic: str) -> bool:
        """Return a positively unentered incarnation to pending for later retry."""
        if not claim.acquired:
            return False
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "UPDATE local_jobs SET state=?, execution_incarnation='', runner_owner='', diagnostic=?, updated_at=? " "WHERE job_id=? AND state=? AND execution_incarnation=? AND provider_entered=0",
                    (
                        LocalJobState.PENDING.value,
                        diagnostic,
                        time.time(),
                        claim.record.job_id,
                        LocalJobState.RUNNING.value,
                        claim.record.execution_incarnation,
                    ),
                )
            return cursor.rowcount == 1
        except (OSError, sqlite3.Error):
            return False

    def release_dead_owner_claim(self, record: LocalJobRecord, diagnostic: str) -> bool:
        """Recover an exact unentered incarnation after its owner is proven dead."""
        if record.state is not LocalJobState.RUNNING or record.provider_entered or not record.execution_incarnation or not record.runner_owner:
            return False
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "UPDATE local_jobs SET state=?, execution_incarnation='', runner_owner='', diagnostic=?, updated_at=? " "WHERE job_id=? AND state=? AND execution_incarnation=? AND runner_owner=? AND provider_entered=0",
                    (
                        LocalJobState.PENDING.value,
                        diagnostic,
                        time.time(),
                        record.job_id,
                        LocalJobState.RUNNING.value,
                        record.execution_incarnation,
                        record.runner_owner,
                    ),
                )
            return cursor.rowcount == 1
        except (OSError, sqlite3.Error):
            return False

    def recover_downstream_pending(self, record: LocalJobRecord, *, executor_token: str = "") -> bool:
        """Promote an exact committed result after validating its durable artifact."""
        if record.state is not LocalJobState.RESULT_RECORDED or record.invocation_outcome is None or not record.result_reference:
            return False
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                artifact = connection.execute(
                    "SELECT 1 FROM local_job_results WHERE artifact_id=? AND job_id=? AND execution_incarnation=? AND outcome=?",
                    (record.result_reference, record.job_id, record.execution_incarnation, record.invocation_outcome.value),
                ).fetchone()
                if artifact is None:
                    connection.rollback()
                    return False
                cursor = connection.execute(
                    "UPDATE local_jobs AS j SET state=?, updated_at=? WHERE job_id=? AND state=? "
                    "AND execution_incarnation=? AND result_reference=? "
                    "AND NOT EXISTS (SELECT 1 FROM local_job_continuations c WHERE c.job_id=j.job_id "
                    "AND (c.execution_incarnation<>j.execution_incarnation OR c.executor_token<>? OR c.state<>?))",
                    (
                        LocalJobState.DOWNSTREAM_EFFECTS_PENDING.value,
                        time.time(),
                        record.job_id,
                        LocalJobState.RESULT_RECORDED.value,
                        record.execution_incarnation,
                        record.result_reference,
                        executor_token,
                        ContinuationState.FINAL_CONFIRMED.value,
                    ),
                )
                connection.commit()
            return cursor.rowcount == 1
        except (OSError, sqlite3.Error):
            return False

    def recover_recorded_result(self, record: LocalJobRecord, *, executor_token: str = "") -> bool:
        """Checkpoint a previously persisted exact-incarnation provider result."""
        if record.state is not LocalJobState.RUNNING or not record.provider_entered or not record.execution_incarnation:
            return False
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                artifact = connection.execute(
                    "SELECT artifact_id, outcome, output FROM local_job_results WHERE job_id=? AND execution_incarnation=?",
                    (record.job_id, record.execution_incarnation),
                ).fetchone()
                if artifact is None:
                    connection.rollback()
                    return False
                cursor = connection.execute(
                    "UPDATE local_jobs SET state=?, invocation_outcome=?, result_reference=?, updated_at=? "
                    "WHERE job_id=? AND state=? AND execution_incarnation=? AND provider_entered=1 AND result_reference='' "
                    "AND NOT EXISTS (SELECT 1 FROM local_job_continuations c WHERE c.job_id=local_jobs.job_id "
                    "AND (c.execution_incarnation<>local_jobs.execution_incarnation OR c.executor_token<>? "
                    "OR c.state<>?))",
                    (
                        LocalJobState.RESULT_RECORDED.value,
                        str(artifact["outcome"]),
                        str(artifact["artifact_id"]),
                        time.time(),
                        record.job_id,
                        LocalJobState.RUNNING.value,
                        record.execution_incarnation,
                        executor_token,
                        ContinuationState.FINAL_CONFIRMED.value,
                    ),
                )
                connection.commit()
            return cursor.rowcount == 1
        except (OSError, sqlite3.Error):
            return False

    def persist_result_artifact(
        self,
        claim: LocalJobClaim,
        outcome: InvocationOutcome,
        output: str,
        *,
        executor_token: str = "",
    ) -> Optional[LocalJobResultArtifact]:
        """Durably bind invocation output to the exact job and incarnation."""
        if not claim.acquired:
            return None
        artifact_id = hashlib.sha256(json.dumps([claim.record.job_id, claim.record.execution_incarnation, outcome.value, output], separators=(",", ":")).encode()).hexdigest()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    "SELECT 1 FROM local_jobs j WHERE job_id=? AND state=? AND execution_incarnation=? " "AND NOT EXISTS (SELECT 1 FROM local_job_continuations c WHERE c.job_id=j.job_id " "AND (c.execution_incarnation<>j.execution_incarnation OR c.executor_token<>? " "OR (?=1 AND c.state<>?)))",
                    (
                        claim.record.job_id,
                        LocalJobState.RUNNING.value,
                        claim.record.execution_incarnation,
                        executor_token,
                        int(outcome in {InvocationOutcome.COMPLETED, InvocationOutcome.CANNOT_FIX}),
                        ContinuationState.FINAL_CONFIRMED.value,
                    ),
                ).fetchone()
                if current is None:
                    connection.rollback()
                    return None
                connection.execute(
                    "INSERT OR IGNORE INTO local_job_results VALUES (?, ?, ?, ?, ?, ?)",
                    (artifact_id, claim.record.job_id, claim.record.execution_incarnation, outcome.value, output, time.time()),
                )
                row = connection.execute(
                    "SELECT artifact_id, job_id, execution_incarnation, outcome FROM local_job_results " "WHERE job_id=? AND execution_incarnation=?",
                    (claim.record.job_id, claim.record.execution_incarnation),
                ).fetchone()
                connection.commit()
            if row is None or str(row["artifact_id"]) != artifact_id or str(row["outcome"]) != outcome.value:
                return None
            return self.get_result_artifact(artifact_id)
        except (OSError, sqlite3.Error, ValueError):
            return None

    def bind_workspace(
        self,
        claim: LocalJobClaim,
        *,
        workspace_path: Path,
        source_commit: str,
        source_ref: str,
        work_branch: str,
        publication_remote: str,
        owner_incarnation: str,
        owner_generation: Optional[str],
    ) -> bool:
        """Fence a prepared full-job workspace to its exact running incarnation."""
        if not claim.acquired or not workspace_path.is_absolute() or not source_commit or not source_ref or not work_branch or not publication_remote:
            return False
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "UPDATE local_jobs SET workspace_path=?, source_commit=?, source_ref=?, work_branch=?, " "publication_remote=?, owner_incarnation=?, owner_generation=?, updated_at=? " "WHERE job_id=? AND state=? AND execution_incarnation=? AND workspace_path=''",
                    (
                        str(workspace_path),
                        source_commit,
                        source_ref,
                        work_branch,
                        publication_remote,
                        owner_incarnation,
                        owner_generation,
                        time.time(),
                        claim.record.job_id,
                        LocalJobState.RUNNING.value,
                        claim.record.execution_incarnation,
                    ),
                )
            return cursor.rowcount == 1
        except (OSError, sqlite3.Error):
            return False

    def get_result_artifact(self, artifact_id: str) -> Optional[LocalJobResultArtifact]:
        """Reconstruct one durable output artifact for downstream processing."""
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT artifact_id, job_id, execution_incarnation, outcome, output " "FROM local_job_results WHERE artifact_id=?",
                    (artifact_id,),
                ).fetchone()
            if row is None:
                return None
            return LocalJobResultArtifact(
                str(row["artifact_id"]),
                str(row["job_id"]),
                str(row["execution_incarnation"]),
                InvocationOutcome(row["outcome"]),
                str(row["output"]),
            )
        except (OSError, sqlite3.Error, ValueError):
            return None

    def get_execution_result_artifact(self, job_id: str, execution_incarnation: str) -> Optional[LocalJobResultArtifact]:
        """Read one exact execution artifact, distinguishing absence from read failure."""
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT artifact_id, job_id, execution_incarnation, outcome, output " "FROM local_job_results WHERE job_id=? AND execution_incarnation=?",
                    (job_id, execution_incarnation),
                ).fetchone()
            if row is None:
                return None
            return LocalJobResultArtifact(
                str(row["artifact_id"]),
                str(row["job_id"]),
                str(row["execution_incarnation"]),
                InvocationOutcome(row["outcome"]),
                str(row["output"]),
            )
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise RuntimeError("local execution artifact lookup failed") from exc

    def adopt_controller_result(self, record: LocalJobRecord, artifact: LocalJobResultArtifact) -> bool:
        """Fence a validated no-provider conflict result into its result checkpoint."""
        if record.kind is not LocalJobKind.PR_MERGE_CONFLICT or record.provider_entered:
            return False
        claim = LocalJobClaim(record, True)
        return self.record_result(claim, artifact.outcome, artifact.artifact_id, "recovered verified controller result", executor_token=record.runner_owner)

    def record_result(
        self,
        claim: LocalJobClaim,
        outcome: InvocationOutcome,
        result_reference: str,
        diagnostic: str = "",
        *,
        executor_token: str = "",
    ) -> bool:
        """Checkpoint a result only from a verified exact-job artifact."""
        if not claim.acquired or not result_reference:
            return False
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                evidence = connection.execute(
                    "SELECT 1 FROM local_job_results r JOIN local_jobs j ON j.job_id=r.job_id "
                    "WHERE r.artifact_id=? AND r.job_id=? AND r.execution_incarnation=? AND r.outcome=? "
                    "AND j.execution_incarnation=r.execution_incarnation "
                    "AND NOT EXISTS (SELECT 1 FROM local_job_continuations c WHERE c.job_id=j.job_id "
                    "AND (c.execution_incarnation<>j.execution_incarnation OR c.executor_token<>? "
                    "OR (?=1 AND c.state<>?)))",
                    (
                        result_reference,
                        claim.record.job_id,
                        claim.record.execution_incarnation,
                        outcome.value,
                        executor_token,
                        int(outcome in {InvocationOutcome.COMPLETED, InvocationOutcome.CANNOT_FIX}),
                        ContinuationState.FINAL_CONFIRMED.value,
                    ),
                ).fetchone()
                if evidence is None:
                    connection.rollback()
                    return False
                cursor = connection.execute(
                    "UPDATE local_jobs SET state=?, invocation_outcome=?, result_reference=?, diagnostic=?, updated_at=? " "WHERE job_id=? AND state=? AND execution_incarnation=?",
                    (
                        LocalJobState.RESULT_RECORDED.value,
                        outcome.value,
                        result_reference,
                        diagnostic,
                        time.time(),
                        claim.record.job_id,
                        LocalJobState.RUNNING.value,
                        claim.record.execution_incarnation,
                    ),
                )
                connection.commit()
            return cursor.rowcount == 1
        except (OSError, sqlite3.Error):
            return False

    def mark_downstream_pending(self, claim: LocalJobClaim, *, executor_token: str = "") -> bool:
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "UPDATE local_jobs AS j SET state=?, updated_at=? WHERE job_id=? AND state=? "
                    "AND execution_incarnation=? AND NOT EXISTS (SELECT 1 FROM local_job_continuations c "
                    "WHERE c.job_id=j.job_id AND (c.execution_incarnation<>j.execution_incarnation "
                    "OR c.executor_token<>? OR c.state<>?))",
                    (
                        LocalJobState.DOWNSTREAM_EFFECTS_PENDING.value,
                        time.time(),
                        claim.record.job_id,
                        LocalJobState.RESULT_RECORDED.value,
                        claim.record.execution_incarnation,
                        executor_token,
                        ContinuationState.FINAL_CONFIRMED.value,
                    ),
                )
            return cursor.rowcount == 1
        except (OSError, sqlite3.Error):
            return False

    def record_effect(self, claim: LocalJobClaim, name: str, state: str, evidence: str = "") -> bool:
        """Upsert fenced publication evidence for the exact result incarnation."""
        if not claim.acquired or not name or state not in {"pending", "completed", "indeterminate", "failed", "skipped"}:
            return False
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    "SELECT 1 FROM local_jobs WHERE job_id=? AND execution_incarnation=? " "AND state IN (?, ?)",
                    (
                        claim.record.job_id,
                        claim.record.execution_incarnation,
                        LocalJobState.RESULT_RECORDED.value,
                        LocalJobState.DOWNSTREAM_EFFECTS_PENDING.value,
                    ),
                ).fetchone()
                if current is None:
                    connection.rollback()
                    return False
                previous = connection.execute(
                    "SELECT state FROM local_job_effects WHERE job_id=? AND execution_incarnation=? AND name=?",
                    (claim.record.job_id, claim.record.execution_incarnation, name),
                ).fetchone()
                if previous is not None and str(previous["state"]) in {"completed", "skipped"} and state not in {"completed", "skipped"}:
                    connection.rollback()
                    return False
                connection.execute(
                    "INSERT INTO local_job_effects VALUES (?, ?, ?, ?, ?, ?) " "ON CONFLICT(job_id, execution_incarnation, name) DO UPDATE SET " "state=excluded.state, evidence=excluded.evidence, updated_at=excluded.updated_at",
                    (claim.record.job_id, claim.record.execution_incarnation, name, state, evidence, time.time()),
                )
                connection.commit()
            return True
        except (OSError, sqlite3.Error):
            return False

    def get_effect(self, job_id: str, execution_incarnation: str, name: str) -> Optional[LocalJobEffect]:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT job_id, execution_incarnation, name, state, evidence FROM local_job_effects " "WHERE job_id=? AND execution_incarnation=? AND name=?",
                    (job_id, execution_incarnation, name),
                ).fetchone()
            return LocalJobEffect(str(row["job_id"]), str(row["execution_incarnation"]), str(row["name"]), str(row["state"]), str(row["evidence"])) if row is not None else None
        except (OSError, sqlite3.Error):
            return None

    def get_effect_strict(self, job_id: str, execution_incarnation: str, name: str) -> Optional[LocalJobEffect]:
        """Read an effect while distinguishing confirmed absence from failure."""
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT job_id, execution_incarnation, name, state, evidence FROM local_job_effects " "WHERE job_id=? AND execution_incarnation=? AND name=?",
                    (job_id, execution_incarnation, name),
                ).fetchone()
            return LocalJobEffect(str(row["job_id"]), str(row["execution_incarnation"]), str(row["name"]), str(row["state"]), str(row["evidence"])) if row is not None else None
        except (OSError, sqlite3.Error) as exc:
            raise RuntimeError("local job effect lookup failed") from exc

    def claim_effect_intent_if_absent(self, claim: LocalJobClaim, name: str, evidence: str) -> EffectIntentClaim:
        """Atomically fence the first authorization for an external effect.

        Existing evidence is returned unchanged.  In particular, pending and
        indeterminate attempts are never refreshed into permission to resend.
        """
        if not claim.acquired or not name:
            raise RuntimeError("invalid effect intent claim")
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    "SELECT 1 FROM local_jobs WHERE job_id=? AND execution_incarnation=? AND state IN (?, ?)",
                    (claim.record.job_id, claim.record.execution_incarnation, LocalJobState.RESULT_RECORDED.value, LocalJobState.DOWNSTREAM_EFFECTS_PENDING.value),
                ).fetchone()
                if current is None:
                    connection.rollback()
                    raise RuntimeError("job execution no longer owns downstream effects")
                row = connection.execute(
                    "SELECT job_id, execution_incarnation, name, state, evidence FROM local_job_effects WHERE job_id=? AND execution_incarnation=? AND name=?",
                    (claim.record.job_id, claim.record.execution_incarnation, name),
                ).fetchone()
                inserted = row is None
                if inserted:
                    connection.execute(
                        "INSERT INTO local_job_effects VALUES (?, ?, ?, ?, ?, ?)",
                        (claim.record.job_id, claim.record.execution_incarnation, name, "pending", evidence, time.time()),
                    )
                    row = connection.execute(
                        "SELECT job_id, execution_incarnation, name, state, evidence FROM local_job_effects WHERE job_id=? AND execution_incarnation=? AND name=?",
                        (claim.record.job_id, claim.record.execution_incarnation, name),
                    ).fetchone()
                connection.commit()
            if row is None:
                raise RuntimeError("effect intent was not retained")
            return EffectIntentClaim(
                LocalJobEffect(str(row["job_id"]), str(row["execution_incarnation"]), str(row["name"]), str(row["state"]), str(row["evidence"])),
                inserted,
            )
        except (OSError, sqlite3.Error) as exc:
            raise RuntimeError("local job effect intent claim failed") from exc

    def settle(self, claim: LocalJobClaim, diagnostic: str = "") -> bool:
        return self._transition(
            claim,
            LocalJobState.DOWNSTREAM_EFFECTS_PENDING,
            LocalJobState.SETTLED,
            "diagnostic=?",
            (diagnostic,),
        )

    @staticmethod
    def _continuation(row: sqlite3.Row) -> LocalJobContinuation:
        return LocalJobContinuation(
            job_id=str(row["job_id"]),
            execution_incarnation=str(row["execution_incarnation"]),
            executor_token=str(row["executor_token"]),
            provider_session=str(row["provider_session"]),
            state=ContinuationState(str(row["state"])),
            frontier=int(row["frontier"]),
            candidate_frontier=int(row["candidate_frontier"]) if row["candidate_frontier"] is not None else None,
            candidate_output=str(row["candidate_output"]),
            final_output=str(row["final_output"]),
            provider_position=str(row["provider_position"]),
            deadline_at=float(row["deadline_at"]) if row["deadline_at"] is not None else None,
            reconciliation_reason=str(row["reconciliation_reason"]),
            provider_workspace=str(row["provider_workspace"]),
            writer_owner=str(row["writer_owner"]),
        )

    def bind_continuation(
        self,
        claim: LocalJobClaim,
        provider_session: str,
        executor_token: str,
        *,
        deadline_at: Optional[float] = None,
    ) -> Optional[LocalJobContinuation]:
        """Bind a provider session once to the exact accepted execution and workspace."""
        if not claim.acquired or not provider_session or not executor_token:
            return None
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                job = connection.execute(
                    "SELECT state, execution_incarnation, workspace_path FROM local_jobs WHERE job_id=?",
                    (claim.record.job_id,),
                ).fetchone()
                if job is None or str(job["state"]) != LocalJobState.RUNNING.value or str(job["execution_incarnation"]) != claim.record.execution_incarnation or not str(job["workspace_path"]):
                    connection.rollback()
                    return None
                connection.execute(
                    "INSERT OR IGNORE INTO local_job_continuations " "(job_id, execution_incarnation, executor_token, provider_session, state, deadline_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (claim.record.job_id, claim.record.execution_incarnation, executor_token, provider_session, ContinuationState.FOREGROUND.value, deadline_at, time.time()),
                )
                row = connection.execute("SELECT * FROM local_job_continuations WHERE job_id=?", (claim.record.job_id,)).fetchone()
                connection.commit()
            if row is None or str(row["provider_session"]) != provider_session or str(row["execution_incarnation"]) != claim.record.execution_incarnation:
                return None
            return self._continuation(row)
        except (OSError, sqlite3.Error, ValueError):
            return None

    def bind_continuation_writer(self, job_id: str, executor_token: str, workspace: Path, writer_owner: str, *, deadline_at: Optional[float] = None) -> bool:
        """Retain the actual provider root and process lifetime for explicit resumption."""
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE local_job_continuations SET provider_workspace=?, writer_owner=?, deadline_at=COALESCE(?, deadline_at) WHERE job_id=? AND executor_token=?",
                (str(workspace.resolve()), writer_owner, deadline_at, job_id, executor_token),
            )
        return cursor.rowcount == 1

    def claim_explicit_resume(self, job: LocalJobRecord, continuation: LocalJobContinuation, runner_owner: str, executor_token: str) -> bool:
        """Fence one operator continuation without changing the attempt or workspace."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE local_jobs SET runner_owner=? WHERE job_id=? AND execution_incarnation=? AND runner_owner=? AND state=? AND provider_entered=1 "
                "AND EXISTS (SELECT 1 FROM local_job_continuations c WHERE c.job_id=local_jobs.job_id AND c.execution_incarnation=local_jobs.execution_incarnation AND c.executor_token=? AND c.state<>?)",
                (runner_owner, job.job_id, job.execution_incarnation, job.runner_owner, LocalJobState.RUNNING.value, continuation.executor_token, ContinuationState.FINAL_CONFIRMED.value),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return False
            connection.execute(
                "UPDATE local_job_continuations SET executor_token=?, state=?, frontier=frontier+1, candidate_frontier=NULL, candidate_output='', final_output='', reconciliation_reason='', updated_at=? WHERE job_id=?",
                (executor_token, ContinuationState.FOREGROUND.value, time.time(), job.job_id),
            )
            connection.commit()
        return True

    def get_continuation(self, job_id: str) -> Optional[LocalJobContinuation]:
        try:
            with self._connect() as connection:
                row = connection.execute("SELECT * FROM local_job_continuations WHERE job_id=?", (job_id,)).fetchone()
            return self._continuation(row) if row is not None else None
        except (OSError, sqlite3.Error, ValueError):
            return None

    def persist_continuation_observation(self, job_id: str, executor_token: str, payload: str) -> bool:
        """Persist provider-specific normalized evidence under the executor fence."""
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "UPDATE local_job_continuations AS c SET observation_json=?, updated_at=? " "WHERE job_id=? AND executor_token=? AND EXISTS (SELECT 1 FROM local_jobs j WHERE j.job_id=c.job_id " "AND j.execution_incarnation=c.execution_incarnation AND j.state=?)",
                    (payload, time.time(), job_id, executor_token, LocalJobState.RUNNING.value),
                )
            return cursor.rowcount == 1
        except (OSError, sqlite3.Error):
            return False

    def get_continuation_observation(self, job_id: str) -> str:
        try:
            with self._connect() as connection:
                row = connection.execute("SELECT observation_json FROM local_job_continuations WHERE job_id=?", (job_id,)).fetchone()
            return str(row["observation_json"]) if row is not None else ""
        except (OSError, sqlite3.Error):
            return ""

    def discover_pending_continuations(self) -> tuple[LocalJobContinuation, ...]:
        """Discover continuation work independently of ordinary intake eligibility."""
        terminal = (ContinuationState.FINAL_CONFIRMED.value, ContinuationState.INTERRUPTED.value, ContinuationState.FAILED.value)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM local_job_continuations WHERE state NOT IN (?, ?, ?) ORDER BY updated_at", terminal).fetchall()
        return tuple(self._continuation(row) for row in rows)

    def transition_continuation(
        self,
        job_id: str,
        executor_token: str,
        expected: ContinuationState,
        target: ContinuationState,
        *,
        provider_position: str = "",
    ) -> bool:
        """Apply an idempotent, executor-fenced progress transition."""
        if target is ContinuationState.FINAL_CONFIRMED:
            return False
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "UPDATE local_job_continuations AS c SET state=?, provider_position=CASE WHEN ?='' THEN provider_position ELSE ? END, updated_at=? "
                    "WHERE job_id=? AND executor_token=? AND state=? AND EXISTS (SELECT 1 FROM local_jobs j WHERE j.job_id=c.job_id "
                    "AND j.execution_incarnation=c.execution_incarnation AND j.state=?)",
                    (target.value, provider_position, provider_position, time.time(), job_id, executor_token, expected.value, LocalJobState.RUNNING.value),
                )
                if cursor.rowcount == 1:
                    return True
                row = connection.execute(
                    "SELECT c.state, c.executor_token, c.provider_position FROM local_job_continuations c " "JOIN local_jobs j ON j.job_id=c.job_id AND j.execution_incarnation=c.execution_incarnation " "WHERE c.job_id=? AND j.state=?",
                    (job_id, LocalJobState.RUNNING.value),
                ).fetchone()
            return bool(row is not None and str(row["executor_token"]) == executor_token and str(row["state"]) == target.value and (not provider_position or str(row["provider_position"]) == provider_position))
        except (OSError, sqlite3.Error):
            return False

    def take_over_continuation(self, job_id: str, expected_executor_token: str, new_executor_token: str, *, expected_runner_owner: str) -> bool:
        """Explicitly replace recovery ownership; elapsed time grants no authority."""
        if not expected_executor_token or not new_executor_token or expected_executor_token == new_executor_token:
            return False
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE local_job_continuations AS c SET executor_token=?, state=?, updated_at=? "
                "WHERE job_id=? AND executor_token=? AND state NOT IN (?, ?, ?) "
                "AND EXISTS (SELECT 1 FROM local_jobs j WHERE j.job_id=c.job_id "
                "AND j.execution_incarnation=c.execution_incarnation AND j.state=? AND j.runner_owner=?)",
                (
                    new_executor_token,
                    ContinuationState.RECONCILIATION_REQUIRED.value,
                    time.time(),
                    job_id,
                    expected_executor_token,
                    ContinuationState.FINAL_CONFIRMED.value,
                    ContinuationState.INTERRUPTED.value,
                    ContinuationState.FAILED.value,
                    LocalJobState.RUNNING.value,
                    expected_runner_owner,
                ),
            )
        return cursor.rowcount == 1

    def retain_reconciliation_reason(self, job_id: str, executor_token: str, reason: str) -> bool:
        """Retain why recovery cannot currently authorize provider activity."""
        if not reason:
            return False
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "UPDATE local_job_continuations AS c SET state=?, reconciliation_reason=?, updated_at=? "
                    "WHERE job_id=? AND executor_token=? AND state NOT IN (?, ?, ?) "
                    "AND EXISTS (SELECT 1 FROM local_jobs j WHERE j.job_id=c.job_id "
                    "AND j.execution_incarnation=c.execution_incarnation AND j.state=?)",
                    (
                        ContinuationState.RECONCILIATION_REQUIRED.value,
                        reason[:2000],
                        time.time(),
                        job_id,
                        executor_token,
                        ContinuationState.FINAL_CONFIRMED.value,
                        ContinuationState.INTERRUPTED.value,
                        ContinuationState.FAILED.value,
                        LocalJobState.RUNNING.value,
                    ),
                )
            return cursor.rowcount == 1
        except (OSError, sqlite3.Error):
            return False

    def observe_continuation_task(
        self,
        job_id: str,
        executor_token: str,
        task_id: str,
        lifecycle: str,
        *,
        terminal_outcome: str = "",
        result_reference: str = "",
        provider_position: str = "",
    ) -> Optional[ContinuationTask]:
        """Retain monotonic task evidence; unavailable inventory is not a task update."""
        if not task_id or lifecycle not in {"running", "terminal"} or (lifecycle == "terminal" and not terminal_outcome):
            return None
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                continuation = connection.execute(
                    "SELECT c.frontier FROM local_job_continuations c JOIN local_jobs j ON j.job_id=c.job_id " "AND j.execution_incarnation=c.execution_incarnation WHERE c.job_id=? AND c.executor_token=? AND j.state=?",
                    (job_id, executor_token, LocalJobState.RUNNING.value),
                ).fetchone()
                if continuation is None:
                    connection.rollback()
                    return None
                old = connection.execute("SELECT * FROM local_continuation_tasks WHERE job_id=? AND task_id=?", (job_id, task_id)).fetchone()
                if old is not None and str(old["lifecycle"]) == lifecycle and str(old["terminal_outcome"]) == terminal_outcome and str(old["result_reference"]) == result_reference and str(old["provider_position"]) == provider_position:
                    connection.commit()
                    return ContinuationTask(
                        job_id,
                        task_id,
                        lifecycle,
                        terminal_outcome,
                        result_reference,
                        provider_position,
                        int(old["sequence"]),
                    )
                if old is not None and str(old["lifecycle"]) == "terminal" and lifecycle != "terminal":
                    connection.commit()
                    return ContinuationTask(job_id, task_id, "terminal", str(old["terminal_outcome"]), str(old["result_reference"]), str(old["provider_position"]), int(old["sequence"]))
                if old is not None and self._provider_position_is_older(provider_position, str(old["provider_position"])):
                    connection.commit()
                    return ContinuationTask(
                        job_id,
                        task_id,
                        str(old["lifecycle"]),
                        str(old["terminal_outcome"]),
                        str(old["result_reference"]),
                        str(old["provider_position"]),
                        int(old["sequence"]),
                    )
                sequence = int(continuation["frontier"]) + 1
                connection.execute(
                    "INSERT INTO local_continuation_tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(job_id, task_id) DO UPDATE SET lifecycle=excluded.lifecycle, terminal_outcome=excluded.terminal_outcome, "
                    "result_reference=excluded.result_reference, provider_position=excluded.provider_position, sequence=excluded.sequence, updated_at=excluded.updated_at",
                    (job_id, task_id, lifecycle, terminal_outcome, result_reference, provider_position, sequence, time.time()),
                )
                connection.execute(
                    "UPDATE local_job_continuations SET frontier=?, provider_position=?, updated_at=? WHERE job_id=? AND executor_token=?",
                    (sequence, provider_position, time.time(), job_id, executor_token),
                )
                connection.commit()
            return ContinuationTask(job_id, task_id, lifecycle, terminal_outcome, result_reference, provider_position, sequence)
        except (OSError, sqlite3.Error):
            return None

    @staticmethod
    def _provider_position_is_older(candidate: str, accepted: str) -> bool:
        """Compare provider sequence positions when both expose an integer order."""
        if not candidate or not accepted or not candidate.isdecimal() or not accepted.isdecimal():
            return False
        return int(candidate) <= int(accepted)

    def record_candidate_output(self, job_id: str, executor_token: str, frontier: int, output: str) -> bool:
        if not output:
            return False
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE local_job_continuations AS c SET candidate_frontier=?, candidate_output=?, updated_at=? "
                "WHERE job_id=? AND executor_token=? AND frontier=? AND state NOT IN (?, ?, ?) "
                "AND EXISTS (SELECT 1 FROM local_jobs j WHERE j.job_id=c.job_id "
                "AND j.execution_incarnation=c.execution_incarnation AND j.state=?)",
                (frontier, output, time.time(), job_id, executor_token, frontier, ContinuationState.FINAL_CONFIRMED.value, ContinuationState.INTERRUPTED.value, ContinuationState.FAILED.value, LocalJobState.RUNNING.value),
            )
        return cursor.rowcount == 1

    def confirm_final_output(self, job_id: str, executor_token: str, frontier: int) -> bool:
        """Confirm only output validated against the still-current settled frontier."""
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                unresolved = connection.execute("SELECT 1 FROM local_continuation_tasks WHERE job_id=? AND lifecycle!='terminal' LIMIT 1", (job_id,)).fetchone()
                if unresolved is not None:
                    connection.rollback()
                    return False
                cursor = connection.execute(
                    "UPDATE local_job_continuations AS c SET state=?, final_output=candidate_output, updated_at=? "
                    "WHERE job_id=? AND executor_token=? AND frontier=? AND candidate_frontier=? AND candidate_output<>'' "
                    "AND EXISTS (SELECT 1 FROM local_jobs j WHERE j.job_id=c.job_id "
                    "AND j.execution_incarnation=c.execution_incarnation AND j.state=?)",
                    (ContinuationState.FINAL_CONFIRMED.value, time.time(), job_id, executor_token, frontier, frontier, LocalJobState.RUNNING.value),
                )
                connection.commit()
            return cursor.rowcount == 1
        except (OSError, sqlite3.Error):
            return False

    def unresolved_continuation_tasks(self, job_id: str) -> tuple[ContinuationTask, ...]:
        """Read pending evidence without interpreting a missing live inventory."""
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM local_continuation_tasks WHERE job_id=? AND lifecycle!='terminal' ORDER BY task_id", (job_id,)).fetchall()
        return tuple(ContinuationTask(job_id, str(row["task_id"]), str(row["lifecycle"]), str(row["terminal_outcome"]), str(row["result_reference"]), str(row["provider_position"]), int(row["sequence"])) for row in rows)

    def confirm_retained_task_result(self, job_id: str, executor_token: str, frontier: int, output: str, terminals: tuple[ContinuationTask, ...]) -> bool:
        """Atomically reconcile proven predecessor terminals and their later result."""
        if not output or not terminals or any(task.job_id != job_id or task.lifecycle != "terminal" or not task.terminal_outcome or not task.provider_position for task in terminals):
            return False
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT 1 FROM local_job_continuations c JOIN local_jobs j ON j.job_id=c.job_id AND j.execution_incarnation=c.execution_incarnation "
                "WHERE c.job_id=? AND c.executor_token=? AND c.frontier=? AND c.candidate_frontier=? AND c.candidate_output=? AND c.state NOT IN (?, ?, ?) AND j.state=?",
                (job_id, executor_token, frontier, frontier, output, ContinuationState.FINAL_CONFIRMED.value, ContinuationState.INTERRUPTED.value, ContinuationState.FAILED.value, LocalJobState.RUNNING.value),
            ).fetchone()
            pending = connection.execute("SELECT task_id, sequence FROM local_continuation_tasks WHERE job_id=? AND lifecycle!='terminal'", (job_id,)).fetchall()
            expected = {(task.task_id, task.sequence) for task in terminals}
            if current is None or len(expected) != len(terminals) or expected != {(str(row["task_id"]), int(row["sequence"])) for row in pending}:
                connection.rollback()
                return False
            for index, task in enumerate(terminals, start=1):
                connection.execute(
                    "UPDATE local_continuation_tasks SET lifecycle='terminal', terminal_outcome=?, result_reference=?, provider_position=?, sequence=?, updated_at=? WHERE job_id=? AND task_id=?",
                    (task.terminal_outcome, task.result_reference, task.provider_position, frontier + index, time.time(), job_id, task.task_id),
                )
            connection.execute(
                "UPDATE local_job_continuations SET state=?, frontier=?, candidate_frontier=?, final_output=candidate_output, reconciliation_reason='', updated_at=? WHERE job_id=?", (ContinuationState.FINAL_CONFIRMED.value, frontier + len(terminals), frontier + len(terminals), time.time(), job_id)
            )
            connection.commit()
        return True

    def create_continuation_intent(
        self,
        job_id: str,
        executor_token: str,
        causal_frontier: int,
        payload: str,
        *,
        command_kind: str = "controller",
        provider_turn: str = "",
    ) -> Optional[ContinuationIntent]:
        """Create or replay the stable command for one exact causal result batch."""
        if not payload or command_kind not in {"controller", "native"}:
            return None
        payload_identity = hashlib.sha256(payload.encode()).hexdigest()
        intent_id = hashlib.sha256(json.dumps([job_id, causal_frontier, command_kind, payload_identity], separators=(",", ":")).encode()).hexdigest()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                continuation = connection.execute(
                    "SELECT c.provider_session, c.frontier FROM local_job_continuations c " "JOIN local_jobs j ON j.job_id=c.job_id AND j.execution_incarnation=c.execution_incarnation " "WHERE c.job_id=? AND c.executor_token=? AND j.state=?",
                    (job_id, executor_token, LocalJobState.RUNNING.value),
                ).fetchone()
                if continuation is None or int(continuation["frontier"]) != causal_frontier:
                    connection.rollback()
                    return None
                existing = connection.execute(
                    "SELECT * FROM local_continuation_intents WHERE job_id=? AND provider_session=? AND causal_frontier=? AND command_kind=?",
                    (job_id, str(continuation["provider_session"]), causal_frontier, command_kind),
                ).fetchone()
                if existing is not None:
                    connection.commit()
                    return self._intent(existing) if str(existing["payload_identity"]) == payload_identity else None
                connection.execute(
                    "INSERT INTO local_continuation_intents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (intent_id, job_id, str(continuation["provider_session"]), causal_frontier, command_kind, payload_identity, provider_turn, ContinuationIntentState.NOT_SUBMITTED.value, time.time()),
                )
                row = connection.execute("SELECT * FROM local_continuation_intents WHERE intent_id=?", (intent_id,)).fetchone()
                connection.commit()
            return self._intent(row) if row is not None else None
        except (OSError, sqlite3.Error, ValueError):
            return None

    @staticmethod
    def _intent(row: sqlite3.Row) -> ContinuationIntent:
        return ContinuationIntent(str(row["intent_id"]), str(row["job_id"]), str(row["provider_session"]), int(row["causal_frontier"]), str(row["command_kind"]), str(row["payload_identity"]), str(row["provider_turn"]), ContinuationIntentState(str(row["state"])))

    def transition_continuation_intent(self, intent_id: str, executor_token: str, expected: ContinuationIntentState, target: ContinuationIntentState, *, provider_turn: str = "") -> bool:
        """Fence command acknowledgement to the continuation's current executor."""
        allowed = {
            ContinuationIntentState.NOT_SUBMITTED: {ContinuationIntentState.ADMITTED, ContinuationIntentState.INDETERMINATE},
            ContinuationIntentState.ADMITTED: {ContinuationIntentState.TERMINAL},
            ContinuationIntentState.INDETERMINATE: {ContinuationIntentState.TERMINAL},
        }
        if target not in allowed.get(expected, set()):
            return False
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                intent = connection.execute(
                    "SELECT i.job_id, i.causal_frontier FROM local_continuation_intents i "
                    "JOIN local_job_continuations c ON c.job_id=i.job_id "
                    "JOIN local_jobs j ON j.job_id=c.job_id AND j.execution_incarnation=c.execution_incarnation "
                    "WHERE i.intent_id=? AND i.state=? AND c.executor_token=? AND j.state=?",
                    (intent_id, expected.value, executor_token, LocalJobState.RUNNING.value),
                ).fetchone()
                if intent is None:
                    connection.rollback()
                    return False
                cursor = connection.execute(
                    "UPDATE local_continuation_intents SET state=?, provider_turn=CASE WHEN ?='' THEN provider_turn ELSE ? END, updated_at=? " "WHERE intent_id=? AND state=?",
                    (target.value, provider_turn, provider_turn, time.time(), intent_id, expected.value),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    return False
                if target is ContinuationIntentState.ADMITTED:
                    advanced = connection.execute(
                        "UPDATE local_job_continuations SET frontier=frontier+1, provider_position=CASE WHEN ?='' THEN provider_position ELSE ? END, " "updated_at=? WHERE job_id=? AND executor_token=? AND frontier=?",
                        (provider_turn, provider_turn, time.time(), str(intent["job_id"]), executor_token, int(intent["causal_frontier"])),
                    )
                    if advanced.rowcount != 1:
                        connection.rollback()
                        return False
                connection.commit()
            return True
        except (OSError, sqlite3.Error):
            return False

    def _transition(
        self,
        claim: LocalJobClaim,
        expected: LocalJobState,
        target: LocalJobState,
        extra_sql: str = "",
        extra_values: tuple[str, ...] = (),
    ) -> bool:
        assignments = "state=?, updated_at=?" + (f", {extra_sql}" if extra_sql else "")
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    f"UPDATE local_jobs SET {assignments} WHERE job_id=? AND state=? AND execution_incarnation=?",  # nosec B608: fixed internal fragments
                    (target.value, time.time(), *extra_values, claim.record.job_id, expected.value, claim.record.execution_incarnation),
                )
            return cursor.rowcount == 1
        except (OSError, sqlite3.Error):
            return False
