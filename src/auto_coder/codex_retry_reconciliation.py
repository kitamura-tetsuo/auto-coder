"""Reconcile already-accepted Codex retry work across durable stores.

This module deliberately has no dispatch or submission dependency.  It repairs
bookkeeping for a provider responsibility that was accepted before entry.
"""

import json
import os
from collections.abc import Callable
from pathlib import Path

from .automation_config import ExplicitTargetOutcome
from .cloud_manager import CloudManager, CloudTaskBinding
from .cloud_run import CloudRun, CloudRunRepository
from .codex_cloud_task import canonical_codex_cloud_task_url
from .implementation_slots import ImplementationOwner, ImplementationSlotRepository
from .issue_stage_routing import IssueStageRoutingStore
from .retry_dispatch import RetryDispatchRepository


class CodexRetryReconciler:
    """Join one repository's accepted retry evidence without provider work."""

    def __init__(
        self,
        repository: str,
        slots: ImplementationSlotRepository,
        defer: Callable[[str, int], None],
    ) -> None:
        if slots.repo_name != repository:
            raise ValueError("Codex retry reconciler requires repository-matched slots")
        self.repository = repository
        self.slots = slots
        self._defer = defer

    def reconcile(self, request_id: str) -> tuple[ExplicitTargetOutcome, str]:
        """Repair and confirm one accepted receipt across durable projections."""
        dispatch = RetryDispatchRepository(self.repository)
        handoff = dispatch.get(request_id)
        routing_path = Path(os.environ.get("AUTO_CODER_ISSUE_STAGE_ROUTING_DB", "~/.auto-coder/issue-stage-routing.sqlite3")).expanduser()
        try:
            authority = IssueStageRoutingStore(routing_path).retry_request(request_id)
        except Exception as exc:
            if handoff is not None:
                self._defer(request_id, handoff.issue_number)
            return ExplicitTargetOutcome.DEFERRED, f"retry request {request_id} authority evidence is unreadable: {exc}"
        if handoff is None:
            if authority is None or authority.repository != self.repository or authority.status != "owned" or not authority.ownership_reference:
                return ExplicitTargetOutcome.FAILED, f"retry request {request_id} has no durable dispatch receipt or owned recovery authority"
            manager = CloudManager(self.repository)
            binding = manager.read_bindings_strict().get(str(authority.target_number))
            matching_runs = [
                run
                for run in CloudRunRepository(self.repository).list_for_issue(authority.target_number)
                if binding is not None and run.provider == "codex-cloud" and run.submission_outcome == "accepted" and run.task_id == binding.task_id and run.backend_name == binding.backend_name and run.environment_id and run.base_branch
            ]
            if len(matching_runs) != 1:
                self._defer(request_id, authority.target_number)
                return ExplicitTargetOutcome.DEFERRED, f"retry request {request_id} has no durable dispatch receipt and matching accepted CloudRun provenance is unavailable or contradictory"
            recovered_run = matching_runs[0]
            try:
                handoff = dispatch.recover_accepted_receipt(
                    authority,
                    numeric_attempt=recovered_run.attempt,
                    backend_name=recovered_run.backend_name,
                    task_id=recovered_run.task_id,
                    task_url=canonical_codex_cloud_task_url(recovered_run.task_id) or recovered_run.task_url,
                    environment_id=recovered_run.environment_id,
                    base_branch=recovered_run.base_branch,
                )
            except Exception as exc:
                self._defer(request_id, authority.target_number)
                return ExplicitTargetOutcome.DEFERRED, f"retry request {request_id} accepted receipt recovery is incomplete: {exc}"
        identity = f"repo={self.repository} issue={handoff.issue_number} R={handoff.request_id} A={handoff.attempt_id} G={handoff.generation} N={handoff.numeric_attempt if handoff.numeric_attempt is not None else 'unassigned'}"
        if authority is None or (
            authority.repository,
            authority.target_number,
            authority.request_id,
            authority.attempt_id,
            authority.generation,
        ) != (
            handoff.repository,
            handoff.issue_number,
            handoff.request_id,
            handoff.attempt_id,
            handoff.generation,
        ):
            reason = f"{identity} phase=accepted-tracking-incomplete; durable request attribution is missing or contradictory"
            dispatch.mark_tracking_incomplete(request_id, reason)
            self._defer(request_id, handoff.issue_number)
            return ExplicitTargetOutcome.DEFERRED, reason
        if authority.status not in {"owned", "invalidated"} or not authority.ownership_reference:
            reason = f"{identity} phase=accepted-tracking-incomplete; durable implementation ownership is unavailable"
            dispatch.mark_tracking_incomplete(request_id, reason)
            self._defer(request_id, handoff.issue_number)
            return ExplicitTargetOutcome.DEFERRED, reason
        if handoff.outcome == "definitely-not-started":
            return ExplicitTargetOutcome.DEFERRED, f"{identity} phase=definitely-not-started; provider refused before acceptance"
        if handoff.outcome in {"claimed", "indeterminate"}:
            return ExplicitTargetOutcome.DEFERRED, f"{identity} phase=indeterminate-creation; reconciliation or operator attention is required"
        if handoff.route != "codex-cloud" or handoff.outcome not in {"accepted", "completed"} or not handoff.external_id or handoff.numeric_attempt is None:
            return ExplicitTargetOutcome.FAILED, f"{identity} has an invalid durable retry disposition"
        task_identity = f"{identity} T=codex-cloud/{handoff.external_id}/{handoff.backend_name}"

        # Logical ownership is recovered before provider projection.  This is
        # accounting for an already accepted responsibility, not permission to
        # execute again, so capacity and provider quota are intentionally not
        # consulted.  The owner fence also prevents retirement or a competing
        # retry from observing a half-restored incarnation.
        slots = self.slots
        owner = ImplementationOwner("issue", handoff.issue_number)
        historical_reason = None
        if authority.status == "invalidated":
            historical_reason = "retry authority was durably invalidated"
        elif handoff.projection_disposition == "accepted-historical" or not dispatch.is_latest_accepted(request_id):
            historical_reason = "a later accepted retry owns the current pointer"
        try:
            with slots.serialize(owner):
                restoration = slots.restore_accepted_retry_session(
                    owner,
                    request_id=handoff.request_id,
                    attempt_id=handoff.attempt_id,
                    generation=handoff.generation,
                    acquisition_reference=authority.ownership_reference,
                    session_id=handoff.external_id,
                )
                if restoration == "retired":
                    dispatch.mark_historical(request_id, "accepted ownership is retained in exact retired history")
                    return ExplicitTargetOutcome.SKIPPED, f"{task_identity} phase=accepted-historical; ownership is validly retired"
                if restoration == "conflict":
                    raise RuntimeError("logical owner belongs to an incompatible or unattributable generation")
                if historical_reason is not None:
                    dispatch.mark_historical(request_id, historical_reason)
                    return ExplicitTargetOutcome.SKIPPED, f"{task_identity} phase=accepted-historical; {historical_reason}"
        except Exception as exc:
            reason = f"{task_identity} phase=accepted-tracking-incomplete boundary=owner-reconstruction; {exc}"
            dispatch.mark_tracking_incomplete(request_id, reason)
            self._defer(request_id, handoff.issue_number)
            return ExplicitTargetOutcome.DEFERRED, reason

        run_repository = CloudRunRepository(self.repository)
        try:
            run = run_repository.get(handoff.issue_number, handoff.numeric_attempt)
            if run is None:
                retained_config = json.loads(handoff.route_config)
                if not isinstance(retained_config, dict):
                    raise ValueError("retained launch provenance is not an object")
                base_branch = retained_config.get("base_branch")
                if not isinstance(base_branch, str) or not base_branch or not handoff.environment_id:
                    raise ValueError("retained environment/base provenance is incomplete")
                run = run_repository.repair_accepted(
                    CloudRun(
                        repo_name=self.repository,
                        issue_number=handoff.issue_number,
                        attempt=handoff.numeric_attempt,
                        provider="codex-cloud",
                        task_id=handoff.external_id,
                        backend_name=handoff.backend_name,
                        environment_id=handoff.environment_id,
                        base_branch=base_branch,
                        submission_outcome="accepted",
                        task_url=canonical_codex_cloud_task_url(handoff.external_id) or handoff.external_url or "",
                        launch_identity=handoff.request_id,
                        publication_head_repository=str(retained_config.get("publication_head_repository", "")),
                        publication_head_ref=str(retained_config.get("publication_head_ref", "")),
                    )
                )
            if (run.provider, run.task_id, run.backend_name, run.submission_outcome) != ("codex-cloud", handoff.external_id, handoff.backend_name, "accepted"):
                raise ValueError("matching accepted CloudRun is contradictory")
        except Exception as exc:
            reason = f"{task_identity} phase=accepted-tracking-incomplete boundary=cloud-run; {exc}"
            dispatch.mark_tracking_incomplete(request_id, reason)
            self._defer(request_id, handoff.issue_number)
            return ExplicitTargetOutcome.DEFERRED, reason

        expected = CloudTaskBinding("codex-cloud", handoff.external_id, handoff.backend_name)
        manager = CloudManager(self.repository)
        try:
            with slots.serialize(owner):
                restoration = slots.restore_accepted_retry_session(
                    owner,
                    request_id=handoff.request_id,
                    attempt_id=handoff.attempt_id,
                    generation=handoff.generation,
                    acquisition_reference=authority.ownership_reference,
                    session_id=handoff.external_id,
                )
                if restoration == "retired":
                    dispatch.mark_historical(request_id, "accepted ownership is retained in exact retired history")
                    return ExplicitTargetOutcome.SKIPPED, f"{task_identity} phase=accepted-historical; ownership is validly retired"
                if restoration == "conflict":
                    raise RuntimeError("logical owner changed to an incompatible or unattributable generation")
                if not dispatch.is_latest_accepted(request_id):
                    dispatch.mark_historical(request_id, "a later accepted retry owns the current pointer")
                    return ExplicitTargetOutcome.SKIPPED, f"{task_identity} phase=accepted-historical; newer accepted work won confirmation"
                predecessor = None
                if handoff.predecessor_provider is not None and handoff.predecessor_task_id is not None and handoff.predecessor_backend_name is not None:
                    predecessor = CloudTaskBinding(handoff.predecessor_provider, handoff.predecessor_task_id, handoff.predecessor_backend_name)
                elif authority.predecessor_captured and authority.predecessor_provider is not None and authority.predecessor_task_id is not None and authority.predecessor_backend_name is not None:
                    predecessor = CloudTaskBinding(authority.predecessor_provider, authority.predecessor_task_id, authority.predecessor_backend_name)
                recognized = tuple(CloudTaskBinding(*item) for item in dispatch.accepted_predecessors(request_id))
                disposition = manager.promote_retry_binding(
                    handoff.issue_number,
                    expected,
                    predecessor,
                    lambda: dispatch.is_latest_accepted(request_id),
                    recognized,
                    authority.predecessor_captured,
                )
                if disposition == "historical":
                    dispatch.mark_historical(request_id, "a later accepted retry owns the current pointer")
                    return ExplicitTargetOutcome.SKIPPED, f"{task_identity} phase=accepted-historical; newer accepted work won projection"
                dispatch.mark_prior_accepted_historical(request_id)
                if not slots.record_provider_session(owner, handoff.external_id):
                    if slots.has_retired_session(handoff.external_id):
                        dispatch.mark_historical(request_id, "accepted ownership is retained in retired history")
                        return ExplicitTargetOutcome.SKIPPED, f"{task_identity} phase=accepted-historical; ownership is validly retired"
                    raise RuntimeError("logical implementation slot is unavailable")
                if not dispatch.is_latest_accepted(request_id) or manager.read_bindings_strict().get(str(handoff.issue_number)) != expected:
                    raise RuntimeError("current ownership changed during slot confirmation")
                if not handoff.tracking_complete:
                    dispatch.mark_tracking_complete(request_id)
                dispatch.mark_handoff_complete(request_id)
        except Exception as exc:
            actual = None
            try:
                actual = manager.read_bindings_strict().get(str(handoff.issue_number))
            except Exception:
                pass
            reason = f"{task_identity} phase=accepted-tracking-incomplete boundary=pointer-or-owner; actual={actual!r} expected={expected!r}; {exc}"
            dispatch.mark_tracking_incomplete(request_id, reason)
            self._defer(request_id, handoff.issue_number)
            return ExplicitTargetOutcome.DEFERRED, reason
        return ExplicitTargetOutcome.SUCCESS, f"{task_identity} phase=accepted-current; run, pointer, and slot bookkeeping confirmed"
