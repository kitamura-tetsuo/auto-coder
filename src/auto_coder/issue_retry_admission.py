"""Issue retry admission at the implementation-ownership boundary.

This service deliberately stops after durable ownership acquisition.  Provider
selection, submission, receipt handling, and logical-slot release remain the
responsibility of the existing dispatch pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .execution_trace import EventKind, Outcome, get_trace_collector
from .implementation_ownership import acquire_explicit_retry
from .implementation_slots import (
    ImplementationHierarchyConflict,
    ImplementationHierarchyUnavailable,
    ImplementationOwner,
    ImplementationSlotRepository,
    ImplementationSlotUnavailable,
)
from .issue_stage_routing import ImplementationRetryRequest, IssueStageRoutingStore
from .local_job_handoff import ContinuationState, LocalJobContinuation, LocalJobRecord, LocalJobState, LocalJobStore
from .local_job_runner import runner_owner_alive, writer_group_settled
from .logger_config import get_logger

logger = get_logger(__name__)


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

    def __init__(self, routing: IssueStageRoutingStore, slots: ImplementationSlotRepository, local_jobs: Optional[LocalJobStore] = None) -> None:
        self._routing = routing
        self._slots = slots
        self._local_jobs = local_jobs

    def _stopped_predecessors(self, repository: str, target_number: int) -> tuple[tuple[tuple[LocalJobRecord, LocalJobContinuation], ...], Optional[str]]:
        """Inspect exact transferred executions before accepting retry authority."""
        owner = ImplementationOwner("issue", target_number)
        executions = self._slots.active_execution_ids(owner)
        if not executions:
            return (), None
        store = self._local_jobs or LocalJobStore()
        evidence = store.issue_job_evidence(repository, target_number)
        jobs = tuple(entry.job for entry in evidence if entry.job.implementation_execution_id in executions)
        for execution in executions:
            matching = tuple(job for job in jobs if job.implementation_execution_id == execution)
            if len(matching) > 1:
                raise ImplementationSlotUnavailable("Multiple local jobs claim the same implementation execution")
        unmatched = set(executions) - {job.implementation_execution_id for job in jobs}
        if unmatched:
            self._slots.reclaim_stale_executions()
            if unmatched.intersection(self._slots.active_execution_ids(owner)):
                return (), f"implementation ownership already exists ({owner.key})"
        predecessors = []
        for job in jobs:
            if not job.owner_generation or job.owner_incarnation != self._slots.owner_incarnation(owner) or job.owner_generation != self._slots.implementation_generation(owner):
                return (), f"retained local job {job.job_id} has inconsistent implementation ownership"
            if job.state is LocalJobState.SETTLED:
                if any(effect.name == "explicit-retry-supersession" and effect.state == "completed" and effect.evidence for entry in evidence if entry.job.job_id == job.job_id for effect in entry.effects):
                    # Complete a crash-interrupted projection of the durable receipt.
                    self._slots.finish_execution(owner, job.implementation_execution_id)
                continue
            continuation = store.get_continuation(job.job_id)
            if job.state not in {LocalJobState.RUNNING, LocalJobState.PAUSED} or not job.provider_entered or continuation is None or continuation.execution_incarnation != job.execution_incarnation or continuation.state is ContinuationState.FINAL_CONFIRMED:
                return (), f"retained local job {job.job_id} requires result/session reconciliation before retry"
            if runner_owner_alive(job.runner_owner) is not False or not writer_group_settled(continuation.writer_owner):
                return (), f"retained local job {job.job_id} has live or unconfirmed runner/writers"
            if any(entry.effects for entry in evidence if entry.job.job_id == job.job_id) or store.get_execution_result_artifact(job.job_id, job.execution_incarnation) is not None:
                return (), f"retained local job {job.job_id} has a recorded result/effect; reconcile it before retry"
            predecessors.append((job, continuation))
        return tuple(predecessors), None

    def _retire_stopped_predecessors(self, predecessors: tuple[tuple[LocalJobRecord, LocalJobContinuation], ...], authority: ImplementationRetryRequest) -> Optional[str]:
        store = self._local_jobs or LocalJobStore()
        owner = ImplementationOwner("issue", authority.target_number)
        for job, continuation in predecessors:
            if not store.supersede_stopped_issue_for_retry(job, continuation, authority.request_id):
                return f"retained local job {job.job_id} changed or has a recorded result; reconcile it before retry"
            self._slots.finish_execution(owner, job.implementation_execution_id)
            try:
                get_trace_collector().record_event(
                    EventKind.STAGE_RESULT,
                    stage_id="issue.retry-predecessor-retirement",
                    origin="issue.retry-predecessor-retirement",
                    label=f"issue#{authority.target_number} stopped retry predecessor",
                    outcome=Outcome.SUPERSEDED,
                    facts={"request_id": authority.request_id, "job_id": job.job_id, "execution_id": job.implementation_execution_id, "reason": "explicit retry after runner and writer settlement"},
                )
            except Exception:
                logger.opt(exception=True).debug("Could not trace stopped predecessor retirement for issue#{}", authority.target_number)
        # A dead execution without a transferred job is also safe to reclaim.
        self._slots.reclaim_stale_executions()
        return None

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
                with self._slots.serialize(ImplementationOwner("issue", target_number)):
                    authority = self._routing.retry_request(request_id)
                    if authority is None or authority.status == "pending":
                        predecessors, reason = self._stopped_predecessors(repository, target_number)
                        if reason is not None:
                            return IssueRetryAdmission(IssueRetryDisposition.DEFERRED, authority, reason)
                        authority = self._routing.accept_retry_request(request_id, repository, target_number, generation)
                        reason = self._retire_stopped_predecessors(predecessors, authority)
                        if reason is not None:
                            return IssueRetryAdmission(IssueRetryDisposition.DEFERRED, authority, reason)
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
