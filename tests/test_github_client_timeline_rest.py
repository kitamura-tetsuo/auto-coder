from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.auto_coder.util.gh_cache import GitHubClient


def _connected_prs_response(nodes, has_next_page=False, end_cursor=None):
    return httpx.Response(
        200,
        json={
            "data": {
                "repository": {
                    "issue": {
                        "closedByPullRequestsReferences": {
                            "pageInfo": {"hasNextPage": has_next_page, "endCursor": end_cursor},
                            "nodes": nodes,
                        }
                    }
                }
            }
        },
        request=httpx.Request("POST", "https://api.github.com/graphql"),
    )


def _pr_node(number, owner="owner", name="repo"):
    return {"number": number, "repository": {"owner": {"login": owner}, "name": name}}


class TestGitHubClientTimelineREST:
    @patch("src.auto_coder.util.gh_cache.get_caching_client")
    def test_verify_pr_closes_issue_rest(self, mock_get_caching, mock_github_token):
        """Test verify_pr_closes_issue uses REST Timeline connected events."""
        # Setup
        mock_client = MagicMock()
        mock_get_caching.return_value = mock_client

        # Mock Response
        mock_response = httpx.Response(
            200,
            json=[{"event": "connected", "source": {"issue": {"number": 101, "pull_request": {}}}}],
            request=httpx.Request("GET", "https://api.github.com/repos/owner/repo/issues/1/timeline?per_page=100"),
        )
        mock_client.request.return_value = mock_response

        client = GitHubClient.get_instance("token")
        client._caching_client = mock_client

        # Execute & Assert
        assert client.verify_pr_closes_issue("owner/repo", 101, 1) is True
        assert client.verify_pr_closes_issue("owner/repo", 999, 1) is False


class TestGetConnectedPrs:
    """get_connected_prs() returns only native GitHub-tracked closing relationships."""

    @patch("src.auto_coder.util.gh_cache.get_caching_client")
    def test_returns_a_native_connection(self, mock_get_caching, mock_github_token):
        mock_client = MagicMock()
        mock_get_caching.return_value = mock_client
        mock_client.request.return_value = _connected_prs_response([_pr_node(101)])

        client = GitHubClient.get_instance("token")
        client._caching_client = mock_client

        assert client.get_connected_prs("owner/repo", 1) == [101]

    @patch("src.auto_coder.util.gh_cache.get_caching_client")
    def test_excludes_a_foreign_repository_pr(self, mock_get_caching, mock_github_token):
        """A same-numbered PR in another repository must never be conflated with a local one."""
        mock_client = MagicMock()
        mock_get_caching.return_value = mock_client
        mock_client.request.return_value = _connected_prs_response([_pr_node(1771, owner="kitamura-tetsuo", name="auto-coder")])

        client = GitHubClient.get_instance("token")
        client._caching_client = mock_client

        assert client.get_connected_prs("kitamura-tetsuo/outliner", 5290) == []

    @patch("src.auto_coder.util.gh_cache.get_caching_client")
    def test_foreign_duplicate_number_does_not_erase_a_validated_local_connection(self, mock_get_caching, mock_github_token):
        mock_client = MagicMock()
        mock_get_caching.return_value = mock_client
        mock_client.request.return_value = _connected_prs_response(
            [
                _pr_node(1771, owner="kitamura-tetsuo", name="auto-coder"),
                _pr_node(1771, owner="kitamura-tetsuo", name="outliner"),
            ]
        )

        client = GitHubClient.get_instance("token")
        client._caching_client = mock_client

        assert client.get_connected_prs("kitamura-tetsuo/outliner", 5290) == [1771]

    @patch("src.auto_coder.util.gh_cache.get_caching_client")
    def test_paginates_across_multiple_pages(self, mock_get_caching, mock_github_token):
        mock_client = MagicMock()
        mock_get_caching.return_value = mock_client
        mock_client.request.side_effect = [
            _connected_prs_response([_pr_node(101)], has_next_page=True, end_cursor="cursor-1"),
            _connected_prs_response([_pr_node(102)], has_next_page=False),
        ]

        client = GitHubClient.get_instance("token")
        client._caching_client = mock_client

        assert client.get_connected_prs("owner/repo", 1) == [101, 102]
        assert mock_client.request.call_count == 2

    @patch("src.auto_coder.util.gh_cache.get_caching_client")
    def test_non_strict_failure_returns_empty_list(self, mock_get_caching, mock_github_token):
        mock_client = MagicMock()
        mock_get_caching.return_value = mock_client
        mock_client.request.side_effect = httpx.ConnectError("boom")

        client = GitHubClient.get_instance("token")
        client._caching_client = mock_client

        with patch("time.sleep"):
            assert client.get_connected_prs("owner/repo", 1) == []

    @patch("src.auto_coder.util.gh_cache.get_caching_client")
    def test_strict_failure_raises(self, mock_get_caching, mock_github_token):
        client = GitHubClient.get_instance("token")

        with patch("httpx.Client") as mock_client_cls, patch("time.sleep"):
            mock_client_cls.return_value.__enter__.return_value.post.side_effect = httpx.ConnectError("boom")

            with pytest.raises(Exception):
                client.get_connected_prs("owner/repo", 1, strict=True)

        mock_get_caching.assert_not_called()

    def test_strict_mode_bypasses_the_shared_caching_client(self, mock_github_token):
        """REQ-005: strict evidence must never be served from the reusable cache."""
        client = GitHubClient.get_instance("token")

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "data": {
                "repository": {
                    "issue": {
                        "closedByPullRequestsReferences": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [_pr_node(101)],
                        }
                    }
                }
            }
        }

        with (
            patch("src.auto_coder.util.gh_cache.get_caching_client") as mock_get_caching_client,
            patch("httpx.Client") as mock_client_cls,
        ):
            mock_client_cls.return_value.__enter__.return_value.post.return_value = mock_response

            result = client.get_connected_prs("owner/repo", 1, strict=True)

        assert result == [101]
        mock_get_caching_client.assert_not_called()
        mock_client_cls.return_value.__enter__.return_value.post.assert_called_once()

    @patch("src.auto_coder.util.gh_cache.get_caching_client")
    def test_strict_raises_on_an_invalid_pr_number(self, mock_get_caching, mock_github_token):
        mock_client = MagicMock()
        mock_get_caching.return_value = mock_client

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "data": {
                "repository": {
                    "issue": {
                        "closedByPullRequestsReferences": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [{"number": "not-a-number", "repository": {"owner": {"login": "owner"}, "name": "repo"}}],
                        }
                    }
                }
            }
        }

        client = GitHubClient.get_instance("token")

        with patch("httpx.Client") as mock_client_cls:
            mock_client_cls.return_value.__enter__.return_value.post.return_value = mock_response

            with pytest.raises(RuntimeError):
                client.get_connected_prs("owner/repo", 1, strict=True)

    @patch("src.auto_coder.util.gh_cache.get_caching_client")
    def test_strict_raises_on_ambiguous_repository_identity(self, mock_get_caching, mock_github_token):
        mock_client = MagicMock()
        mock_get_caching.return_value = mock_client

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "data": {
                "repository": {
                    "issue": {
                        "closedByPullRequestsReferences": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [{"number": 101, "repository": None}],
                        }
                    }
                }
            }
        }

        client = GitHubClient.get_instance("token")

        with patch("httpx.Client") as mock_client_cls:
            mock_client_cls.return_value.__enter__.return_value.post.return_value = mock_response

            with pytest.raises(RuntimeError):
                client.get_connected_prs("owner/repo", 1, strict=True)
