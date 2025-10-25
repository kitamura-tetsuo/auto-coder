"""
Tests for GitHub client sub-issues detection functionality using GraphQL API.
"""

import json
from unittest.mock import Mock, patch

import pytest

from src.auto_coder.github_client import GitHubClient


class TestGitHubClientSubIssues:
    """Test cases for sub-issues detection in GitHubClient using GraphQL API."""

    @patch("subprocess.run")
    def test_get_open_sub_issues_all_open(self, mock_subprocess_run):
        """Test get_open_sub_issues when all sub-issues are open."""
        client = GitHubClient("test_token")

        # Mock GraphQL response
        graphql_response = {
            "data": {
                "repository": {
                    "issue": {
                        "number": 1,
                        "title": "Parent issue",
                        "trackedIssues": {
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

        mock_result = Mock()
        mock_result.stdout = json.dumps(graphql_response)
        mock_subprocess_run.return_value = mock_result

        result = client.get_open_sub_issues("owner/repo", 1)
        assert result == [100, 200, 300]

    @patch("subprocess.run")
    def test_get_open_sub_issues_some_closed(self, mock_subprocess_run):
        """Test get_open_sub_issues when some sub-issues are closed."""
        client = GitHubClient("test_token")

        # Mock GraphQL response with mixed states
        graphql_response = {
            "data": {
                "repository": {
                    "issue": {
                        "number": 1,
                        "title": "Parent issue",
                        "trackedIssues": {
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

        mock_result = Mock()
        mock_result.stdout = json.dumps(graphql_response)
        mock_subprocess_run.return_value = mock_result

        result = client.get_open_sub_issues("owner/repo", 1)
        assert result == [100, 300]

    @patch("subprocess.run")
    def test_get_open_sub_issues_all_closed(self, mock_subprocess_run):
        """Test get_open_sub_issues when all sub-issues are closed."""
        client = GitHubClient("test_token")

        # Mock GraphQL response with all closed
        graphql_response = {
            "data": {
                "repository": {
                    "issue": {
                        "number": 1,
                        "title": "Parent issue",
                        "trackedIssues": {
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

        mock_result = Mock()
        mock_result.stdout = json.dumps(graphql_response)
        mock_subprocess_run.return_value = mock_result

        result = client.get_open_sub_issues("owner/repo", 1)
        assert result == []

    @patch("subprocess.run")
    def test_get_open_sub_issues_no_sub_issues(self, mock_subprocess_run):
        """Test get_open_sub_issues when issue has no sub-issues."""
        client = GitHubClient("test_token")

        # Mock GraphQL response with no tracked issues
        graphql_response = {
            "data": {
                "repository": {
                    "issue": {
                        "number": 1,
                        "title": "Parent issue",
                        "trackedIssues": {"nodes": []},
                    }
                }
            }
        }

        mock_result = Mock()
        mock_result.stdout = json.dumps(graphql_response)
        mock_subprocess_run.return_value = mock_result

        result = client.get_open_sub_issues("owner/repo", 1)
        assert result == []

    @patch("subprocess.run")
    def test_get_open_sub_issues_graphql_error(self, mock_subprocess_run):
        """Test get_open_sub_issues when GraphQL query fails."""
        import subprocess

        client = GitHubClient("test_token")

        # Mock subprocess error
        mock_subprocess_run.side_effect = subprocess.CalledProcessError(
            1, "gh api graphql", stderr="GraphQL error"
        )

        result = client.get_open_sub_issues("owner/repo", 1)
        # Should return empty list on error
        assert result == []

