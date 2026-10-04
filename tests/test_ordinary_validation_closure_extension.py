import json
from dataclasses import replace

import pytest

from auto_coder.adversarial_validator import parse_adversarial_validation_response
from auto_coder.pr_review_cycle import ContractSnapshot, Finding, StrongPolicyIdentity
from auto_coder.pr_review_execution import ReviewExecutionInput, ReviewMode, ScopeAssessment


def _closure_input() -> ReviewExecutionInput:
    finding = Finding(
        finding_id="finding-1",
        origin_round_id="round-1",
        requirement_ids=("#2405/REQ-003",),
        requirement_texts=("REQ-003: Assess every supplied finding.",),
        counterexample="The old head omits a required disposition.",
        expected_behavior="Every accepted finding is assessed.",
        actual_behavior="The finding is omitted.",
        evidence="The ordinary response has no disposition.",
        affected_boundary="ordinary validator response",
        material_consequence="Closure could be granted without assessing a blocker.",
        focused_regression_scenario="Parse a response carrying the complete disposition.",
    )
    return ReviewExecutionInput(
        mode=ReviewMode.ORDINARY_CLOSURE,
        round_id="round-1",
        attempt_id="7:3",
        head_sha="2" * 40,
        base_sha="b" * 40,
        contract=ContractSnapshot(("#2405",), "REQ-003: Assess every supplied finding."),
        policy=StrongPolicyIdentity("strong", "model", "v1"),
        repository_evidence="Current source and tests.",
        diff_evidence="Complete H0-to-H2 diff.",
        repository="owner/repo",
        pr_number=2405,
        open_epoch=7,
        attempt_sequence=3,
        finding_set_revision=4,
        findings=(finding,),
        audited_head_sha="0" * 40,
    )


def _ordinary_payload() -> dict[str, object]:
    return {
        "result": "PASS",
        "summary": "Ordinary requirements are satisfied.",
        "findings": [],
        "closure_assessment": {
            "result": "PASS",
            "findings": [],
            "dispositions": [
                {
                    "finding_id": "finding-1",
                    "status": "FIXED",
                    "evidence": "The current production boundary now retains every disposition.",
                }
            ],
            "scope": "BOUNDED",
            "scope_evidence": "The complete cumulative diff contains only the correction and regression test.",
        },
    }


def test_ordinary_response_retains_bound_non_authorizing_closure_assessment() -> None:
    result = parse_adversarial_validation_response(
        json.dumps(_ordinary_payload()),
        closure_input=_closure_input(),
        reviewer_provenance="codex/model",
    )

    assert result.result == "PASS"
    assert result.closure_assessment_diagnostic == ""
    assert result.closure_assessment is not None
    assert result.closure_assessment.attempt_id == "7:3"
    assert result.closure_assessment.audited_head_sha == "0" * 40
    assert result.closure_assessment.repository == "owner/repo"
    assert result.closure_assessment.pr_number == 2405
    assert result.closure_assessment.open_epoch == 7
    assert result.closure_assessment.attempt_sequence == 3
    assert result.closure_assessment.reviewer_provenance == "codex/model"
    assert result.closure_assessment.scope is ScopeAssessment.BOUNDED
    assert [item.finding_id for item in result.closure_assessment.dispositions] == ["finding-1"]


def test_invalid_closure_extension_preserves_valid_ordinary_result() -> None:
    payload = _ordinary_payload()
    assessment = payload["closure_assessment"]
    assert isinstance(assessment, dict)
    assessment["head_sha"] = "wrong-head"

    result = parse_adversarial_validation_response(
        json.dumps(payload),
        closure_input=_closure_input(),
        reviewer_provenance="claude/model",
    )

    assert result.result == "PASS"
    assert result.closure_assessment is None
    assert result.closure_assessment_diagnostic == "Closure assessment contradicts controller-owned head_sha"


def test_contradictory_audited_head_is_non_authorizing() -> None:
    payload = _ordinary_payload()
    assessment = payload["closure_assessment"]
    assert isinstance(assessment, dict)
    assessment["audited_head_sha"] = "f" * 40

    result = parse_adversarial_validation_response(
        json.dumps(payload),
        closure_input=_closure_input(),
        reviewer_provenance="codex/model",
    )

    assert result.result == "PASS"
    assert result.closure_assessment is None
    assert result.closure_assessment_diagnostic == "Closure assessment contradicts controller-owned audited_head_sha"


def test_omitted_audited_head_is_bound_from_controller_context() -> None:
    result = parse_adversarial_validation_response(
        json.dumps(_ordinary_payload()),
        closure_input=_closure_input(),
        reviewer_provenance="codex/model",
    )

    assert result.closure_assessment is not None
    assert result.closure_assessment.is_complete
    assert result.closure_assessment.grants_closure_evidence


def test_distinct_captured_contexts_remain_distinguishable_in_results() -> None:
    first_input = _closure_input()
    second_input = replace(first_input, audited_head_sha="1" * 40, open_epoch=8, attempt_sequence=4)

    first = parse_adversarial_validation_response(json.dumps(_ordinary_payload()), closure_input=first_input)
    second = parse_adversarial_validation_response(json.dumps(_ordinary_payload()), closure_input=second_input)

    assert first.closure_assessment is not None
    assert second.closure_assessment is not None
    assert first.closure_assessment.audited_head_sha == "0" * 40
    assert second.closure_assessment.audited_head_sha == "1" * 40
    assert first.closure_assessment.open_epoch == 7
    assert second.closure_assessment.open_epoch == 8
    assert first.closure_assessment != second.closure_assessment


def _ordinary_json_with_duplicate_identity(identity: str, first: object, second: object) -> str:
    assessment = _ordinary_payload()["closure_assessment"]
    assert isinstance(assessment, dict)
    members = json.dumps(assessment)[1:-1]
    return '{"result":"PASS","summary":"ordinary remains valid","findings":[],' f'"closure_assessment":{{"{identity}":{json.dumps(first)},"{identity}":{json.dumps(second)},{members}}}}}'


@pytest.mark.parametrize(
    ("identity", "first", "second"),
    [
        ("head_sha", "wrong", "2" * 40),
        ("head_sha", "2" * 40, "wrong"),
        ("attempt_id", "wrong", "7:3"),
        ("attempt_id", "7:3", "wrong"),
    ],
)
def test_duplicate_closure_identity_is_non_authorizing(identity: str, first: object, second: object) -> None:
    result = parse_adversarial_validation_response(
        _ordinary_json_with_duplicate_identity(identity, first, second),
        closure_input=_closure_input(),
    )

    assert result.result == "PASS"
    assert result.closure_assessment is None
    assert result.closure_assessment_diagnostic == f"Closure assessment contains duplicate JSON members: {identity}"


@pytest.mark.parametrize(
    "closure_input",
    [
        replace(_closure_input(), repository=""),
        replace(_closure_input(), pr_number=0),
        replace(_closure_input(), open_epoch=-1),
        replace(_closure_input(), attempt_sequence=0),
        replace(_closure_input(), audited_head_sha=""),
        replace(_closure_input(), repository_evidence=""),
        replace(_closure_input(), diff_evidence=""),
    ],
)
def test_incomplete_closure_context_is_explicitly_unavailable(closure_input: ReviewExecutionInput) -> None:
    result = parse_adversarial_validation_response(json.dumps(_ordinary_payload()), closure_input=closure_input)

    assert result.result == "PASS"
    assert result.closure_assessment is None
    assert result.closure_assessment_diagnostic.startswith("Closure context unavailable:")


@pytest.mark.parametrize("malformed_verdict", [[], {"value": "PASS"}])
@pytest.mark.parametrize("ordinary_result", ["PASS", "NEEDS_FIX"])
def test_malformed_closure_verdict_preserves_ordinary_result(malformed_verdict: object, ordinary_result: str) -> None:
    payload = _ordinary_payload()
    payload["result"] = ordinary_result
    if ordinary_result == "NEEDS_FIX":
        payload["findings"] = [
            {
                "requirement_id": "#2405/REQ-005",
                "finding_identity": "closure-binding-loss",
                "correction_identity": "retain-controller-context",
                "violated_requirement": "Retain captured context",
                "requirement_text": "REQ-005: Bind closure assessments to captured context.",
                "reachability": "The ordinary parser returns the assessment.",
                "required_behavior": "Captured context remains distinguishable.",
                "actual_behavior": "Captured context is discarded.",
                "evidence": "The returned result omits the context.",
                "evidence_classification": "DEMONSTRATED",
                "anchor_path": "src/auto_coder/pr_review_execution.py",
                "anchor_line": 70,
                "counterexample": "Given two contexts, when parsed, then the results are indistinguishable while tests only inspect H2.",
                "test_gap": "No context-retention assertion.",
                "suggested_regression_scenario": "Compare results from two contexts.",
            }
        ]
    assessment = payload["closure_assessment"]
    assert isinstance(assessment, dict)
    assessment["result"] = malformed_verdict

    result = parse_adversarial_validation_response(json.dumps(payload), closure_input=_closure_input())

    assert result.result == ordinary_result
    assert result.summary == "Ordinary requirements are satisfied."
    assert len(result.findings) == (0 if ordinary_result == "PASS" else 1)
    assert result.closure_assessment is not None
    assert not result.closure_assessment.is_complete
    assert not result.closure_assessment.grants_closure_evidence
    assert result.closure_assessment_diagnostic == "Invalid result"


def test_missing_or_inconclusive_assessment_never_grants_closure_evidence() -> None:
    missing = parse_adversarial_validation_response(
        json.dumps({"result": "PASS", "summary": "valid ordinary result", "findings": []}),
        closure_input=_closure_input(),
    )
    payload = _ordinary_payload()
    assessment = payload["closure_assessment"]
    assert isinstance(assessment, dict)
    dispositions = assessment["dispositions"]
    assert isinstance(dispositions, list) and isinstance(dispositions[0], dict)
    dispositions[0]["status"] = "INCONCLUSIVE"
    assessment["result"] = "INCONCLUSIVE"
    inconclusive = parse_adversarial_validation_response(json.dumps(payload), closure_input=_closure_input())

    assert missing.result == "PASS"
    assert missing.closure_assessment is None
    assert missing.closure_assessment_diagnostic == "Closure assessment is absent or malformed"
    assert inconclusive.closure_assessment is not None
    assert inconclusive.closure_assessment.is_complete
    assert not inconclusive.closure_assessment.grants_closure_evidence


def test_first_open_epoch_zero_is_valid_closure_context() -> None:
    """The review cycle's first open epoch is 0; it must not make production closure context unavailable."""
    result = parse_adversarial_validation_response(json.dumps(_ordinary_payload()), closure_input=replace(_closure_input(), open_epoch=0))

    assert result.closure_assessment_diagnostic == ""
    assert result.closure_assessment is not None and result.closure_assessment.open_epoch == 0


def test_closure_prompt_uses_one_result_envelope_and_exact_requirement_ids() -> None:
    from auto_coder.adversarial_validator import IssueRequirement, _closure_prompt_extension

    prompt = _closure_prompt_extension(_closure_input(), [IssueRequirement(requirement_id="#2405/REQ-003", text="Assess every supplied finding.")])
    assert '"closure_assessment": {' in prompt
    assert '"result": "PASS"' in prompt
    assert '"verdict"' not in prompt
    assert "copy every value exactly into the nested `closure_assessment` object" in prompt
    assert '["#2405/REQ-003"]' in prompt
    result = parse_adversarial_validation_response(json.dumps(_ordinary_payload()), closure_input=_closure_input())
    assert result.result == "PASS"
    assert result.closure_assessment is not None
    assert result.closure_assessment.grants_closure_evidence


def test_nested_obsolete_verdict_cannot_authorize_closure() -> None:
    payload = _ordinary_payload()
    assessment = payload["closure_assessment"]
    assert isinstance(assessment, dict)
    assessment["verdict"] = assessment.pop("result")
    result = parse_adversarial_validation_response(json.dumps(payload), closure_input=_closure_input())
    assert result.result == "PASS"
    assert result.closure_assessment_diagnostic == "Invalid result"
    assert result.closure_assessment is not None and not result.closure_assessment.grants_closure_evidence
