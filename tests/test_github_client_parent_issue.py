"""
Tests for GitHub client parent issue detection functionality using GraphQL API.
"""

import json
from unittest.mock import Mock, patch

from src.auto_coder.github_client import GitHubClient


class TestGitHubClientParentIssue:
    """Test cases for parent issue detection in GitHubClient using GraphQL API."""

    def test_get_parent_issue_exists(self):
        """Test get_parent_issue when parent issue exists."""
        GitHubClient.reset_singleton()
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

        with patch.object(client, "graphql_query", return_value=graphql_response) as mock_graphql_query:
            result = client.get_parent_issue("owner/repo", 100)
            assert result == 1

            # Verify the GraphQL-Features header was included
            mock_graphql_query.assert_called_once()
            call_args = mock_graphql_query.call_args
            assert call_args[0][2] == {"GraphQL-Features": "sub_issues"}

    def test_get_parent_issue_no_parent(self):
        """Test get_parent_issue when issue has no parent."""
        GitHubClient.reset_singleton()
        client = GitHubClient.get_instance("test_token")

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

        with patch.object(client, "graphql_query", return_value=graphql_response):
            result = client.get_parent_issue("owner/repo", 1)
            assert result is None

    def test_get_parent_issue_graphql_error(self):
        """Test get_parent_issue when GraphQL query fails."""
        GitHubClient.reset_singleton()
        client = GitHubClient.get_instance("test_token")

        with patch.object(client, "graphql_query", side_effect=Exception("GraphQL error")):
            result = client.get_parent_issue("owner/repo", 1)
            # Should return None on error
            assert result is None

    def test_get_parent_issue_closed_parent(self):
        """Test get_parent_issue when parent issue is closed."""
        GitHubClient.reset_singleton()
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

        with patch.object(client, "graphql_query", return_value=graphql_response):
            result = client.get_parent_issue("owner/repo", 100)
            # Should still return parent issue number even if closed
            assert result == 1


class TestGitHubClientParentIssueBody:
    """Test cases for parent issue body retrieval in GitHubClient."""

    def test_get_parent_issue_body_exists(self):
        """Test get_parent_issue_body when parent issue exists with body."""
        GitHubClient.reset_singleton()
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

        with patch.object(client, "graphql_query", side_effect=[parent_details_response, issue_with_body_response]) as mock_graphql_query:
            result = client.get_parent_issue_body("owner/repo", 100)
            assert result == "This is the parent issue body with full details."
            assert mock_graphql_query.call_count == 2

    def test_get_parent_issue_body_no_parent(self):
        """Test get_parent_issue_body when issue has no parent."""
        GitHubClient.reset_singleton()
        client = GitHubClient.get_instance("test_token")

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

        with patch.object(client, "graphql_query", return_value=graphql_response) as mock_graphql_query:
            result = client.get_parent_issue_body("owner/repo", 1)
            assert result is None
            # Should only call once for get_parent_issue_details
            assert mock_graphql_query.call_count == 1

    @patch("subprocess.run")
    def test_get_parent_issue_body_empty_body(self, mock_subprocess_run):
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
        issue_with_empty_body_response = {
            "data": {
                "repository": {
                    "issue": {
                        "number": 1,
                        "title": "Parent issue",
                        "body": None,
                        "state": "OPEN",
                        "url": "https://github.com/owner/repo/issues/1",
                    }
                }
            }
        }

        mock_result1 = Mock()
        mock_result1.stdout = json.dumps(parent_details_response)
        mock_result2 = Mock()
        mock_result2.stdout = json.dumps(issue_with_empty_body_response)

        mock_subprocess_run.side_effect = [mock_result1, mock_result2]

        result = client.get_parent_issue_body("owner/repo", 100)
        assert result is None

    @patch("subprocess.run")
    def test_get_parent_issue_body_graphql_error(self, mock_subprocess_run):
        """Test get_parent_issue_body when fetching parent issue body fails."""
        import subprocess

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

        mock_result1 = Mock()
        mock_result1.stdout = json.dumps(parent_details_response)
        mock_subprocess_run.side_effect = [mock_result1, subprocess.CalledProcessError(1, "gh api graphql", stderr="GraphQL error")]

        result = client.get_parent_issue_body("owner/repo", 100)
        assert result is None

    def test_get_parent_issue_body_multiline_body(self):
        """Test get_parent_issue_body with multiline body content."""
        GitHubClient.reset_singleton()
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

        with patch.object(client, "graphql_query", side_effect=[parent_details_response, issue_with_multiline_body_response]):
            result = client.get_parent_issue_body("owner/repo", 100)
            assert result == multiline_body
            assert "multiple paragraphs" in result
            assert "- Point 1" in result
