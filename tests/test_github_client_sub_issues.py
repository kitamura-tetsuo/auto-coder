"""
Tests for GitHub client sub-issues detection functionality using GraphQL API.
"""

import json
from unittest.mock import Mock, patch

import pytest

from src.auto_coder.github_client import GitHubClient


class TestGitHubClientSubIssues:
    """Test cases for sub-issues detection in GitHubClient using GraphQL API."""

    @patch("src.auto_coder.github_client.GitHubClient._get_ghapi_client")
    def test_get_open_sub_issues_all_open(self, mock_get_ghapi_client):
        """Test get_open_sub_issues when all sub-issues are open."""
        client = GitHubClient.get_instance("test_token")

        # Mock GraphQL response
        graphql_response = {
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

        mock_ghapi_client = Mock()
        mock_ghapi_client.graphql.return_value = graphql_response
        mock_get_ghapi_client.return_value = mock_ghapi_client

        result = client.get_open_sub_issues("owner/repo", 1)
        assert result == [100, 200, 300]

        # Verify the GraphQL-Features header was included
        mock_ghapi_client.graphql.assert_called_once()
        call_args = mock_ghapi_client.graphql.call_args
        assert "headers" in call_args.kwargs
        assert call_args.kwargs["headers"] == {"GraphQL-Features": "sub_issues"}

    @patch("src.auto_coder.github_client.GitHubClient._get_ghapi_client")
    def test_get_open_sub_issues_some_closed(self, mock_get_ghapi_client):
        """Test get_open_sub_issues when some sub-issues are closed."""
        client = GitHubClient.get_instance("test_token")

        # Mock GraphQL response with mixed states
        graphql_response = {
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

        mock_ghapi_client = Mock()
        mock_ghapi_client.graphql.return_value = graphql_response
        mock_get_ghapi_client.return_value = mock_ghapi_client

        result = client.get_open_sub_issues("owner/repo", 1)
        assert result == [100, 300]

    @patch("src.auto_coder.github_client.GitHubClient._get_ghapi_client")
    def test_get_open_sub_issues_all_closed(self, mock_get_ghapi_client):
        """Test get_open_sub_issues when all sub-issues are closed."""
        client = GitHubClient.get_instance("test_token")

        # Mock GraphQL response with all closed
        graphql_response = {
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

        mock_ghapi_client = Mock()
        mock_ghapi_client.graphql.return_value = graphql_response
        mock_get_ghapi_client.return_value = mock_ghapi_client

        result = client.get_open_sub_issues("owner/repo", 1)
        assert result == []

    @patch("src.auto_coder.github_client.GitHubClient._get_ghapi_client")
    def test_get_open_sub_issues_no_sub_issues(self, mock_get_ghapi_client):
        """Test get_open_sub_issues when issue has no sub-issues."""
        client = GitHubClient.get_instance("test_token")

        # Mock GraphQL response with no sub-issues
        graphql_response = {
            "repository": {
                "issue": {
                    "number": 1,
                    "title": "Parent issue",
                    "subIssues": {"nodes": []},
                }
            }
        }

        mock_ghapi_client = Mock()
        mock_ghapi_client.graphql.return_value = graphql_response
        mock_get_ghapi_client.return_value = mock_ghapi_client

        result = client.get_open_sub_issues("owner/repo", 1)
        assert result == []

    @patch("src.auto_coder.github_client.GitHubClient._get_ghapi_client")
    def test_get_open_sub_issues_graphql_error(self, mock_get_ghapi_client):
        """Test get_open_sub_issues when GraphQL query fails."""
        client = GitHubClient.get_instance("test_token")

        # Mock ghapi error
        mock_ghapi_client = Mock()
        mock_ghapi_client.graphql.side_effect = Exception("GraphQL error")
        mock_get_ghapi_client.return_value = mock_ghapi_client

        result = client.get_open_sub_issues("owner/repo", 1)
        # Should return empty list on error
        assert result == []
