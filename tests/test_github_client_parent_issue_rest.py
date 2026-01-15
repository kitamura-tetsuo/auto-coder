from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.github_client import GitHubClient


class TestGitHubClientParentIssueREST:
    @patch("src.auto_coder.github_client.get_caching_client")
    def test_get_parent_issue_details_rest(self, mock_get_caching, mock_github_token):
        """Test get_parent_issue_details uses REST endpoint with version header."""
        # Setup
        mock_client = MagicMock()
        mock_get_caching.return_value = mock_client

        # Mock Response for issue with parent
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"number": 100, "title": "Child Issue", "parent": {"number": 50, "title": "Parent Issue", "state": "open"}}
        mock_client.get.return_value = mock_response

        client = GitHubClient.get_instance("token")
        client._caching_client = mock_client

        # Execute
        result = client.get_parent_issue_details("owner/repo", 100)

        # Assert
        assert result is not None
        assert result["number"] == 50
        assert result["title"] == "Parent Issue"

        # Verify Headers
        args, kwargs = mock_client.get.call_args
        assert "/repos/owner/repo/issues/100" in args[0]
        assert kwargs["headers"]["X-GitHub-Api-Version"] == "2022-11-28"

    @patch("src.auto_coder.github_client.get_caching_client")
    @patch.object(GitHubClient, "get_issue")
    def test_get_parent_issue_body_rest(self, mock_get_issue, mock_get_caching, mock_github_token):
        """Test get_parent_issue_body uses REST calls."""
        # Setup
        mock_client = MagicMock()
        mock_get_caching.return_value = mock_client

        # Mock Response for get_parent_issue_details (internal call)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"number": 100, "parent": {"number": 50, "title": "Parent"}}
        mock_client.get.return_value = mock_response

        # Mock Response for get_issue (parent)
        mock_parent_issue = MagicMock()
        mock_parent_issue.body = "This is parent body."
        mock_get_issue.return_value = mock_parent_issue

        client = GitHubClient.get_instance("token")
        client._caching_client = mock_client

        # Execute
        body = client.get_parent_issue_body("owner/repo", 100)

        # Assert
        assert body == "This is parent body."
        # Verify calls
        mock_client.get.assert_called()  # fetched child to find parent
        mock_get_issue.assert_called_with("owner/repo", 50)  # fetched parent to get body
