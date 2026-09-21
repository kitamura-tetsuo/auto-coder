"""Read-only dashboard projections for repository-scoped jobs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence

from .repo_job_trace import (
    RepoJobExecutionSummary,
    RepoJobFacts,
    RepoJobObservation,
    RepoJobSnapshot,
    RepoJobTarget,
    executions_for_target,
    observations_for_execution,
    unassociated_observations_for_target,
)


@dataclass(frozen=True)
class RepoJobSelection:
    execution: Optional[RepoJobExecutionSummary]
    pinned_execution_id: Optional[str]
    pinned_evicted: bool


@dataclass
class RepoJobPageState:
    pinned_execution_id: Optional[str] = None
    last_sequence: int = -1
    last_snapshot: Optional[RepoJobSnapshot] = None
    signature: Optional[object] = None
    refreshing: bool = False


def select_execution(
    snapshot: RepoJobSnapshot,
    target: RepoJobTarget,
    pinned_execution_id: Optional[str],
) -> RepoJobSelection:
    """Follow newest start sequence, or preserve an exact pinned identity."""
    executions = executions_for_target(snapshot, target)
    if pinned_execution_id is None:
        return RepoJobSelection(executions[-1] if executions else None, None, False)
    selected = next((item for item in executions if item.execution_id == pinned_execution_id), None)
    return RepoJobSelection(selected, pinned_execution_id, selected is None)


def observation_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")


def status_text(summary: RepoJobExecutionSummary, observations: Sequence[RepoJobObservation]) -> str:
    """Describe only recorded attempt state; never infer downstream success."""
    if not summary.finished:
        return "Running"
    outcome = summary.outcome or "unknown/unavailable"
    if outcome == "completed":
        completed = any(item.stage_id == "dependency-rescan.completed" and item.outcome == "completed" for item in observations)
        return "Completed" if completed else "Finished; rescan completion unavailable"
    if outcome == "failed":
        return "Partial failure" if any(_has_confirmed_handoff(item.facts) for item in observations) else "Failed"
    if outcome == "cancelled":
        return "Cancelled"
    if outcome == "deferred":
        return "Deferred"
    return f"Unknown/unavailable ({outcome})"


def _has_confirmed_handoff(facts: Optional[RepoJobFacts]) -> bool:
    return facts is not None and bool(facts.confirmed_handoff_count)


def facts_rows(facts: Optional[RepoJobFacts]) -> tuple[tuple[str, str], ...]:
    """Project explicitly recorded facts without deriving absent values."""
    if facts is None:
        return ()
    rows: list[tuple[str, str]] = []
    scalar_fields = (
        ("Observed generation", facts.observed_invalidation_generation),
        ("Trigger event", facts.trigger_event),
        ("Trigger action", facts.trigger_action),
        ("Queue phase", facts.queue_phase),
        ("Worker phase", facts.worker_phase),
        ("Scan available", facts.scan_available),
        ("Discovered Issues", facts.discovered_issue_count),
        ("Attempted handoffs", facts.attempted_handoff_count),
        ("Confirmed handoffs", facts.confirmed_handoff_count),
        ("Failed/unconfirmed handoffs", facts.failed_or_unconfirmed_handoff_count),
        ("New pending", facts.new_pending_handoff_count),
        ("Coalesced", facts.coalesced_handoff_count),
        ("Follow-up required", facts.followup_required_handoff_count),
        ("Handoff disposition", facts.handoff_disposition),
        ("Failure", facts.failure_reason),
        ("Scheduled wake (not an eligibility deadline)", facts.scheduled_retry_not_before),
    )
    rows.extend((name, "unavailable" if value is None else str(value).lower() if isinstance(value, bool) else str(value)) for name, value in scalar_fields)
    return tuple(rows)


def selected_observations(snapshot: RepoJobSnapshot, selection: RepoJobSelection) -> list[RepoJobObservation]:
    if selection.execution is None:
        return []
    return observations_for_execution(snapshot, selection.execution.execution_id)


def pending_observations(snapshot: RepoJobSnapshot, target: RepoJobTarget) -> list[RepoJobObservation]:
    return unassociated_observations_for_target(snapshot, target)
