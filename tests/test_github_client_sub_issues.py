"""
Tests for GitHub client sub-issues detection functionality using REST API.
"""

import json
from unittest.mock import MagicMock, Mock, patch

import pytest

from src.auto_coder.util.gh_cache import GitHubClient


@pytest.fixture(autouse=True)
def reset_singleton():
    """Reset GitHubClient singleton before each test."""
    GitHubClient.reset_singleton()
    yield
    GitHubClient.reset_singleton()


class TestGitHubClientSubIssues:
    """Test cases for sub-issues detection in GitHubClient using REST API."""

    def test_get_open_sub_issues_all_open(self):
        """Test get_open_sub_issues when all sub-issues are open."""
        # Mock REST API response (not GraphQL)
        rest_response = [
            {"number": 100, "title": "Sub-issue 1", "state": "OPEN", "url": "https://github.com/owner/repo/issues/100"},
            {"number": 200, "title": "Sub-issue 2", "state": "OPEN", "url": "https://github.com/owner/repo/issues/200"},
            {"number": 300, "title": "Sub-issue 3", "state": "OPEN", "url": "https://github.com/owner/repo/issues/300"},
        ]

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = rest_response
        mock_response.raise_for_status = Mock()

        # Create a mock client that has a get method returning our mock response
        mock_client = Mock()
        mock_client.get.return_value = mock_response

        with patch("src.auto_coder.util.gh_cache.get_caching_client", return_value=mock_client):
            client = GitHubClient.get_instance("test_token")
            result = client.get_open_sub_issues("owner/repo", 1)
            assert result == [100, 200, 300]

            # Verify the request was made
            mock_client.get.assert_called_once()
            call_kwargs = mock_client.get.call_args
            assert "sub_issues" in call_kwargs.kwargs.get("url", "")

    def test_get_open_sub_issues_some_closed(self):
        """Test get_open_sub_issues when some sub-issues are closed."""
        # Mock REST API response with mixed states
        rest_response = [
            {"number": 100, "title": "Sub-issue 1", "state": "OPEN", "url": "https://github.com/owner/repo/issues/100"},
            {"number": 200, "title": "Sub-issue 2", "state": "CLOSED", "url": "https://github.com/owner/repo/issues/200"},
            {"number": 300, "title": "Sub-issue 3", "state": "OPEN", "url": "https://github.com/owner/repo/issues/300"},
        ]

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = rest_response
        mock_response.raise_for_status = Mock()

        mock_client = Mock()
        mock_client.get.return_value = mock_response

        with patch("src.auto_coder.util.gh_cache.get_caching_client", return_value=mock_client):
            client = GitHubClient.get_instance("test_token")
            result = client.get_open_sub_issues("owner/repo", 1)
            assert result == [100, 300]

    def test_get_open_sub_issues_all_closed(self):
        """Test get_open_sub_issues when all sub-issues are closed."""
        # Mock REST API response with all closed
        rest_response = [
            {"number": 100, "title": "Sub-issue 1", "state": "CLOSED", "url": "https://github.com/owner/repo/issues/100"},
            {"number": 200, "title": "Sub-issue 2", "state": "CLOSED", "url": "https://github.com/owner/repo/issues/200"},
        ]

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = rest_response
        mock_response.raise_for_status = Mock()

        mock_client = Mock()
        mock_client.get.return_value = mock_response

        with patch("src.auto_coder.util.gh_cache.get_caching_client", return_value=mock_client):
            client = GitHubClient.get_instance("test_token")
            result = client.get_open_sub_issues("owner/repo", 1)
            assert result == []

    def test_get_open_sub_issues_no_sub_issues(self):
        """Test get_open_sub_issues when issue has no sub-issues."""
        # Mock REST API response with no sub-issues
        rest_response = []

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = rest_response
        mock_response.raise_for_status = Mock()

        mock_client = Mock()
        mock_client.get.return_value = mock_response

        with patch("src.auto_coder.util.gh_cache.get_caching_client", return_value=mock_client):
            client = GitHubClient.get_instance("test_token")
            result = client.get_open_sub_issues("owner/repo", 1)
            assert result == []

    def test_get_open_sub_issues_graphql_error(self):
        """Test get_open_sub_issues when REST API returns error."""
        import httpx

        # Mock httpx error
        mock_error_response = Mock()
        mock_error_response.status_code = 500
        mock_error_response.text = "Internal Server Error"

        mock_client = Mock()
        mock_client.get.side_effect = httpx.HTTPStatusError("Server Error", request=Mock(), response=mock_error_response)

        with patch("src.auto_coder.util.gh_cache.get_caching_client", return_value=mock_client):
            client = GitHubClient.get_instance("test_token")
            result = client.get_open_sub_issues("owner/repo", 1)
            # Should return empty list on error
            assert result == []

    def test_get_open_sub_issues_404_returns_empty(self):
        """Test get_open_sub_issues when API returns 404 (feature not enabled)."""
        mock_response = Mock()
        mock_response.status_code = 404
        mock_response.raise_for_status = Mock()

        mock_client = Mock()
        mock_client.get.return_value = mock_response

        with patch("src.auto_coder.util.gh_cache.get_caching_client", return_value=mock_client):
            client = GitHubClient.get_instance("test_token")
            result = client.get_open_sub_issues("owner/repo", 1)
            # 404 should return empty list
            assert result == []
