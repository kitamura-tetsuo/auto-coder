"""Dedicated consumer for durable Issue Implementation-lane work.

The worker deliberately knows nothing about semantic review.  Its only inputs
are Implementation rows which have already been admitted from durable READY
evidence by :mod:`auto_coder.issue_stage_routing`, plus an authoritative
refresh supplied by the controller immediately before dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .issue_stage_routing import IMPLEMENTATION_STAGE, IssueStageRoutingStore, PendingLaneItem


@dataclass(frozen=True)
class ImplementationLaneOutcome:
    """Observable result of one Implementation-lane admission attempt."""

    target_number: int
    generation: str
    status: str


class IssueImplementationWorker:
    """Claim priority/FIFO Implementation work without executing Review work."""

    def __init__(self, routing: IssueStageRoutingStore) -> None:
        self._routing = routing

    def next_work(self, repository: str) -> Optional[PendingLaneItem]:
        pending = self._routing.pending(repository, IMPLEMENTATION_STAGE)
        return pending[0] if pending else None

    def recover(self, repository: str) -> None:
        """Restore pre-ownership attempts while owned generations stay suppressed."""
        self._routing.recover(repository)

    def run_one(
        self,
        repository: str,
        *,
        refresh: Callable[[PendingLaneItem], Optional[PendingLaneItem]],
        dispatch: Callable[[PendingLaneItem], str],
    ) -> Optional[ImplementationLaneOutcome]:
        """Refresh twice, then dispatch the exact still-current generation.

        ``refresh`` must rebuild routing from authoritative GitHub and durable
        review state.  Returning no matching item means eligibility was lost.
        ``dispatch`` returns ``owned``, ``deferred`` or ``stale``; it is the
        controller's non-review admission and production-ownership boundary.
        """
        item = self.next_work(repository)
        if item is None:
            return None
        current = refresh(item)
        if current is None or current.generation != item.generation:
            self._routing.remove_generation(repository, IMPLEMENTATION_STAGE, item.target_number, item.generation)
            return ImplementationLaneOutcome(item.target_number, item.generation, "stale")
        if not self._routing.begin(item):
            return ImplementationLaneOutcome(item.target_number, item.generation, "deferred")
        current = refresh(item)
        if current is None or current.generation != item.generation:
            self._routing.remove_generation(repository, IMPLEMENTATION_STAGE, item.target_number, item.generation)
            return ImplementationLaneOutcome(item.target_number, item.generation, "stale")
        status = dispatch(item)
        if status == "deferred":
            self._routing.defer(item)
        elif status == "stale":
            self._routing.remove_generation(repository, IMPLEMENTATION_STAGE, item.target_number, item.generation)
        elif status != "owned":
            self._routing.defer(item)
            raise ValueError(f"unknown Implementation-lane outcome: {status}")
        return ImplementationLaneOutcome(item.target_number, item.generation, status)
