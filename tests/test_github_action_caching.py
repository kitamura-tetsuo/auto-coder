"""Tests for GitHub Action Caching Integration."""

import unittest
from unittest.mock import MagicMock, patch

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.util.github_action import _check_github_actions_status, _check_github_actions_status_from_history
from src.auto_coder.util.github_cache import get_github_cache


class TestGitHubActionCaching(unittest.TestCase):
    """Test cases for GitHub Action caching integration."""

    def setUp(self):
        """Set up test fixtures."""
        get_github_cache().clear()
        self.config = AutomationConfig()

    @patch("src.auto_coder.util.github_action.get_gh_logger")
    def test_check_github_actions_status_uses_cache(self, mock_get_gh_logger):
        """Test that _check_github_actions_status uses the cache."""
        repo_name = "owner/repo"
        pr_data = {"number": 123, "head": {"sha": "abc1234"}}

        # Pre-populate cache
        cache = get_github_cache()
        cache_key = f"gh_actions_status:{repo_name}:abc1234"
        expected_result = MagicMock()
        cache.set(cache_key, expected_result)

        # Call function
        result = _check_github_actions_status(repo_name, pr_data, self.config)

        # Verify result is from cache
        self.assertIs(result, expected_result)

        # Verify API was NOT called (mock logger shouldn't be used)
        mock_get_gh_logger.assert_not_called()

    @patch("src.auto_coder.util.github_action.get_gh_logger")
    def test_check_github_actions_status_populates_cache(self, mock_get_gh_logger):
        """Test that _check_github_actions_status populates the cache on miss."""
        repo_name = "owner/repo"
        pr_data = {"number": 124, "head": {"sha": "def5678"}}

        # Mock API response
        mock_logger_instance = MagicMock()
        mock_get_gh_logger.return_value = mock_logger_instance
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"check_runs": []}'  # Empty checks
        mock_logger_instance.execute_with_logging.return_value = mock_result

        # Call function
        result = _check_github_actions_status(repo_name, pr_data, self.config)

        # Verify cache is populated
        cache = get_github_cache()
        cache_key = f"gh_actions_status:{repo_name}:def5678"
        cached_result = cache.get(cache_key)

        self.assertIsNotNone(cached_result)
        self.assertEqual(cached_result.success, True)  # Empty checks = success in this logic

    @patch("src.auto_coder.util.github_action.get_gh_logger")
    def test_check_github_actions_status_from_history_uses_cache(self, mock_get_gh_logger):
        """Test that _check_github_actions_status_from_history uses the cache."""
        repo_name = "owner/repo"
        pr_data = {"number": 125, "head_branch": "feature-branch"}

        # Pre-populate cache
        cache = get_github_cache()
        cache_key = f"gh_actions_history:{repo_name}:125:feature-branch"
        expected_result = MagicMock()
        cache.set(cache_key, expected_result)

        # Call function
        result = _check_github_actions_status_from_history(repo_name, pr_data, self.config)

        # Verify result is from cache
        self.assertIs(result, expected_result)

        # Verify API was NOT called
        mock_get_gh_logger.assert_not_called()


if __name__ == "__main__":
    unittest.main()
