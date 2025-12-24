from unittest.mock import MagicMock, patch

import pytest

from auto_coder.issue_context import extract_linked_issues_from_pr_body, get_linked_issues_context


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
    assert "Parent Issue #99 (of #100): Epic Feature" in context
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
