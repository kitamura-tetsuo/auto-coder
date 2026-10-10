"""Durable, attempt-bound envelopes for accepted local model jobs.

This module is deliberately an evidence producer.  It does not start a worker,
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

from .durable_repair_allowance import RepairAllowanceLedger
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
    upstream_incarnation: str = ""

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


@dataclass(frozen=True)
class LocalJobClaim:
    record: LocalJobRecord
    acquired: bool


def default_local_job_db_path() -> Path:
    return Path.home() / ".auto-coder" / "local_jobs.sqlite3"


class LocalJobStore:
    """SQLite envelope with transactional offer and incarnation-fenced writes."""

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
                updated_at REAL NOT NULL,
                UNIQUE(kind, repository, target_number, upstream_attempt))"""
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
        )

    def offer_issue(self, offer: LocalJobOffer, identity: IssueAttemptIdentity, guard: IssueDispatchGuard) -> Optional[LocalJobRecord]:
        """Persist an Issue job only while its exact dispatch claim is current."""
        if offer.kind is not LocalJobKind.ISSUE_IMPLEMENTATION or offer.repository != identity.full_repository_name or offer.target_number != identity.issue_number or offer.upstream_attempt != identity.implementation_attempt_id:
            return None
        ownership = guard.inspect_pending_claim(identity)
        if ownership is None or ownership.outcome is not DispatchOutcome.INDETERMINATE or not ownership.claim_incarnation or ownership.backend_name != offer.backend_name:
            return None
        return self._offer(offer, ownership.claim_incarnation)

    def offer_pr_correction(
        self,
        offer: LocalJobOffer,
        request: LocalReviewRepairRequest,
        repair_store: LocalReviewRepairStore,
        allowance_ledger: RepairAllowanceLedger,
    ) -> Optional[LocalJobRecord]:
        """Persist a PR job only for the exact repair claim and allowance generation."""
        if offer.kind is not LocalJobKind.PR_REVIEW_CORRECTION or offer.repository != request.repository or offer.target_number != request.pr_number or offer.upstream_attempt != request.attempt_id:
            return None
        try:
            repair = repair_store.get(request)
            allowance = allowance_ledger.get_snapshot("https://api.github.com", request.repository, request.pr_number)
        except Exception:
            # Upstream authorities own their state and diagnostics.  A local
            # job offer must fail closed without attempting to repair, erase,
            # or reinterpret either database.
            return None
        generation = allowance.get_outstanding_generation()
        if repair is None or repair.phase != "executing" or generation is None or generation.owning_identity != "local-review-repair" or generation.bundle_reference != request.attempt_id:
            return None
        return self._offer(offer, f"{repair.incarnation}:{generation.generation_id}")

    def _offer(self, offer: LocalJobOffer, upstream_incarnation: str) -> Optional[LocalJobRecord]:
        """Commit first, then reread the exact record before reporting acceptance."""
        now = time.time()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM local_jobs WHERE kind=? AND repository=? AND target_number=? AND upstream_attempt=?",
                    (offer.kind.value, offer.repository, offer.target_number, offer.upstream_attempt),
                ).fetchone()
                if existing is not None:
                    record = self._record(existing)
                    connection.commit()
                    if record.job_id != offer.job_id or record.upstream_incarnation != upstream_incarnation:
                        return None
                    return record
                connection.execute(
                    "INSERT INTO local_jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', NULL, '', '', ?, ?)",
                    (offer.job_id, offer.kind.value, offer.repository, offer.target_number, offer.upstream_attempt, upstream_incarnation, offer.backend_name, offer.input_identity, offer.invocation_input, LocalJobState.PENDING.value, now, now),
                )
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

    def record_result(self, claim: LocalJobClaim, outcome: InvocationOutcome, result_reference: str, diagnostic: str = "") -> bool:
        """Checkpoint an actual invocation result; success requires evidence."""
        if not claim.acquired or not result_reference:
            return False
        return self._transition(
            claim,
            LocalJobState.RUNNING,
            LocalJobState.RESULT_RECORDED,
            "invocation_outcome=?, result_reference=?, diagnostic=?",
            (outcome.value, result_reference, diagnostic),
        )

    def mark_downstream_pending(self, claim: LocalJobClaim) -> bool:
        return self._transition(claim, LocalJobState.RESULT_RECORDED, LocalJobState.DOWNSTREAM_EFFECTS_PENDING)

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
