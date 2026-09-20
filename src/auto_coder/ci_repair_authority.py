"""Fresh, exact-head authority for starting CI-triggered repair work."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from .ci_observation import (
    CheckObservation,
    CIConclusion,
    CIObservationSnapshot,
    ObservationAvailability,
    WorkflowObservation,
)
from .github_ci_observer import ci_observation_merge_authority, ci_read_phase, observe_ci
from .util.gh_cache import get_ghapi_client

_FAILURES = frozenset({CIConclusion.FAILURE, CIConclusion.TIMED_OUT, CIConclusion.CANCELLED})
_SETTLED = _FAILURES | frozenset({CIConclusion.SUCCESS, CIConclusion.SKIPPED, CIConclusion.NEUTRAL})


@dataclass(frozen=True)
class CIRepairAuthority:
    """Result of the final admission read at a repair-effect boundary."""

    allowed: bool
    reason: str
    head_sha: str
    snapshot: CIObservationSnapshot | None = None

    @property
    def failure_identities(self) -> tuple[str, ...]:
        if self.snapshot is None:
            return ()
        identities: list[str] = []
        for fact in self.snapshot.facts:
            if fact.conclusion not in _FAILURES:
                continue
            if isinstance(fact, WorkflowObservation):
                workflow_execution = fact.execution
                identities.append(f"workflow:{workflow_execution.workflow_id}:run:{workflow_execution.run_id}:attempt:{workflow_execution.attempt}")
            elif isinstance(fact, CheckObservation):
                check_execution = fact.execution
                identities.append(f"check:{check_execution.app_id}:{check_execution.check_id}")
        return tuple(identities)


def _current_attempt_facts(snapshot: CIObservationSnapshot) -> tuple[Any, ...]:
    """Discard explicitly superseded workflow attempts, without guessing identity."""
    newest: dict[tuple[str, str], int] = {}
    for fact in snapshot.facts:
        if isinstance(fact, WorkflowObservation) and fact.execution.attempt is not None:
            execution_key = (fact.execution.workflow_id, fact.execution.run_id)
            newest[execution_key] = max(newest.get(execution_key, 0), fact.execution.attempt)
    return tuple(fact for fact in snapshot.facts if not isinstance(fact, WorkflowObservation) or fact.execution.attempt is None or fact.execution.attempt == newest[(fact.execution.workflow_id, fact.execution.run_id)])


def evaluate_ci_repair_snapshot(snapshot: CIObservationSnapshot) -> tuple[bool, str]:
    """Apply the fail-closed selected-fact policy to one complete read cycle."""
    if snapshot.availability is not ObservationAvailability.KNOWN:
        return False, f"CI evidence is {snapshot.availability.value}"
    facts = _current_attempt_facts(snapshot)
    if not facts:
        return False, "CI evidence is empty"
    if any(fact.conclusion not in _SETTLED for fact in facts):
        return False, "CI evidence contains pending, unknown, or action-required facts"
    if not any(fact.conclusion in _FAILURES for fact in facts):
        return False, "CI has no current terminal failure"
    return True, "current exact-head CI failure"


@contextmanager
def current_ci_failure_authority(
    github_client: Any,
    repository: str,
    pr_number: int,
    expected_head: str,
) -> Iterator[CIRepairAuthority]:
    """Re-read PR and CI, then fence accepted invalidations through first effect.

    Callers must enter this context only after read-only preparation and put the
    first provider request or local repair effect inside it.  The authority is
    deliberately neither persisted nor reusable by a later repair initiation.
    """
    if github_client is None:
        yield CIRepairAuthority(False, "an authoritative GitHub client is unavailable", expected_head)
        return
    try:
        metadata = github_client.get_pull_request_metadata_strict(repository, pr_number)
    except Exception as exc:
        yield CIRepairAuthority(False, f"strict PR read is unavailable: {exc}", expected_head)
        return
    head = metadata.get("head") if isinstance(metadata, dict) else None
    live_head = head.get("sha") if isinstance(head, dict) else None
    if metadata.get("state") != "open" or metadata.get("merged_at") is not None:
        yield CIRepairAuthority(False, "PR is no longer open", expected_head)
        return
    if live_head != expected_head:
        yield CIRepairAuthority(False, "PR head changed before repair initiation", expected_head)
        return
    stack = ExitStack()
    try:
        token = github_client.token
        api = get_ghapi_client(token)
        stack.enter_context(ci_read_phase("ci-repair-final-admission"))
        snapshot = observe_ci(api, token, repository, pr_number, expected_head)
        allowed, reason = evaluate_ci_repair_snapshot(snapshot)
        if allowed:
            current = stack.enter_context(ci_observation_merge_authority(snapshot))
    except Exception as exc:
        stack.close()
        yield CIRepairAuthority(False, f"CI observation is unavailable: {exc}", expected_head)
        return
    if not allowed:
        stack.close()
        yield CIRepairAuthority(False, reason, expected_head, snapshot)
        return
    if not current:
        stack.close()
        yield CIRepairAuthority(
            False,
            "CI evidence was invalidated before repair initiation",
            expected_head,
            snapshot,
        )
        return
    try:
        yield CIRepairAuthority(True, reason, expected_head, snapshot)
    finally:
        stack.close()
