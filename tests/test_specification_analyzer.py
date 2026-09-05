import json

import pytest

from auto_coder.requirement_contract import build_normative_issue_manifest
from auto_coder.specification_analyzer import analyze_issue_specification, parse_specification_analysis_response


def _manifest():
    return build_normative_issue_manifest(
        1727,
        "Independent preview contract",
        "## Requirements\nREQ-001: Preview renders the selected draft.\nREQ-002: The result is JSON.\n\n## Context\nPreview must not alter live state.",
    )


def _response(verdict="READY", findings=None):
    return json.dumps({"verdict": verdict, "findings": findings or []})


def _finding(category="hidden_requirement", requirement_ids=None):
    return {
        "category": category,
        "requirement_ids": requirement_ids or [],
        "explanation": "Live-state immutability is mandatory only in Context.",
        "clarification": "Add an explicit Requirement forbidding live-state writes.",
        "counterexample": "",
        "missing_normative_boundary": "",
    }


def test_supported_manifest_origin_reaches_analysis_without_reparsing_body():
    manifest = _manifest()
    captured = []

    def runner(prompt):
        captured.append(prompt)
        return _response("BLOCKED", [_finding()])

    result = analyze_issue_specification(
        manifest,
        "## Requirements\nREQ-999: This conflicting raw Markdown must not enter the manifest.",
        parent_context="REQ-888: Parent behavior is context only.",
        prompt_runner=runner,
    )

    assert result.verdict == "BLOCKED"
    assert result.findings[0].category == "hidden_requirement"
    assert '"requirement_id": "REQ-001"' in captured[0]
    assert '"requirement_id": "REQ-002"' in captured[0]
    assert '"requirement_id": "REQ-999"' not in captured[0]
    assert "REQ-999: This conflicting raw Markdown" in captured[0]
    assert "Parent Issue context (non-normative evidence only)" in captured[0]


def test_ready_preserves_implementation_freedom_and_missing_examples():
    result = analyze_issue_specification(_manifest(), "No architecture or examples supplied.", prompt_runner=lambda _prompt: _response())
    assert result.verdict == "READY"
    assert result.is_ready is True
    assert result.findings == ()
    assert result.error is None


def test_false_success_requires_written_counterexample_and_boundary():
    finding = _finding("false_success_gap", ["REQ-001"])
    finding["counterexample"] = "Ordinary --only also bypasses capacity and still satisfies the stated rule."
    finding["missing_normative_boundary"] = "The contract does not forbid bypass without both flags."
    result = parse_specification_analysis_response(_response("BLOCKED", [finding]), _manifest())
    assert result.verdict == "BLOCKED"
    assert result.findings[0].requirement_ids == ("REQ-001",)
    assert result.findings[0].counterexample.startswith("Ordinary --only")


@pytest.mark.parametrize(
    "response",
    [
        "The issue is READY.",
        '{"findings": []}',
        '{"verdict": "READY", "findings": [], "summary": "ready"}',
        _response("READY", [_finding()]),
        _response("BLOCKED", []),
        _response("BLOCKED", [_finding("unknown")]),
        _response("BLOCKED", [_finding(requirement_ids=["REQ-999"])]),
        _response("ERROR", [_finding()]),
        _response("false", []),
    ],
)
def test_invalid_or_contradictory_model_output_fails_closed(response):
    result = parse_specification_analysis_response(response, _manifest())
    assert result.verdict == "ERROR"
    assert result.is_ready is False
    assert result.findings == ()
    assert result.error


def test_false_success_without_required_evidence_fails_closed():
    result = parse_specification_analysis_response(_response("BLOCKED", [_finding("false_success_gap", ["REQ-001"])]), _manifest())
    assert result.verdict == "ERROR"
    assert result.findings == ()


def test_invalid_authoritative_manifest_never_invokes_provider():
    manifest = build_normative_issue_manifest(8, "Bad", "## Requirements\nREQ-001: First\nREQ-001: Duplicate")
    invoked = []
    result = analyze_issue_specification(manifest, "body", prompt_runner=lambda prompt: invoked.append(prompt) or _response())
    assert result.verdict == "ERROR"
    assert "duplicate IDs" in (result.error or "")
    assert invoked == []


def test_provider_failure_returns_error_not_a_policy_verdict():
    def failing_runner(_prompt):
        raise RuntimeError("provider unavailable")

    result = analyze_issue_specification(_manifest(), "body", prompt_runner=failing_runner)
    assert result.verdict == "ERROR"
    assert result.findings == ()
    assert result.error == "Specification analysis execution failed: RuntimeError"


@pytest.mark.parametrize("invalid_verdict", [[], {}])
def test_public_operation_fails_closed_for_unhashable_verdict(invalid_verdict):
    response = json.dumps({"verdict": invalid_verdict, "findings": []})
    result = analyze_issue_specification(_manifest(), "body", prompt_runner=lambda _prompt: response)
    assert result.verdict == "ERROR"
    assert result.findings == ()
    assert result.is_ready is False


@pytest.mark.parametrize("invalid_category", [[], {}])
def test_public_operation_fails_closed_for_unhashable_category(invalid_category):
    finding = _finding()
    finding["category"] = invalid_category
    response = _response("BLOCKED", [finding])
    result = analyze_issue_specification(_manifest(), "body", prompt_runner=lambda _prompt: response)
    assert result.verdict == "ERROR"
    assert result.findings == ()
    assert result.is_ready is False


@pytest.mark.parametrize(
    "response",
    [
        '{"verdict":"ERROR","verdict":"READY","findings":[]}',
        '{"verdict":"READY","findings":[{"category":"hidden_requirement","requirement_ids":[],"explanation":"gap","clarification":"clarify","counterexample":"","missing_normative_boundary":""}],"findings":[]}',
    ],
)
def test_public_operation_rejects_conflicting_duplicate_json_members(response):
    result = analyze_issue_specification(_manifest(), "body", prompt_runner=lambda _prompt: response)
    assert result.verdict == "ERROR"
    assert result.findings == ()
    assert result.is_ready is False
