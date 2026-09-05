"""Tests for the shared, provider-independent Issue specification boundary."""

from auto_coder.adversarial_validator import build_issue_requirement_manifest
from auto_coder.issue_context import IssueOracleResolution, VerifiedIssueOracle
from auto_coder.issue_specification import parse_issue_specification
from auto_coder.requirement_contract import parse_requirement_contract


def test_only_requirements_section_creates_normative_entries() -> None:
    manifest = parse_issue_specification(
        "Persistence",
        """## Context
State must survive restart.
## Requirements
- REQ-001: Emit an audit event exactly once.
## Acceptance Scenarios
### AC-001 — restart example
Covers: REQ-001
Given state must survive restart.
""",
    )

    assert [(item.requirement_id, item.text) for item in manifest.requirements] == [("REQ-001", "Emit an audit event exactly once.")]
    assert manifest.acceptance_scenarios[0].requirement_ids == ("REQ-001",)
    assert manifest.is_structurally_valid


def test_acceptance_scenario_cannot_invent_normative_requirement() -> None:
    manifest = parse_issue_specification(
        "Example",
        """## Requirements
REQ-001: Return success.
## Acceptance Scenarios
### AC-001 — persistence
Covers: REQ-999
Then persist state.
""",
    )

    assert [item.requirement_id for item in manifest.requirements] == ["REQ-001"]
    assert manifest.acceptance_scenarios[0].requirement_ids == ("REQ-999",)
    assert [(item.code, item.line) for item in manifest.diagnostics] == [("unknown-requirement-reference", 5)]


def test_structural_diagnostics_are_complete_and_deterministic() -> None:
    body = """## Requirements
REQ-003: First.
REQ-003: Duplicate.
REQ-004:
This has no stable ID.
"""

    first = parse_issue_specification("Contract", body)
    second = parse_issue_specification("Contract", body)

    assert first == second
    assert [item.code for item in first.diagnostics] == [
        "duplicate-requirement-id",
        "empty-requirement-text",
        "malformed-requirement",
    ]
    assert [(item.requirement_id, item.text) for item in first.requirements] == [("REQ-003", "First.")]
    assert not first.is_structurally_valid


def test_missing_requirements_is_explicit_and_does_not_infer_context() -> None:
    manifest = parse_issue_specification("No contract", "## Context\nMust persist.\n")

    assert not manifest.has_requirements_section
    assert manifest.requirements == []
    assert [item.code for item in manifest.diagnostics] == ["missing-requirements-section"]


def test_adversarial_production_context_observes_shared_exact_manifest() -> None:
    issue = VerifiedIssueOracle(
        number=1726,
        title="Shared contract",
        body="## Context\nREQ-999: Not normative.\n## Requirements\nREQ-001: Preserve exact punctuation: yes!\n",
    )
    shared = parse_issue_specification(issue.title, issue.body)
    adversarial = build_issue_requirement_manifest(IssueOracleResolution(issues=(issue,)))

    assert adversarial.requirements == shared.requirements
    assert adversarial.mode == "explicit-contract"


def test_non_rendered_markdown_cannot_create_a_normative_section() -> None:
    body = """## Context
<!--
## Requirements
REQ-001: This is commented out.
-->
```markdown
## Requirements
REQ-002: This is only an example.
```
"""

    shared = parse_issue_specification("Hidden examples", body)
    intake = parse_requirement_contract(1726, body)
    adversarial = build_issue_requirement_manifest(IssueOracleResolution(issues=(VerifiedIssueOracle(number=1726, title="Hidden examples", body=body),)))

    assert shared.has_requirements_section is False
    assert shared.requirements == []
    assert [item.code for item in shared.diagnostics] == ["missing-requirements-section"]
    assert intake.explicit is False
    assert intake.entries == []
    assert [item.text for item in adversarial.requirements] == ["Context"]
    assert adversarial.mode == "legacy-extraction"


def test_only_first_requirements_section_is_normative_across_consumers() -> None:
    body = """## Requirements
REQ-001: First contract.
## Context
Intervening prose.
## Requirements
REQ-002: Later lookalike.
"""

    shared = parse_issue_specification("Repeated section", body)
    adversarial = build_issue_requirement_manifest(IssueOracleResolution(issues=(VerifiedIssueOracle(number=1726, title="Repeated section", body=body),)))

    assert [(item.requirement_id, item.text) for item in shared.requirements] == [("REQ-001", "First contract.")]
    assert adversarial.requirements == shared.requirements


def test_duplicate_empty_entry_reports_both_problems_on_stable_line() -> None:
    manifest = parse_issue_specification("Combined errors", "## Requirements\nREQ-003: First.\nREQ-003:\n")

    assert [(item.code, item.line) for item in manifest.diagnostics] == [
        ("duplicate-requirement-id", 3),
        ("empty-requirement-text", 3),
    ]
