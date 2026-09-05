import json

import pytest

from auto_coder.decomposition_analyzer import (
    DecompositionIssue,
    analyze_issue_decomposition,
    parse_decomposition_analysis_response,
)
from auto_coder.requirement_contract import build_normative_issue_manifest


def _issue(number, title, requirement, body_evidence="Context evidence"):
    body = f"## Requirements\nREQ-001: {requirement}\n\n## Context\n{body_evidence}"
    return DecompositionIssue(build_normative_issue_manifest(number, title, body), body)


def _set():
    return (
        _issue(1730, "Persistent workflow", "The completed workflow survives restart."),
        (
            _issue(1729, "Store workflow", "Persist each completed workflow using its stable identity."),
            _issue(1731, "Read workflow", "Restore a persisted workflow using the same stable identity."),
        ),
    )


def _false_success_set():
    return (
        _issue(100, "Restart-readable workflows", "Keep completed workflows readable after restart."),
        (
            _issue(101, "Encrypted retention", "Retain each workflow encrypted with a fresh process key."),
            _issue(102, "Workflow reader", "Read retained workflows using the current process key."),
        ),
    )


def _finding(category="missing_requirement_ownership", issue_number=1730, requirement_ids=None):
    if requirement_ids is None:
        requirement_ids = ["REQ-001"]
    return {
        "category": category,
        "affected_issues": [{"issue_number": issue_number, "requirement_ids": requirement_ids}],
        "explanation": "The parent persistence guarantee has no child owner.",
        "clarification": "Add explicit persistence ownership to a child Requirement.",
    }


def _response(verdict="READY", findings=None):
    return json.dumps({"verdict": verdict, "findings": findings or []})


def test_authoritative_manifest_origin_reaches_complete_set_analysis_unchanged():
    parent, children = _set()
    captured = []
    result = analyze_issue_decomposition(parent, children, prompt_runner=lambda prompt: captured.append(prompt) or _response())

    assert result.verdict == "READY"
    assert result.is_ready is True
    assert '"issue_number": 1730' in captured[0]
    assert '"issue_number": 1729' in captured[0]
    assert '"issue_number": 1731' in captured[0]
    assert '"requirement_id": "REQ-001"' in captured[0]
    assert "Context evidence" in captured[0]
    assert "sole authoritative normative Requirements" in captured[0]


def test_supplied_manifest_remains_authoritative_when_body_evidence_disagrees():
    parent, _ = _set()
    authoritative_body = "## Requirements\nREQ-002: Preserve the supplied stable identity."
    child_manifest = build_normative_issue_manifest(1729, "Store workflow", authoritative_body)
    conflicting_body = "## Requirements\nREQ-099: Replace the identity after every restart."
    child = DecompositionIssue(child_manifest, conflicting_body)
    captured = []

    result = analyze_issue_decomposition(
        parent,
        (child,),
        prompt_runner=lambda prompt: captured.append(prompt) or _response(),
    )

    assert result.verdict == "READY"
    assert '"requirement_id": "REQ-002"' in captured[0]
    assert '"text": "Preserve the supplied stable identity."' in captured[0]
    assert '"requirement_id": "REQ-099"' not in captured[0]
    assert "REQ-099: Replace the identity after every restart." in captured[0]


@pytest.mark.parametrize(
    "category",
    [
        "missing_requirement_ownership",
        "cross_issue_contradiction",
        "unstated_cross_issue_dependency",
        "boundary_semantics_conflict",
        "decomposition_false_success",
    ],
)
def test_every_stable_blocked_category_is_preserved(category):
    parent, children = _set()
    result = parse_decomposition_analysis_response(_response("BLOCKED", [_finding(category)]), parent, children)
    assert result.verdict == "BLOCKED"
    assert result.is_ready is False
    assert result.findings[0].category == category
    assert result.findings[0].affected_issues[0].issue_number == 1730
    assert result.findings[0].affected_issues[0].requirement_ids == ("REQ-001",)


@pytest.mark.parametrize(
    "response",
    [
        "The set is READY.",
        '{"findings":[]}',
        '{"verdict":"READY","findings":[],"summary":"ready"}',
        _response("READY", [_finding()]),
        _response("BLOCKED"),
        _response("BLOCKED", [_finding("unknown")]),
        _response("BLOCKED", [_finding(issue_number=9999)]),
        _response("BLOCKED", [_finding(requirement_ids=["REQ-999"])]),
        _response("ERROR", [_finding()]),
        '{"verdict":"ERROR","verdict":"READY","findings":[]}',
    ],
)
def test_invalid_partial_contradictory_or_membership_inconsistent_output_is_error(response):
    parent, children = _set()
    result = parse_decomposition_analysis_response(response, parent, children)
    assert result.verdict == "ERROR"
    assert result.is_ready is False
    assert result.findings == ()
    assert result.error


@pytest.mark.parametrize(
    ("category", "finding"),
    [
        ("missing_requirement_ownership", _finding(requirement_ids=[])),
        ("missing_requirement_ownership", _finding(issue_number=1729)),
        (
            "decomposition_false_success",
            _finding(
                category="decomposition_false_success",
                issue_number=100,
                requirement_ids=[],
            ),
        ),
        (
            "decomposition_false_success",
            _finding(category="decomposition_false_success", issue_number=101),
        ),
    ],
)
def test_incomplete_parent_requirement_reference_fails_closed_through_public_operation(category, finding):
    if category == "decomposition_false_success":
        parent, children = _false_success_set()
    else:
        parent, children = _set()

    result = analyze_issue_decomposition(
        parent,
        children,
        prompt_runner=lambda _prompt: _response("BLOCKED", [finding]),
    )

    assert result.verdict == "ERROR"
    assert result.is_ready is False
    assert result.findings == ()
    assert result.error == f"{category} finding must identify the parent and an applicable Requirement"


def test_invalid_child_manifest_fails_before_provider_execution():
    parent, children = _set()
    invalid = DecompositionIssue(build_normative_issue_manifest(1732, "Invalid", "## Context\nNo contract"), "raw")
    invoked = []
    result = analyze_issue_decomposition(parent, (*children, invalid), prompt_runner=lambda prompt: invoked.append(prompt) or _response())
    assert result.verdict == "ERROR"
    assert result.error == "Issue #1732 requires a valid explicit normative Requirement manifest"
    assert invoked == []


def test_duplicate_membership_fails_closed_without_provider():
    parent, children = _set()
    invoked = []
    result = analyze_issue_decomposition(parent, (children[0], children[0]), prompt_runner=lambda prompt: invoked.append(prompt) or _response())
    assert result.verdict == "ERROR"
    assert result.error == "Decomposition membership contains duplicate Issue identities"
    assert invoked == []


def test_provider_error_cannot_become_a_semantic_decision():
    parent, children = _set()

    def fail(_prompt):
        raise TimeoutError("unavailable")

    result = analyze_issue_decomposition(parent, children, prompt_runner=fail)
    assert result.verdict == "ERROR"
    assert result.findings == ()
    assert result.error == "Decomposition analysis execution failed: TimeoutError"


def test_parent_implementation_ownership_premise_is_explicit_in_prompt():
    parent, children = _set()
    captured = []
    analyze_issue_decomposition(
        parent,
        children,
        parent_implemented_independently=True,
        prompt_runner=lambda prompt: captured.append(prompt) or _response(),
    )
    assert "Parent independently implemented: true" in captured[0]
