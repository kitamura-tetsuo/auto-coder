from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.pr_processor import _find_issue_by_session_id_in_comments


class TestPRProcessorSearchOptimization:
    def test_find_issue_uses_search_api(self):
        """Test that _find_issue_by_session_id_in_comments uses search_issues instead of get_open_issues."""
        repo_name = "owner/repo"
        session_id = "test-session-id"

        mock_github_client = MagicMock()
        # Setup search_issues to return a found issue number
        mock_github_client.search_issues.return_value = [MagicMock(number=123, body=f"Session ID: {session_id}")]

        # Call the function
        found_issue = _find_issue_by_session_id_in_comments(repo_name, session_id, mock_github_client)

        # Verify it used search_issues using the expected query format
        mock_github_client.search_issues.assert_called_once()
        call_args = mock_github_client.search_issues.call_args[0][0]
        assert f"repo:{repo_name}" in call_args
        assert session_id in call_args
        assert "type:issue" in call_args

        # Verify result
        assert found_issue == 123

        # Verify it did NOT call get_open_issues (the inefficient way)
        mock_github_client.get_open_issues.assert_not_called()

    def test_find_issue_search_no_results(self):
        """Test that _find_issue_by_session_id_in_comments handles no search results."""
        repo_name = "owner/repo"
        session_id = "idx-123"

        mock_github_client = MagicMock()
        mock_github_client.search_issues.return_value = []

        found_issue = _find_issue_by_session_id_in_comments(repo_name, session_id, mock_github_client)

        assert found_issue is None
        mock_github_client.search_issues.assert_called_once()
