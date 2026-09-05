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


def test_commonmark_fence_grammar_is_shared_by_all_manifest_consumers() -> None:
    hidden_body = """```markdown
```still-code
## Requirements
REQ-001: Hidden example.
```
"""
    visible_body = """```lang`bad
## Requirements
REQ-001: Visible contract.
"""

    hidden_shared = parse_issue_specification("Fence content", hidden_body)
    hidden_intake = parse_requirement_contract(1726, hidden_body)
    hidden_adversarial = build_issue_requirement_manifest(IssueOracleResolution(issues=(VerifiedIssueOracle(number=1726, title="Fence content", body=hidden_body),)))
    visible_shared = parse_issue_specification("Invalid opener", visible_body)
    visible_intake = parse_requirement_contract(1726, visible_body)
    visible_adversarial = build_issue_requirement_manifest(IssueOracleResolution(issues=(VerifiedIssueOracle(number=1726, title="Invalid opener", body=visible_body),)))

    assert hidden_shared.has_requirements_section is False
    assert hidden_shared.requirements == []
    assert hidden_intake.explicit is False
    assert [item.text for item in hidden_adversarial.requirements] == []
    expected = [("REQ-001", "Visible contract.")]
    assert [(item.requirement_id, item.text) for item in visible_shared.requirements] == expected
    assert visible_intake.entries == expected
    assert [(item.requirement_id, item.text) for item in visible_adversarial.requirements] == expected


def test_commonmark_indented_code_is_shared_by_all_manifest_consumers() -> None:
    hidden_entry = "## Requirements\n    REQ-001: Indented example.\n"
    hidden_heading = "\t## Requirements\n\tREQ-001: Indented example.\n"
    visible_after_indented_fence = "\t```markdown\n## Requirements\nREQ-001: Visible contract.\n"
    hidden_after_indented_fence_like_content = "```markdown\n\t```\n## Requirements\nREQ-001: Hidden example.\n```\n"

    def manifests(body: str):
        return (
            parse_issue_specification("Indented code", body),
            parse_requirement_contract(1726, body),
            build_issue_requirement_manifest(IssueOracleResolution(issues=(VerifiedIssueOracle(number=1726, title="Indented code", body=body),))),
        )

    for body in (hidden_entry, hidden_heading, hidden_after_indented_fence_like_content):
        shared, intake, adversarial = manifests(body)
        assert shared.requirements == []
        assert intake.entries == []
        assert adversarial.requirements == []

    shared, intake, adversarial = manifests(visible_after_indented_fence)
    expected = [("REQ-001", "Visible contract.")]
    assert [(item.requirement_id, item.text) for item in shared.requirements] == expected
    assert intake.entries == expected
    assert [(item.requirement_id, item.text) for item in adversarial.requirements] == expected


def test_rendered_indented_continuation_fails_closed_across_consumers() -> None:
    body = "## Requirements\n- REQ-001: First line.\n    Continued normative text.\n"

    shared = parse_issue_specification("Continuation", body)
    intake = parse_requirement_contract(1726, body)
    adversarial = build_issue_requirement_manifest(IssueOracleResolution(issues=(VerifiedIssueOracle(number=1726, title="Continuation", body=body),)))

    assert [(item.requirement_id, item.text) for item in shared.requirements] == [("REQ-001", "First line.")]
    assert [(item.code, item.line) for item in shared.diagnostics] == [("malformed-requirement", 3)]
    assert intake.entries == []
    assert "malformed entries" in (intake.error or "")
    assert adversarial.requirements == []
    assert "malformed entries" in (adversarial.error or "")


def test_indented_html_comment_closer_exposes_following_contract_to_all_consumers() -> None:
    body = "<!--\n    -->\n## Requirements\nREQ-001: Visible contract.\n"

    shared = parse_issue_specification("Comment closer", body)
    intake = parse_requirement_contract(1726, body)
    adversarial = build_issue_requirement_manifest(IssueOracleResolution(issues=(VerifiedIssueOracle(number=1726, title="Comment closer", body=body),)))

    expected = [("REQ-001", "Visible contract.")]
    assert [(item.requirement_id, item.text) for item in shared.requirements] == expected
    assert intake.entries == expected
    assert [(item.requirement_id, item.text) for item in adversarial.requirements] == expected


def test_list_continuations_never_truncate_or_invent_requirements() -> None:
    bodies = (
        "## Requirements\n- REQ-001: First line.\n\n    Continued normative text.\n",
        "## Requirements\n- REQ-001: First line.\n    REQ-002: Still text in the same list item.\n",
        "## Requirements\n- REQ-001: First line.\n  REQ-002: Bullet continuation.\n",
        "## Requirements\n1. REQ-001: First line.\n   REQ-002: Ordered continuation.\n",
        "## Requirements\n- [ ] REQ-001: First line.\n  REQ-002: Unchecked task continuation.\n",
        "## Requirements\n- [x] REQ-001: First line.\n  REQ-002: Checked task continuation.\n",
        "## Requirements\n- REQ-001: First line. <!--\nhidden comment\n-->\n    Continued after comment.\n",
    )

    for body in bodies:
        shared = parse_issue_specification("Continuation", body)
        intake = parse_requirement_contract(1726, body)
        adversarial = build_issue_requirement_manifest(IssueOracleResolution(issues=(VerifiedIssueOracle(number=1726, title="Continuation", body=body),)))

        assert [(item.requirement_id, item.text) for item in shared.requirements] == [("REQ-001", "First line.")]
        assert [item.code for item in shared.diagnostics] == ["malformed-requirement"]
        assert intake.entries == []
        assert "malformed entries" in (intake.error or "")
        assert adversarial.requirements == []
        assert "malformed entries" in (adversarial.error or "")
