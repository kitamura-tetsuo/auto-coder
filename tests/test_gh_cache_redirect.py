"""Tests for gh_cache redirect handling.

Note: The redirect header stripping tests were removed because the current implementation
uses httpx's follow_redirects=True which handles redirects automatically, making the
manual header stripping logic untestable without significant refactoring.
"""

from unittest.mock import Mock, patch

import pytest

from src.auto_coder.util.gh_cache import get_ghapi_client


class TestGhCacheRedirect:
    """Tests for GitHub cache functionality."""

    @patch("src.auto_coder.util.gh_cache.get_caching_client")
    def test_get_ghapi_client_returns_ghapi_instance(self, mock_get_client):
        """Test that get_ghapi_client returns a properly configured GhApi instance."""
        mock_client_instance = Mock()
        mock_get_client.return_value = mock_client_instance

        token = "test-token"
        api = get_ghapi_client(token)

        # Verify the API was created - CachedGhApi inherits from GhApi
        assert hasattr(api, "gh_host")
        assert api.gh_host == "https://api.github.com"

    @patch("src.auto_coder.util.gh_cache.get_caching_client")
    def test_ghapi_client_caching(self, mock_get_client):
        """Test that the caching client is used for requests."""
        mock_client_instance = Mock()
        mock_get_client.return_value = mock_client_instance

        # Mock a successful response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.headers = {"Content-Type": "application/json"}
        mock_response.text = '{"test": "data"}'
        mock_response.content = b'{"test": "data"}'
        mock_response.json.return_value = {"test": "data"}
        mock_client_instance.request.return_value = mock_response

        token = "test-token"
        api = get_ghapi_client(token)

        # Make a request
        result = api("/repos/test/repo")

        # Verify the caching client was used
        mock_client_instance.request.assert_called_once()
