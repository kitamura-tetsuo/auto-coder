"""
Tests for GitHub client parent issue detection functionality using GraphQL API.
"""

import json
from unittest.mock import Mock, patch

import pytest

from src.auto_coder.github_client import GitHubClient


class TestGitHubClientParentIssue:
    """Test cases for parent issue detection in GitHubClient using GraphQL API."""

    @patch("subprocess.run")
    def test_get_parent_issue_exists(self, mock_subprocess_run):
        """Test get_parent_issue when parent issue exists."""
        client = GitHubClient("test_token")

        # Mock GraphQL response
        graphql_response = {
            "data": {
                "repository": {
                    "issue": {
                        "number": 100,
                        "title": "Sub-issue",
                        "parent": {
                            "number": 1,
                            "title": "Parent issue",
                            "state": "OPEN",
                            "url": "https://github.com/owner/repo/issues/1",
                        },
                    }
                }
            }
        }

        mock_result = Mock()
        mock_result.stdout = json.dumps(graphql_response)
        mock_subprocess_run.return_value = mock_result

        result = client.get_parent_issue("owner/repo", 100)
        assert result == 1

        # Verify the GraphQL-Features header was included
        mock_subprocess_run.assert_called_once()
        call_args = mock_subprocess_run.call_args[0][0]
        assert "-H" in call_args
        header_index = call_args.index("-H")
        assert call_args[header_index + 1] == "GraphQL-Features: sub_issues"

    @patch("subprocess.run")
    def test_get_parent_issue_no_parent(self, mock_subprocess_run):
        """Test get_parent_issue when issue has no parent."""
        client = GitHubClient("test_token")

        # Mock GraphQL response with no parent issue
        graphql_response = {
            "data": {
                "repository": {
                    "issue": {
                        "number": 1,
                        "title": "Top-level issue",
                        "parent": None,
                    }
                }
            }
        }

        mock_result = Mock()
        mock_result.stdout = json.dumps(graphql_response)
        mock_subprocess_run.return_value = mock_result

        result = client.get_parent_issue("owner/repo", 1)
        assert result is None

    @patch("subprocess.run")
    def test_get_parent_issue_graphql_error(self, mock_subprocess_run):
        """Test get_parent_issue when GraphQL query fails."""
        import subprocess

        client = GitHubClient("test_token")

        # Mock subprocess error
        mock_subprocess_run.side_effect = subprocess.CalledProcessError(
            1, "gh api graphql", stderr="GraphQL error"
        )

        result = client.get_parent_issue("owner/repo", 1)
        # Should return None on error
        assert result is None

    @patch("subprocess.run")
    def test_get_parent_issue_closed_parent(self, mock_subprocess_run):
        """Test get_parent_issue when parent issue is closed."""
        client = GitHubClient("test_token")

        # Mock GraphQL response with closed parent
        graphql_response = {
            "data": {
                "repository": {
                    "issue": {
                        "number": 100,
                        "title": "Sub-issue",
                        "parent": {
                            "number": 1,
                            "title": "Parent issue",
                            "state": "CLOSED",
                            "url": "https://github.com/owner/repo/issues/1",
                        },
                    }
                }
            }
        }

        mock_result = Mock()
        mock_result.stdout = json.dumps(graphql_response)
        mock_subprocess_run.return_value = mock_result

        result = client.get_parent_issue("owner/repo", 100)
        # Should still return parent issue number even if closed
        assert result == 1
