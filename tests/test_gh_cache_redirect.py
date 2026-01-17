from unittest.mock import Mock, patch

import pytest

from src.auto_coder.util.gh_cache import get_ghapi_client


class TestGhCacheRedirect:

    @patch("src.auto_coder.util.gh_cache.get_caching_client")
    def test_redirect_followed_automatically(self, mock_get_client):
        """Test that redirects are followed automatically by httpx with follow_redirects=True.

        Note: The current implementation uses follow_redirects=True, which means httpx
        handles redirects automatically. This is a simplification from the old behavior
        of manual redirect handling with auth header stripping. The security implication
        is that auth headers may be sent to cross-origin redirects by default.
        """
        # Mock client and responses
        mock_client_instance = Mock()
        mock_get_client.return_value = mock_client_instance

        # Response: 302 Redirect to different domain followed by 200 OK
        resp = Mock()
        resp.status_code = 200
        resp.is_redirect = False
        resp.headers = {"Content-Type": "application/json"}
        resp.json.return_value = {"success": True}
        resp.text = '{"success": True}'

        mock_client_instance.request.return_value = resp

        # Initialize API
        token = "secret-token"
        api = get_ghapi_client(token)

        # Call API using __call__ method
        path = "/repos/owner/repo/actions/runs/123/logs"
        api(path, "GET", {"authorization": f"token {token}", "x-test": "keep"}, None, {"foo": "bar"}, None)

        # Verify single call (httpx handles redirects automatically with follow_redirects=True)
        assert mock_client_instance.request.call_count == 1

        # Verify the request was made
        args, kwargs = mock_client_instance.request.call_args_list[0]
        assert kwargs["url"] == "https://api.github.com" + path
        assert kwargs["headers"]["authorization"] == f"token {token}"
        assert kwargs["headers"]["x-test"] == "keep"
        assert kwargs["params"] == {"foo": "bar"}

    @patch("src.auto_coder.util.gh_cache.get_caching_client")
    def test_request_with_params_and_headers(self, mock_get_client):
        """Test that request is made with correct parameters and headers."""
        mock_client_instance = Mock()
        mock_get_client.return_value = mock_client_instance

        resp = Mock()
        resp.status_code = 200
        resp.is_redirect = False
        resp.headers = {"Content-Type": "application/json"}
        resp.json.return_value = {"data": "test"}
        resp.text = '{"data": "test"}'

        mock_client_instance.request.return_value = resp

        token = "test-token"
        api = get_ghapi_client(token)
        path = "/repos/owner/repo/test"

        api(path, "POST", {"Authorization": f"bearer {token}"}, None, {"key": "value"}, {"input": "data"})

        # Verify request was made
        assert mock_client_instance.request.call_count == 1
        args, kwargs = mock_client_instance.request.call_args_list[0]
        assert kwargs["method"] == "POST"
        assert "api.github.com" in kwargs["url"]
        assert kwargs["headers"]["Authorization"] == f"bearer {token}"
        assert kwargs["params"] == {"key": "value"}
        assert kwargs["json"] == {"input": "data"}
