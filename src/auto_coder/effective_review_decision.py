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
    CATEGORY_REGRESSION_GAP,
    CURRENCY_CURRENT_HEAD,
    OUTCOME_AMBIGUOUS,
    OUTCOME_CLOSURE_PROPOSAL_NOT_ACCEPTED,
    OUTCOME_STILL_VALID_OBSERVED,
    OUTCOME_UNAVAILABLE,
    UNAVAILABLE,
    AcceptedFindingProjection,
    AcceptedFindingRecord,
    ProjectionTarget,
)
from .adversarial_validator import AdversarialValidationFinding, AdversarialValidationResult, RequirementCoverageEntry
from .reviewer_session_registry import TestOracleGap


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
    accepted_state: str = ""
    source_revision: int = 0
    association_revision: int = 0


@dataclass(frozen=True)
class EffectiveDecisionBinding:
    target: ProjectionTarget = field(default_factory=ProjectionTarget)
    validation_attempt_id: str = ""
    validation_attempt_sequence: int = 0
    source_revision: int = -1
    finding_set_revision: int = 0
    association_revision: int = 0
    reopen_epoch: int = 0


@dataclass(frozen=True)
class EffectiveReviewDecision:
    status: str = "BLOCKED"
    corrections: tuple[EffectiveCorrection, ...] = ()
    pending_closures: tuple[EffectiveCorrection, ...] = ()
    pending_reconciliation: tuple[EffectiveCorrection, ...] = ()
    ordinary_findings: tuple[AdversarialValidationFinding, ...] = ()
    ordinary_gaps: tuple[TestOracleGap, ...] = ()
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
        reopen_epoch=projection.open_epoch,
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
        accepted_state=record.accepted_state,
        source_revision=record.source_revision,
        association_revision=record.association_revision,
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
    pending_closures: list[EffectiveCorrection] | None = None,
    pending_reconciliation: list[EffectiveCorrection] | None = None,
    ordinary_findings: tuple[AdversarialValidationFinding, ...] = (),
    ordinary_gaps: tuple[TestOracleGap, ...] = (),
) -> EffectiveReviewDecision:
    # Source identity is the lifecycle identity.  It prevents aliases or
    # repeated observations from duplicating a correction.
    distinct = {item.source_identity: item for item in corrections}
    ordered = tuple(distinct[key] for key in sorted(distinct))
    closures = {item.source_identity: item for item in pending_closures or []}
    reconciliation = {item.source_identity: item for item in pending_reconciliation or []}
    return EffectiveReviewDecision(
        status=status,
        corrections=ordered,
        pending_closures=tuple(closures[key] for key in sorted(closures)),
        pending_reconciliation=tuple(reconciliation[key] for key in sorted(reconciliation)),
        ordinary_findings=ordinary_findings,
        ordinary_gaps=ordinary_gaps,
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
        retained = [_correction(record, "Known accepted obligation retained during an unfinished ordinary invocation") for record in projection.unresolved if record.evidence_currency == CURRENCY_CURRENT_HEAD]
        historical = [_correction(record, "Historical accepted obligation retained during an unfinished ordinary invocation") for record in projection.unresolved if record.evidence_currency != CURRENCY_CURRENT_HEAD]
        return _decision(result, projection, raw_status, EffectiveNextAction.OPERATIONAL_WAIT, [f"Ordinary validation ended with {raw_status}"], retained, pending_reconciliation=historical)

    if raw_status not in {"PASS", "NEEDS_FIX", "NEEDS_TESTS"}:
        return _decision(result, projection, "BLOCKED", EffectiveNextAction.RECONCILIATION, [f"Unsupported ordinary status {raw_status or '<empty>'}"], [])

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

    # An accepted regression gap that normalization promoted to a finding (an explicit test
    # deliverable independently upheld for this target) keeps its identity but is an implementation repair.
    promoted_gap_ids = {identity for finding in result.findings for identity in (finding.finding_identity, finding.correction_identity) if identity}
    promoted_sources: set[str] = set()
    repairs: list[EffectiveCorrection] = []
    pending_closure: list[EffectiveCorrection] = []
    reconciliation: list[EffectiveCorrection] = []
    authority_gaps: list[EffectiveCorrection] = []
    for record in projection.records:
        if record.accepted_state != "OPEN":
            if record.accepted_state not in {"FIXED", "INVALID"}:
                authority_gaps.append(_correction(record, "Accepted finding has no recognized lifecycle closure"))
            elif record.target_binding != BINDING_CURRENT:
                authority_gaps.append(_correction(record, "Accepted closure is not applicable to the current target"))
            continue
        observations = tuple(outcome for outcome in record.current_observations if outcome.source_identity == record.source_identity)
        closure = next((outcome for outcome in observations if outcome.outcome == OUTCOME_CLOSURE_PROPOSAL_NOT_ACCEPTED), None)
        if record.target_binding != BINDING_CURRENT or not record.canonical_blocker_id or record.association in {AMBIGUOUS, UNAVAILABLE} or any(outcome.outcome in {OUTCOME_AMBIGUOUS, OUTCOME_UNAVAILABLE} for outcome in observations):
            authority_gaps.append(_correction(record, "Accepted finding lacks unambiguous current-target authority"))
        elif record.category not in {CATEGORY_IMPLEMENTATION, CATEGORY_REGRESSION_GAP}:
            authority_gaps.append(_correction(record, "Accepted finding has an unsupported category"))
        elif closure is not None:
            pending_closure.append(_correction(record, closure.detail or "Exact closure proposal awaits lifecycle acceptance"))
        elif record.evidence_currency == CURRENCY_CURRENT_HEAD or any(outcome.outcome == OUTCOME_STILL_VALID_OBSERVED for outcome in observations):
            repairs.append(_correction(record, "Accepted OPEN finding is independently upheld for the current target"))
            if record.category == CATEGORY_REGRESSION_GAP and record.known_gap_id in promoted_gap_ids:
                promoted_sources.add(record.source_identity)
        else:
            reconciliation.append(_correction(record, "Accepted OPEN finding lacks current-target adjudication"))

    if authority_gaps:
        return _decision(
            result,
            projection,
            "BLOCKED",
            EffectiveNextAction.RECONCILIATION,
            [item.reason for item in authority_gaps],
            repairs,
            pending_closures=pending_closure,
            pending_reconciliation=[*authority_gaps, *reconciliation],
        )
    # Lifecycle closure cannot complete while another current violation remains.
    # Dispatch the demonstrated correction without reintroducing addressed counterexamples.
    implementation = [item for item in repairs if item.category == CATEGORY_IMPLEMENTATION or item.source_identity in promoted_sources]
    tests = [item for item in repairs if item.category == CATEGORY_REGRESSION_GAP and item.source_identity not in promoted_sources]

    # Ordinary evidence has already been normalized against the explicit contract.
    # A status label alone is not repair authority, and accepted identities are
    # adjudicated exclusively through their records above, never through restated prose.
    demonstrated = tuple(finding for finding in result.findings if finding.evidence_classification == "DEMONSTRATED" and finding.counterexample.strip())
    finding_requirements = {requirement_id for finding in demonstrated for requirement_id in finding.all_requirement_ids}
    coverage_complete = bool(result.requirement_coverage) and all(entry.status in {"VERIFIED", "IRRELEVANT"} or (raw_status == "NEEDS_FIX" and entry.status == "VIOLATED" and entry.requirement_id in finding_requirements) for entry in result.requirement_coverage)
    ordinary_complete = coverage_complete and not result.specification_gaps and not result.unexplained_changes
    accepted_identities = {identity for record in projection.records for identity in (record.source_identity, record.canonical_blocker_id, record.known_gap_id) if identity}
    violated = {entry.requirement_id for entry in result.requirement_coverage if entry.status == "VIOLATED"}
    covered = {entry.requirement_id for entry in result.requirement_coverage if entry.status in {"VERIFIED", "VIOLATED"}}
    ordinary_findings = (
        tuple(finding for finding in demonstrated if violated.intersection(finding.all_requirement_ids) and not accepted_identities.intersection((finding.finding_identity, finding.correction_identity.removeprefix("canonical:")))) if ordinary_complete and raw_status == "NEEDS_FIX" else ()
    )
    ordinary_gaps = (
        tuple(
            gap
            for gap in result.open_test_oracle_gaps
            if not accepted_identities.intersection((gap.gap_id, gap.correction_identity.removeprefix("canonical:")))
            and gap.requirement_id in covered
            and all((gap.gap_id, gap.authoritative_boundary, gap.invariant, gap.plausible_incorrect_implementation, gap.why_tests_still_pass, gap.material_consequence, gap.focused_regression_scenario))
        )
        if ordinary_complete and raw_status in {"NEEDS_FIX", "NEEDS_TESTS"}
        else ()
    )
    if implementation or ordinary_findings:
        reasons = [item.reason for item in repairs]
        if ordinary_findings:
            reasons.append("Ordinary validation produced independently demonstrated implementation findings")
        return _decision(
            result,
            projection,
            "NEEDS_FIX",
            EffectiveNextAction.IMPLEMENTATION_REPAIR,
            reasons,
            repairs,
            pending_closures=pending_closure,
            pending_reconciliation=reconciliation,
            ordinary_findings=ordinary_findings,
            ordinary_gaps=ordinary_gaps,
        )
    if tests or ordinary_gaps:
        reasons = [item.reason for item in tests]
        if ordinary_gaps:
            reasons.append("Ordinary validation produced an independently established focused test gap")
        return _decision(
            result,
            projection,
            "NEEDS_TESTS",
            EffectiveNextAction.FOCUSED_TEST_REPAIR,
            reasons,
            tests,
            pending_closures=pending_closure,
            pending_reconciliation=reconciliation,
            ordinary_gaps=ordinary_gaps,
        )
    if not ordinary_complete:
        return _decision(result, projection, "BLOCKED", EffectiveNextAction.RECONCILIATION, ["Specification, provenance, or required coverage remains unresolved"], [], pending_closures=pending_closure, pending_reconciliation=reconciliation)
    if reconciliation:
        return _decision(result, projection, "BLOCKED", EffectiveNextAction.RECONCILIATION, [item.reason for item in reconciliation], [], pending_closures=pending_closure, pending_reconciliation=reconciliation)
    if pending_closure:
        return _decision(result, projection, "BLOCKED", EffectiveNextAction.CLOSURE_ACCEPTANCE, [item.reason for item in pending_closure], [], pending_closures=pending_closure)
    if raw_status == "PASS" and not result.findings and not result.open_test_oracle_gaps:
        return _decision(result, projection, "PASS", EffectiveNextAction.NONE, [], [], approve=True)
    return _decision(result, projection, "BLOCKED", EffectiveNextAction.RECONCILIATION, ["Ordinary verdict lacks an independently substantiated current correction"], [])
