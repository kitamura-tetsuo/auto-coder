"""Durable Jules competition-generation ledger.

Implements the specification and requirements from GitHub Issue #2070
(Stage of the Jules multi-candidate competition family, GitHub Issue
#2069): represent competing Jules executions dispatched for the same
source Issue attempt as one durable logical "speculative generation",
with durable candidate-identity records, a single-winner selection
boundary, and durable retirement of every non-winning result.

This module owns only the provider-independent durable state model and
its transitions. It does not itself start Jules sessions, poll provider
state, evaluate CI, merge pull requests, or read/write ``cloud.csv`` or
any other legacy single-session binding; those integrations belong to
later stages (see the Issue's "Non-goals" and "Implementation Notes").
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
from typing import Optional

from .logger_config import get_logger

logger = get_logger(__name__)

SUPPORTED_SCHEMA_VERSION = 2


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class GenerationLifecycleState(str, Enum):
    """Lifecycle of one speculative generation (REQ-001, REQ-010)."""

    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"


class CandidateAuthorityState(str, Enum):
    """Durable authority state of one candidate within a generation (REQ-002)."""

    NEVER_SUBMITTED = "NEVER_SUBMITTED"
    SUBMISSION_CLAIMED = "SUBMISSION_CLAIMED"
    ACCEPTED = "ACCEPTED"
    DEFINITELY_NOT_ACCEPTED = "DEFINITELY_NOT_ACCEPTED"
    SUBMISSION_OUTCOME_UNKNOWN = "SUBMISSION_OUTCOME_UNKNOWN"
    RETIRED = "RETIRED"


ELIGIBLE_CANDIDATE_STATES = frozenset(
    {
        CandidateAuthorityState.NEVER_SUBMITTED,
        CandidateAuthorityState.SUBMISSION_CLAIMED,
        CandidateAuthorityState.ACCEPTED,
    }
)


class MergeOutcome(str, Enum):
    """Observed merge outcome of a generation's selected winner (REQ-008, REQ-010)."""

    UNKNOWN = "UNKNOWN"
    MERGED = "MERGED"
    DEFINITELY_FAILED = "DEFINITELY_FAILED"


class ObligationKind(str, Enum):
    """Kinds of durable, acknowledgement-recoverable obligations (REQ-009)."""

    ADOPTION = "ADOPTION"
    ARTIFACT_CLEANUP = "ARTIFACT_CLEANUP"
    AGGREGATE_FAILURE = "AGGREGATE_FAILURE"


class ObligationStatus(str, Enum):
    """Delivery lifecycle of one durable obligation (REQ-009)."""

    PENDING = "PENDING"
    DELIVERED = "DELIVERED"
    ACKNOWLEDGED = "ACKNOWLEDGED"


class ReconciliationStatus(str, Enum):
    """Status of one pending-merge-effect reconciliation record (REQ-010)."""

    PENDING = "PENDING"
    RESOLVED = "RESOLVED"


class GenerationRetirementSource(str, Enum):
    """What explicit input retired a generation (REQ-010)."""

    ISSUE_CLOSED = "ISSUE_CLOSED"
    ORACLE_REPLACED = "ORACLE_REPLACED"
    AGGREGATE_FAILURE = "AGGREGATE_FAILURE"
    OPERATOR = "OPERATOR"


# ---------------------------------------------------------------------------
# Input / domain dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapturedPolicySettings:
    """Policy knobs captured immutably at generation admission time."""

    retry_limit: int = 0
    timeout_seconds: int = 0
    provider_name: str = ""
    extra_settings: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class SpeculativeGenerationBundle:
    """Immutable input describing one speculative-generation admission request (REQ-001)."""

    source_attempt_number: int = 0
    candidate_ids: tuple[str, ...] = ()
    issue_oracle_snapshot: str = ""
    issue_oracle_fingerprint: str = ""
    source_branch: str = ""
    policy_settings: CapturedPolicySettings = field(default_factory=CapturedPolicySettings)


@dataclass(frozen=True)
class CandidateBindingObservation:
    """One observed remote artifact (PR) binding for a candidate (REQ-002, REQ-012)."""

    pr_repository: str = ""
    pr_number: int = 0
    head_sha: str = ""
    base_sha: str = ""


@dataclass(frozen=True)
class AcceptanceRecord:
    """Caller-supplied input identifying the PR/revision a selection would adopt (REQ-004)."""

    repository: str = ""
    generation_id: str = ""
    candidate_id: str = ""
    pr_repository: str = ""
    pr_number: int = 0
    head_sha: str = ""
    base_sha: str = ""
    issue_oracle_fingerprint: str = ""
    expected_binding_revision: int = 0
    validation_revision: int = 0
    invalidation_revision: int = 0


@dataclass(frozen=True)
class AdoptionObligationPayload:
    candidate_id: str = ""
    pr_repository: str = ""
    pr_number: int = 0
    head_sha: str = ""
    base_sha: str = ""
    validation_revision: int = 0
    invalidation_revision: int = 0


@dataclass(frozen=True)
class ArtifactCleanupObligationPayload:
    candidate_id: str = ""
    provider_id: str = ""
    session_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class AggregateFailureObligationPayload:
    reason: str = ""
    winner_candidate_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Read-only snapshot dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BindingSnapshot:
    """One durable, append-only observed artifact binding (REQ-002, REQ-012)."""

    binding_id: str = ""
    pr_repository: str = ""
    pr_number: int = 0
    head_sha: str = ""
    base_sha: str = ""
    revision: int = 0
    retired: bool = False
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class CandidateSnapshot:
    """Immutable/read-only snapshot of one candidate's durable authority state."""

    candidate_id: str = ""
    authority_state: CandidateAuthorityState = CandidateAuthorityState.NEVER_SUBMITTED
    provider_id: str = ""
    session_id: str = ""
    retirement_reason: Optional[str] = None
    bindings: tuple[BindingSnapshot, ...] = ()
    created_at: str = ""
    updated_at: str = ""

    def latest_binding(self) -> Optional[BindingSnapshot]:
        if not self.bindings:
            return None
        return max(self.bindings, key=lambda b: b.revision)

    def is_eligible(self) -> bool:
        return self.authority_state in ELIGIBLE_CANDIDATE_STATES


@dataclass(frozen=True)
class GenerationSnapshot:
    """Immutable/read-only snapshot of one speculative generation."""

    generation_id: str = ""
    repository: str = ""
    issue_number: int = 0
    source_attempt_number: int = 0
    candidate_count: int = 0
    candidate_ids: tuple[str, ...] = ()
    issue_oracle_snapshot: str = ""
    issue_oracle_fingerprint: str = ""
    source_branch: str = ""
    policy_settings: CapturedPolicySettings = field(default_factory=CapturedPolicySettings)
    lifecycle_state: GenerationLifecycleState = GenerationLifecycleState.ACTIVE
    retirement_reason: Optional[str] = None
    retirement_source: Optional[GenerationRetirementSource] = None
    winner_candidate_id: Optional[str] = None
    winner_pr_repository: Optional[str] = None
    winner_pr_number: Optional[int] = None
    winner_head_sha: Optional[str] = None
    winner_base_sha: Optional[str] = None
    winner_validation_revision: Optional[int] = None
    winner_invalidation_revision: Optional[int] = None
    merge_outcome: MergeOutcome = MergeOutcome.UNKNOWN
    aggregate_failure_reason: Optional[str] = None
    candidates: tuple[CandidateSnapshot, ...] = ()
    created_at: str = ""
    updated_at: str = ""

    def is_active(self) -> bool:
        return self.lifecycle_state == GenerationLifecycleState.ACTIVE

    def has_winner(self) -> bool:
        return self.winner_candidate_id is not None

    def get_candidate(self, candidate_id: str) -> Optional[CandidateSnapshot]:
        for candidate in self.candidates:
            if candidate.candidate_id == candidate_id:
                return candidate
        return None


@dataclass(frozen=True)
class ObligationSnapshot:
    """Immutable/read-only snapshot of one durable obligation (REQ-009)."""

    obligation_id: str = ""
    generation_id: str = ""
    kind: ObligationKind = ObligationKind.ADOPTION
    status: ObligationStatus = ObligationStatus.PENDING
    payload_json: str = "{}"
    created_at: str = ""
    updated_at: str = ""

    def adoption_payload(self) -> AdoptionObligationPayload:
        data = json.loads(self.payload_json)
        return AdoptionObligationPayload(**data)

    def artifact_cleanup_payload(self) -> ArtifactCleanupObligationPayload:
        data = json.loads(self.payload_json)
        return ArtifactCleanupObligationPayload(**data)

    def aggregate_failure_payload(self) -> AggregateFailureObligationPayload:
        data = json.loads(self.payload_json)
        return AggregateFailureObligationPayload(**data)


@dataclass(frozen=True)
class ReconciliationSnapshot:
    """Immutable/read-only snapshot of one pending-merge-effect reconciliation (REQ-010)."""

    reconciliation_id: str = ""
    generation_id: str = ""
    pr_repository: Optional[str] = None
    pr_number: Optional[int] = None
    status: ReconciliationStatus = ReconciliationStatus.PENDING
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class RepoIssueSnapshot:
    """Immutable/read-only snapshot of the entire competition namespace (REQ-012)."""

    repository: str = ""
    issue_number: int = 0
    epoch: int = 0
    generations: tuple[GenerationSnapshot, ...] = ()
    obligations: tuple[ObligationSnapshot, ...] = ()
    reconciliations: tuple[ReconciliationSnapshot, ...] = ()
    created_at: str = ""
    updated_at: str = ""

    def get_generation(self, generation_id: str) -> Optional[GenerationSnapshot]:
        for generation in self.generations:
            if generation.generation_id == generation_id:
                return generation
        return None

    def get_active_generation(self) -> Optional[GenerationSnapshot]:
        for generation in self.generations:
            if generation.is_active():
                return generation
        return None

    def has_pending_reconciliation(self) -> bool:
        return any(r.status == ReconciliationStatus.PENDING for r in self.reconciliations)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GenerationAdmissionResult:
    admitted: bool
    generation_id: Optional[str]
    denial_reason: Optional[str]
    snapshot: RepoIssueSnapshot


@dataclass(frozen=True)
class CandidateTransitionResult:
    applied: bool
    denial_reason: Optional[str]
    snapshot: RepoIssueSnapshot


@dataclass(frozen=True)
class SelectionResult:
    selected: bool
    denial_reason: Optional[str]
    winner_candidate_id: Optional[str]
    snapshot: RepoIssueSnapshot


@dataclass(frozen=True)
class AggregateFailureResult:
    recorded: bool
    denial_reason: Optional[str]
    obligation_id: Optional[str]
    snapshot: RepoIssueSnapshot


@dataclass(frozen=True)
class GenerationRetirementResult:
    retired: bool
    denial_reason: Optional[str]
    snapshot: RepoIssueSnapshot


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class JulesCompetitionError(RuntimeError):
    """Base exception for all durable Jules-competition ledger operations."""


class JulesCompetitionUnavailableError(JulesCompetitionError):
    """Storage is unreadable, corrupt, an unsupported schema version, or missing required state."""


class StaleJulesCompetitionEpochError(JulesCompetitionError):
    """Compare-and-set failed because the current namespace epoch did not match expected."""


class JulesCompetitionIdempotencyConflictError(JulesCompetitionError):
    """The same operation ID was reused with a conflicting payload."""


class UnknownGenerationReferenceError(JulesCompetitionError):
    """A referenced generation ID is unknown in this namespace."""


class UnknownCandidateReferenceError(JulesCompetitionError):
    """A referenced candidate ID is unknown in the referenced generation."""


class InvalidGenerationTransitionError(JulesCompetitionError):
    """The requested transition is not valid given current durable state."""


class JulesCompetitionPersistenceError(JulesCompetitionError):
    """A required durable write operation failed."""


# ---------------------------------------------------------------------------
# Storage & ledger implementation
# ---------------------------------------------------------------------------


def default_jules_competition_db_path() -> Path:
    return Path.home() / ".auto-coder" / "jules_competition.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_namespace_key(repository: str, issue_number: int) -> str:
    return f"{repository}::{issue_number}"


def _encode_policy_settings(settings: CapturedPolicySettings) -> str:
    return json.dumps(
        {
            "retry_limit": settings.retry_limit,
            "timeout_seconds": settings.timeout_seconds,
            "provider_name": settings.provider_name,
            "extra_settings": list(settings.extra_settings),
        }
    )


def _decode_policy_settings(raw: str) -> CapturedPolicySettings:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return CapturedPolicySettings()
    return CapturedPolicySettings(
        retry_limit=int(data.get("retry_limit", 0)),
        timeout_seconds=int(data.get("timeout_seconds", 0)),
        provider_name=str(data.get("provider_name", "")),
        extra_settings=tuple(tuple(pair) for pair in data.get("extra_settings", [])),
    )


class JulesCompetitionLedger:
    """Durable, transactional, restart-surviving Jules competition-generation ledger.

    Backed by SQLite in WAL mode with ``BEGIN IMMEDIATE`` write-serialization,
    compare-and-set namespace epochs, and an idempotent operation journal,
    mirroring ``canonical_pr_blocker_ledger.CanonicalPRBlockerLedger`` and
    ``durable_repair_allowance.RepairAllowanceLedger``. Every mutating
    operation fails closed: any storage exception is surfaced as
    ``JulesCompetitionUnavailableError``/``JulesCompetitionPersistenceError``
    rather than silently behaving as an empty store (REQ-003).
    """

    _lock = threading.RLock()

    def __init__(self, db_path: Optional[Path] = None):
        self._db_path = Path(db_path) if db_path is not None else default_jules_competition_db_path()
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
            raise JulesCompetitionUnavailableError(f"Jules competition database is corrupt or unreadable: {exc}") from exc
        except OSError as exc:
            raise JulesCompetitionUnavailableError(f"Jules competition database cannot be accessed: {exc}") from exc

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
                    raise JulesCompetitionUnavailableError(f"Invalid schema version in Jules competition metadata: {row[0]}") from exc
                if version > SUPPORTED_SCHEMA_VERSION:
                    raise JulesCompetitionUnavailableError(f"Unsupported schema version {version} (max supported: {SUPPORTED_SCHEMA_VERSION})")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS namespaces (
                    namespace_key TEXT PRIMARY KEY,
                    repository TEXT NOT NULL,
                    issue_number INTEGER NOT NULL,
                    epoch INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS generations (
                    generation_id TEXT PRIMARY KEY,
                    namespace_key TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    issue_number INTEGER NOT NULL,
                    source_attempt_number INTEGER NOT NULL,
                    candidate_count INTEGER NOT NULL,
                    candidate_ids_json TEXT NOT NULL,
                    issue_oracle_snapshot TEXT NOT NULL,
                    issue_oracle_fingerprint TEXT NOT NULL,
                    source_branch TEXT NOT NULL,
                    policy_settings_json TEXT NOT NULL,
                    lifecycle_state TEXT NOT NULL,
                    retirement_reason TEXT,
                    retirement_source TEXT,
                    winner_candidate_id TEXT,
                    winner_pr_repository TEXT,
                    winner_pr_number INTEGER,
                    winner_head_sha TEXT,
                    winner_base_sha TEXT,
                    winner_validation_revision INTEGER,
                    winner_invalidation_revision INTEGER,
                    merge_outcome TEXT NOT NULL DEFAULT 'UNKNOWN',
                    aggregate_failure_reason TEXT,
                    admission_epoch INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            generation_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(generations)").fetchall()}
            if "winner_validation_revision" not in generation_columns:
                conn.execute("ALTER TABLE generations ADD COLUMN winner_validation_revision INTEGER")
            if "winner_invalidation_revision" not in generation_columns:
                conn.execute("ALTER TABLE generations ADD COLUMN winner_invalidation_revision INTEGER")
            conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SUPPORTED_SCHEMA_VERSION),))
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS candidates (
                    namespace_key TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    authority_state TEXT NOT NULL,
                    provider_id TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL DEFAULT '',
                    retirement_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (generation_id, candidate_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS candidate_bindings (
                    binding_id TEXT PRIMARY KEY,
                    generation_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    pr_repository TEXT NOT NULL,
                    pr_number INTEGER NOT NULL,
                    head_sha TEXT NOT NULL,
                    base_sha TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    retired INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS obligations (
                    obligation_id TEXT PRIMARY KEY,
                    namespace_key TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reconciliations (
                    reconciliation_id TEXT PRIMARY KEY,
                    namespace_key TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    pr_repository TEXT,
                    pr_number INTEGER,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
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
            raise JulesCompetitionUnavailableError(f"Jules competition schema verification failed: {exc}") from exc

    def _check_db_integrity(self) -> None:
        if not self._db_path.exists():
            return
        try:
            with sqlite3.connect(str(self._db_path), timeout=5.0) as test_conn:
                cursor = test_conn.execute("PRAGMA quick_check")
                row = cursor.fetchone()
                if row is None or row[0] != "ok":
                    raise JulesCompetitionUnavailableError(f"Jules competition quick_check failed: {row}")
        except sqlite3.DatabaseError as exc:
            raise JulesCompetitionUnavailableError(f"Jules competition database corrupt or unreadable: {exc}") from exc

    def _hash_payload(self, payload_dict: dict) -> str:
        canonical_json = json.dumps(payload_dict, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    def _check_idempotency(self, conn: sqlite3.Connection, operation_id: str, payload_hash: str) -> Optional[str]:
        cursor = conn.execute("SELECT payload_hash, result_json FROM operation_journal WHERE operation_id = ?", (operation_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        committed_hash, result_json = row
        if committed_hash != payload_hash:
            raise JulesCompetitionIdempotencyConflictError(f"Conflicting reuse of operation ID {operation_id!r} with a different payload")
        return result_json

    def _journal(self, conn: sqlite3.Connection, operation_id: str, key: str, payload_hash: str, committed_epoch: int, result: dict, now: str) -> None:
        conn.execute(
            "INSERT INTO operation_journal (operation_id, namespace_key, payload_hash, committed_epoch, result_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (operation_id, key, payload_hash, committed_epoch, json.dumps(result), now),
        )

    def _get_or_create_namespace(self, conn: sqlite3.Connection, repository: str, issue_number: int, now: str) -> tuple[str, int]:
        key = _make_namespace_key(repository, issue_number)
        cursor = conn.execute("SELECT epoch FROM namespaces WHERE namespace_key = ?", (key,))
        row = cursor.fetchone()
        if row is not None:
            return key, row[0]
        conn.execute(
            "INSERT INTO namespaces (namespace_key, repository, issue_number, epoch, created_at, updated_at) VALUES (?, ?, ?, 0, ?, ?)",
            (key, repository, issue_number, now, now),
        )
        return key, 0

    def _check_cas(self, conn: sqlite3.Connection, key: str, expected_epoch: int) -> int:
        cursor = conn.execute("SELECT epoch FROM namespaces WHERE namespace_key = ?", (key,))
        row = cursor.fetchone()
        if row is None:
            if expected_epoch == 0:
                return 0
            raise JulesCompetitionUnavailableError(f"Namespace {key} does not exist for CAS mutation")
        current_epoch = row[0]
        if current_epoch != expected_epoch:
            raise StaleJulesCompetitionEpochError(f"Contention: expected namespace epoch {expected_epoch}, but current epoch is {current_epoch}")
        return current_epoch

    def _bump_namespace_epoch(self, conn: sqlite3.Connection, key: str, new_epoch: int, now: str) -> None:
        conn.execute("UPDATE namespaces SET epoch = ?, updated_at = ? WHERE namespace_key = ?", (new_epoch, now, key))

    # -- namespace / read -----------------------------------------------------

    def get_current_epoch(self, repository: str, issue_number: int) -> int:
        """Return the current namespace epoch, or 0 if the namespace does not exist yet."""
        self._check_db_integrity()
        key = _make_namespace_key(repository, issue_number)
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute("SELECT epoch FROM namespaces WHERE namespace_key = ?", (key,))
                row = cursor.fetchone()
                return int(row[0]) if row is not None else 0
            except sqlite3.DatabaseError as exc:
                raise JulesCompetitionUnavailableError(f"Jules competition read error: {exc}") from exc
            finally:
                conn.close()

    def get_namespace_snapshot(self, repository: str, issue_number: int) -> RepoIssueSnapshot:
        """Return an immutable read-only snapshot for the namespace.

        A namespace that has never had a generation admitted returns an
        empty snapshot at epoch 0; a corrupt or unreadable database raises
        ``JulesCompetitionUnavailableError`` rather than behaving as an
        empty store (REQ-003).
        """
        self._check_db_integrity()
        key = _make_namespace_key(repository, issue_number)

        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute("SELECT epoch, created_at, updated_at FROM namespaces WHERE namespace_key = ?", (key,))
                ns_row = cursor.fetchone()
                if ns_row is None:
                    return RepoIssueSnapshot(repository=repository, issue_number=issue_number, epoch=0)
                epoch, ns_created, ns_updated = ns_row

                cursor = conn.execute(
                    "SELECT generation_id, source_attempt_number, candidate_count, candidate_ids_json, issue_oracle_snapshot, "
                    "issue_oracle_fingerprint, source_branch, policy_settings_json, lifecycle_state, retirement_reason, "
                    "retirement_source, winner_candidate_id, winner_pr_repository, winner_pr_number, winner_head_sha, "
                    "winner_base_sha, winner_validation_revision, winner_invalidation_revision, merge_outcome, aggregate_failure_reason, created_at, updated_at "
                    "FROM generations WHERE namespace_key = ? ORDER BY admission_epoch ASC, generation_id ASC",
                    (key,),
                )
                gen_rows = cursor.fetchall()

                cursor = conn.execute(
                    "SELECT generation_id, candidate_id, authority_state, provider_id, session_id, retirement_reason, created_at, updated_at " "FROM candidates WHERE namespace_key = ? ORDER BY created_at ASC, candidate_id ASC",
                    (key,),
                )
                candidates_by_generation: dict[str, list[CandidateSnapshot]] = {}
                candidate_rows = cursor.fetchall()

                cursor = conn.execute(
                    "SELECT binding_id, generation_id, candidate_id, pr_repository, pr_number, head_sha, base_sha, revision, retired, created_at, updated_at " "FROM candidate_bindings WHERE generation_id IN (SELECT generation_id FROM generations WHERE namespace_key = ?) " "ORDER BY revision ASC",
                    (key,),
                )
                bindings_by_candidate: dict[tuple[str, str], list[BindingSnapshot]] = {}
                for binding_id, gen_id, cand_id, pr_repo, pr_num, head_sha, base_sha, revision, retired, b_created, b_updated in cursor.fetchall():
                    bindings_by_candidate.setdefault((gen_id, cand_id), []).append(
                        BindingSnapshot(
                            binding_id=binding_id,
                            pr_repository=pr_repo,
                            pr_number=pr_num,
                            head_sha=head_sha,
                            base_sha=base_sha,
                            revision=revision,
                            retired=bool(retired),
                            created_at=b_created,
                            updated_at=b_updated,
                        )
                    )

                for gen_id, cand_id, authority_state, provider_id, session_id, retirement_reason, c_created, c_updated in candidate_rows:
                    candidates_by_generation.setdefault(gen_id, []).append(
                        CandidateSnapshot(
                            candidate_id=cand_id,
                            authority_state=CandidateAuthorityState(authority_state),
                            provider_id=provider_id,
                            session_id=session_id,
                            retirement_reason=retirement_reason,
                            bindings=tuple(bindings_by_candidate.get((gen_id, cand_id), ())),
                            created_at=c_created,
                            updated_at=c_updated,
                        )
                    )

                generations = []
                for (
                    gen_id,
                    source_attempt_number,
                    candidate_count,
                    candidate_ids_json,
                    oracle_snapshot,
                    oracle_fingerprint,
                    source_branch,
                    policy_json,
                    lifecycle,
                    retirement_reason,
                    retirement_source,
                    winner_candidate_id,
                    winner_pr_repository,
                    winner_pr_number,
                    winner_head_sha,
                    winner_base_sha,
                    winner_validation_revision,
                    winner_invalidation_revision,
                    merge_outcome,
                    aggregate_failure_reason,
                    g_created,
                    g_updated,
                ) in gen_rows:
                    try:
                        candidate_ids = tuple(json.loads(candidate_ids_json))
                    except (json.JSONDecodeError, TypeError):
                        candidate_ids = ()
                    generations.append(
                        GenerationSnapshot(
                            generation_id=gen_id,
                            repository=repository,
                            issue_number=issue_number,
                            source_attempt_number=source_attempt_number,
                            candidate_count=candidate_count,
                            candidate_ids=candidate_ids,
                            issue_oracle_snapshot=oracle_snapshot,
                            issue_oracle_fingerprint=oracle_fingerprint,
                            source_branch=source_branch,
                            policy_settings=_decode_policy_settings(policy_json),
                            lifecycle_state=GenerationLifecycleState(lifecycle),
                            retirement_reason=retirement_reason,
                            retirement_source=(GenerationRetirementSource(retirement_source) if retirement_source else None),
                            winner_candidate_id=winner_candidate_id,
                            winner_pr_repository=winner_pr_repository,
                            winner_pr_number=winner_pr_number,
                            winner_head_sha=winner_head_sha,
                            winner_base_sha=winner_base_sha,
                            winner_validation_revision=winner_validation_revision,
                            winner_invalidation_revision=winner_invalidation_revision,
                            merge_outcome=MergeOutcome(merge_outcome),
                            aggregate_failure_reason=aggregate_failure_reason,
                            candidates=tuple(candidates_by_generation.get(gen_id, ())),
                            created_at=g_created,
                            updated_at=g_updated,
                        )
                    )

                cursor = conn.execute(
                    "SELECT obligation_id, generation_id, kind, payload_json, status, created_at, updated_at FROM obligations WHERE namespace_key = ? ORDER BY created_at ASC",
                    (key,),
                )
                obligations = [
                    ObligationSnapshot(
                        obligation_id=oid,
                        generation_id=gen_id,
                        kind=ObligationKind(kind),
                        status=ObligationStatus(status),
                        payload_json=payload_json,
                        created_at=o_created,
                        updated_at=o_updated,
                    )
                    for oid, gen_id, kind, payload_json, status, o_created, o_updated in cursor.fetchall()
                ]

                cursor = conn.execute(
                    "SELECT reconciliation_id, generation_id, pr_repository, pr_number, status, created_at, updated_at FROM reconciliations WHERE namespace_key = ? ORDER BY created_at ASC",
                    (key,),
                )
                reconciliations = [
                    ReconciliationSnapshot(
                        reconciliation_id=rid,
                        generation_id=gen_id,
                        pr_repository=pr_repo,
                        pr_number=pr_num,
                        status=ReconciliationStatus(status),
                        created_at=r_created,
                        updated_at=r_updated,
                    )
                    for rid, gen_id, pr_repo, pr_num, status, r_created, r_updated in cursor.fetchall()
                ]

                return RepoIssueSnapshot(
                    repository=repository,
                    issue_number=issue_number,
                    epoch=epoch,
                    generations=tuple(generations),
                    obligations=tuple(obligations),
                    reconciliations=tuple(reconciliations),
                    created_at=ns_created,
                    updated_at=ns_updated,
                )
            except JulesCompetitionUnavailableError:
                raise
            except sqlite3.DatabaseError as exc:
                raise JulesCompetitionUnavailableError(f"Jules competition read error: {exc}") from exc
            finally:
                conn.close()

    def list_issue_numbers(self, repository: str) -> tuple[int, ...]:
        """List retained Issue namespaces, including retired generations."""
        self._check_db_integrity()
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT issue_number FROM namespaces WHERE repository = ? ORDER BY issue_number",
                    (repository.lower(),),
                ).fetchall()
                return tuple(int(row[0]) for row in rows)
            except sqlite3.DatabaseError as exc:
                raise JulesCompetitionUnavailableError(f"Jules competition read error: {exc}") from exc
            finally:
                conn.close()

    def _get_generation_row(self, conn: sqlite3.Connection, key: str, generation_id: str):
        cursor = conn.execute(
            "SELECT generation_id, lifecycle_state, winner_candidate_id, winner_pr_repository, winner_pr_number, " "winner_head_sha, winner_base_sha, merge_outcome, candidate_ids_json, issue_oracle_fingerprint " "FROM generations WHERE generation_id = ? AND namespace_key = ?",
            (generation_id, key),
        )
        return cursor.fetchone()

    # -- generation admission -------------------------------------------------

    def create_generation(
        self,
        repository: str,
        issue_number: int,
        operation_id: str,
        expected_epoch: int,
        bundle: SpeculativeGenerationBundle,
    ) -> GenerationAdmissionResult:
        """Admit a new speculative generation, or deny it (REQ-001).

        Denies admission (without raising) when an active generation already
        exists for this repository/Issue, or when a pending reconciliation
        from a retired generation's uncertain merge outcome has not yet been
        resolved would allow a competing merge effect (REQ-010). Replaying
        the same ``operation_id``/bundle is idempotent and never creates or
        resizes the candidate set (REQ-001).
        """
        if not bundle.candidate_ids:
            raise ValueError("A speculative generation requires a non-empty, fixed candidate set")
        if len(set(bundle.candidate_ids)) != len(bundle.candidate_ids):
            raise ValueError("Candidate identities within one generation must be distinct")

        self._check_db_integrity()
        key = _make_namespace_key(repository, issue_number)
        payload_dict = {
            "op": "create_generation",
            "source_attempt_number": bundle.source_attempt_number,
            "candidate_ids": list(bundle.candidate_ids),
            "issue_oracle_snapshot": bundle.issue_oracle_snapshot,
            "issue_oracle_fingerprint": bundle.issue_oracle_fingerprint,
            "source_branch": bundle.source_branch,
            "policy_settings": _encode_policy_settings(bundle.policy_settings),
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
                    snapshot = self.get_namespace_snapshot(repository, issue_number)
                    return GenerationAdmissionResult(admitted=result["admitted"], generation_id=result.get("generation_id"), denial_reason=result.get("denial_reason"), snapshot=snapshot)

                _, current_epoch = self._get_or_create_namespace(conn, repository, issue_number, now)
                if expected_epoch != current_epoch:
                    raise StaleJulesCompetitionEpochError(f"Contention: expected namespace epoch {expected_epoch}, but current epoch is {current_epoch}")

                cursor = conn.execute("SELECT generation_id FROM generations WHERE namespace_key = ? AND lifecycle_state = ?", (key, GenerationLifecycleState.ACTIVE.value))
                active_row = cursor.fetchone()

                denial_reason: Optional[str] = None
                if active_row is not None:
                    denial_reason = f"An active speculative generation already exists for this Issue: {active_row[0]}"

                if denial_reason is not None:
                    denial_epoch = current_epoch + 1
                    self._bump_namespace_epoch(conn, key, denial_epoch, now)
                    result_dict = {"admitted": False, "generation_id": None, "denial_reason": denial_reason}
                    self._journal(conn, operation_id, key, payload_hash, denial_epoch, result_dict, now)
                    conn.execute("COMMIT")
                    snapshot = self.get_namespace_snapshot(repository, issue_number)
                    return GenerationAdmissionResult(admitted=False, generation_id=None, denial_reason=denial_reason, snapshot=snapshot)

                generation_id = f"jgen_{uuid.uuid4().hex[:12]}"
                new_epoch = current_epoch + 1

                conn.execute(
                    "INSERT INTO generations (generation_id, namespace_key, repository, issue_number, source_attempt_number, candidate_count, "
                    "candidate_ids_json, issue_oracle_snapshot, issue_oracle_fingerprint, source_branch, policy_settings_json, lifecycle_state, "
                    "retirement_reason, retirement_source, winner_candidate_id, winner_pr_repository, winner_pr_number, winner_head_sha, "
                    "winner_base_sha, merge_outcome, aggregate_failure_reason, admission_epoch, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, NULL, ?, ?, ?)",
                    (
                        generation_id,
                        key,
                        repository,
                        issue_number,
                        bundle.source_attempt_number,
                        len(bundle.candidate_ids),
                        json.dumps(list(bundle.candidate_ids)),
                        bundle.issue_oracle_snapshot,
                        bundle.issue_oracle_fingerprint,
                        bundle.source_branch,
                        _encode_policy_settings(bundle.policy_settings),
                        GenerationLifecycleState.ACTIVE.value,
                        MergeOutcome.UNKNOWN.value,
                        new_epoch,
                        now,
                        now,
                    ),
                )
                for candidate_id in bundle.candidate_ids:
                    conn.execute(
                        "INSERT INTO candidates (namespace_key, generation_id, candidate_id, authority_state, provider_id, session_id, retirement_reason, created_at, updated_at) " "VALUES (?, ?, ?, ?, '', '', NULL, ?, ?)",
                        (key, generation_id, candidate_id, CandidateAuthorityState.NEVER_SUBMITTED.value, now, now),
                    )

                self._bump_namespace_epoch(conn, key, new_epoch, now)
                result_dict = {"admitted": True, "generation_id": generation_id, "denial_reason": None}
                self._journal(conn, operation_id, key, payload_hash, new_epoch, result_dict, now)

                if self._simulate_failure_before_commit:
                    raise JulesCompetitionPersistenceError("Simulated write failure before commit")

                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                conn.close()

        snapshot = self.get_namespace_snapshot(repository, issue_number)
        return GenerationAdmissionResult(admitted=True, generation_id=generation_id, denial_reason=None, snapshot=snapshot)

    # -- candidate transitions -------------------------------------------------

    def _transition_candidate(
        self,
        repository: str,
        issue_number: int,
        generation_id: str,
        candidate_id: str,
        operation_id: str,
        expected_epoch: int,
        op_name: str,
        payload_extra: dict,
        apply_fn,
    ) -> CandidateTransitionResult:
        self._check_db_integrity()
        key = _make_namespace_key(repository, issue_number)
        payload_dict = {"op": op_name, "generation_id": generation_id, "candidate_id": candidate_id, **payload_extra}
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
                    snapshot = self.get_namespace_snapshot(repository, issue_number)
                    return CandidateTransitionResult(applied=result["applied"], denial_reason=result.get("denial_reason"), snapshot=snapshot)

                current_epoch = self._check_cas(conn, key, expected_epoch)

                gen_row = self._get_generation_row(conn, key, generation_id)
                if gen_row is None:
                    raise UnknownGenerationReferenceError(f"Generation {generation_id!r} not found in namespace {key}")

                cursor = conn.execute("SELECT authority_state FROM candidates WHERE generation_id = ? AND candidate_id = ?", (generation_id, candidate_id))
                cand_row = cursor.fetchone()
                if cand_row is None:
                    raise UnknownCandidateReferenceError(f"Candidate {candidate_id!r} not found in generation {generation_id!r}")
                current_state = CandidateAuthorityState(cand_row[0])

                applied, denial_reason = apply_fn(conn, key, generation_id, candidate_id, current_state, now)

                new_epoch = current_epoch + 1
                self._bump_namespace_epoch(conn, key, new_epoch, now)
                result_dict = {"applied": applied, "denial_reason": denial_reason}
                self._journal(conn, operation_id, key, payload_hash, new_epoch, result_dict, now)

                if self._simulate_failure_before_commit:
                    raise JulesCompetitionPersistenceError("Simulated write failure before commit")

                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                conn.close()

        snapshot = self.get_namespace_snapshot(repository, issue_number)
        return CandidateTransitionResult(applied=applied, denial_reason=denial_reason, snapshot=snapshot)

    def claim_candidate_submission(self, repository: str, issue_number: int, generation_id: str, candidate_id: str, operation_id: str, expected_epoch: int) -> CandidateTransitionResult:
        """NEVER_SUBMITTED -> SUBMISSION_CLAIMED (REQ-002)."""

        def apply_fn(conn, key, gen_id, cand_id, current_state, now):
            if current_state == CandidateAuthorityState.SUBMISSION_CLAIMED:
                return True, None
            if current_state != CandidateAuthorityState.NEVER_SUBMITTED:
                return False, f"Cannot claim submission for candidate {cand_id!r} in state {current_state.value}"
            conn.execute(
                "UPDATE candidates SET authority_state = ?, updated_at = ? WHERE generation_id = ? AND candidate_id = ?",
                (CandidateAuthorityState.SUBMISSION_CLAIMED.value, now, gen_id, cand_id),
            )
            return True, None

        return self._transition_candidate(repository, issue_number, generation_id, candidate_id, operation_id, expected_epoch, "claim_candidate_submission", {}, apply_fn)

    def record_candidate_accepted(
        self,
        repository: str,
        issue_number: int,
        generation_id: str,
        candidate_id: str,
        operation_id: str,
        expected_epoch: int,
        provider_id: str,
        session_id: str,
    ) -> CandidateTransitionResult:
        """Record acceptance with provider/session identity (REQ-002).

        A candidate that is already RETIRED keeps its RETIRED authority
        state (a late acceptance observation cannot resurrect selection
        authority), but its provider/session identity is still recorded so
        the result remains inspectable (REQ-002, REQ-006, AS-002).
        """

        def apply_fn(conn, key, gen_id, cand_id, current_state, now):
            if current_state == CandidateAuthorityState.RETIRED:
                conn.execute(
                    "UPDATE candidates SET provider_id = CASE WHEN provider_id = '' THEN ? ELSE provider_id END, " "session_id = CASE WHEN session_id = '' THEN ? ELSE session_id END, updated_at = ? " "WHERE generation_id = ? AND candidate_id = ?",
                    (provider_id, session_id, now, gen_id, cand_id),
                )
                return False, "Candidate is retired; acceptance identity recorded but authority not restored"
            if current_state == CandidateAuthorityState.DEFINITELY_NOT_ACCEPTED:
                return False, f"Cannot accept candidate {cand_id!r} after definite non-acceptance"
            if current_state not in (CandidateAuthorityState.NEVER_SUBMITTED, CandidateAuthorityState.SUBMISSION_CLAIMED, CandidateAuthorityState.SUBMISSION_OUTCOME_UNKNOWN, CandidateAuthorityState.ACCEPTED):
                return False, f"Cannot accept candidate {cand_id!r} in state {current_state.value}"
            conn.execute(
                "UPDATE candidates SET authority_state = ?, provider_id = ?, session_id = ?, updated_at = ? WHERE generation_id = ? AND candidate_id = ?",
                (CandidateAuthorityState.ACCEPTED.value, provider_id, session_id, now, gen_id, cand_id),
            )
            return True, None

        return self._transition_candidate(repository, issue_number, generation_id, candidate_id, operation_id, expected_epoch, "record_candidate_accepted", {"provider_id": provider_id, "session_id": session_id}, apply_fn)

    def record_candidate_not_accepted(self, repository: str, issue_number: int, generation_id: str, candidate_id: str, operation_id: str, expected_epoch: int, evidence: str = "") -> CandidateTransitionResult:
        """Record definite non-acceptance (REQ-002)."""

        def apply_fn(conn, key, gen_id, cand_id, current_state, now):
            if current_state == CandidateAuthorityState.RETIRED:
                return True, None
            if current_state == CandidateAuthorityState.DEFINITELY_NOT_ACCEPTED:
                return True, None
            if current_state == CandidateAuthorityState.ACCEPTED:
                return False, f"Cannot mark accepted candidate {cand_id!r} as definitely not accepted"
            conn.execute(
                "UPDATE candidates SET authority_state = ?, updated_at = ? WHERE generation_id = ? AND candidate_id = ?",
                (CandidateAuthorityState.DEFINITELY_NOT_ACCEPTED.value, now, gen_id, cand_id),
            )
            return True, None

        return self._transition_candidate(repository, issue_number, generation_id, candidate_id, operation_id, expected_epoch, "record_candidate_not_accepted", {"evidence": evidence}, apply_fn)

    def record_candidate_outcome_unknown(self, repository: str, issue_number: int, generation_id: str, candidate_id: str, operation_id: str, expected_epoch: int, evidence: str = "") -> CandidateTransitionResult:
        """Record an unavailable/indeterminate submission observation (REQ-002).

        Never erases a durable authority state that is more informative than
        "unknown" (ACCEPTED, DEFINITELY_NOT_ACCEPTED, RETIRED are preserved).
        """

        def apply_fn(conn, key, gen_id, cand_id, current_state, now):
            if current_state in (CandidateAuthorityState.ACCEPTED, CandidateAuthorityState.DEFINITELY_NOT_ACCEPTED, CandidateAuthorityState.RETIRED):
                return False, f"An unavailable observation cannot override candidate {cand_id!r}'s current state {current_state.value}"
            conn.execute(
                "UPDATE candidates SET authority_state = ?, updated_at = ? WHERE generation_id = ? AND candidate_id = ?",
                (CandidateAuthorityState.SUBMISSION_OUTCOME_UNKNOWN.value, now, gen_id, cand_id),
            )
            return True, None

        return self._transition_candidate(repository, issue_number, generation_id, candidate_id, operation_id, expected_epoch, "record_candidate_outcome_unknown", {"evidence": evidence}, apply_fn)

    def retire_candidate(self, repository: str, issue_number: int, generation_id: str, candidate_id: str, operation_id: str, expected_epoch: int, reason: str) -> CandidateTransitionResult:
        """Durably retire a candidate, preserving its identity/provenance (REQ-002, REQ-006)."""

        def apply_fn(conn, key, gen_id, cand_id, current_state, now):
            if current_state == CandidateAuthorityState.RETIRED:
                return True, None
            conn.execute(
                "UPDATE candidates SET authority_state = ?, retirement_reason = ?, updated_at = ? WHERE generation_id = ? AND candidate_id = ?",
                (CandidateAuthorityState.RETIRED.value, reason, now, gen_id, cand_id),
            )
            conn.execute(
                "UPDATE candidate_bindings SET retired = 1, updated_at = ? WHERE generation_id = ? AND candidate_id = ?",
                (now, gen_id, cand_id),
            )
            return True, None

        return self._transition_candidate(repository, issue_number, generation_id, candidate_id, operation_id, expected_epoch, "retire_candidate", {"reason": reason}, apply_fn)

    # -- artifact (binding) observations --------------------------------------

    def record_candidate_binding(
        self,
        repository: str,
        issue_number: int,
        generation_id: str,
        candidate_id: str,
        operation_id: str,
        expected_epoch: int,
        observation: CandidateBindingObservation,
    ) -> CandidateTransitionResult:
        """Append a new observed artifact binding (REQ-002, REQ-012).

        Bindings are recorded regardless of the candidate's current
        authority state (identity/provenance is never erased), and never by
        themselves change authority state (REQ-002, REQ-011).
        """
        self._check_db_integrity()
        key = _make_namespace_key(repository, issue_number)
        payload_dict = {
            "op": "record_candidate_binding",
            "generation_id": generation_id,
            "candidate_id": candidate_id,
            "pr_repository": observation.pr_repository,
            "pr_number": observation.pr_number,
            "head_sha": observation.head_sha,
            "base_sha": observation.base_sha,
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
                    snapshot = self.get_namespace_snapshot(repository, issue_number)
                    return CandidateTransitionResult(applied=True, denial_reason=None, snapshot=snapshot)

                current_epoch = self._check_cas(conn, key, expected_epoch)

                gen_row = self._get_generation_row(conn, key, generation_id)
                if gen_row is None:
                    raise UnknownGenerationReferenceError(f"Generation {generation_id!r} not found in namespace {key}")

                cursor = conn.execute("SELECT 1 FROM candidates WHERE generation_id = ? AND candidate_id = ?", (generation_id, candidate_id))
                if cursor.fetchone() is None:
                    raise UnknownCandidateReferenceError(f"Candidate {candidate_id!r} not found in generation {generation_id!r}")

                cursor = conn.execute("SELECT COALESCE(MAX(revision), 0) FROM candidate_bindings WHERE generation_id = ? AND candidate_id = ?", (generation_id, candidate_id))
                next_revision = cursor.fetchone()[0] + 1
                binding_id = f"bind_{uuid.uuid4().hex[:12]}"
                conn.execute(
                    "INSERT INTO candidate_bindings (binding_id, generation_id, candidate_id, pr_repository, pr_number, head_sha, base_sha, revision, retired, created_at, updated_at) " "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                    (binding_id, generation_id, candidate_id, observation.pr_repository, observation.pr_number, observation.head_sha, observation.base_sha, next_revision, now, now),
                )

                new_epoch = current_epoch + 1
                self._bump_namespace_epoch(conn, key, new_epoch, now)
                self._journal(conn, operation_id, key, payload_hash, new_epoch, {"applied": True, "denial_reason": None}, now)

                if self._simulate_failure_before_commit:
                    raise JulesCompetitionPersistenceError("Simulated write failure before commit")

                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                conn.close()

        snapshot = self.get_namespace_snapshot(repository, issue_number)
        return CandidateTransitionResult(applied=True, denial_reason=None, snapshot=snapshot)

    def retire_binding(self, repository: str, issue_number: int, generation_id: str, candidate_id: str, operation_id: str, expected_epoch: int, reason: str = "") -> CandidateTransitionResult:
        """Retire the latest observed binding without retiring the candidate identity (REQ-006)."""

        def apply_fn(conn, key, gen_id, cand_id, current_state, now):
            cursor = conn.execute(
                "SELECT binding_id FROM candidate_bindings WHERE generation_id = ? AND candidate_id = ? ORDER BY revision DESC LIMIT 1",
                (gen_id, cand_id),
            )
            row = cursor.fetchone()
            if row is None:
                return False, f"Candidate {cand_id!r} has no recorded binding to retire"
            conn.execute("UPDATE candidate_bindings SET retired = 1, updated_at = ? WHERE binding_id = ?", (now, row[0]))
            return True, None

        return self._transition_candidate(repository, issue_number, generation_id, candidate_id, operation_id, expected_epoch, "retire_binding", {"reason": reason}, apply_fn)

    # -- selection --------------------------------------------------------------

    def select_winner(
        self,
        repository: str,
        issue_number: int,
        generation_id: str,
        candidate_id: str,
        operation_id: str,
        expected_epoch: int,
        acceptance: AcceptanceRecord,
    ) -> SelectionResult:
        """Atomically select at most one candidate-and-PR pair for a generation (REQ-004..REQ-007).

        Retires every other candidate and its latest binding durably on
        success (REQ-006), and is idempotent for a repeated successful
        selection of the same candidate/PR (REQ-005).
        """
        self._check_db_integrity()
        key = _make_namespace_key(repository, issue_number)
        payload_dict = {
            "op": "select_winner",
            "generation_id": generation_id,
            "candidate_id": candidate_id,
            "acceptance": {
                "repository": acceptance.repository,
                "generation_id": acceptance.generation_id,
                "candidate_id": acceptance.candidate_id,
                "pr_repository": acceptance.pr_repository,
                "pr_number": acceptance.pr_number,
                "head_sha": acceptance.head_sha,
                "base_sha": acceptance.base_sha,
                "issue_oracle_fingerprint": acceptance.issue_oracle_fingerprint,
                "expected_binding_revision": acceptance.expected_binding_revision,
                "validation_revision": acceptance.validation_revision,
                "invalidation_revision": acceptance.invalidation_revision,
            },
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
                    snapshot = self.get_namespace_snapshot(repository, issue_number)
                    return SelectionResult(selected=result["selected"], denial_reason=result.get("denial_reason"), winner_candidate_id=result.get("winner_candidate_id"), snapshot=snapshot)

                current_epoch = self._check_cas(conn, key, expected_epoch)

                gen_row = self._get_generation_row(conn, key, generation_id)
                if gen_row is None:
                    raise UnknownGenerationReferenceError(f"Generation {generation_id!r} not found in namespace {key}")
                (
                    _gid,
                    lifecycle,
                    existing_winner,
                    existing_winner_pr_repo,
                    existing_winner_pr_number,
                    existing_winner_head,
                    existing_winner_base,
                    _merge_outcome,
                    candidate_ids_json,
                    generation_oracle_fingerprint,
                ) = gen_row

                def deny(reason: str) -> SelectionResult:
                    result_dict = {"selected": False, "denial_reason": reason, "winner_candidate_id": existing_winner}
                    new_epoch_local = current_epoch + 1
                    self._bump_namespace_epoch(conn, key, new_epoch_local, now)
                    self._journal(conn, operation_id, key, payload_hash, new_epoch_local, result_dict, now)
                    conn.execute("COMMIT")
                    snap = self.get_namespace_snapshot(repository, issue_number)
                    return SelectionResult(selected=False, denial_reason=reason, winner_candidate_id=existing_winner, snapshot=snap)

                if acceptance.repository != repository or acceptance.generation_id != generation_id or acceptance.candidate_id != candidate_id:
                    return deny("Acceptance record identity does not match the requested selection target")

                if existing_winner is not None:
                    if not (existing_winner == candidate_id and existing_winner_pr_repo == acceptance.pr_repository and existing_winner_pr_number == acceptance.pr_number and existing_winner_head == acceptance.head_sha and existing_winner_base == acceptance.base_sha):
                        return deny(f"Generation {generation_id!r} already has a different selected winner: {existing_winner!r}")
                    revision_row = conn.execute(
                        "SELECT winner_validation_revision, winner_invalidation_revision FROM generations WHERE generation_id = ?",
                        (generation_id,),
                    ).fetchone()
                    if revision_row != (acceptance.validation_revision, acceptance.invalidation_revision):
                        return deny("Acceptance record revisions do not match the committed winner")

                if GenerationLifecycleState(lifecycle) != GenerationLifecycleState.ACTIVE:
                    return deny(f"Generation {generation_id!r} is not active (state {lifecycle})")

                cursor = conn.execute("SELECT status FROM reconciliations WHERE namespace_key = ? AND status = ? AND generation_id != ?", (key, ReconciliationStatus.PENDING.value, generation_id))
                if cursor.fetchone() is not None:
                    return deny("A pending reconciliation from a prior generation's uncertain merge outcome blocks new merge authorization")

                try:
                    covered_ids = list(json.loads(candidate_ids_json))
                except (json.JSONDecodeError, TypeError):
                    covered_ids = []
                if candidate_id not in covered_ids:
                    raise UnknownCandidateReferenceError(f"Candidate {candidate_id!r} is not part of generation {generation_id!r}")

                cursor = conn.execute("SELECT authority_state FROM candidates WHERE generation_id = ? AND candidate_id = ?", (generation_id, candidate_id))
                cand_row = cursor.fetchone()
                if cand_row is None:
                    raise UnknownCandidateReferenceError(f"Candidate {candidate_id!r} not found in generation {generation_id!r}")
                if CandidateAuthorityState(cand_row[0]) == CandidateAuthorityState.RETIRED:
                    return deny(f"Candidate {candidate_id!r} is retired and cannot be selected")

                if acceptance.issue_oracle_fingerprint != generation_oracle_fingerprint:
                    return deny("Acceptance record's Issue-oracle fingerprint does not match the generation's dispatched snapshot")
                if acceptance.validation_revision <= 0 or acceptance.invalidation_revision < 0:
                    return deny("Acceptance record revisions are invalid")
                if acceptance.invalidation_revision > acceptance.validation_revision:
                    return deny("Acceptance record is superseded by newer invalidation evidence")

                cursor = conn.execute(
                    "SELECT pr_repository, pr_number, head_sha, base_sha, revision, retired FROM candidate_bindings " "WHERE generation_id = ? AND candidate_id = ? ORDER BY revision DESC LIMIT 1",
                    (generation_id, candidate_id),
                )
                binding_row = cursor.fetchone()
                if binding_row is None:
                    return deny(f"Candidate {candidate_id!r} has no recorded artifact binding to select")
                b_pr_repo, b_pr_number, b_head, b_base, b_revision, b_retired = binding_row
                if bool(b_retired):
                    return deny("The latest recorded artifact binding has been retired")
                if b_pr_repo != acceptance.pr_repository or b_pr_number != acceptance.pr_number or b_head != acceptance.head_sha or b_base != acceptance.base_sha:
                    return deny("Acceptance record does not match the latest recorded target revision (stale or mismatched)")
                if b_revision != acceptance.expected_binding_revision:
                    return deny(f"Acceptance record's expected binding revision {acceptance.expected_binding_revision} does not match current revision {b_revision}")

                if existing_winner is not None:
                    # This is idempotent only after the complete acceptance record has
                    # been checked against the current generation and latest binding.
                    # A matching winner pointer alone cannot authorize a stale record.
                    result_dict = {"selected": True, "denial_reason": None, "winner_candidate_id": existing_winner}
                    new_epoch_local = current_epoch + 1
                    self._bump_namespace_epoch(conn, key, new_epoch_local, now)
                    self._journal(conn, operation_id, key, payload_hash, new_epoch_local, result_dict, now)
                    conn.execute("COMMIT")
                    snap = self.get_namespace_snapshot(repository, issue_number)
                    return SelectionResult(selected=True, denial_reason=None, winner_candidate_id=existing_winner, snapshot=snap)

                # All checks passed: commit this selection and retire everyone else.
                conn.execute(
                    "UPDATE generations SET winner_candidate_id = ?, winner_pr_repository = ?, winner_pr_number = ?, winner_head_sha = ?, winner_base_sha = ?, winner_validation_revision = ?, winner_invalidation_revision = ?, updated_at = ? WHERE generation_id = ?",
                    (candidate_id, acceptance.pr_repository, acceptance.pr_number, acceptance.head_sha, acceptance.base_sha, acceptance.validation_revision, acceptance.invalidation_revision, now, generation_id),
                )

                cursor = conn.execute("SELECT candidate_id, provider_id, session_id FROM candidates WHERE generation_id = ? AND candidate_id != ?", (generation_id, candidate_id))
                other_candidates = cursor.fetchall()
                for other_id, other_provider, other_session in other_candidates:
                    conn.execute(
                        "UPDATE candidates SET authority_state = ?, retirement_reason = ?, updated_at = ? WHERE generation_id = ? AND candidate_id = ?",
                        (CandidateAuthorityState.RETIRED.value, f"Superseded by winning candidate {candidate_id!r}", now, generation_id, other_id),
                    )
                    conn.execute(
                        "UPDATE candidate_bindings SET retired = 1, updated_at = ? WHERE generation_id = ? AND candidate_id = ?",
                        (now, generation_id, other_id),
                    )
                    cleanup_dedupe = f"{generation_id}:ARTIFACT_CLEANUP:{other_id}"
                    cleanup_payload = json.dumps(
                        {
                            "candidate_id": other_id,
                            "provider_id": other_provider,
                            "session_id": other_session,
                            "reason": f"Superseded by winning candidate {candidate_id!r}",
                        }
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO obligations (obligation_id, namespace_key, generation_id, kind, dedupe_key, payload_json, status, created_at, updated_at) " "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (f"obl_{uuid.uuid4().hex[:12]}", key, generation_id, ObligationKind.ARTIFACT_CLEANUP.value, cleanup_dedupe, cleanup_payload, ObligationStatus.PENDING.value, now, now),
                    )

                adoption_dedupe = f"{generation_id}:ADOPTION"
                adoption_payload = json.dumps(
                    {
                        "candidate_id": candidate_id,
                        "pr_repository": acceptance.pr_repository,
                        "pr_number": acceptance.pr_number,
                        "head_sha": acceptance.head_sha,
                        "base_sha": acceptance.base_sha,
                        "validation_revision": acceptance.validation_revision,
                        "invalidation_revision": acceptance.invalidation_revision,
                    }
                )
                conn.execute(
                    "INSERT OR IGNORE INTO obligations (obligation_id, namespace_key, generation_id, kind, dedupe_key, payload_json, status, created_at, updated_at) " "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (f"obl_{uuid.uuid4().hex[:12]}", key, generation_id, ObligationKind.ADOPTION.value, adoption_dedupe, adoption_payload, ObligationStatus.PENDING.value, now, now),
                )

                new_epoch = current_epoch + 1
                self._bump_namespace_epoch(conn, key, new_epoch, now)
                result_dict = {"selected": True, "denial_reason": None, "winner_candidate_id": candidate_id}
                self._journal(conn, operation_id, key, payload_hash, new_epoch, result_dict, now)

                if self._simulate_failure_before_commit:
                    raise JulesCompetitionPersistenceError("Simulated write failure before commit")

                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                conn.close()

        snapshot = self.get_namespace_snapshot(repository, issue_number)
        return SelectionResult(selected=True, denial_reason=None, winner_candidate_id=candidate_id, snapshot=snapshot)

    # -- merge outcome / aggregate failure / retirement -------------------------

    def record_merge_outcome(self, repository: str, issue_number: int, generation_id: str, operation_id: str, expected_epoch: int, outcome: MergeOutcome) -> RepoIssueSnapshot:
        """Record the observed merge outcome of a generation's selected winner (REQ-008, REQ-010).

        When the outcome becomes established (``MERGED`` or
        ``DEFINITELY_FAILED``), any pending reconciliation for this
        generation is automatically resolved.
        """
        self._check_db_integrity()
        key = _make_namespace_key(repository, issue_number)
        payload_dict = {"op": "record_merge_outcome", "generation_id": generation_id, "outcome": outcome.value}
        payload_hash = self._hash_payload(payload_dict)
        now = _now_iso()

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")

                cached = self._check_idempotency(conn, operation_id, payload_hash)
                if cached is not None:
                    conn.execute("COMMIT")
                    return self.get_namespace_snapshot(repository, issue_number)

                current_epoch = self._check_cas(conn, key, expected_epoch)

                gen_row = self._get_generation_row(conn, key, generation_id)
                if gen_row is None:
                    raise UnknownGenerationReferenceError(f"Generation {generation_id!r} not found in namespace {key}")
                if gen_row[2] is None:
                    raise InvalidGenerationTransitionError(f"Generation {generation_id!r} has no selected winner to record a merge outcome for")

                conn.execute("UPDATE generations SET merge_outcome = ?, updated_at = ? WHERE generation_id = ?", (outcome.value, now, generation_id))

                if outcome != MergeOutcome.UNKNOWN:
                    conn.execute(
                        "UPDATE reconciliations SET status = ?, updated_at = ? WHERE generation_id = ? AND status = ?",
                        (ReconciliationStatus.RESOLVED.value, now, generation_id, ReconciliationStatus.PENDING.value),
                    )

                new_epoch = current_epoch + 1
                self._bump_namespace_epoch(conn, key, new_epoch, now)
                self._journal(conn, operation_id, key, payload_hash, new_epoch, {"ok": True}, now)

                if self._simulate_failure_before_commit:
                    raise JulesCompetitionPersistenceError("Simulated write failure before commit")

                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                conn.close()

        return self.get_namespace_snapshot(repository, issue_number)

    def record_aggregate_failure(self, repository: str, issue_number: int, generation_id: str, operation_id: str, expected_epoch: int, reason: str) -> AggregateFailureResult:
        """Record one durable, generation-keyed aggregate-failure obligation (REQ-008).

        Refuses while any relevant submission/merge outcome remains unknown,
        while an unselected candidate is still eligible, or after confirmed
        merge; duplicate reports are idempotent (REQ-008).
        """
        self._check_db_integrity()
        key = _make_namespace_key(repository, issue_number)
        payload_dict = {"op": "record_aggregate_failure", "generation_id": generation_id, "reason": reason}
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
                    snapshot = self.get_namespace_snapshot(repository, issue_number)
                    return AggregateFailureResult(recorded=result["recorded"], denial_reason=result.get("denial_reason"), obligation_id=result.get("obligation_id"), snapshot=snapshot)

                current_epoch = self._check_cas(conn, key, expected_epoch)

                gen_row = self._get_generation_row(conn, key, generation_id)
                if gen_row is None:
                    raise UnknownGenerationReferenceError(f"Generation {generation_id!r} not found in namespace {key}")
                winner_candidate_id = gen_row[2]
                merge_outcome = MergeOutcome(gen_row[7])

                dedupe_key = f"{generation_id}:AGGREGATE_FAILURE"
                cursor = conn.execute("SELECT obligation_id FROM obligations WHERE dedupe_key = ?", (dedupe_key,))
                existing_obligation = cursor.fetchone()
                if existing_obligation is not None:
                    result_dict = {"recorded": True, "denial_reason": None, "obligation_id": existing_obligation[0]}
                    new_epoch_local = current_epoch + 1
                    self._bump_namespace_epoch(conn, key, new_epoch_local, now)
                    self._journal(conn, operation_id, key, payload_hash, new_epoch_local, result_dict, now)
                    conn.execute("COMMIT")
                    snapshot = self.get_namespace_snapshot(repository, issue_number)
                    return AggregateFailureResult(recorded=True, denial_reason=None, obligation_id=existing_obligation[0], snapshot=snapshot)

                def deny(reason_text: str) -> AggregateFailureResult:
                    result_dict = {"recorded": False, "denial_reason": reason_text, "obligation_id": None}
                    new_epoch_local = current_epoch + 1
                    self._bump_namespace_epoch(conn, key, new_epoch_local, now)
                    self._journal(conn, operation_id, key, payload_hash, new_epoch_local, result_dict, now)
                    conn.execute("COMMIT")
                    snap = self.get_namespace_snapshot(repository, issue_number)
                    return AggregateFailureResult(recorded=False, denial_reason=reason_text, obligation_id=None, snapshot=snap)

                if winner_candidate_id is not None:
                    if merge_outcome == MergeOutcome.MERGED:
                        return deny("Cannot record aggregate failure: the selected result already merged")
                    if merge_outcome == MergeOutcome.UNKNOWN:
                        return deny("Cannot record aggregate failure: the selected result's merge outcome is not yet established")
                else:
                    cursor = conn.execute("SELECT candidate_id, authority_state FROM candidates WHERE generation_id = ?", (generation_id,))
                    for cand_id, authority_state in cursor.fetchall():
                        state = CandidateAuthorityState(authority_state)
                        if state == CandidateAuthorityState.SUBMISSION_OUTCOME_UNKNOWN:
                            return deny(f"Cannot record aggregate failure: candidate {cand_id!r}'s submission outcome is unknown")
                        if state in ELIGIBLE_CANDIDATE_STATES:
                            return deny(f"Cannot record aggregate failure: candidate {cand_id!r} is still eligible")

                obligation_id = f"obl_{uuid.uuid4().hex[:12]}"
                payload = json.dumps({"reason": reason, "winner_candidate_id": winner_candidate_id})
                conn.execute(
                    "INSERT INTO obligations (obligation_id, namespace_key, generation_id, kind, dedupe_key, payload_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (obligation_id, key, generation_id, ObligationKind.AGGREGATE_FAILURE.value, dedupe_key, payload, ObligationStatus.PENDING.value, now, now),
                )
                conn.execute(
                    "UPDATE generations SET lifecycle_state = ?, aggregate_failure_reason = ?, retirement_reason = ?, retirement_source = ?, updated_at = ? WHERE generation_id = ?",
                    (GenerationLifecycleState.RETIRED.value, reason, reason, GenerationRetirementSource.AGGREGATE_FAILURE.value, now, generation_id),
                )

                new_epoch = current_epoch + 1
                self._bump_namespace_epoch(conn, key, new_epoch, now)
                result_dict = {"recorded": True, "denial_reason": None, "obligation_id": obligation_id}
                self._journal(conn, operation_id, key, payload_hash, new_epoch, result_dict, now)

                if self._simulate_failure_before_commit:
                    raise JulesCompetitionPersistenceError("Simulated write failure before commit")

                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                conn.close()

        snapshot = self.get_namespace_snapshot(repository, issue_number)
        return AggregateFailureResult(recorded=True, denial_reason=None, obligation_id=obligation_id, snapshot=snapshot)

    def retire_generation(
        self,
        repository: str,
        issue_number: int,
        generation_id: str,
        operation_id: str,
        expected_epoch: int,
        reason: str,
        source: GenerationRetirementSource,
    ) -> GenerationRetirementResult:
        """Explicitly retire a generation without classifying its competitors as failures (REQ-010).

        If a winner exists whose merge outcome is still unknown, a pending
        reconciliation record is durably kept so a replacement generation
        cannot authorize a competing merge until it is resolved (REQ-010).
        """
        self._check_db_integrity()
        key = _make_namespace_key(repository, issue_number)
        payload_dict = {"op": "retire_generation", "generation_id": generation_id, "reason": reason, "source": source.value}
        payload_hash = self._hash_payload(payload_dict)
        now = _now_iso()

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")

                cached = self._check_idempotency(conn, operation_id, payload_hash)
                if cached is not None:
                    conn.execute("COMMIT")
                    snapshot = self.get_namespace_snapshot(repository, issue_number)
                    return GenerationRetirementResult(retired=True, denial_reason=None, snapshot=snapshot)

                current_epoch = self._check_cas(conn, key, expected_epoch)

                gen_row = self._get_generation_row(conn, key, generation_id)
                if gen_row is None:
                    raise UnknownGenerationReferenceError(f"Generation {generation_id!r} not found in namespace {key}")
                lifecycle = GenerationLifecycleState(gen_row[1])
                winner_candidate_id = gen_row[2]
                winner_pr_repository = gen_row[3]
                winner_pr_number = gen_row[4]
                merge_outcome = MergeOutcome(gen_row[7])

                if lifecycle == GenerationLifecycleState.RETIRED:
                    new_epoch = current_epoch + 1
                    self._bump_namespace_epoch(conn, key, new_epoch, now)
                    self._journal(conn, operation_id, key, payload_hash, new_epoch, {"retired": True}, now)
                    conn.execute("COMMIT")
                    snapshot = self.get_namespace_snapshot(repository, issue_number)
                    return GenerationRetirementResult(retired=True, denial_reason=None, snapshot=snapshot)

                conn.execute(
                    "UPDATE generations SET lifecycle_state = ?, retirement_reason = ?, retirement_source = ?, updated_at = ? WHERE generation_id = ?",
                    (GenerationLifecycleState.RETIRED.value, reason, source.value, now, generation_id),
                )

                if winner_candidate_id is not None and merge_outcome == MergeOutcome.UNKNOWN:
                    cursor = conn.execute("SELECT 1 FROM reconciliations WHERE generation_id = ? AND status = ?", (generation_id, ReconciliationStatus.PENDING.value))
                    if cursor.fetchone() is None:
                        conn.execute(
                            "INSERT INTO reconciliations (reconciliation_id, namespace_key, generation_id, pr_repository, pr_number, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (f"recon_{uuid.uuid4().hex[:12]}", key, generation_id, winner_pr_repository, winner_pr_number, ReconciliationStatus.PENDING.value, now, now),
                        )

                new_epoch = current_epoch + 1
                self._bump_namespace_epoch(conn, key, new_epoch, now)
                self._journal(conn, operation_id, key, payload_hash, new_epoch, {"retired": True}, now)

                if self._simulate_failure_before_commit:
                    raise JulesCompetitionPersistenceError("Simulated write failure before commit")

                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                conn.close()

        snapshot = self.get_namespace_snapshot(repository, issue_number)
        return GenerationRetirementResult(retired=True, denial_reason=None, snapshot=snapshot)

    def resolve_reconciliation(self, repository: str, issue_number: int, reconciliation_id: str, operation_id: str, expected_epoch: int) -> RepoIssueSnapshot:
        """Explicitly mark a pending reconciliation resolved (REQ-010)."""
        self._check_db_integrity()
        key = _make_namespace_key(repository, issue_number)
        payload_dict = {"op": "resolve_reconciliation", "reconciliation_id": reconciliation_id}
        payload_hash = self._hash_payload(payload_dict)
        now = _now_iso()

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")

                cached = self._check_idempotency(conn, operation_id, payload_hash)
                if cached is not None:
                    conn.execute("COMMIT")
                    return self.get_namespace_snapshot(repository, issue_number)

                current_epoch = self._check_cas(conn, key, expected_epoch)

                cursor = conn.execute("SELECT 1 FROM reconciliations WHERE reconciliation_id = ? AND namespace_key = ?", (reconciliation_id, key))
                if cursor.fetchone() is None:
                    raise JulesCompetitionUnavailableError(f"Reconciliation {reconciliation_id!r} not found in namespace {key}")

                conn.execute("UPDATE reconciliations SET status = ?, updated_at = ? WHERE reconciliation_id = ?", (ReconciliationStatus.RESOLVED.value, now, reconciliation_id))

                new_epoch = current_epoch + 1
                self._bump_namespace_epoch(conn, key, new_epoch, now)
                self._journal(conn, operation_id, key, payload_hash, new_epoch, {"ok": True}, now)

                if self._simulate_failure_before_commit:
                    raise JulesCompetitionPersistenceError("Simulated write failure before commit")

                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                conn.close()

        return self.get_namespace_snapshot(repository, issue_number)

    # -- obligation delivery lifecycle (REQ-009) --------------------------------

    def list_pending_obligations(self, repository: str, issue_number: int, kind: Optional[ObligationKind] = None) -> tuple[ObligationSnapshot, ...]:
        snapshot = self.get_namespace_snapshot(repository, issue_number)
        return tuple(o for o in snapshot.obligations if o.status != ObligationStatus.ACKNOWLEDGED and (kind is None or o.kind == kind))

    def mark_obligation_delivered(self, obligation_id: str, operation_id: str) -> ObligationSnapshot:
        """PENDING -> DELIVERED, idempotent, restart-safe (REQ-009)."""
        self._check_db_integrity()
        now = _now_iso()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                cached = self._check_idempotency(conn, operation_id, self._hash_payload({"op": "mark_obligation_delivered", "obligation_id": obligation_id}))
                if cached is not None:
                    conn.execute("COMMIT")
                    return self._get_obligation(conn, obligation_id)

                cursor = conn.execute("SELECT status FROM obligations WHERE obligation_id = ?", (obligation_id,))
                row = cursor.fetchone()
                if row is None:
                    raise JulesCompetitionUnavailableError(f"Obligation {obligation_id!r} not found")
                status = ObligationStatus(row[0])
                if status == ObligationStatus.PENDING:
                    conn.execute("UPDATE obligations SET status = ?, updated_at = ? WHERE obligation_id = ?", (ObligationStatus.DELIVERED.value, now, obligation_id))

                self._journal(conn, operation_id, "", self._hash_payload({"op": "mark_obligation_delivered", "obligation_id": obligation_id}), 0, {"ok": True}, now)

                if self._simulate_failure_before_commit:
                    raise JulesCompetitionPersistenceError("Simulated write failure before commit")
                conn.execute("COMMIT")
                return self._get_obligation(conn, obligation_id)
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                conn.close()

    def acknowledge_obligation(self, obligation_id: str, operation_id: str) -> ObligationSnapshot:
        """DELIVERED (or PENDING) -> ACKNOWLEDGED, idempotent, restart-safe (REQ-009)."""
        self._check_db_integrity()
        now = _now_iso()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                cached = self._check_idempotency(conn, operation_id, self._hash_payload({"op": "acknowledge_obligation", "obligation_id": obligation_id}))
                if cached is not None:
                    conn.execute("COMMIT")
                    return self._get_obligation(conn, obligation_id)

                cursor = conn.execute("SELECT status FROM obligations WHERE obligation_id = ?", (obligation_id,))
                row = cursor.fetchone()
                if row is None:
                    raise JulesCompetitionUnavailableError(f"Obligation {obligation_id!r} not found")
                conn.execute("UPDATE obligations SET status = ?, updated_at = ? WHERE obligation_id = ?", (ObligationStatus.ACKNOWLEDGED.value, now, obligation_id))

                self._journal(conn, operation_id, "", self._hash_payload({"op": "acknowledge_obligation", "obligation_id": obligation_id}), 0, {"ok": True}, now)

                if self._simulate_failure_before_commit:
                    raise JulesCompetitionPersistenceError("Simulated write failure before commit")
                conn.execute("COMMIT")
                return self._get_obligation(conn, obligation_id)
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                conn.close()

    def _get_obligation(self, conn: sqlite3.Connection, obligation_id: str) -> ObligationSnapshot:
        cursor = conn.execute(
            "SELECT obligation_id, generation_id, kind, payload_json, status, created_at, updated_at FROM obligations WHERE obligation_id = ?",
            (obligation_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise JulesCompetitionUnavailableError(f"Obligation {obligation_id!r} not found")
        oid, gen_id, kind, payload_json, status, created_at, updated_at = row
        return ObligationSnapshot(obligation_id=oid, generation_id=gen_id, kind=ObligationKind(kind), status=ObligationStatus(status), payload_json=payload_json, created_at=created_at, updated_at=updated_at)
