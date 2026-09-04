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


def validation_response(payload: dict[str, object], result: str = "PASS") -> str:
    return json.dumps(
        {
            "result": result,
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
    )


def parsed_result(payload: dict[str, object]):
    return parse_adversarial_validation_response(validation_response(payload))


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
    assert result.test_oracle_gaps[0].requirement_text == "Server mutation paths reject invalid candidates independently of the browser."
    comment = format_adversarial_validation_comment(result, "sha-a")
    assert "missing regression protections, not demonstrated production-code violations" in comment
    assert "Focused regression scenario" in comment


def test_llm_requirement_text_is_ignored_in_favor_of_manifest_text() -> None:
    payload = gap_payload()
    payload["requirement_text"] = "A harmlessly reformatted model paraphrase."

    result = _apply_coverage_and_verdict_precedence(parsed_result(payload), context())

    assert result.result == "NEEDS_TESTS"
    assert result.diagnostic_category != "test_oracle_gap_requirement_text_mismatch"
    assert result.test_oracle_gaps[0].requirement_text == context().issue_requirements[0].text


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
    assert [gap.gap_id for gap in result.open_test_oracle_gaps] == [initial.gap_id]
    assert result.open_test_oracle_gaps[0].requirement_text == context().issue_requirements[0].text


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


def test_resolution_accepts_paraphrased_narrative_for_the_same_stable_scope() -> None:
    initial = parsed_result(gap_payload()).test_oracle_gaps[0]
    resolved_payload = gap_payload(
        status="RESOLVED",
        phase="REREVIEW",
        resolution_evidence="The committed direct-boundary regression test protects the recorded invariant.",
    )
    resolved_payload["plausible_incorrect_implementation"] = "The server rejection check is omitted."
    resolved_payload["why_tests_still_pass"] = "Earlier coverage never reached this server entry point."
    resolved_payload["material_consequence"] = "Rejected data could alter durable state."
    resolved_payload["focused_regression_scenario"] = "Directly reject the candidate and compare durable state before and after."

    result = _reconcile_test_oracle_gap_lifecycle(parsed_result(resolved_payload), prior_session(initial), "sha-b")
    result = _apply_coverage_and_verdict_precedence(result, context())

    assert result.result == "PASS"
    assert result.test_oracle_gaps[0].status == "RESOLVED"
    assert result.test_oracle_gaps[0].focused_regression_scenario == initial.focused_regression_scenario


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
    assert [gap.gap_id for gap in result.test_oracle_gaps] == [resolved.gap_id]
    assert result.test_oracle_gaps[0].status == "RESOLVED"
    assert result.test_oracle_gaps[0].requirement_text == context().issue_requirements[0].text


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
        patch("auto_coder.adversarial_validator.run_llm_prompt", return_value=response) as run_prompt,
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
    assert saved.test_oracle_gaps[0].requirement_text == validation_context.issue_requirements[0].text
    assert '"requirement_text"' not in run_prompt.call_args.args[0]


def test_validation_run_rejects_unknown_gap_requirement_id(tmp_path) -> None:
    validation_context = context()
    validation_context.issue_context = "Linked Issue requires independent server validation."
    registry = ReviewerSessionRegistry(tmp_path / "reviewer-sessions.json")
    manager = MagicMock()
    manager.get_current_backend_identity.return_value = ("reviewer", "codex", "strong")
    manager._last_session_id = "session-1"
    payload = gap_payload()
    payload["requirement_id"] = "REQ-999"
    payload["gap_id"] = _stable_test_oracle_gap_id(
        "REQ-999",
        str(payload["authoritative_boundary"]),
        str(payload["invariant"]),
    )

    with (
        patch("auto_coder.adversarial_validator.build_adversarial_validation_context", return_value=validation_context),
        patch(
            "auto_coder.adversarial_validator.run_llm_prompt",
            return_value=validation_response(payload, "NEEDS_TESTS"),
        ),
    ):
        result = run_adversarial_validation(
            "owner/repo",
            {"number": 1, "head": {"sha": "sha-a"}},
            AutomationConfig(),
            backend_manager=manager,
            session_registry=registry,
        )

    assert result.result == "ERROR"
    assert result.diagnostic_category == "unknown_test_oracle_gap_requirement_id"
    assert result.test_oracle_gaps == []


def test_rereview_prompt_replaces_persisted_requirement_text_from_manifest(tmp_path) -> None:
    stale_text = "A stale model-authored paraphrase from an earlier review."
    authoritative_text = context().issue_requirements[0].text
    persisted_gap = parsed_result(gap_payload()).test_oracle_gaps[0]
    persisted_gap.requirement_text = stale_text
    registry = ReviewerSessionRegistry(tmp_path / "reviewer-sessions.json")
    registry.save(prior_session(persisted_gap, "sha-a"))
    validation_context = context()
    validation_context.issue_context = "Linked Issue requires independent server validation."
    manager = MagicMock()
    manager.get_current_backend_identity.return_value = ("reviewer", "codex", "strong")
    manager._last_session_id = "session-1"
    manager.continue_session.return_value = validation_response(
        gap_payload(phase="REREVIEW"),
        "NEEDS_TESTS",
    )

    with patch(
        "auto_coder.adversarial_validator.build_adversarial_validation_context",
        return_value=validation_context,
    ):
        result = run_adversarial_validation(
            "owner/repo",
            {"number": 1, "head": {"sha": "sha-b"}},
            AutomationConfig(),
            backend_manager=manager,
            session_registry=registry,
        )

    prompt = manager.continue_session.call_args.args[1]
    assert result.result == "NEEDS_TESTS"
    assert authoritative_text in prompt
    assert stale_text not in prompt


def test_failed_first_attempt_keeps_retry_in_initial_discovery_phase(tmp_path) -> None:
    validation_context = context()
    validation_context.issue_context = "Linked Issue requires independent server validation."
    registry = ReviewerSessionRegistry(tmp_path / "reviewer-sessions.json")
    manager = MagicMock()
    manager.get_current_backend_identity.return_value = ("reviewer", "codex", "strong")
    manager._last_session_id = "session-1"
    manager.continue_session.return_value = validation_response(gap_payload(), "NEEDS_TESTS")

    with (
        patch("auto_coder.adversarial_validator.build_adversarial_validation_context", return_value=validation_context),
        patch("auto_coder.adversarial_validator.run_llm_prompt", return_value="malformed response"),
    ):
        failed = run_adversarial_validation(
            "owner/repo",
            {"number": 1, "head": {"sha": "sha-a"}},
            AutomationConfig(),
            backend_manager=manager,
            session_registry=registry,
        )
        saved_after_failure = registry.get("owner/repo", 1, "reviewer", "codex", "strong")
        retried = run_adversarial_validation(
            "owner/repo",
            {"number": 1, "head": {"sha": "sha-a"}},
            AutomationConfig(),
            backend_manager=manager,
            session_registry=registry,
        )

    assert failed.result == "ERROR"
    assert saved_after_failure is not None
    assert saved_after_failure.last_head_sha == ""
    assert retried.result == "NEEDS_TESTS"
    retry_prompt = manager.continue_session.call_args.args[1]
    assert "Your mission: Falsify the implementation" in retry_prompt
    assert "Do NOT restart unrestricted broad adversarial exploration" not in retry_prompt


def test_incomplete_initial_review_does_not_advance_the_lifecycle_checkpoint(tmp_path) -> None:
    validation_context = context()
    validation_context.issue_context = "Linked Issue requires independent server validation."
    validation_context.unverified_files = ["src/unavailable.py"]
    validation_context.all_changed_files.append("src/unavailable.py")
    registry = ReviewerSessionRegistry(tmp_path / "reviewer-sessions.json")
    manager = MagicMock()
    manager.get_current_backend_identity.return_value = ("reviewer", "codex", "strong")
    manager._last_session_id = "session-1"

    with (
        patch("auto_coder.adversarial_validator.build_adversarial_validation_context", return_value=validation_context),
        patch("auto_coder.adversarial_validator.run_llm_prompt", return_value=validation_response(gap_payload(), "NEEDS_TESTS")),
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
    assert result.diagnostic_category == "incomplete_evidence_coverage"
    assert saved is not None
    assert saved.last_head_sha == ""
    assert saved.test_oracle_gaps == []


def test_failed_new_head_attempt_does_not_prevent_gap_resolution_on_retry(tmp_path) -> None:
    initial = parsed_result(gap_payload()).test_oracle_gaps[0]
    registry = ReviewerSessionRegistry(tmp_path / "reviewer-sessions.json")
    registry.save(prior_session(initial, "sha-a"))
    validation_context = context()
    validation_context.issue_context = "Linked Issue requires independent server validation."
    manager = MagicMock()
    manager.get_current_backend_identity.return_value = ("reviewer", "codex", "strong")
    manager._last_session_id = "session-1"
    resolved = gap_payload(
        status="RESOLVED",
        phase="REREVIEW",
        resolution_evidence="The new commit directly tests rejection and unchanged durable state.",
    )
    manager.continue_session.side_effect = ["malformed response", validation_response(resolved)]

    with patch("auto_coder.adversarial_validator.build_adversarial_validation_context", return_value=validation_context):
        failed = run_adversarial_validation(
            "owner/repo",
            {"number": 1, "head": {"sha": "sha-b"}},
            AutomationConfig(),
            backend_manager=manager,
            session_registry=registry,
        )
        saved_after_failure = registry.get("owner/repo", 1, "reviewer", "codex", "strong")
        retried = run_adversarial_validation(
            "owner/repo",
            {"number": 1, "head": {"sha": "sha-b"}},
            AutomationConfig(),
            backend_manager=manager,
            session_registry=registry,
        )

    saved_after_retry = registry.get("owner/repo", 1, "reviewer", "codex", "strong")
    assert failed.result == "ERROR"
    assert saved_after_failure is not None
    assert saved_after_failure.last_head_sha == "sha-a"
    assert saved_after_failure.test_oracle_gaps[0].status == "OPEN"
    assert retried.result == "PASS"
    assert saved_after_retry is not None
    assert saved_after_retry.last_head_sha == "sha-b"
    assert saved_after_retry.test_oracle_gaps[0].status == "RESOLVED"


def test_non_authoritative_resolved_response_preserves_the_open_checkpoint(tmp_path) -> None:
    initial = parsed_result(gap_payload()).test_oracle_gaps[0]
    registry = ReviewerSessionRegistry(tmp_path / "reviewer-sessions.json")
    registry.save(prior_session(initial, "sha-a"))
    validation_context = context()
    validation_context.issue_context = "Linked Issue requires independent server validation."
    validation_context.unverified_files = ["src/unavailable.py"]
    validation_context.all_changed_files.append("src/unavailable.py")
    manager = MagicMock()
    manager.get_current_backend_identity.return_value = ("reviewer", "codex", "strong")
    manager._last_session_id = "session-1"
    resolved = gap_payload(
        status="RESOLVED",
        phase="REREVIEW",
        resolution_evidence="The new commit contains a direct regression test.",
    )
    manager.continue_session.return_value = validation_response(resolved)

    with patch("auto_coder.adversarial_validator.build_adversarial_validation_context", return_value=validation_context):
        result = run_adversarial_validation(
            "owner/repo",
            {"number": 1, "head": {"sha": "sha-b"}},
            AutomationConfig(),
            backend_manager=manager,
            session_registry=registry,
        )

    saved = registry.get("owner/repo", 1, "reviewer", "codex", "strong")
    assert result.result == "ERROR"
    assert result.diagnostic_category == "pass_with_unresolved_changed_file_evidence"
    assert result.test_oracle_gaps[0].status == "RESOLVED"
    assert saved is not None
    assert saved.last_head_sha == "sha-a"
    assert saved.test_oracle_gaps[0].status == "OPEN"
    assert initial.status == "OPEN"
