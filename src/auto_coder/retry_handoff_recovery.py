"""Finish local bookkeeping for accepted Codex retry handoffs.

Provider acceptance is intentionally not success here.  A handoff is current
only after its accepted run, current pointer, and enclosing implementation slot
all retain the same task.  The durable receipt is also the recovery queue, so a
restart never needs to manufacture a new retry request or provider call.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .cloud_manager import CloudManager, CloudTaskBinding
from .cloud_run import CloudRun, CloudRunRepository
from .implementation_slots import ImplementationOwner, ImplementationSlotRepository
from .issue_stage_routing import IssueStageRoutingStore
from .retry_dispatch import RetryDispatchRepository, RetryHandoff


class RetryHandoffDisposition(str, Enum):
    SUCCESS = "success"
    DEFERRED = "deferred"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class RetryHandoffResult:
    disposition: RetryHandoffDisposition
    handoff: RetryHandoff
    phase: str
    reason: str

    @property
    def diagnostic(self) -> str:
        numeric = self.handoff.numeric_attempt if self.handoff.numeric_attempt is not None else "unassigned"
        task = self.handoff.external_id or "unknown"
        return f"issue #{self.handoff.issue_number} retry request={self.handoff.request_id} " f"attempt={self.handoff.attempt_id} numeric_attempt={numeric} task={task} " f"phase={self.phase}: {self.reason}"


def settle_codex_retry_handoff(
    repository: str,
    request_id: str,
    slots: ImplementationSlotRepository,
) -> RetryHandoffResult:
    """Confirm or repair every local projection for one exact accepted receipt."""
    dispatch = RetryDispatchRepository(repository)
    handoff = dispatch.get(request_id)
    if handoff is None:
        raise ValueError(f"unknown retry handoff {request_id!r}")
    if handoff.route != "codex-cloud":
        return RetryHandoffResult(RetryHandoffDisposition.DEFERRED, handoff, "receipt", "the retained retry is not a Codex Cloud handoff")
    if handoff.outcome == "definitely-not-started":
        return RetryHandoffResult(RetryHandoffDisposition.DEFERRED, handoff, "provider-refusal", handoff.diagnostic or "provider creation definitely did not start")
    if handoff.outcome in {"claimed", "indeterminate"} or not handoff.external_id:
        return RetryHandoffResult(RetryHandoffDisposition.DEFERRED, handoff, "creation-uncertain", handoff.diagnostic or "provider creation outcome is indeterminate")
    if handoff.numeric_attempt is None:
        dispatch.mark_tracking_incomplete(request_id, "accepted receipt has no allocated numeric attempt")
        refreshed = dispatch.get(request_id) or handoff
        return RetryHandoffResult(RetryHandoffDisposition.DEFERRED, refreshed, "run", "accepted receipt has no allocated numeric attempt")

    task_id = handoff.external_id
    binding = CloudTaskBinding("codex-cloud", task_id, handoff.backend_name)
    owner = ImplementationOwner("issue", handoff.issue_number)
    if slots.has_retired_session(task_id):
        historical = dispatch.mark_historical(request_id, "accepted task is retained in retired implementation history")
        return RetryHandoffResult(RetryHandoffDisposition.SKIPPED, historical, "historical", "accepted task is retained in retired implementation history")

    config = json.loads(handoff.route_config)
    run = CloudRun(
        repo_name=repository,
        issue_number=handoff.issue_number,
        attempt=handoff.numeric_attempt,
        provider="codex-cloud",
        task_id=task_id,
        backend_name=handoff.backend_name,
        environment_id=handoff.environment_id or "",
        base_branch=str(config.get("base_branch", "")),
        submission_outcome="accepted",
        task_url=handoff.external_url or "",
    )
    manager = CloudManager(repository)
    try:
        routing_path = Path(os.environ.get("AUTO_CODER_ISSUE_STAGE_ROUTING_DB", "~/.auto-coder/issue-stage-routing.sqlite3")).expanduser()
        authority = IssueStageRoutingStore(routing_path).retry_request(request_id)
        if authority is None or (authority.repository, authority.target_number, authority.attempt_id, authority.generation) != (
            repository,
            handoff.issue_number,
            handoff.attempt_id,
            handoff.generation,
        ):
            raise RuntimeError("durable retry authority provenance is unavailable or contradictory")
        CloudRunRepository(repository).repair_accepted(run)
        predecessor = None
        if handoff.predecessor_provider and handoff.predecessor_task_id:
            predecessor = CloudTaskBinding(handoff.predecessor_provider, handoff.predecessor_task_id, handoff.predecessor_backend_name or "")
        recognized = tuple(CloudTaskBinding(provider, task, backend) for provider, task, backend in dispatch.accepted_predecessors(request_id))
        projection = manager.promote_retry_binding(
            handoff.issue_number,
            binding,
            predecessor,
            lambda: dispatch.is_latest_accepted(request_id),
            recognized,
            authority.predecessor_captured,
        )
        if projection == "historical":
            historical = dispatch.mark_historical(request_id, "a later accepted retry owns the current pointer")
            return RetryHandoffResult(RetryHandoffDisposition.SKIPPED, historical, "historical", "a later accepted retry owns the current pointer")
        dispatch.mark_prior_accepted_historical(request_id)
        if not slots.record_provider_session(owner, task_id):
            raise RuntimeError("enclosing implementation slot is unavailable")
        # Recheck currentness and the exact pointer after the independently
        # durable slot write. A concurrent newer acceptance must win.
        if not dispatch.is_latest_accepted(request_id) or manager.read_bindings_strict().get(str(handoff.issue_number)) != binding:
            historical = dispatch.mark_historical(request_id, "a later accepted retry won during slot projection")
            return RetryHandoffResult(RetryHandoffDisposition.SKIPPED, historical, "historical", "a later accepted retry won during slot projection")
        completed = dispatch.mark_handoff_complete(request_id)
        return RetryHandoffResult(RetryHandoffDisposition.SUCCESS, completed, "complete", "accepted run, pointer, slot, and acknowledgement are confirmed")
    except Exception as exc:
        incomplete = dispatch.mark_tracking_incomplete(request_id, str(exc))
        return RetryHandoffResult(RetryHandoffDisposition.DEFERRED, incomplete, "tracking", str(exc))


def discover_unfinished_codex_handoffs(repository: str, slots: ImplementationSlotRepository) -> tuple[RetryHandoffResult, ...]:
    """Recover accepted receipts, including legacy provider-only completion."""
    results = []
    for handoff in RetryDispatchRepository(repository).list_accepted():
        if handoff.route != "codex-cloud" or handoff.outcome == "completed" or handoff.projection_disposition == "accepted-historical":
            continue
        result = settle_codex_retry_handoff(repository, handoff.request_id, slots)
        if result.disposition is not RetryHandoffDisposition.SUCCESS:
            results.append(result)
    return tuple(results)
