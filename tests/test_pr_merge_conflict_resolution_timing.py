"""Tests for PR merge conflict resolution timing and mergeability polling."""

from unittest.mock import MagicMock, Mock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.pr_processor import _poll_pr_mergeable


class TestPollPrMergeable:
    """Test the _poll_pr_mergeable function."""

    @pytest.fixture
    def config(self):
        """Create a test configuration."""
        config = AutomationConfig()
        config.MERGE_METHOD = "--squash"
        config.MAIN_BRANCH = "main"
        return config

    @patch("src.auto_coder.pr_processor.GitHubClient")
    @patch("auto_coder.util.gh_cache.get_ghapi_client")
    def test_poll_returns_true_when_mergeable(self, mock_get_ghapi_client, mock_github_client_class, config):
        """Test that polling returns True when PR becomes mergeable."""
        # Setup
        mock_github_client = MagicMock()
        mock_github_client.token = "test-token"
        mock_github_client_class.get_instance.return_value = mock_github_client

        # Mock API
        mock_api = MagicMock()
        mock_get_ghapi_client.return_value = mock_api

        # Simulate GitHub returning mergeable=true after 2 attempts
        mock_api.pulls.get.side_effect = [
            {"mergeable": None, "mergeStateStatus": "UNKNOWN"},  # First attempt
            {"mergeable": True, "mergeStateStatus": "CLEAN"},  # Second attempt - becomes mergeable
        ]

        # Execute
        result = _poll_pr_mergeable("owner/repo", 123, config, timeout_seconds=15, interval=1)

        # Verify
        assert result is True
        # Should have called pulls.get twice (once null, once true)
        assert mock_api.pulls.get.call_count == 2

    @patch("src.auto_coder.pr_processor.GitHubClient")
    @patch("auto_coder.util.gh_cache.get_ghapi_client")
    def test_poll_returns_false_on_timeout(self, mock_get_ghapi_client, mock_github_client_class, config):
        """Test that polling returns False when timeout is reached."""
        # Setup
        mock_github_client = MagicMock()
        mock_github_client.token = "test-token"
        mock_github_client_class.get_instance.return_value = mock_github_client

        # Mock API - always return null mergeable
        mock_api = MagicMock()
        mock_api.pulls.get.return_value = {"mergeable": None, "mergeStateStatus": "UNKNOWN"}
        mock_get_ghapi_client.return_value = mock_api

        # Execute with very short timeout
        result = _poll_pr_mergeable("owner/repo", 123, config, timeout_seconds=3, interval=1)

        # Verify
        assert result is False
        # Should have made multiple attempts within timeout
        assert mock_api.pulls.get.call_count >= 2

    @patch("src.auto_coder.pr_processor.GitHubClient")
    @patch("auto_coder.util.gh_cache.get_ghapi_client")
    def test_poll_handles_api_errors_gracefully(self, mock_get_ghapi_client, mock_github_client_class, config):
        """Test that polling handles API errors gracefully and returns False."""
        # Setup
        mock_github_client = MagicMock()
        mock_github_client.token = "test-token"
        mock_github_client_class.get_instance.return_value = mock_github_client

        # Mock API to raise exception
        mock_api = MagicMock()
        mock_api.pulls.get.side_effect = Exception("API error")
        mock_get_ghapi_client.return_value = mock_api

        # Execute
        result = _poll_pr_mergeable("owner/repo", 123, config, timeout_seconds=3, interval=1)

        # Verify
        assert result is False

    @patch("src.auto_coder.pr_processor.GitHubClient")
    @patch("auto_coder.util.gh_cache.get_ghapi_client")
    def test_poll_immediately_returns_true_if_already_mergeable(self, mock_get_ghapi_client, mock_github_client_class, config):
        """Test that polling returns True immediately if PR is already mergeable."""
        # Setup
        mock_github_client = MagicMock()
        mock_github_client.token = "test-token"
        mock_github_client_class.get_instance.return_value = mock_github_client

        # Mock API - return mergeable=true on first attempt
        mock_api = MagicMock()
        mock_api.pulls.get.return_value = {"mergeable": True, "mergeStateStatus": "CLEAN"}
        mock_get_ghapi_client.return_value = mock_api

        # Execute
        result = _poll_pr_mergeable("owner/repo", 123, config, timeout_seconds=60, interval=5)

        # Verify
        assert result is True
        # Should only call once since it succeeded immediately
        assert mock_api.pulls.get.call_count == 1
