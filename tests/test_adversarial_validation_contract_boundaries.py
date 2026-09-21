"""Tests for PR validation contract boundaries (Issue #2136).

Verifies strict separation of demonstrated implementation violations,
material test-oracle gaps, and specification defects according to REQ-001
through REQ-010 and Acceptance Scenarios AS-001 through AS-005.
"""

import json
from unittest.mock import MagicMock

import pytest

from auto_coder.adversarial_validator import (
    AdversarialValidationContext,
    AdversarialValidationFinding,
    AdversarialValidationResult,
    IssueRequirement,
    RequirementCoverageEntry,
    ReviewThreadDisposition,
    SpecificationGap,
    _addressed_test_oracle_gap_evidence,
    _apply_coverage_and_verdict_precedence,
    _reconcile_test_oracle_gap_lifecycle,
    _stable_test_oracle_gap_id,
    assemble_adversarial_repair_prompt,
    format_adversarial_finding_comment,
    format_test_oracle_gap_comment,
    is_valid_test_oracle_resolution_evidence,
    parse_adversarial_validation_response,
)
from auto_coder.pr_repair import ExistingPrRepairTarget
from auto_coder.requirement_contract import is_explicit_test_deliverable
from auto_coder.reviewer_session_registry import ReviewerSession, TestOracleGap
from auto_coder.util.gh_cache import ReviewThread


def _make_finding(
    requirement_id: str = "REQ-001",
    violated_requirement: str = "Requirement text",
    actual_behavior: str = "Actual defect",
    counterexample: str = "Counterexample",
    anchor_path: str = "src/example.py",
    test_gap: str = "",
    finding_identity: str = "finding-1",
    correction_identity: str = "fix-1",
    required_behavior: str = "Required behavior",
    reachability: str = "production_boundary",
    requirement_ids: list[str] | None = None,
) -> AdversarialValidationFinding:
    return AdversarialValidationFinding(
        requirement_id=requirement_id,
        requirement_ids=requirement_ids or [requirement_id],
        finding_identity=finding_identity,
        correction_identity=correction_identity,
        violated_requirement=violated_requirement,
        reachability=reachability,
        required_behavior=required_behavior,
        actual_behavior=actual_behavior,
        evidence="Evidence showing failure",
        counterexample=counterexample,
        test_gap=test_gap,
        suggested_regression_scenario="Add regression test",
        anchor_path=anchor_path,
        anchor_line=10,
        anchor_side="RIGHT",
    )


def _make_gap(
    requirement_id: str = "REQ-001",
    boundary: str = "example_boundary",
    invariant: str = "Runtime invariant holds",
    anchor_path: str = "src/example.py",
    status: str = "OPEN",
    resolution_evidence: str = "",
) -> TestOracleGap:
    return TestOracleGap(
        gap_id=_stable_test_oracle_gap_id(requirement_id, boundary, invariant),
        requirement_id=requirement_id,
        authoritative_boundary=boundary,
        invariant=invariant,
        plausible_incorrect_implementation="Omit guard check",
        why_tests_still_pass="Tests pass without asserting invariant",
        material_consequence="Invalid state is possible",
        focused_regression_scenario="Call boundary and assert invariant",
        anchor_path=anchor_path,
        anchor_line=10,
        anchor_side="RIGHT",
        status=status,
        resolution_evidence=resolution_evidence,
    )


def test_as_001_documentation_defect_independence() -> None:
    """AS-001: Documentation defect is an implementation finding under doc requirement;

    runtime rendering behavior lacking tests is a TOG under rendering requirement.
    Updating documentation resolves doc finding while leaving TOG open.
    """
    req_rendering = IssueRequirement("REQ-001", "Render truthful snapshots and suppress change-provenance emission when complete.")
    req_docs = IssueRequirement("REQ-002", "Document accurate behavior and coverage under docs/client-features/.")
    context = AdversarialValidationContext(
        all_changed_files=["src/snapshot.py", "docs/client-features/snapshot.md"],
        issue_requirements=[req_rendering, req_docs],
    )

    # Reviewer output:
    # 1. Implementation finding for documentation defect (false claim under REQ-002)
    finding_doc = _make_finding(
        requirement_id="REQ-002",
        violated_requirement="Document accurate behavior and coverage",
        actual_behavior="Documentation falsely claims suppression coverage was added",
        counterexample="docs/client-features/snapshot.md states suppression tests exist, but no tests were added",
        anchor_path="docs/client-features/snapshot.md",
        finding_identity="doc-false-claim",
        correction_identity="correct-doc",
    )
    # 2. Test-oracle gap for runtime snapshot rendering under REQ-001
    gap_rendering = _make_gap(
        requirement_id="REQ-001",
        boundary="SnapshotRenderer.render",
        invariant="Suppressed change-provenance emission is omitted from log output",
        anchor_path="src/snapshot.py",
    )
    # 3. Supposed duplicate finding for REQ-001 runtime behavior solely due to missing tests
    finding_rendering_missing_tests = _make_finding(
        requirement_id="REQ-001",
        violated_requirement="Render truthful snapshots",
        actual_behavior="No dedicated regression test covers suppression of change-provenance emission",
        counterexample="Untested suppression logic",
        test_gap="Existing tests do not assert suppression",
        anchor_path="src/snapshot.py",
        finding_identity="missing-test-rendering",
        correction_identity="add-test-rendering",
    )

    result = AdversarialValidationResult(
        result="NEEDS_FIX",
        summary="Validation found defects",
        findings=[finding_doc, finding_rendering_missing_tests],
        test_oracle_gaps=[gap_rendering],
        requirement_coverage=[
            RequirementCoverageEntry("REQ-001", "VIOLATED", "Missing tests"),
            RequirementCoverageEntry("REQ-002", "VIOLATED", "Doc false claim"),
        ],
    )

    normalized = _apply_coverage_and_verdict_precedence(result, context)

    # 1. Implementation finding for documentation defect under REQ-002
    assert len(normalized.findings) == 1
    assert normalized.findings[0].requirement_id == "REQ-002"
    assert normalized.findings[0].finding_identity == "doc-false-claim"

    # 2. Test-oracle gap for runtime snapshot rendering under REQ-001
    assert len(normalized.open_test_oracle_gaps) == 1
    assert normalized.open_test_oracle_gaps[0].requirement_id == "REQ-001"

    # 3. No implementation finding for runtime rendering behavior solely due to missing tests
    assert not any(f.requirement_id == "REQ-001" for f in normalized.findings)

    # 4. When documentation is updated to truthfully describe the omission,
    # the documentation finding closes while the runtime TOG remains open!
    result_after_doc_fix = AdversarialValidationResult(
        result="NEEDS_TESTS",
        summary="Documentation corrected; runtime test oracle still required",
        findings=[],  # doc finding closed
        test_oracle_gaps=[gap_rendering],  # TOG still open
        requirement_coverage=[
            RequirementCoverageEntry("REQ-001", "VERIFIED", "Production code correct, test gap tracked"),
            RequirementCoverageEntry("REQ-002", "VERIFIED", "Documentation accurate"),
        ],
    )
    normalized_after_fix = _apply_coverage_and_verdict_precedence(result_after_doc_fix, context)
    assert len(normalized_after_fix.findings) == 0
    assert len(normalized_after_fix.open_test_oracle_gaps) == 1
    assert normalized_after_fix.result == "NEEDS_TESTS"
    assert normalized_after_fix.needs_tests is True
    assert normalized_after_fix.needs_fix is False


def test_as_002_explicit_test_deliverable_vs_runtime_requirement() -> None:
    """AS-002: Explicit test deliverable requirement (REQ-001) absence is an implementation

    deliverable finding without duplicate TOG. Runtime requirement (REQ-002) missing
    tests is a TOG.
    """
    req_deliverable = IssueRequirement("REQ-001", "Include a negative-control regression test verifying invalid anchors are rejected.")
    req_runtime = IssueRequirement("REQ-002", "Validate review anchors and reject invalid review anchors.")
    context = AdversarialValidationContext(
        all_changed_files=["src/validator.py"],
        issue_requirements=[req_deliverable, req_runtime],
    )

    # Both a finding and a TOG for REQ-001 (explicit test deliverable)
    finding_deliverable = _make_finding(
        requirement_id="REQ-001",
        violated_requirement="Include a negative-control regression test",
        actual_behavior="Missing negative-control regression test deliverable",
        counterexample="No negative-control test exists",
        anchor_path="src/validator.py",
        finding_identity="missing-negative-control",
        correction_identity="add-negative-control",
    )
    gap_deliverable_duplicate = _make_gap(
        requirement_id="REQ-001",
        boundary="validate_anchor",
        invariant="Invalid anchors are rejected with negative control",
        anchor_path="src/validator.py",
    )

    # TOG for REQ-002 (untested runtime validation behavior)
    gap_runtime = _make_gap(
        requirement_id="REQ-002",
        boundary="validate_anchor",
        invariant="Rejected anchors do not throw unhandled exception",
        anchor_path="src/validator.py",
    )

    result = AdversarialValidationResult(
        result="NEEDS_FIX",
        summary="Validation reported missing tests",
        findings=[finding_deliverable],
        test_oracle_gaps=[gap_deliverable_duplicate, gap_runtime],
        requirement_coverage=[
            RequirementCoverageEntry("REQ-001", "VIOLATED", "Missing deliverable"),
            RequirementCoverageEntry("REQ-002", "VERIFIED", "Validation implemented"),
        ],
    )

    normalized = _apply_coverage_and_verdict_precedence(result, context)

    # REQ-001 is reported as an implementation deliverable finding
    assert any(f.requirement_id == "REQ-001" for f in normalized.findings)
    # Duplicate TOG for REQ-001 is deduplicated
    assert not any(gap.requirement_id == "REQ-001" for gap in normalized.test_oracle_gaps)
    # REQ-002 runtime behavior lacking test remains independently reported as TOG
    assert any(gap.requirement_id == "REQ-002" for gap in normalized.test_oracle_gaps)


def test_as_003_equivalent_test_technique_acceptance_and_rejection() -> None:
    """AS-003: Integration test using different valid technique asserting invariant

    across boundary is accepted. Tests that only assert source text or use empty
    inputs without exercising production path are rejected.
    """
    valid_integration_evidence = "Added integration test in tests/test_grid.py that invokes " "GridMutation.apply_candidate with invalid candidate and verifies " "rejection, unchanged state, and unchanged revision across the boundary."
    assert is_valid_test_oracle_resolution_evidence(valid_integration_evidence) is True

    source_text_evidence = "Added test checking that 'guard = True' appears in src/grid.py source text via file inspection."
    assert is_valid_test_oracle_resolution_evidence(source_text_evidence) is False

    empty_input_evidence = "Test invokes helper with empty input without exercising production boundary."
    assert is_valid_test_oracle_resolution_evidence(empty_input_evidence) is False

    # Test in _addressed_test_oracle_gap_evidence
    gap = _make_gap(requirement_id="REQ-001", boundary="GridMutation.apply_candidate", invariant="Reject invalid candidate")
    claimed_thread = MagicMock()
    claimed_thread.thread_id = "thread-1"
    claimed_thread.original_finding = f"Gap identity: `{gap.gap_id}`\n`REQ-001`: Runtime validation"

    # Case A: Valid evidence resolves gap
    result_valid = AdversarialValidationResult(
        result="PASS",
        test_oracle_gaps=[gap],
        thread_dispositions=[
            ReviewThreadDisposition(
                thread_id="thread-1",
                status="ADDRESSED",
                rationale="Added integration test",
                evidence=valid_integration_evidence,
            )
        ],
    )
    evidence_map = _addressed_test_oracle_gap_evidence(result_valid, [claimed_thread], [gap])
    assert gap.gap_id in evidence_map

    # Case B: Superficial source-text evidence is rejected (gap stays unaddressed)
    result_source_text = AdversarialValidationResult(
        result="PASS",
        test_oracle_gaps=[gap],
        thread_dispositions=[
            ReviewThreadDisposition(
                thread_id="thread-1",
                status="ADDRESSED",
                rationale="Added source text check",
                evidence=source_text_evidence,
            )
        ],
    )
    evidence_map_rejected = _addressed_test_oracle_gap_evidence(result_source_text, [claimed_thread], [gap])
    assert gap.gap_id not in evidence_map_rejected


def test_as_004_retired_configuration_artifact_converted_to_specification_gap() -> None:
    """AS-004: Objection flagging deletion of docs/client-features.yaml is reclassified

    as structured SpecificationGap rather than implementation finding. Remaining
    requirements are evaluated independently; auto-merge is disabled.
    """
    req_feature = IssueRequirement("REQ-001", "Migrate client feature docs to standalone fragments.")
    context = AdversarialValidationContext(
        all_changed_files=["docs/client-features/feature-a.md"],
        issue_requirements=[req_feature],
    )

    # Reviewer output flags deletion of docs/client-features.yaml
    finding_yaml = _make_finding(
        requirement_id="REQ-001",
        violated_requirement="Migrate client feature docs to standalone fragments",
        actual_behavior="docs/client-features.yaml was deleted",
        counterexample="docs/client-features.yaml missing from repo",
        anchor_path="docs/client-features.yaml",
        finding_identity="deleted-client-features-yaml",
        correction_identity="restore-client-features-yaml",
    )

    result = AdversarialValidationResult(
        result="NEEDS_FIX",
        summary="Reviewer flagged deleted docs/client-features.yaml",
        findings=[finding_yaml],
        requirement_coverage=[
            RequirementCoverageEntry("REQ-001", "VIOLATED", "Deleted docs/client-features.yaml"),
        ],
    )

    normalized = _apply_coverage_and_verdict_precedence(result, context)

    # Reclassified into structured SpecificationGap
    assert len(normalized.findings) == 0
    assert len(normalized.specification_gaps) == 1
    gap = normalized.specification_gaps[0]
    assert "docs/client-features.yaml" in gap.question
    assert "standalone documentation fragments" in gap.question
    assert "docs/client-features.yaml" in gap.affected_scope

    # Remaining requirements evaluated independently: verdict is PASS, but allows_auto_merge is False
    assert normalized.result == "PASS"
    assert normalized.is_pass is True
    assert normalized.allows_auto_merge is False


def test_as_005_response_normalization_and_repair_prompt_assembly() -> None:
    """AS-005: Response normalization:

    1. Misquoted requirement text replaced with authoritative manifest text.
    2. Sibling-issue finding isolated without discarding valid finding.
    3. Duplicate finding for runtime test removed in favor of TOG.
    4. Objective-only demand converted to SpecificationGap.
    5. Repair prompt contains only surviving valid finding and TOG.
    """
    req_valid = IssueRequirement("REQ-001", "Server mutation paths reject invalid candidates independently of the browser.")
    req_runtime = IssueRequirement("REQ-002", "Persist state changes transactionally.")
    context = AdversarialValidationContext(
        all_changed_files=["src/server.py"],
        issue_requirements=[req_valid, req_runtime],
    )

    # 1. Finding citing valid requirement ID with misquoted text
    finding_valid = _make_finding(
        requirement_id="REQ-001",
        violated_requirement="Paraphrased/misquoted text: Server rejects candidates",
        actual_behavior="Server mutation does not reject candidate without client header",
        counterexample="Server accepted invalid candidate when called directly",
        anchor_path="src/server.py",
        finding_identity="unauthorized-candidate",
        correction_identity="fix-server-guard",
    )

    # 2. Finding citing sibling-issue requirement ID not in target Issue manifest
    finding_sibling = _make_finding(
        requirement_id="SIBLING-001",
        violated_requirement="Parent or sibling obligation: Emit real-time telemetry",
        actual_behavior="Missing telemetry event",
        counterexample="Telemetry not sent",
        anchor_path="src/server.py",
        finding_identity="missing-telemetry",
        correction_identity="add-telemetry",
    )

    # 3. Both a finding and TOG for same missing test on runtime requirement REQ-002
    finding_duplicate_test = _make_finding(
        requirement_id="REQ-002",
        violated_requirement="Persist state changes transactionally",
        actual_behavior="No dedicated regression test covers transactional rollback on error",
        counterexample="Untested transactional rollback",
        test_gap="No test asserts rollback",
        anchor_path="src/server.py",
        finding_identity="missing-rollback-test",
        correction_identity="add-rollback-test",
    )
    gap_runtime = _make_gap(
        requirement_id="REQ-002",
        boundary="TransactionManager.commit",
        invariant="State changes roll back on error",
        anchor_path="src/server.py",
    )

    # 4. Finding demanding outcome mentioned only in Objective
    finding_objective_only = _make_finding(
        requirement_id="OBJECTIVE",
        violated_requirement="Objective-only goal: Maximize system performance by 50%",
        actual_behavior="System performance was not benchmarked or improved by 50%",
        counterexample="Performance not measured",
        anchor_path="src/server.py",
        finding_identity="objective-perf-goal",
        correction_identity="optimize-performance",
        required_behavior="50% performance improvement mentioned only in the objective",
    )

    result = AdversarialValidationResult(
        result="NEEDS_FIX",
        summary="Reviewer reported multiple items",
        findings=[finding_valid, finding_sibling, finding_duplicate_test, finding_objective_only],
        test_oracle_gaps=[gap_runtime],
        requirement_coverage=[
            RequirementCoverageEntry("REQ-001", "VIOLATED", "Server guard broken"),
            RequirementCoverageEntry("REQ-002", "VIOLATED", "Untested rollback"),
        ],
    )

    normalized = _apply_coverage_and_verdict_precedence(result, context)

    # 1. Valid finding's requirement text replaced with authoritative text from manifest
    assert len(normalized.findings) == 1
    surviving = normalized.findings[0]
    assert surviving.requirement_id == "REQ-001"
    assert surviving.requirement_text == req_valid.text

    # 2. Sibling finding isolated; unrelated valid finding survived
    assert not any(f.requirement_id == "SIBLING-001" for f in normalized.findings)
    assert normalized.result == "NEEDS_FIX"

    # 3. Duplicate finding for runtime test removed in favor of TOG
    assert not any(f.requirement_id == "REQ-002" for f in normalized.findings)
    assert any(g.requirement_id == "REQ-002" for g in normalized.open_test_oracle_gaps)

    # 4. Objective-only demand converted to SpecificationGap
    assert len(normalized.specification_gaps) == 1
    assert "Objective or Acceptance Scenario" in normalized.specification_gaps[0].why_existing_issue_is_insufficient

    # 5. Repair prompt assembled from normalized result contains only surviving valid finding and TOG
    repair_target = ExistingPrRepairTarget(
        repo_name="owner/repo",
        pr_number=10,
        head_branch="feature",
        base_branch="main",
        head_sha="head123",
    )
    prompt = assemble_adversarial_repair_prompt(normalized, repair_target, "owner/repo", 10, "head123")

    # Contains surviving valid finding
    assert "Server mutation paths reject invalid candidates" in prompt
    assert "Server accepted invalid candidate when called directly" in prompt
    # Contains test-oracle gap
    assert "State changes roll back on error" in prompt
    assert "TransactionManager.commit" in prompt
    # Does NOT contain sibling demand, objective demand, or instruction to restore retired files
    assert "SIBLING-001" not in prompt
    assert "missing-telemetry" not in prompt
    assert "50% performance improvement" not in prompt
    assert "docs/client-features.yaml" not in prompt


def test_is_explicit_test_deliverable_classification() -> None:
    """Verify is_explicit_test_deliverable distinguishes test deliverables from

    runtime behavior and documentation coverage descriptions.
    """
    # Explicit test deliverables
    assert is_explicit_test_deliverable("Include a negative-control regression test verifying invalid anchors are rejected.") is True
    assert is_explicit_test_deliverable("Add a regression test deliverable for the cache eviction path.") is True
    assert is_explicit_test_deliverable("Provide an integration test suite for cross-component calls.") is True
    assert is_explicit_test_deliverable("Add deterministic production-boundary regressions for routing.") is True
    assert is_explicit_test_deliverable("The change must include automated tests that exercise reload.") is True

    # Runtime behavior without explicit test deliverable
    assert is_explicit_test_deliverable("Validate review anchors and reject invalid review anchors.") is False
    assert is_explicit_test_deliverable("Filter comments by author and sort oldest first.") is False
    assert is_explicit_test_deliverable("Persist state changes transactionally.") is False

    # Documentation updates describing coverage
    assert is_explicit_test_deliverable("Document accurate behavior and coverage in docs/client-features/.") is False
    assert is_explicit_test_deliverable("Update docs/client-features/test.md to describe test coverage.") is False
    assert is_explicit_test_deliverable("Do not add unrelated regression tests.") is False


def test_gap_only_explicit_deliverable_is_promoted_with_identity() -> None:
    """A demonstrated gap-only response becomes one deliverable finding."""
    requirement = IssueRequirement("REQ-011", "Add deterministic production-boundary regressions for persisted routing.")
    context = AdversarialValidationContext(all_changed_files=["tests/test_routing.py"], issue_requirements=[requirement])
    gap = _make_gap(
        requirement_id="REQ-011",
        boundary="Router.reload",
        invariant="Production routing after reload has no committed regression test",
        anchor_path="tests/test_routing.py",
    )
    result = AdversarialValidationResult(
        result="NEEDS_TESTS",
        summary="Missing coverage",
        test_oracle_gaps=[gap],
        requirement_coverage=[RequirementCoverageEntry("REQ-011", "VERIFIED", "Runtime behavior appears correct")],
    )

    normalized = _apply_coverage_and_verdict_precedence(result, context)

    assert normalized.result == "NEEDS_FIX"
    assert normalized.test_oracle_gaps == []
    assert len(normalized.findings) == 1
    assert normalized.findings[0].finding_identity == gap.gap_id
    assert normalized.findings[0].correction_identity == gap.gap_id
    assert normalized.findings[0].requirement_text == requirement.text
    assert normalized.requirement_coverage[0].status == "VIOLATED"


def test_distinct_explicit_deliverables_sharing_requirement_survive() -> None:
    """Requirement-ID overlap does not erase a separate missing boundary test."""
    requirement = IssueRequirement("REQ-011", "Add production-boundary regressions for boundary A and boundary B.")
    context = AdversarialValidationContext(all_changed_files=["tests/test_boundaries.py"], issue_requirements=[requirement])
    finding = _make_finding(
        requirement_id="REQ-011",
        actual_behavior="Boundary A regression is missing",
        required_behavior="Add the boundary A regression",
        counterexample="No test invokes boundary A",
        anchor_path="tests/test_boundaries.py",
        finding_identity="missing-a",
        correction_identity="add-a",
    )
    gap_b = _make_gap(
        requirement_id="REQ-011",
        boundary="BoundaryB.reload",
        invariant="Boundary B regression is missing",
        anchor_path="tests/test_boundaries.py",
    )
    result = AdversarialValidationResult(
        result="NEEDS_FIX",
        findings=[finding],
        test_oracle_gaps=[gap_b],
        requirement_coverage=[RequirementCoverageEntry("REQ-011", "VIOLATED", "Both regressions absent")],
    )

    normalized = _apply_coverage_and_verdict_precedence(result, context)

    assert {item.correction_identity for item in normalized.findings} == {"add-a", gap_b.gap_id}
    assert normalized.test_oracle_gaps == []


def test_unavailable_explicit_deliverable_evidence_is_not_promoted() -> None:
    """An inability to inspect tests does not fabricate a deliverable absence."""
    requirement = IssueRequirement("REQ-011", "Add a production-boundary regression for reload.")
    context = AdversarialValidationContext(all_changed_files=["tests/test_reload.py"], issue_requirements=[requirement])
    gap = _make_gap(requirement_id="REQ-011", boundary="reload", invariant="Could not inspect whether the reload test exists", anchor_path="tests/test_reload.py")
    gap.why_tests_still_pass = "Evidence unavailable because committed tests could not be inspected"
    result = AdversarialValidationResult(
        result="NEEDS_TESTS",
        test_oracle_gaps=[gap],
        requirement_coverage=[RequirementCoverageEntry("REQ-011", "UNVERIFIED", "Tests unavailable")],
    )

    normalized = _apply_coverage_and_verdict_precedence(result, context)

    assert normalized.findings == []
    assert normalized.requirement_coverage[0].status == "UNVERIFIED"
    assert normalized.result != "PASS"


def test_advisory_semantic_pr_validation_contract_evaluation() -> None:
    """Advisory semantic evaluation verifying contract boundary prompt policies."""
    from auto_coder.prompt_loader import get_prompt_template, render_prompt

    rendered_system_prompt = render_prompt("pr.adversarial_validation")
    initial_review_prompt = get_prompt_template("pr.adversarial_validation_initial_review")
    fix_prompt = get_prompt_template("pr.adversarial_validation_fix")

    # Rendered system prompt has prepended contract boundary policy
    assert "PR VALIDATION CONTRACT AND REVIEW CLASSIFICATION POLICY" in rendered_system_prompt
    assert "current explicit Requirements manifest" in rendered_system_prompt
    assert "DOCUMENTATION DEFECT INDEPENDENCE" in rendered_system_prompt

    # Initial review prompt
    assert "explicit Requirements manifest as the absolute authoritative source" in initial_review_prompt
    assert "Accept equivalent test techniques" in initial_review_prompt
    assert "Add ... regressions" in rendered_system_prompt
    assert "must include ... tests" in rendered_system_prompt
    assert "tests could not be inspected" in rendered_system_prompt

    # Fix prompt
    assert "Specification gaps in the report are non-actionable metadata" in fix_prompt
    assert "accept different valid test techniques" in fix_prompt
