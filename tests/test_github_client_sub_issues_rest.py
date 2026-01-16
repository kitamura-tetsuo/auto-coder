from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.util.gh_cache import GitHubClient


class TestGitHubClientSubIssuesREST:
    @patch("src.auto_coder.util.gh_cache.get_caching_client")
    def test_get_open_sub_issues_rest(self, mock_get_caching, mock_github_token):
        """Test get_open_sub_issues uses REST endpoint correctly."""
        # Setup
        mock_client = MagicMock()
        mock_get_caching.return_value = mock_client

        # Mock Response
        # Endpoint: /repos/owner/repo/issues/1/sub_issues
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [{"number": 101, "state": "open"}, {"number": 102, "state": "closed"}, {"number": 103, "state": "open"}]
        mock_client.get.return_value = mock_response

        client = GitHubClient.get_instance("token")
        client.clear_sub_issue_cache()  # Clear cache from previous tests
        client._caching_client = mock_client  # Force inject

        # Execute
        result = client.get_open_sub_issues("owner/repo", 1)

        # Assert
        assert result == [101, 103]
        args, kwargs = mock_client.get.call_args
        assert "/repos/owner/repo/issues/1/sub_issues" in args[0]
        assert kwargs["headers"]["X-GitHub-Api-Version"] == "2022-11-28"

    @patch("src.auto_coder.util.gh_cache.get_caching_client")
    def test_get_all_sub_issues_rest(self, mock_get_caching, mock_github_token):
        """Test get_all_sub_issues uses REST endpoint correctly."""
        # Setup
        mock_client = MagicMock()
        mock_get_caching.return_value = mock_client

        # Mock Response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [{"number": 201, "state": "closed"}, {"number": 202, "state": "open"}]
        mock_client.get.return_value = mock_response

        client = GitHubClient.get_instance("token")
        client._caching_client = mock_client

        # Execute
        result = client.get_all_sub_issues("owner/repo", 2)

        # Assert
        assert result == [201, 202]
