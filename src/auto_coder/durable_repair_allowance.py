"""Durable repair allowance state machine for canonical PR blockers.

Implements the specification and requirements from GitHub Issue #2140:
model bounded automatic repair allowances using durable corrective
generations rather than review or commit counts (Stage of the convergent
PR review tracking family #2134, consuming canonical blocker IDs persisted
by ``canonical_pr_blocker_ledger.py`` / GitHub Issue #2135).

This module owns only the provider-independent durable state machine and
allowance policy. It does not itself observe production providers, send
repair requests, resolve GitHub threads, or authorize merges; those belong
to later stages (see docs/client-features for the current wiring).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional, Sequence

from .logger_config import get_logger
from .util.github_request_outcome import normalize_api_origin

logger = get_logger(__name__)

SUPPORTED_SCHEMA_VERSION = 1
DEFAULT_REPAIR_ALLOWANCE_LIMIT = 3


# ---------------------------------------------------------------------------
# Enums and Domain Dataclasses
# ---------------------------------------------------------------------------


class RepairAllowanceStatus(str, Enum):
    """Status of a single blocker's repair allowance."""

    ALLOWABLE = "ALLOWABLE"
    EXHAUSTED = "EXHAUSTED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class GenerationLifecycleState(str, Enum):
    """Distinct lifecycle representations of one corrective generation (REQ-003)."""

    RESERVED = "RESERVED"
    CONFIRMED_DELIVERED = "CONFIRMED_DELIVERED"
    INDETERMINATE = "INDETERMINATE"
    PENDING_COMPLETION = "PENDING_COMPLETION"
    PENDING_REVALIDATION = "PENDING_REVALIDATION"
    SETTLED = "SETTLED"
    SUPERSEDED = "SUPERSEDED"


OUTSTANDING_LIFECYCLE_STATES = frozenset(
    {
        GenerationLifecycleState.RESERVED,
        GenerationLifecycleState.CONFIRMED_DELIVERED,
        GenerationLifecycleState.INDETERMINATE,
        GenerationLifecycleState.PENDING_COMPLETION,
        GenerationLifecycleState.PENDING_REVALIDATION,
    }
)
TERMINAL_LIFECYCLE_STATES = frozenset({GenerationLifecycleState.SETTLED, GenerationLifecycleState.SUPERSEDED})


class DeliveryOutcome(str, Enum):
    """Observed outcome of one delivery attempt for a reserved generation."""

    CONFIRMED = "CONFIRMED"
    INDETERMINATE = "INDETERMINATE"
    DEFINITE_NON_DELIVERY = "DEFINITE_NON_DELIVERY"


class CompletionAvailability(str, Enum):
    """Observable availability of completion evidence for a generation."""

    KNOWN = "KNOWN"
    UNAVAILABLE = "UNAVAILABLE"


class ValidationAvailability(str, Enum):
    """Observable availability of validation evidence for a covered blocker."""

    KNOWN = "KNOWN"
    UNAVAILABLE = "UNAVAILABLE"
    INCONCLUSIVE = "INCONCLUSIVE"


class BlockerSettlement(str, Enum):
    """Per-blocker settlement disposition for one corrective generation."""

    PENDING = "PENDING"
    CORRECTED = "CORRECTED"
    STILL_OPEN = "STILL_OPEN"


@dataclass(frozen=True)
class CorrectiveGenerationBundle:
    """Immutable input describing one corrective generation admission request."""

    bundle_reference: str = ""
    covered_blocker_ids: tuple[str, ...] = ()
    scope_revision: str = ""
    requirement_manifest_revision: str = ""
    owning_identity: str = ""
    observed_baseline: str = ""


@dataclass(frozen=True)
class DeliveryAttemptRecord:
    """One recorded delivery attempt for a generation (transport retries are not new generations)."""

    attempt_id: str = ""
    outcome: DeliveryOutcome = DeliveryOutcome.INDETERMINATE
    delivery_operation_identity: str = ""
    evidence: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class BlockerSettlementRecord:
    """Per-blocker settlement outcome recorded for one corrective generation."""

    blocker_id: str = ""
    settlement: BlockerSettlement = BlockerSettlement.PENDING
    charged: bool = False
    validation_seq: Optional[int] = None
    evidence: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class PendingValidationRecord:
    """One raw validation observation recorded against a generation/blocker pair."""

    blocker_id: str = ""
    still_unmet: bool = True
    availability: ValidationAvailability = ValidationAvailability.KNOWN
    validation_seq: int = 0
    evidence: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class GenerationSnapshot:
    """Immutable/read-only snapshot of one corrective generation."""

    generation_id: str = ""
    bundle_reference: str = ""
    covered_blocker_ids: tuple[str, ...] = ()
    scope_revision: str = ""
    requirement_manifest_revision: str = ""
    owning_identity: str = ""
    admission_epoch: int = 0
    delivery_operation_identity: str = ""
    observed_baseline: str = ""
    lifecycle_state: GenerationLifecycleState = GenerationLifecycleState.RESERVED
    completion_seq: Optional[int] = None
    completion_code_changed: Optional[bool] = None
    superseded_reason: Optional[str] = None
    delivery_attempts: tuple[DeliveryAttemptRecord, ...] = ()
    settlements: tuple[BlockerSettlementRecord, ...] = ()
    pending_validations: tuple[PendingValidationRecord, ...] = ()
    created_at: str = ""
    updated_at: str = ""

    def is_outstanding(self) -> bool:
        """Whether this generation still occupies the single-outstanding-generation slot."""
        return self.lifecycle_state in OUTSTANDING_LIFECYCLE_STATES

    def get_settlement(self, blocker_id: str) -> Optional[BlockerSettlementRecord]:
        for settlement in self.settlements:
            if settlement.blocker_id == blocker_id:
                return settlement
        return None


@dataclass(frozen=True)
class OperatorGrantRecord:
    """Durable record of one accepted explicit operator-grant transition (REQ-009)."""

    request_id: str = ""
    target_blocker_ids: tuple[str, ...] = ()
    granted_blocker_ids: tuple[str, ...] = ()
    new_limit: int = 0
    created_at: str = ""


@dataclass(frozen=True)
class BlockerAllowanceSnapshot:
    """Immutable/read-only snapshot of one blocker's repair allowance."""

    blocker_id: str = ""
    limit: int = 0
    failed_count: int = 0
    total_failed_count: int = 0
    status: RepairAllowanceStatus = RepairAllowanceStatus.ALLOWABLE
    exhaustion_reason: Optional[str] = None
    historical_unknown: bool = False
    created_at: str = ""
    updated_at: str = ""

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.failed_count)


@dataclass(frozen=True)
class RepairAllowanceLedgerSnapshot:
    """Immutable/read-only snapshot of the entire repair-allowance namespace."""

    api_origin: str = ""
    repository: str = ""
    pr_number: int = 0
    epoch: int = 0
    revision_token: str = ""
    blockers: tuple[BlockerAllowanceSnapshot, ...] = ()
    generations: tuple[GenerationSnapshot, ...] = ()
    operator_grants: tuple[OperatorGrantRecord, ...] = ()
    created_at: str = ""
    updated_at: str = ""

    def get_blocker_allowance(self, blocker_id: str) -> Optional[BlockerAllowanceSnapshot]:
        for blocker in self.blockers:
            if blocker.blocker_id == blocker_id:
                return blocker
        return None

    def get_generation(self, generation_id: str) -> Optional[GenerationSnapshot]:
        for generation in self.generations:
            if generation.generation_id == generation_id:
                return generation
        return None

    def get_outstanding_generation(self) -> Optional[GenerationSnapshot]:
        for generation in self.generations:
            if generation.is_outstanding():
                return generation
        return None

    def has_exhausted_open_blocker(self, open_blocker_ids: Sequence[str]) -> bool:
        open_ids = set(open_blocker_ids)
        return any(b.blocker_id in open_ids and b.status == RepairAllowanceStatus.EXHAUSTED for b in self.blockers)


@dataclass(frozen=True)
class AdmissionResult:
    """Outcome of one attempted corrective-generation admission."""

    admitted: bool
    generation_id: Optional[str]
    denial_reason: Optional[str]
    snapshot: RepairAllowanceLedgerSnapshot


@dataclass(frozen=True)
class ValidationObservation:
    """One caller-supplied, pre-normalized validation observation for a covered blocker."""

    blocker_id: str = ""
    still_unmet: bool = True
    availability: ValidationAvailability = ValidationAvailability.KNOWN
    validation_seq: int = 0
    evidence: str = ""


@dataclass(frozen=True)
class OperatorGrantResult:
    """Outcome of one attempted explicit operator-grant transition."""

    granted: bool
    request_id: str = ""
    granted_blocker_ids: tuple[str, ...] = ()
    denial_reason: Optional[str] = None
    snapshot: Optional[RepairAllowanceLedgerSnapshot] = None


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RepairAllowanceError(RuntimeError):
    """Base exception for all durable repair allowance operations."""


class RepairAllowanceUnavailableError(RepairAllowanceError):
    """Storage is unreadable, corrupt, unsupported version, or missing retained state."""


class StaleRepairAllowanceEpochError(RepairAllowanceError):
    """Compare-and-set failed because the current namespace epoch did not match expected."""


class RepairAllowanceIdempotencyConflictError(RepairAllowanceError):
    """The same operation/request ID was reused with a conflicting payload."""


class UnknownGenerationReferenceError(RepairAllowanceError):
    """A referenced generation ID is unknown in this namespace."""


class InvalidGenerationTransitionError(RepairAllowanceError):
    """The requested transition is not valid from the generation's current lifecycle state."""


class RepairAllowancePersistenceError(RepairAllowanceError):
    """A required durable write operation failed."""


class InvalidOperatorGrantError(RepairAllowanceError):
    """An operator grant was rejected: stale epoch, conflicting request ID, or outstanding work."""


# ---------------------------------------------------------------------------
# Storage & Ledger Implementation
# ---------------------------------------------------------------------------


def default_repair_allowance_db_path() -> Path:
    return Path.home() / ".auto-coder" / "repair_allowance.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_namespace_key(api_origin: str, repository: str, pr_number: int) -> str:
    normalized_origin = normalize_api_origin(api_origin)
    return f"{normalized_origin}::{repository}::{pr_number}"


class RepairAllowanceLedger:
    """Durable, transactional, restart-surviving repair-allowance state machine.

    Backed by SQLite in WAL mode with compare-and-set optimistic locking,
    idempotent operation journal, and fail-closed persistence guarantees,
    mirroring ``canonical_pr_blocker_ledger.CanonicalPRBlockerLedger``.
    """

    _lock = threading.RLock()

    def __init__(self, db_path: Optional[Path] = None):
        self._db_path = Path(db_path) if db_path is not None else default_repair_allowance_db_path()
        self._simulate_failure_before_commit: bool = False

    # -- storage plumbing ---------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            conn = sqlite3.connect(str(self._db_path), timeout=30.0, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._ensure_schema(conn)
            return conn
        except sqlite3.DatabaseError as exc:
            raise RepairAllowanceUnavailableError(f"Repair allowance database is corrupt or unreadable: {exc}") from exc
        except OSError as exc:
            raise RepairAllowanceUnavailableError(f"Repair allowance database cannot be accessed: {exc}") from exc

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            cursor = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'")
            row = cursor.fetchone()
            if row is None:
                conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?)", (str(SUPPORTED_SCHEMA_VERSION),))
            else:
                try:
                    version = int(row[0])
                except ValueError as exc:
                    raise RepairAllowanceUnavailableError(f"Invalid schema version in repair allowance metadata: {row[0]}") from exc
                if version > SUPPORTED_SCHEMA_VERSION:
                    raise RepairAllowanceUnavailableError(f"Unsupported schema version {version} (max supported: {SUPPORTED_SCHEMA_VERSION})")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS namespaces (
                    namespace_key TEXT PRIMARY KEY,
                    api_origin TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    pr_number INTEGER NOT NULL,
                    epoch INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS blocker_allowances (
                    namespace_key TEXT NOT NULL,
                    blocker_id TEXT NOT NULL,
                    active_limit INTEGER NOT NULL,
                    total_failed_count INTEGER NOT NULL DEFAULT 0,
                    grant_baseline INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    exhaustion_reason TEXT,
                    historical_unknown INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (namespace_key, blocker_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS generations (
                    generation_id TEXT PRIMARY KEY,
                    namespace_key TEXT NOT NULL,
                    bundle_reference TEXT NOT NULL,
                    covered_blocker_ids_json TEXT NOT NULL,
                    scope_revision TEXT NOT NULL,
                    requirement_manifest_revision TEXT NOT NULL,
                    owning_identity TEXT NOT NULL,
                    admission_epoch INTEGER NOT NULL,
                    delivery_operation_identity TEXT NOT NULL DEFAULT '',
                    observed_baseline TEXT NOT NULL,
                    lifecycle_state TEXT NOT NULL,
                    completion_seq INTEGER,
                    completion_code_changed INTEGER,
                    superseded_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS generation_delivery_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    generation_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    delivery_operation_identity TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS generation_blocker_settlements (
                    generation_id TEXT NOT NULL,
                    blocker_id TEXT NOT NULL,
                    settlement TEXT NOT NULL,
                    charged INTEGER NOT NULL DEFAULT 0,
                    validation_seq INTEGER,
                    evidence TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (generation_id, blocker_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS generation_pending_validations (
                    id TEXT PRIMARY KEY,
                    generation_id TEXT NOT NULL,
                    blocker_id TEXT NOT NULL,
                    still_unmet INTEGER NOT NULL,
                    availability TEXT NOT NULL,
                    validation_seq INTEGER NOT NULL,
                    evidence TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS operator_grants (
                    request_id TEXT PRIMARY KEY,
                    namespace_key TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    target_blocker_ids_json TEXT NOT NULL,
                    granted_blocker_ids_json TEXT NOT NULL,
                    new_limit INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS operation_journal (
                    operation_id TEXT PRIMARY KEY,
                    namespace_key TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    committed_epoch INTEGER NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
        except sqlite3.DatabaseError as exc:
            raise RepairAllowanceUnavailableError(f"Repair allowance schema verification failed: {exc}") from exc

    def _check_db_integrity(self) -> None:
        if not self._db_path.exists():
            return
        try:
            with sqlite3.connect(str(self._db_path), timeout=5.0) as test_conn:
                cursor = test_conn.execute("PRAGMA quick_check")
                row = cursor.fetchone()
                if row is None or row[0] != "ok":
                    raise RepairAllowanceUnavailableError(f"Repair allowance quick_check failed: {row}")
        except sqlite3.DatabaseError as exc:
            raise RepairAllowanceUnavailableError(f"Repair allowance database corrupt or unreadable: {exc}") from exc

    def _hash_payload(self, payload_dict: dict) -> str:
        canonical_json = json.dumps(payload_dict, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    def _check_idempotency(self, conn: sqlite3.Connection, operation_id: str, payload_hash: str) -> Optional[str]:
        """Return the committed result_json if operation_id was already committed with a matching payload."""
        cursor = conn.execute("SELECT payload_hash, result_json FROM operation_journal WHERE operation_id = ?", (operation_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        committed_hash, result_json = row
        if committed_hash != payload_hash:
            raise RepairAllowanceIdempotencyConflictError(f"Conflicting reuse of operation ID {operation_id!r} with a different payload")
        return result_json

    def _check_cas(self, conn: sqlite3.Connection, key: str, expected_epoch: int) -> int:
        cursor = conn.execute("SELECT epoch FROM namespaces WHERE namespace_key = ?", (key,))
        row = cursor.fetchone()
        if row is None:
            raise RepairAllowanceUnavailableError(f"Namespace {key} does not exist for CAS mutation")
        current_epoch = row[0]
        if current_epoch != expected_epoch:
            raise StaleRepairAllowanceEpochError(f"Contention: expected namespace epoch {expected_epoch}, but current epoch is {current_epoch}")
        return current_epoch

    def _journal(self, conn: sqlite3.Connection, operation_id: str, key: str, payload_hash: str, committed_epoch: int, result: dict, now: str) -> None:
        conn.execute(
            "INSERT INTO operation_journal (operation_id, namespace_key, payload_hash, committed_epoch, result_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (operation_id, key, payload_hash, committed_epoch, json.dumps(result), now),
        )

    # -- namespace lifecycle --------------------------------------------------

    def initialize_namespace(self, api_origin: str, repository: str, pr_number: int) -> RepairAllowanceLedgerSnapshot:
        """Explicitly initialize an empty namespace with epoch 1."""
        self._check_db_integrity()
        norm_origin = normalize_api_origin(api_origin)
        key = _make_namespace_key(norm_origin, repository, pr_number)
        now = _now_iso()

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute("SELECT epoch FROM namespaces WHERE namespace_key = ?", (key,))
                if cursor.fetchone() is None:
                    conn.execute(
                        "INSERT INTO namespaces (namespace_key, api_origin, repository, pr_number, epoch, created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?)",
                        (key, norm_origin, repository, pr_number, now, now),
                    )
                if self._simulate_failure_before_commit:
                    raise RepairAllowancePersistenceError("Simulated persistence failure during namespace initialization")
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                conn.close()

        return self.get_snapshot(norm_origin, repository, pr_number, require_retained_state=True)

    def get_snapshot(self, api_origin: str, repository: str, pr_number: int, require_retained_state: bool = True) -> RepairAllowanceLedgerSnapshot:
        """Return an immutable read-only snapshot for the namespace.

        Raises RepairAllowanceUnavailableError if the database is corrupt,
        unreadable, unsupported version, or if retained state is missing.
        """
        self._check_db_integrity()
        norm_origin = normalize_api_origin(api_origin)
        key = _make_namespace_key(norm_origin, repository, pr_number)

        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute("SELECT epoch, created_at, updated_at FROM namespaces WHERE namespace_key = ?", (key,))
                ns_row = cursor.fetchone()
                if ns_row is None:
                    if require_retained_state:
                        raise RepairAllowanceUnavailableError(f"Required retained state is missing for namespace {key}")
                    raise RepairAllowanceUnavailableError(f"Namespace {key} is uninitialized")
                epoch, ns_created, ns_updated = ns_row

                cursor = conn.execute(
                    "SELECT blocker_id, active_limit, total_failed_count, grant_baseline, status, exhaustion_reason, historical_unknown, created_at, updated_at " "FROM blocker_allowances WHERE namespace_key = ? ORDER BY created_at ASC, blocker_id ASC",
                    (key,),
                )
                blockers = []
                known_blocker_ids: set[str] = set()
                for bid, limit, total_failed, baseline, status, ex_reason, hist_unknown, b_created, b_updated in cursor.fetchall():
                    known_blocker_ids.add(bid)
                    blockers.append(
                        BlockerAllowanceSnapshot(
                            blocker_id=bid,
                            limit=limit,
                            failed_count=max(0, total_failed - baseline),
                            total_failed_count=total_failed,
                            status=RepairAllowanceStatus(status),
                            exhaustion_reason=ex_reason,
                            historical_unknown=bool(hist_unknown),
                            created_at=b_created,
                            updated_at=b_updated,
                        )
                    )

                cursor = conn.execute(
                    "SELECT generation_id, bundle_reference, covered_blocker_ids_json, scope_revision, requirement_manifest_revision, "
                    "owning_identity, admission_epoch, delivery_operation_identity, observed_baseline, lifecycle_state, "
                    "completion_seq, completion_code_changed, superseded_reason, created_at, updated_at "
                    "FROM generations WHERE namespace_key = ? ORDER BY admission_epoch ASC, generation_id ASC",
                    (key,),
                )
                gen_rows = cursor.fetchall()

                cursor = conn.execute(
                    "SELECT attempt_id, generation_id, outcome, delivery_operation_identity, evidence, created_at " "FROM generation_delivery_attempts WHERE generation_id IN (SELECT generation_id FROM generations WHERE namespace_key = ?) " "ORDER BY created_at ASC, attempt_id ASC",
                    (key,),
                )
                attempts_by_generation: dict[str, list[DeliveryAttemptRecord]] = {}
                for attempt_id, gen_id, outcome, delivery_id, evidence, created_at in cursor.fetchall():
                    attempts_by_generation.setdefault(gen_id, []).append(DeliveryAttemptRecord(attempt_id=attempt_id, outcome=DeliveryOutcome(outcome), delivery_operation_identity=delivery_id, evidence=evidence, created_at=created_at))

                cursor = conn.execute(
                    "SELECT generation_id, blocker_id, settlement, charged, validation_seq, evidence, created_at, updated_at " "FROM generation_blocker_settlements WHERE generation_id IN (SELECT generation_id FROM generations WHERE namespace_key = ?) " "ORDER BY created_at ASC",
                    (key,),
                )
                settlements_by_generation: dict[str, list[BlockerSettlementRecord]] = {}
                for gen_id, bid, settlement, charged, vseq, evidence, created_at, updated_at in cursor.fetchall():
                    settlements_by_generation.setdefault(gen_id, []).append(
                        BlockerSettlementRecord(
                            blocker_id=bid,
                            settlement=BlockerSettlement(settlement),
                            charged=bool(charged),
                            validation_seq=vseq,
                            evidence=evidence,
                            created_at=created_at,
                            updated_at=updated_at,
                        )
                    )

                cursor = conn.execute(
                    "SELECT id, generation_id, blocker_id, still_unmet, availability, validation_seq, evidence, created_at " "FROM generation_pending_validations WHERE generation_id IN (SELECT generation_id FROM generations WHERE namespace_key = ?) " "ORDER BY created_at ASC",
                    (key,),
                )
                pending_by_generation: dict[str, list[PendingValidationRecord]] = {}
                for _id, gen_id, bid, still_unmet, availability, vseq, evidence, created_at in cursor.fetchall():
                    pending_by_generation.setdefault(gen_id, []).append(
                        PendingValidationRecord(
                            blocker_id=bid,
                            still_unmet=bool(still_unmet),
                            availability=ValidationAvailability(availability),
                            validation_seq=vseq,
                            evidence=evidence,
                            created_at=created_at,
                        )
                    )

                generations = []
                referenced_blocker_ids: set[str] = set()
                for (
                    gen_id,
                    bundle_ref,
                    covered_json,
                    scope_rev,
                    manifest_rev,
                    owning_identity,
                    admission_epoch,
                    delivery_id,
                    baseline,
                    lifecycle,
                    completion_seq,
                    completion_changed,
                    superseded_reason,
                    g_created,
                    g_updated,
                ) in gen_rows:
                    try:
                        covered = tuple(json.loads(covered_json))
                    except (json.JSONDecodeError, TypeError):
                        covered = ()
                    referenced_blocker_ids.update(covered)
                    generations.append(
                        GenerationSnapshot(
                            generation_id=gen_id,
                            bundle_reference=bundle_ref,
                            covered_blocker_ids=covered,
                            scope_revision=scope_rev,
                            requirement_manifest_revision=manifest_rev,
                            owning_identity=owning_identity,
                            admission_epoch=admission_epoch,
                            delivery_operation_identity=delivery_id,
                            observed_baseline=baseline,
                            lifecycle_state=GenerationLifecycleState(lifecycle),
                            completion_seq=completion_seq,
                            completion_code_changed=(None if completion_changed is None else bool(completion_changed)),
                            superseded_reason=superseded_reason,
                            delivery_attempts=tuple(attempts_by_generation.get(gen_id, ())),
                            settlements=tuple(settlements_by_generation.get(gen_id, ())),
                            pending_validations=tuple(pending_by_generation.get(gen_id, ())),
                            created_at=g_created,
                            updated_at=g_updated,
                        )
                    )

                # REQ-011: a blocker referenced by generation history but missing its
                # allowance row is ambiguous historical correlation, not a fresh
                # zero-failure allowance. Surface it explicitly rather than silently.
                for orphan_id in sorted(referenced_blocker_ids - known_blocker_ids):
                    blockers.append(
                        BlockerAllowanceSnapshot(
                            blocker_id=orphan_id,
                            limit=0,
                            failed_count=0,
                            total_failed_count=0,
                            status=RepairAllowanceStatus.RECONCILIATION_REQUIRED,
                            exhaustion_reason=None,
                            historical_unknown=True,
                            created_at="",
                            updated_at="",
                        )
                    )

                cursor = conn.execute(
                    "SELECT request_id, target_blocker_ids_json, granted_blocker_ids_json, new_limit, created_at " "FROM operator_grants WHERE namespace_key = ? ORDER BY created_at ASC",
                    (key,),
                )
                grants = []
                for request_id, target_json, granted_json, new_limit, created_at in cursor.fetchall():
                    try:
                        target_ids = tuple(json.loads(target_json))
                    except (json.JSONDecodeError, TypeError):
                        target_ids = ()
                    try:
                        granted_ids = tuple(json.loads(granted_json))
                    except (json.JSONDecodeError, TypeError):
                        granted_ids = ()
                    grants.append(OperatorGrantRecord(request_id=request_id, target_blocker_ids=target_ids, granted_blocker_ids=granted_ids, new_limit=new_limit, created_at=created_at))

                return RepairAllowanceLedgerSnapshot(
                    api_origin=norm_origin,
                    repository=repository,
                    pr_number=pr_number,
                    epoch=epoch,
                    revision_token=f"epoch-{epoch}",
                    blockers=tuple(sorted(blockers, key=lambda b: b.blocker_id)),
                    generations=tuple(generations),
                    operator_grants=tuple(grants),
                    created_at=ns_created,
                    updated_at=ns_updated,
                )
            except RepairAllowanceUnavailableError:
                raise
            except sqlite3.DatabaseError as exc:
                raise RepairAllowanceUnavailableError(f"Repair allowance read error: {exc}") from exc
            finally:
                conn.close()

    # -- internal helpers -------------------------------------------------

    def _ensure_blocker_allowance_row(self, conn: sqlite3.Connection, key: str, blocker_id: str, default_limit: int, now: str) -> None:
        cursor = conn.execute("SELECT 1 FROM blocker_allowances WHERE namespace_key = ? AND blocker_id = ?", (key, blocker_id))
        if cursor.fetchone() is not None:
            return
        conn.execute(
            "INSERT INTO blocker_allowances (namespace_key, blocker_id, active_limit, total_failed_count, grant_baseline, status, exhaustion_reason, historical_unknown, created_at, updated_at) " "VALUES (?, ?, ?, 0, 0, ?, NULL, 0, ?, ?)",
            (key, blocker_id, default_limit, RepairAllowanceStatus.ALLOWABLE.value, now, now),
        )

    def _bump_namespace_epoch(self, conn: sqlite3.Connection, key: str, new_epoch: int, now: str) -> None:
        conn.execute("UPDATE namespaces SET epoch = ?, updated_at = ? WHERE namespace_key = ?", (new_epoch, now, key))

    # -- admission ----------------------------------------------------------

    def admit_generation(
        self,
        api_origin: str,
        repository: str,
        pr_number: int,
        operation_id: str,
        expected_epoch: int,
        bundle: CorrectiveGenerationBundle,
        open_blocker_ids: Sequence[str],
        default_limit_for_new_blockers: int = DEFAULT_REPAIR_ALLOWANCE_LIMIT,
    ) -> AdmissionResult:
        """Admit a new corrective generation, or deny it, under the public transition boundary.

        Denies admission (without raising) when any currently open blocker in
        the PR is EXHAUSTED (REQ-007), or when an outstanding (non-terminal)
        generation already exists for this PR (REQ-010).
        """
        if default_limit_for_new_blockers <= 0:
            raise ValueError("repair allowance limit must be a positive integer")

        self._check_db_integrity()
        norm_origin = normalize_api_origin(api_origin)
        key = _make_namespace_key(norm_origin, repository, pr_number)

        payload_dict = {
            "op": "admit_generation",
            "bundle_reference": bundle.bundle_reference,
            "covered_blocker_ids": sorted(bundle.covered_blocker_ids),
            "scope_revision": bundle.scope_revision,
            "requirement_manifest_revision": bundle.requirement_manifest_revision,
            "owning_identity": bundle.owning_identity,
            "observed_baseline": bundle.observed_baseline,
            "open_blocker_ids": sorted(open_blocker_ids),
            "default_limit": default_limit_for_new_blockers,
        }
        payload_hash = self._hash_payload(payload_dict)
        now = _now_iso()

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")

                cached = self._check_idempotency(conn, operation_id, payload_hash)
                if cached is not None:
                    conn.execute("COMMIT")
                    result = json.loads(cached)
                    snapshot = self.get_snapshot(norm_origin, repository, pr_number, require_retained_state=True)
                    return AdmissionResult(admitted=result["admitted"], generation_id=result.get("generation_id"), denial_reason=result.get("denial_reason"), snapshot=snapshot)

                current_epoch = self._check_cas(conn, key, expected_epoch)

                cursor = conn.execute("SELECT blocker_id, status FROM blocker_allowances WHERE namespace_key = ?", (key,))
                status_by_blocker = {bid: status for bid, status in cursor.fetchall()}
                open_set = set(open_blocker_ids)
                exhausted_open = sorted(bid for bid in open_set if status_by_blocker.get(bid) == RepairAllowanceStatus.EXHAUSTED.value)

                cursor = conn.execute("SELECT generation_id, lifecycle_state FROM generations WHERE namespace_key = ?", (key,))
                outstanding_generations = [gid for gid, lifecycle in cursor.fetchall() if GenerationLifecycleState(lifecycle) in OUTSTANDING_LIFECYCLE_STATES]

                denial_reason: Optional[str] = None
                if exhausted_open:
                    denial_reason = f"PR has exhausted open blocker(s): {', '.join(exhausted_open)}"
                elif outstanding_generations:
                    denial_reason = f"An outstanding corrective generation already exists for this PR: {outstanding_generations[0]}"

                if denial_reason is not None:
                    result_dict = {"admitted": False, "generation_id": None, "denial_reason": denial_reason}
                    self._journal(conn, operation_id, key, payload_hash, current_epoch, result_dict, now)
                    if self._simulate_failure_before_commit:
                        raise RepairAllowancePersistenceError("Simulated write failure before commit")
                    conn.execute("COMMIT")
                    snapshot = self.get_snapshot(norm_origin, repository, pr_number, require_retained_state=True)
                    return AdmissionResult(admitted=False, generation_id=None, denial_reason=denial_reason, snapshot=snapshot)

                generation_id = f"gen_{uuid.uuid4().hex[:12]}"
                new_epoch = current_epoch + 1

                for blocker_id in bundle.covered_blocker_ids:
                    self._ensure_blocker_allowance_row(conn, key, blocker_id, default_limit_for_new_blockers, now)

                conn.execute(
                    "INSERT INTO generations (generation_id, namespace_key, bundle_reference, covered_blocker_ids_json, scope_revision, requirement_manifest_revision, "
                    "owning_identity, admission_epoch, delivery_operation_identity, observed_baseline, lifecycle_state, completion_seq, completion_code_changed, "
                    "superseded_reason, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, NULL, NULL, NULL, ?, ?)",
                    (
                        generation_id,
                        key,
                        bundle.bundle_reference,
                        json.dumps(list(bundle.covered_blocker_ids)),
                        bundle.scope_revision,
                        bundle.requirement_manifest_revision,
                        bundle.owning_identity,
                        new_epoch,
                        bundle.observed_baseline,
                        GenerationLifecycleState.RESERVED.value,
                        now,
                        now,
                    ),
                )

                self._bump_namespace_epoch(conn, key, new_epoch, now)

                result_dict = {"admitted": True, "generation_id": generation_id, "denial_reason": None}
                self._journal(conn, operation_id, key, payload_hash, new_epoch, result_dict, now)

                if self._simulate_failure_before_commit:
                    raise RepairAllowancePersistenceError("Simulated write failure before commit")

                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                conn.close()

        snapshot = self.get_snapshot(norm_origin, repository, pr_number, require_retained_state=True)
        return AdmissionResult(admitted=True, generation_id=generation_id, denial_reason=None, snapshot=snapshot)

    # -- delivery -------------------------------------------------------------

    def record_delivery_outcome(
        self,
        api_origin: str,
        repository: str,
        pr_number: int,
        operation_id: str,
        expected_epoch: int,
        generation_id: str,
        outcome: DeliveryOutcome,
        delivery_operation_identity: str,
        evidence: str = "",
    ) -> RepairAllowanceLedgerSnapshot:
        """Record one delivery attempt outcome for a reserved/indeterminate generation.

        Definite non-delivery keeps the generation RESERVED so the same
        logical generation may retry delivery (REQ-003); it never charges a
        failure and never allocates a new generation.
        """
        self._check_db_integrity()
        norm_origin = normalize_api_origin(api_origin)
        key = _make_namespace_key(norm_origin, repository, pr_number)

        payload_dict = {
            "op": "record_delivery_outcome",
            "generation_id": generation_id,
            "outcome": outcome.value,
            "delivery_operation_identity": delivery_operation_identity,
            "evidence": evidence,
        }
        payload_hash = self._hash_payload(payload_dict)
        now = _now_iso()

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")

                cached = self._check_idempotency(conn, operation_id, payload_hash)
                if cached is not None:
                    conn.execute("COMMIT")
                    return self.get_snapshot(norm_origin, repository, pr_number, require_retained_state=True)

                current_epoch = self._check_cas(conn, key, expected_epoch)

                cursor = conn.execute("SELECT lifecycle_state FROM generations WHERE generation_id = ? AND namespace_key = ?", (generation_id, key))
                row = cursor.fetchone()
                if row is None:
                    raise UnknownGenerationReferenceError(f"Generation {generation_id!r} not found in namespace {key}")
                lifecycle = GenerationLifecycleState(row[0])

                if lifecycle not in (GenerationLifecycleState.RESERVED, GenerationLifecycleState.CONFIRMED_DELIVERED, GenerationLifecycleState.INDETERMINATE):
                    raise InvalidGenerationTransitionError(f"Cannot record a delivery outcome for generation {generation_id!r} in state {lifecycle.value}")

                if outcome == DeliveryOutcome.DEFINITE_NON_DELIVERY and lifecycle != GenerationLifecycleState.RESERVED:
                    raise InvalidGenerationTransitionError(f"Definite non-delivery is only valid while reserved (generation {generation_id!r} is {lifecycle.value})")

                new_epoch = current_epoch + 1
                attempt_id = f"attempt_{uuid.uuid4().hex[:12]}"
                conn.execute(
                    "INSERT INTO generation_delivery_attempts (attempt_id, generation_id, outcome, delivery_operation_identity, evidence, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (attempt_id, generation_id, outcome.value, delivery_operation_identity, evidence, now),
                )

                if outcome == DeliveryOutcome.CONFIRMED:
                    new_lifecycle = GenerationLifecycleState.CONFIRMED_DELIVERED
                    new_delivery_id = delivery_operation_identity
                elif outcome == DeliveryOutcome.INDETERMINATE:
                    new_lifecycle = GenerationLifecycleState.INDETERMINATE
                    new_delivery_id = delivery_operation_identity
                else:  # DEFINITE_NON_DELIVERY
                    new_lifecycle = GenerationLifecycleState.RESERVED
                    new_delivery_id = ""

                conn.execute(
                    "UPDATE generations SET lifecycle_state = ?, delivery_operation_identity = ?, updated_at = ? WHERE generation_id = ?",
                    (new_lifecycle.value, new_delivery_id, now, generation_id),
                )
                self._bump_namespace_epoch(conn, key, new_epoch, now)
                self._journal(conn, operation_id, key, payload_hash, new_epoch, {"ok": True}, now)

                if self._simulate_failure_before_commit:
                    raise RepairAllowancePersistenceError("Simulated write failure before commit")

                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                conn.close()

        return self.get_snapshot(norm_origin, repository, pr_number, require_retained_state=True)

    # -- completion ------------------------------------------------------------

    def record_completion_observation(
        self,
        api_origin: str,
        repository: str,
        pr_number: int,
        operation_id: str,
        expected_epoch: int,
        generation_id: str,
        availability: CompletionAvailability,
        completion_seq: Optional[int] = None,
        code_changed: Optional[bool] = None,
        causally_after_admission: bool = True,
        evidence: str = "",
    ) -> RepairAllowanceLedgerSnapshot:
        """Record a completion observation for a confirmed-delivered generation.

        Unavailable evidence preserves the generation's prior state
        (REQ-003). Evidence that does not causally follow this generation's
        admission (e.g. a completion belonging to the pre-repair baseline)
        cannot advance or settle it (REQ-004, AS-003).
        """
        self._check_db_integrity()
        norm_origin = normalize_api_origin(api_origin)
        key = _make_namespace_key(norm_origin, repository, pr_number)

        payload_dict = {
            "op": "record_completion_observation",
            "generation_id": generation_id,
            "availability": availability.value,
            "completion_seq": completion_seq,
            "code_changed": code_changed,
            "causally_after_admission": causally_after_admission,
            "evidence": evidence,
        }
        payload_hash = self._hash_payload(payload_dict)
        now = _now_iso()

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")

                cached = self._check_idempotency(conn, operation_id, payload_hash)
                if cached is not None:
                    conn.execute("COMMIT")
                    return self.get_snapshot(norm_origin, repository, pr_number, require_retained_state=True)

                current_epoch = self._check_cas(conn, key, expected_epoch)

                cursor = conn.execute("SELECT lifecycle_state, completion_seq FROM generations WHERE generation_id = ? AND namespace_key = ?", (generation_id, key))
                row = cursor.fetchone()
                if row is None:
                    raise UnknownGenerationReferenceError(f"Generation {generation_id!r} not found in namespace {key}")
                lifecycle = GenerationLifecycleState(row[0])

                if lifecycle not in (GenerationLifecycleState.CONFIRMED_DELIVERED, GenerationLifecycleState.PENDING_COMPLETION):
                    raise InvalidGenerationTransitionError(f"Cannot record completion evidence for generation {generation_id!r} in state {lifecycle.value}")

                new_epoch = current_epoch + 1

                if availability == CompletionAvailability.UNAVAILABLE:
                    conn.execute("UPDATE generations SET lifecycle_state = ?, updated_at = ? WHERE generation_id = ?", (GenerationLifecycleState.PENDING_COMPLETION.value, now, generation_id))
                elif not causally_after_admission:
                    # Known evidence that predates this generation's admission cannot
                    # advance or settle it; the prior state is preserved (AS-003).
                    conn.execute("UPDATE generations SET updated_at = ? WHERE generation_id = ?", (now, generation_id))
                else:
                    if completion_seq is None:
                        raise ValueError("completion_seq is required for a causally-valid known completion observation")
                    conn.execute(
                        "UPDATE generations SET lifecycle_state = ?, completion_seq = ?, completion_code_changed = ?, updated_at = ? WHERE generation_id = ?",
                        (GenerationLifecycleState.PENDING_REVALIDATION.value, completion_seq, (None if code_changed is None else int(code_changed)), now, generation_id),
                    )

                self._bump_namespace_epoch(conn, key, new_epoch, now)
                self._journal(conn, operation_id, key, payload_hash, new_epoch, {"ok": True}, now)

                if self._simulate_failure_before_commit:
                    raise RepairAllowancePersistenceError("Simulated write failure before commit")

                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                conn.close()

        return self.get_snapshot(norm_origin, repository, pr_number, require_retained_state=True)

    # -- validation / settlement ------------------------------------------------

    def record_validation_results(
        self,
        api_origin: str,
        repository: str,
        pr_number: int,
        operation_id: str,
        expected_epoch: int,
        generation_id: str,
        validations: Sequence[ValidationObservation],
    ) -> RepairAllowanceLedgerSnapshot:
        """Record independent validation observations and settle covered blockers.

        A blocker is charged at most once per generation, only once a KNOWN
        validation causally at-or-after the generation's completion evidence
        demonstrates it remains unmet (REQ-004..REQ-006). Premature
        validations (before completion evidence) are recorded but do not
        settle anything until a later, causally-bound validation arrives
        (REQ-003, AS-003).
        """
        self._check_db_integrity()
        norm_origin = normalize_api_origin(api_origin)
        key = _make_namespace_key(norm_origin, repository, pr_number)

        payload_dict = {
            "op": "record_validation_results",
            "generation_id": generation_id,
            "validations": [{"blocker_id": v.blocker_id, "still_unmet": v.still_unmet, "availability": v.availability.value, "validation_seq": v.validation_seq, "evidence": v.evidence} for v in validations],
        }
        payload_hash = self._hash_payload(payload_dict)
        now = _now_iso()

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")

                cached = self._check_idempotency(conn, operation_id, payload_hash)
                if cached is not None:
                    conn.execute("COMMIT")
                    return self.get_snapshot(norm_origin, repository, pr_number, require_retained_state=True)

                current_epoch = self._check_cas(conn, key, expected_epoch)

                cursor = conn.execute(
                    "SELECT lifecycle_state, covered_blocker_ids_json, completion_seq FROM generations WHERE generation_id = ? AND namespace_key = ?",
                    (generation_id, key),
                )
                row = cursor.fetchone()
                if row is None:
                    raise UnknownGenerationReferenceError(f"Generation {generation_id!r} not found in namespace {key}")
                lifecycle = GenerationLifecycleState(row[0])
                try:
                    covered_blocker_ids = list(json.loads(row[1]))
                except (json.JSONDecodeError, TypeError):
                    covered_blocker_ids = []
                completion_seq = row[2]

                if lifecycle in (GenerationLifecycleState.SUPERSEDED,):
                    raise InvalidGenerationTransitionError(f"Cannot record validations for a superseded generation {generation_id!r}")

                new_epoch = current_epoch + 1

                for validation in validations:
                    if validation.blocker_id not in covered_blocker_ids:
                        raise UnknownGenerationReferenceError(f"Blocker {validation.blocker_id!r} is not covered by generation {generation_id!r}")

                    pending_id = f"pval_{uuid.uuid4().hex[:12]}"
                    conn.execute(
                        "INSERT INTO generation_pending_validations (id, generation_id, blocker_id, still_unmet, availability, validation_seq, evidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (pending_id, generation_id, validation.blocker_id, int(validation.still_unmet), validation.availability.value, validation.validation_seq, validation.evidence, now),
                    )

                    cursor = conn.execute("SELECT settlement FROM generation_blocker_settlements WHERE generation_id = ? AND blocker_id = ?", (generation_id, validation.blocker_id))
                    existing = cursor.fetchone()
                    already_settled = existing is not None and existing[0] != BlockerSettlement.PENDING.value
                    if already_settled:
                        # A settled blocker in this generation never charges again (REQ-005, REQ-006).
                        continue

                    causally_bound = completion_seq is not None and validation.availability == ValidationAvailability.KNOWN and validation.validation_seq >= completion_seq
                    if not causally_bound:
                        continue

                    settlement = BlockerSettlement.STILL_OPEN if validation.still_unmet else BlockerSettlement.CORRECTED
                    charged = settlement == BlockerSettlement.STILL_OPEN

                    if existing is None:
                        conn.execute(
                            "INSERT INTO generation_blocker_settlements (generation_id, blocker_id, settlement, charged, validation_seq, evidence, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (generation_id, validation.blocker_id, settlement.value, int(charged), validation.validation_seq, validation.evidence, now, now),
                        )
                    else:
                        conn.execute(
                            "UPDATE generation_blocker_settlements SET settlement = ?, charged = ?, validation_seq = ?, evidence = ?, updated_at = ? WHERE generation_id = ? AND blocker_id = ?",
                            (settlement.value, int(charged), validation.validation_seq, validation.evidence, now, generation_id, validation.blocker_id),
                        )

                    if charged:
                        cursor = conn.execute(
                            "SELECT active_limit, total_failed_count, grant_baseline FROM blocker_allowances WHERE namespace_key = ? AND blocker_id = ?",
                            (key, validation.blocker_id),
                        )
                        allowance_row = cursor.fetchone()
                        if allowance_row is None:
                            raise UnknownGenerationReferenceError(f"Blocker {validation.blocker_id!r} has no repair allowance in namespace {key}")
                        active_limit, total_failed, baseline = allowance_row
                        new_total_failed = total_failed + 1
                        remaining_after = active_limit - (new_total_failed - baseline)
                        new_status = RepairAllowanceStatus.EXHAUSTED if remaining_after <= 0 else RepairAllowanceStatus.ALLOWABLE
                        new_reason = f"Failed count reached captured limit {active_limit} at generation {generation_id}" if new_status == RepairAllowanceStatus.EXHAUSTED else None
                        conn.execute(
                            "UPDATE blocker_allowances SET total_failed_count = ?, status = ?, exhaustion_reason = ?, updated_at = ? WHERE namespace_key = ? AND blocker_id = ?",
                            (new_total_failed, new_status.value, new_reason, now, key, validation.blocker_id),
                        )

                # Determine generation-level lifecycle after settlement.
                cursor = conn.execute("SELECT blocker_id, settlement FROM generation_blocker_settlements WHERE generation_id = ?", (generation_id,))
                settled_map = {bid: settlement for bid, settlement in cursor.fetchall()}
                all_settled = bool(covered_blocker_ids) and all(settled_map.get(bid) not in (None, BlockerSettlement.PENDING.value) for bid in covered_blocker_ids)
                if all_settled:
                    conn.execute("UPDATE generations SET lifecycle_state = ?, updated_at = ? WHERE generation_id = ?", (GenerationLifecycleState.SETTLED.value, now, generation_id))
                elif lifecycle == GenerationLifecycleState.PENDING_REVALIDATION:
                    conn.execute("UPDATE generations SET updated_at = ? WHERE generation_id = ?", (now, generation_id))

                self._bump_namespace_epoch(conn, key, new_epoch, now)
                self._journal(conn, operation_id, key, payload_hash, new_epoch, {"ok": True}, now)

                if self._simulate_failure_before_commit:
                    raise RepairAllowancePersistenceError("Simulated write failure before commit")

                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                conn.close()

        return self.get_snapshot(norm_origin, repository, pr_number, require_retained_state=True)

    # -- reconciliation of stuck generations -----------------------------------

    def supersede_generation(
        self,
        api_origin: str,
        repository: str,
        pr_number: int,
        operation_id: str,
        expected_epoch: int,
        generation_id: str,
        reason: str,
    ) -> RepairAllowanceLedgerSnapshot:
        """Explicitly reconcile a stuck INDETERMINATE generation without charging any blocker.

        This is the only way to free the single-outstanding-generation slot
        held by a generation whose delivery outcome could never be confirmed
        (REQ-003: indeterminate delivery must remain pending reconciliation
        without authorizing another speculative repair on its own).
        """
        self._check_db_integrity()
        norm_origin = normalize_api_origin(api_origin)
        key = _make_namespace_key(norm_origin, repository, pr_number)

        payload_dict = {"op": "supersede_generation", "generation_id": generation_id, "reason": reason}
        payload_hash = self._hash_payload(payload_dict)
        now = _now_iso()

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")

                cached = self._check_idempotency(conn, operation_id, payload_hash)
                if cached is not None:
                    conn.execute("COMMIT")
                    return self.get_snapshot(norm_origin, repository, pr_number, require_retained_state=True)

                current_epoch = self._check_cas(conn, key, expected_epoch)

                cursor = conn.execute("SELECT lifecycle_state FROM generations WHERE generation_id = ? AND namespace_key = ?", (generation_id, key))
                row = cursor.fetchone()
                if row is None:
                    raise UnknownGenerationReferenceError(f"Generation {generation_id!r} not found in namespace {key}")
                lifecycle = GenerationLifecycleState(row[0])
                if lifecycle != GenerationLifecycleState.INDETERMINATE:
                    raise InvalidGenerationTransitionError(f"Only an INDETERMINATE generation may be superseded (generation {generation_id!r} is {lifecycle.value})")

                new_epoch = current_epoch + 1
                conn.execute(
                    "UPDATE generations SET lifecycle_state = ?, superseded_reason = ?, updated_at = ? WHERE generation_id = ?",
                    (GenerationLifecycleState.SUPERSEDED.value, reason, now, generation_id),
                )
                self._bump_namespace_epoch(conn, key, new_epoch, now)
                self._journal(conn, operation_id, key, payload_hash, new_epoch, {"ok": True}, now)

                if self._simulate_failure_before_commit:
                    raise RepairAllowancePersistenceError("Simulated write failure before commit")

                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                conn.close()

        return self.get_snapshot(norm_origin, repository, pr_number, require_retained_state=True)

    # -- operator grants ---------------------------------------------------------

    def operator_grant(
        self,
        api_origin: str,
        repository: str,
        pr_number: int,
        request_id: str,
        expected_epoch: int,
        target_blocker_ids: Optional[Sequence[str]] = None,
        new_limit: int = DEFAULT_REPAIR_ALLOWANCE_LIMIT,
    ) -> OperatorGrantResult:
        """Grant a fresh bounded repair allowance to exhausted (or historical-unknown) blockers.

        Valid only when no generation for this PR is currently outstanding
        or indeterminate (REQ-009). ``target_blocker_ids`` of ``None``
        selects every currently EXHAUSTED or RECONCILIATION_REQUIRED open
        blocker known to this namespace. Never sends a repair or permits
        merge on its own.
        """
        if new_limit <= 0:
            raise ValueError("repair allowance grant limit must be a positive integer")

        self._check_db_integrity()
        norm_origin = normalize_api_origin(api_origin)
        key = _make_namespace_key(norm_origin, repository, pr_number)

        payload_dict = {
            "op": "operator_grant",
            "target_blocker_ids": sorted(target_blocker_ids) if target_blocker_ids is not None else None,
            "new_limit": new_limit,
        }
        payload_hash = self._hash_payload(payload_dict)
        now = _now_iso()

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")

                cursor = conn.execute("SELECT payload_hash, target_blocker_ids_json, granted_blocker_ids_json, new_limit FROM operator_grants WHERE request_id = ?", (request_id,))
                existing_grant = cursor.fetchone()
                if existing_grant is not None:
                    existing_hash = existing_grant[0]
                    if existing_hash != payload_hash:
                        raise RepairAllowanceIdempotencyConflictError(f"Conflicting reuse of operator grant request ID {request_id!r} with a different payload")
                    conn.execute("COMMIT")
                    replayed_granted_ids = tuple(json.loads(existing_grant[2]))
                    snapshot = self.get_snapshot(norm_origin, repository, pr_number, require_retained_state=True)
                    return OperatorGrantResult(granted=True, request_id=request_id, granted_blocker_ids=replayed_granted_ids, snapshot=snapshot)

                current_epoch = self._check_cas(conn, key, expected_epoch)

                cursor = conn.execute("SELECT generation_id, lifecycle_state FROM generations WHERE namespace_key = ?", (key,))
                outstanding = [gid for gid, lifecycle in cursor.fetchall() if GenerationLifecycleState(lifecycle) in OUTSTANDING_LIFECYCLE_STATES]
                if outstanding:
                    raise InvalidOperatorGrantError(f"Cannot grant a fresh repair allowance while a generation is outstanding or indeterminate: {outstanding[0]}")

                if target_blocker_ids is not None:
                    candidate_ids = list(target_blocker_ids)
                else:
                    cursor = conn.execute(
                        "SELECT blocker_id FROM blocker_allowances WHERE namespace_key = ? AND status IN (?, ?)",
                        (key, RepairAllowanceStatus.EXHAUSTED.value, RepairAllowanceStatus.RECONCILIATION_REQUIRED.value),
                    )
                    candidate_ids = [row[0] for row in cursor.fetchall()]

                new_epoch = current_epoch + 1
                granted_ids: list[str] = []
                for blocker_id in candidate_ids:
                    cursor = conn.execute("SELECT total_failed_count FROM blocker_allowances WHERE namespace_key = ? AND blocker_id = ?", (key, blocker_id))
                    row = cursor.fetchone()
                    if row is None:
                        self._ensure_blocker_allowance_row(conn, key, blocker_id, new_limit, now)
                        total_failed = 0
                    else:
                        total_failed = row[0]
                    conn.execute(
                        "UPDATE blocker_allowances SET active_limit = ?, grant_baseline = ?, status = ?, exhaustion_reason = NULL, historical_unknown = 0, updated_at = ? " "WHERE namespace_key = ? AND blocker_id = ?",
                        (new_limit, total_failed, RepairAllowanceStatus.ALLOWABLE.value, now, key, blocker_id),
                    )
                    granted_ids.append(blocker_id)

                conn.execute(
                    "INSERT INTO operator_grants (request_id, namespace_key, payload_hash, target_blocker_ids_json, granted_blocker_ids_json, new_limit, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (request_id, key, payload_hash, json.dumps(list(target_blocker_ids) if target_blocker_ids is not None else []), json.dumps(granted_ids), new_limit, now),
                )
                self._bump_namespace_epoch(conn, key, new_epoch, now)

                if self._simulate_failure_before_commit:
                    raise RepairAllowancePersistenceError("Simulated write failure before commit")

                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                conn.close()

        snapshot = self.get_snapshot(norm_origin, repository, pr_number, require_retained_state=True)
        return OperatorGrantResult(granted=True, request_id=request_id, granted_blocker_ids=tuple(granted_ids), snapshot=snapshot)
