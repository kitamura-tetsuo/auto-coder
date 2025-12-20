"""
Tests for GitHub client sub-issues detection functionality using GraphQL API.
"""

import json
from unittest.mock import Mock, patch

import pytest

from src.auto_coder.github_client import GitHubClient


class TestGitHubClientSubIssues:
    """Test cases for sub-issues detection in GitHubClient using GraphQL API."""

    def test_get_open_sub_issues_all_open(self):
        """Test get_open_sub_issues when all sub-issues are open."""
        client = GitHubClient.get_instance("test_token")

        # Mock GraphQL response
        graphql_response = {
            "data": {
                "repository": {
                    "issue": {
                        "number": 1,
                        "title": "Parent issue",
                        "subIssues": {
                            "nodes": [
                                {
                                    "number": 100,
                                    "title": "Sub-issue 1",
                                    "state": "OPEN",
                                    "url": "https://github.com/owner/repo/issues/100",
                                },
                                {
                                    "number": 200,
                                    "title": "Sub-issue 2",
                                    "state": "OPEN",
                                    "url": "https://github.com/owner/repo/issues/200",
                                },
                                {
                                    "number": 300,
                                    "title": "Sub-issue 3",
                                    "state": "OPEN",
                                    "url": "https://github.com/owner/repo/issues/300",
                                },
                            ]
                        },
                    }
                }
            }
        }

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = graphql_response
        mock_response.raise_for_status = Mock()

        with patch.object(client._caching_client, "post", return_value=mock_response) as mock_post:
            result = client.get_open_sub_issues("owner/repo", 1)
            assert result == [100, 200, 300]

            # Verify the GraphQL-Features header was included
            mock_post.assert_called_once()
            call_kwargs = mock_post.call_args
            headers = call_kwargs.kwargs.get("headers", {})
            assert headers.get("GraphQL-Features") == "sub_issues"

    def test_get_open_sub_issues_some_closed(self):
        """Test get_open_sub_issues when some sub-issues are closed."""
        client = GitHubClient.get_instance("test_token")

        # Mock GraphQL response with mixed states
        graphql_response = {
            "data": {
                "repository": {
                    "issue": {
                        "number": 1,
                        "title": "Parent issue",
                        "subIssues": {
                            "nodes": [
                                {
                                    "number": 100,
                                    "title": "Sub-issue 1",
                                    "state": "OPEN",
                                    "url": "https://github.com/owner/repo/issues/100",
                                },
                                {
                                    "number": 200,
                                    "title": "Sub-issue 2",
                                    "state": "CLOSED",
                                    "url": "https://github.com/owner/repo/issues/200",
                                },
                                {
                                    "number": 300,
                                    "title": "Sub-issue 3",
                                    "state": "OPEN",
                                    "url": "https://github.com/owner/repo/issues/300",
                                },
                            ]
                        },
                    }
                }
            }
        }

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = graphql_response
        mock_response.raise_for_status = Mock()

        with patch.object(client._caching_client, "post", return_value=mock_response):
            result = client.get_open_sub_issues("owner/repo", 1)
            assert result == [100, 300]

    def test_get_open_sub_issues_all_closed(self):
        """Test get_open_sub_issues when all sub-issues are closed."""
        client = GitHubClient.get_instance("test_token")

        # Mock GraphQL response with all closed
        graphql_response = {
            "data": {
                "repository": {
                    "issue": {
                        "number": 1,
                        "title": "Parent issue",
                        "subIssues": {
                            "nodes": [
                                {
                                    "number": 100,
                                    "title": "Sub-issue 1",
                                    "state": "CLOSED",
                                    "url": "https://github.com/owner/repo/issues/100",
                                },
                                {
                                    "number": 200,
                                    "title": "Sub-issue 2",
                                    "state": "CLOSED",
                                    "url": "https://github.com/owner/repo/issues/200",
                                },
                            ]
                        },
                    }
                }
            }
        }

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = graphql_response
        mock_response.raise_for_status = Mock()

        with patch.object(client._caching_client, "post", return_value=mock_response):
            result = client.get_open_sub_issues("owner/repo", 1)
            assert result == []

    def test_get_open_sub_issues_no_sub_issues(self):
        """Test get_open_sub_issues when issue has no sub-issues."""
        client = GitHubClient.get_instance("test_token")

        # Mock GraphQL response with no sub-issues
        graphql_response = {
            "data": {
                "repository": {
                    "issue": {
                        "number": 1,
                        "title": "Parent issue",
                        "subIssues": {"nodes": []},
                    }
                }
            }
        }

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = graphql_response
        mock_response.raise_for_status = Mock()

        with patch.object(client._caching_client, "post", return_value=mock_response):
            result = client.get_open_sub_issues("owner/repo", 1)
            assert result == []

    def test_get_open_sub_issues_graphql_error(self):
        """Test get_open_sub_issues when GraphQL query fails."""
        import httpx

        client = GitHubClient.get_instance("test_token")

        # Mock httpx error
        mock_response = Mock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"

        with patch.object(
            client._caching_client,
            "post",
            side_effect=httpx.HTTPStatusError("Server Error", request=Mock(), response=mock_response),
        ):
            result = client.get_open_sub_issues("owner/repo", 1)
            # Should return empty list on error
            assert result == []
