"""Production pre-effect fence for ordinary Issue-owned Codex work."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable, Optional, TypeVar

from .codex_work_accounting import CodexWorkAccounting, CodexWorkPhase
from .implementation_slots import ImplementationOwner, ImplementationSlotUnavailable

T = TypeVar("T")


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
