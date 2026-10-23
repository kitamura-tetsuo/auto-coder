from dataclasses import replace

import pytest

from auto_coder.accepted_finding_bridge import (
    AMBIGUOUS,
    ASSOCIATED,
    BINDING_CURRENT,
    CATEGORY_IMPLEMENTATION,
    CATEGORY_REGRESSION_GAP,
    CURRENCY_CURRENT_HEAD,
    OUTCOME_CLOSURE_PROPOSAL_NOT_ACCEPTED,
    OUTCOME_UNAVAILABLE,
    AcceptedFindingProjection,
    AcceptedFindingRecord,
    DispositionOutcome,
    ProjectionTarget,
)
from auto_coder.adversarial_validator import AdversarialValidationFinding, AdversarialValidationResult, RequirementCoverageEntry, SpecificationGap
from auto_coder.canonical_pr_blocker_ledger import QualifiedRequirement
from auto_coder.effective_review_decision import EffectiveNextAction, derive_effective_review_decision
from auto_coder.reviewer_session_registry import TestOracleGap

TARGET = ProjectionTarget(repository="owner/repo", pr_number=42, head_sha="head", base_sha="base", contract_identity="contract", policy_identity="policy")


def result(status: str = "PASS") -> AdversarialValidationResult:
    return AdversarialValidationResult(
        result=status,
        raw_response="model evidence",
        attempt_id="attempt-7",
        attempt_sequence=7,
        requirement_coverage=[RequirementCoverageEntry("REQ-001", "VERIFIED", "src/state.py:20")],
    )


def ordinary_correction(status: str = "NEEDS_FIX") -> AdversarialValidationResult:
    raw = result(status)
    if status == "NEEDS_FIX":
        raw.requirement_coverage[0].status = "VIOLATED"
        raw.findings = [
            AdversarialValidationFinding(
                requirement_id="REQ-001",
                finding_identity="ordinary-join-binding",
                correction_identity="join-on-binding",
                counterexample="Rewritten JOIN ON retains tasks.id after tasks becomes tasks_2",
            )
        ]
    else:
        raw.test_oracle_gaps = [
            TestOracleGap(
                gap_id="TOG-join-on-binding",
                requirement_id="REQ-001",
                authoritative_boundary="SQL rewrite before destination persistence",
                invariant="JOIN ON preserves inner table binding",
                plausible_incorrect_implementation="Install the joined-result alias in the child scope",
                why_tests_still_pass="Existing JOIN fixture uses ON true",
                material_consequence="Destination SQL raises a missing FROM-clause error",
                focused_regression_scenario="Execute both original and rewritten bound JOIN ON expressions",
            )
        ]
    return raw


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
    assert decision.blocker_ids == ("blocker-gap",)
    assert decision.corrections[0].accepted_state == "OPEN"


@pytest.mark.parametrize(
    ("ordinary_status", "expected_action"),
    [
        ("NEEDS_FIX", EffectiveNextAction.IMPLEMENTATION_REPAIR),
        ("NEEDS_TESTS", EffectiveNextAction.FOCUSED_TEST_REPAIR),
    ],
)
def test_ordinary_repair_result_remains_nonapproving_without_accepted_findings(
    ordinary_status: str,
    expected_action: EffectiveNextAction,
) -> None:
    raw = ordinary_correction(ordinary_status)

    decision = derive_effective_review_decision(raw, projection())

    assert decision.status == ordinary_status
    assert decision.next_action is expected_action
    assert decision.approval_eligible is False
    assert decision.corrections == ()
    assert decision.ordinary_findings == tuple(raw.findings)
    assert decision.ordinary_gaps == tuple(raw.test_oracle_gaps)
    assert decision.raw_result == ordinary_status
    assert decision.requirement_coverage == tuple(raw.requirement_coverage)


def test_incomplete_or_historical_authority_requires_reconciliation() -> None:
    unavailable = derive_effective_review_decision(result(), projection(complete=False))
    historical = replace(finding("round:gap", "blocker-gap", CATEGORY_REGRESSION_GAP), evidence_currency="HISTORICAL")
    stale = derive_effective_review_decision(result(), projection(historical))

    assert (unavailable.status, unavailable.next_action) == ("BLOCKED", EffectiveNextAction.RECONCILIATION)
    assert (stale.status, stale.next_action) == ("BLOCKED", EffectiveNextAction.RECONCILIATION)


def test_demonstrated_ordinary_violation_dispatches_repair_without_approval() -> None:
    raw = result("NEEDS_FIX")
    raw.requirement_coverage.append(RequirementCoverageEntry("#5458/REQ-005", "VIOLATED", "Layout drops the read-only restriction"))
    raw.findings = [AdversarialValidationFinding(requirement_ids=["#5458/REQ-005"], counterexample="Nested read-only Grid commits width")]

    decision = derive_effective_review_decision(raw, projection())

    assert (decision.status, decision.next_action) == ("NEEDS_FIX", EffectiveNextAction.IMPLEMENTATION_REPAIR)
    assert decision.approval_eligible is False
    assert decision.requirement_coverage == tuple(raw.requirement_coverage)

    raw.result = "PASS"
    clearance = derive_effective_review_decision(raw, projection())
    assert (clearance.status, clearance.next_action) == ("BLOCKED", EffectiveNextAction.RECONCILIATION)
    assert clearance.approval_eligible is False


@pytest.mark.parametrize("status", ["PASS", "NEEDS_TESTS", "NEEDS_FIX"])
@pytest.mark.parametrize("coverage_status", ["VIOLATED", "UNVERIFIED"])
def test_unsubstantiated_or_unverified_coverage_never_dispatches_repair(status: str, coverage_status: str) -> None:
    raw = result(status)
    raw.requirement_coverage.append(RequirementCoverageEntry("REQ-002", coverage_status, "Unsettled evidence"))
    raw.findings = [AdversarialValidationFinding(requirement_id="REQ-001", counterexample="Different requirement")]

    decision = derive_effective_review_decision(raw, projection())

    assert (decision.status, decision.next_action) == ("BLOCKED", EffectiveNextAction.RECONCILIATION)
    assert decision.approval_eligible is False


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


@pytest.mark.parametrize(
    ("category", "status", "action"),
    [(CATEGORY_IMPLEMENTATION, "NEEDS_FIX", EffectiveNextAction.IMPLEMENTATION_REPAIR), (CATEGORY_REGRESSION_GAP, "NEEDS_TESTS", EffectiveNextAction.FOCUSED_TEST_REPAIR)],
)
def test_current_repair_precedes_pending_closure_without_repairing_the_closed_counterexample(category: str, status: str, action: EffectiveNextAction) -> None:
    closure = replace(
        finding("round:gap", "blocker-gap", CATEGORY_REGRESSION_GAP),
        current_observations=(DispositionOutcome(OUTCOME_CLOSURE_PROPOSAL_NOT_ACCEPTED, "round:gap", "ADDRESSED", "exact evidence"),),
    )
    implementation = finding("round:implementation", "blocker-implementation", category)

    decision = derive_effective_review_decision(result(), projection(closure, implementation))

    assert (decision.status, decision.next_action, decision.approval_eligible) == (status, action, False)
    assert decision.blocker_ids == ("blocker-implementation",)
    assert tuple(item.source_identity for item in decision.pending_closures) == ("round:gap",)


def test_new_ordinary_violation_dispatches_while_all_accepted_findings_await_closure() -> None:
    closure = replace(
        finding("round:implementation", "blocker-implementation", CATEGORY_IMPLEMENTATION),
        current_observations=(DispositionOutcome(OUTCOME_CLOSURE_PROPOSAL_NOT_ACCEPTED, "round:implementation", "ADDRESSED", "exact evidence"),),
    )
    raw = result("NEEDS_FIX")
    raw.requirement_coverage.append(RequirementCoverageEntry("#2402/REQ-004", "VIOLATED", "Grid mutation discards the dependency snapshot"))
    raw.findings = [AdversarialValidationFinding(requirement_id="#2402/REQ-004", counterexample="A concurrent Table schema change permits a stale Grid query")]

    decision = derive_effective_review_decision(raw, projection(closure))

    assert (decision.status, decision.next_action, decision.approval_eligible) == ("NEEDS_FIX", EffectiveNextAction.IMPLEMENTATION_REPAIR, False)
    assert decision.corrections == ()
    assert tuple(item.source_identity for item in decision.pending_closures) == (closure.source_identity,)
    assert decision.requirement_coverage == tuple(raw.requirement_coverage)

    raw.requirement_coverage[-1].status = "UNVERIFIED"
    blocked = derive_effective_review_decision(raw, projection(closure))
    assert (blocked.status, blocked.next_action, blocked.approval_eligible) == ("BLOCKED", EffectiveNextAction.RECONCILIATION, False)


def test_stale_accepted_closure_requires_reconciliation() -> None:
    closed = replace(
        finding("round:gap", "blocker-gap", CATEGORY_REGRESSION_GAP),
        accepted_state="FIXED",
        target_binding="RECONCILIATION_REQUIRED",
    )

    decision = derive_effective_review_decision(result(), projection(closed))

    assert (decision.status, decision.next_action) == ("BLOCKED", EffectiveNextAction.RECONCILIATION)
    assert decision.corrections == ()
    assert decision.pending_reconciliation[0].accepted_state == "FIXED"


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
    assert first.binding.reopen_epoch == 0
    assert (specification.status, specification.next_action) == ("BLOCKED", EffectiveNextAction.RECONCILIATION)


@pytest.mark.parametrize(
    ("status", "action"),
    [("NEEDS_FIX", EffectiveNextAction.IMPLEMENTATION_REPAIR), ("NEEDS_TESTS", EffectiveNextAction.FOCUSED_TEST_REPAIR)],
)
def test_current_ordinary_correction_dispatches_while_historical_open_source_awaits_adjudication(status: str, action: EffectiveNextAction) -> None:
    historical = replace(finding("round:historical", "blocker-historical", CATEGORY_IMPLEMENTATION), evidence_currency="HISTORICAL")
    raw = ordinary_correction(status)

    decision = derive_effective_review_decision(raw, projection(historical))

    assert (decision.status, decision.next_action, decision.approval_eligible) == (status, action, False)
    assert decision.corrections == ()
    assert decision.blocker_ids == ()
    assert decision.pending_closures == ()
    assert tuple(item.source_identity for item in decision.pending_reconciliation) == (historical.source_identity,)
    assert decision.pending_reconciliation[0].accepted_state == "OPEN"
    assert decision.ordinary_findings == tuple(raw.findings)
    assert decision.ordinary_gaps == tuple(raw.test_oracle_gaps)


@pytest.mark.parametrize("category", [CATEGORY_IMPLEMENTATION, CATEGORY_REGRESSION_GAP])
def test_current_accepted_correction_is_independent_of_historical_adjudication(category: str) -> None:
    historical = replace(finding("round:historical", "blocker-historical", CATEGORY_IMPLEMENTATION), evidence_currency="HISTORICAL")
    current = finding("round:current", "blocker-current", category)
    raw = result()
    raw.requirement_coverage[0].status = "UNVERIFIED"

    decision = derive_effective_review_decision(raw, projection(historical, current))

    assert decision.status == ("NEEDS_FIX" if category == CATEGORY_IMPLEMENTATION else "NEEDS_TESTS")
    assert decision.blocker_ids == ("blocker-current",)
    assert tuple(item.source_identity for item in decision.pending_reconciliation) == (historical.source_identity,)
    assert not decision.approval_eligible


def test_complete_closure_proposal_and_historical_adjudication_do_not_enter_new_repair() -> None:
    closure = replace(
        finding("round:fixed", "blocker-fixed", CATEGORY_IMPLEMENTATION),
        evidence_currency="HISTORICAL",
        current_observations=(DispositionOutcome(OUTCOME_CLOSURE_PROPOSAL_NOT_ACCEPTED, "round:fixed", "ADDRESSED", "exact current-head FIXED proposal"),),
    )
    historical = replace(finding("round:historical", "blocker-historical", CATEGORY_IMPLEMENTATION), evidence_currency="HISTORICAL")
    raw = ordinary_correction()

    decision = derive_effective_review_decision(raw, projection(closure, historical))

    assert (decision.status, decision.next_action, decision.approval_eligible) == ("NEEDS_FIX", EffectiveNextAction.IMPLEMENTATION_REPAIR, False)
    assert decision.corrections == ()
    assert decision.ordinary_findings == tuple(raw.findings)
    assert tuple(item.source_identity for item in decision.pending_closures) == (closure.source_identity,)
    assert tuple(item.source_identity for item in decision.pending_reconciliation) == (historical.source_identity,)


@pytest.mark.parametrize("problem", ["incomplete", "stale", "ambiguous", "unavailable", "missing_owner", "unknown_state", "unknown_category"])
def test_independent_ordinary_correction_does_not_bypass_invalid_accepted_authority(problem: str) -> None:
    historical = replace(finding("round:historical", "blocker-historical", CATEGORY_IMPLEMENTATION), evidence_currency="HISTORICAL")
    if problem == "stale":
        historical = replace(historical, target_binding="RECONCILIATION_REQUIRED")
    elif problem == "ambiguous":
        historical = replace(historical, association=AMBIGUOUS)
    elif problem == "unavailable":
        historical = replace(historical, current_observations=(DispositionOutcome(OUTCOME_UNAVAILABLE, historical.source_identity),))
    elif problem == "missing_owner":
        historical = replace(historical, canonical_blocker_id="")
    elif problem == "unknown_state":
        historical = replace(historical, accepted_state="UNRECOGNIZED")
    elif problem == "unknown_category":
        historical = replace(historical, category="UNSUPPORTED")

    decision = derive_effective_review_decision(ordinary_correction(), projection(historical, complete=problem != "incomplete"))

    assert (decision.status, decision.next_action, decision.approval_eligible) == ("BLOCKED", EffectiveNextAction.RECONCILIATION, False)
    assert decision.ordinary_findings == ()
    assert decision.ordinary_gaps == ()


@pytest.mark.parametrize("status", ["NEEDS_FIX", "NEEDS_TESTS", "UNSUPPORTED"])
def test_status_string_without_current_corrective_evidence_does_not_authorize_repair(status: str) -> None:
    decision = derive_effective_review_decision(result(status), projection())

    assert (decision.status, decision.next_action, decision.approval_eligible) == ("BLOCKED", EffectiveNextAction.RECONCILIATION, False)
    assert decision.ordinary_findings == ()
    assert decision.ordinary_gaps == ()


def test_unsupported_verdict_cannot_borrow_current_accepted_repair_authority() -> None:
    current = finding("round:current", "blocker-current", CATEGORY_IMPLEMENTATION)

    decision = derive_effective_review_decision(result("UNSUPPORTED"), projection(current))

    assert (decision.status, decision.next_action, decision.approval_eligible) == ("BLOCKED", EffectiveNextAction.RECONCILIATION, False)
    assert decision.ordinary_findings == ()


def test_invalid_canonical_reference_stays_nonactionable_with_current_accepted_findings() -> None:
    current = finding("round:current", "blocker-current", CATEGORY_IMPLEMENTATION)
    raw = ordinary_correction()
    raw.result = "BLOCKED"
    raw.diagnostic_category = "correction_reference_invalid"

    decision = derive_effective_review_decision(raw, projection(current))

    assert (decision.status, decision.next_action, decision.approval_eligible) == ("BLOCKED", EffectiveNextAction.OPERATIONAL_WAIT, False)
    assert decision.ordinary_findings == ()
    assert decision.ordinary_gaps == ()


@pytest.mark.parametrize("problem", ["missing_counterexample", "unverified_finding", "verified_coverage", "unmatched_coverage", "specification_gap", "missing_gap_scope"])
def test_historical_pending_work_does_not_relax_ordinary_evidence_requirements(problem: str) -> None:
    historical = replace(finding("round:historical", "blocker-historical", CATEGORY_IMPLEMENTATION), evidence_currency="HISTORICAL")
    raw = ordinary_correction("NEEDS_TESTS" if problem == "missing_gap_scope" else "NEEDS_FIX")
    if problem == "missing_counterexample":
        raw.findings[0].counterexample = ""
    elif problem == "unverified_finding":
        raw.findings[0].evidence_classification = "UNVERIFIED"
    elif problem == "verified_coverage":
        raw.requirement_coverage[0].status = "VERIFIED"
    elif problem == "unmatched_coverage":
        raw.findings[0].requirement_id = "REQ-999"
    elif problem == "specification_gap":
        raw.specification_gaps = [SpecificationGap(question="Which behavior is required?")]
    else:
        raw.test_oracle_gaps[0].authoritative_boundary = ""

    decision = derive_effective_review_decision(raw, projection(historical))

    assert (decision.status, decision.next_action, decision.approval_eligible) == ("BLOCKED", EffectiveNextAction.RECONCILIATION, False)
    assert decision.ordinary_findings == ()
    assert decision.ordinary_gaps == ()
    assert tuple(item.source_identity for item in decision.pending_reconciliation) == (historical.source_identity,)


@pytest.mark.parametrize(("identity_field", "prefix"), [("finding_identity", ""), ("correction_identity", ""), ("correction_identity", "canonical:")])
def test_historical_accepted_identity_cannot_become_new_repair_through_raw_restatement(identity_field: str, prefix: str) -> None:
    historical = replace(finding("round:historical", "blocker-historical", CATEGORY_IMPLEMENTATION), evidence_currency="HISTORICAL")
    raw = ordinary_correction()
    setattr(raw.findings[0], identity_field, historical.source_identity if identity_field == "finding_identity" else f"{prefix}{historical.canonical_blocker_id}")

    decision = derive_effective_review_decision(raw, projection(historical))

    assert (decision.status, decision.next_action) == ("BLOCKED", EffectiveNextAction.RECONCILIATION)
    assert decision.corrections == ()
    assert decision.ordinary_findings == ()
    assert tuple(item.source_identity for item in decision.pending_reconciliation) == (historical.source_identity,)


def test_gap_restatement_cannot_convert_historical_implementation_to_test_repair() -> None:
    historical = replace(finding("round:historical", "blocker-historical", CATEGORY_IMPLEMENTATION), evidence_currency="HISTORICAL")
    raw = ordinary_correction("NEEDS_TESTS")
    raw.test_oracle_gaps[0].correction_identity = f"canonical:{historical.canonical_blocker_id}"

    decision = derive_effective_review_decision(raw, projection(historical))

    assert (decision.status, decision.next_action) == ("BLOCKED", EffectiveNextAction.RECONCILIATION)
    assert decision.corrections == ()
    assert decision.ordinary_gaps == ()
    assert decision.pending_reconciliation[0].category == CATEGORY_IMPLEMENTATION


def test_independent_implementation_violation_takes_priority_over_accepted_test_correction() -> None:
    accepted_gap = finding("round:gap", "blocker-gap", CATEGORY_REGRESSION_GAP)
    raw = ordinary_correction()

    decision = derive_effective_review_decision(raw, projection(accepted_gap))

    assert (decision.status, decision.next_action) == ("NEEDS_FIX", EffectiveNextAction.IMPLEMENTATION_REPAIR)
    assert decision.blocker_ids == ("blocker-gap",)
    assert decision.ordinary_findings == tuple(raw.findings)
    assert not decision.approval_eligible
