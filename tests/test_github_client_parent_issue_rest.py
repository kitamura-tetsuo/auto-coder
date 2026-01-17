from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.util.gh_cache import GitHubClient


class TestGitHubClientParentIssueREST:
    @patch("src.auto_coder.util.gh_cache.get_ghapi_client")
    def test_get_parent_issue_details_rest(self, mock_get_ghapi, mock_github_token):
        """Test get_parent_issue_details uses GhApi and proper endpoint."""
        # Setup
        mock_api = MagicMock()
        mock_get_ghapi.return_value = mock_api

        # Mock Parent Issue Object (Plain Dict)
        mock_parent_issue = {"number": 50, "title": "Parent Issue"}

        # api is called directly: api(path, verb=..., headers=...)
        mock_api.return_value = mock_parent_issue

        client = GitHubClient.get_instance("token")

        # Execute
        result = client.get_parent_issue_details("owner/repo", 100)

        # Assert
        assert result is not None
        assert result["number"] == 50
        assert result["title"] == "Parent Issue"

        # Verify Call
        mock_api.assert_called_once_with("/repos/owner/repo/issues/100/parent", verb="GET", headers={"X-GitHub-Api-Version": "2022-11-28", "Accept": "application/vnd.github+json"})

    @patch("src.auto_coder.util.gh_cache.get_ghapi_client")
    @patch.object(GitHubClient, "get_issue")
    def test_get_parent_issue_body_rest(self, mock_get_issue, mock_get_ghapi, mock_github_token):
        """Test get_parent_issue_body uses REST calls."""
        # Setup
        mock_api = MagicMock()
        mock_get_ghapi.return_value = mock_api

        # Mock Response for get_parent_issue_details
        mock_parent_issue = {"number": 50, "title": "Parent"}
        mock_api.return_value = mock_parent_issue

        # Mock Response for get_issue (parent)
        mock_parent_issue_full = MagicMock()
        mock_parent_issue_full.body = "This is parent body."
        mock_get_issue.return_value = mock_parent_issue_full

        client = GitHubClient.get_instance("token")

        # Execute
        body = client.get_parent_issue_body("owner/repo", 100)

        # Assert
        assert body == "This is parent body."

        # Verify calls
        mock_api.assert_called()
        mock_get_issue.assert_called_with("owner/repo", 50)

    @patch("src.auto_coder.util.gh_cache.get_ghapi_client")
    def test_get_parent_issue_no_parent(self, mock_get_ghapi, mock_github_token):
        """Test get_parent_issue_details returns None when no parent exists (dedicated endpoint fails)."""
        # Setup
        mock_api = MagicMock()
        mock_get_ghapi.return_value = mock_api

        # dedicated api() raises 404 - the new implementation returns None without fallback
        mock_api.side_effect = Exception("HTTP 404: Not Found")

        client = GitHubClient.get_instance("token")

        # Execute
        result = client.get_parent_issue_details("owner/repo", 100)

        # Assert
        assert result is None

        # Verify the dedicated endpoint was called
        mock_api.assert_called_once()

    @patch("src.auto_coder.util.gh_cache.get_ghapi_client")
    def test_get_parent_issue_wrapped(self, mock_get_ghapi, mock_github_token):
        """Test get_parent_issue_details handles wrapped nested response."""
        # Setup
        mock_api = MagicMock()
        mock_get_ghapi.return_value = mock_api

        # Mock Wrapped Response: {"parent": {"number": 50, ...}}
        mock_response = {"parent": {"number": 50, "title": "Wrapper Parent"}}

        mock_api.side_effect = None
        mock_api.return_value = mock_response

        client = GitHubClient.get_instance("token")

        # Execute
        result = client.get_parent_issue_details("owner/repo", 100)

        # Assert
        assert result is not None
        assert result["number"] == 50
        assert result["title"] == "Wrapper Parent"

    @patch("src.auto_coder.util.gh_cache.get_ghapi_client")
    def test_get_parent_issue_error(self, mock_get_ghapi, mock_github_token):
        """Test get_parent_issue_details handles unexpected errors."""
        # Setup
        mock_api = MagicMock()
        mock_get_ghapi.return_value = mock_api

        # 1. dedicated api() raises error
        # 2. fallback api.issues.get() raises error

        mock_api.side_effect = Exception("Some other error")
        mock_api.issues.get.side_effect = Exception("Fallback Error")

        client = GitHubClient.get_instance("token")

        # Execute
        result = client.get_parent_issue_details("owner/repo", 100)

        # Assert
        assert result is None

    @patch("src.auto_coder.util.gh_cache.get_ghapi_client")
    def test_get_parent_issue_fallback(self, mock_get_ghapi, mock_github_token):
        """Test get_parent_issue_details returns None when dedicated endpoint returns 404 response.

        Note: The new implementation does not fallback to api.issues.get(). It simply returns None
        when the dedicated parent endpoint indicates no parent exists.
        """
        # Setup
        mock_api = MagicMock()
        mock_get_ghapi.return_value = mock_api

        # Dedicated endpoint returns 404 dict
        # The new implementation logs a warning and returns None
        mock_response_dedicated = {"message": "Not Found", "status": "404"}

        mock_api.return_value = mock_response_dedicated

        client = GitHubClient.get_instance("token")

        # Execute
        result = client.get_parent_issue_details("owner/repo", 100)

        # Assert
        # The new implementation returns None when there's no parent
        assert result is None

        # Verify dedicated endpoint was called
        mock_api.assert_called()
