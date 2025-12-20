"""
Tests for GitHub client parent issue detection functionality using GraphQL API.
"""

import json
from unittest.mock import Mock, patch

import pytest

from src.auto_coder.github_client import GitHubClient


class TestGitHubClientParentIssue:
    """Test cases for parent issue detection in GitHubClient using GraphQL API."""

    @patch("src.auto_coder.github_client.GitHubClient.graphql_query")
    def test_get_parent_issue_exists(self, mock_graphql_query):
        """Test get_parent_issue when parent issue exists."""
        client = GitHubClient.get_instance("test_token")

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
        mock_graphql_query.return_value = graphql_response

        result = client.get_parent_issue("owner/repo", 100)
        assert result == 1

        # Verify the GraphQL-Features header was included
        mock_graphql_query.assert_called_once()
        call_args = mock_graphql_query.call_args[1]
        assert "headers" in call_args
        assert call_args["headers"] == {"GraphQL-Features": "sub_issues"}

    @patch("src.auto_coder.github_client.GitHubClient.graphql_query")
    def test_get_parent_issue_no_parent(self, mock_graphql_query):
        """Test get_parent_issue when issue has no parent."""
        client = GitHubClient.get_instance("test_token")

        # Mock GraphQL response with no parent issue
        graphql_response = {"data": {"repository": {"issue": {"number": 1, "title": "Top-level issue", "parent": None}}}}
        mock_graphql_query.return_value = graphql_response

        result = client.get_parent_issue("owner/repo", 1)
        assert result is None

    @patch("src.auto_coder.github_client.GitHubClient.graphql_query")
    def test_get_parent_issue_graphql_error(self, mock_graphql_query):
        """Test get_parent_issue when GraphQL query fails."""
        client = GitHubClient.get_instance("test_token")

        # Mock ghapi error
        mock_graphql_query.side_effect = Exception("GraphQL error")

        result = client.get_parent_issue("owner/repo", 1)
        # Should return None on error
        assert result is None

    @patch("src.auto_coder.github_client.GitHubClient.graphql_query")
    def test_get_parent_issue_closed_parent(self, mock_graphql_query):
        """Test get_parent_issue when parent issue is closed."""
        client = GitHubClient.get_instance("test_token")

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
        mock_graphql_query.return_value = graphql_response

        result = client.get_parent_issue("owner/repo", 100)
        # Should still return parent issue number even if closed
        assert result == 1


class TestGitHubClientParentIssueBody:
    """Test cases for parent issue body retrieval in GitHubClient."""

    @patch("src.auto_coder.github_client.GitHubClient.graphql_query")
    def test_get_parent_issue_body_exists(self, mock_graphql_query):
        """Test get_parent_issue_body when parent issue exists with body."""
        client = GitHubClient.get_instance("test_token")

        # First call: get_parent_issue_details response
        parent_details_response = {
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

        # Second call: get full issue with body
        issue_with_body_response = {
            "data": {
                "repository": {
                    "issue": {
                        "number": 1,
                        "title": "Parent issue",
                        "body": "This is the parent issue body with full details.",
                        "state": "OPEN",
                        "url": "https://github.com/owner/repo/issues/1",
                    }
                }
            }
        }

        mock_graphql_query.side_effect = [parent_details_response, issue_with_body_response]

        result = client.get_parent_issue_body("owner/repo", 100)
        assert result == "This is the parent issue body with full details."
        assert mock_graphql_query.call_count == 2
        call_args = mock_graphql_query.call_args_list[0][1]
        assert "headers" in call_args
        assert call_args["headers"] == {"GraphQL-Features": "sub_issues"}

    @patch("src.auto_coder.github_client.GitHubClient.graphql_query")
    def test_get_parent_issue_body_no_parent(self, mock_graphql_query):
        """Test get_parent_issue_body when issue has no parent."""
        client = GitHubClient.get_instance("test_token")

        # Mock GraphQL response with no parent issue
        graphql_response = {"data": {"repository": {"issue": {"number": 1, "title": "Top-level issue", "parent": None}}}}
        mock_graphql_query.return_value = graphql_response

        result = client.get_parent_issue_body("owner/repo", 1)
        assert result is None
        # Should only call once for get_parent_issue_details
        assert mock_graphql_query.call_count == 1

    @patch("src.auto_coder.github_client.GitHubClient.graphql_query")
    def test_get_parent_issue_body_empty_body(self, mock_graphql_query):
        """Test get_parent_issue_body when parent issue has empty body."""
        client = GitHubClient.get_instance("test_token")

        # First call: get_parent_issue_details response
        parent_details_response = {
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

        # Second call: get issue with empty body
        issue_with_empty_body_response = {"data": {"repository": {"issue": {"number": 1, "title": "Parent issue", "body": None, "state": "OPEN", "url": "https://github.com/owner/repo/issues/1"}}}}

        mock_graphql_query.side_effect = [parent_details_response, issue_with_empty_body_response]

        result = client.get_parent_issue_body("owner/repo", 100)
        assert result is None

    @patch("src.auto_coder.github_client.GitHubClient.graphql_query")
    def test_get_parent_issue_body_graphql_error(self, mock_graphql_query):
        """Test get_parent_issue_body when fetching parent issue body fails."""
        client = GitHubClient.get_instance("test_token")

        # First call succeeds (get_parent_issue_details)
        parent_details_response = {
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

        mock_graphql_query.side_effect = [parent_details_response, Exception("GraphQL error")]

        result = client.get_parent_issue_body("owner/repo", 100)
        assert result is None

    @patch("src.auto_coder.github_client.GitHubClient.graphql_query")
    def test_get_parent_issue_body_multiline_body(self, mock_graphql_query):
        """Test get_parent_issue_body with multiline body content."""
        client = GitHubClient.get_instance("test_token")

        # First call: get_parent_issue_details response
        parent_details_response = {
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

        # Second call: get issue with multiline body
        multiline_body = """This is a multiline parent issue body.

It contains multiple paragraphs and detailed information about the issue.

- Point 1
- Point 2
- Point 3"""

        issue_with_multiline_body_response = {
            "data": {
                "repository": {
                    "issue": {
                        "number": 1,
                        "title": "Parent issue",
                        "body": multiline_body,
                        "state": "OPEN",
                        "url": "https://github.com/owner/repo/issues/1",
                    }
                }
            }
        }

        mock_graphql_query.side_effect = [parent_details_response, issue_with_multiline_body_response]

        result = client.get_parent_issue_body("owner/repo", 100)
        assert result == multiline_body
        assert "multiple paragraphs" in result
        assert "- Point 1" in result
