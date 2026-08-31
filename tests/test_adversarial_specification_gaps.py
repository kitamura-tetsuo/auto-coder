"""Specification-gap behavior in adversarial validation."""

import json
from unittest.mock import MagicMock

from auto_coder.adversarial_validator import (
    AdversarialValidationResult,
    SpecificationGap,
    format_adversarial_validation_comment,
    parse_adversarial_validation_response,
)
from auto_coder.pr_processor import _get_published_adversarial_validation_status


def gap_payload() -> dict[str, object]:
    return {
        "question": "Should oversized changes receive a complete review?",
        "why_existing_issue_is_insufficient": "The Issue defines no oversized-change policy.",
        "observed_case": "The PR changes more files than raw diff retrieval returns.",
        "affected_scope": "Review completeness and automatic merge.",
        "candidate_options": ["Perform a complete review", "Require human review"],
    }


def test_gap_coexists_with_finding_and_needs_fix_precedence() -> None:
    response = {
        "result": "PASS",
        "summary": "Defined requirements reviewed.",
        "requirement_coverage": [],
        "findings": [
            {
                "requirement_id": "REQ-001",
                "violated_requirement": "R1",
                "evidence_classification": "DEMONSTRATED",
                "reachability": "Call the public entry point.",
                "required_behavior": "Return R.",
                "actual_behavior": "Returns X.",
                "evidence": "The branch returns X.",
                "counterexample": "Given S, when A, R is required, but X occurs and tests miss it.",
            }
        ],
        "specification_gaps": [gap_payload()],
    }

    result = parse_adversarial_validation_response(json.dumps(response))

    assert result.result == "NEEDS_FIX"
    assert len(result.findings) == 1
    assert result.specification_gaps == [SpecificationGap(**gap_payload())]
    assert not result.auto_merge_allowed


def test_pass_with_gap_is_not_automatic_merge_eligible_and_is_persisted() -> None:
    result = AdversarialValidationResult(
        result="PASS",
        summary="All defined requirements pass.",
        specification_gaps=[SpecificationGap(**gap_payload())],
    )
    comment = format_adversarial_validation_comment(result, "abc123")
    client = MagicMock()
    client.get_pr_comments.return_value = [{"body": comment}]

    status, error = _get_published_adversarial_validation_status(client, "owner/repo", 42, "abc123")

    assert result.is_pass
    assert not result.auto_merge_allowed
    assert status == "PASS_WITH_SPECIFICATION_GAPS"
    assert error is None
    assert "not proven implementation defects" in comment
    assert "Auto-Coder did not choose one" in comment
    assert "automatic merge is disabled" in comment
    assert "human may still merge manually" in comment


def test_malformed_gap_fails_closed_without_becoming_a_finding() -> None:
    response = {
        "result": "PASS",
        "summary": "Done",
        "requirement_coverage": [],
        "findings": [],
        "specification_gaps": [{"question": "Missing required fields"}],
    }

    result = parse_adversarial_validation_response(json.dumps(response))

    assert result.result == "ERROR"
    assert result.findings == []
    assert result.diagnostic_category == "schema_error"
