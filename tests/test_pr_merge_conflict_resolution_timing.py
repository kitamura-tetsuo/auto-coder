"""Tests for PR merge conflict resolution timing and mergeability polling."""

from unittest.mock import Mock, patch

import pytest

import src.auto_coder.pr_processor as pr_processor
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

    def test_poll_returns_true_when_mergeable(self, config):
        """Test that polling returns True when PR becomes mergeable."""
        # Setup - configure the GitHubClient mock
        mock_instance = Mock()
        mock_instance.token = "test_token"

        with patch.object(pr_processor, "GitHubClient") as mock_github_client_class:
            mock_github_client_class.get_instance.return_value = mock_instance

            with patch("auto_coder.util.gh_cache.get_ghapi_client") as mock_get_api:
                mock_api = Mock()
                mock_get_api.return_value = mock_api

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

    def test_poll_returns_false_on_timeout(self, config):
        """Test that polling returns False when timeout is reached."""
        # Setup - configure the GitHubClient mock
        mock_instance = Mock()
        mock_instance.token = "test_token"

        with patch.object(pr_processor, "GitHubClient") as mock_github_client_class:
            mock_github_client_class.get_instance.return_value = mock_instance

            with patch("auto_coder.util.gh_cache.get_ghapi_client") as mock_get_api:
                mock_api = Mock()
                mock_get_api.return_value = mock_api

                # Simulate GitHub never returning mergeable=true
                mock_api.pulls.get.return_value = {"mergeable": None, "mergeStateStatus": "UNKNOWN"}

                # Execute with very short timeout
                result = _poll_pr_mergeable("owner/repo", 123, config, timeout_seconds=2, interval=1)

                # Verify
                assert result is False
                # Should have made multiple attempts within timeout
                assert mock_api.pulls.get.call_count >= 2

    def test_poll_handles_api_errors_gracefully(self, config):
        """Test that polling handles API errors gracefully and returns False."""
        # Setup - configure the GitHubClient mock
        mock_instance = Mock()
        mock_instance.token = "test_token"

        with patch.object(pr_processor, "GitHubClient") as mock_github_client_class:
            mock_github_client_class.get_instance.return_value = mock_instance

            with patch("auto_coder.util.gh_cache.get_ghapi_client") as mock_get_api:
                mock_api = Mock()
                mock_get_api.return_value = mock_api

                # Simulate API error
                mock_api.pulls.get.side_effect = Exception("API error")

                # Execute
                result = _poll_pr_mergeable("owner/repo", 123, config, timeout_seconds=2, interval=1)

                # Verify
                assert result is False

    def test_poll_immediately_returns_true_if_already_mergeable(self, config):
        """Test that polling returns True immediately if PR is already mergeable."""
        # Setup - configure the GitHubClient mock
        mock_instance = Mock()
        mock_instance.token = "test_token"

        with patch.object(pr_processor, "GitHubClient") as mock_github_client_class:
            mock_github_client_class.get_instance.return_value = mock_instance

            with patch("auto_coder.util.gh_cache.get_ghapi_client") as mock_get_api:
                mock_api = Mock()
                mock_get_api.return_value = mock_api

                # Simulate GitHub returning mergeable=true on first attempt
                mock_api.pulls.get.return_value = {"mergeable": True, "mergeStateStatus": "CLEAN"}

                # Execute
                result = _poll_pr_mergeable("owner/repo", 123, config, timeout_seconds=60, interval=5)

                # Verify
                assert result is True
                # Should only call once since it succeeded immediately
                assert mock_api.pulls.get.call_count == 1
