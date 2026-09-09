from unittest.mock import MagicMock, patch

import pytest

from auto_coder.issue_context import (
    extract_lifecycle_branch_issue_number,
    extract_lifecycle_directive_issue_references,
    extract_linked_issues_from_pr_body,
    get_linked_issues_context,
    resolve_issue_oracles,
)


def test_extract_linked_issues():
    # Test cases
    body1 = "Fixes #123. Also resolves owner/repo#456"
    assert extract_linked_issues_from_pr_body(body1) == [123, 456]

    body2 = "No linked issues here."
    assert extract_linked_issues_from_pr_body(body2) == []

    body3 = "Closes #1. Fix #1. Resolves #2."
    assert extract_linked_issues_from_pr_body(body3) == [1, 2]  # Order preserved, duplicates removed

    body4 = ""
    assert extract_linked_issues_from_pr_body(body4) == []


def test_extract_lifecycle_directive_issue_references_excludes_mentions():
    # Restricted keywords with an optional owner/repo qualifier are extracted.
    assert extract_lifecycle_directive_issue_references("Fixes #100") == [(None, 100)]
    assert extract_lifecycle_directive_issue_references("Closes owner/repo#100") == [("owner/repo", 100)]
    assert extract_lifecycle_directive_issue_references("Closing: #7") == [(None, 7)]

    # Ordinary mentions and "related"/"relates" prose are never lifecycle evidence.
    assert extract_lifecycle_directive_issue_references("See #100 for context") == []
    assert extract_lifecycle_directive_issue_references("Related issue: #100") == []
    assert extract_lifecycle_directive_issue_references("Relates to #100") == []
    assert extract_lifecycle_directive_issue_references("Issue #100") == []
    assert extract_lifecycle_directive_issue_references("") == []

    # An exact number token must not match a numeric prefix.
    assert extract_lifecycle_directive_issue_references("Fixes #1000") == [(None, 1000)]


def test_extract_lifecycle_branch_issue_number():
    assert extract_lifecycle_branch_issue_number("issue-100-fix") == 100
    assert extract_lifecycle_branch_issue_number("feat/issue-100") == 100
    assert extract_lifecycle_branch_issue_number("fix-42-typo") == 42
    assert extract_lifecycle_branch_issue_number("100-cleanup") == 100
    assert extract_lifecycle_branch_issue_number("main") is None
    assert extract_lifecycle_branch_issue_number("") is None


def test_get_linked_issues_context():
    mock_client = MagicMock()
    repo_name = "owner/repo"

    # Mock Issue
    mock_issue = MagicMock()
    mock_issue.title = "Bug Fix"
    mock_issue.body = "Fixing a bug."
    mock_client.get_issue.return_value = mock_issue

    # Mock Parent Issue
    mock_client.get_parent_issue_details.return_value = {"number": 99, "title": "Epic Feature"}
    mock_client.get_parent_issue_body.return_value = "This is a big feature."

    pr_body = "Fixes #100"

    context = get_linked_issues_context(mock_client, repo_name, pr_body)

    assert "Linked Issue #100: Bug Fix" in context
    assert "Issue Description:\nFixing a bug." in context
    assert "Parent Issue #99 (CONTEXT ONLY - Parent of #100): Epic Feature" in context
    assert "SCOPE BOUNDARY NOTICE" in context
    assert "Parent Issue Description:\nThis is a big feature." in context
    assert context.startswith("Linked Issues Context:")


def test_get_linked_issues_context_no_issues():
    mock_client = MagicMock()
    context = get_linked_issues_context(mock_client, "repo", "Just text")
    assert context == ""


def test_get_linked_issues_context_fetch_error():
    mock_client = MagicMock()
    mock_client.get_issue.side_effect = Exception("API Error")

    context = get_linked_issues_context(mock_client, "repo", "Fixes #123")
    assert context == ""  # Should handle exception gracefully and return empty or partial


def test_title_inferred_issue_is_shared_by_resolution_and_context():
    mock_client = MagicMock()
    mock_client.get_issue.return_value = {"number": 42, "title": "Behavioral contract", "body": "Required behavior"}
    mock_client.get_parent_issue_details.return_value = None
    pr_data = {"number": 100, "title": "Implement issue #42", "body": "Implementation details"}

    resolution = resolve_issue_oracles(mock_client, "owner/repo", pr_data=pr_data)
    context = get_linked_issues_context(mock_client, "owner/repo", pr_data=pr_data)

    assert resolution.error is None
    assert tuple(issue.number for issue in resolution.issues) == (42,)
    assert "Linked Issue #42: Behavioral contract" in context
    assert "Issue Description:\nRequired behavior" in context
