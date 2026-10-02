from dataclasses import replace

from auto_coder.accepted_finding_bridge import (
    ASSOCIATED,
    BINDING_CURRENT,
    CATEGORY_IMPLEMENTATION,
    CATEGORY_REGRESSION_GAP,
    CURRENCY_CURRENT_HEAD,
    OUTCOME_CLOSURE_PROPOSAL_NOT_ACCEPTED,
    AcceptedFindingProjection,
    AcceptedFindingRecord,
    DispositionOutcome,
    ProjectionTarget,
)
from auto_coder.adversarial_validator import AdversarialValidationResult, RequirementCoverageEntry, SpecificationGap
from auto_coder.canonical_pr_blocker_ledger import QualifiedRequirement
from auto_coder.effective_review_decision import EffectiveNextAction, derive_effective_review_decision

TARGET = ProjectionTarget(repository="owner/repo", pr_number=42, head_sha="head", base_sha="base", contract_identity="contract", policy_identity="policy")


def result(status: str = "PASS") -> AdversarialValidationResult:
    return AdversarialValidationResult(
        result=status,
        raw_response="model evidence",
        attempt_id="attempt-7",
        attempt_sequence=7,
        requirement_coverage=[RequirementCoverageEntry("REQ-001", "VERIFIED", "src/state.py:20")],
    )


def finding(identity: str, blocker: str, category: str) -> AcceptedFindingRecord:
    return AcceptedFindingRecord(
        source_identity=identity,
        finding_id=identity.split(":")[-1],
        canonical_blocker_id=blocker,
        category=category,
        accepted_state="OPEN",
        association=ASSOCIATED,
        target_binding=BINDING_CURRENT,
        evidence_currency=CURRENCY_CURRENT_HEAD,
        original_scope=f"correct {identity}",
        affected_boundary="src/state.py:20",
        evidence="src/state.py:20",
        qualified_requirements=(QualifiedRequirement(2402, "REQ-003"),),
    )


def projection(*records: AcceptedFindingRecord, complete: bool = True, revision: int = 9) -> AcceptedFindingProjection:
    return AcceptedFindingProjection(target=TARGET, records=records, complete=complete, source_revision=4, finding_set_revision=5, ledger_revision=revision)


def test_open_gap_overrides_pass_without_corrupting_runtime_coverage() -> None:
    raw = result()
    decision = derive_effective_review_decision(raw, projection(finding("round:gap", "blocker-gap", CATEGORY_REGRESSION_GAP)))

    assert decision.status == "NEEDS_TESTS"
    assert decision.next_action is EffectiveNextAction.FOCUSED_TEST_REPAIR
    assert decision.approval_eligible is False
    assert decision.blocker_ids == ("blocker-gap",)
    assert decision.corrections[0].qualified_requirements == ("#2402/REQ-003",)
    assert decision.requirement_coverage[0].status == "VERIFIED"
    assert decision.raw_result == "PASS"


def test_mixed_categories_choose_fix_and_retain_each_distinct_scope_once() -> None:
    implementation = finding("round:implementation", "blocker-implementation", CATEGORY_IMPLEMENTATION)
    gap = finding("round:gap", "blocker-gap", CATEGORY_REGRESSION_GAP)
    duplicate = replace(gap, current_observations=(DispositionOutcome("STILL_VALID_OBSERVED", gap.source_identity),))

    decision = derive_effective_review_decision(result("NEEDS_TESTS"), projection(implementation, gap, duplicate))

    assert decision.status == "NEEDS_FIX"
    assert decision.next_action is EffectiveNextAction.IMPLEMENTATION_REPAIR
    assert decision.blocker_ids == ("blocker-gap", "blocker-implementation")
    assert {item.category for item in decision.corrections} == {CATEGORY_IMPLEMENTATION, CATEGORY_REGRESSION_GAP}


def test_operational_failure_precedes_known_repairs() -> None:
    decision = derive_effective_review_decision(result("EXHAUSTED"), projection(finding("round:gap", "blocker-gap", CATEGORY_REGRESSION_GAP)))

    assert decision.status == "EXHAUSTED"
    assert decision.next_action is EffectiveNextAction.OPERATIONAL_WAIT
    assert decision.corrections == ()


def test_incomplete_or_historical_authority_requires_reconciliation() -> None:
    unavailable = derive_effective_review_decision(result(), projection(complete=False))
    historical = replace(finding("round:gap", "blocker-gap", CATEGORY_REGRESSION_GAP), evidence_currency="HISTORICAL")
    stale = derive_effective_review_decision(result(), projection(historical))

    assert (unavailable.status, unavailable.next_action) == ("BLOCKED", EffectiveNextAction.RECONCILIATION)
    assert (stale.status, stale.next_action) == ("BLOCKED", EffectiveNextAction.RECONCILIATION)


def test_exact_closure_proposal_waits_for_owner_then_accepted_closure_passes() -> None:
    open_record = replace(
        finding("round:gap", "blocker-gap", CATEGORY_REGRESSION_GAP),
        current_observations=(DispositionOutcome(OUTCOME_CLOSURE_PROPOSAL_NOT_ACCEPTED, "round:gap", "ADDRESSED", "independent exact evidence"),),
    )
    pending = derive_effective_review_decision(result(), projection(open_record))
    closed = derive_effective_review_decision(result(), projection(replace(open_record, accepted_state="FIXED"), revision=10))

    assert (pending.status, pending.next_action, pending.approval_eligible) == ("BLOCKED", EffectiveNextAction.CLOSURE_ACCEPTANCE, False)
    assert (closed.status, closed.next_action, closed.approval_eligible) == ("PASS", EffectiveNextAction.NONE, True)
    assert closed.binding.association_revision == 10


def test_same_head_decision_is_revision_bound_and_specification_gap_blocks() -> None:
    first = derive_effective_review_decision(result(), projection(revision=1))
    second = derive_effective_review_decision(result(), projection(finding("round:gap", "blocker-gap", CATEGORY_REGRESSION_GAP), revision=2))
    ambiguous = result()
    ambiguous.specification_gaps = [SpecificationGap(question="Which behavior is required?")]
    specification = derive_effective_review_decision(ambiguous, projection(revision=3))

    assert first.status == "PASS"
    assert second.status == "NEEDS_TESTS"
    assert first.binding.target.head_sha == second.binding.target.head_sha == "head"
    assert first.binding.association_revision != second.binding.association_revision
    assert (specification.status, specification.next_action) == ("BLOCKED", EffectiveNextAction.RECONCILIATION)
