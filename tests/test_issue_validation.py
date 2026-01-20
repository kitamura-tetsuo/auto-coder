import re
from unittest.mock import MagicMock

import pytest

from auto_coder.issue_context import extract_linked_issues_from_pr_body, validate_issue_references


def test_extract_linked_issues_extended_keywords():
    """Test that new keywords 'Related issue(s)' are extracted correctly."""

    # Existing keywords
    assert extract_linked_issues_from_pr_body("Closes #1") == [1]
    assert extract_linked_issues_from_pr_body("Fixes #2") == [2]

    # New keywords
    assert extract_linked_issues_from_pr_body("Related issue #3") == [3]
    assert extract_linked_issues_from_pr_body("Related issues #4") == [4]

    # Case insensitivity
    assert extract_linked_issues_from_pr_body("related issue #5") == [5]

    # Optional colon
    # Based on regex: keywords = r"..." -> pattern = rf"{keywords}:?\s+..."
    assert extract_linked_issues_from_pr_body("Related issue: #6") == [6]
    assert extract_linked_issues_from_pr_body("Related issues: #7") == [7]
    assert extract_linked_issues_from_pr_body("Closes: #8") == [8]

    # Multiple
    body = "Closes #1. Related issue: #2. Fixes owner/repo#3"
    assert set(extract_linked_issues_from_pr_body(body)) == {1, 2, 3}


def test_validate_issue_references_valid_issue():
    """Test validation passes when referencing a standard issue."""
    mock_client = MagicMock()
    # Mock get_issue to return an Issue object (dict without pull_request key)
    # Or AttrDict. GitHub API returns PRs with "pull_request" key, Issues without it.
    mock_client.get_issue.return_value = {"number": 123, "title": "Real Issue"}

    pr_body = "Closes #123"
    # Should not raise
    validate_issue_references(pr_body, mock_client, "owner/repo")


def test_validate_issue_references_invalid_pr():
    """Test validation raises ValueError when referencing a PR."""
    mock_client = MagicMock()
    # Mock get_issue to return a PR object (dict WITH pull_request key)
    mock_client.get_issue.return_value = {"number": 456, "title": "A Pull Request", "pull_request": {"url": "..."}}

    pr_body = "Related issue: #456"

    with pytest.raises(ValueError) as excinfo:
        validate_issue_references(pr_body, mock_client, "owner/repo")

    assert "Reference #456 points to a Pull Request" in str(excinfo.value)


def test_validate_issue_references_mixed():
    """Test validation passes for valid issues but fails if ANY is a PR."""
    mock_client = MagicMock()

    def side_effect(repo, number):
        if number == 1:
            return {"number": 1}  # Issue
        if number == 2:
            return {"number": 2, "pull_request": {}}  # PR
        return None

    mock_client.get_issue.side_effect = side_effect

    pr_body = "Closes #1. Related issue: #2."

    with pytest.raises(ValueError) as excinfo:
        validate_issue_references(pr_body, mock_client, "owner/repo")

    assert "Reference #2 points to a Pull Request" in str(excinfo.value)


def test_validate_issue_references_api_error():
    """Test validation logs warning but doesn't crash on API error."""
    mock_client = MagicMock()
    mock_client.get_issue.side_effect = Exception("API Down")

    pr_body = "Closes #999"
    # Should not raise exception (caught and logged)
    validate_issue_references(pr_body, mock_client, "owner/repo")


def test_validate_pr_update_with_invalid_ref():
    """Test validation prevents updating PR with reference to another PR."""
    mock_client = MagicMock()
    mock_client.get_issue.return_value = {"number": 888, "pull_request": {}}

    # Simulating what happens in pr_processor.py update paths
    new_body = "Existing body.\n\nRelated issue: #888"

    with pytest.raises(ValueError) as excinfo:
        validate_issue_references(new_body, mock_client, "owner/repo")

    assert "Reference #888 points to a Pull Request" in str(excinfo.value)
