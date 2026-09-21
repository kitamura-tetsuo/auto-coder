"""Provider-neutral, durable admission for one logical Issue implementation attempt.

The guard deliberately stops at the adapter boundary: provider adapters classify
their observations into :class:`DispatchOutcome`; this module only makes that
classification durable and prevents another local or remote implementation from
starting while ownership is uncertain.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Optional

from .cloud_manager import CloudManager, CloudTaskBinding
from .cloud_run import CloudRun, CloudRunRepository
from .logger_config import get_logger

logger = get_logger(__name__)


class DispatchOutcome(str, Enum):
    """Machine-readable outcomes at the implementation dispatch boundary."""

    NOT_STARTED = "not_started"
    LOCAL_COMPLETED = "local_completed"
    REMOTE_ACCEPTED = "remote_accepted"
    INDETERMINATE = "indeterminate"
    FAILED = "failed"
    DEFERRED = "deferred"


@dataclass(frozen=True)
class IssueAttemptIdentity:
    """Stable logical identity shared by every candidate for an Issue attempt."""

    repository_owner: str
    repository_name: str
    issue_number: int
    implementation_attempt_id: str

    def __post_init__(self) -> None:
        if not self.repository_owner or not self.repository_name:
            raise ValueError("Repository owner and name are required")
        if self.issue_number <= 0:
            raise ValueError("Issue number must be positive")
        if not self.implementation_attempt_id:
            raise ValueError("Implementation attempt identifier is required")

    @property
    def full_repository_name(self) -> str:
        return f"{self.repository_owner}/{self.repository_name}"


@dataclass(frozen=True)
class CandidateHandoff:
    """Configuration and provider selected for a candidate handoff."""

    backend_name: str
    provider: str


@dataclass(frozen=True)
class AdapterOutcome:
    """An adapter's classified observation of its one implementation call."""

    outcome: DispatchOutcome
    provider_reference: str = ""
    diagnostic: str = ""


@dataclass(frozen=True)
class DispatchResult:
    """Complete, machine-readable result returned to dispatch consumers."""

    identity: IssueAttemptIdentity
    outcome: DispatchOutcome
    backend_name: str = ""
    provider: str = ""
    provider_reference: str = ""
    diagnostic: str = ""
    tracking_complete: bool = True
    claim_incarnation: str = ""
    admitted: bool = False


@dataclass(frozen=True)
class LegacyIssueOwnership:
    """Attempt-unassociated ownership retained from ``cloud.csv``."""

    repository_owner: str
    repository_name: str
    issue_number: int
    backend_name: str
    provider: str
    provider_reference: str
    diagnostic: str


def default_issue_dispatch_db_path() -> Path:
    return Path.home() / ".auto-coder" / "issue_dispatch_handoffs.sqlite3"


class IssueDispatchGuard:
    """Fail-closed durable authority for local and remote Issue handoffs."""

    _process_lock = threading.Lock()

    def __init__(
        self,
        db_path: Optional[Path] = None,
        cloud_run_repository_factory: Callable[[str], CloudRunRepository] = CloudRunRepository,
        cloud_manager_factory: Callable[[str], CloudManager] = CloudManager,
    ) -> None:
        self._db_path = Path(db_path) if db_path is not None else default_issue_dispatch_db_path()
        self._cloud_run_repository_factory = cloud_run_repository_factory
        self._cloud_manager_factory = cloud_manager_factory

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self._db_path), timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        self._ensure_schema(connection)
        return connection

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS issue_dispatch_handoffs (
                repository_owner TEXT NOT NULL,
                repository_name TEXT NOT NULL,
                issue_number INTEGER NOT NULL,
                attempt_id TEXT NOT NULL,
                incarnation TEXT NOT NULL,
                state TEXT NOT NULL,
                backend_name TEXT NOT NULL,
                provider TEXT NOT NULL,
                provider_reference TEXT NOT NULL,
                diagnostic TEXT NOT NULL,
                tracking_complete INTEGER NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (repository_owner, repository_name, issue_number, attempt_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS legacy_issue_dispatch_ownership (
                repository_owner TEXT NOT NULL,
                repository_name TEXT NOT NULL,
                issue_number INTEGER NOT NULL,
                provider TEXT NOT NULL,
                backend_name TEXT NOT NULL,
                provider_reference TEXT NOT NULL,
                diagnostic TEXT NOT NULL,
                observed_at REAL NOT NULL,
                PRIMARY KEY (repository_owner, repository_name, issue_number)
            )
            """
        )

    @staticmethod
    def _key(identity: IssueAttemptIdentity) -> tuple[object, ...]:
        return (
            identity.repository_owner,
            identity.repository_name,
            identity.issue_number,
            identity.implementation_attempt_id,
        )

    @staticmethod
    def _result_from_row(identity: IssueAttemptIdentity, row: sqlite3.Row, diagnostic: str = "") -> DispatchResult:
        stored_state = str(row["state"])
        outcome = DispatchOutcome.INDETERMINATE if stored_state == "pending" else DispatchOutcome(stored_state)
        return DispatchResult(
            identity=identity,
            outcome=outcome,
            backend_name=str(row["backend_name"]),
            provider=str(row["provider"]),
            provider_reference=str(row["provider_reference"]),
            diagnostic=diagnostic or str(row["diagnostic"]),
            tracking_complete=bool(row["tracking_complete"]),
            claim_incarnation=str(row["incarnation"]),
        )

    def _legacy_evidence(self, identity: IssueAttemptIdentity) -> tuple[Optional[CloudRun], Optional[CloudTaskBinding], str]:
        """Read old production writers strictly; any ambiguity remains suppressing."""
        repository = self._cloud_run_repository_factory(identity.full_repository_name)
        manager = self._cloud_manager_factory(identity.full_repository_name)
        runs = repository.list_for_issue(identity.issue_number)
        bindings = manager.read_bindings_strict()
        binding = bindings.get(str(identity.issue_number))
        exact_runs = [run for run in runs if str(run.attempt) == identity.implementation_attempt_id]
        if len(exact_runs) > 1:
            return None, binding, "multiple legacy runs match the logical attempt"
        run = exact_runs[0] if exact_runs else None
        if run is not None and binding is not None:
            if binding.task_id != run.task_id or (binding.provider and binding.provider != run.provider):
                return run, binding, "legacy CloudRun and provider binding contradict each other"
        if run is None and binding is not None:
            return None, binding, "legacy provider binding has no unambiguous attempt association"
        return run, binding, ""

    def _materialize_legacy(
        self,
        connection: sqlite3.Connection,
        identity: IssueAttemptIdentity,
        allow_unassociated_binding: bool = False,
    ) -> Optional[DispatchResult]:
        run, binding, conflict = self._legacy_evidence(identity)
        if run is None and binding is None and not conflict:
            return None
        now = time.time()
        provider = run.provider if run is not None else (binding.provider if binding is not None else "")
        backend = run.backend_name if run is not None else (binding.backend_name if binding is not None else "")
        reference = run.task_id if run is not None else (binding.task_id if binding is not None else "")
        if run is None and binding is not None:
            connection.execute(
                "INSERT OR IGNORE INTO legacy_issue_dispatch_ownership VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    identity.repository_owner,
                    identity.repository_name,
                    identity.issue_number,
                    provider,
                    backend,
                    reference,
                    conflict,
                    now,
                ),
            )
            if allow_unassociated_binding:
                return None
            return DispatchResult(
                identity,
                DispatchOutcome.DEFERRED,
                backend,
                provider,
                reference,
                conflict,
                tracking_complete=False,
            )
        if conflict:
            state = DispatchOutcome.DEFERRED.value
            diagnostic = conflict
        elif run is not None and run.task_id and run.submission_outcome == "accepted":
            state = DispatchOutcome.REMOTE_ACCEPTED.value
            diagnostic = "imported legacy CloudRun ownership"
        else:
            state = DispatchOutcome.INDETERMINATE.value
            diagnostic = "imported unresolved legacy CloudRun ownership"
        # A migrated legacy record is a newly acquired claim. Its incarnation
        # must therefore never be derived only from the logical attempt: after
        # a confirmed release, unchanged legacy evidence may be observed and
        # acquired again, and a deterministic value would give that successor
        # the released predecessor's authority token.
        incarnation = f"legacy-{uuid.uuid4()}"
        connection.execute(
            "INSERT INTO issue_dispatch_handoffs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (*self._key(identity), incarnation, state, backend, provider, reference, diagnostic, 1, now, now),
        )
        row = connection.execute(
            "SELECT * FROM issue_dispatch_handoffs WHERE repository_owner=? AND repository_name=? AND issue_number=? AND attempt_id=?",
            self._key(identity),
        ).fetchone()
        return self._result_from_row(identity, row)

    def inspect(self, identity: IssueAttemptIdentity) -> Optional[DispatchResult]:
        """Return current ownership, importing legacy evidence before reporting empty."""
        try:
            with self._process_lock, self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM issue_dispatch_handoffs WHERE repository_owner=? AND repository_name=? AND issue_number=? AND attempt_id=?",
                    self._key(identity),
                ).fetchone()
                result = self._result_from_row(identity, row) if row is not None else self._materialize_legacy(connection, identity)
                connection.execute("COMMIT")
                return result
        except Exception as exc:
            logger.error(f"Issue dispatch ownership read failed for {identity}: {exc}")
            return DispatchResult(identity, DispatchOutcome.DEFERRED, diagnostic=f"ownership read failed: {exc}", tracking_complete=False)

    def get_legacy_issue_ownership(self, repository_owner: str, repository_name: str, issue_number: int) -> Optional[LegacyIssueOwnership]:
        """Return preserved attempt-unassociated legacy ownership evidence."""
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM legacy_issue_dispatch_ownership WHERE repository_owner=? AND repository_name=? AND issue_number=?",
                    (repository_owner, repository_name, issue_number),
                ).fetchone()
            if row is None:
                return None
            return LegacyIssueOwnership(
                repository_owner,
                repository_name,
                issue_number,
                str(row["backend_name"]),
                str(row["provider"]),
                str(row["provider_reference"]),
                str(row["diagnostic"]),
            )
        except Exception as exc:
            logger.error(f"Legacy Issue ownership read failed for {repository_owner}/{repository_name}#{issue_number}: {exc}")
            return None

    def reserve(
        self,
        identity: IssueAttemptIdentity,
        candidate: CandidateHandoff,
        *,
        authorize_new_attempt: bool = False,
    ) -> DispatchResult:
        """Atomically acquire a new incarnation, or expose the suppressing owner."""
        try:
            with self._process_lock, self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM issue_dispatch_handoffs WHERE repository_owner=? AND repository_name=? AND issue_number=? AND attempt_id=?",
                    self._key(identity),
                ).fetchone()
                if row is not None:
                    connection.execute("COMMIT")
                    return self._result_from_row(identity, row, "logical attempt already has suppressing ownership")
                legacy = self._materialize_legacy(connection, identity, allow_unassociated_binding=authorize_new_attempt)
                if legacy is not None:
                    connection.execute("COMMIT")
                    return legacy
                incarnation = str(uuid.uuid4())
                now = time.time()
                connection.execute(
                    "INSERT INTO issue_dispatch_handoffs VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, '', '', 0, ?, ?)",
                    (*self._key(identity), incarnation, candidate.backend_name, candidate.provider, now, now),
                )
                connection.execute("COMMIT")
                return DispatchResult(
                    identity,
                    DispatchOutcome.INDETERMINATE,
                    candidate.backend_name,
                    candidate.provider,
                    diagnostic="handoff reserved; adapter outcome pending",
                    tracking_complete=False,
                    claim_incarnation=incarnation,
                    admitted=True,
                )
        except Exception as exc:
            logger.error(f"Issue dispatch reservation failed for {identity}: {exc}")
            return DispatchResult(identity, DispatchOutcome.DEFERRED, candidate.backend_name, candidate.provider, diagnostic=f"reservation failed: {exc}", tracking_complete=False)

    def finalize(self, claim: DispatchResult, observation: AdapterOutcome) -> DispatchResult:
        """Finalize only the current incarnation; NOT_STARTED durably releases it."""
        allowed = {
            DispatchOutcome.NOT_STARTED,
            DispatchOutcome.LOCAL_COMPLETED,
            DispatchOutcome.REMOTE_ACCEPTED,
            DispatchOutcome.INDETERMINATE,
            DispatchOutcome.FAILED,
            DispatchOutcome.DEFERRED,
        }
        if observation.outcome not in allowed:
            return replace(claim, outcome=DispatchOutcome.DEFERRED, diagnostic="unsupported adapter outcome", tracking_complete=False, admitted=False)
        if observation.outcome == DispatchOutcome.REMOTE_ACCEPTED and not observation.provider_reference:
            observation = AdapterOutcome(DispatchOutcome.INDETERMINATE, diagnostic="provider acceptance lacked a task/session reference")
        try:
            with self._process_lock, self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM issue_dispatch_handoffs WHERE repository_owner=? AND repository_name=? AND issue_number=? AND attempt_id=?",
                    self._key(claim.identity),
                ).fetchone()
                if row is None or str(row["incarnation"]) != claim.claim_incarnation:
                    connection.execute("COMMIT")
                    return replace(claim, outcome=DispatchOutcome.DEFERRED, diagnostic="claim incarnation is stale", tracking_complete=False, admitted=False)
                current_reference = str(row["provider_reference"])
                if current_reference and observation.provider_reference and current_reference != observation.provider_reference:
                    connection.execute("COMMIT")
                    return replace(
                        self._result_from_row(claim.identity, row),
                        outcome=DispatchOutcome.DEFERRED,
                        diagnostic="contradictory provider task/session reference",
                        tracking_complete=False,
                    )
                if observation.outcome == DispatchOutcome.NOT_STARTED:
                    cursor = connection.execute(
                        "DELETE FROM issue_dispatch_handoffs WHERE repository_owner=? AND repository_name=? AND issue_number=? AND attempt_id=? AND incarnation=?",
                        (*self._key(claim.identity), claim.claim_incarnation),
                    )
                    if cursor.rowcount != 1:
                        raise OSError("confirmed release did not remove the current claim")
                    connection.execute("COMMIT")
                    return replace(
                        claim,
                        outcome=DispatchOutcome.NOT_STARTED,
                        diagnostic=observation.diagnostic,
                        tracking_complete=True,
                        admitted=False,
                    )
                connection.execute(
                    "UPDATE issue_dispatch_handoffs SET state=?, provider_reference=?, diagnostic=?, tracking_complete=1, updated_at=? WHERE repository_owner=? AND repository_name=? AND issue_number=? AND attempt_id=? AND incarnation=?",
                    (
                        observation.outcome.value,
                        observation.provider_reference or current_reference,
                        observation.diagnostic,
                        time.time(),
                        *self._key(claim.identity),
                        claim.claim_incarnation,
                    ),
                )
                connection.execute("COMMIT")
                return replace(
                    claim,
                    outcome=observation.outcome,
                    provider_reference=observation.provider_reference or current_reference,
                    diagnostic=observation.diagnostic,
                    tracking_complete=True,
                    admitted=False,
                )
        except Exception as exc:
            logger.error(f"Issue dispatch finalization failed for {claim.identity}: {exc}")
            # The reserved claim remains suppressing (or its durable disposition is
            # unknown), so callers must never interpret this as safe fallback.
            return replace(claim, outcome=DispatchOutcome.DEFERRED, diagnostic=f"ownership finalization failed: {exc}", tracking_complete=False, admitted=False)

    def _mark_tracking_incomplete(self, result: DispatchResult, diagnostic: str) -> DispatchResult:
        """Retain accepted ownership while durably recording projection failure."""
        try:
            with self._process_lock, self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "UPDATE issue_dispatch_handoffs SET tracking_complete=0, diagnostic=?, updated_at=? " "WHERE repository_owner=? AND repository_name=? AND issue_number=? AND attempt_id=? " "AND incarnation=? AND state=? AND provider_reference=?",
                    (
                        diagnostic,
                        time.time(),
                        *self._key(result.identity),
                        result.claim_incarnation,
                        DispatchOutcome.REMOTE_ACCEPTED.value,
                        result.provider_reference,
                    ),
                )
                if cursor.rowcount != 1:
                    raise OSError("accepted ownership changed before tracking update")
                connection.execute("COMMIT")
            return replace(result, diagnostic=diagnostic, tracking_complete=False)
        except Exception as exc:
            return replace(
                result,
                diagnostic=f"{diagnostic}; tracking-state persistence failed: {exc}",
                tracking_complete=False,
            )

    def dispatch_remote(
        self,
        identity: IssueAttemptIdentity,
        candidate: CandidateHandoff,
        submit: Callable[[], AdapterOutcome],
        publish_tracking: Optional[Callable[[DispatchResult], bool]] = None,
    ) -> DispatchResult:
        """Reserve, invoke one remote adapter callback, and persist its outcome."""
        claim = self.reserve(identity, candidate)
        if not claim.admitted:
            return claim
        try:
            observation = submit()
        except Exception as exc:
            observation = AdapterOutcome(DispatchOutcome.INDETERMINATE, diagnostic=f"submission raised after reservation: {exc}")
        result = self.finalize(claim, observation)
        if result.outcome == DispatchOutcome.REMOTE_ACCEPTED and publish_tracking is not None:
            try:
                tracking_complete = bool(publish_tracking(result))
            except Exception as exc:
                return self._mark_tracking_incomplete(result, f"{result.diagnostic}; secondary tracking failed: {exc}".strip("; "))
            if not tracking_complete:
                return self._mark_tracking_incomplete(result, f"{result.diagnostic}; secondary tracking incomplete".strip("; "))
        return result

    def dispatch_candidates(
        self,
        identity: IssueAttemptIdentity,
        candidates: Iterable[CandidateHandoff],
        invoke: Callable[[CandidateHandoff], AdapterOutcome],
        *,
        authorize_new_attempt: bool = False,
    ) -> DispatchResult:
        """Invoke one ranked candidate sequence through the shared claim boundary.

        Candidate mode is deliberately irrelevant here.  Both synchronous local
        invocations and asynchronous remote submissions acquire the same logical
        Issue-attempt claim before their real execution boundary.  An adapter may
        permit fallback only by returning ``NOT_STARTED``; every other observation
        is suppressing and ends this pass.
        """
        seen: set[tuple[str, str]] = set()
        last: Optional[DispatchResult] = None
        for candidate in candidates:
            key = (candidate.backend_name, candidate.provider)
            if key in seen:
                continue
            seen.add(key)
            claim = self.reserve(
                identity,
                candidate,
                authorize_new_attempt=authorize_new_attempt,
            )
            # New-attempt authority belongs to the pass, not an individual
            # fallback.  It is safe to present on each reservation because only
            # a confirmed NOT_STARTED result can have released the predecessor.
            if not claim.admitted:
                return claim
            try:
                observation = invoke(candidate)
            except Exception as exc:
                observation = AdapterOutcome(
                    DispatchOutcome.INDETERMINATE,
                    diagnostic=f"adapter raised after admission: {exc}",
                )
            last = self.finalize(claim, observation)
            if last.outcome != DispatchOutcome.NOT_STARTED:
                return last

        if last is not None:
            return replace(
                last,
                outcome=DispatchOutcome.DEFERRED,
                backend_name="",
                provider="",
                diagnostic="all ranked candidates were confirmed not started",
                tracking_complete=True,
                claim_incarnation="",
                admitted=False,
            )
        return DispatchResult(
            identity,
            DispatchOutcome.DEFERRED,
            diagnostic="no ranked dispatch candidates were supplied",
        )
