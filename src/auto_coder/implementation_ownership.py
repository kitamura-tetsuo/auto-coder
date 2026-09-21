"""Bind durable Issue Implementation generations to production ownership.

``issue_stage_routing.py`` defines exact Implementation-generation identity
and a durable owned-start tombstone, but deliberately never acquires an
implementation slot or dispatches a provider. ``implementation_slots.py``
already durably tracks the production implementation-owner authority
(logical owner records plus retained executions, provider sessions, and
implementation-PR membership) with no notion of an Issue "generation" at
all. This module is the recoverable handoff between those two existing
authorities (Issue #2061): it decides, from durable evidence alone, whether
a captured generation may durably start production implementation work, and
records the production acquisition back into the routing store's tombstone.

Correctness boundary (see Issue #2061 for the full normative contract):

* A generation must remain retryable until durable production state proves
  implementation-start responsibility was acquired for it (REQ-002).
* Acquisition happens at the first durable transition that binds
  ``(repository, owner, generation)`` and retains implementation-mutating
  responsibility: a durable local execution, a retained provider session, or
  implementation-PR membership (REQ-003). ``ImplementationSlotRepository``
  already persists all three atomically with capacity/hierarchy admission;
  this module only adds the generation binding and the routing tombstone on
  top, so a persisted execution awaiting provider submission already
  qualifies without any extra write.
* Once acquired, the fact is monotonic until the routing tombstone for that
  exact generation is durable: finishing/reclaiming a local execution,
  provider/session terminality, or owner release must not erase the
  captured binding before the tombstone exists (REQ-004). Because the
  binding lives on the owner record itself (a field independent from the
  mutable ``executions`` list), a stale-execution reclaim cannot lose it.
* A different, superseding generation is a distinct admissible attempt and
  never inherits an older generation's tombstone or binding (REQ-001,
  REQ-006); an older generation's binding is tombstoned before the owner
  record is ever rebound to a new one, so the older fact remains
  recoverable forever even though only one binding fits on one owner
  record at a time.
* Retained qualifying activity with a missing/inconsistent generation
  binding is a fail-closed target-scoped ambiguity (REQ-008): no new start
  for that owner may proceed until it resolves (typically by that legacy
  evidence fully releasing through the existing owner lifecycle).

This module intentionally does not redefine implementation-slot capacity,
hierarchy, retry, quota, or release policy (#2061 non-goals); it only reads
and narrowly extends ``ImplementationSlotRepository`` state to add the
generation binding, and it reuses the existing ``implementation_owned_starts``
tombstone table already defined by ``issue_stage_routing.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .implementation_slots import ImplementationOwner, ImplementationSlotRepository
from .issue_stage_routing import ImplementationRetryRequest, IssueStageRoutingStore


class OwnershipStartDecision(str, Enum):
    """The outcome of evaluating one production implementation-start attempt."""

    START_NEW = "start_new"
    CONTINUE = "continue"
    SUPERSEDE = "supersede"
    ALREADY_OWNED = "already_owned"
    BUSY_OTHER_GENERATION = "busy_other_generation"
    AMBIGUOUS_BINDING = "ambiguous_binding"


@dataclass(frozen=True)
class OwnershipStartGate:
    """A single evaluated decision for one ``(owner, generation)`` attempt."""

    decision: OwnershipStartDecision
    generation: str
    superseded_generation: Optional[str] = None

    @property
    def may_start(self) -> bool:
        return self.decision in (
            OwnershipStartDecision.START_NEW,
            OwnershipStartDecision.CONTINUE,
            OwnershipStartDecision.SUPERSEDE,
        )


def evaluate_implementation_start(
    routing: IssueStageRoutingStore,
    slots: ImplementationSlotRepository,
    repository: str,
    owner: ImplementationOwner,
    generation: str,
) -> OwnershipStartGate:
    """Decide whether *owner* may durably start production work for *generation*.

    Reads only; callers apply the decision via :func:`begin_implementation_ownership`
    (before calling ``slots.start_execution``) and :func:`confirm_implementation_ownership`
    (after it durably succeeds).
    """
    existing_generation = slots.implementation_generation(owner)
    owned = routing.is_implementation_owned(repository, owner.number, generation)
    if existing_generation == generation:
        if owned and not slots.has_qualifying_implementation_activity(owner):
            # Already durably started *and* the tombstone is already
            # durable, with nothing left retained: this owner is idle, not
            # mid-attempt. A duplicate wake for the exact same generation
            # must not create another fresh execution (REQ-006) -- unlike
            # the branch below, retained evidence would make this a genuine
            # continuation instead.
            return OwnershipStartGate(OwnershipStartDecision.ALREADY_OWNED, generation)
        # Same captured attempt with either retained evidence or a not-yet-
        # durable tombstone: a live execution finishing/being reclaimed, a
        # provider session ending, or a restart resuming it are all
        # continuations (REQ-007), never a new routing start. Recomputing
        # the tombstone here also recovers a prior crash that acquired this
        # generation durably but did not yet persist it (REQ-004).
        return OwnershipStartGate(OwnershipStartDecision.CONTINUE, generation)
    if owned:
        # Tombstoned, and the local owner record does not itself corroborate
        # an in-progress attempt for this exact generation: a genuine
        # duplicate (restart, duplicate wake, or exact semantic reversion
        # back to an already-completed generation) rather than a
        # continuation (REQ-002, REQ-006).
        return OwnershipStartGate(OwnershipStartDecision.ALREADY_OWNED, generation)
    if existing_generation is not None:
        if slots.has_qualifying_implementation_activity(owner):
            # The owner is still legitimately busy with a different,
            # not-yet-released generation's retained evidence; this is
            # ordinary operational deferral, not ownership of the new
            # generation (REQ-006).
            return OwnershipStartGate(OwnershipStartDecision.BUSY_OTHER_GENERATION, generation)
        # The old generation, once bound, can only have been bound alongside
        # real qualifying evidence (start_execution binds generation and its
        # first execution atomically). With none retained now, it is safe to
        # tombstone that recoverable fact before rebinding the owner record
        # to the new generation (REQ-001, REQ-004, REQ-008 supersession).
        return OwnershipStartGate(OwnershipStartDecision.SUPERSEDE, generation, existing_generation)
    if slots.has_qualifying_implementation_activity(owner):
        # Retained implementation-mutating evidence with no recorded
        # generation at all: a legacy or corrupted binding. Fail closed for
        # this owner rather than guess which generation it belongs to
        # (REQ-008); it resolves once that evidence fully releases through
        # the existing owner lifecycle.
        return OwnershipStartGate(OwnershipStartDecision.AMBIGUOUS_BINDING, generation)
    return OwnershipStartGate(OwnershipStartDecision.START_NEW, generation)


def begin_implementation_ownership(
    routing: IssueStageRoutingStore,
    repository: str,
    owner: ImplementationOwner,
    gate: OwnershipStartGate,
) -> None:
    """Tombstone a superseded generation before its owner record is rebound.

    A no-op for every decision except :attr:`OwnershipStartDecision.SUPERSEDE`.
    """
    if gate.decision is OwnershipStartDecision.SUPERSEDE and gate.superseded_generation is not None:
        routing.record_implementation_owned(repository, owner.number, gate.superseded_generation)


def confirm_implementation_ownership(
    routing: IssueStageRoutingStore,
    repository: str,
    owner: ImplementationOwner,
    generation: str,
) -> None:
    """Durably tombstone *generation* now that production acquisition succeeded.

    Idempotent; safe to call again during crash recovery for the same
    generation (REQ-004, REQ-005).
    """
    routing.record_implementation_owned(repository, owner.number, generation)


def acquire_explicit_retry(
    routing: IssueStageRoutingStore,
    slots: ImplementationSlotRepository,
    repository: str,
    target_number: int,
    generation: str,
    request_id: str,
    *,
    github_client: object | None = None,
    bypass_capacity: bool = False,
) -> ImplementationRetryRequest:
    """Consume one accepted request at the real local-execution boundary.

    The per-owner lock serializes independent consumers. If a process dies
    after the slot write, the next caller recovers the exact R/A/G binding from
    the slot rather than starting another execution.
    """
    if slots.repo_name != repository:
        raise ValueError("slot repository does not match retry repository")
    owner = ImplementationOwner("issue", target_number)
    with slots.serialize(owner):
        request = routing.claim_retry_acquisition(request_id, repository, target_number, generation)
        if request.status in {"owned", "invalidated"}:
            return request
        from .cloud_manager import CloudManager

        predecessor = CloudManager(repository).read_bindings_strict().get(str(target_number))
        request = routing.capture_retry_predecessor(
            request_id,
            predecessor.provider if predecessor is not None else None,
            predecessor.task_id if predecessor is not None else None,
            predecessor.backend_name if predecessor is not None else None,
        )
        recovered = slots.retry_acquisition_reference(owner, request.request_id, request.attempt_id, request.generation)
        if recovered is not None:
            return routing.mark_retry_owned(request_id, recovered)
        # Validate any historical binding through the strict public reader;
        # start_execution deliberately accepts legacy callers and therefore
        # cannot interpret a malformed value as explicit retry authority.
        slots.implementation_generation(owner)
        execution_id = slots.start_execution(
            owner,
            bypass_capacity=bypass_capacity,
            github_client=github_client,
            generation=generation,
            retry_request_id=request.request_id,
            implementation_attempt_id=request.attempt_id,
        )
        if execution_id is None:
            return routing.defer_retry_acquisition(request_id, "local execution contention or capacity unavailable")
        # The generation tombstone remains historical; this request is the
        # narrow authority for a distinct attempt of that exact generation.
        routing.record_implementation_owned(repository, target_number, generation)
        return routing.mark_retry_owned(request_id, execution_id)


def invalidate_explicit_retry(
    routing: IssueStageRoutingStore,
    slots: ImplementationSlotRepository,
    request_id: str,
    current_generation: str,
    *,
    reason: Optional[str] = None,
) -> ImplementationRetryRequest:
    """Order authoritative supersession against ownership acquisition."""
    request = routing.retry_request(request_id)
    if request is None:
        raise ValueError(f"unknown retry request {request_id!r}")
    if slots.repo_name != request.repository:
        raise ValueError("slot repository does not match retry repository")
    owner = ImplementationOwner("issue", request.target_number)
    with slots.serialize(owner):
        return routing.invalidate_retry_request(request_id, current_generation, reason)
