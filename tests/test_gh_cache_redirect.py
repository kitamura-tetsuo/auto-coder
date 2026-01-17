from unittest.mock import Mock, patch

import httpx
import pytest

from src.auto_coder.util.gh_cache import get_ghapi_client


class TestGhCacheRedirect:

    @patch("src.auto_coder.util.gh_cache.get_caching_client")
    def test_redirect_strips_auth_header(self, mock_get_client):
        """Test that Authorization header is stripped on cross-origin redirect."""

        # Mock client and responses
        mock_client_instance = Mock()
        mock_get_client.return_value = mock_client_instance

        # Response 1: 302 Redirect to different domain
        resp1 = Mock()
        resp1.status_code = 302
        resp1.is_redirect = True
        resp1.headers = {"Location": "https://other-domain.com/blob"}
        resp1.read.return_value = None

        # Response 2: 200 OK from other domain
        resp2 = Mock()
        resp2.status_code = 200
        resp2.is_redirect = False
        resp2.headers = {"Content-Type": "application/zip"}
        resp2.content = b"zip-content"
        resp2.text = ""
        resp2.json.side_effect = Exception("Not JSON")

        mock_client_instance.request.side_effect = [resp1, resp2]

        # Initialize API
        token = "secret-token"
        api = get_ghapi_client(token)

        # Call API using __call__ method (the correct ghapi interface)
        # ghapi signature: __call__(path, verb=None, headers=None, route=None, query=None, data=None, timeout=None, decode=True)
        path = "/repos/owner/repo/actions/runs/123/logs"
        # path, verb, headers, route, query, data
        api(path, "GET", {"authorization": f"token {token}", "x-test": "keep"}, None, {"foo": "bar"}, None)

        # Verify calls
        assert mock_client_instance.request.call_count == 2

        # First call: To GitHub API, with Auth header and query
        args1, kwargs1 = mock_client_instance.request.call_args_list[0]
        # In adapter: url = f"{self.gh_host}{path}"
        assert kwargs1["url"] == "https://api.github.com" + path
        assert kwargs1["headers"]["authorization"] == f"token {token}"
        assert kwargs1["headers"]["x-test"] == "keep"
        assert kwargs1["params"] == {"foo": "bar"}

        # Second call: To Other Domain, WITHOUT Auth header and WITHOUT query
        args2, kwargs2 = mock_client_instance.request.call_args_list[1]
        assert kwargs2["url"] == "https://other-domain.com/blob"
        # Check case-insensitive removal
        assert "authorization" not in kwargs2["headers"]
        assert "Authorization" not in kwargs2["headers"]
        assert kwargs2["headers"]["x-test"] == "keep"  # Should preserve other headers

        # Query params should be stripped (empty dict)
        assert kwargs2["params"] == {}

    @patch("src.auto_coder.util.gh_cache.get_caching_client")
    def test_redirect_preserves_auth_header_same_origin(self, mock_get_client):
        """Test that Authorization header is preserved on same-origin redirect."""

        mock_client_instance = Mock()
        mock_get_client.return_value = mock_client_instance

        # Response 1: 302 Redirect to same domain
        resp1 = Mock()
        resp1.status_code = 302
        resp1.is_redirect = True
        resp1.headers = {"Location": "/repos/owner/repo/other/path"}
        resp1.read.return_value = None

        # Response 2: 200 OK
        resp2 = Mock()
        resp2.status_code = 200
        resp2.is_redirect = False
        resp2.headers = {"Content-Type": "application/json"}
        resp2.json.return_value = {"success": True}

        mock_client_instance.request.side_effect = [resp1, resp2]

        token = "secret-token"
        api = get_ghapi_client(token)
        path = "/repos/owner/repo/path"

        # Call API using __call__ method (the correct ghapi interface)
        # path, verb, headers, route, query, data
        api(path, "GET", {"Authorization": f"token {token}"}, None, {}, None)

        assert mock_client_instance.request.call_count == 2

        # Second call: To Same Domain (reconstructed from relative path), WITH Auth header
        args2, kwargs2 = mock_client_instance.request.call_args_list[1]
        assert "api.github.com" in kwargs2["url"]
        assert "Authorization" in kwargs2["headers"]
