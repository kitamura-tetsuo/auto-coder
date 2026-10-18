"""Issue retry admission at the implementation-ownership boundary.

This service deliberately stops after durable ownership acquisition.  Provider
selection, submission, receipt handling, and logical-slot release remain the
responsibility of the existing dispatch pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .implementation_ownership import acquire_explicit_retry
from .implementation_slots import (
    ImplementationHierarchyConflict,
    ImplementationHierarchyUnavailable,
    ImplementationOwner,
    ImplementationSlotRepository,
    ImplementationSlotUnavailable,
)
from .issue_stage_routing import ImplementationRetryRequest, IssueStageRoutingStore


class IssueRetryOrigin(str, Enum):
    """Supported sources of durable Issue retry authority."""

    MANUAL = "manual"
    STALE_JULES = "stale_jules"


class IssueRetryDisposition(str, Enum):
    """Outcome of attempting to cross the retry ownership boundary."""

    OWNED = "owned"
    DEFERRED = "deferred"
    STALE = "stale"
    FAILED = "failed"


@dataclass(frozen=True)
class IssueRetryAdmission:
    """Typed retry outcome, retaining the exact durable authority when known."""

    disposition: IssueRetryDisposition
    authority: Optional[ImplementationRetryRequest]
    reason: str


class IssueRetryAdmissionService:
    """Coordinate manual and stale-Jules Issue retry ownership acquisition."""

    def __init__(self, routing: IssueStageRoutingStore, slots: ImplementationSlotRepository) -> None:
        self._routing = routing
        self._slots = slots

    def admit(
        self,
        repository: str,
        target_number: int,
        generation: str,
        request_id: str,
        origin: IssueRetryOrigin,
        *,
        github_client: object | None = None,
        bypass_capacity: bool = False,
    ) -> IssueRetryAdmission:
        """Acquire one existing request without performing provider submission."""
        authority: Optional[ImplementationRetryRequest] = None
        try:
            if origin is IssueRetryOrigin.MANUAL:
                blockers = tuple(request for request in self._routing.retry_requests(repository, target_number) if request.status == "pending" and request.request_id != request_id)
                if blockers:
                    blocker = blockers[0]
                    return IssueRetryAdmission(
                        IssueRetryDisposition.DEFERRED,
                        blocker,
                        f"retry request {blocker.request_id} has no known creation outcome",
                    )
                authority = self._routing.accept_retry_request(request_id, repository, target_number, generation)
            else:
                authority = self._routing.retry_request(request_id)
                if authority is None:
                    return IssueRetryAdmission(IssueRetryDisposition.FAILED, None, "durable retry request is missing")
                if authority.repository != repository or authority.target_number != target_number:
                    return IssueRetryAdmission(IssueRetryDisposition.FAILED, authority, "durable retry request targets a different Issue")
                if authority.generation != generation or authority.status == "invalidated":
                    return IssueRetryAdmission(IssueRetryDisposition.STALE, authority, "retry request no longer matches the current generation")
                owner = ImplementationOwner("issue", target_number)
                if self._slots.has_qualifying_implementation_activity(owner):
                    return IssueRetryAdmission(
                        IssueRetryDisposition.DEFERRED,
                        authority,
                        "stale-Jules predecessor retains implementation activity",
                    )

            assert authority is not None
            acquired = acquire_explicit_retry(
                self._routing,
                self._slots,
                repository,
                target_number,
                generation,
                authority.request_id,
                github_client=github_client,
                bypass_capacity=bypass_capacity if origin is IssueRetryOrigin.MANUAL else False,
            )
        except (ImplementationHierarchyConflict, ImplementationHierarchyUnavailable, ImplementationSlotUnavailable):
            # These classifications are part of the engine's ownership-boundary
            # contract.  In particular, temporary hierarchy/slot failures must
            # retain the capacity-refill obligation, while hierarchy conflicts
            # remain an ordinary deferral.  Do not flatten them into FAILED.
            raise
        except Exception as exc:
            return IssueRetryAdmission(IssueRetryDisposition.FAILED, authority, str(exc))

        if acquired.status == "owned":
            return IssueRetryAdmission(IssueRetryDisposition.OWNED, acquired, "retry ownership acquired")
        if acquired.status == "invalidated":
            return IssueRetryAdmission(IssueRetryDisposition.STALE, acquired, acquired.refusal or "retry request was invalidated")
        return IssueRetryAdmission(IssueRetryDisposition.DEFERRED, acquired, acquired.refusal or "retry ownership is unavailable")
