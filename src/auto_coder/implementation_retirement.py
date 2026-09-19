"""Safe retirement of terminal PR-backed implementation slots.

Defines the retirement predicate, data model, and coordinated commit
transaction for releasing active capacity of terminal PR-backed implementations
while preserving live/uncertain work, durable duplicate-start evidence, and
historical associations (Issue #2146).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from .implementation_slots import (
    ImplementationOwner,
    ImplementationSlotRepository,
)
from .issue_stage_routing import IssueStageRoutingStore
from .logger_config import get_logger

logger = get_logger(__name__)


class PRTerminalState(str, Enum):
    """Lifecycle status of an implementation pull request."""

    CLOSED = "closed"
    MERGED = "merged"
    OPEN = "open"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ImplementationPRObservation:
    """Normalized observation of one implementation PR."""

    number: int
    state: PRTerminalState
    merged: bool = False

    @property
    def is_terminal(self) -> bool:
        return self.merged or self.state in (PRTerminalState.CLOSED, PRTerminalState.MERGED)


class SessionTerminalState(str, Enum):
    """Lifecycle status of a remote provider session."""

    ENDED = "ended"
    ACTIVE = "active"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProviderSessionObservation:
    """Normalized observation of one remote provider session."""

    session_id: str
    provider: str
    state: SessionTerminalState
    latest_activity_ended: bool = True

    @property
    def is_terminal(self) -> bool:
        return self.state is SessionTerminalState.ENDED and self.latest_activity_ended


class ExecutionTerminalState(str, Enum):
    """Lifecycle status of a local execution."""

    ENDED = "ended"
    LIVE = "live"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class LocalExecutionObservation:
    """Normalized observation of one local execution."""

    execution_id: str
    state: ExecutionTerminalState

    @property
    def is_terminal(self) -> bool:
        return self.state is ExecutionTerminalState.ENDED


@dataclass(frozen=True)
class ContinuingObligations:
    """Continuing implementation work obligations."""

    has_unresolved_submission: bool = False
    has_assigned_repair: bool = False
    has_replacement_publication: bool = False
    has_admitted_retry_handoff: bool = False

    @property
    def has_active_obligations(self) -> bool:
        return self.has_assigned_repair or self.has_replacement_publication or self.has_admitted_retry_handoff

    @property
    def has_unknown_obligations(self) -> bool:
        return self.has_unresolved_submission


@dataclass(frozen=True)
class ImplementationRetirementObservation:
    """Complete, coherent lifecycle observation for an implementation reservation."""

    repository: str
    owner: ImplementationOwner
    reservation_incarnation: str
    activity_revision: int
    implementation_prs: tuple[ImplementationPRObservation, ...]
    provider_sessions: tuple[ProviderSessionObservation, ...] = ()
    local_executions: tuple[LocalExecutionObservation, ...] = ()
    continuing_obligations: ContinuingObligations = field(default_factory=ContinuingObligations)
    speculative: bool = False
    recurrent: bool = False


class RetirementStatus(str, Enum):
    """Outcome status of an implementation slot retirement operation."""

    RELEASED = "RELEASED"
    RETAINED_ACTIVE = "RETAINED_ACTIVE"
    RETAINED_UNKNOWN = "RETAINED_UNKNOWN"
    STALE_OBSERVATION = "STALE_OBSERVATION"


@dataclass(frozen=True)
class RetirementResult:
    """Machine-readable result of a retirement evaluation or transaction."""

    status: RetirementStatus
    responsible_members: tuple[str, ...] = ()
    diagnostic: Optional[str] = None

    @property
    def is_released(self) -> bool:
        return self.status is RetirementStatus.RELEASED


def evaluate_retirement_predicate(
    observation: ImplementationRetirementObservation,
) -> RetirementResult:
    """Evaluate whether *observation* warrants retirement, active retention, or unknown retention.

    Validates that the owner is within retirement scope (REQ-001) and evaluates
    terminality across PRs, sessions, executions, and continuing obligations (REQ-002, REQ-003).
    """
    if observation.owner.kind != "issue":
        return RetirementResult(
            RetirementStatus.RETAINED_UNKNOWN,
            diagnostic=f"Unsupported candidate type {observation.owner.kind}: only Issue-owned reservations are eligible for retirement",
        )

    if observation.speculative:
        return RetirementResult(
            RetirementStatus.RETAINED_UNKNOWN,
            diagnostic="Speculative competition owners are not eligible for PR-backed retirement",
        )

    if observation.recurrent:
        return RetirementResult(
            RetirementStatus.RETAINED_UNKNOWN,
            diagnostic="Recurrent owners are not eligible for PR-backed retirement",
        )

    if not observation.implementation_prs:
        return RetirementResult(
            RetirementStatus.RETAINED_UNKNOWN,
            diagnostic="Never-published owners with no implementation PR are not eligible for retirement",
        )

    active_blockers: list[str] = []
    unknown_blockers: list[str] = []

    # 1. Implementation PRs
    for pr in observation.implementation_prs:
        if pr.state == PRTerminalState.OPEN:
            active_blockers.append(f"pr:{pr.number}")
        elif pr.state == PRTerminalState.UNKNOWN or not pr.is_terminal:
            unknown_blockers.append(f"pr:{pr.number}")

    # 2. Provider Sessions
    for session in observation.provider_sessions:
        if session.provider.lower() != "jules":
            unknown_blockers.append(f"session:{session.session_id}")
        elif session.state == SessionTerminalState.ACTIVE or not session.latest_activity_ended:
            active_blockers.append(f"session:{session.session_id}")
        elif session.state == SessionTerminalState.UNKNOWN:
            unknown_blockers.append(f"session:{session.session_id}")

    # 3. Local Executions
    for execution in observation.local_executions:
        if execution.state == ExecutionTerminalState.LIVE:
            active_blockers.append(f"execution:{execution.execution_id}")
        elif execution.state == ExecutionTerminalState.UNKNOWN:
            unknown_blockers.append(f"execution:{execution.execution_id}")

    # 4. Continuing Obligations
    obligations = observation.continuing_obligations
    if obligations.has_assigned_repair:
        active_blockers.append("obligation:assigned_repair")
    if obligations.has_replacement_publication:
        active_blockers.append("obligation:replacement_publication")
    if obligations.has_admitted_retry_handoff:
        active_blockers.append("obligation:admitted_retry_handoff")
    if obligations.has_unresolved_submission:
        unknown_blockers.append("obligation:unresolved_submission")

    if active_blockers:
        return RetirementResult(
            RetirementStatus.RETAINED_ACTIVE,
            responsible_members=tuple(active_blockers),
            diagnostic=f"Active work units retained: {', '.join(active_blockers)}",
        )

    if unknown_blockers:
        return RetirementResult(
            RetirementStatus.RETAINED_UNKNOWN,
            responsible_members=tuple(unknown_blockers),
            diagnostic=f"Uncertain or unavailable lifecycle evidence: {', '.join(unknown_blockers)}",
        )

    return RetirementResult(RetirementStatus.RELEASED)


def retire_implementation_slot(
    slots: ImplementationSlotRepository,
    observation: ImplementationRetirementObservation,
    routing: Optional[IssueStageRoutingStore] = None,
    *,
    pre_lock_barrier: Optional[Callable[[], None]] = None,
) -> RetirementResult:
    """Atomically evaluate and commit retirement for *observation*.

    Coordinates locking with serialization facilities, validates against the
    live store, preserves acquired-start facts, writes retired history, and
    removes the active reservation.
    """
    predicate_result = evaluate_retirement_predicate(observation)
    if predicate_result.status in (RetirementStatus.RETAINED_ACTIVE, RetirementStatus.RETAINED_UNKNOWN):
        return predicate_result

    if callable(pre_lock_barrier):
        pre_lock_barrier()

    with slots.serialize(observation.owner):
        with slots._state_lock():
            # Check idempotency first: if already retired and absent from active store
            if slots.is_incarnation_retired(observation.reservation_incarnation):
                active_record = slots._read().get(observation.owner.key)
                if active_record is None:
                    return RetirementResult(RetirementStatus.RELEASED, diagnostic="Already retired")

            active_owners = slots._read()
            record = active_owners.get(observation.owner.key)
            if record is None:
                return RetirementResult(
                    RetirementStatus.STALE_OBSERVATION,
                    diagnostic=f"Owner {observation.owner.key} not active in store",
                )

            # Validate incarnation identity
            stored_incarnation = record.get("incarnation")
            if stored_incarnation != observation.reservation_incarnation:
                return RetirementResult(
                    RetirementStatus.STALE_OBSERVATION,
                    diagnostic=f"Reservation incarnation mismatch: store has {stored_incarnation!r}, observation has {observation.reservation_incarnation!r}",
                )

            # Validate activity revision
            stored_revision = record.get("activity_revision", 1)
            if stored_revision != observation.activity_revision:
                return RetirementResult(
                    RetirementStatus.STALE_OBSERVATION,
                    diagnostic=f"Activity revision mismatch: store has {stored_revision}, observation has {observation.activity_revision}",
                )

            # Check membership completeness (stored members must be covered in observation)
            stored_prs_raw = record.get("implementation_prs")
            stored_prs: set[int] = (
                {p for p in stored_prs_raw if isinstance(p, int) and not isinstance(p, bool)}
                if isinstance(stored_prs_raw, list)
                else set()
            )
            observed_prs = {pr.number for pr in observation.implementation_prs}
            if not stored_prs.issubset(observed_prs):
                missing = tuple(f"pr:{num}" for num in sorted(stored_prs - observed_prs))
                return RetirementResult(
                    RetirementStatus.RETAINED_UNKNOWN,
                    responsible_members=missing,
                    diagnostic=f"Incomplete PR membership observation: missing {missing}",
                )

            stored_sessions_raw = record.get("provider_sessions")
            stored_sessions: set[str] = (
                {s for s in stored_sessions_raw if isinstance(s, str)}
                if isinstance(stored_sessions_raw, list)
                else set()
            )
            observed_sessions = {s.session_id for s in observation.provider_sessions}
            if not stored_sessions.issubset(observed_sessions):
                missing = tuple(f"session:{sid}" for sid in sorted(stored_sessions - observed_sessions))
                return RetirementResult(
                    RetirementStatus.RETAINED_UNKNOWN,
                    responsible_members=missing,
                    diagnostic=f"Incomplete provider session membership observation: missing {missing}",
                )

            # Check for live local executions recorded in active store
            stored_executions_raw = record.get("executions")
            stored_executions = stored_executions_raw if isinstance(stored_executions_raw, list) else []
            if stored_executions:
                stored_exec_ids: set[str] = {
                    str(e["id"])
                    for e in stored_executions
                    if isinstance(e, dict) and "id" in e and isinstance(e["id"], str)
                }
                observed_ended_exec_ids = {
                    e.execution_id for e in observation.local_executions if e.state is ExecutionTerminalState.ENDED
                }
                active_execs: list[str] = sorted(stored_exec_ids - observed_ended_exec_ids)
                if active_execs:
                    responsible = tuple(f"execution:{eid}" for eid in active_execs)
                    return RetirementResult(
                        RetirementStatus.RETAINED_ACTIVE,
                        responsible_members=responsible,
                        diagnostic=f"Active local executions retained in store: {responsible}",
                    )

            # Predicate check passed; proceed to atomic commit sequence:
            # Step 1: Preserve acquired-start responsibility in routing store
            generation = record.get("implementation_generation")
            if generation is not None and isinstance(generation, str) and routing is not None:
                routing.record_implementation_owned(observation.repository, observation.owner.number, generation)

            # Step 2: History commit to retired store
            retired_records = slots._read_retired()
            retired_record = {
                "repository": observation.repository,
                "kind": observation.owner.kind,
                "number": observation.owner.number,
                "incarnation": observation.reservation_incarnation,
                "implementation_prs": sorted(stored_prs),
                "provider_sessions": sorted(stored_sessions),
                "generation": generation if isinstance(generation, str) else None,
                "retired_at": time.time(),
            }
            retired_records[observation.reservation_incarnation] = retired_record
            slots._write_retired(retired_records)

            # Step 3: Active removal from active store
            active_owners.pop(observation.owner.key, None)
            slots._write(active_owners)

            logger.info(f"Successfully retired terminal implementation slot {observation.owner.key} " f"(incarnation={observation.reservation_incarnation})")
            return RetirementResult(RetirementStatus.RELEASED)
