from unittest.mock import Mock, patch

import pytest

from src.auto_coder.util.gh_cache import get_ghapi_client


class TestGhCacheRedirect:

    @patch("src.auto_coder.util.gh_cache.get_caching_client")
    def test_api_call_with_caching_client(self, mock_get_client):
        """Test that API calls use the caching client correctly."""

        # Mock client and response
        mock_client_instance = Mock()
        mock_get_client.return_value = mock_client_instance

        # Mock response
        resp = Mock()
        resp.status_code = 200
        resp.is_redirect = False
        resp.headers = {"Content-Type": "application/json"}
        resp.json.return_value = {"success": True}
        resp.text = ""

        mock_client_instance.request.return_value = resp

        # Initialize API
        token = "secret-token"
        api = get_ghapi_client(token)

        path = "/repos/owner/repo/actions/runs/123/logs"
        # Call API with path, verb, headers, route, query, data
        result = api(path, "GET", {"authorization": f"token {token}", "x-test": "keep"}, None, {"foo": "bar"}, None)

        # Verify the caching client was used
        assert mock_client_instance.request.call_count == 1

        # Verify the request was made with correct parameters
        args, kwargs = mock_client_instance.request.call_args
        assert kwargs["method"] == "GET"
        assert kwargs["url"] == "https://api.github.com" + path
        assert kwargs["headers"]["authorization"] == f"token {token}"
        assert kwargs["headers"]["x-test"] == "keep"
        assert kwargs["params"] == {"foo": "bar"}

        # Verify result
        assert result == {"success": True}

    @patch("src.auto_coder.util.gh_cache.get_caching_client")
    def test_api_call_returns_zip_content(self, mock_get_client):
        """Test that API calls correctly handle ZIP content (binary response)."""

        mock_client_instance = Mock()
        mock_get_client.return_value = mock_client_instance

        # Mock ZIP content response - configure headers mock properly
        resp = Mock()
        resp.status_code = 200
        resp.is_redirect = False
        # Mock headers.get() to return the content type (case-insensitive)
        resp.headers = Mock()
        resp.headers.get = Mock(return_value="application/zip")
        resp.content = b"zip-content-binary-data"
        resp.text = ""
        # json() should raise an exception for non-JSON responses
        resp.json.side_effect = Exception("Not JSON")

        mock_client_instance.request.return_value = resp

        token = "secret-token"
        api = get_ghapi_client(token)
        path = "/repos/owner/repo/actions/runs/123/logs"

        result = api(path, "GET", {}, None, {}, None)

        # Verify ZIP content is returned as bytes
        assert result == b"zip-content-binary-data"

    @patch("src.auto_coder.util.gh_cache.get_caching_client")
    def test_api_call_with_relative_path(self, mock_get_client):
        """Test that relative paths are correctly prefixed with gh_host."""

        mock_client_instance = Mock()
        mock_get_client.return_value = mock_client_instance

        resp = Mock()
        resp.status_code = 200
        resp.is_redirect = False
        resp.headers = {"Content-Type": "application/json"}
        resp.json.return_value = {"data": "test"}
        resp.text = ""

        mock_client_instance.request.return_value = resp

        token = "test-token"
        api = get_ghapi_client(token)
        path = "/user/repos"

        api(path, "GET", {}, None, {}, None)

        # Verify URL was constructed with gh_host
        args, kwargs = mock_client_instance.request.call_args
        assert kwargs["url"] == "https://api.github.com/user/repos"
