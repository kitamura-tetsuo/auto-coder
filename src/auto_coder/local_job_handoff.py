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
from .local_review_repair import LocalReviewRepairRequest, LocalReviewRepairStore


class LocalJobKind(str, Enum):
    ISSUE_IMPLEMENTATION = "issue_implementation"
    PR_REVIEW_CORRECTION = "pr_review_correction"


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


@dataclass(frozen=True)
class LocalJobOffer:
    kind: LocalJobKind
    repository: str
    target_number: int
    upstream_attempt: str
    backend_name: str
    invocation_input: str

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
    workspace_path: str = ""
    source_commit: str = ""
    source_ref: str = ""
    work_branch: str = ""


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
                updated_at REAL NOT NULL, workspace_path TEXT NOT NULL DEFAULT '',
                source_commit TEXT NOT NULL DEFAULT '', source_ref TEXT NOT NULL DEFAULT '',
                work_branch TEXT NOT NULL DEFAULT '',
                UNIQUE(kind, repository, target_number, upstream_attempt))"""
            )
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(local_jobs)")}
            for name in ("workspace_path", "source_commit", "source_ref", "work_branch"):
                if name not in columns:
                    connection.execute(f"ALTER TABLE local_jobs ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")  # nosec B608: fixed names
            connection.execute(
                """CREATE TABLE IF NOT EXISTS local_job_results (
                artifact_id TEXT PRIMARY KEY, job_id TEXT NOT NULL,
                execution_incarnation TEXT NOT NULL, outcome TEXT NOT NULL,
                output TEXT NOT NULL, created_at REAL NOT NULL,
                UNIQUE(job_id, execution_incarnation))"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS local_job_effects (
                job_id TEXT NOT NULL, execution_incarnation TEXT NOT NULL,
                name TEXT NOT NULL, state TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL,
                PRIMARY KEY(job_id, execution_incarnation, name))"""
            )

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
            workspace_path=str(row["workspace_path"]),
            source_commit=str(row["source_commit"]),
            source_ref=str(row["source_ref"]),
            work_branch=str(row["work_branch"]),
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
            "INSERT INTO local_jobs (job_id, kind, repository, target_number, upstream_attempt, "
            "upstream_incarnation, backend_name, input_identity, invocation_input, state, "
            "execution_incarnation, invocation_outcome, result_reference, diagnostic, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', NULL, '', '', ?, ?)",
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
                connection.commit()
            return self.get(offer.job_id)
        except (OSError, sqlite3.Error, ValueError):
            return None

    def get(self, job_id: str) -> Optional[LocalJobRecord]:
        try:
            with self._connect() as connection:
                row = connection.execute("SELECT * FROM local_jobs WHERE job_id=?", (job_id,)).fetchone()
            return self._record(row) if row is not None else None
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

    def claim(self, job_id: str) -> Optional[LocalJobClaim]:
        incarnation = str(uuid.uuid4())
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "UPDATE local_jobs SET state=?, execution_incarnation=?, updated_at=? WHERE job_id=? AND state=? AND execution_incarnation=''",
                    (LocalJobState.RUNNING.value, incarnation, time.time(), job_id, LocalJobState.PENDING.value),
                )
                row = connection.execute("SELECT * FROM local_jobs WHERE job_id=?", (job_id,)).fetchone()
                connection.commit()
            return LocalJobClaim(self._record(row), cursor.rowcount == 1) if row is not None else None
        except (OSError, sqlite3.Error, ValueError):
            return None

    def persist_result_artifact(self, claim: LocalJobClaim, outcome: InvocationOutcome, output: str) -> Optional[LocalJobResultArtifact]:
        """Durably bind invocation output to the exact job and incarnation."""
        if not claim.acquired or not output:
            return None
        artifact_id = hashlib.sha256(json.dumps([claim.record.job_id, claim.record.execution_incarnation, outcome.value, output], separators=(",", ":")).encode()).hexdigest()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    "SELECT 1 FROM local_jobs WHERE job_id=? AND state=? AND execution_incarnation=?",
                    (claim.record.job_id, LocalJobState.RUNNING.value, claim.record.execution_incarnation),
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

    def bind_workspace(self, claim: LocalJobClaim, *, workspace_path: Path, source_commit: str, source_ref: str, work_branch: str) -> bool:
        """Fence a prepared full-job workspace to its exact running incarnation."""
        if not claim.acquired or not workspace_path.is_absolute() or not source_commit or not source_ref or not work_branch:
            return False
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "UPDATE local_jobs SET workspace_path=?, source_commit=?, source_ref=?, work_branch=?, updated_at=? " "WHERE job_id=? AND state=? AND execution_incarnation=? AND workspace_path=''",
                    (str(workspace_path), source_commit, source_ref, work_branch, time.time(), claim.record.job_id, LocalJobState.RUNNING.value, claim.record.execution_incarnation),
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

    def record_result(self, claim: LocalJobClaim, outcome: InvocationOutcome, result_reference: str, diagnostic: str = "") -> bool:
        """Checkpoint a result only from a verified exact-job artifact."""
        if not claim.acquired or not result_reference:
            return False
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                evidence = connection.execute(
                    "SELECT 1 FROM local_job_results WHERE artifact_id=? AND job_id=? " "AND execution_incarnation=? AND outcome=?",
                    (result_reference, claim.record.job_id, claim.record.execution_incarnation, outcome.value),
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

    def mark_downstream_pending(self, claim: LocalJobClaim) -> bool:
        return self._transition(claim, LocalJobState.RESULT_RECORDED, LocalJobState.DOWNSTREAM_EFFECTS_PENDING)

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

    def settle(self, claim: LocalJobClaim, diagnostic: str = "") -> bool:
        return self._transition(
            claim,
            LocalJobState.DOWNSTREAM_EFFECTS_PENDING,
            LocalJobState.SETTLED,
            "diagnostic=?",
            (diagnostic,),
        )

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
