"""Pure derivation of the effective ordinary-review decision.

The model result is diagnostic evidence, not the complete decision.  Accepted
Strong findings are owned by their durable lifecycle and are folded in here
without changing either that lifecycle or GitHub state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .accepted_finding_bridge import (
    AMBIGUOUS,
    BINDING_CURRENT,
    CATEGORY_IMPLEMENTATION,
    CURRENCY_CURRENT_HEAD,
    OUTCOME_AMBIGUOUS,
    OUTCOME_CLOSURE_PROPOSAL_NOT_ACCEPTED,
    OUTCOME_STILL_VALID_OBSERVED,
    OUTCOME_UNAVAILABLE,
    AcceptedFindingProjection,
    AcceptedFindingRecord,
    ProjectionTarget,
)
from .adversarial_validator import AdversarialValidationResult, RequirementCoverageEntry


class EffectiveNextAction(str, Enum):
    IMPLEMENTATION_REPAIR = "IMPLEMENTATION_REPAIR"
    FOCUSED_TEST_REPAIR = "FOCUSED_TEST_REPAIR"
    RECONCILIATION = "RECONCILIATION"
    CLOSURE_ACCEPTANCE = "CLOSURE_ACCEPTANCE"
    OPERATIONAL_WAIT = "OPERATIONAL_WAIT"
    NONE = "NONE"


@dataclass(frozen=True)
class EffectiveCorrection:
    """One original accepted correction, never inferred from model prose."""

    blocker_id: str = ""
    source_identity: str = ""
    category: str = ""
    qualified_requirements: tuple[str, ...] = ()
    correction_scope: str = ""
    affected_boundary: str = ""
    evidence: str = ""
    reason: str = ""


@dataclass(frozen=True)
class EffectiveDecisionBinding:
    target: ProjectionTarget = field(default_factory=ProjectionTarget)
    validation_attempt_id: str = ""
    validation_attempt_sequence: int = 0
    source_revision: int = -1
    finding_set_revision: int = 0
    association_revision: int = 0


@dataclass(frozen=True)
class EffectiveReviewDecision:
    status: str = "BLOCKED"
    corrections: tuple[EffectiveCorrection, ...] = ()
    reasons: tuple[str, ...] = ()
    binding: EffectiveDecisionBinding = field(default_factory=EffectiveDecisionBinding)
    approval_eligible: bool = False
    next_action: EffectiveNextAction = EffectiveNextAction.RECONCILIATION
    raw_result: str = ""
    requirement_coverage: tuple[RequirementCoverageEntry, ...] = ()

    @property
    def blocker_ids(self) -> tuple[str, ...]:
        return tuple(correction.blocker_id for correction in self.corrections)


def _binding(result: AdversarialValidationResult, projection: AcceptedFindingProjection) -> EffectiveDecisionBinding:
    return EffectiveDecisionBinding(
        target=projection.target,
        validation_attempt_id=result.attempt_id,
        validation_attempt_sequence=result.attempt_sequence,
        source_revision=projection.source_revision,
        finding_set_revision=projection.finding_set_revision,
        association_revision=projection.ledger_revision,
    )


def _correction(record: AcceptedFindingRecord, reason: str) -> EffectiveCorrection:
    requirements = tuple(f"#{item.issue_number}/{item.requirement_id}" for item in record.qualified_requirements)
    return EffectiveCorrection(
        blocker_id=record.canonical_blocker_id,
        source_identity=record.source_identity,
        category=record.category,
        qualified_requirements=requirements,
        correction_scope=record.original_scope,
        affected_boundary=record.affected_boundary,
        evidence=record.evidence,
        reason=reason,
    )


def _decision(
    result: AdversarialValidationResult,
    projection: AcceptedFindingProjection,
    status: str,
    action: EffectiveNextAction,
    reasons: list[str],
    corrections: list[EffectiveCorrection],
    *,
    approve: bool = False,
) -> EffectiveReviewDecision:
    # Source identity is the lifecycle identity.  It prevents aliases or
    # repeated observations from duplicating a correction.
    distinct = {item.source_identity: item for item in corrections}
    ordered = tuple(distinct[key] for key in sorted(distinct))
    return EffectiveReviewDecision(
        status=status,
        corrections=ordered,
        reasons=tuple(reasons),
        binding=_binding(result, projection),
        approval_eligible=approve,
        next_action=action,
        raw_result=result.result,
        requirement_coverage=tuple(result.requirement_coverage),
    )


def derive_effective_review_decision(
    result: AdversarialValidationResult,
    projection: AcceptedFindingProjection,
) -> EffectiveReviewDecision:
    """Combine an ordinary result and authoritative accepted-finding state.

    This function is deliberately side-effect free.  In particular, an
    ADDRESSED observation is only a request to the lifecycle owner; it does
    not close the accepted finding here.
    """

    raw_status = result.result.strip().upper()
    if raw_status in {"ERROR", "EXHAUSTED", "BLOCKED", "INCONCLUSIVE"}:
        return _decision(result, projection, raw_status, EffectiveNextAction.OPERATIONAL_WAIT, [f"Ordinary validation ended with {raw_status}"], [])

    if not projection.complete:
        reasons = ["Accepted-finding projection is incomplete"]
        reasons.extend(diagnostic.code for diagnostic in projection.diagnostics)
        return _decision(result, projection, "BLOCKED", EffectiveNextAction.RECONCILIATION, reasons, [])

    unresolved_outcomes = [outcome for outcome in projection.disposition_outcomes if outcome.outcome in {OUTCOME_AMBIGUOUS, OUTCOME_UNAVAILABLE} and not outcome.source_identity]
    if unresolved_outcomes:
        return _decision(
            result,
            projection,
            "BLOCKED",
            EffectiveNextAction.RECONCILIATION,
            [outcome.detail or outcome.outcome for outcome in unresolved_outcomes],
            [],
        )

    repairs: list[EffectiveCorrection] = []
    pending_closure: list[EffectiveCorrection] = []
    reconciliation: list[EffectiveCorrection] = []
    for record in projection.unresolved:
        observations = tuple(outcome for outcome in record.current_observations if outcome.source_identity == record.source_identity)
        closure = next((outcome for outcome in observations if outcome.outcome == OUTCOME_CLOSURE_PROPOSAL_NOT_ACCEPTED), None)
        if record.target_binding != BINDING_CURRENT or not record.canonical_blocker_id or record.association in {AMBIGUOUS}:
            reconciliation.append(_correction(record, "Accepted finding lacks unambiguous current-target authority"))
        elif closure is not None:
            pending_closure.append(_correction(record, closure.detail or "Exact closure proposal awaits lifecycle acceptance"))
        elif record.evidence_currency == CURRENCY_CURRENT_HEAD or any(outcome.outcome == OUTCOME_STILL_VALID_OBSERVED for outcome in observations):
            repairs.append(_correction(record, "Accepted OPEN finding is independently upheld for the current target"))
        else:
            reconciliation.append(_correction(record, "Accepted OPEN finding lacks current-target adjudication"))

    if reconciliation:
        return _decision(result, projection, "BLOCKED", EffectiveNextAction.RECONCILIATION, [item.reason for item in reconciliation], reconciliation)
    if pending_closure:
        return _decision(result, projection, "BLOCKED", EffectiveNextAction.CLOSURE_ACCEPTANCE, [item.reason for item in pending_closure], pending_closure)

    implementation = [item for item in repairs if item.category == CATEGORY_IMPLEMENTATION]
    tests = [item for item in repairs if item.category != CATEGORY_IMPLEMENTATION]
    if implementation:
        return _decision(result, projection, "NEEDS_FIX", EffectiveNextAction.IMPLEMENTATION_REPAIR, [item.reason for item in repairs], repairs)
    if tests:
        return _decision(result, projection, "NEEDS_TESTS", EffectiveNextAction.FOCUSED_TEST_REPAIR, [item.reason for item in tests], tests)

    coverage_complete = bool(result.requirement_coverage) and all(entry.status in {"VERIFIED", "IRRELEVANT"} for entry in result.requirement_coverage)
    if result.specification_gaps or result.unexplained_changes or not coverage_complete:
        return _decision(result, projection, "BLOCKED", EffectiveNextAction.RECONCILIATION, ["Specification, provenance, or required coverage remains unresolved"], [])
    if raw_status == "NEEDS_FIX":
        return _decision(result, projection, "NEEDS_FIX", EffectiveNextAction.IMPLEMENTATION_REPAIR, ["Ordinary validation produced implementation findings"], [])
    if raw_status == "NEEDS_TESTS":
        return _decision(result, projection, "NEEDS_TESTS", EffectiveNextAction.FOCUSED_TEST_REPAIR, ["Ordinary validation produced a focused test gap"], [])
    if raw_status == "PASS":
        return _decision(result, projection, "PASS", EffectiveNextAction.NONE, [], [], approve=True)
    return _decision(result, projection, "BLOCKED", EffectiveNextAction.RECONCILIATION, [f"Unsupported ordinary status {raw_status or '<empty>'}"], [])
