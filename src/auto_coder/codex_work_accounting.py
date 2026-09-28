"""Durable, incarnation-bound accounting for admitted Codex work.

This module deliberately does not submit work or retire a slot.  It supplies the
durable authority which later producer adapters and retirement code can use.
The authority is stored in the slot record so its revision and membership are
committed atomically with the slot's existing cross-process fencing token.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterator, Optional

from .implementation_slots import ImplementationOwner, ImplementationSlotRepository, ImplementationSlotUnavailable


class WorkAccountingStatus(str, Enum):
    """Completeness of the obligation inventory."""

    COMPLETE = "complete"
    UNINITIALIZED = "uninitialized"
    UNAVAILABLE = "unavailable"
    UNRECONCILED = "unreconciled"


class CodexWorkPhase(str, Enum):
    """Decisive lifecycle state of one logical operation."""

    RESERVED = "reserved"
    DELIVERY_UNKNOWN = "delivery_unknown"
    ACCEPTED = "accepted"
    SETTLED = "settled"


class RetirementValidation(str, Enum):
    AUTHORIZED = "AUTHORIZED"
    STALE = "STALE"
    NON_RELEASABLE = "NON_RELEASABLE"


@dataclass(frozen=True)
class ReconstructionReceipt:
    """Proof that all named durable sources were consistently enumerated."""

    receipt_id: str
    sources: tuple[str, ...]
    consistent_sources: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return bool(self.receipt_id and self.sources and set(self.sources) == set(self.consistent_sources))


@dataclass(frozen=True)
class CodexWorkOperation:
    logical_operation_id: str
    kind: str
    source_request_id: str
    causal_baseline: Optional[str]
    task_id: Optional[str]
    phase: CodexWorkPhase
    execution_complete: bool
    publication_complete: bool
    tracking_complete: bool
    settlement_evidence_id: Optional[str]

    @property
    def settled(self) -> bool:
        return self.phase is CodexWorkPhase.SETTLED


@dataclass(frozen=True)
class CodexWorkSnapshot:
    repository: str
    owner: ImplementationOwner
    incarnation: str
    revision: int
    accounting_status: WorkAccountingStatus
    operations: tuple[CodexWorkOperation, ...]
    reconstruction_receipt_id: Optional[str] = None

    @property
    def releasable(self) -> bool:
        return self.accounting_status is WorkAccountingStatus.COMPLETE and all(operation.settled for operation in self.operations)


@dataclass(frozen=True)
class CodexWorkRegistration:
    snapshot: CodexWorkSnapshot
    created: bool
    send_authorized: bool


@dataclass(frozen=True)
class RetirementGuard:
    status: RetirementValidation
    snapshot: CodexWorkSnapshot


class CodexWorkAccounting:
    """Manage Codex obligations under a slot owner's serialization domain."""

    _FIELD = "codex_work_accounting"

    def __init__(self, slots: ImplementationSlotRepository):
        self.slots = slots

    def initialize_fresh(self, owner: ImplementationOwner, incarnation: str) -> CodexWorkSnapshot:
        """Initialize complete-empty accounting only for an active empty admission."""
        return self._initialize(owner, incarnation, None)

    def initialize_from_receipt(self, owner: ImplementationOwner, incarnation: str, receipt: ReconstructionReceipt) -> CodexWorkSnapshot:
        if not receipt.complete:
            raise ValueError("A complete reconstruction receipt must identify every consistent durable source")
        return self._initialize(owner, incarnation, receipt)

    def _initialize(self, owner: ImplementationOwner, incarnation: str, receipt: Optional[ReconstructionReceipt]) -> CodexWorkSnapshot:
        with self.slots.serialize(owner), self.slots._state_lock():
            owners = self.slots._read()
            record = self._active_record(owners, owner, incarnation)
            existing = record.get(self._FIELD)
            if existing is not None:
                return self._snapshot_from_record(owner, incarnation, record)
            # Existing activity cannot be declared covered by a bare fresh call.
            if receipt is None and self._has_preexisting_activity(record):
                raise ImplementationSlotUnavailable("Fresh accounting cannot establish complete coverage for existing activity")
            record[self._FIELD] = {
                "status": WorkAccountingStatus.COMPLETE.value,
                "operations": {},
                "reconstruction_receipt_id": receipt.receipt_id if receipt else None,
                "reconstruction_sources": list(receipt.sources) if receipt else [],
            }
            self._advance_revision(record)
            self.slots._write(owners)
            return self._snapshot_from_record(owner, incarnation, record)

    def snapshot(self, owner: ImplementationOwner, incarnation: str) -> CodexWorkSnapshot:
        with self.slots.serialize(owner), self.slots._state_lock():
            record = self._active_record(self.slots._read(), owner, incarnation)
            return self._snapshot_from_record(owner, incarnation, record)

    def register(
        self,
        owner: ImplementationOwner,
        incarnation: str,
        *,
        logical_operation_id: str,
        kind: str,
        source_request_id: str,
        causal_baseline: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> CodexWorkRegistration:
        """Record already-authorized work; registration grants no send authority."""
        required = (logical_operation_id, kind, source_request_id)
        if any(not value for value in required):
            raise ValueError("Logical operation, kind, and source request identities are required")
        with self.slots.serialize(owner), self.slots._state_lock():
            owners = self.slots._read()
            record = self._active_record(owners, owner, incarnation)
            accounting = self._complete_accounting(record)
            operations = accounting["operations"]
            assert isinstance(operations, dict)
            proposed = {
                "kind": kind,
                "source_request_id": source_request_id,
                "causal_baseline": causal_baseline,
                "task_id": task_id,
                "phase": CodexWorkPhase.RESERVED.value,
                "execution_complete": False,
                "publication_complete": False,
                "tracking_complete": False,
                "settlement_evidence_id": None,
            }
            existing = operations.get(logical_operation_id)
            if existing is not None:
                immutable_fields = ("kind", "source_request_id", "causal_baseline", "task_id")
                if not isinstance(existing, dict) or any(existing.get(field) != proposed[field] for field in immutable_fields):
                    raise ValueError("Logical operation identity was replayed with different attributes")
                return CodexWorkRegistration(self._snapshot_from_record(owner, incarnation, record), False, False)
            operations[logical_operation_id] = proposed
            self._advance_revision(record)
            self.slots._write(owners)
            return CodexWorkRegistration(self._snapshot_from_record(owner, incarnation, record), True, False)

    def transition(
        self,
        owner: ImplementationOwner,
        incarnation: str,
        logical_operation_id: str,
        phase: CodexWorkPhase,
        *,
        evidence_id: str,
        task_id: Optional[str] = None,
        execution_complete: bool = False,
        publication_complete: bool = False,
        tracking_complete: bool = False,
        definite_non_delivery: bool = False,
    ) -> CodexWorkSnapshot:
        """Apply causally identified evidence without weakening unsettled state."""
        if not evidence_id:
            raise ValueError("A causal evidence identity is required")
        with self.slots.serialize(owner), self.slots._state_lock():
            owners = self.slots._read()
            record = self._active_record(owners, owner, incarnation)
            accounting = self._complete_accounting(record)
            operations = accounting["operations"]
            assert isinstance(operations, dict)
            raw = operations.get(logical_operation_id)
            if not isinstance(raw, dict):
                raise KeyError(logical_operation_id)
            old = copy.deepcopy(raw)
            existing_task = raw.get("task_id")
            if existing_task and task_id and existing_task != task_id:
                raise ValueError("Evidence task identity does not match the operation")
            if task_id and not existing_task:
                raw["task_id"] = task_id
            accepted = existing_task is not None or raw.get("phase") == CodexWorkPhase.ACCEPTED.value
            if phase is CodexWorkPhase.SETTLED:
                completed = execution_complete and publication_complete and tracking_complete
                if not completed and not (definite_non_delivery and not accepted and task_id is None):
                    raise ValueError("Settlement requires matching execution and handoff completion evidence")
                raw.update(
                    execution_complete=execution_complete,
                    publication_complete=publication_complete,
                    tracking_complete=tracking_complete,
                    settlement_evidence_id=evidence_id,
                )
            raw["phase"] = phase.value
            if raw != old:
                self._advance_revision(record)
                self.slots._write(owners)
            return self._snapshot_from_record(owner, incarnation, record)

    @contextmanager
    def retirement_guard(self, snapshot: CodexWorkSnapshot) -> Iterator[RetirementGuard]:
        """Hold owner serialization across validation and the caller's removal boundary."""
        with self.slots.serialize(snapshot.owner), self.slots._state_lock():
            owners = self.slots._read()
            record = owners.get(snapshot.owner.key)
            status = RetirementValidation.STALE
            current = snapshot
            if isinstance(record, dict) and record.get("incarnation") == snapshot.incarnation:
                current = self._snapshot_from_record(snapshot.owner, snapshot.incarnation, record)
                if current.revision == snapshot.revision:
                    status = RetirementValidation.AUTHORIZED if current.releasable else RetirementValidation.NON_RELEASABLE
            yield RetirementGuard(status, current)

    def _snapshot_from_record(self, owner: ImplementationOwner, incarnation: str, record: dict[str, object]) -> CodexWorkSnapshot:
        revision = record.get("activity_revision")
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise ImplementationSlotUnavailable("Codex accounting requires a valid slot activity revision")
        raw = record.get(self._FIELD)
        if raw is None:
            status = WorkAccountingStatus.UNINITIALIZED
            operations: tuple[CodexWorkOperation, ...] = ()
            receipt_id = None
        elif not isinstance(raw, dict) or not isinstance(raw.get("operations"), dict):
            status, operations, receipt_id = WorkAccountingStatus.UNAVAILABLE, (), None
        else:
            try:
                status = WorkAccountingStatus(str(raw.get("status")))
                operations = tuple(self._parse_operation(key, value) for key, value in sorted(raw["operations"].items()))
                receipt = raw.get("reconstruction_receipt_id")
                receipt_id = receipt if isinstance(receipt, str) else None
            except (TypeError, ValueError):
                status, operations, receipt_id = WorkAccountingStatus.UNAVAILABLE, (), None
        return CodexWorkSnapshot(self.slots.repo_name, owner, incarnation, revision, status, operations, receipt_id)

    @staticmethod
    def _parse_operation(key: object, raw: object) -> CodexWorkOperation:
        if not isinstance(key, str) or not isinstance(raw, dict):
            raise ValueError("Malformed Codex operation")
        return CodexWorkOperation(
            key,
            str(raw["kind"]),
            str(raw["source_request_id"]),
            raw.get("causal_baseline"),
            raw.get("task_id"),
            CodexWorkPhase(str(raw["phase"])),
            bool(raw["execution_complete"]),
            bool(raw["publication_complete"]),
            bool(raw["tracking_complete"]),
            raw.get("settlement_evidence_id"),
        )

    @staticmethod
    def _active_record(owners: dict[str, dict[str, object]], owner: ImplementationOwner, incarnation: str) -> dict[str, object]:
        record = owners.get(owner.key)
        if record is None or record.get("incarnation") != incarnation:
            raise ImplementationSlotUnavailable("Reservation incarnation is absent, retired, or stale")
        return record

    def _complete_accounting(self, record: dict[str, object]) -> dict[str, object]:
        raw = record.get(self._FIELD)
        if not isinstance(raw, dict) or raw.get("status") != WorkAccountingStatus.COMPLETE.value or not isinstance(raw.get("operations"), dict):
            raise ImplementationSlotUnavailable("Codex accounting is not positively initialized and complete")
        return raw

    @staticmethod
    def _advance_revision(record: dict[str, object]) -> None:
        revision = record.get("activity_revision")
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise ImplementationSlotUnavailable("Cannot advance an invalid activity revision")
        record["activity_revision"] = revision + 1

    @staticmethod
    def _has_preexisting_activity(record: dict[str, object]) -> bool:
        return any(record.get(field) for field in ("implementation_prs", "provider_sessions", "executions"))
