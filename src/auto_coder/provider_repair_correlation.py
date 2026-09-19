"""Provider repair follow-up delivery and completion correlation adapters.

Implements the specification and requirements from GitHub Issue #2141:
Normalize actual provider follow-up delivery and completion into causally
correlated corrective-generation evidence (Stage S7 of the convergent PR
review tracking family #2134, consuming #2139 and #2140).

This module coordinates repair delivery and terminal correction observation
across Auto-Coder's supported execution boundaries:
- Local implementation boundary (in-process / workspace tool execution)
- Jules session follow-up boundary (REST session API)
- Codex Cloud follow-up boundary (WHAM task turns)
- Claude Routine follow-up boundary (session API / cloud CLI)
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

from .bounded_repair_bundle import RepairHandoffBundle
from .durable_repair_allowance import (
    CompletionAvailability,
    CorrectiveGenerationBundle,
    DeliveryOutcome,
    GenerationLifecycleState,
    RepairAllowanceLedger,
    RepairAllowanceLedgerSnapshot,
    ValidationAvailability,
    ValidationObservation,
)
from .logger_config import get_logger
from .util.github_request_outcome import normalize_api_origin

logger = get_logger(__name__)

SUPPORTED_CORRELATION_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Enums (REQ-001, REQ-003, REQ-004, REQ-005)
# ---------------------------------------------------------------------------


class ProviderType(str, Enum):
    """Supported repair execution and follow-up provider boundaries."""

    LOCAL = "local"
    JULES = "jules"
    CODEX_CLOUD = "codex-cloud"
    CLAUDE_ROUTINE = "claude-routine"


class DeliveryStatus(str, Enum):
    """Normalized delivery status from the actual client/transport boundary (REQ-003)."""

    CONFIRMED = "CONFIRMED"
    DEFINITE_NON_DELIVERY = "DEFINITE_NON_DELIVERY"
    INDETERMINATE = "INDETERMINATE"


class CorrectionCompletionStatus(str, Enum):
    """Normalized status of observed corrective work completion (REQ-004, REQ-005)."""

    COMPLETED = "COMPLETED"
    UNAVAILABLE = "UNAVAILABLE"
    AMBIGUOUS = "AMBIGUOUS"


class TicketStatus(str, Enum):
    """Lifecycle status of a durably bound admission ticket (REQ-002)."""

    BOUND = "BOUND"
    DELIVERED = "DELIVERED"
    NON_DELIVERY = "NON_DELIVERY"
    INDETERMINATE = "INDETERMINATE"
    SUPERSEDED = "SUPERSEDED"


# ---------------------------------------------------------------------------
# Exceptions (REQ-002, REQ-003, REQ-008)
# ---------------------------------------------------------------------------


class ProviderRepairCorrelationError(RuntimeError):
    """Base exception for provider repair correlation errors."""


class CorrelationUnavailableError(ProviderRepairCorrelationError):
    """Storage is unreadable, corrupt, unsupported version, or missing retained state."""


class AdmissionTicketError(ProviderRepairCorrelationError):
    """Base exception for admission ticket binding errors."""


class AdmissionTicketPersistenceError(AdmissionTicketError):
    """Raised when an admission ticket or its baseline cannot be durably persisted."""


class AdmissionRefusalError(ProviderRepairCorrelationError):
    """Raised when an admission ticket cannot be granted due to policy or state."""


class StaleTicketEpochError(ProviderRepairCorrelationError):
    """Raised when compare-and-set detects a stale namespace epoch."""


class OwnerMismatchError(ProviderRepairCorrelationError):
    """Raised when the authoritative owner task or invocation changed."""


class AmbiguousObservationError(ProviderRepairCorrelationError):
    """Raised when native evidence is interleaved, conflicting, or inconclusive."""


# ---------------------------------------------------------------------------
# Domain Dataclasses (REQ-001, REQ-002, REQ-004, REQ-007)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderBaselineEvidence:
    """Pre-send baseline evidence captured from provider or local state (REQ-001, REQ-002)."""

    provider: str = ""
    owner_id: str = ""
    baseline_token: str = ""
    baseline_details_json: str = "{}"
    observed_at: str = ""


@dataclass(frozen=True)
class AdmissionTicket:
    """Durably bound ticket required before crossing outbound repair boundary (REQ-002)."""

    ticket_id: str = ""
    generation_id: str = ""
    bundle_id: str = ""
    api_origin: str = "https://api.github.com"
    repository: str = ""
    pr_number: int = 0
    head_sha: str = ""
    provider: str = ""
    owner_id: str = ""
    expected_epoch: int = 0
    baseline: ProviderBaselineEvidence = field(default_factory=ProviderBaselineEvidence)
    status: TicketStatus = TicketStatus.BOUND
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class NormalizedDeliveryObservation:
    """Normalized observation of a delivery attempt at provider boundary (REQ-001, REQ-003)."""

    observation_id: str = ""
    ticket_id: str = ""
    generation_id: str = ""
    bundle_id: str = ""
    api_origin: str = "https://api.github.com"
    repository: str = ""
    pr_number: int = 0
    provider: str = ""
    owner_id: str = ""
    delivery_status: DeliveryStatus = DeliveryStatus.INDETERMINATE
    delivery_operation_identity: str = ""
    receipt_identity: str = ""
    baseline_evidence: str = ""
    evidence: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class NormalizedCorrectionObservation:
    """Normalized observation of terminal corrective work completion (REQ-001, REQ-004, REQ-005)."""

    observation_id: str = ""
    ticket_id: str = ""
    generation_id: str = ""
    bundle_id: str = ""
    api_origin: str = "https://api.github.com"
    repository: str = ""
    pr_number: int = 0
    provider: str = ""
    owner_id: str = ""
    completion_status: CorrectionCompletionStatus = CorrectionCompletionStatus.UNAVAILABLE
    provider_native_ref: str = ""
    code_changed: Optional[bool] = None
    is_no_change_result: bool = False
    evidence: str = ""
    observed_at: str = ""


@dataclass(frozen=True)
class ValidationEvidenceBinding:
    """Durably captured completion evidence bound to validation input (REQ-007)."""

    binding_id: str = ""
    generation_id: str = ""
    bundle_id: str = ""
    api_origin: str = "https://api.github.com"
    repository: str = ""
    pr_number: int = 0
    head_sha: str = ""
    provider: str = ""
    owner_id: str = ""
    provider_native_ref: str = ""
    completion_seq: int = 0
    code_changed: Optional[bool] = None
    captured_at: str = ""
    is_settled: bool = False


# ---------------------------------------------------------------------------
# Storage & Correlation Store Implementation (REQ-002, REQ-008)
# ---------------------------------------------------------------------------


def default_correlation_db_path() -> Path:
    return Path.home() / ".auto-coder" / "provider_repair_correlation.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_correlation_key(api_origin: str, repository: str, pr_number: int) -> str:
    norm_origin = normalize_api_origin(api_origin)
    return f"{norm_origin}::{repository}::{pr_number}"


class ProviderRepairCorrelationStore:
    """Durable, transactional storage for admission tickets and correlation observations.

    Backed by SQLite in WAL mode with immediate transactions, optimistic locking,
    and fail-closed persistence guarantees (REQ-002, REQ-008).
    """

    _lock = threading.RLock()

    def __init__(self, db_path: Optional[Path] = None):
        self._db_path = Path(db_path) if db_path is not None else default_correlation_db_path()
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
            raise CorrelationUnavailableError(f"Correlation database is corrupt or unreadable: {exc}") from exc
        except OSError as exc:
            raise CorrelationUnavailableError(f"Correlation database cannot be accessed: {exc}") from exc

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            cursor = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'")
            row = cursor.fetchone()
            if row is None:
                conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?)", (str(SUPPORTED_CORRELATION_SCHEMA_VERSION),))
            else:
                try:
                    version = int(row[0])
                except ValueError as exc:
                    raise CorrelationUnavailableError(f"Invalid schema version in correlation metadata: {row[0]}") from exc
                if version > SUPPORTED_CORRELATION_SCHEMA_VERSION:
                    raise CorrelationUnavailableError(f"Unsupported schema version {version} (max: {SUPPORTED_CORRELATION_SCHEMA_VERSION})")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS admission_tickets (
                    ticket_id TEXT PRIMARY KEY,
                    correlation_key TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    bundle_id TEXT NOT NULL,
                    api_origin TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    pr_number INTEGER NOT NULL,
                    head_sha TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    expected_epoch INTEGER NOT NULL,
                    baseline_token TEXT NOT NULL,
                    baseline_details_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS delivery_observations (
                    observation_id TEXT PRIMARY KEY,
                    ticket_id TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    bundle_id TEXT NOT NULL,
                    correlation_key TEXT NOT NULL,
                    api_origin TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    pr_number INTEGER NOT NULL,
                    provider TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    delivery_status TEXT NOT NULL,
                    delivery_operation_identity TEXT NOT NULL,
                    receipt_identity TEXT NOT NULL,
                    baseline_evidence TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS correction_observations (
                    observation_id TEXT PRIMARY KEY,
                    ticket_id TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    bundle_id TEXT NOT NULL,
                    correlation_key TEXT NOT NULL,
                    api_origin TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    pr_number INTEGER NOT NULL,
                    provider TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    completion_status TEXT NOT NULL,
                    provider_native_ref TEXT NOT NULL,
                    code_changed INTEGER,
                    is_no_change_result INTEGER NOT NULL DEFAULT 0,
                    evidence TEXT NOT NULL,
                    observed_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS validation_bindings (
                    binding_id TEXT PRIMARY KEY,
                    generation_id TEXT NOT NULL,
                    bundle_id TEXT NOT NULL,
                    correlation_key TEXT NOT NULL,
                    api_origin TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    pr_number INTEGER NOT NULL,
                    head_sha TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    provider_native_ref TEXT NOT NULL,
                    completion_seq INTEGER NOT NULL,
                    code_changed INTEGER,
                    captured_at TEXT NOT NULL,
                    is_settled INTEGER NOT NULL DEFAULT 0
                )
                """
            )
        except sqlite3.DatabaseError as exc:
            raise CorrelationUnavailableError(f"Correlation schema verification failed: {exc}") from exc

    def _check_db_integrity(self) -> None:
        if not self._db_path.exists():
            return
        try:
            with sqlite3.connect(str(self._db_path), timeout=5.0) as test_conn:
                cursor = test_conn.execute("PRAGMA quick_check")
                row = cursor.fetchone()
                if row is None or row[0] != "ok":
                    raise CorrelationUnavailableError(f"Correlation quick_check failed: {row}")
        except sqlite3.DatabaseError as exc:
            raise CorrelationUnavailableError(f"Correlation database corrupt or unreadable: {exc}") from exc

    def save_admission_ticket(self, ticket: AdmissionTicket) -> None:
        """Durably persist an admission ticket before crossing an outbound boundary (REQ-002)."""
        self._check_db_integrity()
        norm_origin = normalize_api_origin(ticket.api_origin)
        key = _make_correlation_key(norm_origin, ticket.repository, ticket.pr_number)

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                if self._simulate_failure_before_commit:
                    raise AdmissionTicketPersistenceError("Simulated failure persisting admission ticket")
                conn.execute(
                    """
                    INSERT INTO admission_tickets (
                        ticket_id, correlation_key, generation_id, bundle_id,
                        api_origin, repository, pr_number, head_sha,
                        provider, owner_id, expected_epoch,
                        baseline_token, baseline_details_json,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(ticket_id) DO UPDATE SET
                        status = excluded.status,
                        updated_at = excluded.updated_at
                    """,
                    (
                        ticket.ticket_id,
                        key,
                        ticket.generation_id,
                        ticket.bundle_id,
                        norm_origin,
                        ticket.repository,
                        ticket.pr_number,
                        ticket.head_sha,
                        ticket.provider,
                        ticket.owner_id,
                        ticket.expected_epoch,
                        ticket.baseline.baseline_token,
                        ticket.baseline.baseline_details_json,
                        ticket.status.value,
                        ticket.created_at,
                        ticket.updated_at,
                    ),
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

    def get_admission_ticket(self, ticket_id: str) -> Optional[AdmissionTicket]:
        self._check_db_integrity()
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    """
                    SELECT ticket_id, generation_id, bundle_id, api_origin,
                           repository, pr_number, head_sha, provider, owner_id,
                           expected_epoch, baseline_token, baseline_details_json,
                           status, created_at, updated_at
                    FROM admission_tickets WHERE ticket_id = ?
                    """,
                    (ticket_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                return AdmissionTicket(
                    ticket_id=row[0],
                    generation_id=row[1],
                    bundle_id=row[2],
                    api_origin=row[3],
                    repository=row[4],
                    pr_number=row[5],
                    head_sha=row[6],
                    provider=row[7],
                    owner_id=row[8],
                    expected_epoch=row[9],
                    baseline=ProviderBaselineEvidence(
                        provider=row[7],
                        owner_id=row[8],
                        baseline_token=row[10],
                        baseline_details_json=row[11],
                        observed_at=row[13],
                    ),
                    status=TicketStatus(row[12]),
                    created_at=row[13],
                    updated_at=row[14],
                )
            finally:
                conn.close()

    def get_ticket_for_generation(self, generation_id: str) -> Optional[AdmissionTicket]:
        self._check_db_integrity()
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    """
                    SELECT ticket_id, generation_id, bundle_id, api_origin,
                           repository, pr_number, head_sha, provider, owner_id,
                           expected_epoch, baseline_token, baseline_details_json,
                           status, created_at, updated_at
                    FROM admission_tickets WHERE generation_id = ?
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (generation_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                return AdmissionTicket(
                    ticket_id=row[0],
                    generation_id=row[1],
                    bundle_id=row[2],
                    api_origin=row[3],
                    repository=row[4],
                    pr_number=row[5],
                    head_sha=row[6],
                    provider=row[7],
                    owner_id=row[8],
                    expected_epoch=row[9],
                    baseline=ProviderBaselineEvidence(
                        provider=row[7],
                        owner_id=row[8],
                        baseline_token=row[10],
                        baseline_details_json=row[11],
                        observed_at=row[13],
                    ),
                    status=TicketStatus(row[12]),
                    created_at=row[13],
                    updated_at=row[14],
                )
            finally:
                conn.close()

    def save_delivery_observation(self, obs: NormalizedDeliveryObservation) -> None:
        self._check_db_integrity()
        norm_origin = normalize_api_origin(obs.api_origin)
        key = _make_correlation_key(norm_origin, obs.repository, obs.pr_number)

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                if self._simulate_failure_before_commit:
                    raise ProviderRepairCorrelationError("Simulated failure persisting delivery observation")
                conn.execute(
                    """
                    INSERT INTO delivery_observations (
                        observation_id, ticket_id, generation_id, bundle_id,
                        correlation_key, api_origin, repository, pr_number,
                        provider, owner_id, delivery_status,
                        delivery_operation_identity, receipt_identity,
                        baseline_evidence, evidence, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        obs.observation_id,
                        obs.ticket_id,
                        obs.generation_id,
                        obs.bundle_id,
                        key,
                        norm_origin,
                        obs.repository,
                        obs.pr_number,
                        obs.provider,
                        obs.owner_id,
                        obs.delivery_status.value,
                        obs.delivery_operation_identity,
                        obs.receipt_identity,
                        obs.baseline_evidence,
                        obs.evidence,
                        obs.created_at,
                    ),
                )
                ticket_status = TicketStatus.DELIVERED if obs.delivery_status == DeliveryStatus.CONFIRMED else (TicketStatus.NON_DELIVERY if obs.delivery_status == DeliveryStatus.DEFINITE_NON_DELIVERY else TicketStatus.INDETERMINATE)
                conn.execute(
                    "UPDATE admission_tickets SET status = ?, updated_at = ? WHERE ticket_id = ?",
                    (ticket_status.value, obs.created_at, obs.ticket_id),
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

    def save_correction_observation(self, obs: NormalizedCorrectionObservation) -> None:
        self._check_db_integrity()
        norm_origin = normalize_api_origin(obs.api_origin)
        key = _make_correlation_key(norm_origin, obs.repository, obs.pr_number)

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                if self._simulate_failure_before_commit:
                    raise ProviderRepairCorrelationError("Simulated failure persisting correction observation")
                code_changed_int = None if obs.code_changed is None else (1 if obs.code_changed else 0)
                conn.execute(
                    """
                    INSERT INTO correction_observations (
                        observation_id, ticket_id, generation_id, bundle_id,
                        correlation_key, api_origin, repository, pr_number,
                        provider, owner_id, completion_status,
                        provider_native_ref, code_changed, is_no_change_result,
                        evidence, observed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        obs.observation_id,
                        obs.ticket_id,
                        obs.generation_id,
                        obs.bundle_id,
                        key,
                        norm_origin,
                        obs.repository,
                        obs.pr_number,
                        obs.provider,
                        obs.owner_id,
                        obs.completion_status.value,
                        obs.provider_native_ref,
                        code_changed_int,
                        1 if obs.is_no_change_result else 0,
                        obs.evidence,
                        obs.observed_at,
                    ),
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

    def get_correction_observations_for_generation(self, generation_id: str) -> tuple[NormalizedCorrectionObservation, ...]:
        self._check_db_integrity()
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    """
                    SELECT observation_id, ticket_id, generation_id, bundle_id,
                           api_origin, repository, pr_number, provider, owner_id,
                           completion_status, provider_native_ref, code_changed,
                           is_no_change_result, evidence, observed_at
                    FROM correction_observations WHERE generation_id = ?
                    ORDER BY observed_at ASC
                    """,
                    (generation_id,),
                )
                results = []
                for row in cursor.fetchall():
                    code_changed = None if row[11] is None else bool(row[11])
                    results.append(
                        NormalizedCorrectionObservation(
                            observation_id=row[0],
                            ticket_id=row[1],
                            generation_id=row[2],
                            bundle_id=row[3],
                            api_origin=row[4],
                            repository=row[5],
                            pr_number=row[6],
                            provider=row[7],
                            owner_id=row[8],
                            completion_status=CorrectionCompletionStatus(row[9]),
                            provider_native_ref=row[10],
                            code_changed=code_changed,
                            is_no_change_result=bool(row[12]),
                            evidence=row[13],
                            observed_at=row[14],
                        )
                    )
                return tuple(results)
            finally:
                conn.close()

    def save_validation_binding(self, binding: ValidationEvidenceBinding) -> None:
        self._check_db_integrity()
        norm_origin = normalize_api_origin(binding.api_origin)
        key = _make_correlation_key(norm_origin, binding.repository, binding.pr_number)

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                if self._simulate_failure_before_commit:
                    raise ProviderRepairCorrelationError("Simulated failure persisting validation binding")
                code_changed_int = None if binding.code_changed is None else (1 if binding.code_changed else 0)
                conn.execute(
                    """
                    INSERT INTO validation_bindings (
                        binding_id, generation_id, bundle_id, correlation_key,
                        api_origin, repository, pr_number, head_sha, provider,
                        owner_id, provider_native_ref, completion_seq,
                        code_changed, captured_at, is_settled
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(binding_id) DO UPDATE SET
                        is_settled = excluded.is_settled
                    """,
                    (
                        binding.binding_id,
                        binding.generation_id,
                        binding.bundle_id,
                        key,
                        norm_origin,
                        binding.repository,
                        binding.pr_number,
                        binding.head_sha,
                        binding.provider,
                        binding.owner_id,
                        binding.provider_native_ref,
                        binding.completion_seq,
                        code_changed_int,
                        binding.captured_at,
                        1 if binding.is_settled else 0,
                    ),
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

    def get_validation_binding(self, binding_id: str) -> Optional[ValidationEvidenceBinding]:
        self._check_db_integrity()
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    """
                    SELECT binding_id, generation_id, bundle_id, api_origin,
                           repository, pr_number, head_sha, provider, owner_id,
                           provider_native_ref, completion_seq, code_changed,
                           captured_at, is_settled
                    FROM validation_bindings WHERE binding_id = ?
                    """,
                    (binding_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                code_changed = None if row[11] is None else bool(row[11])
                return ValidationEvidenceBinding(
                    binding_id=row[0],
                    generation_id=row[1],
                    bundle_id=row[2],
                    api_origin=row[3],
                    repository=row[4],
                    pr_number=row[5],
                    head_sha=row[6],
                    provider=row[7],
                    owner_id=row[8],
                    provider_native_ref=row[9],
                    completion_seq=row[10],
                    code_changed=code_changed,
                    captured_at=row[12],
                    is_settled=bool(row[13]),
                )
            finally:
                conn.close()


# ---------------------------------------------------------------------------
# Provider Adapters (REQ-001, REQ-004, REQ-005, REQ-010, REQ-011)
# ---------------------------------------------------------------------------


class BaseProviderRepairAdapter:
    """Base adapter protocol for provider repair boundaries."""

    def capture_pre_send_baseline(self, target_owner_id: str, client: Optional[object] = None) -> ProviderBaselineEvidence:
        raise NotImplementedError

    def send_followup(self, ticket: AdmissionTicket, message: str, client: Optional[object] = None) -> tuple[DeliveryStatus, str, str]:
        """Dispatch repair follow-up, returning (DeliveryStatus, delivery_operation_identity, receipt_identity)."""
        raise NotImplementedError

    def observe_completion(
        self,
        ticket: AdmissionTicket,
        client: Optional[object] = None,
    ) -> tuple[CorrectionCompletionStatus, str, Optional[bool], bool, str]:
        """Observe terminal correction, returning (status, native_ref, code_changed, is_no_change, evidence)."""
        raise NotImplementedError


class CodexCloudRepairAdapter(BaseProviderRepairAdapter):
    """Adapter for Codex Cloud follow-up boundary using WHAM assistant turns (REQ-004, REQ-010)."""

    def capture_pre_send_baseline(self, target_owner_id: str, client: Optional[object] = None) -> ProviderBaselineEvidence:
        latest_turn_id = ""
        wham = getattr(client, "wham_client", None)
        if wham is not None and hasattr(wham, "resolve_latest_assistant_turn"):
            try:
                latest_turn_id = wham.resolve_latest_assistant_turn(target_owner_id) or ""
            except Exception as exc:
                logger.warning(f"Could not resolve pre-send assistant turn for Codex Cloud task {target_owner_id}: {exc}")
        details = json.dumps({"latest_assistant_turn_id": latest_turn_id})
        token = f"codex_pre_send_turn:{latest_turn_id}" if latest_turn_id else f"codex_task_baseline:{target_owner_id}"
        return ProviderBaselineEvidence(
            provider=ProviderType.CODEX_CLOUD.value,
            owner_id=target_owner_id,
            baseline_token=token,
            baseline_details_json=details,
            observed_at=_now_iso(),
        )

    def send_followup(self, ticket: AdmissionTicket, message: str, client: Optional[object] = None) -> tuple[DeliveryStatus, str, str]:
        op_id = f"codex_deliv_{ticket.generation_id}"
        if client is None:
            return DeliveryStatus.INDETERMINATE, op_id, ""

        # Check pre-send quota if supported by client or config
        if hasattr(client, "is_quota_insufficient") and callable(client.is_quota_insufficient):
            if client.is_quota_insufficient():
                return DeliveryStatus.DEFINITE_NON_DELIVERY, op_id, ""

        logical_identities = (f"{ticket.bundle_id}:generation:{ticket.generation_id}",)
        try:
            send_fn = getattr(client, "send_followup", None)
            if not callable(send_fn):
                return DeliveryStatus.INDETERMINATE, op_id, ""

            # CodexCloudClient supports logical_identities parameter
            import inspect

            sig = inspect.signature(send_fn)
            if "logical_identities" in sig.parameters:
                accepted = send_fn(ticket.owner_id, message, logical_identities=logical_identities)
            else:
                accepted = send_fn(ticket.owner_id, message)

            if accepted:
                receipt = f"codex_receipt_{hashlib.sha256(f'{ticket.owner_id}:{ticket.generation_id}'.encode()).hexdigest()[:16]}"
                return DeliveryStatus.CONFIRMED, op_id, receipt

            # Generic False without transport certainty remains indeterminate
            return DeliveryStatus.INDETERMINATE, op_id, ""
        except Exception as exc:
            # Check for definite pre-send quota errors
            from .exceptions import AutoCoderUsageLimitError

            if isinstance(exc, AutoCoderUsageLimitError) or "usage limit" in str(exc).lower() or "quota" in str(exc).lower():
                return DeliveryStatus.DEFINITE_NON_DELIVERY, op_id, ""
            logger.warning(f"Codex Cloud follow-up transport exception for task {ticket.owner_id}: {exc}")
            return DeliveryStatus.INDETERMINATE, op_id, ""

    def observe_completion(
        self,
        ticket: AdmissionTicket,
        client: Optional[object] = None,
    ) -> tuple[CorrectionCompletionStatus, str, Optional[bool], bool, str]:
        if client is None:
            return CorrectionCompletionStatus.UNAVAILABLE, "", None, False, "No client provided"

        wham = getattr(client, "wham_client", None)
        if wham is None:
            return CorrectionCompletionStatus.UNAVAILABLE, "", None, False, "No WHAM client available"

        try:
            baseline_details = json.loads(ticket.baseline.baseline_details_json)
            pre_send_turn_id = baseline_details.get("latest_assistant_turn_id", "")
        except Exception:
            pre_send_turn_id = ""

        # Inspect task turns for causal ordering and intervening requests (REQ-004, AS-003)
        get_turns = getattr(wham, "get_task_turns", None)
        if callable(get_turns):
            turns = get_turns(ticket.owner_id)
            if not isinstance(turns, list):
                return CorrectionCompletionStatus.UNAVAILABLE, "", None, False, "Invalid turns response"

            # Check for competing intervening manual/user requests after pre_send_turn_id
            seen_baseline = not bool(pre_send_turn_id)
            user_turns_after = []
            assistant_turns_after = []
            for turn in turns:
                t_id = getattr(turn, "id", "")
                role = getattr(turn, "role", "")
                status = getattr(turn, "status", "")

                if not seen_baseline:
                    if t_id == pre_send_turn_id:
                        seen_baseline = True
                    continue

                if role == "user":
                    user_turns_after.append(turn)
                elif role == "assistant":
                    assistant_turns_after.append(turn)

            # More than one user turn means an intervening request was sent (AS-003)
            if len(user_turns_after) > 1:
                return CorrectionCompletionStatus.AMBIGUOUS, "", None, False, "Competing intervening user request detected in session"

            if not assistant_turns_after:
                return CorrectionCompletionStatus.UNAVAILABLE, "", None, False, "No assistant turn observed after baseline"

            latest_assistant = assistant_turns_after[-1]
            if getattr(latest_assistant, "status", "") != "completed":
                return CorrectionCompletionStatus.UNAVAILABLE, "", None, False, f"Assistant turn is not completed (status: {latest_assistant.status})"

            # Positive terminal completion
            turn_id = getattr(latest_assistant, "id", "")
            turn_content = str(getattr(latest_assistant, "content", "") or getattr(latest_assistant, "text", "") or "")
            is_no_change = "CANNOT_FIX" in turn_content or "no changes" in turn_content.lower() or "unable to fix" in turn_content.lower()
            code_changed = False if is_no_change else True
            native_ref = f"completed_assistant_turn:{turn_id}"
            return CorrectionCompletionStatus.COMPLETED, native_ref, code_changed, is_no_change, f"Positively correlated completed turn {turn_id}"

        # Fallback to resolve_completed_assistant_turn_after
        resolve_completed = getattr(wham, "resolve_completed_assistant_turn_after", None)
        if callable(resolve_completed):
            completed_turn = resolve_completed(ticket.owner_id, pre_send_turn_id)
            if completed_turn:
                native_ref = f"completed_assistant_turn:{completed_turn}"
                return CorrectionCompletionStatus.COMPLETED, native_ref, True, False, f"Completed turn {completed_turn}"

        return CorrectionCompletionStatus.UNAVAILABLE, "", None, False, "No completed assistant turn after baseline"


class JulesRepairAdapter(BaseProviderRepairAdapter):
    """Adapter for Google Jules session follow-up boundary (REQ-004, REQ-005, REQ-010)."""

    def capture_pre_send_baseline(self, target_owner_id: str, client: Optional[object] = None) -> ProviderBaselineEvidence:
        update_time = ""
        state = ""
        msg_count = 0
        if client is not None and hasattr(client, "get_session"):
            try:
                session = client.get_session(target_owner_id)
                if isinstance(session, dict):
                    update_time = str(session.get("updateTime") or "")
                    state = str(session.get("state") or "")
                    msg_count = len(session.get("messages") or []) if isinstance(session.get("messages"), list) else 0
            except Exception as exc:
                logger.warning(f"Could not capture pre-send baseline for Jules session {target_owner_id}: {exc}")
        details = json.dumps({"update_time": update_time, "state": state, "msg_count": msg_count})
        token = f"jules_baseline:{target_owner_id}:{update_time}"
        return ProviderBaselineEvidence(
            provider=ProviderType.JULES.value,
            owner_id=target_owner_id,
            baseline_token=token,
            baseline_details_json=details,
            observed_at=_now_iso(),
        )

    def send_followup(self, ticket: AdmissionTicket, message: str, client: Optional[object] = None) -> tuple[DeliveryStatus, str, str]:
        op_id = f"jules_deliv_{ticket.generation_id}"
        if client is None:
            return DeliveryStatus.INDETERMINATE, op_id, ""

        try:
            send_fn = getattr(client, "send_followup", None)
            if not callable(send_fn):
                return DeliveryStatus.INDETERMINATE, op_id, ""
            accepted = send_fn(ticket.owner_id, message)
            if accepted:
                receipt = f"jules_receipt_{hashlib.sha256(f'{ticket.owner_id}:{ticket.generation_id}'.encode()).hexdigest()[:16]}"
                return DeliveryStatus.CONFIRMED, op_id, receipt
            return DeliveryStatus.INDETERMINATE, op_id, ""
        except Exception as exc:
            from .exceptions import AutoCoderUsageLimitError

            if isinstance(exc, AutoCoderUsageLimitError) or "quota" in str(exc).lower() or "429" in str(exc):
                return DeliveryStatus.DEFINITE_NON_DELIVERY, op_id, ""
            logger.warning(f"Jules follow-up transport exception for session {ticket.owner_id}: {exc}")
            return DeliveryStatus.INDETERMINATE, op_id, ""

    def observe_completion(
        self,
        ticket: AdmissionTicket,
        client: Optional[object] = None,
    ) -> tuple[CorrectionCompletionStatus, str, Optional[bool], bool, str]:
        if client is None:
            return CorrectionCompletionStatus.UNAVAILABLE, "", None, False, "No client provided"

        session_fn = getattr(client, "get_session", None)
        if not callable(session_fn):
            return CorrectionCompletionStatus.UNAVAILABLE, "", None, False, "No get_session on Jules client"

        try:
            session = session_fn(ticket.owner_id)
        except Exception as exc:
            return CorrectionCompletionStatus.UNAVAILABLE, "", None, False, f"Could not fetch Jules session: {exc}"

        if not isinstance(session, dict):
            return CorrectionCompletionStatus.UNAVAILABLE, "", None, False, "Invalid session response"

        # Check for task mismatch (REQ-006, AS-002)
        sess_name = session.get("name", "")
        sess_id = sess_name.split("/")[-1] if sess_name else session.get("id", "")
        if sess_id and sess_id != ticket.owner_id:
            return CorrectionCompletionStatus.UNAVAILABLE, "", None, False, f"Session ID mismatch: {sess_id} != {ticket.owner_id}"

        raw_state = str(session.get("state") or "")
        # REQ-005: PAUSED or AWAITING_USER_FEEDBACK or changed updated-at alone is NOT completed correction!
        if raw_state in ("AWAITING_PLAN_APPROVAL", "AWAITING_USER_FEEDBACK", "AWAITING_COMMENT", "AWAITING_COMMENTS") or raw_state.startswith("AWAITING_"):
            return CorrectionCompletionStatus.UNAVAILABLE, "", None, False, f"Jules task is paused waiting for input ({raw_state})"

        if raw_state in ("IN_PROGRESS", "QUEUED"):
            return CorrectionCompletionStatus.UNAVAILABLE, "", None, False, f"Jules task is still running ({raw_state})"

        current_update_time = str(session.get("updateTime") or "")
        try:
            baseline_details = json.loads(ticket.baseline.baseline_details_json)
            baseline_update_time = baseline_details.get("update_time", "")
            baseline_msg_count = baseline_details.get("msg_count", 0)
        except Exception:
            baseline_update_time = ""
            baseline_msg_count = 0

        # REQ-004: Old terminal state or unchanged timestamp is insufficient (AS-002)
        if current_update_time and baseline_update_time and current_update_time <= baseline_update_time:
            return CorrectionCompletionStatus.UNAVAILABLE, "", None, False, "Session updateTime has not advanced past pre-send baseline"

        # Intervening competing user request detection (AS-003)
        messages = session.get("messages")
        if isinstance(messages, list) and baseline_msg_count > 0:
            user_msgs_after = [m for m in messages[baseline_msg_count:] if isinstance(m, dict) and m.get("role") in ("user", "human")]
            if len(user_msgs_after) > 1:
                return CorrectionCompletionStatus.AMBIGUOUS, "", None, False, "Intervening competing user message detected in Jules session"

        if raw_state == "COMPLETED":
            outputs = session.get("outputs", {})
            pr = None
            if isinstance(outputs, dict):
                pr = outputs.get("pullRequest") or outputs.get("pull_request")
            output_msg = str(session.get("outputMessage") or session.get("result") or "")
            is_no_change = "CANNOT_FIX" in output_msg or "no changes" in output_msg.lower() or "unable to fix" in output_msg.lower()
            code_changed = False if is_no_change else (True if pr else True)
            native_ref = f"jules_completed_session:{ticket.owner_id}:{current_update_time}"
            return CorrectionCompletionStatus.COMPLETED, native_ref, code_changed, is_no_change, f"Jules session {ticket.owner_id} completed"

        return CorrectionCompletionStatus.UNAVAILABLE, "", None, False, f"Jules session state is {raw_state}"


class ClaudeRoutineRepairAdapter(BaseProviderRepairAdapter):
    """Adapter for Claude Routine follow-up boundary (REQ-004, REQ-005, REQ-010)."""

    def capture_pre_send_baseline(self, target_owner_id: str, client: Optional[object] = None) -> ProviderBaselineEvidence:
        updated_at = ""
        state_val = ""
        if client is not None and hasattr(client, "get_task"):
            try:
                task = client.get_task(target_owner_id)
                if task is not None:
                    state_val = str(task.state.name if hasattr(task.state, "name") else task.state)
                    if task.updated_at is not None:
                        updated_at = task.updated_at.isoformat()
                    elif isinstance(task.raw_data, dict):
                        updated_at = str(task.raw_data.get("updated_at") or task.raw_data.get("updatedAt") or "")
            except Exception as exc:
                logger.warning(f"Could not capture baseline for Claude Routine task {target_owner_id}: {exc}")
        details = json.dumps({"updated_at": updated_at, "state": state_val})
        token = f"claude_routine_baseline:{target_owner_id}:{updated_at}"
        return ProviderBaselineEvidence(
            provider=ProviderType.CLAUDE_ROUTINE.value,
            owner_id=target_owner_id,
            baseline_token=token,
            baseline_details_json=details,
            observed_at=_now_iso(),
        )

    def send_followup(self, ticket: AdmissionTicket, message: str, client: Optional[object] = None) -> tuple[DeliveryStatus, str, str]:
        op_id = f"claude_deliv_{ticket.generation_id}"
        if client is None:
            return DeliveryStatus.INDETERMINATE, op_id, ""

        # Pre-send usage check via client if available
        if hasattr(client, "is_quota_insufficient") and callable(client.is_quota_insufficient):
            if client.is_quota_insufficient():
                return DeliveryStatus.DEFINITE_NON_DELIVERY, op_id, ""
        if getattr(client, "quota_insufficient", False):
            return DeliveryStatus.DEFINITE_NON_DELIVERY, op_id, ""

        try:
            send_fn = getattr(client, "send_followup", None)
            if not callable(send_fn):
                return DeliveryStatus.INDETERMINATE, op_id, ""
            accepted = send_fn(ticket.owner_id, message)
            if accepted:
                receipt = f"claude_receipt_{hashlib.sha256(f'{ticket.owner_id}:{ticket.generation_id}'.encode()).hexdigest()[:16]}"
                return DeliveryStatus.CONFIRMED, op_id, receipt
            return DeliveryStatus.INDETERMINATE, op_id, ""
        except Exception as exc:
            from .exceptions import AutoCoderUsageLimitError

            if isinstance(exc, AutoCoderUsageLimitError) or "rate limit" in str(exc).lower() or "quota" in str(exc).lower() or "429" in str(exc):
                return DeliveryStatus.DEFINITE_NON_DELIVERY, op_id, ""
            logger.warning(f"Claude Routine follow-up transport exception for session {ticket.owner_id}: {exc}")
            return DeliveryStatus.INDETERMINATE, op_id, ""

    def observe_completion(
        self,
        ticket: AdmissionTicket,
        client: Optional[object] = None,
    ) -> tuple[CorrectionCompletionStatus, str, Optional[bool], bool, str]:
        if client is None:
            return CorrectionCompletionStatus.UNAVAILABLE, "", None, False, "No client provided"

        get_task_fn = getattr(client, "get_task", None)
        if not callable(get_task_fn):
            return CorrectionCompletionStatus.UNAVAILABLE, "", None, False, "No get_task on Claude Routine client"

        try:
            task = get_task_fn(ticket.owner_id)
        except Exception as exc:
            return CorrectionCompletionStatus.UNAVAILABLE, "", None, False, f"Could not fetch Claude Routine task: {exc}"

        if task is None:
            return CorrectionCompletionStatus.UNAVAILABLE, "", None, False, "Claude Routine task not found"

        # Check for task mismatch (AS-002)
        if task.task_id and task.task_id != ticket.owner_id:
            return CorrectionCompletionStatus.UNAVAILABLE, "", None, False, f"Claude task ID mismatch: {task.task_id} != {ticket.owner_id}"

        from .cloud_task_client_base import CloudTaskState

        # REQ-005: PAUSED (e.g. absence of PR or waiting) is NOT completed correction!
        if task.state == CloudTaskState.PAUSED:
            return CorrectionCompletionStatus.UNAVAILABLE, "", None, False, "Claude Routine task is in PAUSED state"

        if task.state in (CloudTaskState.RUNNING, CloudTaskState.QUEUED):
            return CorrectionCompletionStatus.UNAVAILABLE, "", None, False, f"Claude Routine task is still running ({task.state.name})"

        # Check timestamp advancement
        current_updated_at = task.updated_at.isoformat() if task.updated_at is not None else ""
        if not current_updated_at and isinstance(task.raw_data, dict):
            current_updated_at = str(task.raw_data.get("updated_at") or task.raw_data.get("updatedAt") or "")

        try:
            baseline_details = json.loads(ticket.baseline.baseline_details_json)
            baseline_updated_at = baseline_details.get("updated_at", "")
        except Exception:
            baseline_updated_at = ""

        if current_updated_at and baseline_updated_at and current_updated_at <= baseline_updated_at:
            return CorrectionCompletionStatus.UNAVAILABLE, "", None, False, "Claude Routine task timestamp has not advanced past pre-send baseline"

        if task.state == CloudTaskState.COMPLETED:
            # Check for explicit no-change / inability result
            raw_data = task.raw_data if isinstance(task.raw_data, dict) else {}
            output_text = str(raw_data.get("output") or raw_data.get("result") or raw_data.get("text") or task.prompt or "")
            is_no_change = "CANNOT_FIX" in output_text or "no changes" in output_text.lower() or "unable to fix" in output_text.lower()
            code_changed = False if is_no_change else True
            native_ref = f"claude_completed_routine:{ticket.owner_id}:{current_updated_at}"
            return CorrectionCompletionStatus.COMPLETED, native_ref, code_changed, is_no_change, f"Claude Routine session {ticket.owner_id} completed"

        return CorrectionCompletionStatus.UNAVAILABLE, "", None, False, f"Claude Routine state is {task.state.name}"


class LocalRepairAdapter(BaseProviderRepairAdapter):
    """Adapter for Auto-Coder's local implementation boundary (REQ-004, REQ-010)."""

    def capture_pre_send_baseline(self, target_owner_id: str, client: Optional[object] = None) -> ProviderBaselineEvidence:
        details = json.dumps({"invocation_id": target_owner_id})
        token = f"local_baseline:{target_owner_id}"
        return ProviderBaselineEvidence(
            provider=ProviderType.LOCAL.value,
            owner_id=target_owner_id,
            baseline_token=token,
            baseline_details_json=details,
            observed_at=_now_iso(),
        )

    def send_followup(self, ticket: AdmissionTicket, message: str, client: Optional[object] = None) -> tuple[DeliveryStatus, str, str]:
        op_id = f"local_exec_{ticket.generation_id}"
        if client is None:
            return DeliveryStatus.CONFIRMED, op_id, f"local_receipt_{ticket.generation_id}"

        # If client provides an execution callback or callable
        if callable(client):
            try:
                res = client(message)
                if res:
                    return DeliveryStatus.CONFIRMED, op_id, f"local_receipt_{ticket.generation_id}"
                return DeliveryStatus.INDETERMINATE, op_id, ""
            except Exception as exc:
                logger.warning(f"Local repair execution exception: {exc}")
                return DeliveryStatus.INDETERMINATE, op_id, ""

        return DeliveryStatus.CONFIRMED, op_id, f"local_receipt_{ticket.generation_id}"

    def observe_completion(
        self,
        ticket: AdmissionTicket,
        client: Optional[object] = None,
    ) -> tuple[CorrectionCompletionStatus, str, Optional[bool], bool, str]:
        if client is None:
            # Default local invocation completion
            native_ref = f"local_invocation_completed:{ticket.owner_id}"
            return CorrectionCompletionStatus.COMPLETED, native_ref, True, False, "Local invocation completed"

        if isinstance(client, dict):
            # Simulated local runner result dict
            status = client.get("status", "completed")
            if status != "completed":
                return CorrectionCompletionStatus.UNAVAILABLE, "", None, False, f"Local run did not complete normally ({status})"
            output = str(client.get("output", ""))
            is_no_change = "CANNOT_FIX" in output or "no changes" in output.lower()
            code_changed = bool(client.get("code_changed", not is_no_change))
            native_ref = f"local_invocation_completed:{ticket.owner_id}"
            return CorrectionCompletionStatus.COMPLETED, native_ref, code_changed, is_no_change, "Local invocation completed"

        native_ref = f"local_invocation_completed:{ticket.owner_id}"
        return CorrectionCompletionStatus.COMPLETED, native_ref, True, False, "Local invocation completed"


# ---------------------------------------------------------------------------
# Coordinator (REQ-001..REQ-009)
# ---------------------------------------------------------------------------


class ProviderRepairCoordinator:
    """Coordinates admission ticket binding, delivery normalization, and completion correlation.

    Consumes ``RepairAllowanceLedger`` (#2140) and ``RepairHandoffBundle`` (#2139) to provide
    the definitive provider-independent correlation layer (REQ-001..REQ-011).
    """

    def __init__(
        self,
        allowance_ledger: RepairAllowanceLedger,
        correlation_store: Optional[ProviderRepairCorrelationStore] = None,
    ):
        self.allowance_ledger = allowance_ledger
        self.correlation_store = correlation_store or ProviderRepairCorrelationStore()
        self._adapters: dict[str, BaseProviderRepairAdapter] = {
            ProviderType.LOCAL.value: LocalRepairAdapter(),
            ProviderType.JULES.value: JulesRepairAdapter(),
            ProviderType.CODEX_CLOUD.value: CodexCloudRepairAdapter(),
            ProviderType.CLAUDE_ROUTINE.value: ClaudeRoutineRepairAdapter(),
        }

    def get_adapter(self, provider: str) -> BaseProviderRepairAdapter:
        adapter = self._adapters.get(provider)
        if adapter is None:
            raise ValueError(f"Unsupported repair provider: {provider}")
        return adapter

    def register_adapter(self, provider: str, adapter: BaseProviderRepairAdapter) -> None:
        self._adapters[provider] = adapter

    def bind_admission_ticket(
        self,
        api_origin: str,
        repository: str,
        pr_number: int,
        bundle: RepairHandoffBundle,
        provider: str,
        owner_id: str,
        client: Optional[object] = None,
    ) -> AdmissionTicket:
        """Durably bind an admission ticket before crossing an outbound repair boundary (REQ-002).

        1. Verifies the bundle is bound to the target.
        2. Captures pre-send baseline evidence.
        3. Admits a generation in the durable repair allowance state machine.
        4. Durably persists the admission ticket before returning.
        Refuses the send if admission, binding, or persistence fails (AS-006).
        """
        norm_origin = normalize_api_origin(api_origin)
        adapter = self.get_adapter(provider)
        baseline = adapter.capture_pre_send_baseline(owner_id, client=client)

        # Get snapshot to observe current epoch and check open blockers
        snapshot = self.allowance_ledger.get_snapshot(norm_origin, repository, pr_number, require_retained_state=True)
        expected_epoch = snapshot.epoch

        # Collect covered canonical blocker IDs from the bundle
        covered_ids = tuple(b.blocker_id for b in bundle.blockers)
        if not covered_ids:
            raise AdmissionRefusalError("Cannot admit a repair generation with an empty bundle")

        # Check if an outstanding generation already exists
        outstanding = snapshot.get_outstanding_generation()
        if outstanding is not None:
            raise AdmissionRefusalError(f"PR #{pr_number} already has outstanding generation {outstanding.generation_id} in state {outstanding.lifecycle_state.value}")

        gen_bundle = CorrectiveGenerationBundle(
            bundle_reference=bundle.bundle_id,
            covered_blocker_ids=covered_ids,
            scope_revision=bundle.reviewed_head_sha,
            requirement_manifest_revision=bundle.requirement_manifest_revision,
            owning_identity=owner_id,
            observed_baseline=baseline.baseline_token,
        )
        op_id = f"admit_ticket_{uuid.uuid4().hex[:12]}"
        admission_res = self.allowance_ledger.admit_generation(
            norm_origin,
            repository,
            pr_number,
            op_id,
            expected_epoch,
            gen_bundle,
            open_blocker_ids=covered_ids,
        )
        if not admission_res.admitted:
            raise AdmissionRefusalError(f"Admission denied for PR #{pr_number}: {admission_res.denial_reason}")

        generation_id = admission_res.generation_id
        if not generation_id:
            raise AdmissionRefusalError("Admission succeeded but returned no generation ID")

        ticket = AdmissionTicket(
            ticket_id=f"tkt_{uuid.uuid4().hex[:16]}",
            generation_id=generation_id,
            bundle_id=bundle.bundle_id,
            api_origin=norm_origin,
            repository=repository,
            pr_number=pr_number,
            head_sha=bundle.reviewed_head_sha,
            provider=provider,
            owner_id=owner_id,
            expected_epoch=admission_res.snapshot.epoch,
            baseline=baseline,
            status=TicketStatus.BOUND,
            created_at=_now_iso(),
            updated_at=_now_iso(),
        )

        # Durably persist ticket before crossing outbound boundary (REQ-002, AS-006)
        try:
            self.correlation_store.save_admission_ticket(ticket)
        except Exception as exc:
            logger.error(f"Failed to persist admission ticket: {exc}")
            raise AdmissionTicketPersistenceError(f"Failed to persist admission ticket: {exc}") from exc

        return ticket

    def dispatch_repair(
        self,
        ticket: AdmissionTicket,
        prompt: str,
        current_owner_id: str,
        current_epoch: int,
        client: Optional[object] = None,
    ) -> NormalizedDeliveryObservation:
        """Dispatch a repair request across the outbound boundary using a bound ticket (REQ-002, REQ-003, AS-006).

        Refuses if the owner or epoch has changed, or if binding is invalid.
        Records delivery outcome in the allowance ledger and correlation store.
        """
        if ticket.owner_id != current_owner_id:
            raise OwnerMismatchError(f"Authoritative owner task changed: ticket is bound to {ticket.owner_id}, but current owner is {current_owner_id}")
        if ticket.expected_epoch != current_epoch:
            raise StaleTicketEpochError(f"Namespace epoch changed: ticket expected epoch {ticket.expected_epoch}, but current is {current_epoch}")

        adapter = self.get_adapter(ticket.provider)
        status, deliv_op_id, receipt = adapter.send_followup(ticket, prompt, client=client)

        obs = NormalizedDeliveryObservation(
            observation_id=f"deliv_obs_{uuid.uuid4().hex[:12]}",
            ticket_id=ticket.ticket_id,
            generation_id=ticket.generation_id,
            bundle_id=ticket.bundle_id,
            api_origin=ticket.api_origin,
            repository=ticket.repository,
            pr_number=ticket.pr_number,
            provider=ticket.provider,
            owner_id=ticket.owner_id,
            delivery_status=status,
            delivery_operation_identity=deliv_op_id,
            receipt_identity=receipt,
            baseline_evidence=ticket.baseline.baseline_token,
            evidence=f"Delivery status: {status.value}, receipt: {receipt}",
            created_at=_now_iso(),
        )

        # Map to allowance ledger DeliveryOutcome
        ledger_outcome = DeliveryOutcome.CONFIRMED if status == DeliveryStatus.CONFIRMED else (DeliveryOutcome.DEFINITE_NON_DELIVERY if status == DeliveryStatus.DEFINITE_NON_DELIVERY else DeliveryOutcome.INDETERMINATE)
        self.allowance_ledger.record_delivery_outcome(
            ticket.api_origin,
            ticket.repository,
            ticket.pr_number,
            f"op_rec_{deliv_op_id}",
            ticket.expected_epoch,
            ticket.generation_id,
            ledger_outcome,
            deliv_op_id,
            evidence=obs.evidence,
        )

        self.correlation_store.save_delivery_observation(obs)
        return obs

    def observe_and_correlate_completion(
        self,
        ticket: AdmissionTicket,
        client: Optional[object] = None,
    ) -> NormalizedCorrectionObservation:
        """Observe and correlate provider-native completion with the admitted generation (REQ-004, REQ-005, REQ-006).

        Advances generation in allowance ledger only on confirmed, positively correlated completion.
        """
        adapter = self.get_adapter(ticket.provider)
        comp_status, native_ref, code_changed, is_no_change, evidence = adapter.observe_completion(ticket, client=client)

        obs = NormalizedCorrectionObservation(
            observation_id=f"corr_obs_{uuid.uuid4().hex[:12]}",
            ticket_id=ticket.ticket_id,
            generation_id=ticket.generation_id,
            bundle_id=ticket.bundle_id,
            api_origin=ticket.api_origin,
            repository=ticket.repository,
            pr_number=ticket.pr_number,
            provider=ticket.provider,
            owner_id=ticket.owner_id,
            completion_status=comp_status,
            provider_native_ref=native_ref,
            code_changed=code_changed,
            is_no_change_result=is_no_change,
            evidence=evidence,
            observed_at=_now_iso(),
        )
        self.correlation_store.save_correction_observation(obs)

        # Advance allowance ledger if completion is known and causally correlated
        if comp_status == CorrectionCompletionStatus.COMPLETED:
            snapshot = self.allowance_ledger.get_snapshot(ticket.api_origin, ticket.repository, ticket.pr_number)
            gen = snapshot.get_generation(ticket.generation_id)
            if gen is not None and gen.lifecycle_state == GenerationLifecycleState.CONFIRMED_DELIVERED:
                comp_seq = int(datetime.now(timezone.utc).timestamp() * 1000)
                self.allowance_ledger.record_completion_observation(
                    ticket.api_origin,
                    ticket.repository,
                    ticket.pr_number,
                    f"op_comp_{ticket.generation_id}",
                    snapshot.epoch,
                    ticket.generation_id,
                    CompletionAvailability.KNOWN,
                    completion_seq=comp_seq,
                    code_changed=code_changed,
                    causally_after_admission=True,
                    evidence=evidence,
                )

        return obs

    def capture_validation_evidence(
        self,
        ticket: AdmissionTicket,
        head_sha: str,
    ) -> ValidationEvidenceBinding:
        """Capture correlated completed-generation evidence before independent validation starts (REQ-007, AS-005).

        Raises CorrelationUnavailableError if generation is not completed yet.
        """
        snapshot = self.allowance_ledger.get_snapshot(ticket.api_origin, ticket.repository, ticket.pr_number)
        gen = snapshot.get_generation(ticket.generation_id)
        if gen is None:
            raise CorrelationUnavailableError(f"Generation {ticket.generation_id} not found in allowance ledger")

        # REQ-007, AS-005: Validation captured before generation reaches PENDING_REVALIDATION cannot settle it!
        if gen.lifecycle_state != GenerationLifecycleState.PENDING_REVALIDATION:
            raise CorrelationUnavailableError(f"Cannot capture validation evidence: generation {ticket.generation_id} is in state {gen.lifecycle_state.value}, expected PENDING_REVALIDATION")

        comp_obs = self.correlation_store.get_correction_observations_for_generation(ticket.generation_id)
        latest_comp = next((o for o in reversed(comp_obs) if o.completion_status == CorrectionCompletionStatus.COMPLETED), None)
        native_ref = latest_comp.provider_native_ref if latest_comp else ""

        binding = ValidationEvidenceBinding(
            binding_id=f"val_bind_{uuid.uuid4().hex[:12]}",
            generation_id=ticket.generation_id,
            bundle_id=ticket.bundle_id,
            api_origin=ticket.api_origin,
            repository=ticket.repository,
            pr_number=ticket.pr_number,
            head_sha=head_sha,
            provider=ticket.provider,
            owner_id=ticket.owner_id,
            provider_native_ref=native_ref,
            completion_seq=gen.completion_seq or 0,
            code_changed=gen.completion_code_changed,
            captured_at=_now_iso(),
            is_settled=False,
        )
        self.correlation_store.save_validation_binding(binding)
        return binding

    def settle_validation_results(
        self,
        binding: ValidationEvidenceBinding,
        validations: Sequence[ValidationObservation],
    ) -> RepairAllowanceLedgerSnapshot:
        """Record validation results using a previously captured validation binding (REQ-007)."""
        snapshot = self.allowance_ledger.get_snapshot(binding.api_origin, binding.repository, binding.pr_number)
        gen = snapshot.get_generation(binding.generation_id)
        if gen is None:
            raise CorrelationUnavailableError(f"Generation {binding.generation_id} not found in allowance ledger")

        val_op_id = f"settle_val_{uuid.uuid4().hex[:12]}"
        updated_snapshot = self.allowance_ledger.record_validation_results(
            binding.api_origin,
            binding.repository,
            binding.pr_number,
            val_op_id,
            snapshot.epoch,
            binding.generation_id,
            validations,
        )

        # Mark binding settled
        settled_binding = ValidationEvidenceBinding(
            binding_id=binding.binding_id,
            generation_id=binding.generation_id,
            bundle_id=binding.bundle_id,
            api_origin=binding.api_origin,
            repository=binding.repository,
            pr_number=binding.pr_number,
            head_sha=binding.head_sha,
            provider=binding.provider,
            owner_id=binding.owner_id,
            provider_native_ref=binding.provider_native_ref,
            completion_seq=binding.completion_seq,
            code_changed=binding.code_changed,
            captured_at=binding.captured_at,
            is_settled=True,
        )
        self.correlation_store.save_validation_binding(settled_binding)
        return updated_snapshot
