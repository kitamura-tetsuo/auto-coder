import json

from auto_coder.pr_review_cycle import ContractSnapshot, Finding, StrongPolicyIdentity
from auto_coder.pr_review_execution import ReviewExecutionInput, ReviewMode, ScopeAssessment, build_review_prompt, parse_review_result


def _input(mode: ReviewMode = ReviewMode.STRONG_AUDIT) -> ReviewExecutionInput:
    finding = Finding(
        finding_id="finding-a",
        origin_round_id="round-a",
        requirement_ids=("#2101/REQ-007",),
        requirement_texts=("Verify every outstanding finding.",),
        counterexample="The second supported path remains unprotected.",
        expected_behavior="Both supported paths preserve the invariant.",
        actual_behavior="One path loses the state.",
        evidence="src/state.py:40 reaches delete_two.",
        affected_boundary="delete_two",
        material_consequence="Persisted state is lost.",
        focused_regression_scenario="Exercise both deletion paths.",
    )
    return ReviewExecutionInput(
        mode=mode,
        round_id="round-a",
        attempt_id="attempt-1",
        head_sha="a" * 40,
        base_sha="b" * 40,
        contract=ContractSnapshot(("#2101",), "REQ-007: Verify every outstanding finding."),
        policy=StrongPolicyIdentity("strong-route", '{"model":"strong"}', "v1"),
        repository_evidence="current source and tests",
        diff_evidence="complete cumulative diff",
        finding_set_revision=3 if mode is ReviewMode.ORDINARY_CLOSURE else 0,
        findings=(finding,) if mode is ReviewMode.ORDINARY_CLOSURE else (),
        audited_head_sha="c" * 40,
    )


def _identity(expected: ReviewExecutionInput) -> dict:
    return {
        "mode": expected.mode.value,
        "round_id": expected.round_id,
        "attempt_id": expected.attempt_id,
        "head_sha": expected.head_sha,
        "base_sha": expected.base_sha,
        "contract_identity": expected.contract.identity,
        "policy_identity": expected.policy.identity,
        "finding_set_revision": expected.finding_set_revision,
    }


def test_strong_prompt_is_independent_and_role_tagged() -> None:
    prompt = build_review_prompt(_input())
    assert "STRONG_AUDIT" in prompt
    assert "current source and tests" in prompt
    assert "Accepted portable findings (empty for independent STRONG_AUDIT):\n[]" in prompt
    assert "continue a prior conversation" in prompt


def test_strong_parser_preserves_portable_finding_fields() -> None:
    expected = _input()
    payload = {
        **_identity(expected),
        "verdict": "FINDINGS",
        "findings": [
            {
                "finding_id": "stable-a",
                "requirement_ids": ["#2101/REQ-007"],
                "requirement_texts": ["Verify every outstanding finding."],
                "counterexample": "Given both paths, the second remains broken.",
                "expected_behavior": "Both paths preserve state.",
                "actual_behavior": "The second loses state.",
                "evidence": "src/state.py:40",
                "affected_boundary": "delete_two",
                "is_regression_gap": True,
                "plausible_incorrect_implementation": "Guard only delete_one.",
                "why_tests_admit_it": "Tests exercise delete_one only.",
                "material_consequence": "Persisted state is lost.",
                "focused_regression_scenario": "Exercise delete_two.",
            }
        ],
    }
    result = parse_review_result(json.dumps(payload), expected, "strong/codex/model")
    assert result.is_complete
    assert result.findings[0].finding_id == "stable-a"
    assert result.findings[0].why_tests_admit_it == "Tests exercise delete_one only."
    assert result.reviewer_provenance == "strong/codex/model"


def test_closure_rejects_missing_disposition_and_identity_mismatch() -> None:
    expected = _input(ReviewMode.ORDINARY_CLOSURE)
    missing = {**_identity(expected), "verdict": "PASS", "findings": [], "dispositions": [], "scope": "BOUNDED", "scope_evidence": "Only the guard and regression changed."}
    assert "Every accepted finding" in parse_review_result(json.dumps(missing), expected, "ordinary/model").diagnostic
    missing["head_sha"] = "d" * 40
    assert parse_review_result(json.dumps(missing), expected, "ordinary/model").diagnostic == "Mismatched or missing head_sha"


def test_closure_pass_requires_fixed_or_invalid_and_bounded_scope() -> None:
    expected = _input(ReviewMode.ORDINARY_CLOSURE)
    payload = {
        **_identity(expected),
        "verdict": "PASS",
        "findings": [],
        "dispositions": [{"finding_id": "finding-a", "status": "FIXED", "evidence": "Both production paths now enforce the invariant."}],
        "scope": "BOUNDED",
        "scope_evidence": "The full audited-head-to-current-head diff contains only the two-path guard and its regression.",
    }
    result = parse_review_result(json.dumps(payload), expected, "ordinary/codex/model")
    assert result.grants_closure_evidence
    assert result.scope is ScopeAssessment.BOUNDED
    assert result.dispositions[0].status == "FIXED"


def test_closure_unknown_scope_never_normalizes_to_pass() -> None:
    expected = _input(ReviewMode.ORDINARY_CLOSURE)
    payload = {
        **_identity(expected),
        "verdict": "PASS",
        "findings": [],
        "dispositions": [{"finding_id": "finding-a", "status": "FIXED", "evidence": "The named path is corrected."}],
        "scope": "UNKNOWN",
        "scope_evidence": "Source evidence exists but semantic impact is unclear.",
    }
    result = parse_review_result(json.dumps(payload), expected, "ordinary/codex/model")
    assert not result.is_complete
    assert not result.grants_closure_evidence
    assert "scope" in result.diagnostic
