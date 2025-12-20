"""
Tests for GitHub client parent issue detection functionality using GraphQL API.
"""

import json
from unittest.mock import Mock, patch

import pytest

from src.auto_coder.github_client import GitHubClient


class TestGitHubClientParentIssue:
    """Test cases for parent issue detection in GitHubClient using GraphQL API."""

    def test_get_parent_issue_exists(self):
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

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = graphql_response
        mock_response.raise_for_status = Mock()

        with patch.object(client._caching_client, "post", return_value=mock_response) as mock_post:
            result = client.get_parent_issue("owner/repo", 100)
            assert result == 1

            # Verify the GraphQL-Features header was included
            mock_post.assert_called_once()
            call_kwargs = mock_post.call_args
            headers = call_kwargs.kwargs.get("headers", {})
            assert headers.get("GraphQL-Features") == "sub_issues"

    def test_get_parent_issue_no_parent(self):
        """Test get_parent_issue when issue has no parent."""
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

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = graphql_response
        mock_response.raise_for_status = Mock()

        with patch.object(client._caching_client, "post", return_value=mock_response):
            result = client.get_parent_issue("owner/repo", 1)
            assert result is None

    def test_get_parent_issue_graphql_error(self):
        """Test get_parent_issue when GraphQL query fails."""
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
            result = client.get_parent_issue("owner/repo", 1)
            # Should return None on error
            assert result is None

    def test_get_parent_issue_closed_parent(self):
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

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = graphql_response
        mock_response.raise_for_status = Mock()

        with patch.object(client._caching_client, "post", return_value=mock_response):
            result = client.get_parent_issue("owner/repo", 100)
            # Should still return parent issue number even if closed
            assert result == 1


class TestGitHubClientParentIssueBody:
    """Test cases for parent issue body retrieval in GitHubClient."""

    def test_get_parent_issue_body_exists(self):
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

        mock_response1 = Mock()
        mock_response1.status_code = 200
        mock_response1.json.return_value = parent_details_response
        mock_response1.raise_for_status = Mock()

        mock_response2 = Mock()
        mock_response2.status_code = 200
        mock_response2.json.return_value = issue_with_body_response
        mock_response2.raise_for_status = Mock()

        with patch.object(client._caching_client, "post", side_effect=[mock_response1, mock_response2]) as mock_post:
            result = client.get_parent_issue_body("owner/repo", 100)
            assert result == "This is the parent issue body with full details."
            assert mock_post.call_count == 2

    def test_get_parent_issue_body_no_parent(self):
        """Test get_parent_issue_body when issue has no parent."""
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

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = graphql_response
        mock_response.raise_for_status = Mock()

        with patch.object(client._caching_client, "post", return_value=mock_response) as mock_post:
            result = client.get_parent_issue_body("owner/repo", 1)
            assert result is None
            # Should only call once for get_parent_issue_details
            assert mock_post.call_count == 1

    def test_get_parent_issue_body_empty_body(self):
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

        mock_response1 = Mock()
        mock_response1.status_code = 200
        mock_response1.json.return_value = parent_details_response
        mock_response1.raise_for_status = Mock()

        mock_response2 = Mock()
        mock_response2.status_code = 200
        mock_response2.json.return_value = issue_with_empty_body_response
        mock_response2.raise_for_status = Mock()

        with patch.object(client._caching_client, "post", side_effect=[mock_response1, mock_response2]):
            result = client.get_parent_issue_body("owner/repo", 100)
            assert result is None

    def test_get_parent_issue_body_graphql_error(self):
        """Test get_parent_issue_body when fetching parent issue body fails."""
        import httpx

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

        mock_response1 = Mock()
        mock_response1.status_code = 200
        mock_response1.json.return_value = parent_details_response
        mock_response1.raise_for_status = Mock()

        # Second call fails
        mock_error_response = Mock()
        mock_error_response.status_code = 500
        mock_error_response.text = "Internal Server Error"

        with patch.object(
            client._caching_client,
            "post",
            side_effect=[mock_response1, httpx.HTTPStatusError("Server Error", request=Mock(), response=mock_error_response)],
        ):
            result = client.get_parent_issue_body("owner/repo", 100)
            assert result is None

    def test_get_parent_issue_body_multiline_body(self):
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

        mock_response1 = Mock()
        mock_response1.status_code = 200
        mock_response1.json.return_value = parent_details_response
        mock_response1.raise_for_status = Mock()

        mock_response2 = Mock()
        mock_response2.status_code = 200
        mock_response2.json.return_value = issue_with_multiline_body_response
        mock_response2.raise_for_status = Mock()

        with patch.object(client._caching_client, "post", side_effect=[mock_response1, mock_response2]):
            result = client.get_parent_issue_body("owner/repo", 100)
            assert result == multiline_body
            assert "multiple paragraphs" in result
            assert "- Point 1" in result
