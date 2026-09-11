"""Material test-oracle-gap lifecycle and convergence tests."""

import json
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.adversarial_validator import (
    AdversarialValidationContext,
    AdversarialValidationResult,
    IssueRequirement,
    ReviewThreadDisposition,
    _addressed_test_oracle_gap_evidence,
    _apply_coverage_and_verdict_precedence,
    _reconcile_test_oracle_gap_lifecycle,
    _stable_test_oracle_gap_id,
    format_adversarial_validation_comment,
    format_test_oracle_gap_comment,
    parse_adversarial_validation_response,
    run_adversarial_validation,
)
from auto_coder.automation_config import AutomationConfig
from auto_coder.review_feedback_marker import REVIEW_ADDRESSED_MARKER
from auto_coder.review_thread_validation import (
    ClaimedReviewThread,
    StaleReviewThreadRegistry,
    classify_review_threads,
    render_claimed_review_threads_section,
    resolve_addressed_review_threads,
)
from auto_coder.reviewer_session_registry import ReviewerSession, ReviewerSessionRegistry, TestOracleGap
from auto_coder.util.gh_cache import ReviewThread, ReviewThreadComment


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


def test_addressed_gap_evidence_rejects_a_root_with_the_wrong_requirement() -> None:
    gap = parsed_result(gap_payload()).test_oracle_gaps[0]
    result = parsed_result(gap_payload())
    result.thread_dispositions = [
        ReviewThreadDisposition(
            thread_id="thread-gap",
            status="ADDRESSED",
            rationale="The requested regression is present.",
            evidence="tests/test_grid.py exercises the persisted-state invariant.",
        )
    ]
    claimed = ClaimedReviewThread(
        thread_id="thread-gap",
        original_finding=(f"### Auto-Coder material test-oracle gap\n\nGap identity: `{gap.gap_id}`\n\n" "**Issue requirement**\n\n`REQ-OTHER`: unrelated requirement"),
    )

    assert _addressed_test_oracle_gap_evidence(result, (claimed,), (gap,)) == {}


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


def test_same_sha_current_head_evidence_can_resolve_a_stale_open_gap() -> None:
    initial = parsed_result(gap_payload()).test_oracle_gaps[0]
    resolved_payload = gap_payload(
        status="RESOLVED",
        phase="REREVIEW",
        resolution_evidence="The committed direct-boundary regression test exercises rejection and unchanged state.",
    )
    rereview = parsed_result(resolved_payload)

    result = _reconcile_test_oracle_gap_lifecycle(rereview, prior_session(initial), "sha-a")
    result = _apply_coverage_and_verdict_precedence(result, context())

    assert result.result == "PASS"
    assert result.open_test_oracle_gaps == []
    assert result.test_oracle_gaps[0].status == "RESOLVED"


def test_explicit_resolution_without_stored_gap_id_fails_closed() -> None:
    initial = parsed_result(gap_payload()).test_oracle_gaps[0]
    resolved_payload = gap_payload(
        status="RESOLVED",
        phase="REREVIEW",
        resolution_evidence="The direct-boundary regression is present.",
    )
    del resolved_payload["gap_id"]

    result = parsed_result(resolved_payload)

    assert result.result == "ERROR"
    assert result.diagnostic_category == "schema_error"
    assert "explicit gap_id" in (result.diagnostic_reason or "")
    assert initial.status == "OPEN"


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

    assert unrestricted_result.result == "BLOCKED"
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

    assert permitted_result.result == "BLOCKED"
    assert len(permitted_result.open_test_oracle_gaps) == 1


def test_new_head_open_evidence_reopens_gap_and_preserves_historical_closure() -> None:
    resolved = parsed_result(gap_payload()).test_oracle_gaps[0]
    resolved.status = "RESOLVED"
    resolved.resolution_evidence = "The focused direct-boundary test was committed."
    attempted_reopen = parsed_result(gap_payload(phase="REREVIEW"))

    result = _reconcile_test_oracle_gap_lifecycle(attempted_reopen, prior_session(resolved), "sha-c")
    result = _apply_coverage_and_verdict_precedence(result, context())

    assert result.result == "NEEDS_TESTS"
    assert [gap.gap_id for gap in result.test_oracle_gaps] == [resolved.gap_id]
    assert result.test_oracle_gaps[0].status == "OPEN"
    assert result.test_oracle_gaps[0].resolution_evidence == ""
    assert result.test_oracle_gaps[0].historical_resolution_head_sha == "sha-a"
    assert result.test_oracle_gaps[0].historical_resolution_evidence == "The focused direct-boundary test was committed."


def test_new_head_omission_cannot_become_reusable_after_session_head_advances() -> None:
    resolved = parsed_result(gap_payload()).test_oracle_gaps[0]
    resolved.status = "RESOLVED"
    resolved.resolution_evidence = "H1 independently proved the focused regression."

    first_h2 = _reconcile_test_oracle_gap_lifecycle(
        AdversarialValidationResult(result="NEEDS_FIX", summary="An unrelated implementation finding remains."),
        prior_session(resolved, "sha-h1"),
        "sha-h2",
    )
    persisted_h2 = prior_session(first_h2.test_oracle_gaps[0], "sha-h2")
    second_h2 = _reconcile_test_oracle_gap_lifecycle(
        AdversarialValidationResult(result="PASS", summary="No other blocker remains."),
        persisted_h2,
        "sha-h2",
    )

    assert first_h2.result == "BLOCKED"
    assert first_h2.test_oracle_gaps[0].resolution_head_sha == "sha-h1"
    assert second_h2.result == "BLOCKED"
    assert second_h2.diagnostic_category == "test_oracle_gap_current_head_evidence_missing"
    assert second_h2.test_oracle_gaps[0].resolution_head_sha == "sha-h1"


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


@pytest.mark.parametrize("echo_gap", [False, True])
@pytest.mark.parametrize("unrelated_blocker", ["provenance", "evidence", "finding"])
def test_same_head_addressed_gap_thread_persists_before_unrelated_inconclusive_resolution(tmp_path, echo_gap, unrelated_blocker) -> None:
    """Exercise the production validator boundary used before thread resolution."""
    initial = parsed_result(gap_payload()).test_oracle_gaps[0]
    registry = ReviewerSessionRegistry(tmp_path / "reviewer-sessions.json")
    registry.save(prior_session(initial, "sha-a"))
    validation_context = context()
    validation_context.issue_context = "Linked Issue requires independent server validation."
    if unrelated_blocker in {"evidence", "finding"}:
        validation_context.issue_requirements.append(IssueRequirement("REQ-002", "Audit evidence must be available."))
    if unrelated_blocker == "finding":
        validation_context.unverified_files = ["src/audit.py"]
        validation_context.all_changed_files.append("src/audit.py")
    classification = classify_review_threads(
        (
            ReviewThread(
                id="thread-gap",
                comments=[
                    ReviewThreadComment(
                        database_id=42,
                        author_id=7,
                        author_login="auto-coder-reviewer[bot]",
                        body=(f"### Auto-Coder material test-oracle gap\n\nGap identity: `{initial.gap_id}`\n\n" f"**Issue requirement**\n\n`{initial.requirement_id}`: {context().issue_requirements[0].text}"),
                    ),
                    ReviewThreadComment(
                        database_id=43,
                        author_id=8,
                        author_login="agent[bot]",
                        body=f"Added and ran the direct-boundary regression test.\n{REVIEW_ADDRESSED_MARKER}",
                    ),
                ],
            ),
        ),
        {7},
    )
    assert classification.blocking_unresolved_count == 0
    assert len(classification.claimed) == 1
    claimed = classification.claimed[0]
    manager = MagicMock()
    manager.get_current_backend_identity.return_value = ("reviewer", "codex", "strong")
    manager._last_session_id = "session-1"
    response = {
        "result": "INCONCLUSIVE" if unrelated_blocker == "evidence" else "NEEDS_FIX" if unrelated_blocker == "finding" else "PASS",
        "summary": "Requirements and regression protections are verified; provenance remains unclear.",
        "requirement_coverage": [
            {"requirement_id": "REQ-001", "status": "VERIFIED", "evidence": "Guard and direct regression test verified."},
            *([{"requirement_id": "REQ-002", "status": "UNVERIFIED", "evidence": "The audit artifact is unavailable."}] if unrelated_blocker == "evidence" else []),
            *([{"requirement_id": "REQ-002", "status": "VIOLATED", "evidence": "The audit handler returns success after failure."}] if unrelated_blocker == "finding" else []),
        ],
        "findings": (
            [
                {
                    "requirement_ids": ["REQ-002"],
                    "finding_identity": "audit-failure-is-swallowed",
                    "correction_identity": "propagate-audit-failure",
                    "violated_requirement": "Audit evidence must be available.",
                    "evidence_classification": "DEMONSTRATED",
                    "reachability": "The production audit handler catches the storage failure.",
                    "required_behavior": "Report the audit failure.",
                    "actual_behavior": "Returns success.",
                    "evidence": "src/audit.py catches and returns success.",
                    "counterexample": "Given a storage failure, the handler reports success.",
                    "anchor_path": "src/audit.py",
                }
            ]
            if unrelated_blocker == "finding"
            else []
        ),
        "test_oracle_gaps": [gap_payload(phase="REREVIEW")] if echo_gap else [],
        "unexplained_changes": (
            [
                {
                    "paths": ["docs/generated.md"],
                    "change_group": "Generated documentation contract update.",
                    "why_unexplained": "No source relationship is established.",
                }
            ]
            if unrelated_blocker == "provenance"
            else []
        ),
        "evidence_recovery": (
            [
                {
                    "path": "artifacts/audit.json",
                    "source": "repository inspection",
                    "status": "UNAVAILABLE",
                    "evidence": "The required audit artifact is absent from the checkout.",
                    "requirement_ids": ["REQ-002"],
                }
            ]
            if unrelated_blocker == "evidence"
            else []
        ),
        "decision_critical_evidence_gaps": (
            [
                {
                    "requirement_id": "REQ-002",
                    "evidence_needed": "The generated audit artifact.",
                    "recovery_attempts": ["Inspected the current checkout."],
                }
            ]
            if unrelated_blocker == "evidence"
            else []
        ),
        "thread_dispositions": [
            {
                "thread_id": "thread-gap",
                "status": "ADDRESSED",
                "rationale": "The exact requested persisted-state invariant is now covered.",
                "evidence": "tests/test_grid.py calls GridMutation directly and asserts rejection, unchanged state, and revision.",
            }
        ],
    }
    manager.continue_session.return_value = json.dumps(response)

    with patch("auto_coder.adversarial_validator.build_adversarial_validation_context", return_value=validation_context):
        result = run_adversarial_validation(
            "owner/repo",
            {"number": 1, "head": {"sha": "sha-a"}},
            AutomationConfig(),
            backend_manager=manager,
            session_registry=registry,
            claimed_review_threads_section=render_claimed_review_threads_section((claimed,)),
            claimed_review_threads=(claimed,),
        )

    saved = registry.get("owner/repo", 1, "reviewer", "codex", "strong")
    assert result.result == ("NEEDS_FIX" if unrelated_blocker == "finding" else "INCONCLUSIVE")
    assert result.diagnostic_category == ("change_provenance_clarification" if unrelated_blocker == "provenance" else "incomplete_evidence_coverage" if unrelated_blocker == "finding" else None)
    assert result.open_test_oracle_gaps == []
    assert result.thread_dispositions[0].status == "ADDRESSED"
    assert saved is not None
    assert saved.last_head_sha == "sha-a"
    assert saved.test_oracle_gaps[0].status == "RESOLVED"
    assert "tests/test_grid.py" in saved.test_oracle_gaps[0].resolution_evidence

    client = MagicMock()
    client.get_pull_request_head_sha_strict.return_value = "sha-a"
    client.resolve_review_thread.return_value = True
    resolved = resolve_addressed_review_threads(
        client,
        "owner/repo",
        1,
        "sha-a",
        (claimed,),
        result.thread_dispositions,
        stale_registry=StaleReviewThreadRegistry(tmp_path / "stale-threads.json"),
    )

    assert resolved == ["thread-gap"]
    client.resolve_review_thread.assert_called_once_with("thread-gap")
    reloaded = registry.get("owner/repo", 1, "reviewer", "codex", "strong")
    assert reloaded is not None
    assert reloaded.test_oracle_gaps[0].status == "RESOLVED"


def test_same_head_explicit_gap_resolution_persists_when_resolved_thread_is_not_classified(tmp_path) -> None:
    initial = parsed_result(gap_payload()).test_oracle_gaps[0]
    registry = ReviewerSessionRegistry(tmp_path / "reviewer-sessions.json")
    registry.save(prior_session(initial, "sha-a"))
    resolved_thread = ReviewThread(
        id="thread-gap",
        is_resolved=True,
        comments=[
            ReviewThreadComment(
                database_id=42,
                author_id=7,
                author_login="auto-coder-reviewer[bot]",
                body=(f"### Auto-Coder material test-oracle gap\n\nGap identity: `{initial.gap_id}`\n\n" f"**Issue requirement**\n\n`{initial.requirement_id}`: {context().issue_requirements[0].text}"),
            )
        ],
    )
    classification = classify_review_threads((resolved_thread,), {7})
    assert classification.claimed == ()
    assert classification.blocking_unresolved_count == 0

    validation_context = context()
    validation_context.issue_context = "Linked Issue requires independent server validation."
    resolved_payload = gap_payload(
        status="RESOLVED",
        phase="REREVIEW",
        resolution_evidence="tests/test_grid.py directly verifies rejection and unchanged persisted state at sha-a.",
    )
    manager = MagicMock()
    manager.get_current_backend_identity.return_value = ("reviewer", "codex", "strong")
    manager._last_session_id = "session-1"
    manager.continue_session.return_value = json.dumps(
        {
            "result": "PASS",
            "summary": "Regression protection is proven; documentation provenance remains unclear.",
            "requirement_coverage": [{"requirement_id": "REQ-001", "status": "VERIFIED", "evidence": "Current-head guard and direct regression verified."}],
            "findings": [],
            "test_oracle_gaps": [resolved_payload],
            "unexplained_changes": [
                {
                    "paths": ["docs/generated.md"],
                    "change_group": "Generated documentation contract update.",
                    "why_unexplained": "No source relationship is established.",
                }
            ],
            "thread_dispositions": [],
        }
    )

    with patch("auto_coder.adversarial_validator.build_adversarial_validation_context", return_value=validation_context):
        result = run_adversarial_validation(
            "owner/repo",
            {"number": 1, "head": {"sha": "sha-a"}},
            AutomationConfig(),
            backend_manager=manager,
            session_registry=registry,
            claimed_review_threads_section=render_claimed_review_threads_section(classification.claimed),
            claimed_review_threads=classification.claimed,
        )

    saved = registry.get("owner/repo", 1, "reviewer", "codex", "strong")
    assert result.result == "INCONCLUSIVE"
    assert result.diagnostic_category == "change_provenance_clarification"
    assert result.thread_dispositions == []
    assert result.test_oracle_gaps[0].status == "RESOLVED"
    assert saved is not None
    assert saved.last_head_sha == "sha-a"
    assert saved.test_oracle_gaps[0].status == "RESOLVED"
    assert saved.test_oracle_gaps[0].resolution_evidence == resolved_payload["resolution_evidence"]


def test_deferred_production_checkpoint_does_not_eagerly_mutate_registry(tmp_path) -> None:
    initial = parsed_result(gap_payload()).test_oracle_gaps[0]
    registry = ReviewerSessionRegistry(tmp_path / "reviewer-sessions.json")
    registry.save(prior_session(initial, "sha-a"))
    validation_context = context()
    validation_context.issue_context = "Linked Issue requires independent server validation."
    manager = MagicMock()
    manager.get_current_backend_identity.return_value = ("reviewer", "codex", "strong")
    manager._last_session_id = "session-1"
    manager.continue_session.return_value = validation_response(
        gap_payload(
            status="RESOLVED",
            phase="REREVIEW",
            resolution_evidence="The exact focused regression protects the persisted-state boundary.",
        )
    )

    with patch("auto_coder.adversarial_validator.build_adversarial_validation_context", return_value=validation_context):
        result = run_adversarial_validation(
            "owner/repo",
            {"number": 1, "head": {"sha": "sha-a"}},
            AutomationConfig(),
            backend_manager=manager,
            session_registry=registry,
            defer_session_persistence=True,
        )

    unchanged = registry.get("owner/repo", 1, "reviewer", "codex", "strong")
    assert unchanged is not None
    assert unchanged.test_oracle_gaps[0].status == "OPEN"
    assert result.reviewer_session_checkpoint is not None
    assert result.reviewer_session_checkpoint.test_oracle_gaps[0].status == "RESOLVED"


def test_current_gap_closure_persists_while_historical_gap_blocks_new_head(tmp_path) -> None:
    first = parsed_result(gap_payload()).test_oracle_gaps[0]
    historical = parsed_result(gap_payload(boundary="AuditMutation.commit")).test_oracle_gaps[0]
    historical.status = "RESOLVED"
    historical.resolution_evidence = "H1 independently proved the audit regression."
    historical.resolution_head_sha = "sha-h1"
    registry = ReviewerSessionRegistry(tmp_path / "reviewer-sessions.json")
    session = prior_session(first, "sha-h1")
    session.test_oracle_gaps.append(historical)
    registry.save(session)
    validation_context = context()
    validation_context.issue_context = "Linked Issue requires independent server validation."
    root = format_test_oracle_gap_comment(first)
    classification = classify_review_threads(
        (
            ReviewThread(
                id="thread-first",
                comments=[
                    ReviewThreadComment(database_id=42, author_id=7, author_login="auto-coder-reviewer[bot]", body=root),
                    ReviewThreadComment(
                        database_id=43,
                        author_id=8,
                        author_login="agent[bot]",
                        body=f"Added the focused regression.\n{REVIEW_ADDRESSED_MARKER}",
                    ),
                ],
            ),
        ),
        {7},
    )
    manager = MagicMock()
    manager.get_current_backend_identity.return_value = ("reviewer", "codex", "strong")
    manager._last_session_id = "session-1"
    manager.continue_session.return_value = json.dumps(
        {
            "result": "PASS",
            "summary": "The first regression is proven; current audit protection is unavailable.",
            "requirement_coverage": [{"requirement_id": "REQ-001", "status": "VERIFIED", "evidence": "Current implementation behavior is verified."}],
            "findings": [],
            "test_oracle_gaps": [],
            "thread_dispositions": [
                {
                    "thread_id": "thread-first",
                    "status": "ADDRESSED",
                    "rationale": "The exact requested first-gap invariant is covered.",
                    "evidence": "tests/test_grid.py exercises the authoritative boundary on sha-h2.",
                }
            ],
        }
    )

    with patch("auto_coder.adversarial_validator.build_adversarial_validation_context", return_value=validation_context):
        result = run_adversarial_validation(
            "owner/repo",
            {"number": 1, "head": {"sha": "sha-h2"}},
            AutomationConfig(),
            backend_manager=manager,
            session_registry=registry,
            claimed_review_threads_section=render_claimed_review_threads_section(classification.claimed),
            claimed_review_threads=classification.claimed,
        )

    saved = ReviewerSessionRegistry(registry.path).get("owner/repo", 1, "reviewer", "codex", "strong")
    assert result.result == "BLOCKED"
    assert result.diagnostic_category == "test_oracle_gap_current_head_evidence_missing"
    assert saved is not None
    saved_by_id = {gap.gap_id: gap for gap in saved.test_oracle_gaps}
    assert saved_by_id[first.gap_id].status == "RESOLVED"
    assert saved_by_id[first.gap_id].resolution_head_sha == "sha-h2"
    assert saved_by_id[historical.gap_id].resolution_head_sha == "sha-h1"
    assert saved_by_id[historical.gap_id].historical_resolution_head_sha == "sha-h1"


def test_gap_persistence_failure_prevents_thread_projection_and_same_head_retry_recovers(tmp_path) -> None:
    initial = parsed_result(gap_payload()).test_oracle_gaps[0]
    registry = ReviewerSessionRegistry(tmp_path / "reviewer-sessions.json")
    registry.save(prior_session(initial, "sha-a"))
    validation_context = context()
    validation_context.issue_context = "Linked Issue requires independent server validation."
    claimed = ClaimedReviewThread(
        thread_id="thread-gap",
        root_comment_database_id=42,
        root_author_login="auto-coder[bot]",
        original_finding=(f"### Auto-Coder material test-oracle gap\n\nGap identity: `{initial.gap_id}`\n\n" f"**Issue requirement**\n\n`{initial.requirement_id}`: {context().issue_requirements[0].text}"),
        discussion="agent[bot]: Added the direct-boundary regression test.",
    )
    manager = MagicMock()
    manager.get_current_backend_identity.return_value = ("reviewer", "codex", "strong")
    manager._last_session_id = "session-1"
    manager.continue_session.return_value = json.dumps(
        {
            "result": "PASS",
            "summary": "The current head and requested regression are verified.",
            "requirement_coverage": [{"requirement_id": "REQ-001", "status": "VERIFIED", "evidence": "Current-head implementation and test verified."}],
            "findings": [],
            "test_oracle_gaps": [],
            "thread_dispositions": [
                {
                    "thread_id": "thread-gap",
                    "status": "ADDRESSED",
                    "rationale": "The requested invariant has direct committed coverage.",
                    "evidence": "tests/test_grid.py exercises GridMutation and unchanged persistence.",
                }
            ],
        }
    )
    client = MagicMock()
    client.get_pull_request_head_sha_strict.return_value = "sha-a"
    client.resolve_review_thread.return_value = True

    with (
        patch("auto_coder.adversarial_validator.build_adversarial_validation_context", return_value=validation_context),
        patch("pathlib.Path.write_text", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        failed_result = run_adversarial_validation(
            "owner/repo",
            {"number": 1, "head": {"sha": "sha-a"}},
            AutomationConfig(),
            backend_manager=manager,
            session_registry=registry,
            claimed_review_threads_section=render_claimed_review_threads_section((claimed,)),
            claimed_review_threads=(claimed,),
        )
        resolve_addressed_review_threads(client, "owner/repo", 1, "sha-a", (claimed,), failed_result.thread_dispositions)

    client.resolve_review_thread.assert_not_called()
    still_open = registry.get("owner/repo", 1, "reviewer", "codex", "strong")
    assert still_open is not None
    assert still_open.test_oracle_gaps[0].status == "OPEN"

    with patch("auto_coder.adversarial_validator.build_adversarial_validation_context", return_value=validation_context):
        recovered = run_adversarial_validation(
            "owner/repo",
            {"number": 1, "head": {"sha": "sha-a"}},
            AutomationConfig(),
            backend_manager=manager,
            session_registry=registry,
            claimed_review_threads_section=render_claimed_review_threads_section((claimed,)),
            claimed_review_threads=(claimed,),
        )
    resolved = resolve_addressed_review_threads(
        client,
        "owner/repo",
        1,
        "sha-a",
        (claimed,),
        recovered.thread_dispositions,
        stale_registry=StaleReviewThreadRegistry(tmp_path / "stale-retry.json"),
    )

    assert recovered.result == "PASS"
    assert resolved == ["thread-gap"]
    durable = registry.get("owner/repo", 1, "reviewer", "codex", "strong")
    assert durable is not None
    assert durable.test_oracle_gaps[0].status == "RESOLVED"


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
