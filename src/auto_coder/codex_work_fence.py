"""Production pre-effect fence for ordinary Issue-owned Codex work."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable, Optional, TypeVar

from .codex_work_accounting import CodexWorkAccounting, CodexWorkPhase
from .implementation_slots import ImplementationOwner, ImplementationSlotUnavailable

T = TypeVar("T")


class CodexWorkOutsideScope(LookupError):
    """The task has no durable ordinary Issue-owned Codex provenance."""


@dataclass(frozen=True)
class CodexWorkIdentity:
    """Authoritative identity carried from a durable producer to transport."""

    repository: str
    issue_number: int
    incarnation: str
    operation_id: str
    operation_kind: str
    source_request_id: str
    task_id: Optional[str] = None
    causal_baseline: Optional[str] = None


class CodexWorkFence:
    """Register a mutation before invoking its external side effect.

    The caller remains responsible for deciding whether work is allowed.  This
    class only proves that an already-authorized logical operation is attached
    to the current Issue reservation before transport can observe it.
    """

    def __init__(self, accounting: CodexWorkAccounting) -> None:
        self.accounting = accounting

    def execute(self, identity: CodexWorkIdentity, send: Callable[[], T]) -> T:
        if identity.repository != self.accounting.slots.repo_name:
            raise ImplementationSlotUnavailable("Codex work belongs to a different repository")
        owner = ImplementationOwner("issue", identity.issue_number)
        with self.accounting.slots.serialize(owner):
            registration = self.accounting.register(
                owner,
                identity.incarnation,
                logical_operation_id=identity.operation_id,
                kind=identity.operation_kind,
                source_request_id=identity.source_request_id,
                causal_baseline=identity.causal_baseline,
                task_id=identity.task_id,
            )
            if not registration.created:
                raise ImplementationSlotUnavailable("Codex logical operation was already accounted; duplicate send refused")
            return send()

    def record_delivery(
        self,
        identity: CodexWorkIdentity,
        *,
        accepted: bool,
        indeterminate: bool,
        evidence_id: str,
        task_id: Optional[str] = None,
    ) -> None:
        owner = ImplementationOwner("issue", identity.issue_number)
        phase = CodexWorkPhase.ACCEPTED if accepted else CodexWorkPhase.DELIVERY_UNKNOWN
        if not accepted and not indeterminate:
            phase = CodexWorkPhase.SETTLED
        self.accounting.transition(
            owner,
            identity.incarnation,
            identity.operation_id,
            phase,
            evidence_id=evidence_id,
            evidence_source_request_id=identity.source_request_id if phase is CodexWorkPhase.SETTLED else None,
            evidence_causal_baseline=identity.causal_baseline if phase is CodexWorkPhase.SETTLED else None,
            task_id=task_id or identity.task_id,
            definite_non_delivery=phase is CodexWorkPhase.SETTLED,
        )


def stable_codex_operation_id(kind: str, source_request_id: str) -> str:
    """Derive a replay-stable, non-secret accounting key."""
    digest = hashlib.sha256(f"{kind}\0{source_request_id}".encode("utf-8")).hexdigest()
    return f"{kind}:{digest}"


def production_codex_fence(repository: str, task_id: str, kind: str, source_request_id: str, causal_baseline: Optional[str] = None) -> tuple[CodexWorkFence, CodexWorkIdentity]:
    """Resolve a task through durable CloudRun and current slot evidence."""
    from .cloud_run import CloudRunRepository
    from .codex_work_reconstruction import production_codex_reconstructor
    from .implementation_slots import ImplementationSlotRepository

    if not repository:
        raise CodexWorkOutsideScope("Codex client has no repository ownership context")
    matches = [run for run in CloudRunRepository(repository).list_all() if run.provider == "codex-cloud" and run.task_id == task_id]
    if not matches:
        raise CodexWorkOutsideScope("Codex task is positively outside tracked Issue work")
    if len(matches) != 1:
        raise ImplementationSlotUnavailable("Codex task has no unique durable Issue ownership")
    run = matches[0]
    slots = ImplementationSlotRepository(repository, 1)
    owner = ImplementationOwner("issue", run.issue_number)
    incarnation = slots.owner_incarnation(owner)
    if not incarnation:
        raise ImplementationSlotUnavailable("Codex task owner has no active incarnation")
    reconstructor = production_codex_reconstructor(repository, run.issue_number)
    accounting = CodexWorkAccounting(slots, reconstructor.consistency_ids)
    accounting.reconcile_from_receipt(owner, incarnation, reconstructor.reconstruct())
    identity = CodexWorkIdentity(
        repository,
        run.issue_number,
        incarnation,
        stable_codex_operation_id(kind, source_request_id),
        kind,
        source_request_id,
        task_id,
        causal_baseline,
    )
    return CodexWorkFence(accounting), identity


def production_codex_issue_fence(repository: str, issue_number: int, kind: str, source_request_id: str) -> tuple[CodexWorkFence, CodexWorkIdentity]:
    """Prepare a pre-task fence for an already-reserved Issue owner."""
    from .codex_work_reconstruction import production_codex_reconstructor
    from .implementation_slots import ImplementationSlotRepository

    slots = ImplementationSlotRepository(repository, 1)
    owner = ImplementationOwner("issue", issue_number)
    incarnation = slots.owner_incarnation(owner)
    if not incarnation:
        raise ImplementationSlotUnavailable("Codex Issue owner has no active incarnation")
    reconstructor = production_codex_reconstructor(repository, issue_number)
    accounting = CodexWorkAccounting(slots, reconstructor.consistency_ids)
    accounting.reconcile_from_receipt(owner, incarnation, reconstructor.reconstruct())
    return CodexWorkFence(accounting), CodexWorkIdentity(
        repository,
        issue_number,
        incarnation,
        stable_codex_operation_id(kind, source_request_id),
        kind,
        source_request_id,
    )
