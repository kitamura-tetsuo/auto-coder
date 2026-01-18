"""Tests for PR merge conflict resolution timing and mergeability polling."""

from unittest.mock import Mock, patch

import pytest

from src.auto_coder import pr_processor
from src.auto_coder.automation_config import AutomationConfig


class TestPollPrMergeable:
    """Test the _poll_pr_mergeable function."""

    @pytest.fixture
    def config(self):
        """Create a test configuration."""
        config = AutomationConfig()
        config.MERGE_METHOD = "--squash"
        config.MAIN_BRANCH = "main"
        return config

    @pytest.fixture
    def mock_ghapi_client(self):
        """Create a mock GitHub API client."""
        return Mock()

    def test_poll_returns_true_when_mergeable(self, config, mock_ghapi_client):
        """Test that polling returns True when PR becomes mergeable."""
        mock_github_client_class = Mock()
        mock_github_client = Mock()
        mock_github_client.token = "test_token"
        mock_github_client_class.get_instance.return_value = mock_github_client

        # Simulate GitHub returning mergeable=true after 2 attempts
        mock_ghapi_client.pulls.get.side_effect = [
            {"mergeable": None, "mergeStateStatus": "UNKNOWN"},  # First attempt
            {"mergeable": True, "mergeStateStatus": "CLEAN"},  # Second attempt - becomes mergeable
        ]

        with patch.object(pr_processor, "GitHubClient", mock_github_client_class), patch("auto_coder.util.gh_cache.get_ghapi_client", return_value=mock_ghapi_client), patch("time.sleep"):
            # Execute
            result = pr_processor._poll_pr_mergeable("owner/repo", 123, config, timeout_seconds=15, interval=0.1)

        # Verify
        assert result is True
        # Should have called pulls.get twice (once null, once true)
        assert mock_ghapi_client.pulls.get.call_count == 2

    def test_poll_returns_false_on_timeout(self, config, mock_ghapi_client):
        """Test that polling returns False when timeout is reached."""
        mock_github_client_class = Mock()
        mock_github_client = Mock()
        mock_github_client.token = "test_token"
        mock_github_client_class.get_instance.return_value = mock_github_client

        # Simulate GitHub never returning mergeable=true
        mock_ghapi_client.pulls.get.return_value = {
            "mergeable": None,
            "mergeStateStatus": "UNKNOWN",
        }

        with patch.object(pr_processor, "GitHubClient", mock_github_client_class), patch("auto_coder.util.gh_cache.get_ghapi_client", return_value=mock_ghapi_client), patch("time.sleep"):
            # Execute with very short timeout
            result = pr_processor._poll_pr_mergeable("owner/repo", 123, config, timeout_seconds=0.5, interval=0.1)

        # Verify
        assert result is False
        # Should have made multiple attempts within timeout
        assert mock_ghapi_client.pulls.get.call_count >= 2

    def test_poll_handles_api_errors_gracefully(self, config, mock_ghapi_client):
        """Test that polling handles API errors gracefully and returns False."""
        mock_github_client_class = Mock()
        mock_github_client = Mock()
        mock_github_client.token = "test_token"
        mock_github_client_class.get_instance.return_value = mock_github_client

        # Simulate API error
        mock_ghapi_client.pulls.get.side_effect = Exception("API error")

        with patch.object(pr_processor, "GitHubClient", mock_github_client_class), patch("auto_coder.util.gh_cache.get_ghapi_client", return_value=mock_ghapi_client), patch("time.sleep"):
            # Execute
            result = pr_processor._poll_pr_mergeable("owner/repo", 123, config, timeout_seconds=0.5, interval=0.1)

        # Verify
        assert result is False

    def test_poll_immediately_returns_true_if_already_mergeable(self, config, mock_ghapi_client):
        """Test that polling returns True immediately if PR is already mergeable."""
        mock_github_client_class = Mock()
        mock_github_client = Mock()
        mock_github_client.token = "test_token"
        mock_github_client_class.get_instance.return_value = mock_github_client

        # Simulate GitHub returning mergeable=true on first attempt
        mock_ghapi_client.pulls.get.return_value = {
            "mergeable": True,
            "mergeStateStatus": "CLEAN",
        }

        with patch.object(pr_processor, "GitHubClient", mock_github_client_class), patch("auto_coder.util.gh_cache.get_ghapi_client", return_value=mock_ghapi_client), patch("time.sleep"):
            # Execute
            result = pr_processor._poll_pr_mergeable("owner/repo", 123, config, timeout_seconds=60, interval=5)

        # Verify
        assert result is True
        # Should only call once since it succeeded immediately
        assert mock_ghapi_client.pulls.get.call_count == 1
