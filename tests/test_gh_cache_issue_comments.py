"""Tests for paginated issue comment retrieval in gh_cache.

Issue comments drive attempt tracking; a truncated listing makes Auto-Coder read a
stale attempt number and post the same attempt comment on every cycle.
"""

from unittest.mock import Mock, patch

import pytest

from src.auto_coder.util.gh_cache import COMMENTS_MAX_PAGES, GitHubClient


def _make_client() -> GitHubClient:
    GitHubClient.reset_singleton()
    return GitHubClient.get_instance(token="test-token")


def _comment(index: int) -> dict:
    return {"body": f"comment {index}", "created_at": "2026-08-10T00:00:00Z", "user": {"login": "auto-coder"}, "id": index}


class TestGetIssueCommentsPagination:
    def teardown_method(self):
        GitHubClient.reset_singleton()

    @patch("src.auto_coder.util.gh_cache.get_ghapi_client")
    def test_fetches_every_page(self, mock_get_api):
        """A conversation longer than one page is returned in full."""
        pages = {1: [_comment(i) for i in range(0, 100)], 2: [_comment(i) for i in range(100, 200)], 3: [_comment(i) for i in range(200, 205)]}

        mock_api = Mock()
        mock_api.issues.list_comments.side_effect = lambda owner, repo, issue_number, per_page, page: pages.get(page, [])
        mock_get_api.return_value = mock_api

        client = _make_client()
        comments = client.get_issue_comments("owner/repo", 4787)

        assert len(comments) == 205
        assert comments[0]["body"] == "comment 0"
        assert comments[-1]["body"] == "comment 204"
        assert mock_api.issues.list_comments.call_count == 3
        assert [call.kwargs["page"] for call in mock_api.issues.list_comments.call_args_list] == [1, 2, 3]
        assert {call.kwargs["per_page"] for call in mock_api.issues.list_comments.call_args_list} == {100}

    @patch("src.auto_coder.util.gh_cache.get_ghapi_client")
    def test_stops_after_short_page(self, mock_get_api):
        """A page shorter than per_page is the last one; no extra request is made."""
        mock_api = Mock()
        mock_api.issues.list_comments.side_effect = lambda owner, repo, issue_number, per_page, page: [_comment(0), _comment(1)] if page == 1 else []
        mock_get_api.return_value = mock_api

        client = _make_client()
        comments = client.get_issue_comments("owner/repo", 4787)

        assert len(comments) == 2
        assert mock_api.issues.list_comments.call_count == 1

    @patch("src.auto_coder.util.gh_cache.get_ghapi_client")
    def test_empty_issue_returns_empty_list(self, mock_get_api):
        mock_api = Mock()
        mock_api.issues.list_comments.return_value = []
        mock_get_api.return_value = mock_api

        client = _make_client()

        assert client.get_issue_comments("owner/repo", 4787) == []
        assert mock_api.issues.list_comments.call_count == 1

    @patch("src.auto_coder.util.gh_cache.get_ghapi_client")
    def test_pagination_is_bounded(self, mock_get_api):
        """A server that never returns a short page cannot loop forever."""
        mock_api = Mock()
        mock_api.issues.list_comments.side_effect = lambda owner, repo, issue_number, per_page, page: [_comment(i) for i in range(per_page)]
        mock_get_api.return_value = mock_api

        client = _make_client()
        comments = client.get_issue_comments("owner/repo", 4787)

        assert mock_api.issues.list_comments.call_count == COMMENTS_MAX_PAGES
        assert len(comments) == COMMENTS_MAX_PAGES * 100

    @patch("src.auto_coder.util.gh_cache.get_ghapi_client")
    def test_api_error_returns_empty_list(self, mock_get_api):
        mock_api = Mock()
        mock_api.issues.list_comments.side_effect = RuntimeError("API error")
        mock_get_api.return_value = mock_api

        client = _make_client()

        assert client.get_issue_comments("owner/repo", 4787) == []

    @patch("src.auto_coder.util.gh_cache.get_ghapi_client")
    def test_strict_pr_comment_lookup_propagates_api_error(self, mock_get_api):
        mock_api = Mock()
        mock_api.side_effect = RuntimeError("API error")
        mock_get_api.return_value = mock_api

        client = _make_client()

        with pytest.raises(RuntimeError, match="API error"):
            client.get_pr_comments_strict("owner/repo", 4787)

        assert mock_api.call_args.kwargs["headers"] == {"Cache-Control": "no-cache"}

    @patch("src.auto_coder.util.gh_cache.get_ghapi_client")
    def test_strict_issue_comment_lookup_propagates_api_error(self, mock_get_api):
        """Strict Issue reads preserve ambiguity instead of converting it to []."""
        mock_api = Mock()
        mock_api.side_effect = RuntimeError("API error")
        mock_get_api.return_value = mock_api

        client = _make_client()

        with pytest.raises(RuntimeError, match="API error"):
            client.get_issue_comments_strict("owner/repo", 4787)

        assert mock_api.call_args.kwargs["headers"] == {"Cache-Control": "no-cache"}
