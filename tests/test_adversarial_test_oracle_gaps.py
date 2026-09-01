"""Material test-oracle-gap lifecycle and convergence tests."""

import json
from unittest.mock import MagicMock, patch

from auto_coder.adversarial_validator import (
    AdversarialValidationContext,
    IssueRequirement,
    _apply_coverage_and_verdict_precedence,
    _reconcile_test_oracle_gap_lifecycle,
    _stable_test_oracle_gap_id,
    format_adversarial_validation_comment,
    parse_adversarial_validation_response,
    run_adversarial_validation,
)
from auto_coder.automation_config import AutomationConfig
from auto_coder.reviewer_session_registry import ReviewerSession, ReviewerSessionRegistry, TestOracleGap


def gap_payload(
    *,
    status: str = "OPEN",
    phase: str = "INITIAL",
    exception: str = "NONE",
    exception_evidence: str = "",
    boundary: str = "GridMutation.apply_candidate",
    resolution_evidence: str = "",
) -> dict[str, object]:
    requirement_id = "REQ-001"
    invariant = "Rejected candidates preserve stored state and revision."
    return {
        "gap_id": _stable_test_oracle_gap_id(requirement_id, boundary, invariant),
        "requirement_id": requirement_id,
        "requirement_text": "Server mutation paths reject invalid candidates independently of the browser.",
        "authoritative_boundary": boundary,
        "invariant": invariant,
        "plausible_incorrect_implementation": "Delete the server-side rejection guard.",
        "why_tests_still_pass": "Client tests reject first and server tests invoke only ScheduleMutation.",
        "material_consequence": "Invalid persisted Grid state and revision changes become possible.",
        "focused_regression_scenario": "Call GridMutation directly with an invalid candidate and assert rejection, unchanged state, and unchanged revision.",
        "anchor_path": "src/grid.py",
        "anchor_line": 12,
        "anchor_side": "RIGHT",
        "anchor_start_line": None,
        "discovery_phase": phase,
        "rereview_exception_reason": exception,
        "rereview_exception_evidence": exception_evidence,
        "status": status,
        "resolution_evidence": resolution_evidence,
    }


def parsed_result(payload: dict[str, object]):
    response = {
        "result": "PASS",
        "summary": "Production behavior is correct.",
        "requirement_coverage": [
            {
                "requirement_id": "REQ-001",
                "status": "VERIFIED",
                "evidence": "The server guard enforces the Issue requirement.",
            }
        ],
        "findings": [],
        "test_oracle_gaps": [payload],
    }
    return parse_adversarial_validation_response(json.dumps(response))


def context() -> AdversarialValidationContext:
    return AdversarialValidationContext(
        pr_diff="diff --git a/src/grid.py b/src/grid.py\n+guard = True",
        all_changed_files=["src/grid.py"],
        issue_requirements=[IssueRequirement("REQ-001", "Server mutation paths reject invalid candidates independently of the browser.")],
    )


def prior_session(gap: TestOracleGap, head_sha: str = "sha-a") -> ReviewerSession:
    return ReviewerSession(
        repository="owner/repo",
        pr_number=1,
        backend_name="reviewer",
        backend_type="codex",
        model_name="strong",
        session_id="session-1",
        last_head_sha=head_sha,
        test_oracle_gaps=[gap],
    )


def test_initial_gap_is_separate_from_a_production_violation_and_blocks_merge() -> None:
    result = parsed_result(gap_payload())
    result = _reconcile_test_oracle_gap_lifecycle(result, None, "sha-a")
    result = _apply_coverage_and_verdict_precedence(result, context())

    assert result.result == "NEEDS_TESTS"
    assert result.needs_tests is True
    assert result.needs_fix is False
    assert result.findings == []
    assert result.allows_auto_merge is False
    assert result.requirement_coverage[0].status == "VERIFIED"
    comment = format_adversarial_validation_comment(result, "sha-a")
    assert "missing regression protections, not demonstrated production-code violations" in comment
    assert "Focused regression scenario" in comment


def test_same_sha_inspection_cannot_resolve_an_open_gap() -> None:
    initial = parsed_result(gap_payload()).test_oracle_gaps[0]
    resolved_payload = gap_payload(
        status="RESOLVED",
        phase="REREVIEW",
        resolution_evidence="Reviewer inspected correct production behavior.",
    )
    rereview = parsed_result(resolved_payload)

    result = _reconcile_test_oracle_gap_lifecycle(rereview, prior_session(initial), "sha-a")
    result = _apply_coverage_and_verdict_precedence(result, context())

    assert result.result == "NEEDS_TESTS"
    assert result.open_test_oracle_gaps == [initial]


def test_new_commit_with_focused_boundary_test_can_resolve_and_converge() -> None:
    initial = parsed_result(gap_payload()).test_oracle_gaps[0]
    resolved_payload = gap_payload(
        status="RESOLVED",
        phase="REREVIEW",
        resolution_evidence="tests/test_grid.py directly calls GridMutation and asserts rejection plus unchanged state and revision.",
    )
    rereview = parsed_result(resolved_payload)

    result = _reconcile_test_oracle_gap_lifecycle(rereview, prior_session(initial), "sha-b")
    result = _apply_coverage_and_verdict_precedence(result, context())

    assert result.result == "PASS"
    assert result.is_pass is True
    assert result.test_oracle_gaps[0].status == "RESOLVED"


def test_rereview_discards_unbounded_new_gap_but_accepts_required_exception() -> None:
    resolved = parsed_result(gap_payload()).test_oracle_gaps[0]
    resolved.status = "RESOLVED"
    resolved.resolution_evidence = "Focused committed test exists."
    new_payload = gap_payload(
        phase="REREVIEW",
        boundary="CalendarMutation.apply_candidate",
    )

    unrestricted = parsed_result(new_payload)
    unrestricted_result = _reconcile_test_oracle_gap_lifecycle(unrestricted, prior_session(resolved), "sha-b")
    unrestricted_result = _apply_coverage_and_verdict_precedence(unrestricted_result, context())

    assert unrestricted_result.result == "PASS"
    assert [gap.status for gap in unrestricted_result.test_oracle_gaps] == ["RESOLVED"]
    assert "discarded newly invented test-oracle gaps" in unrestricted_result.summary

    permitted_payload = gap_payload(
        phase="REREVIEW",
        exception="CORRECTIVE_DIFF_NEW_BOUNDARY",
        exception_evidence="The corrective diff added CalendarMutation.apply_candidate.",
        boundary="CalendarMutation.apply_candidate",
    )
    permitted = parsed_result(permitted_payload)
    permitted_result = _reconcile_test_oracle_gap_lifecycle(permitted, prior_session(resolved), "sha-b")
    permitted_result = _apply_coverage_and_verdict_precedence(permitted_result, context())

    assert permitted_result.result == "NEEDS_TESTS"
    assert len(permitted_result.open_test_oracle_gaps) == 1


def test_resolved_gap_cannot_be_reopened_with_a_variant() -> None:
    resolved = parsed_result(gap_payload()).test_oracle_gaps[0]
    resolved.status = "RESOLVED"
    resolved.resolution_evidence = "The focused direct-boundary test was committed."
    attempted_reopen = parsed_result(gap_payload(phase="REREVIEW"))

    result = _reconcile_test_oracle_gap_lifecycle(attempted_reopen, prior_session(resolved), "sha-c")
    result = _apply_coverage_and_verdict_precedence(result, context())

    assert result.result == "PASS"
    assert result.test_oracle_gaps == [resolved]


def test_validation_run_persists_gap_identity_and_scope_for_rereview(tmp_path) -> None:
    validation_context = context()
    validation_context.issue_context = "Linked Issue requires independent server validation."
    registry = ReviewerSessionRegistry(tmp_path / "reviewer-sessions.json")
    manager = MagicMock()
    manager.get_current_backend_identity.return_value = ("reviewer", "codex", "strong")
    manager._last_session_id = "session-1"
    response = json.dumps(
        {
            "result": "NEEDS_TESTS",
            "summary": "The implementation is correct but the server boundary lacks a direct oracle.",
            "requirement_coverage": [
                {
                    "requirement_id": "REQ-001",
                    "status": "VERIFIED",
                    "evidence": "The server guard is present.",
                }
            ],
            "findings": [],
            "test_oracle_gaps": [gap_payload()],
        }
    )

    with (
        patch("auto_coder.adversarial_validator.build_adversarial_validation_context", return_value=validation_context),
        patch("auto_coder.adversarial_validator.run_llm_prompt", return_value=response),
    ):
        result = run_adversarial_validation(
            "owner/repo",
            {"number": 1, "head": {"sha": "sha-a"}},
            AutomationConfig(),
            backend_manager=manager,
            session_registry=registry,
        )

    saved = registry.get("owner/repo", 1, "reviewer", "codex", "strong")
    assert result.result == "NEEDS_TESTS"
    assert saved is not None
    assert saved.last_head_sha == "sha-a"
    assert saved.test_oracle_gaps == result.test_oracle_gaps
