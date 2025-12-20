import unittest
import os
from unittest.mock import MagicMock, patch

import httpx

from src.auto_coder.github_client import GitHubClient

CACHE_FILE = ".cache/gh/cache.sqlite"


class TestGHCaching(unittest.TestCase):
    def setUp(self):
        GitHubClient.reset_singleton()
        if os.path.exists(CACHE_FILE):
            os.remove(CACHE_FILE)

    @patch("httpx.HTTPTransport.handle_request")
    def test_etag_header_sent_on_second_request(self, mock_handle_request):
        # Mock the first response with an ETag
        mock_response1 = httpx.Response(
            200,
            headers={"ETag": "test-etag"},
            json={"data": "test"},
        )
        mock_response1.request = httpx.Request("GET", "https://api.github.com/users/test")

        # Mock the second response (304 Not Modified)
        mock_response2 = httpx.Response(304)
        mock_response2.request = httpx.Request("GET", "https://api.github.com/users/test")

        # Configure the mock to return the responses in order
        mock_handle_request.side_effect = [mock_response1, mock_response2]

        client = GitHubClient.get_instance(token="test_token")._get_httpx_client()

        # First request
        client.get("https://api.github.com/users/test")

        # Second request
        client.get("https://api.github.com/users/test")

        # Check that the second request had the If-None-Match header
        self.assertEqual(mock_handle_request.call_count, 2)
        second_call_args = mock_handle_request.call_args_list[1].args
        request = second_call_args[0]
        self.assertIn("if-none-match", request.headers)
        self.assertEqual(request.headers["if-none-match"], "test-etag")

    @patch("httpx.HTTPTransport.handle_request")
    def test_cached_response_used_on_304(self, mock_handle_request):
        # Mock the first response with an ETag
        mock_response1 = httpx.Response(
            200,
            headers={"ETag": "test-etag"},
            json={"data": "test"},
        )
        mock_response1.request = httpx.Request("GET", "https://api.github.com/users/test")

        # Mock the second response (304 Not Modified)
        # hishel will see the 304 and return the cached response
        mock_response2 = httpx.Response(304)
        mock_response2.request = httpx.Request("GET", "https://api.github.com/users/test")

        # Configure the mock to return the responses in order
        mock_handle_request.side_effect = [mock_response1, mock_response2]

        client = GitHubClient.get_instance(token="test_token")._get_httpx_client()

        # First request
        result1 = client.get("https://api.github.com/users/test")

        # Second request
        result2 = client.get("https://api.github.com/users/test")

        self.assertEqual(result1.json(), {"data": "test"})
        self.assertEqual(result2.json(), {"data": "test"})
        self.assertEqual(mock_handle_request.call_count, 2)


if __name__ == "__main__":
    unittest.main()
