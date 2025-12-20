import json
from unittest.mock import Mock, patch

from src.auto_coder.github_client import GitHubClient


class TestGitHubClientComplexityFix:
    """Tests for the fix of GraphQL complexity issue in get_open_prs_json."""

    def test_get_open_prs_json_uses_graphql(self, mock_github_token):
        """Test that get_open_prs_json uses graphql_query and parses response correctly."""
        # Setup
        # Mock successful GraphQL response
        graphql_response = {
            "data": {
                "repository": {
                    "pullRequests": {
                        "nodes": [
                            {
                                "number": 123,
                                "title": "Test PR",
                                "body": "Body content",
                                "state": "OPEN",
                                "url": "https://github.com/owner/repo/pull/123",
                                "createdAt": "2024-01-01T00:00:00Z",
                                "updatedAt": "2024-01-02T00:00:00Z",
                                "isDraft": False,
                                "mergeable": "MERGEABLE",
                                "headRefName": "feature-branch",
                                "headRefOid": "abc123sha",
                                "baseRefName": "main",
                                "author": {"login": "dev-user"},
                                "assignees": {"nodes": [{"login": "assignee-user"}]},
                                "labels": {"nodes": [{"name": "bug"}, {"name": "priority"}]},
                                "comments": {"totalCount": 5},
                                "commits": {"totalCount": 10},
                                "additions": 100,
                                "deletions": 20,
                                "changedFiles": 3,
                            }
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": "cursor123"},
                    }
                }
            }
        }

        with patch("src.auto_coder.github_client.Github"), patch("src.auto_coder.github_client.get_caching_client"):

            GitHubClient.reset_singleton()
            client = GitHubClient.get_instance("fake-token")

            # We will mock graphql_query on the client instance once we plan to use it.
            # However, since the current implementation uses 'gh' CLI, this test will FAIL
            # or behave differently until we change the implementation.
            # To verify the FIX, we want to ensure that IF we mock graphql_query, it works.
            # But wait, initially I want to see it fail or rather I want to prepare the test
            # that WILL pass after my changes.

            with patch.object(client, "graphql_query", return_value=graphql_response) as mock_graphql:
                # Execute
                # NOTE: The current implementation calls 'gh' CLI, not graphql_query for this method.
                # So running this test NOW would actually try to run 'gh' and fail or return empty if blocked.
                # I will implement the test expecting the NEW behavior.

                # To make this test fail properly before fix (if I wanted to), I'd have to assert
                # that graphql_query was called.

                result = client.get_open_prs_json("owner/repo")

                # Assertions for the Expected Result AFTER fix
                assert len(result) == 1
                pr = result[0]
                assert pr["number"] == 123
                assert pr["title"] == "Test PR"
                assert pr["comments_count"] == 5
                assert pr["commits_count"] == 10
                assert pr["labels"] == ["bug", "priority"]
                assert pr["assignees"] == ["assignee-user"]
                assert pr["mergeable"] is True

                # Verify graphql_query was called
                mock_graphql.assert_called_once()
                call_args = mock_graphql.call_args
                assert "query($owner: String!, $repo: String!, $cursor: String, $limit: Int)" in call_args[0][0]
