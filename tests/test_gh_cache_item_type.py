from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.auto_coder.util.gh_cache import GitHubClient


class TestGetItemTypeStrict:
    """get_item_type_strict() must never rely on the shared hishel cache.

    It is the authoritative safety gate that decides whether a target may be
    dispatched through Issue implementation. A stale cached response could
    reintroduce the regression this check exists to prevent, so it must talk to
    GitHub directly via a plain httpx.Client rather than get_caching_client()
    / get_ghapi_client().
    """

    def test_bypasses_the_shared_caching_client_for_an_issue(self):
        client = GitHubClient(token="secret-token")

        mock_response = MagicMock()
        mock_response.json.return_value = {"number": 300, "title": "Genuine issue"}
        mock_response.raise_for_status.return_value = None

        with (
            patch("src.auto_coder.util.gh_cache.get_caching_client") as mock_get_caching_client,
            patch("httpx.Client") as mock_client_cls,
        ):
            mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_response

            result = client.get_item_type_strict("owner/repo", 300)

        assert result == "issue"
        mock_get_caching_client.assert_not_called()
        mock_client_cls.return_value.__enter__.return_value.get.assert_called_once()
        called_url = mock_client_cls.return_value.__enter__.return_value.get.call_args[0][0]
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

        with patch("httpx.Client") as mock_client_cls:
            mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_response

            result = client.get_item_type_strict("owner/repo", 5266)

        assert result == "pr"

    def test_raises_on_a_number_mismatch_instead_of_defaulting_to_issue(self):
        client = GitHubClient(token="secret-token")

        mock_response = MagicMock()
        mock_response.json.return_value = {"number": 999}
        mock_response.raise_for_status.return_value = None

        with patch("httpx.Client") as mock_client_cls:
            mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_response

            with pytest.raises(ValueError):
                client.get_item_type_strict("owner/repo", 5266)

    def test_raises_on_http_error_instead_of_defaulting_to_issue(self):
        client = GitHubClient(token="secret-token")

        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError("not found", request=MagicMock(), response=MagicMock(status_code=404))

        with patch("httpx.Client") as mock_client_cls:
            mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_response

            with pytest.raises(httpx.HTTPStatusError):
                client.get_item_type_strict("owner/repo", 5266)
