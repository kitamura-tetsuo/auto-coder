from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.auto_coder.util.gh_cache import GitHubClient, resolve_authoritative_item_type


class TestGetItemTypeStrict:
    """Strict readers validate responses while using the shared HTTP cache."""

    def test_uses_the_shared_caching_client_for_an_issue(self):
        client = GitHubClient(token="secret-token")

        mock_response = MagicMock()
        mock_response.json.return_value = {"number": 300, "title": "Genuine issue"}
        mock_response.raise_for_status.return_value = None

        with (
            patch("src.auto_coder.util.gh_cache.get_caching_client") as mock_get_caching_client,
            patch("src.auto_coder.util.gh_cache._caching_request") as mock_client_cls,
        ):
            mock_client_cls.return_value = mock_response

            result = client.get_item_type_strict("owner/repo", 300)

        assert result == "issue"
        mock_get_caching_client.assert_called_once_with(subsystem="controller-issue-read")
        mock_client_cls.assert_called_once()
        called_url = mock_client_cls.call_args[0][2]
        assert called_url == "https://api.github.com/repos/owner/repo/issues/300"

    def test_detects_a_pull_request_via_the_pull_request_field(self):
        client = GitHubClient(token="secret-token")

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "number": 5266,
            "title": "Cloud-created PR",
            "pull_request": {"url": "https://api.github.com/repos/owner/repo/pulls/5266"},
        }
        mock_response.raise_for_status.return_value = None

        with patch("src.auto_coder.util.gh_cache._caching_request") as mock_client_cls:
            mock_client_cls.return_value = mock_response

            result = client.get_item_type_strict("owner/repo", 5266)

        assert result == "pr"

    def test_raises_on_a_number_mismatch_instead_of_defaulting_to_issue(self):
        client = GitHubClient(token="secret-token")

        mock_response = MagicMock()
        mock_response.json.return_value = {"number": 999}
        mock_response.raise_for_status.return_value = None

        with patch("src.auto_coder.util.gh_cache._caching_request") as mock_client_cls:
            mock_client_cls.return_value = mock_response

            with pytest.raises(ValueError):
                client.get_item_type_strict("owner/repo", 5266)

    def test_raises_on_http_error_instead_of_defaulting_to_issue(self):
        client = GitHubClient(token="secret-token")

        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError("not found", request=MagicMock(), response=MagicMock(status_code=404))

        with patch("src.auto_coder.util.gh_cache._caching_request") as mock_client_cls:
            mock_client_cls.return_value = mock_response

            with pytest.raises(httpx.HTTPStatusError):
                client.get_item_type_strict("owner/repo", 5266)


class TestResolveAuthoritativeItemType:
    """resolve_authoritative_item_type() must never fall back to a cached response.

    A client that cannot perform the strict validated lookup has not established the
    type; it must not be treated as confirmation the target is an Issue.
    """

    def test_uses_get_item_type_strict_when_available(self):
        client = MagicMock()
        client.get_item_type_strict.return_value = "pr"

        result = resolve_authoritative_item_type(client, "owner/repo", 5266)

        assert result == "pr"
        client.get_item_type_strict.assert_called_once_with("owner/repo", 5266)

    def test_raises_when_get_item_type_strict_returns_an_unrecognized_value(self):
        client = MagicMock()
        client.get_item_type_strict.return_value = "something-else"

        with pytest.raises(ValueError):
            resolve_authoritative_item_type(client, "owner/repo", 5266)


class TestStrictIssueHierarchy:
    def test_open_children_bypass_warmed_empty_cache(self):
        client = GitHubClient(token="secret-token")
        client._sub_issue_cache[("owner/repo", 20)] = []
        response = MagicMock()
        response.json.return_value = [{"number": 21, "state": "open"}]
        response.links = {}
        response.raise_for_status.return_value = None
        with patch("src.auto_coder.util.gh_cache._caching_request") as client_class:
            client_class.return_value = response
            assert client.get_open_sub_issues_strict("owner/repo", 20) == [21]
        client_class.assert_called_once()

    def test_open_children_failure_is_not_flattened_to_empty(self):
        client = GitHubClient(token="secret-token")
        response = MagicMock()
        response.raise_for_status.side_effect = RuntimeError("hierarchy unavailable")
        with patch("src.auto_coder.util.gh_cache._caching_request") as client_class, patch("time.sleep"):
            client_class.return_value = response
            with pytest.raises(RuntimeError, match="hierarchy unavailable"):
                client.get_open_sub_issues_strict("owner/repo", 20)

    def test_native_parent_identity_is_read_without_cached_issue_data(self):
        client = GitHubClient(token="secret-token")
        response = MagicMock(status_code=200)
        response.json.return_value = {"number": 10, "state": "open"}
        response.raise_for_status.return_value = None
        with patch("src.auto_coder.util.gh_cache._caching_request") as client_class:
            client_class.return_value = response
            assert client.get_parent_issue_number_strict("owner/repo", 20) == 10
        called_url = client_class.call_args.args[2]
        assert called_url.endswith("/issues/20/parent")

    def test_raises_instead_of_falling_back_to_a_cached_stale_issue_response(self):
        """A client lacking the authoritative lookup must fail closed, not use get_issue().

        get_issue() here returns a stale, issue-shaped object for a number GitHub
        currently identifies as a pull request. Because the client has no
        get_item_type_strict method, resolve_authoritative_item_type must raise rather
        than trust that cached representation -- and no caller may treat this as
        confirmation the target is an Issue.
        """

        class GitHubClientWithoutStrictLookup:
            def get_issue(self, repo_name, item_number):
                return {"number": item_number, "title": "Actually a PR now", "state": "open"}

        client = GitHubClientWithoutStrictLookup()

        with pytest.raises(ValueError):
            resolve_authoritative_item_type(client, "owner/repo", 5266)
