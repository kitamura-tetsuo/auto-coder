"""Tests for LabelManager context manager."""

import time
from unittest.mock import Mock, patch, call
import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.github_client import GitHubClient
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
        mock_github.try_add_work_in_progress_label.assert_called_once_with(
            "owner/repo", 123, label="@auto-coder"
        )

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
        mock_github.try_add_work_in_progress_label.assert_called_once_with(
            "owner/repo", 123, label="@auto-coder"
        )

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
        mock_github.remove_labels_from_issue.assert_called_once_with(
            "owner/repo", 123, ["@auto-coder"]
        )

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
        mock_github.remove_labels_from_issue.assert_called_once_with(
            "owner/repo", 123, ["@auto-coder"]
        )

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
        mock_github.remove_labels_from_issue.assert_called_once_with(
            "owner/repo", 123, ["@auto-coder"]
        )

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
        mock_github.try_add_work_in_progress_label.assert_called_once_with(
            "owner/repo", 123, label="custom-label"
        )

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
        assert lm1._should_cleanup == lm2._should_cleanup == False

        lm1.check_and_add_label()
        lm2.check_and_add_label()

        # Verify both tracked their state independently
        assert lm1._should_cleanup is True
        assert lm2._should_cleanup is True
