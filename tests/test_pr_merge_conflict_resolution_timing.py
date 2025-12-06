"""Tests for PR merge conflict resolution timing and mergeability polling."""

from unittest.mock import Mock, patch

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

    @patch("src.auto_coder.pr_processor.get_gh_logger")
    def test_poll_returns_true_when_mergeable(self, mock_get_gh_logger, config):
        """Test that polling returns True when PR becomes mergeable."""
        # Setup
        mock_logger = Mock()
        mock_get_gh_logger.return_value = mock_logger

        # Simulate GitHub returning mergeable=true after 2 attempts
        mock_logger.execute_with_logging.side_effect = [
            Mock(
                success=True,
                stdout='{"mergeable": null, "mergeStateStatus": "UNKNOWN"}',
            ),  # First attempt
            Mock(success=True, stdout='{"mergeable": true, "mergeStateStatus": "CLEAN"}'),  # Second attempt - becomes mergeable
        ]

        # Execute
        result = _poll_pr_mergeable("owner/repo", 123, config, timeout_seconds=15, interval=1)

        # Verify
        assert result is True
        # Should have called execute_with_logging twice (once null, once true)
        assert mock_logger.execute_with_logging.call_count == 2

    @patch("src.auto_coder.pr_processor.get_gh_logger")
    def test_poll_returns_false_on_timeout(self, mock_get_gh_logger, config):
        """Test that polling returns False when timeout is reached."""
        # Setup
        mock_logger = Mock()
        mock_get_gh_logger.return_value = mock_logger

        # Simulate GitHub never returning mergeable=true
        mock_logger.execute_with_logging.return_value = Mock(success=True, stdout='{"mergeable": null, "mergeStateStatus": "UNKNOWN"}')

        # Execute with very short timeout
        result = _poll_pr_mergeable("owner/repo", 123, config, timeout_seconds=2, interval=1)

        # Verify
        assert result is False
        # Should have made multiple attempts within timeout
        assert mock_logger.execute_with_logging.call_count >= 2

    @patch("src.auto_coder.pr_processor.get_gh_logger")
    def test_poll_handles_api_errors_gracefully(self, mock_get_gh_logger, config):
        """Test that polling handles API errors gracefully and returns False."""
        # Setup
        mock_logger = Mock()
        mock_get_gh_logger.return_value = mock_logger

        # Simulate API error
        mock_logger.execute_with_logging.return_value = Mock(success=False, stdout="", stderr="API error")

        # Execute
        result = _poll_pr_mergeable("owner/repo", 123, config, timeout_seconds=2, interval=1)

        # Verify
        assert result is False

    @patch("src.auto_coder.pr_processor.get_gh_logger")
    def test_poll_immediately_returns_true_if_already_mergeable(self, mock_get_gh_logger, config):
        """Test that polling returns True immediately if PR is already mergeable."""
        # Setup
        mock_logger = Mock()
        mock_get_gh_logger.return_value = mock_logger

        # Simulate GitHub returning mergeable=true on first attempt
        mock_logger.execute_with_logging.return_value = Mock(success=True, stdout='{"mergeable": true, "mergeStateStatus": "CLEAN"}')

        # Execute
        result = _poll_pr_mergeable("owner/repo", 123, config, timeout_seconds=60, interval=5)

        # Verify
        assert result is True
        # Should only call once since it succeeded immediately
        assert mock_logger.execute_with_logging.call_count == 1
