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


def gap() -> SpecificationGap:
    return SpecificationGap(
        question="Should oversized reviews fetch every patch?",
        why_existing_issue_is_insufficient="The Issue defines no oversized-review policy.",
        observed_case="The PR contains 301 changed files.",
        affected_scope="Review completeness and merge eligibility.",
        candidate_options=["Fetch all patches", "Require human review"],
    )


def test_parser_preserves_gap_orthogonally_to_pass() -> None:
    response = json.dumps(
        {
            "result": "PASS",
            "summary": "Every defined requirement passes.",
            "requirement_coverage": [],
            "findings": [],
            "specification_gaps": [gap().__dict__],
        }
    )

    result = parse_adversarial_validation_response(response)

    assert result.result == "PASS"
    assert result.is_pass is True
    assert result.allows_auto_merge is False
    assert result.specification_gaps == [gap()]


def test_comment_distinguishes_gap_and_disables_only_automatic_merge() -> None:
    result = AdversarialValidationResult(result="PASS", summary="Defined requirements pass.", specification_gaps=[gap()])

    comment = format_adversarial_validation_comment(result, "abc123")

    assert "### Specification gaps (1)" in comment
    assert "not proven implementation defects" in comment
    assert "Auto-Coder did not choose one" in comment
    assert "Review of all defined requirements continued" in comment
    assert "Automatic merge is disabled" in comment
    assert "human may still merge manually" in comment
    assert "Neutral candidate options (not requirements)" in comment


def test_saved_gap_pass_remains_blocked_from_automatic_merge() -> None:
    client = MagicMock()
    client.get_pr_comments.return_value = [{"body": format_adversarial_validation_comment(AdversarialValidationResult(result="PASS", specification_gaps=[gap()]), "abc123")}]

    status, error = _get_published_adversarial_validation_status(client, "owner/repo", 12, "abc123")

    assert status == "PASS_WITH_SPECIFICATION_GAPS"
    assert error is None


def test_native_review_does_not_approve_gap_pass() -> None:
    result = AdversarialValidationResult(result="PASS", specification_gaps=[gap()])

    assert result.allows_auto_merge is False
    assert "Auto-Coder did not choose one" in format_adversarial_validation_comment(result, "abc123")
