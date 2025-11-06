"""Tests for LabelManager context manager."""

<<<<<<< HEAD
import time
from unittest.mock import Mock, call, patch
=======
from unittest.mock import Mock, patch
>>>>>>> origin/HEAD

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.github_client import GitHubClient
<<<<<<< HEAD
from src.auto_coder.label_manager import LabelManager, LabelOperationError


class TestLabelManager:
    """Test LabelManager context manager."""

    def test_label_manager_initialization(self):
        """Test LabelManager initialization."""
        mock_github = Mock()
        config = AutomationConfig()

        lm = LabelManager(
            github_client=mock_github,
            repo_name="owner/repo",
            item_number=123,
            item_type="issue",
            dry_run=False,
            config=config,
            label_name="@auto-coder",
            max_retries=3,
            retry_delay=0.5,
        )

        assert lm.github_client == mock_github
        assert lm.repo_name == "owner/repo"
        assert lm.item_number == 123
        assert lm.item_type == "issue"
        assert lm.dry_run is False
        assert lm.config == config
        assert lm.label_name == "@auto-coder"
        assert lm.max_retries == 3
        assert lm.retry_delay == 0.5
        assert lm._should_cleanup is False
        assert lm._labels_disabled is False

    def test_label_manager_labels_disabled_via_client(self):
        """Test LabelManager detects labels disabled via client."""
        mock_github = Mock()
        mock_github.disable_labels = True

        lm = LabelManager(
            github_client=mock_github,
            repo_name="owner/repo",
            item_number=123,
        )

        assert lm._labels_disabled is True

    def test_label_manager_labels_disabled_via_config(self):
        """Test LabelManager detects labels disabled via config."""
        mock_github = Mock()
        config = Mock()
        config.DISABLE_LABELS = True

        lm = LabelManager(
            github_client=mock_github,
            repo_name="owner/repo",
            item_number=123,
            config=config,
        )

        assert lm._labels_disabled is True

    def test_label_manager_check_and_add_label_success(self):
        """Test successfully checking and adding label."""
        mock_github = Mock()
        mock_github.try_add_work_in_progress_label.return_value = True

        lm = LabelManager(
            github_client=mock_github,
            repo_name="owner/repo",
            item_number=123,
            dry_run=False,
        )

        result = lm.check_and_add_label()

        assert result is True
        assert lm._label_added is True
        assert lm._should_cleanup is True
        mock_github.try_add_work_in_progress_label.assert_called_once_with("owner/repo", 123, label="@auto-coder")

    def test_label_manager_check_and_add_label_already_exists(self):
        """Test checking label when it already exists."""
        mock_github = Mock()
        mock_github.try_add_work_in_progress_label.return_value = False

        lm = LabelManager(
            github_client=mock_github,
            repo_name="owner/repo",
            item_number=123,
            dry_run=False,
        )

        result = lm.check_and_add_label()

        assert result is False
        mock_github.try_add_work_in_progress_label.assert_called_once_with("owner/repo", 123, label="@auto-coder")

    def test_label_manager_check_and_add_label_dry_run(self):
        """Test checking and adding label in dry run mode."""
        mock_github = Mock()

        lm = LabelManager(
            github_client=mock_github,
            repo_name="owner/repo",
            item_number=123,
            dry_run=True,
        )

        result = lm.check_and_add_label()

        assert result is True
        mock_github.try_add_work_in_progress_label.assert_not_called()

    def test_label_manager_check_and_add_label_disabled(self):
        """Test checking and adding label when labels are disabled."""
        mock_github = Mock()
        mock_github.disable_labels = True

        lm = LabelManager(
            github_client=mock_github,
            repo_name="owner/repo",
            item_number=123,
            dry_run=False,
        )

        result = lm.check_and_add_label()

        assert result is True
        mock_github.try_add_work_in_progress_label.assert_not_called()

    def test_label_manager_remove_label_success(self):
        """Test successfully removing label."""
        mock_github = Mock()

        lm = LabelManager(
            github_client=mock_github,
            repo_name="owner/repo",
            item_number=123,
            dry_run=False,
        )

        result = lm.remove_label()

        assert result is True
        mock_github.remove_labels_from_issue.assert_called_once_with("owner/repo", 123, ["@auto-coder"])

    def test_label_manager_remove_label_dry_run(self):
        """Test removing label in dry run mode."""
        mock_github = Mock()

        lm = LabelManager(
            github_client=mock_github,
            repo_name="owner/repo",
            item_number=123,
            dry_run=True,
        )

        result = lm.remove_label()

        assert result is True
        mock_github.remove_labels_from_issue.assert_not_called()

    def test_label_manager_remove_label_disabled(self):
        """Test removing label when labels are disabled."""
        mock_github = Mock()
        mock_github.disable_labels = True

        lm = LabelManager(
            github_client=mock_github,
            repo_name="owner/repo",
            item_number=123,
            dry_run=False,
        )

        result = lm.remove_label()

        assert result is True
        mock_github.remove_labels_from_issue.assert_not_called()

    def test_label_manager_verify_label_exists_true(self):
        """Test verifying label exists (true case)."""
        mock_github = Mock()
        mock_github.has_label.return_value = True

        lm = LabelManager(
            github_client=mock_github,
            repo_name="owner/repo",
            item_number=123,
        )

        result = lm.verify_label_exists()

        assert result is True
        mock_github.has_label.assert_called_once_with("owner/repo", 123, "@auto-coder")

    def test_label_manager_verify_label_exists_false(self):
        """Test verifying label exists (false case)."""
        mock_github = Mock()
        mock_github.has_label.return_value = False

        lm = LabelManager(
            github_client=mock_github,
            repo_name="owner/repo",
            item_number=123,
        )

        result = lm.verify_label_exists()

        assert result is False
        mock_github.has_label.assert_called_once_with("owner/repo", 123, "@auto-coder")

    def test_label_manager_verify_label_exists_pr(self):
        """Test verifying label on PR."""
        mock_github = Mock()
        mock_github.get_pr_details_by_number.return_value = {"labels": ["@auto-coder", "bug"]}
        # Explicitly set has_label to return None to test fallback behavior
        mock_github.has_label = None

        lm = LabelManager(
            github_client=mock_github,
            repo_name="owner/repo",
            item_number=123,
            item_type="pr",
        )

        result = lm.verify_label_exists()

        assert result is True
        mock_github.get_pr_details_by_number.assert_called_once_with("owner/repo", 123)

    def test_label_manager_verify_label_exists_issue(self):
        """Test verifying label on issue."""
        mock_github = Mock()
        mock_github.get_issue_details_by_number.return_value = {"labels": ["bug"]}
        # Explicitly set has_label to return None to test fallback behavior
        mock_github.has_label = None

        lm = LabelManager(
            github_client=mock_github,
            repo_name="owner/repo",
            item_number=123,
            item_type="issue",
        )

        result = lm.verify_label_exists()

        assert result is False
        mock_github.get_issue_details_by_number.assert_called_once_with("owner/repo", 123)

    def test_label_manager_context_manager_success(self):
        """Test successful context manager usage."""
        mock_github = Mock()
        mock_github.try_add_work_in_progress_label.return_value = True

        with LabelManager(
            github_client=mock_github,
            repo_name="owner/repo",
            item_number=123,
            dry_run=False,
        ) as lm:
            assert lm is not None
            assert lm._should_cleanup is True

        # Verify label was removed on exit
        mock_github.remove_labels_from_issue.assert_called_once_with("owner/repo", 123, ["@auto-coder"])

    def test_label_manager_context_manager_skips_on_false(self):
        """Test context manager raises exception when label already exists."""
        mock_github = Mock()
        mock_github.try_add_work_in_progress_label.return_value = False

        with pytest.raises(LabelOperationError) as exc_info:
            with LabelManager(
                github_client=mock_github,
                repo_name="owner/repo",
                item_number=123,
                dry_run=False,
            ) as lm:
                pass

        assert "Another instance is already processing" in str(exc_info.value)
        # Label should not be removed since it wasn't added by us
        mock_github.remove_labels_from_issue.assert_not_called()

    def test_label_manager_context_manager_cleanup_on_exception(self):
        """Test label cleanup even when exception occurs."""
        mock_github = Mock()
        mock_github.try_add_work_in_progress_label.return_value = True

        with pytest.raises(RuntimeError):
            with LabelManager(
                github_client=mock_github,
                repo_name="owner/repo",
                item_number=123,
                dry_run=False,
            ) as lm:
                raise RuntimeError("Test exception")

        # Verify label was still removed despite exception
        mock_github.remove_labels_from_issue.assert_called_once_with("owner/repo", 123, ["@auto-coder"])

    def test_label_manager_context_manager_dry_run(self):
        """Test context manager with dry run."""
        mock_github = Mock()

        with LabelManager(
            github_client=mock_github,
            repo_name="owner/repo",
            item_number=123,
            dry_run=True,
        ) as lm:
            assert lm is not None

        # No actual API calls in dry run mode
        mock_github.try_add_work_in_progress_label.assert_not_called()
        mock_github.remove_labels_from_issue.assert_not_called()

    def test_label_manager_retry_operation_success(self):
        """Test retry operation that succeeds on first try."""
        mock_github = Mock()
        mock_operation = Mock(return_value="success")

        lm = LabelManager(
            github_client=mock_github,
            repo_name="owner/repo",
            item_number=123,
        )

        result = lm._retry_operation(mock_operation)

        assert result == "success"
        mock_operation.assert_called_once()

    def test_label_manager_retry_operation_eventual_success(self):
        """Test retry operation that succeeds after retries."""
        mock_github = Mock()
        mock_operation = Mock(side_effect=[Exception("Failed"), Exception("Failed"), "success"])

        lm = LabelManager(
            github_client=mock_github,
            repo_name="owner/repo",
            item_number=123,
            max_retries=3,
            retry_delay=0.01,  # Fast retry for test
        )

        # Patch time.sleep to speed up test
        with patch("time.sleep") as mock_sleep:
            result = lm._retry_operation(mock_operation)

        assert result == "success"
        assert mock_operation.call_count == 3
        assert mock_sleep.call_count == 2

    def test_label_manager_retry_operation_all_failures(self):
        """Test retry operation that fails all attempts."""
        mock_github = Mock()
        error = Exception("API Error")
        mock_operation = Mock(side_effect=error)

        lm = LabelManager(
            github_client=mock_github,
            repo_name="owner/repo",
            item_number=123,
            max_retries=3,
            retry_delay=0.01,
        )

        # Patch time.sleep to speed up test
        with patch("time.sleep") as mock_sleep:
            with pytest.raises(LabelOperationError) as exc_info:
                lm._retry_operation(mock_operation)

        assert "Operation failed after 3 attempts" in str(exc_info.value)
        assert mock_operation.call_count == 3
        assert mock_sleep.call_count == 2

    def test_label_manager_with_custom_label_name(self):
        """Test LabelManager with custom label name."""
        mock_github = Mock()
        mock_github.try_add_work_in_progress_label.return_value = True

        lm = LabelManager(
            github_client=mock_github,
            repo_name="owner/repo",
            item_number=123,
            label_name="custom-label",
        )

        result = lm.check_and_add_label()

        assert result is True
        mock_github.try_add_work_in_progress_label.assert_called_once_with("owner/repo", 123, label="custom-label")

    def test_label_manager_github_client_missing_method(self):
        """Test LabelManager when GitHub client is missing required methods."""
        mock_github = Mock()

        lm = LabelManager(
            github_client=mock_github,
            repo_name="owner/repo",
            item_number=123,
        )

        # Should handle missing method gracefully
        result = lm.check_and_add_label()

        assert result is True  # Allow processing to continue

    def test_label_manager_github_client_remove_missing_method(self):
        """Test LabelManager remove when client lacks removal method."""
        mock_github = Mock()

        lm = LabelManager(
            github_client=mock_github,
            repo_name="owner/repo",
            item_number=123,
        )

        # Mock that client doesn't have remove method
        delattr(mock_github, "remove_labels_from_issue")

        result = lm.remove_label()

        assert result is False


class TestBackwardCompatibility:
    """Test backward compatibility functions."""

    def test_check_and_add_label_deprecated(self):
        """Test deprecated check_and_add_label function."""
        mock_github = Mock()
        mock_github.try_add_work_in_progress_label.return_value = True

        from src.auto_coder.label_manager import check_and_add_label

        with patch("warnings.warn") as mock_warn:
            result = check_and_add_label(mock_github, "owner/repo", 123)

        assert result is True
        mock_warn.assert_called_once()

    def test_remove_label_deprecated(self):
        """Test deprecated remove_label function."""
        mock_github = Mock()

        from src.auto_coder.label_manager import remove_label

        with patch("warnings.warn") as mock_warn:
            remove_label(mock_github, "owner/repo", 123)

        mock_warn.assert_called_once()

    def test_check_label_exists_deprecated(self):
        """Test deprecated check_label_exists function."""
        mock_github = Mock()
        mock_github.has_label.return_value = True

        from src.auto_coder.label_manager import check_label_exists

        with patch("warnings.warn") as mock_warn:
            result = check_label_exists(mock_github, "owner/repo", 123)

        assert result is True
        mock_warn.assert_called_once()


class TestLabelManagerIntegration:
    """Integration tests for LabelManager."""

    @patch("time.sleep")
    def test_label_manager_api_retry_integration(self, mock_sleep):
        """Test LabelManager retries on transient API failures."""
        mock_github = Mock()
        # Simulate API failures followed by success
        mock_github.try_add_work_in_progress_label.side_effect = [
            Exception("Network error"),
            Exception("Rate limit"),
            True,  # Success on third try
        ]
        mock_github.remove_labels_from_issue.return_value = None

        with LabelManager(
            github_client=mock_github,
            repo_name="owner/repo",
            item_number=123,
            max_retries=3,
            retry_delay=0.01,
        ):
            pass

        # Verify retry happened
        assert mock_github.try_add_work_in_progress_label.call_count == 3
        # Verify cleanup was called
        assert mock_github.remove_labels_from_issue.call_count == 1

    def test_label_manager_thread_safety(self):
        """Test LabelManager thread-safety through state isolation."""
        mock_github = Mock()
        mock_github.try_add_work_in_progress_label.return_value = True

        # Create two LabelManager instances
        lm1 = LabelManager(
            github_client=mock_github,
            repo_name="owner/repo",
            item_number=123,
        )
        lm2 = LabelManager(
            github_client=mock_github,
            repo_name="owner/repo",
            item_number=456,
        )

        # Verify state is isolated
        assert lm1.item_number != lm2.item_number
        assert lm1._should_cleanup is False
        assert lm2._should_cleanup is False

        lm1.check_and_add_label()
        lm2.check_and_add_label()

        # Verify both tracked their state independently
        assert lm1._should_cleanup is True
        assert lm2._should_cleanup is True
=======
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
        with LabelManager(mock_github_client, "owner/repo", 123, item_type="issue", config=config) as should_process:
            assert should_process is True
            # Label should be added inside context
            mock_github_client.try_add_work_in_progress_label.assert_called_once_with("owner/repo", 123, label="@auto-coder")

        # Label should be removed after exiting context
        mock_github_client.remove_labels_from_issue.assert_called_once_with("owner/repo", 123, ["@auto-coder"])

    def test_label_manager_skips_when_label_already_exists(self):
        """Test that context manager returns False when label already exists."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.has_label.return_value = True  # Label already exists

        config = AutomationConfig()

        # Use LabelManager context manager
        with LabelManager(mock_github_client, "owner/repo", 123, item_type="issue", config=config) as should_process:
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
        with LabelManager(mock_github_client, "owner/repo", 123, item_type="issue", dry_run=True, config=config) as should_process:
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
        with LabelManager(mock_github_client, "owner/repo", 123, item_type="issue", config=config) as should_process:
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
        with LabelManager(mock_github_client, "owner/repo", 123, item_type="issue", config=config) as should_process:
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
            with LabelManager(mock_github_client, "owner/repo", 123, item_type="issue", config=config) as should_process:
                assert should_process is True
                raise ValueError("Test exception")
        except ValueError:
            pass

        # Label should still be removed after exception
        mock_github_client.remove_labels_from_issue.assert_called_once_with("owner/repo", 123, ["@auto-coder"])

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
            mock_github_client.try_add_work_in_progress_label.assert_called_once_with("owner/repo", 123, label="custom-label")

        # Custom label should be removed
        mock_github_client.remove_labels_from_issue.assert_called_once_with("owner/repo", 123, ["custom-label"])

    def test_label_manager_pr_type(self):
        """Test that LabelManager works with PR type."""
        # Setup mocks
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_github_client.has_label.return_value = False
        mock_github_client.try_add_work_in_progress_label.return_value = True

        config = AutomationConfig()

        # Use LabelManager for PR
        with LabelManager(mock_github_client, "owner/repo", 456, item_type="pr", config=config) as should_process:
            assert should_process is True
            mock_github_client.try_add_work_in_progress_label.assert_called_once_with("owner/repo", 456, label="@auto-coder")

        # Label should be removed from PR
        mock_github_client.remove_labels_from_issue.assert_called_once_with("owner/repo", 456, ["@auto-coder"])

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
        with LabelManager(mock_github_client, "owner/repo", 123, item_type="issue", config=config, max_retries=3) as should_process:
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
        with LabelManager(mock_github_client, "owner/repo", 123, item_type="issue", config=config, max_retries=2) as should_process:
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
        with LabelManager(mock_github_client, "owner/repo", 123, item_type="issue", config=config, max_retries=3) as should_process:
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
        managers = [LabelManager(mock_github_client, "owner/repo", i, item_type="issue", config=config) for i in range(5)]

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
        with LabelManager(mock_github_client, "owner/repo", 123, item_type="issue", config=config) as should_process:
            assert should_process is False
            # Label was not added, so _label_added should be False

        # Label should NOT be removed (since it wasn't added)
        mock_github_client.remove_labels_from_issue.assert_not_called()
>>>>>>> origin/HEAD
