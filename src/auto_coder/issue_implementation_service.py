"""Issue-only implementation ownership, dispatch, and settlement orchestration.

Authorization remains controller-owned.  This service starts only after the
controller has validated a current Issue/family generation and supplies narrow
callbacks for repeating that check and entering the existing provider boundary.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from typing import Callable, Optional

from .automation_config import CandidateProcessingResult, ExplicitTargetOutcome
from .cloud_manager import CloudManager
from .cloud_run import CloudRunRepository
from .deployment_channel import repository_dispatch_authority
from .implementation_ownership import (
    OwnershipStartDecision,
    begin_implementation_ownership,
    confirm_implementation_ownership,
    evaluate_implementation_start,
)
from .implementation_slots import ImplementationOwner, ImplementationSlotRepository
from .issue_dispatch import DispatchOutcome
from .issue_retry_admission import IssueRetryAdmissionService, IssueRetryDisposition, IssueRetryOrigin
from .issue_stage_routing import ImplementationRetryRequest, IssueStageRoutingStore
from .logger_config import get_logger
from .stale_jules_recovery import find_pending_stale_jules_recovery

logger = get_logger(__name__)


@dataclass(frozen=True)
class IssueImplementationRequest:
    """Authorized inputs for one exact Issue implementation generation."""

    repository: str
    target_number: int
    initial_generation: str
    explicit_only: bool = False
    continue_execution: bool = False
    manual_retry: bool = False
    retry_request_id: Optional[str] = None
    urgent: bool = False
    resume_owned_generation: bool = False


@dataclass(frozen=True)
class IssueImplementationPorts:
    """Narrow controller/effect ports consumed by the orchestration boundary."""

    refresh_generation: Callable[[], Optional[str]]
    dispatch: Callable[[Optional[ImplementationRetryRequest]], CandidateProcessingResult]
    is_draining: Callable[[], bool]
    github_client: object | None = None
    reconciliation_client: object | None = None
    explain_duplicate: Optional[Callable[[str, str], str]] = None
    record_duplicate: Optional[Callable[[str, str], None]] = None
    record_retry: Optional[Callable[[ImplementationRetryRequest, bool], None]] = None
    record_not_started: Optional[Callable[[str, Optional[str]], None]] = None
    record_release: Optional[Callable[[bool], None]] = None
    bind_validation_identity: Optional[Callable[[str], bool]] = None
    owner_already_serialized: bool = False


class IssueImplementationService:
    """Own the Issue execution lifetime from ownership through settlement."""

    def __init__(self, routing: IssueStageRoutingStore, slots: ImplementationSlotRepository) -> None:
        self._routing = routing
        self._slots = slots

    def execute(self, request: IssueImplementationRequest, ports: IssueImplementationPorts) -> CandidateProcessingResult:
        owner = ImplementationOwner("issue", request.target_number)
        result = CandidateProcessingResult(type="issue", number=request.target_number)
        generation = request.initial_generation

        if ports.is_draining():
            result.target_outcome = ExplicitTargetOutcome.DEFERRED
            result.actions = ["Deferred - graceful shutdown began before implementation dispatch"]
            return result

        with repository_dispatch_authority(request.repository):
            current = ports.refresh_generation()
            if current != generation:
                result.target_outcome = ExplicitTargetOutcome.SKIPPED
                result.actions = ["Skipped - validated Issue generation changed before ownership admission"]
                return result
            if ports.is_draining():
                result.target_outcome = ExplicitTargetOutcome.DEFERRED
                result.actions = ["Deferred - graceful shutdown began before implementation ownership admission"]
                return result

            execution_id = self._slots.current_execution_id(owner) if request.continue_execution else None
            inherited_execution = execution_id is not None
            owner_existed = owner in self._slots.active_owners()
            retry_authority: Optional[ImplementationRetryRequest] = None
            retry_deferred = False
            already_owned = False

            if not inherited_execution:
                if request.manual_retry:
                    if request.retry_request_id is None:
                        raise ValueError("manual retry requires a durable request id")
                    admission = IssueRetryAdmissionService(self._routing, self._slots).admit(
                        request.repository,
                        request.target_number,
                        generation,
                        request.retry_request_id,
                        IssueRetryOrigin.MANUAL,
                        github_client=ports.github_client,
                        bypass_capacity=request.explicit_only,
                    )
                    if admission.disposition is IssueRetryDisposition.FAILED:
                        result.error = f"Cannot acquire durable Issue retry authority: {admission.reason}"
                        return result
                    retry_authority = admission.authority if admission.disposition is IssueRetryDisposition.OWNED else None
                    execution_id = retry_authority.ownership_reference if retry_authority is not None else None
                    retry_deferred = admission.disposition in {IssueRetryDisposition.DEFERRED, IssueRetryDisposition.STALE}
                    if admission.disposition is IssueRetryDisposition.DEFERRED:
                        result.actions = [f"Deferred - {admission.reason}"]
                else:
                    pending = find_pending_stale_jules_recovery(self._routing, request.repository, request.target_number)
                    if pending is not None and pending.generation != generation:
                        self._routing.invalidate_retry_request(
                            pending.request_id,
                            generation,
                            reason="Implementation generation changed since the automatic stale-Jules recovery was captured",
                        )
                        pending = None
                    if pending is not None:
                        admission = IssueRetryAdmissionService(self._routing, self._slots).admit(
                            request.repository,
                            request.target_number,
                            generation,
                            pending.request_id,
                            IssueRetryOrigin.STALE_JULES,
                            github_client=ports.github_client,
                        )
                        if admission.disposition is IssueRetryDisposition.FAILED:
                            result.error = f"Cannot acquire durable Issue retry authority: {admission.reason}"
                            return result
                        retry_authority = admission.authority if admission.disposition is IssueRetryDisposition.OWNED else None
                        execution_id = retry_authority.ownership_reference if retry_authority is not None else None
                        retry_deferred = admission.disposition in {IssueRetryDisposition.DEFERRED, IssueRetryDisposition.STALE}
                    else:
                        execution_id, already_owned = self._start(owner, generation, request, ports.github_client)

            if execution_id is None and not already_owned and not request.explicit_only and not retry_deferred:
                self._slots.reconcile(ports.reconciliation_client)
                current = ports.refresh_generation()
                if current != generation:
                    result.target_outcome = ExplicitTargetOutcome.SKIPPED
                    result.actions = ["Skipped - validated Issue generation changed during capacity reconciliation"]
                    return result
                execution_id, already_owned = self._start(owner, generation, request, ports.github_client)

        if already_owned:
            result.target_outcome = ExplicitTargetOutcome.SKIPPED
            execution = self._slots.current_execution_id(owner) or ""
            explanation = ports.explain_duplicate(generation, execution) if ports.explain_duplicate else "Skipped - this Issue generation already has implementation ownership"
            result.actions = [explanation]
            if ports.record_duplicate is not None:
                ports.record_duplicate(generation, explanation)
            return result
        if execution_id is None:
            reason = "active execution already exists" if self._slots.active_execution_ids(owner) else "logical implementation limit is occupied"
            result.target_outcome = ExplicitTargetOutcome.DEFERRED
            if not result.actions:
                result.actions = [f"Deferred - {reason} ({owner.key})"]
            result.capacity_deferred = not bool(self._slots.active_execution_ids(owner))
            return result

        if not inherited_execution and ports.bind_validation_identity is not None and not ports.bind_validation_identity(execution_id):
            self._slots.finish_execution(owner, execution_id)
            result.error = "Could not bind implementation ownership to validated Issue generation"
            return result

        try:
            if retry_authority is not None and ports.record_retry is not None:
                ports.record_retry(retry_authority, request.manual_retry)
            serialization = nullcontext() if ports.owner_already_serialized else self._slots.serialize(owner)
            with serialization:
                result = ports.dispatch(retry_authority)
                if retry_authority is not None:
                    result.actions.insert(0, f"Retry accepted for issue #{request.target_number}: request={retry_authority.request_id} attempt={retry_authority.attempt_id} phase=owned")
        finally:
            local_transfer = result.dispatch_result is not None and result.dispatch_result.outcome is DispatchOutcome.LOCAL_ACCEPTED
            if not inherited_execution and not local_transfer:
                self._slots.finish_execution(owner, execution_id)
                if result.cloud_submission_not_started is True:
                    self._routing.record_implementation_not_started(request.repository, request.target_number, generation, result.error or "all dispatch candidates confirmed not started")
                    if ports.record_not_started is not None:
                        ports.record_not_started(generation, result.error)
                    released = False
                    with self._slots.serialize(owner):
                        try:
                            runs = CloudRunRepository(request.repository).list_for_issue(owner.number)
                            binding = CloudManager(request.repository).read_bindings_strict().get(str(owner.number))
                            if not runs and binding is None:
                                released = self._slots.release_unbound_idle_owner(owner)
                        except Exception as exc:
                            logger.warning("Retaining {} after rejected Cloud submission: cannot confirm absent provider ownership: {}", owner.key, exc)
                    if ports.record_release is not None:
                        ports.record_release(released)
                if not owner_existed and result.actions == ["Skipped - another instance started processing (@auto-coder label added)"]:
                    self._slots.release_unbound_idle_owner(owner)
                self._slots.reconcile(ports.reconciliation_client)
        return result

    def _start(
        self,
        owner: ImplementationOwner,
        generation: str,
        request: IssueImplementationRequest,
        github_client: object | None,
    ) -> tuple[Optional[str], bool]:
        if request.resume_owned_generation:
            execution_id = self._slots.start_execution(
                owner,
                generation=generation,
                bypass_capacity=request.explicit_only,
                allow_urgent_emergency=request.urgent,
                github_client=github_client,
            )
            if execution_id is not None:
                confirm_implementation_ownership(self._routing, request.repository, owner, generation)
            return execution_id, False
        gate = evaluate_implementation_start(self._routing, self._slots, request.repository, owner, generation)
        if gate.decision is OwnershipStartDecision.ALREADY_OWNED:
            return None, True
        if not gate.may_start:
            return None, False
        begin_implementation_ownership(self._routing, request.repository, owner, gate)
        execution_id = self._slots.start_execution(
            owner,
            generation=generation,
            bypass_capacity=request.explicit_only,
            allow_urgent_emergency=request.urgent,
            github_client=github_client,
        )
        if execution_id is not None:
            confirm_implementation_ownership(self._routing, request.repository, owner, generation)
        return execution_id, False
