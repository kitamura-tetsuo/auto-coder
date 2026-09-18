"""Durable canonical PR blocker ledger.

Implements the specification and requirements from GitHub Issue #2135:
persist canonical PR review blockers independently of threads, heads,
and reviewer sessions.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional, Sequence

from .logger_config import get_logger
from .util.github_request_outcome import normalize_api_origin

logger = get_logger(__name__)

SUPPORTED_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Enums and Domain Dataclasses
# ---------------------------------------------------------------------------


class BlockerDisposition(str, Enum):
    """Semantic disposition of a canonical PR blocker."""

    OPEN = "OPEN"
    VERIFIED_CORRECTION = "VERIFIED_CORRECTION"
    AUTHORIZED_INVALIDATION = "AUTHORIZED_INVALIDATION"
    RECURRENCE = "RECURRENCE"


class EvidenceAvailability(str, Enum):
    """Observable availability of current verification evidence."""

    KNOWN = "KNOWN"
    UNAVAILABLE = "UNAVAILABLE"
    OMITTED = "OMITTED"
    INCONCLUSIVE = "INCONCLUSIVE"


class ReconciliationDecision(str, Enum):
    """Outcome of an explicit identity reconciliation operation."""

    ASSOCIATE = "ASSOCIATE"
    DISTINCT_DEFECT = "DISTINCT_DEFECT"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True)
class QualifiedRequirement:
    """Qualified Issue requirement reference: Issue number + stable requirement ID."""

    issue_number: int = 0
    requirement_id: str = ""


@dataclass(frozen=True)
class CorrectionScope:
    """Accepted original correction scope and constituent concern IDs."""

    description: str = ""
    concern_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class BlockerAlias:
    """Provenance-bearing alias (GitHub thread, root comment, model session, test oracle gap)."""

    alias_type: str = ""
    alias_value: str = ""
    blocker_id: str = ""
    concern_id: Optional[str] = None


@dataclass(frozen=True)
class ContractRebindingRecord:
    """Explicitly accepted contract-rebinding record."""

    rebinding_id: str = ""
    reason: str = ""
    revised_scope: CorrectionScope = field(default_factory=CorrectionScope)
    requirement_manifest_revision: str = ""
    reviewed_head_sha: str = ""
    reviewed_base_sha: str = ""
    review_attempt_id: str = ""
    ledger_revision: int = 0
    created_at: str = ""


@dataclass(frozen=True)
class BlockerTransitionRecord:
    """Recorded transition or evidence update for a blocker."""

    transition_id: str = ""
    blocker_id: str = ""
    from_disposition: BlockerDisposition = BlockerDisposition.OPEN
    to_disposition: BlockerDisposition = BlockerDisposition.OPEN
    evidence_availability: EvidenceAvailability = EvidenceAvailability.KNOWN
    evidence: str = ""
    transition_reason: str = ""
    reviewed_head_sha: str = ""
    reviewed_base_sha: str = ""
    review_attempt_id: str = ""
    requirement_manifest_revision: str = ""
    ledger_revision: int = 0
    created_at: str = ""
    contract_rebinding: Optional[ContractRebindingRecord] = None


@dataclass(frozen=True)
class ReconciliationRecord:
    """Explicit record of an identity association or distinct-defect decision."""

    reconciliation_id: str = ""
    blocker_ids_considered: tuple[str, ...] = ()
    decision: str = ""
    associated_blocker_id: Optional[str] = None
    evidence: str = ""
    observation_identity: str = ""
    reviewed_head_sha: str = ""
    reviewed_base_sha: str = ""
    review_attempt_id: str = ""
    ledger_revision: int = 0
    created_at: str = ""


@dataclass(frozen=True)
class BlockerAdmissionPayload:
    """Input payload for admitting a blocker or evaluating a candidate."""

    category: str = ""
    qualified_requirements: tuple[QualifiedRequirement, ...] = ()
    authoritative_boundary: str = ""
    incorrect_behavior_or_missing_invariant: str = ""
    required_correction_outcome: str = ""
    evidence_needed: str = ""
    original_objective_anchor: Optional[str] = None
    accepted_scope: CorrectionScope = field(default_factory=CorrectionScope)
    aliases: tuple[BlockerAlias, ...] = ()
    evidence: str = ""
    evidence_availability: EvidenceAvailability = EvidenceAvailability.KNOWN
    reviewed_head_sha: str = ""
    reviewed_base_sha: str = ""
    review_attempt_id: str = ""
    requirement_manifest_revision: str = ""
    observation_identity: str = ""


@dataclass(frozen=True)
class BlockerSnapshot:
    """Immutable/read-only snapshot of a canonical blocker."""

    blocker_id: str = ""
    category: str = ""
    qualified_requirements: tuple[QualifiedRequirement, ...] = ()
    authoritative_boundary: str = ""
    incorrect_behavior_or_missing_invariant: str = ""
    required_correction_outcome: str = ""
    evidence_needed: str = ""
    original_objective_anchor: Optional[str] = None
    accepted_scope: CorrectionScope = field(default_factory=CorrectionScope)
    disposition: BlockerDisposition = BlockerDisposition.OPEN
    concern_ids: tuple[str, ...] = ()
    aliases: tuple[BlockerAlias, ...] = ()
    transitions: tuple[BlockerTransitionRecord, ...] = ()
    reconciliations: tuple[ReconciliationRecord, ...] = ()
    reconciliation_needs: tuple[str, ...] = ()
    created_at: str = ""
    last_updated_at: str = ""
    created_at_revision: int = 0
    last_updated_revision: int = 0

    def get_canonical_root_comment_id(self) -> Optional[int]:
        """Return the earliest authenticated numeric root comment ID if published."""
        root_ids = [int(a.alias_value) for a in self.aliases if a.alias_type in ("github_root_comment", "root_comment_id", "historical_root_comment_id") and a.alias_value.isdigit()]
        return min(root_ids) if root_ids else None

    def get_root_comment_ids(self) -> tuple[int, ...]:
        """Return all authenticated root comment IDs associated with this blocker."""
        return tuple(sorted(int(a.alias_value) for a in self.aliases if a.alias_type in ("github_root_comment", "root_comment_id", "historical_root_comment_id") and a.alias_value.isdigit()))


@dataclass(frozen=True)
class PublicationIntentSnapshot:
    """Durably recorded publication intent and authority assignment."""

    intent_id: str = ""
    namespace_key: str = ""
    destination_repo: str = ""
    destination_pr: int = 0
    reviewed_head_sha: str = ""
    reviewed_base_sha: str = ""
    review_attempt_id: str = ""
    payload_hash: str = ""
    blocker_ids: tuple[str, ...] = ()
    status: str = "PENDING"  # PENDING, CONFIRMED, REJECTED
    confirmed_roots: tuple[tuple[str, int], ...] = ()
    failure_reason: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class BlockerLedgerSnapshot:
    """Immutable/read-only snapshot of the entire blocker namespace."""

    api_origin: str = ""
    repository: str = ""
    pr_number: int = 0
    ledger_revision: int = 0
    revision_token: str = ""
    blockers: tuple[BlockerSnapshot, ...] = ()
    aliases: tuple[BlockerAlias, ...] = ()
    reconciliation_records: tuple[ReconciliationRecord, ...] = ()
    transitions: tuple[BlockerTransitionRecord, ...] = ()
    created_at: str = ""
    updated_at: str = ""

    def get_blocker(self, blocker_id: str) -> Optional[BlockerSnapshot]:
        """Return the blocker with the specified ID if present."""
        for blocker in self.blockers:
            if blocker.blocker_id == blocker_id:
                return blocker
        return None

    def get_blockers_for_alias(self, alias_type: str, alias_value: str) -> tuple[BlockerSnapshot, ...]:
        """Return all blockers associated with a specific alias."""
        matching_ids = {alias.blocker_id for alias in self.aliases if alias.alias_type == alias_type and alias.alias_value == alias_value}
        return tuple(b for b in self.blockers if b.blocker_id in matching_ids)

    def get_open_blockers(self) -> tuple[BlockerSnapshot, ...]:
        """Return all blockers currently in OPEN or RECURRENCE disposition."""
        return tuple(b for b in self.blockers if b.disposition in (BlockerDisposition.OPEN, BlockerDisposition.RECURRENCE))

    def get_blockers_by_requirement(self, issue_number: int, requirement_id: str) -> tuple[BlockerSnapshot, ...]:
        """Return all blockers referencing the specified qualified requirement."""
        matching = []
        for blocker in self.blockers:
            for req in blocker.qualified_requirements:
                if req.issue_number == issue_number and req.requirement_id == requirement_id:
                    matching.append(blocker)
                    break
        return tuple(matching)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class BlockerLedgerError(RuntimeError):
    """Base exception for all canonical blocker ledger operations."""


class BlockerLedgerUnavailableError(BlockerLedgerError):
    """Storage is unreadable, corrupt, unsupported version, or missing retained state."""


class StaleLedgerRevisionError(BlockerLedgerError):
    """Compare-and-set failed because current ledger revision did not match expected revision."""


class IdempotencyConflictError(BlockerLedgerError):
    """The same operation ID was reused with a conflicting payload."""


class UnknownBlockerReferenceError(BlockerLedgerError):
    """A referenced blocker ID is unknown in this namespace or crosses PR boundaries."""


class InconsistentScopeAssociationError(BlockerLedgerError):
    """An association decision conflicts with existing blocker scope or requirements."""


class AssociationAmbiguityError(BlockerLedgerError):
    """Unresolved association ambiguity between candidate observation and existing blockers."""


class BlockerPersistenceError(BlockerLedgerError):
    """A required durable write operation failed."""


class PublicationContentionError(BlockerLedgerError):
    """Another worker or intent currently holds exclusive publication authority for a blocker."""


class PublicationIntentMismatchError(BlockerLedgerError):
    """Publication intent already exists with a different payload or invalid parameters."""


# ---------------------------------------------------------------------------
# Storage & Ledger Implementation
# ---------------------------------------------------------------------------


def default_canonical_pr_blocker_db_path() -> Path:
    return Path.home() / ".auto-coder" / "canonical_pr_blockers.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_namespace_key(api_origin: str, repository: str, pr_number: int) -> str:
    normalized_origin = normalize_api_origin(api_origin)
    return f"{normalized_origin}::{repository}::{pr_number}"


class CanonicalPRBlockerLedger:
    """Durable, transactional, restart-surviving canonical PR blocker ledger.

    Backed by SQLite in WAL mode with compare-and-set optimistic locking,
    idempotent operation journal, and fail-closed persistence guarantees.
    """

    _lock = threading.RLock()

    def __init__(self, db_path: Optional[Path] = None):
        self._db_path = Path(db_path) if db_path is not None else default_canonical_pr_blocker_db_path()
        self._simulate_failure_before_commit: bool = False

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
            raise BlockerLedgerUnavailableError(f"Blocker ledger database is corrupt or unreadable: {exc}") from exc
        except OSError as exc:
            raise BlockerLedgerUnavailableError(f"Blocker ledger database cannot be accessed: {exc}") from exc

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            cursor = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'")
            row = cursor.fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                    (str(SUPPORTED_SCHEMA_VERSION),),
                )
            else:
                try:
                    version = int(row[0])
                except ValueError as exc:
                    raise BlockerLedgerUnavailableError(f"Invalid schema version in ledger metadata: {row[0]}") from exc
                if version > SUPPORTED_SCHEMA_VERSION:
                    raise BlockerLedgerUnavailableError(f"Unsupported schema version {version} (max supported: {SUPPORTED_SCHEMA_VERSION})")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS namespaces (
                    namespace_key TEXT PRIMARY KEY,
                    api_origin TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    pr_number INTEGER NOT NULL,
                    ledger_revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    initialized_explicitly INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS blockers (
                    blocker_id TEXT PRIMARY KEY,
                    namespace_key TEXT NOT NULL,
                    category TEXT NOT NULL,
                    authoritative_boundary TEXT NOT NULL,
                    incorrect_behavior_or_missing_invariant TEXT NOT NULL,
                    required_correction_outcome TEXT NOT NULL,
                    evidence_needed TEXT NOT NULL,
                    original_objective_anchor TEXT,
                    accepted_scope_description TEXT NOT NULL,
                    accepted_scope_concerns_json TEXT NOT NULL,
                    disposition TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    created_at_revision INTEGER NOT NULL,
                    last_updated_at TEXT NOT NULL,
                    last_updated_revision INTEGER NOT NULL,
                    reconciliation_needs_json TEXT NOT NULL DEFAULT '[]'
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS blocker_requirements (
                    blocker_id TEXT NOT NULL,
                    issue_number INTEGER NOT NULL,
                    requirement_id TEXT NOT NULL,
                    PRIMARY KEY (blocker_id, issue_number, requirement_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS blocker_aliases (
                    alias_id TEXT PRIMARY KEY,
                    namespace_key TEXT NOT NULL,
                    blocker_id TEXT NOT NULL,
                    alias_type TEXT NOT NULL,
                    alias_value TEXT NOT NULL,
                    concern_id TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS blocker_transitions (
                    transition_id TEXT PRIMARY KEY,
                    namespace_key TEXT NOT NULL,
                    blocker_id TEXT NOT NULL,
                    from_disposition TEXT NOT NULL,
                    to_disposition TEXT NOT NULL,
                    evidence_availability TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    transition_reason TEXT NOT NULL,
                    reviewed_head_sha TEXT NOT NULL,
                    reviewed_base_sha TEXT NOT NULL,
                    review_attempt_id TEXT NOT NULL,
                    requirement_manifest_revision TEXT NOT NULL,
                    ledger_revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    contract_rebinding_json TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reconciliation_records (
                    reconciliation_id TEXT PRIMARY KEY,
                    namespace_key TEXT NOT NULL,
                    blocker_ids_considered_json TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    associated_blocker_id TEXT,
                    evidence TEXT NOT NULL,
                    observation_identity TEXT NOT NULL,
                    reviewed_head_sha TEXT NOT NULL,
                    reviewed_base_sha TEXT NOT NULL,
                    review_attempt_id TEXT NOT NULL,
                    ledger_revision INTEGER NOT NULL,
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
                    committed_revision INTEGER NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS publication_intents (
                    intent_id TEXT PRIMARY KEY,
                    namespace_key TEXT NOT NULL,
                    destination_repo TEXT NOT NULL,
                    destination_pr INTEGER NOT NULL,
                    reviewed_head_sha TEXT NOT NULL,
                    reviewed_base_sha TEXT NOT NULL,
                    review_attempt_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    blocker_ids_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    confirmed_roots_json TEXT NOT NULL DEFAULT '[]',
                    failure_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
        except sqlite3.DatabaseError as exc:
            raise BlockerLedgerUnavailableError(f"Blocker ledger schema verification failed: {exc}") from exc

    def _check_db_integrity(self) -> None:
        """Verify the SQLite database file exists and is intact."""
        if not self._db_path.exists():
            return
        try:
            with sqlite3.connect(str(self._db_path), timeout=5.0) as test_conn:
                cursor = test_conn.execute("PRAGMA quick_check")
                row = cursor.fetchone()
                if row is None or row[0] != "ok":
                    raise BlockerLedgerUnavailableError(f"Blocker ledger quick_check failed: {row}")
        except sqlite3.DatabaseError as exc:
            raise BlockerLedgerUnavailableError(f"Blocker ledger corrupt or unreadable: {exc}") from exc

    def initialize_namespace(
        self,
        api_origin: str,
        repository: str,
        pr_number: int,
    ) -> BlockerLedgerSnapshot:
        """Explicitly initialize an empty namespace with revision 1."""
        self._check_db_integrity()
        norm_origin = normalize_api_origin(api_origin)
        key = _make_namespace_key(norm_origin, repository, pr_number)
        now = _now_iso()

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    "SELECT ledger_revision FROM namespaces WHERE namespace_key = ?",
                    (key,),
                )
                row = cursor.fetchone()
                if row is None:
                    conn.execute(
                        """
                        INSERT INTO namespaces (
                            namespace_key, api_origin, repository, pr_number,
                            ledger_revision, created_at, updated_at, initialized_explicitly
                        ) VALUES (?, ?, ?, ?, 1, ?, ?, 1)
                        """,
                        (key, norm_origin, repository, pr_number, now, now),
                    )
                if self._simulate_failure_before_commit:
                    raise BlockerPersistenceError("Simulated persistence failure during namespace initialization")
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

    def get_snapshot(
        self,
        api_origin: str,
        repository: str,
        pr_number: int,
        require_retained_state: bool = True,
    ) -> BlockerLedgerSnapshot:
        """Return an immutable read-only snapshot for the namespace.

        Raises BlockerLedgerUnavailableError if the database is corrupt,
        unreadable, unsupported version, or if retained state is missing.
        """
        self._check_db_integrity()
        norm_origin = normalize_api_origin(api_origin)
        key = _make_namespace_key(norm_origin, repository, pr_number)

        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    """
                    SELECT ledger_revision, created_at, updated_at, initialized_explicitly
                    FROM namespaces WHERE namespace_key = ?
                    """,
                    (key,),
                )
                ns_row = cursor.fetchone()
                if ns_row is None:
                    if require_retained_state:
                        raise BlockerLedgerUnavailableError(f"Required retained state is missing for namespace {key}")
                    raise BlockerLedgerUnavailableError(f"Namespace {key} is uninitialized")

                ledger_rev, ns_created, ns_updated, _ = ns_row

                # Load blockers
                cursor = conn.execute(
                    """
                    SELECT blocker_id, category, authoritative_boundary,
                           incorrect_behavior_or_missing_invariant,
                           required_correction_outcome, evidence_needed,
                           original_objective_anchor, accepted_scope_description,
                           accepted_scope_concerns_json, disposition,
                           created_at, created_at_revision,
                           last_updated_at, last_updated_revision,
                           reconciliation_needs_json
                    FROM blockers WHERE namespace_key = ?
                    ORDER BY created_at ASC, blocker_id ASC
                    """,
                    (key,),
                )
                blocker_rows = cursor.fetchall()

                # Load requirements
                cursor = conn.execute(
                    """
                    SELECT r.blocker_id, r.issue_number, r.requirement_id
                    FROM blocker_requirements r
                    JOIN blockers b ON r.blocker_id = b.blocker_id
                    WHERE b.namespace_key = ?
                    ORDER BY r.issue_number ASC, r.requirement_id ASC
                    """,
                    (key,),
                )
                req_map: dict[str, list[QualifiedRequirement]] = {}
                for b_id, issue_num, req_id in cursor.fetchall():
                    req_map.setdefault(b_id, []).append(QualifiedRequirement(issue_num, req_id))

                # Load aliases
                cursor = conn.execute(
                    """
                    SELECT alias_type, alias_value, blocker_id, concern_id
                    FROM blocker_aliases WHERE namespace_key = ?
                    ORDER BY created_at ASC, alias_id ASC
                    """,
                    (key,),
                )
                all_aliases: list[BlockerAlias] = []
                alias_map: dict[str, list[BlockerAlias]] = {}
                for atype, avalue, bid, cid in cursor.fetchall():
                    alias = BlockerAlias(
                        alias_type=atype,
                        alias_value=avalue,
                        blocker_id=bid,
                        concern_id=cid,
                    )
                    all_aliases.append(alias)
                    alias_map.setdefault(bid, []).append(alias)

                # Load transitions
                cursor = conn.execute(
                    """
                    SELECT transition_id, blocker_id, from_disposition, to_disposition,
                           evidence_availability, evidence, transition_reason,
                           reviewed_head_sha, reviewed_base_sha, review_attempt_id,
                           requirement_manifest_revision, ledger_revision, created_at,
                           contract_rebinding_json
                    FROM blocker_transitions WHERE namespace_key = ?
                    ORDER BY ledger_revision ASC, created_at ASC
                    """,
                    (key,),
                )
                all_transitions: list[BlockerTransitionRecord] = []
                transition_map: dict[str, list[BlockerTransitionRecord]] = {}
                for tid, bid, fdisp, tdisp, eavail, evid, treason, rhead, rbase, rattempt, rmanifest, lrev, tcreated, crebind_json in cursor.fetchall():
                    crebind = None
                    if crebind_json:
                        try:
                            raw_cb = json.loads(crebind_json)
                            crebind = ContractRebindingRecord(
                                rebinding_id=raw_cb.get("rebinding_id", ""),
                                reason=raw_cb.get("reason", ""),
                                revised_scope=CorrectionScope(
                                    description=raw_cb.get("revised_scope", {}).get("description", ""),
                                    concern_ids=tuple(raw_cb.get("revised_scope", {}).get("concern_ids", ())),
                                ),
                                requirement_manifest_revision=raw_cb.get("requirement_manifest_revision", ""),
                                reviewed_head_sha=raw_cb.get("reviewed_head_sha", ""),
                                reviewed_base_sha=raw_cb.get("reviewed_base_sha", ""),
                                review_attempt_id=raw_cb.get("review_attempt_id", ""),
                                ledger_revision=raw_cb.get("ledger_revision", 0),
                                created_at=raw_cb.get("created_at", ""),
                            )
                        except (json.JSONDecodeError, TypeError, KeyError):
                            crebind = None

                    trans = BlockerTransitionRecord(
                        transition_id=tid,
                        blocker_id=bid,
                        from_disposition=BlockerDisposition(fdisp),
                        to_disposition=BlockerDisposition(tdisp),
                        evidence_availability=EvidenceAvailability(eavail),
                        evidence=evid,
                        transition_reason=treason,
                        reviewed_head_sha=rhead,
                        reviewed_base_sha=rbase,
                        review_attempt_id=rattempt,
                        requirement_manifest_revision=rmanifest,
                        ledger_revision=lrev,
                        created_at=tcreated,
                        contract_rebinding=crebind,
                    )
                    all_transitions.append(trans)
                    transition_map.setdefault(bid, []).append(trans)

                # Load reconciliations
                cursor = conn.execute(
                    """
                    SELECT reconciliation_id, blocker_ids_considered_json, decision,
                           associated_blocker_id, evidence, observation_identity,
                           reviewed_head_sha, reviewed_base_sha, review_attempt_id,
                           ledger_revision, created_at
                    FROM reconciliation_records WHERE namespace_key = ?
                    ORDER BY ledger_revision ASC, created_at ASC
                    """,
                    (key,),
                )
                all_reconciliations: list[ReconciliationRecord] = []
                reconcil_map: dict[str, list[ReconciliationRecord]] = {}
                for rid, bids_json, rdecision, rassoc_id, revid, robs, rhead, rbase, rattempt, rlrev, rcreated in cursor.fetchall():
                    try:
                        bids = tuple(json.loads(bids_json))
                    except (json.JSONDecodeError, TypeError):
                        bids = ()
                    rec = ReconciliationRecord(
                        reconciliation_id=rid,
                        blocker_ids_considered=bids,
                        decision=rdecision,
                        associated_blocker_id=rassoc_id,
                        evidence=revid,
                        observation_identity=robs,
                        reviewed_head_sha=rhead,
                        reviewed_base_sha=rbase,
                        review_attempt_id=rattempt,
                        ledger_revision=rlrev,
                        created_at=rcreated,
                    )
                    all_reconciliations.append(rec)
                    if rassoc_id:
                        reconcil_map.setdefault(rassoc_id, []).append(rec)

                # Construct BlockerSnapshots
                blockers: list[BlockerSnapshot] = []
                for row in blocker_rows:
                    bid = row[0]
                    cat = row[1]
                    boundary = row[2]
                    behavior = row[3]
                    outcome = row[4]
                    ev_needed = row[5]
                    orig_obj = row[6]
                    scope_desc = row[7]
                    scope_concerns_raw = row[8]
                    disp = row[9]
                    b_created = row[10]
                    b_created_rev = row[11]
                    b_updated = row[12]
                    b_updated_rev = row[13]
                    rec_needs_raw = row[14]

                    try:
                        concerns = tuple(json.loads(scope_concerns_raw))
                    except (json.JSONDecodeError, TypeError):
                        concerns = ()

                    try:
                        rec_needs = tuple(json.loads(rec_needs_raw))
                    except (json.JSONDecodeError, TypeError):
                        rec_needs = ()

                    b_reqs = tuple(req_map.get(bid, ()))
                    b_aliases = tuple(alias_map.get(bid, ()))
                    b_trans = tuple(transition_map.get(bid, ()))
                    b_reconcils = tuple(reconcil_map.get(bid, ()))

                    blocker_snapshot = BlockerSnapshot(
                        blocker_id=bid,
                        category=cat,
                        qualified_requirements=b_reqs,
                        authoritative_boundary=boundary,
                        incorrect_behavior_or_missing_invariant=behavior,
                        required_correction_outcome=outcome,
                        evidence_needed=ev_needed,
                        original_objective_anchor=orig_obj,
                        accepted_scope=CorrectionScope(
                            description=scope_desc,
                            concern_ids=concerns,
                        ),
                        disposition=BlockerDisposition(disp),
                        concern_ids=concerns,
                        aliases=b_aliases,
                        transitions=b_trans,
                        reconciliations=b_reconcils,
                        reconciliation_needs=rec_needs,
                        created_at=b_created,
                        last_updated_at=b_updated,
                        created_at_revision=b_created_rev,
                        last_updated_revision=b_updated_rev,
                    )
                    blockers.append(blocker_snapshot)

                snapshot = BlockerLedgerSnapshot(
                    api_origin=norm_origin,
                    repository=repository,
                    pr_number=pr_number,
                    ledger_revision=ledger_rev,
                    revision_token=f"rev-{ledger_rev}",
                    blockers=tuple(blockers),
                    aliases=tuple(all_aliases),
                    reconciliation_records=tuple(all_reconciliations),
                    transitions=tuple(all_transitions),
                    created_at=ns_created,
                    updated_at=ns_updated,
                )
                return snapshot
            except BlockerLedgerUnavailableError:
                raise
            except sqlite3.DatabaseError as exc:
                raise BlockerLedgerUnavailableError(f"Blocker ledger read error: {exc}") from exc
            finally:
                conn.close()

    def _hash_payload(self, payload_dict: dict) -> str:
        canonical_json = json.dumps(payload_dict, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    def _check_idempotency(
        self,
        conn: sqlite3.Connection,
        operation_id: str,
        payload_hash: str,
        api_origin: str,
        repository: str,
        pr_number: int,
    ) -> Optional[BlockerLedgerSnapshot]:
        """Check if operation_id was already committed.

        If committed with matching payload hash, returns the committed snapshot.
        If committed with different payload hash, raises IdempotencyConflictError.
        If not committed, returns None.
        """
        cursor = conn.execute(
            "SELECT payload_hash, committed_revision FROM operation_journal WHERE operation_id = ?",
            (operation_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None

        committed_hash, _ = row
        if committed_hash != payload_hash:
            raise IdempotencyConflictError(f"Conflicting reuse of operation ID {operation_id!r} with different payload")
        # Identical payload replayed: return snapshot
        return self.get_snapshot(api_origin, repository, pr_number, require_retained_state=True)

    def _check_cas(
        self,
        conn: sqlite3.Connection,
        key: str,
        expected_ledger_revision: int,
    ) -> int:
        """Verify the current ledger revision matches expected_ledger_revision.

        Returns current revision.
        """
        cursor = conn.execute(
            "SELECT ledger_revision FROM namespaces WHERE namespace_key = ?",
            (key,),
        )
        row = cursor.fetchone()
        if row is None:
            raise BlockerLedgerUnavailableError(f"Namespace {key} does not exist for CAS mutation")

        current_rev = row[0]
        if current_rev != expected_ledger_revision:
            raise StaleLedgerRevisionError(f"Contention: expected ledger revision {expected_ledger_revision}, but current revision is {current_rev}")
        return current_rev

    def admit_blocker(
        self,
        api_origin: str,
        repository: str,
        pr_number: int,
        operation_id: str,
        expected_ledger_revision: int,
        payload: BlockerAdmissionPayload,
        review_observation_identity: str = "",
    ) -> tuple[str, BlockerLedgerSnapshot]:
        """Admit a new blocker into the ledger under an allocated opaque blocker ID.

        Enforces compare-and-set revision check and idempotency.
        """
        self._check_db_integrity()
        norm_origin = normalize_api_origin(api_origin)
        key = _make_namespace_key(norm_origin, repository, pr_number)

        payload_dict = {
            "op": "admit_blocker",
            "category": payload.category,
            "boundary": payload.authoritative_boundary,
            "incorrect_behavior": payload.incorrect_behavior_or_missing_invariant,
            "outcome": payload.required_correction_outcome,
            "evidence_needed": payload.evidence_needed,
            "orig_obj": payload.original_objective_anchor,
            "scope": asdict(payload.accepted_scope),
            "requirements": [asdict(r) for r in payload.qualified_requirements],
            "observation_id": review_observation_identity or payload.observation_identity,
        }
        payload_hash = self._hash_payload(payload_dict)
        now = _now_iso()

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")

                cached_snapshot = self._check_idempotency(conn, operation_id, payload_hash, norm_origin, repository, pr_number)
                if cached_snapshot is not None:
                    conn.execute("COMMIT")
                    # Locate the blocker created by this operation
                    # From transitions or blockers matching the boundary/scope
                    for b in cached_snapshot.blockers:
                        if b.authoritative_boundary == payload.authoritative_boundary:
                            return b.blocker_id, cached_snapshot
                    # Fallback to first blocker if only one
                    if cached_snapshot.blockers:
                        return cached_snapshot.blockers[0].blocker_id, cached_snapshot
                    return "", cached_snapshot

                current_rev = self._check_cas(conn, key, expected_ledger_revision)
                new_rev = current_rev + 1

                # Allocate opaque controller-owned blocker ID once
                blocker_id = f"blk_{uuid.uuid4().hex[:12]}"

                # Insert blocker
                conn.execute(
                    """
                    INSERT INTO blockers (
                        blocker_id, namespace_key, category, authoritative_boundary,
                        incorrect_behavior_or_missing_invariant,
                        required_correction_outcome, evidence_needed,
                        original_objective_anchor, accepted_scope_description,
                        accepted_scope_concerns_json, disposition,
                        created_at, created_at_revision,
                        last_updated_at, last_updated_revision,
                        reconciliation_needs_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]')
                    """,
                    (
                        blocker_id,
                        key,
                        payload.category,
                        payload.authoritative_boundary,
                        payload.incorrect_behavior_or_missing_invariant,
                        payload.required_correction_outcome,
                        payload.evidence_needed,
                        payload.original_objective_anchor,
                        payload.accepted_scope.description,
                        json.dumps(list(payload.accepted_scope.concern_ids)),
                        BlockerDisposition.OPEN.value,
                        now,
                        new_rev,
                        now,
                        new_rev,
                    ),
                )

                # Insert qualified requirements
                for req in payload.qualified_requirements:
                    conn.execute(
                        """
                        INSERT INTO blocker_requirements (blocker_id, issue_number, requirement_id)
                        VALUES (?, ?, ?)
                        """,
                        (blocker_id, req.issue_number, req.requirement_id),
                    )

                # Insert aliases
                for alias in payload.aliases:
                    aid = f"alias_{uuid.uuid4().hex[:12]}"
                    conn.execute(
                        """
                        INSERT INTO blocker_aliases (
                            alias_id, namespace_key, blocker_id, alias_type,
                            alias_value, concern_id, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            aid,
                            key,
                            blocker_id,
                            alias.alias_type,
                            alias.alias_value,
                            alias.concern_id,
                            now,
                        ),
                    )

                # Record admission transition
                tid = f"trans_{uuid.uuid4().hex[:12]}"
                conn.execute(
                    """
                    INSERT INTO blocker_transitions (
                        transition_id, namespace_key, blocker_id,
                        from_disposition, to_disposition, evidence_availability,
                        evidence, transition_reason, reviewed_head_sha,
                        reviewed_base_sha, review_attempt_id,
                        requirement_manifest_revision, ledger_revision,
                        created_at, contract_rebinding_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        tid,
                        key,
                        blocker_id,
                        BlockerDisposition.OPEN.value,
                        BlockerDisposition.OPEN.value,
                        payload.evidence_availability.value,
                        payload.evidence,
                        "Initial admission",
                        payload.reviewed_head_sha,
                        payload.reviewed_base_sha,
                        payload.review_attempt_id,
                        payload.requirement_manifest_revision,
                        new_rev,
                        now,
                    ),
                )

                # Update namespace revision
                conn.execute(
                    "UPDATE namespaces SET ledger_revision = ?, updated_at = ? WHERE namespace_key = ?",
                    (new_rev, now, key),
                )

                # Record in operation journal
                conn.execute(
                    """
                    INSERT INTO operation_journal (
                        operation_id, namespace_key, payload_hash,
                        committed_revision, result_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (operation_id, key, payload_hash, new_rev, blocker_id, now),
                )

                if self._simulate_failure_before_commit:
                    raise BlockerPersistenceError("Simulated write failure before commit")

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
        return blocker_id, snapshot

    def reconcile_observation(
        self,
        api_origin: str,
        repository: str,
        pr_number: int,
        operation_id: str,
        expected_ledger_revision: int,
        candidate_payload: BlockerAdmissionPayload,
        blocker_ids_considered: Sequence[str],
        decision: ReconciliationDecision,
        associated_blocker_id: Optional[str] = None,
        evidence: str = "",
        review_observation_identity: str = "",
        justified_category_transition: bool = False,
        category_transition_reason: str = "",
    ) -> tuple[str, BlockerLedgerSnapshot]:
        """Reconcile an observed finding against existing blockers.

        Records considered blocker IDs, decision, and evidence.
        Rejects cross-PR/unknown references and inconsistent scopes.
        Exposes unresolved ambiguity.
        """
        self._check_db_integrity()
        norm_origin = normalize_api_origin(api_origin)
        key = _make_namespace_key(norm_origin, repository, pr_number)

        payload_dict = {
            "op": "reconcile_observation",
            "candidate": {
                "category": candidate_payload.category,
                "boundary": candidate_payload.authoritative_boundary,
                "scope": asdict(candidate_payload.accepted_scope),
                "requirements": [asdict(r) for r in candidate_payload.qualified_requirements],
            },
            "considered": list(blocker_ids_considered),
            "decision": decision.value,
            "associated": associated_blocker_id,
            "evidence": evidence,
            "observation_id": review_observation_identity or candidate_payload.observation_identity,
            "justified_cat_trans": justified_category_transition or bool(category_transition_reason),
            "cat_trans_reason": category_transition_reason,
        }
        payload_hash = self._hash_payload(payload_dict)
        now = _now_iso()

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")

                cached_snapshot = self._check_idempotency(conn, operation_id, payload_hash, norm_origin, repository, pr_number)
                if cached_snapshot is not None:
                    conn.execute("COMMIT")
                    target_id = associated_blocker_id or ""
                    return target_id, cached_snapshot

                current_rev = self._check_cas(conn, key, expected_ledger_revision)

                # Validate blocker_ids_considered belong to this namespace
                for bid in blocker_ids_considered:
                    cursor = conn.execute(
                        "SELECT 1 FROM blockers WHERE blocker_id = ? AND namespace_key = ?",
                        (bid, key),
                    )
                    if cursor.fetchone() is None:
                        raise UnknownBlockerReferenceError(f"Considered blocker ID {bid!r} does not exist in namespace {key}")

                # Handle AMBIGUOUS decision per REQ-005
                if decision == ReconciliationDecision.AMBIGUOUS:
                    raise AssociationAmbiguityError(f"Unresolved association ambiguity for observation {review_observation_identity!r} " f"across considered blockers {list(blocker_ids_considered)}")

                new_rev = current_rev + 1
                effective_blocker_id: str = ""

                if decision == ReconciliationDecision.ASSOCIATE:
                    if not associated_blocker_id:
                        raise InconsistentScopeAssociationError("Association decision requires an associated_blocker_id")
                    if associated_blocker_id not in blocker_ids_considered:
                        raise InconsistentScopeAssociationError(f"Associated blocker ID {associated_blocker_id!r} was not among considered blockers")

                    # Verify scope consistency
                    cursor = conn.execute(
                        """
                        SELECT category, authoritative_boundary
                        FROM blockers WHERE blocker_id = ? AND namespace_key = ?
                        """,
                        (associated_blocker_id, key),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise UnknownBlockerReferenceError(f"Associated blocker ID {associated_blocker_id!r} not found in namespace")
                    existing_cat, existing_boundary = row
                    if candidate_payload.category and existing_cat != candidate_payload.category:
                        if not (justified_category_transition or category_transition_reason):
                            raise InconsistentScopeAssociationError(f"Category mismatch: candidate has {candidate_payload.category!r}, " f"existing blocker has {existing_cat!r}")
                        conn.execute(
                            "UPDATE blockers SET category = ?, last_updated_at = ?, last_updated_revision = ? WHERE blocker_id = ?",
                            (candidate_payload.category, now, new_rev, associated_blocker_id),
                        )
                    if candidate_payload.authoritative_boundary and existing_boundary != candidate_payload.authoritative_boundary:
                        raise InconsistentScopeAssociationError(f"Authoritative boundary mismatch: candidate has {candidate_payload.authoritative_boundary!r}, " f"existing blocker has {existing_boundary!r}")

                    effective_blocker_id = associated_blocker_id

                    # Append new aliases if provided
                    for alias in candidate_payload.aliases:
                        aid = f"alias_{uuid.uuid4().hex[:12]}"
                        conn.execute(
                            """
                            INSERT INTO blocker_aliases (
                                alias_id, namespace_key, blocker_id, alias_type,
                                alias_value, concern_id, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                aid,
                                key,
                                effective_blocker_id,
                                alias.alias_type,
                                alias.alias_value,
                                alias.concern_id,
                                now,
                            ),
                        )

                    # Append observation evidence transition
                    tid = f"trans_{uuid.uuid4().hex[:12]}"
                    cursor = conn.execute(
                        "SELECT disposition FROM blockers WHERE blocker_id = ?",
                        (effective_blocker_id,),
                    )
                    curr_disp = cursor.fetchone()[0]
                    conn.execute(
                        """
                        INSERT INTO blocker_transitions (
                            transition_id, namespace_key, blocker_id,
                            from_disposition, to_disposition, evidence_availability,
                            evidence, transition_reason, reviewed_head_sha,
                            reviewed_base_sha, review_attempt_id,
                            requirement_manifest_revision, ledger_revision,
                            created_at, contract_rebinding_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                        """,
                        (
                            tid,
                            key,
                            effective_blocker_id,
                            curr_disp,
                            curr_disp,
                            candidate_payload.evidence_availability.value,
                            candidate_payload.evidence or evidence,
                            f"Observation associated with existing blocker {effective_blocker_id}",
                            candidate_payload.reviewed_head_sha,
                            candidate_payload.reviewed_base_sha,
                            candidate_payload.review_attempt_id,
                            candidate_payload.requirement_manifest_revision,
                            new_rev,
                            now,
                        ),
                    )

                    conn.execute(
                        "UPDATE blockers SET last_updated_at = ?, last_updated_revision = ? WHERE blocker_id = ?",
                        (now, new_rev, effective_blocker_id),
                    )

                elif decision == ReconciliationDecision.DISTINCT_DEFECT:
                    # Allocate a distinct blocker ID
                    effective_blocker_id = f"blk_{uuid.uuid4().hex[:12]}"
                    conn.execute(
                        """
                        INSERT INTO blockers (
                            blocker_id, namespace_key, category, authoritative_boundary,
                            incorrect_behavior_or_missing_invariant,
                            required_correction_outcome, evidence_needed,
                            original_objective_anchor, accepted_scope_description,
                            accepted_scope_concerns_json, disposition,
                            created_at, created_at_revision,
                            last_updated_at, last_updated_revision,
                            reconciliation_needs_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]')
                        """,
                        (
                            effective_blocker_id,
                            key,
                            candidate_payload.category,
                            candidate_payload.authoritative_boundary,
                            candidate_payload.incorrect_behavior_or_missing_invariant,
                            candidate_payload.required_correction_outcome,
                            candidate_payload.evidence_needed,
                            candidate_payload.original_objective_anchor,
                            candidate_payload.accepted_scope.description,
                            json.dumps(list(candidate_payload.accepted_scope.concern_ids)),
                            BlockerDisposition.OPEN.value,
                            now,
                            new_rev,
                            now,
                            new_rev,
                        ),
                    )

                    for req in candidate_payload.qualified_requirements:
                        conn.execute(
                            """
                            INSERT INTO blocker_requirements (blocker_id, issue_number, requirement_id)
                            VALUES (?, ?, ?)
                            """,
                            (effective_blocker_id, req.issue_number, req.requirement_id),
                        )

                    for alias in candidate_payload.aliases:
                        aid = f"alias_{uuid.uuid4().hex[:12]}"
                        conn.execute(
                            """
                            INSERT INTO blocker_aliases (
                                alias_id, namespace_key, blocker_id, alias_type,
                                alias_value, concern_id, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                aid,
                                key,
                                effective_blocker_id,
                                alias.alias_type,
                                alias.alias_value,
                                alias.concern_id,
                                now,
                            ),
                        )

                    tid = f"trans_{uuid.uuid4().hex[:12]}"
                    conn.execute(
                        """
                        INSERT INTO blocker_transitions (
                            transition_id, namespace_key, blocker_id,
                            from_disposition, to_disposition, evidence_availability,
                            evidence, transition_reason, reviewed_head_sha,
                            reviewed_base_sha, review_attempt_id,
                            requirement_manifest_revision, ledger_revision,
                            created_at, contract_rebinding_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                        """,
                        (
                            tid,
                            key,
                            effective_blocker_id,
                            BlockerDisposition.OPEN.value,
                            BlockerDisposition.OPEN.value,
                            candidate_payload.evidence_availability.value,
                            candidate_payload.evidence,
                            "Distinct defect admitted via reconciliation",
                            candidate_payload.reviewed_head_sha,
                            candidate_payload.reviewed_base_sha,
                            candidate_payload.review_attempt_id,
                            candidate_payload.requirement_manifest_revision,
                            new_rev,
                            now,
                        ),
                    )

                # Record reconciliation record
                rid = f"rec_{uuid.uuid4().hex[:12]}"
                conn.execute(
                    """
                    INSERT INTO reconciliation_records (
                        reconciliation_id, namespace_key, blocker_ids_considered_json,
                        decision, associated_blocker_id, evidence, observation_identity,
                        reviewed_head_sha, reviewed_base_sha, review_attempt_id,
                        ledger_revision, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rid,
                        key,
                        json.dumps(list(blocker_ids_considered)),
                        decision.value,
                        effective_blocker_id if decision == ReconciliationDecision.ASSOCIATE else None,
                        evidence,
                        review_observation_identity or candidate_payload.observation_identity,
                        candidate_payload.reviewed_head_sha,
                        candidate_payload.reviewed_base_sha,
                        candidate_payload.review_attempt_id,
                        new_rev,
                        now,
                    ),
                )

                # Update namespace revision
                conn.execute(
                    "UPDATE namespaces SET ledger_revision = ?, updated_at = ? WHERE namespace_key = ?",
                    (new_rev, now, key),
                )

                # Journal
                conn.execute(
                    """
                    INSERT INTO operation_journal (
                        operation_id, namespace_key, payload_hash,
                        committed_revision, result_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (operation_id, key, payload_hash, new_rev, effective_blocker_id, now),
                )

                if self._simulate_failure_before_commit:
                    raise BlockerPersistenceError("Simulated write failure before commit")

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
        return effective_blocker_id, snapshot

    def record_evidence(
        self,
        api_origin: str,
        repository: str,
        pr_number: int,
        operation_id: str,
        expected_ledger_revision: int,
        blocker_id: str,
        evidence_availability: EvidenceAvailability,
        evidence: str,
        reviewed_head_sha: str = "",
        reviewed_base_sha: str = "",
        review_attempt_id: str = "",
        requirement_manifest_revision: str = "",
        review_observation_identity: str = "",
    ) -> BlockerLedgerSnapshot:
        """Record current verification evidence for an existing blocker.

        Per REQ-004: if evidence_availability is UNAVAILABLE, OMITTED, or
        INCONCLUSIVE, an open blocker remains OPEN and its disposition is
        not resolved or cleared.
        """
        self._check_db_integrity()
        norm_origin = normalize_api_origin(api_origin)
        key = _make_namespace_key(norm_origin, repository, pr_number)

        payload_dict = {
            "op": "record_evidence",
            "blocker_id": blocker_id,
            "availability": evidence_availability.value,
            "evidence": evidence,
            "head": reviewed_head_sha,
            "base": reviewed_base_sha,
            "attempt": review_attempt_id,
            "manifest": requirement_manifest_revision,
            "observation_id": review_observation_identity,
        }
        payload_hash = self._hash_payload(payload_dict)
        now = _now_iso()

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")

                cached_snapshot = self._check_idempotency(conn, operation_id, payload_hash, norm_origin, repository, pr_number)
                if cached_snapshot is not None:
                    conn.execute("COMMIT")
                    return cached_snapshot

                current_rev = self._check_cas(conn, key, expected_ledger_revision)

                cursor = conn.execute(
                    "SELECT disposition FROM blockers WHERE blocker_id = ? AND namespace_key = ?",
                    (blocker_id, key),
                )
                row = cursor.fetchone()
                if row is None:
                    raise UnknownBlockerReferenceError(f"Blocker {blocker_id!r} not found in namespace {key}")
                current_disp = row[0]

                # Semantic disposition does not change when evidence is unavailable/omitted/inconclusive
                new_disp = current_disp
                new_rev = current_rev + 1

                tid = f"trans_{uuid.uuid4().hex[:12]}"
                conn.execute(
                    """
                    INSERT INTO blocker_transitions (
                        transition_id, namespace_key, blocker_id,
                        from_disposition, to_disposition, evidence_availability,
                        evidence, transition_reason, reviewed_head_sha,
                        reviewed_base_sha, review_attempt_id,
                        requirement_manifest_revision, ledger_revision,
                        created_at, contract_rebinding_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        tid,
                        key,
                        blocker_id,
                        current_disp,
                        new_disp,
                        evidence_availability.value,
                        evidence,
                        f"Evidence recorded (availability: {evidence_availability.value})",
                        reviewed_head_sha,
                        reviewed_base_sha,
                        review_attempt_id,
                        requirement_manifest_revision,
                        new_rev,
                        now,
                    ),
                )

                conn.execute(
                    "UPDATE blockers SET last_updated_at = ?, last_updated_revision = ? WHERE blocker_id = ?",
                    (now, new_rev, blocker_id),
                )
                conn.execute(
                    "UPDATE namespaces SET ledger_revision = ?, updated_at = ? WHERE namespace_key = ?",
                    (new_rev, now, key),
                )
                conn.execute(
                    """
                    INSERT INTO operation_journal (
                        operation_id, namespace_key, payload_hash,
                        committed_revision, result_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (operation_id, key, payload_hash, new_rev, "OK", now),
                )

                if self._simulate_failure_before_commit:
                    raise BlockerPersistenceError("Simulated write failure before commit")

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

    def record_transition(
        self,
        api_origin: str,
        repository: str,
        pr_number: int,
        operation_id: str,
        expected_ledger_revision: int,
        blocker_id: str,
        target_disposition: BlockerDisposition,
        evidence: str,
        transition_reason: str = "",
        reviewed_head_sha: str = "",
        reviewed_base_sha: str = "",
        review_attempt_id: str = "",
        requirement_manifest_revision: str = "",
        review_observation_identity: str = "",
    ) -> BlockerLedgerSnapshot:
        """Record a semantic disposition transition for a blocker.

        Per REQ-004: recurrence reuses the original blocker ID.
        """
        self._check_db_integrity()
        norm_origin = normalize_api_origin(api_origin)
        key = _make_namespace_key(norm_origin, repository, pr_number)

        payload_dict = {
            "op": "record_transition",
            "blocker_id": blocker_id,
            "target": target_disposition.value,
            "evidence": evidence,
            "reason": transition_reason,
            "head": reviewed_head_sha,
            "base": reviewed_base_sha,
            "attempt": review_attempt_id,
            "manifest": requirement_manifest_revision,
            "observation_id": review_observation_identity,
        }
        payload_hash = self._hash_payload(payload_dict)
        now = _now_iso()

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")

                cached_snapshot = self._check_idempotency(conn, operation_id, payload_hash, norm_origin, repository, pr_number)
                if cached_snapshot is not None:
                    conn.execute("COMMIT")
                    return cached_snapshot

                current_rev = self._check_cas(conn, key, expected_ledger_revision)

                cursor = conn.execute(
                    "SELECT disposition FROM blockers WHERE blocker_id = ? AND namespace_key = ?",
                    (blocker_id, key),
                )
                row = cursor.fetchone()
                if row is None:
                    raise UnknownBlockerReferenceError(f"Blocker {blocker_id!r} not found in namespace {key}")
                current_disp = row[0]

                new_rev = current_rev + 1

                tid = f"trans_{uuid.uuid4().hex[:12]}"
                conn.execute(
                    """
                    INSERT INTO blocker_transitions (
                        transition_id, namespace_key, blocker_id,
                        from_disposition, to_disposition, evidence_availability,
                        evidence, transition_reason, reviewed_head_sha,
                        reviewed_base_sha, review_attempt_id,
                        requirement_manifest_revision, ledger_revision,
                        created_at, contract_rebinding_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        tid,
                        key,
                        blocker_id,
                        current_disp,
                        target_disposition.value,
                        EvidenceAvailability.KNOWN.value,
                        evidence,
                        transition_reason or f"Transition to {target_disposition.value}",
                        reviewed_head_sha,
                        reviewed_base_sha,
                        review_attempt_id,
                        requirement_manifest_revision,
                        new_rev,
                        now,
                    ),
                )

                conn.execute(
                    """
                    UPDATE blockers
                    SET disposition = ?, last_updated_at = ?, last_updated_revision = ?
                    WHERE blocker_id = ?
                    """,
                    (target_disposition.value, now, new_rev, blocker_id),
                )
                conn.execute(
                    "UPDATE namespaces SET ledger_revision = ?, updated_at = ? WHERE namespace_key = ?",
                    (new_rev, now, key),
                )
                conn.execute(
                    """
                    INSERT INTO operation_journal (
                        operation_id, namespace_key, payload_hash,
                        committed_revision, result_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (operation_id, key, payload_hash, new_rev, "OK", now),
                )

                if self._simulate_failure_before_commit:
                    raise BlockerPersistenceError("Simulated write failure before commit")

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

    def record_requirement_manifest_change(
        self,
        api_origin: str,
        repository: str,
        pr_number: int,
        operation_id: str,
        expected_ledger_revision: int,
        blocker_id: str,
        manifest_revision: str,
        reconciliation_need: str,
    ) -> BlockerLedgerSnapshot:
        """Record a versioned reconciliation need when the requirement manifest changes.

        Per AS-003: a manifest change is recorded as a versioned reconciliation need,
        not a silent scope rewrite.
        """
        self._check_db_integrity()
        norm_origin = normalize_api_origin(api_origin)
        key = _make_namespace_key(norm_origin, repository, pr_number)

        payload_dict = {
            "op": "record_requirement_manifest_change",
            "blocker_id": blocker_id,
            "manifest_revision": manifest_revision,
            "need": reconciliation_need,
        }
        payload_hash = self._hash_payload(payload_dict)
        now = _now_iso()

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")

                cached_snapshot = self._check_idempotency(conn, operation_id, payload_hash, norm_origin, repository, pr_number)
                if cached_snapshot is not None:
                    conn.execute("COMMIT")
                    return cached_snapshot

                current_rev = self._check_cas(conn, key, expected_ledger_revision)

                cursor = conn.execute(
                    "SELECT reconciliation_needs_json FROM blockers WHERE blocker_id = ? AND namespace_key = ?",
                    (blocker_id, key),
                )
                row = cursor.fetchone()
                if row is None:
                    raise UnknownBlockerReferenceError(f"Blocker {blocker_id!r} not found in namespace {key}")

                try:
                    needs = list(json.loads(row[0]))
                except (json.JSONDecodeError, TypeError):
                    needs = []

                need_entry = f"manifest:{manifest_revision} - {reconciliation_need}"
                if need_entry not in needs:
                    needs.append(need_entry)

                new_rev = current_rev + 1
                conn.execute(
                    """
                    UPDATE blockers
                    SET reconciliation_needs_json = ?, last_updated_at = ?, last_updated_revision = ?
                    WHERE blocker_id = ?
                    """,
                    (json.dumps(needs), now, new_rev, blocker_id),
                )
                conn.execute(
                    "UPDATE namespaces SET ledger_revision = ?, updated_at = ? WHERE namespace_key = ?",
                    (new_rev, now, key),
                )
                conn.execute(
                    """
                    INSERT INTO operation_journal (
                        operation_id, namespace_key, payload_hash,
                        committed_revision, result_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (operation_id, key, payload_hash, new_rev, "OK", now),
                )

                if self._simulate_failure_before_commit:
                    raise BlockerPersistenceError("Simulated write failure before commit")

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

    def record_contract_rebinding(
        self,
        api_origin: str,
        repository: str,
        pr_number: int,
        operation_id: str,
        expected_ledger_revision: int,
        blocker_id: str,
        revised_scope: CorrectionScope,
        reason: str,
        manifest_revision: str = "",
        reviewed_head_sha: str = "",
        reviewed_base_sha: str = "",
        review_attempt_id: str = "",
    ) -> BlockerLedgerSnapshot:
        """Explicitly record a contract-rebinding record.

        Per REQ-003: scope expansion or contraction requires an explicitly accepted
        contract-rebinding record, preserving the original accepted scope.
        """
        self._check_db_integrity()
        norm_origin = normalize_api_origin(api_origin)
        key = _make_namespace_key(norm_origin, repository, pr_number)

        payload_dict = {
            "op": "record_contract_rebinding",
            "blocker_id": blocker_id,
            "revised_scope": asdict(revised_scope),
            "reason": reason,
            "manifest": manifest_revision,
            "head": reviewed_head_sha,
            "base": reviewed_base_sha,
            "attempt": review_attempt_id,
        }
        payload_hash = self._hash_payload(payload_dict)
        now = _now_iso()

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")

                cached_snapshot = self._check_idempotency(conn, operation_id, payload_hash, norm_origin, repository, pr_number)
                if cached_snapshot is not None:
                    conn.execute("COMMIT")
                    return cached_snapshot

                current_rev = self._check_cas(conn, key, expected_ledger_revision)

                cursor = conn.execute(
                    "SELECT disposition FROM blockers WHERE blocker_id = ? AND namespace_key = ?",
                    (blocker_id, key),
                )
                row = cursor.fetchone()
                if row is None:
                    raise UnknownBlockerReferenceError(f"Blocker {blocker_id!r} not found in namespace {key}")
                curr_disp = row[0]

                new_rev = current_rev + 1

                rebinding_id = f"rebind_{uuid.uuid4().hex[:12]}"
                rebinding = ContractRebindingRecord(
                    rebinding_id=rebinding_id,
                    reason=reason,
                    revised_scope=revised_scope,
                    requirement_manifest_revision=manifest_revision,
                    reviewed_head_sha=reviewed_head_sha,
                    reviewed_base_sha=reviewed_base_sha,
                    review_attempt_id=review_attempt_id,
                    ledger_revision=new_rev,
                    created_at=now,
                )

                rebinding_json = json.dumps(asdict(rebinding), ensure_ascii=False)

                tid = f"trans_{uuid.uuid4().hex[:12]}"
                conn.execute(
                    """
                    INSERT INTO blocker_transitions (
                        transition_id, namespace_key, blocker_id,
                        from_disposition, to_disposition, evidence_availability,
                        evidence, transition_reason, reviewed_head_sha,
                        reviewed_base_sha, review_attempt_id,
                        requirement_manifest_revision, ledger_revision,
                        created_at, contract_rebinding_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tid,
                        key,
                        blocker_id,
                        curr_disp,
                        curr_disp,
                        EvidenceAvailability.KNOWN.value,
                        f"Contract rebinding: {reason}",
                        "Contract rebinding",
                        reviewed_head_sha,
                        reviewed_base_sha,
                        review_attempt_id,
                        manifest_revision,
                        new_rev,
                        now,
                        rebinding_json,
                    ),
                )

                conn.execute(
                    "UPDATE blockers SET last_updated_at = ?, last_updated_revision = ? WHERE blocker_id = ?",
                    (now, new_rev, blocker_id),
                )
                conn.execute(
                    "UPDATE namespaces SET ledger_revision = ?, updated_at = ? WHERE namespace_key = ?",
                    (new_rev, now, key),
                )
                conn.execute(
                    """
                    INSERT INTO operation_journal (
                        operation_id, namespace_key, payload_hash,
                        committed_revision, result_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (operation_id, key, payload_hash, new_rev, "OK", now),
                )

                if self._simulate_failure_before_commit:
                    raise BlockerPersistenceError("Simulated write failure before commit")

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

    def add_alias(
        self,
        api_origin: str,
        repository: str,
        pr_number: int,
        operation_id: str,
        expected_ledger_revision: int,
        blocker_id: str,
        alias_type: str,
        alias_value: str,
        concern_id: Optional[str] = None,
    ) -> BlockerLedgerSnapshot:
        """Attach a provenance-bearing alias to an existing blocker."""
        self._check_db_integrity()
        norm_origin = normalize_api_origin(api_origin)
        key = _make_namespace_key(norm_origin, repository, pr_number)

        payload_dict = {
            "op": "add_alias",
            "blocker_id": blocker_id,
            "alias_type": alias_type,
            "alias_value": alias_value,
            "concern_id": concern_id,
        }
        payload_hash = self._hash_payload(payload_dict)
        now = _now_iso()

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")

                cached_snapshot = self._check_idempotency(conn, operation_id, payload_hash, norm_origin, repository, pr_number)
                if cached_snapshot is not None:
                    conn.execute("COMMIT")
                    return cached_snapshot

                current_rev = self._check_cas(conn, key, expected_ledger_revision)

                cursor = conn.execute(
                    "SELECT 1 FROM blockers WHERE blocker_id = ? AND namespace_key = ?",
                    (blocker_id, key),
                )
                if cursor.fetchone() is None:
                    raise UnknownBlockerReferenceError(f"Blocker {blocker_id!r} not found in namespace {key}")

                new_rev = current_rev + 1
                aid = f"alias_{uuid.uuid4().hex[:12]}"
                conn.execute(
                    """
                    INSERT INTO blocker_aliases (
                        alias_id, namespace_key, blocker_id, alias_type,
                        alias_value, concern_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (aid, key, blocker_id, alias_type, alias_value, concern_id, now),
                )

                conn.execute(
                    "UPDATE namespaces SET ledger_revision = ?, updated_at = ? WHERE namespace_key = ?",
                    (new_rev, now, key),
                )
                conn.execute(
                    """
                    INSERT INTO operation_journal (
                        operation_id, namespace_key, payload_hash,
                        committed_revision, result_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (operation_id, key, payload_hash, new_rev, "OK", now),
                )

                if self._simulate_failure_before_commit:
                    raise BlockerPersistenceError("Simulated write failure before commit")

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

    def record_publication_intent(
        self,
        api_origin: str,
        repository: str,
        pr_number: int,
        intent_id: str,
        expected_ledger_revision: int,
        blocker_ids: Sequence[str],
        destination_repo: str,
        destination_pr: int,
        reviewed_head_sha: str,
        reviewed_base_sha: str = "",
        review_attempt_id: str = "",
        intended_payload_hash: str = "",
    ) -> PublicationIntentSnapshot:
        """Durably record a publication intent and acquire exclusive publication authority.

        Binds intent_id, destination repo/PR, reviewed head/base, and target blocker IDs.
        Enforces CAS ledger revision fence and rejects conflicting/concurrent intents (REQ-007).
        """
        self._check_db_integrity()
        norm_origin = normalize_api_origin(api_origin)
        key = _make_namespace_key(norm_origin, repository, pr_number)

        payload_dict = {
            "intent_id": intent_id,
            "blocker_ids": sorted(blocker_ids),
            "dest_repo": destination_repo,
            "dest_pr": destination_pr,
            "head": reviewed_head_sha,
            "base": reviewed_base_sha,
            "attempt": review_attempt_id,
        }
        computed_hash = intended_payload_hash or self._hash_payload(payload_dict)
        now = _now_iso()

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")

                cursor = conn.execute(
                    """
                    SELECT intent_id, namespace_key, destination_repo, destination_pr,
                           reviewed_head_sha, reviewed_base_sha, review_attempt_id,
                           payload_hash, blocker_ids_json, status, confirmed_roots_json,
                           failure_reason, created_at, updated_at
                    FROM publication_intents
                    WHERE intent_id = ? AND namespace_key = ?
                    """,
                    (intent_id, key),
                )
                row = cursor.fetchone()
                if row is not None:
                    existing_hash = row[7]
                    if existing_hash != computed_hash:
                        raise IdempotencyConflictError(f"Conflicting reuse of intent ID {intent_id!r} with different payload hash")
                    bids = tuple(json.loads(row[8]))
                    confirmed_roots_raw = json.loads(row[10])
                    confirmed_roots = tuple((str(b), int(c)) for b, c in confirmed_roots_raw)
                    conn.execute("COMMIT")
                    return PublicationIntentSnapshot(
                        intent_id=row[0],
                        namespace_key=row[1],
                        destination_repo=row[2],
                        destination_pr=row[3],
                        reviewed_head_sha=row[4],
                        reviewed_base_sha=row[5],
                        review_attempt_id=row[6],
                        payload_hash=row[7],
                        blocker_ids=bids,
                        status=row[9],
                        confirmed_roots=confirmed_roots,
                        failure_reason=row[11],
                        created_at=row[12],
                        updated_at=row[13],
                    )

                self._check_cas(conn, key, expected_ledger_revision)

                for bid in blocker_ids:
                    c = conn.execute(
                        "SELECT 1 FROM blockers WHERE blocker_id = ? AND namespace_key = ?",
                        (bid, key),
                    )
                    if c.fetchone() is None:
                        raise UnknownBlockerReferenceError(f"Blocker ID {bid!r} does not exist in namespace {key}")

                cursor = conn.execute(
                    "SELECT intent_id, blocker_ids_json FROM publication_intents WHERE namespace_key = ? AND status = 'PENDING'",
                    (key,),
                )
                for existing_id, bids_json in cursor.fetchall():
                    existing_bids = set(json.loads(bids_json))
                    overlap = existing_bids.intersection(set(blocker_ids))
                    if overlap and existing_id != intent_id:
                        raise PublicationContentionError(f"Exclusive publication authority for blocker(s) {sorted(overlap)} " f"is already held by intent {existing_id!r}")

                conn.execute(
                    """
                    INSERT INTO publication_intents (
                        intent_id, namespace_key, destination_repo, destination_pr,
                        reviewed_head_sha, reviewed_base_sha, review_attempt_id,
                        payload_hash, blocker_ids_json, status, confirmed_roots_json,
                        failure_reason, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', '[]', NULL, ?, ?)
                    """,
                    (
                        intent_id,
                        key,
                        destination_repo,
                        destination_pr,
                        reviewed_head_sha,
                        reviewed_base_sha,
                        review_attempt_id,
                        computed_hash,
                        json.dumps(list(blocker_ids)),
                        now,
                        now,
                    ),
                )

                conn.execute("COMMIT")
                return PublicationIntentSnapshot(
                    intent_id=intent_id,
                    namespace_key=key,
                    destination_repo=destination_repo,
                    destination_pr=destination_pr,
                    reviewed_head_sha=reviewed_head_sha,
                    reviewed_base_sha=reviewed_base_sha,
                    review_attempt_id=review_attempt_id,
                    payload_hash=computed_hash,
                    blocker_ids=tuple(blocker_ids),
                    status="PENDING",
                    confirmed_roots=(),
                    failure_reason=None,
                    created_at=now,
                    updated_at=now,
                )
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                conn.close()

    def confirm_publication_intent(
        self,
        api_origin: str,
        repository: str,
        pr_number: int,
        intent_id: str,
        confirmed_root_aliases: Sequence[tuple[str, int]],
        evidence: str = "",
    ) -> BlockerLedgerSnapshot:
        """Confirm a publication intent, recording verified root comment aliases (REQ-007, REQ-008)."""
        self._check_db_integrity()
        norm_origin = normalize_api_origin(api_origin)
        key = _make_namespace_key(norm_origin, repository, pr_number)
        now = _now_iso()

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")

                cursor = conn.execute(
                    """
                    SELECT status, blocker_ids_json, reviewed_head_sha, reviewed_base_sha, review_attempt_id
                    FROM publication_intents WHERE intent_id = ? AND namespace_key = ?
                    """,
                    (intent_id, key),
                )
                row = cursor.fetchone()
                if row is None:
                    raise BlockerPersistenceError(f"Publication intent {intent_id!r} not found in namespace {key}")

                cursor = conn.execute("SELECT ledger_revision FROM namespaces WHERE namespace_key = ?", (key,))
                rev_row = cursor.fetchone()
                current_rev = rev_row[0] if rev_row else 1
                new_rev = current_rev + 1

                confirmed_pairs: list[tuple[str, int]] = []
                for item in confirmed_root_aliases:
                    if isinstance(item, BlockerAlias):
                        bid = item.blocker_id
                        cid = int(item.alias_value)
                    else:
                        bid, cid = item
                    confirmed_pairs.append((bid, cid))
                    c = conn.execute(
                        """
                        SELECT 1 FROM blocker_aliases
                        WHERE namespace_key = ? AND blocker_id = ? AND alias_type = 'github_root_comment' AND alias_value = ?
                        """,
                        (key, bid, str(cid)),
                    )
                    if c.fetchone() is None:
                        aid = f"alias_{uuid.uuid4().hex[:12]}"
                        conn.execute(
                            """
                            INSERT INTO blocker_aliases (
                                alias_id, namespace_key, blocker_id, alias_type,
                                alias_value, concern_id, created_at
                            ) VALUES (?, ?, ?, 'github_root_comment', ?, NULL, ?)
                            """,
                            (aid, key, bid, str(cid), now),
                        )
                    conn.execute(
                        "UPDATE blockers SET last_updated_at = ?, last_updated_revision = ? WHERE blocker_id = ?",
                        (now, new_rev, bid),
                    )

                conn.execute(
                    """
                    UPDATE publication_intents
                    SET status = 'CONFIRMED', confirmed_roots_json = ?, updated_at = ?
                    WHERE intent_id = ? AND namespace_key = ?
                    """,
                    (json.dumps([[b, c] for b, c in confirmed_pairs]), now, intent_id, key),
                )

                conn.execute(
                    "UPDATE namespaces SET ledger_revision = ?, updated_at = ? WHERE namespace_key = ?",
                    (new_rev, now, key),
                )

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

    def reject_publication_intent(
        self,
        api_origin: str,
        repository: str,
        pr_number: int,
        intent_id: str,
        reason: str,
    ) -> None:
        """Mark a publication intent as definitively rejected (REQ-008)."""
        self._check_db_integrity()
        norm_origin = normalize_api_origin(api_origin)
        key = _make_namespace_key(norm_origin, repository, pr_number)
        now = _now_iso()

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    UPDATE publication_intents
                    SET status = 'REJECTED', failure_reason = ?, updated_at = ?
                    WHERE intent_id = ? AND namespace_key = ?
                    """,
                    (reason, now, intent_id, key),
                )
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                conn.close()

    def get_publication_intent(
        self,
        api_origin: str,
        repository: str,
        pr_number: int,
        intent_id: str,
    ) -> Optional[PublicationIntentSnapshot]:
        """Fetch publication intent snapshot if present."""
        self._check_db_integrity()
        norm_origin = normalize_api_origin(api_origin)
        key = _make_namespace_key(norm_origin, repository, pr_number)

        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    """
                    SELECT intent_id, namespace_key, destination_repo, destination_pr,
                           reviewed_head_sha, reviewed_base_sha, review_attempt_id,
                           payload_hash, blocker_ids_json, status, confirmed_roots_json,
                           failure_reason, created_at, updated_at
                    FROM publication_intents
                    WHERE intent_id = ? AND namespace_key = ?
                    """,
                    (intent_id, key),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                bids = tuple(json.loads(row[8]))
                confirmed_roots_raw = json.loads(row[10])
                confirmed_roots = tuple((str(b), int(c)) for b, c in confirmed_roots_raw)
                return PublicationIntentSnapshot(
                    intent_id=row[0],
                    namespace_key=row[1],
                    destination_repo=row[2],
                    destination_pr=row[3],
                    reviewed_head_sha=row[4],
                    reviewed_base_sha=row[5],
                    review_attempt_id=row[6],
                    payload_hash=row[7],
                    blocker_ids=bids,
                    status=row[9],
                    confirmed_roots=confirmed_roots,
                    failure_reason=row[11],
                    created_at=row[12],
                    updated_at=row[13],
                )
            finally:
                conn.close()

    def get_pending_publication_intents(
        self,
        api_origin: str,
        repository: str,
        pr_number: int,
    ) -> tuple[PublicationIntentSnapshot, ...]:
        """Return all PENDING publication intents for a PR namespace."""
        self._check_db_integrity()
        norm_origin = normalize_api_origin(api_origin)
        key = _make_namespace_key(norm_origin, repository, pr_number)

        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    """
                    SELECT intent_id, namespace_key, destination_repo, destination_pr,
                           reviewed_head_sha, reviewed_base_sha, review_attempt_id,
                           payload_hash, blocker_ids_json, status, confirmed_roots_json,
                           failure_reason, created_at, updated_at
                    FROM publication_intents
                    WHERE namespace_key = ? AND status = 'PENDING'
                    ORDER BY created_at ASC
                    """,
                    (key,),
                )
                results: list[PublicationIntentSnapshot] = []
                for row in cursor.fetchall():
                    bids = tuple(json.loads(row[8]))
                    confirmed_roots_raw = json.loads(row[10])
                    confirmed_roots = tuple((str(b), int(c)) for b, c in confirmed_roots_raw)
                    results.append(
                        PublicationIntentSnapshot(
                            intent_id=row[0],
                            namespace_key=row[1],
                            destination_repo=row[2],
                            destination_pr=row[3],
                            reviewed_head_sha=row[4],
                            reviewed_base_sha=row[5],
                            review_attempt_id=row[6],
                            payload_hash=row[7],
                            blocker_ids=bids,
                            status=row[9],
                            confirmed_roots=confirmed_roots,
                            failure_reason=row[11],
                            created_at=row[12],
                            updated_at=row[13],
                        )
                    )
                return tuple(results)
            finally:
                conn.close()
