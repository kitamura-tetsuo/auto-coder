import unittest
from unittest.mock import Mock, patch

import httpx

from auto_coder.util.gh_cache import GitHubClient


class TestGetPRDiff(unittest.TestCase):
    @patch("auto_coder.util.gh_cache.get_caching_client")
    @patch.dict("os.environ", {"GITHUB_TOKEN": "test_token"})
    def test_get_pr_diff_returns_text(self, mock_get_client):
        # Setup
        mock_client_instance = Mock()
        request = httpx.Request("GET", "https://api.github.com/repos/owner/repo/pulls/123")
        mock_response = httpx.Response(200, text="diff content", request=request)
        mock_client_instance.request.return_value = mock_response
        mock_get_client.return_value = mock_client_instance

        client = GitHubClient("test_token")

        # Execute
        diff = client.get_pr_diff("owner/repo", 123)

        # Verify
        assert diff == "diff content"
        mock_client_instance.request.assert_called_once()
        args, kwargs = mock_client_instance.request.call_args
        assert args[0] == "GET"
        assert args[1] == "https://api.github.com/repos/owner/repo/pulls/123"
        assert kwargs["headers"]["Accept"] == "application/vnd.github.v3.diff"
        assert kwargs["headers"]["Authorization"] == "bearer test_token"

    @patch("auto_coder.util.gh_cache.get_caching_client")
    def test_get_pr_diff_handles_error(self, mock_get_client):
        # Setup
        mock_client_instance = Mock()
        mock_client_instance.request.side_effect = Exception("API error")
        mock_get_client.return_value = mock_client_instance

        client = GitHubClient("test_token")

        # Execute
        diff = client.get_pr_diff("owner/repo", 123)

        # Verify
        assert diff == ""
