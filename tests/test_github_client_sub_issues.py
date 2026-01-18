"""
Tests for GitHub client sub-issues detection functionality using REST API.
"""

import json
from unittest.mock import Mock, patch

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
        # Mock REST API response - direct list of issues
        sub_issues_data = [
            {
                "number": 100,
                "title": "Sub-issue 1",
                "state": "open",
                "url": "https://github.com/owner/repo/issues/100",
            },
            {
                "number": 200,
                "title": "Sub-issue 2",
                "state": "open",
                "url": "https://github.com/owner/repo/issues/200",
            },
            {
                "number": 300,
                "title": "Sub-issue 3",
                "state": "open",
                "url": "https://github.com/owner/repo/issues/300",
            },
        ]

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = sub_issues_data
        mock_response.raise_for_status = Mock()

        mock_caching_client = Mock()
        mock_caching_client.get.return_value = mock_response

        with patch("src.auto_coder.util.gh_cache.get_caching_client", return_value=mock_caching_client):
            client = GitHubClient.get_instance("test_token")
            result = client.get_open_sub_issues("owner/repo", 1)
            assert result == [100, 200, 300]

    def test_get_open_sub_issues_some_closed(self):
        """Test get_open_sub_issues when some sub-issues are closed."""
        # Mock REST API response with mixed states
        sub_issues_data = [
            {
                "number": 100,
                "title": "Sub-issue 1",
                "state": "open",
                "url": "https://github.com/owner/repo/issues/100",
            },
            {
                "number": 200,
                "title": "Sub-issue 2",
                "state": "closed",
                "url": "https://github.com/owner/repo/issues/200",
            },
            {
                "number": 300,
                "title": "Sub-issue 3",
                "state": "open",
                "url": "https://github.com/owner/repo/issues/300",
            },
        ]

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = sub_issues_data
        mock_response.raise_for_status = Mock()

        mock_caching_client = Mock()
        mock_caching_client.get.return_value = mock_response

        with patch("src.auto_coder.util.gh_cache.get_caching_client", return_value=mock_caching_client):
            client = GitHubClient.get_instance("test_token")
            result = client.get_open_sub_issues("owner/repo", 1)
            assert result == [100, 300]

    def test_get_open_sub_issues_all_closed(self):
        """Test get_open_sub_issues when all sub-issues are closed."""
        # Mock REST API response with all closed
        sub_issues_data = [
            {
                "number": 100,
                "title": "Sub-issue 1",
                "state": "closed",
                "url": "https://github.com/owner/repo/issues/100",
            },
            {
                "number": 200,
                "title": "Sub-issue 2",
                "state": "closed",
                "url": "https://github.com/owner/repo/issues/200",
            },
        ]

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = sub_issues_data
        mock_response.raise_for_status = Mock()

        mock_caching_client = Mock()
        mock_caching_client.get.return_value = mock_response

        with patch("src.auto_coder.util.gh_cache.get_caching_client", return_value=mock_caching_client):
            client = GitHubClient.get_instance("test_token")
            result = client.get_open_sub_issues("owner/repo", 1)
            assert result == []

    def test_get_open_sub_issues_no_sub_issues(self):
        """Test get_open_sub_issues when issue has no sub-issues."""
        # Mock REST API response with no sub-issues
        sub_issues_data = []

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = sub_issues_data
        mock_response.raise_for_status = Mock()

        mock_caching_client = Mock()
        mock_caching_client.get.return_value = mock_response

        with patch("src.auto_coder.util.gh_cache.get_caching_client", return_value=mock_caching_client):
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

        mock_caching_client = Mock()
        mock_caching_client.get.side_effect = httpx.HTTPStatusError("Server Error", request=Mock(), response=mock_error_response)

        with patch("src.auto_coder.util.gh_cache.get_caching_client", return_value=mock_caching_client):
            client = GitHubClient.get_instance("test_token")
            result = client.get_open_sub_issues("owner/repo", 1)
            # Should return empty list on error
            assert result == []

    def test_get_open_sub_issues_404_returns_empty(self):
        """Test get_open_sub_issues when API returns 404 (no sub-issues feature)."""
        import httpx

        mock_error_response = Mock()
        mock_error_response.status_code = 404

        mock_caching_client = Mock()
        mock_caching_client.get.side_effect = httpx.HTTPStatusError("Not Found", request=Mock(), response=mock_error_response)

        with patch("src.auto_coder.util.gh_cache.get_caching_client", return_value=mock_caching_client):
            client = GitHubClient.get_instance("test_token")
            result = client.get_open_sub_issues("owner/repo", 1)
            # Should return empty list on 404
            assert result == []
