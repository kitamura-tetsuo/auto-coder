"""Tests for LabelManager context manager."""

from unittest.mock import Mock, patch
import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.github_client import GitHubClient
from src.auto_coder.label_manager import LabelManager


class TestLabelManager:
    """Test LabelManager context manager functionality."""

    def test_label_manager_context_manager_success(self):
        """Test successful label management - add and remove label."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.has_label.return_value = False
        mock_github_client.try_add_work_in_progress_label.return_value = True

        config = AutomationConfig()

        # Use LabelManager context manager
        with LabelManager(
            mock_github_client, "owner/repo", 123, item_type="issue", config=config
        ) as should_process:
            assert should_process is True
            # Label should be added inside context
            mock_github_client.try_add_work_in_progress_label.assert_called_once_with(
                "owner/repo", 123, label="@auto-coder"
            )

        # Label should be removed after exiting context
        mock_github_client.remove_labels_from_issue.assert_called_once_with(
            "owner/repo", 123, ["@auto-coder"]
        )

    def test_label_manager_skips_when_label_already_exists(self):
        """Test that context manager returns False when label already exists."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.has_label.return_value = True  # Label already exists

        config = AutomationConfig()

        # Use LabelManager context manager
        with LabelManager(
            mock_github_client, "owner/repo", 123, item_type="issue", config=config
        ) as should_process:
            assert should_process is False
            # try_add_work_in_progress_label should NOT be called
            mock_github_client.try_add_work_in_progress_label.assert_not_called()

        # Label should NOT be removed (since it wasn't added by us)
        mock_github_client.remove_labels_from_issue.assert_not_called()

    def test_label_manager_dry_run(self):
        """Test that dry_run mode logs but doesn't perform actual operations."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.has_label.return_value = False

        config = AutomationConfig()

        # Use LabelManager context manager with dry_run=True
        with LabelManager(
            mock_github_client, "owner/repo", 123, item_type="issue", dry_run=True, config=config
        ) as should_process:
            assert should_process is True
            # No actual API calls should be made
            mock_github_client.try_add_work_in_progress_label.assert_not_called()

        # No removal should happen
        mock_github_client.remove_labels_from_issue.assert_not_called()

    def test_label_manager_with_labels_disabled_via_client(self):
        """Test that context manager skips when labels are disabled via client."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = True  # Labels disabled

        config = AutomationConfig()

        # Use LabelManager context manager
        with LabelManager(
            mock_github_client, "owner/repo", 123, item_type="issue", config=config
        ) as should_process:
            assert should_process is True
            # No label operations should be performed
            mock_github_client.try_add_work_in_progress_label.assert_not_called()

        # No removal should happen
        mock_github_client.remove_labels_from_issue.assert_not_called()

    def test_label_manager_with_labels_disabled_via_config(self):
        """Test that context manager skips when labels are disabled via config."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False

        config = AutomationConfig()
        config.DISABLE_LABELS = True  # Labels disabled via config

        # Use LabelManager context manager
        with LabelManager(
            mock_github_client, "owner/repo", 123, item_type="issue", config=config
        ) as should_process:
            assert should_process is True
            # No label operations should be performed
            mock_github_client.try_add_work_in_progress_label.assert_not_called()

        # No removal should happen
        mock_github_client.remove_labels_from_issue.assert_not_called()

    def test_label_manager_cleanup_on_exception(self):
        """Test that label is removed even when exception occurs."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.has_label.return_value = False
        mock_github_client.try_add_work_in_progress_label.return_value = True

        config = AutomationConfig()

        # Use LabelManager context manager and raise an exception
        try:
            with LabelManager(
                mock_github_client, "owner/repo", 123, item_type="issue", config=config
            ) as should_process:
                assert should_process is True
                raise ValueError("Test exception")
        except ValueError:
            pass

        # Label should still be removed after exception
        mock_github_client.remove_labels_from_issue.assert_called_once_with(
            "owner/repo", 123, ["@auto-coder"]
        )

    def test_label_manager_with_custom_label_name(self):
        """Test that LabelManager works with custom label names."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.has_label.return_value = False
        mock_github_client.try_add_work_in_progress_label.return_value = True

        config = AutomationConfig()

        # Use LabelManager with custom label
        with LabelManager(
            mock_github_client,
            "owner/repo",
            123,
            item_type="issue",
            label_name="custom-label",
            config=config,
        ) as should_process:
            assert should_process is True
            mock_github_client.try_add_work_in_progress_label.assert_called_once_with(
                "owner/repo", 123, label="custom-label"
            )

        # Custom label should be removed
        mock_github_client.remove_labels_from_issue.assert_called_once_with(
            "owner/repo", 123, ["custom-label"]
        )

    def test_label_manager_pr_type(self):
        """Test that LabelManager works with PR type."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.has_label.return_value = False
        mock_github_client.try_add_work_in_progress_label.return_value = True

        config = AutomationConfig()

        # Use LabelManager for PR
        with LabelManager(
            mock_github_client, "owner/repo", 456, item_type="pr", config=config
        ) as should_process:
            assert should_process is True
            mock_github_client.try_add_work_in_progress_label.assert_called_once_with(
                "owner/repo", 456, label="@auto-coder"
            )

        # Label should be removed from PR
        mock_github_client.remove_labels_from_issue.assert_called_once_with(
            "owner/repo", 456, ["@auto-coder"]
        )

    def test_label_manager_retry_on_add_failure(self):
        """Test that LabelManager retries on label addition failure."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.has_label.return_value = False
        # First two calls fail, third succeeds
        mock_github_client.try_add_work_in_progress_label.side_effect = [
            Exception("API error 1"),
            Exception("API error 2"),
            True,  # Success on third try
        ]

        config = AutomationConfig()

        # Use LabelManager with retry
        with LabelManager(
            mock_github_client, "owner/repo", 123, item_type="issue", config=config, max_retries=3
        ) as should_process:
            assert should_process is True
            # Should be called 3 times (2 failures + 1 success)
            assert mock_github_client.try_add_work_in_progress_label.call_count == 3

    def test_label_manager_gives_up_after_max_retries(self):
        """Test that LabelManager gives up after max retries and continues processing."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.has_label.return_value = False
        # Always fails
        mock_github_client.try_add_work_in_progress_label.side_effect = Exception("API error")

        config = AutomationConfig()

        # Use LabelManager with retries
        with LabelManager(
            mock_github_client, "owner/repo", 123, item_type="issue", config=config, max_retries=2
        ) as should_process:
            assert should_process is True  # Still returns True to allow processing
            # Should be called 2 times (max_retries)
            assert mock_github_client.try_add_work_in_progress_label.call_count == 2

    def test_label_manager_retry_on_remove_failure(self):
        """Test that LabelManager retries on label removal failure."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.has_label.return_value = False
        mock_github_client.try_add_work_in_progress_label.return_value = True
        # First two remove calls fail, third succeeds
        mock_github_client.remove_labels_from_issue.side_effect = [
            Exception("API error 1"),
            Exception("API error 2"),
            None,  # Success on third try
        ]

        config = AutomationConfig()

        # Use LabelManager
        with LabelManager(
            mock_github_client, "owner/repo", 123, item_type="issue", config=config, max_retries=3
        ) as should_process:
            assert should_process is True

        # Remove should be called 3 times (2 failures + 1 success)
        assert mock_github_client.remove_labels_from_issue.call_count == 3

    def test_label_manager_thread_safety(self):
        """Test that LabelManager is thread-safe (uses locks internally)."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.has_label.return_value = False
        mock_github_client.try_add_work_in_progress_label.return_value = True

        config = AutomationConfig()

        # Create multiple LabelManager instances to test they don't interfere
        managers = [
            LabelManager(mock_github_client, "owner/repo", i, item_type="issue", config=config)
            for i in range(5)
        ]

        # All should be able to enter and exit independently
        for manager in managers:
            with manager as should_process:
                assert should_process is True

        # All should have been processed
        assert mock_github_client.try_add_work_in_progress_label.call_count == 5
        assert mock_github_client.remove_labels_from_issue.call_count == 5

    def test_label_manager_no_label_added_flag(self):
        """Test that _label_added flag is properly managed."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.has_label.return_value = True  # Label already exists

        config = AutomationConfig()

        # Use LabelManager
        with LabelManager(
            mock_github_client, "owner/repo", 123, item_type="issue", config=config
        ) as should_process:
            assert should_process is False
            # Label was not added, so _label_added should be False

        # Label should NOT be removed (since it wasn't added)
        mock_github_client.remove_labels_from_issue.assert_not_called()
