import json

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
            "verdict": "PASS",
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
    assessment["verdict"] = "INCONCLUSIVE"
    inconclusive = parse_adversarial_validation_response(json.dumps(payload), closure_input=_closure_input())

    assert missing.result == "PASS"
    assert missing.closure_assessment is None
    assert missing.closure_assessment_diagnostic == "Closure assessment is absent or malformed"
    assert inconclusive.closure_assessment is not None
    assert inconclusive.closure_assessment.is_complete
    assert not inconclusive.closure_assessment.grants_closure_evidence
