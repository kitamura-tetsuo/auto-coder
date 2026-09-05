"""Regression coverage for the reusable normative Issue manifest."""

import pytest

from auto_coder.requirement_contract import build_normative_issue_manifest


def test_valid_manifest_preserves_exact_order_and_text_and_stops_at_next_heading():
    body = """## Requirements
REQ-001: First behavior.
- [ ] `REQ-002:` Second behavior: keep punctuation.
## Context
Ignored.
## Requirements
REQ-003: Must not reopen.
"""

    manifest = build_normative_issue_manifest(1726, "Manifest boundary", body)

    assert manifest.issue_number == 1726
    assert manifest.title == "Manifest boundary"
    assert manifest.explicit_contract_present is True
    assert manifest.explicit_contract_valid is True
    assert [(entry.requirement_id, entry.text) for entry in manifest.requirements] == [
        ("REQ-001", "First behavior."),
        ("REQ-002", "Second behavior: keep punctuation."),
    ]
    assert manifest.error is None
    assert manifest.invalid_reason is None


@pytest.mark.parametrize(
    "contract_line",
    [
        "```python",
        "    example = True",
        "  continuation text",
        "- [ ] task-list prose without an identifier",
    ],
)
def test_markdown_structure_does_not_hide_or_merge_physical_lines(contract_line):
    manifest = build_normative_issue_manifest(1726, "Line grammar", f"## Requirements\nREQ-001: Valid.\n{contract_line}")

    assert manifest.explicit_contract_present is True
    assert manifest.explicit_contract_valid is False
    assert manifest.requirements == ()
    assert manifest.invalid_reason == "malformed_entry"
    assert contract_line.strip() in (manifest.error or "")


def test_only_limited_comment_lines_are_ignored():
    body = """## Requirements
<!-- complete opening line
REQ-999: This remains visible because comments are not block parsed.
complete closing line -->
REQ-001: Preserved.
"""

    manifest = build_normative_issue_manifest(1726, "Comments", body)

    assert manifest.explicit_contract_valid is True
    assert [(entry.requirement_id, entry.text) for entry in manifest.requirements] == [
        ("REQ-999", "This remains visible because comments are not block parsed."),
        ("REQ-001", "Preserved."),
    ]


def test_missing_contract_does_not_infer_normative_requirements():
    manifest = build_normative_issue_manifest(1726, "Missing", "## Context\nREQ-001: Prose only.\n## Acceptance Scenarios\nREQ-002: Also prose.")

    assert manifest.explicit_contract_present is False
    assert manifest.explicit_contract_valid is False
    assert manifest.requirements == ()
    assert manifest.error is None
    assert manifest.invalid_reason is None


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("## Requirements\nREQ-001: Valid.\nnot an entry", "malformed_entry"),
        ("## Requirements\nREQ-001: One.\nREQ-001: Two.", "duplicate_ids"),
        ("## Requirements\n\n<!-- ignored -->", "no_entries"),
    ],
)
def test_invalid_contract_reasons_are_distinct_and_never_expose_partial_entries(body, reason):
    manifest = build_normative_issue_manifest(1726, "Invalid", body)

    assert manifest.explicit_contract_present is True
    assert manifest.explicit_contract_valid is False
    assert manifest.requirements == ()
    assert manifest.invalid_reason == reason
    assert manifest.error is not None
