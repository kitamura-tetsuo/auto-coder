"""Pure pre-review routing policy for pull-request processing.

The policy consumes observations made by existing GitHub/CI adapters.  It does
not fetch evidence or perform any of the effects authorized by its routes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .ci_observation import CIObservationSnapshot, ObservationAvailability


class ProgressPhase(str, Enum):
    CANDIDATE_COLLECTION = "candidate_collection"
    IN_HANDLER = "in_handler"


class CIEvidenceState(str, Enum):
    PASSING = "passing"
    PENDING = "pending"
    FAILING = "failing"
    KNOWN_EMPTY = "known_empty"
    UNAVAILABLE = "unavailable"
    MISSING = "missing"
    SUBJECT_MISMATCH = "subject_mismatch"


class ProgressRoute(str, Enum):
    ADMIT_CANDIDATE = "admit_candidate"
    EXCLUDE_CANDIDATE = "exclude_candidate"
    REMEDIATE_CONFLICT = "remediate_conflict"
    BLOCK_EXHAUSTED_CONFLICT = "block_exhausted_conflict"
    WAIT_FOR_CI = "wait_for_ci"
    REPORT_CI_UNAVAILABLE = "report_ci_unavailable"
    DISPATCH_CI = "dispatch_ci"
    REPAIR_FAILED_CI = "repair_failed_ci"
    ENTER_REVIEW = "enter_review"


@dataclass(frozen=True)
class PRProgressScope:
    repository: str
    pr_number: int
    head_sha: str

    def __post_init__(self) -> None:
        if not self.repository or self.pr_number <= 0:
            raise ValueError("repository and a positive PR number are required")


@dataclass(frozen=True)
class PRProgressInputs:
    scope: PRProgressScope
    phase: ProgressPhase
    ci: CIObservationSnapshot | None
    mergeable: bool | None
    remediation_enabled: bool
    remediation_exhausted: bool = False
    ci_success: bool = False
    ci_pending: bool = False


@dataclass(frozen=True)
class PRProgressDecision:
    route: ProgressRoute
    reason: str
    ci_state: CIEvidenceState


def classify_ci_evidence(
    scope: PRProgressScope,
    snapshot: CIObservationSnapshot | None,
    *,
    ci_success: bool = False,
    ci_pending: bool = False,
) -> CIEvidenceState:
    """Classify only complete CI evidence for the exact requested PR head."""
    if snapshot is None or not scope.head_sha:
        return CIEvidenceState.MISSING
    subject = snapshot.subject
    if subject.repository != scope.repository or subject.pr_number != scope.pr_number or subject.head_sha != scope.head_sha:
        return CIEvidenceState.SUBJECT_MISMATCH
    if not snapshot.complete:
        return CIEvidenceState.UNAVAILABLE
    if snapshot.availability is ObservationAvailability.KNOWN_EMPTY:
        return CIEvidenceState.KNOWN_EMPTY
    if snapshot.availability is not ObservationAvailability.KNOWN:
        return CIEvidenceState.UNAVAILABLE
    if ci_pending:
        return CIEvidenceState.PENDING
    if ci_success:
        return CIEvidenceState.PASSING
    return CIEvidenceState.FAILING


def select_pr_progress(inputs: PRProgressInputs) -> PRProgressDecision:
    """Select the next existing executor without performing its effects."""
    ci_state = classify_ci_evidence(inputs.scope, inputs.ci, ci_success=inputs.ci_success, ci_pending=inputs.ci_pending)

    if inputs.phase is ProgressPhase.CANDIDATE_COLLECTION:
        if inputs.mergeable is False and inputs.remediation_enabled and ci_state in {CIEvidenceState.KNOWN_EMPTY, CIEvidenceState.UNAVAILABLE}:
            return PRProgressDecision(ProgressRoute.ADMIT_CANDIDATE, "affirmative conflict has an actionable remediation route", ci_state)
        if ci_state in {CIEvidenceState.PENDING, CIEvidenceState.UNAVAILABLE, CIEvidenceState.MISSING, CIEvidenceState.SUBJECT_MISMATCH}:
            return PRProgressDecision(ProgressRoute.EXCLUDE_CANDIDATE, "CI evidence is not ready for candidate processing", ci_state)
        if ci_state is CIEvidenceState.KNOWN_EMPTY and not (inputs.mergeable is False and inputs.remediation_enabled):
            return PRProgressDecision(ProgressRoute.EXCLUDE_CANDIDATE, "no current-head CI observations are available", ci_state)
        return PRProgressDecision(ProgressRoute.ADMIT_CANDIDATE, "candidate has an actionable current-head route", ci_state)

    if inputs.mergeable is False and inputs.remediation_exhausted:
        return PRProgressDecision(ProgressRoute.BLOCK_EXHAUSTED_CONFLICT, "mergeability remediation allowance is exhausted", ci_state)
    if inputs.mergeable is False and inputs.remediation_enabled:
        return PRProgressDecision(ProgressRoute.REMEDIATE_CONFLICT, "affirmative conflict is eligible for remediation", ci_state)
    if ci_state in {CIEvidenceState.PENDING, CIEvidenceState.KNOWN_EMPTY, CIEvidenceState.SUBJECT_MISMATCH}:
        return PRProgressDecision(ProgressRoute.WAIT_FOR_CI, "current-head CI evidence is pending or stale", ci_state)
    if ci_state in {CIEvidenceState.UNAVAILABLE, CIEvidenceState.MISSING}:
        return PRProgressDecision(ProgressRoute.REPORT_CI_UNAVAILABLE, "current-head CI evidence is unavailable or incomplete", ci_state)
    if ci_state is CIEvidenceState.FAILING:
        return PRProgressDecision(ProgressRoute.REPAIR_FAILED_CI, "complete current-head CI evidence is failing", ci_state)
    return PRProgressDecision(ProgressRoute.ENTER_REVIEW, "complete current-head CI evidence is passing", ci_state)
