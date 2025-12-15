from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.pr_processor import _handle_pr_merge, _should_skip_waiting_for_jules


class TestHandlePrMergeJulesFallback:
    """Test cases for _handle_pr_merge function with Jules fallback logic."""

    @patch("src.auto_coder.pr_processor._is_jules_pr")
    @patch("src.auto_coder.pr_processor._send_jules_error_feedback")
    @patch("src.auto_coder.pr_processor._check_github_actions_status")
    @patch("src.auto_coder.pr_processor.get_detailed_checks_from_history")
    @patch("src.auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress")
    @patch("src.auto_coder.pr_processor._get_mergeable_state")
    @patch("src.auto_coder.pr_processor._checkout_pr_branch")
    @patch("src.auto_coder.pr_processor._update_with_base_branch")
    @patch("src.auto_coder.pr_processor._get_github_actions_logs")
    @patch("src.auto_coder.pr_processor._fix_pr_issues_with_testing")
    def test_handle_pr_merge_jules_normal_flow(
        self,
        mock_fix_issues,
        mock_get_logs,
        mock_update_base,
        mock_checkout,
        mock_mergeable,
        mock_check_in_progress,
        mock_detailed_checks,
        mock_check_status,
        mock_send_feedback,
        mock_is_jules,
    ):
        """Test that normal Jules flow is used when failure count <= 10."""
        # Setup
        repo_name = "owner/repo"
        pr_data = {"number": 123, "title": "Test PR"}
        config = AutomationConfig()
        github_client = Mock()

        # Mock checks failure
        mock_check_in_progress.return_value = True
        mock_mergeable.return_value = {"mergeable": True}
        mock_check_status.return_value = Mock(success=False, error=None)
        mock_detailed_checks.return_value = Mock(success=False, failed_checks=[{"name": "test"}])

        # Mock Jules PR
        mock_is_jules.return_value = True

        # Mock comments (less than 10 failures)
        target_message = "🤖 Auto-Coder: CI checks failed. I've sent the error logs to the Jules session and requested a fix. Please wait for the updates."
        comments = [{"body": "Some comment"}, {"body": target_message}] * 5  # 5 failures
        github_client.get_pr_comments.return_value = comments

        # Mock send feedback
        mock_send_feedback.return_value = ["Sent feedback to Jules"]

        # Execute
        actions = _handle_pr_merge(github_client, repo_name, pr_data, config, {})

        # Assert
        assert "Jules will handle fixing PR #123, skipping local fixes" in actions[-1]
        mock_send_feedback.assert_called_once()
        # Should NOT proceed to checkout and fix
        mock_checkout.assert_not_called()
        mock_fix_issues.assert_not_called()

    @patch("src.auto_coder.pr_processor._is_jules_pr")
    @patch("src.auto_coder.pr_processor._send_jules_error_feedback")
    @patch("src.auto_coder.pr_processor._check_github_actions_status")
    @patch("src.auto_coder.pr_processor.get_detailed_checks_from_history")
    @patch("src.auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress")
    @patch("src.auto_coder.pr_processor._get_mergeable_state")
    @patch("src.auto_coder.pr_processor._checkout_pr_branch")
    @patch("src.auto_coder.pr_processor._update_with_base_branch")
    @patch("src.auto_coder.pr_processor._get_github_actions_logs")
    @patch("src.auto_coder.pr_processor._fix_pr_issues_with_testing")
    @patch("src.auto_coder.pr_processor.cmd")
    def test_handle_pr_merge_jules_fallback_flow(
        self,
        mock_cmd,
        mock_fix_issues,
        mock_get_logs,
        mock_update_base,
        mock_checkout,
        mock_mergeable,
        mock_check_in_progress,
        mock_detailed_checks,
        mock_check_status,
        mock_send_feedback,
        mock_is_jules,
    ):
        """Test that fallback flow is used when failure count > 10."""
        # Setup
        repo_name = "owner/repo"
        pr_data = {"number": 123, "title": "Test PR", "head": {"ref": "feature-branch"}}
        config = AutomationConfig()
        github_client = Mock()

        # Mock checks failure
        mock_check_in_progress.return_value = True
        mock_mergeable.return_value = {"mergeable": True}
        mock_check_status.return_value = Mock(success=False, error=None)
        mock_detailed_checks.return_value = Mock(success=False, failed_checks=[{"name": "test"}])

        # Mock Jules PR
        mock_is_jules.return_value = True

        # Mock comments (more than 10 failures)
        target_message = "🤖 Auto-Coder: CI checks failed. I've sent the error logs to the Jules session and requested a fix. Please wait for the updates."
        comments = [{"body": target_message}] * 11  # 11 failures
        github_client.get_pr_comments.return_value = comments

        # Mock checkout success
        mock_checkout.return_value = True
        mock_cmd.run_command.return_value = Mock(success=True, stdout="feature-branch")  # Already on branch

        # Mock fix issues
        mock_fix_issues.return_value = ["Fixed issues locally"]

        # Execute
        actions = _handle_pr_merge(github_client, repo_name, pr_data, config, {})

        # Assert
        # Should NOT call send feedback
        mock_send_feedback.assert_not_called()

        # Should proceed to checkout and fix
        mock_checkout.assert_called_once()
        mock_fix_issues.assert_called_once()

        # Verify actions contain local fix info
        assert any("Fixed issues locally" in action for action in actions)

    @patch("src.auto_coder.pr_processor._is_jules_pr")
    @patch("src.auto_coder.pr_processor._send_jules_error_feedback")
    @patch("src.auto_coder.pr_processor._check_github_actions_status")
    @patch("src.auto_coder.pr_processor.get_detailed_checks_from_history")
    @patch("src.auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress")
    @patch("src.auto_coder.pr_processor._get_mergeable_state")
    @patch("src.auto_coder.pr_processor._checkout_pr_branch")
    @patch("src.auto_coder.pr_processor._update_with_base_branch")
    @patch("src.auto_coder.pr_processor._get_github_actions_logs")
    @patch("src.auto_coder.pr_processor._fix_pr_issues_with_testing")
    @patch("src.auto_coder.pr_processor.cmd")
    def test_handle_pr_merge_jules_fallback_time_flow(
        self,
        mock_cmd,
        mock_fix_issues,
        mock_get_logs,
        mock_update_base,
        mock_checkout,
        mock_mergeable,
        mock_check_in_progress,
        mock_detailed_checks,
        mock_check_status,
        mock_send_feedback,
        mock_is_jules,
    ):
        """Test that fallback flow is used when waiting > 1 hour."""
        # Setup
        repo_name = "owner/repo"
        pr_data = {"number": 123, "title": "Test PR", "head": {"ref": "feature-branch"}}
        config = AutomationConfig()
        github_client = Mock()

        # Mock checks failure
        mock_check_in_progress.return_value = True
        mock_mergeable.return_value = {"mergeable": True}
        mock_check_status.return_value = Mock(success=False, error=None)
        mock_detailed_checks.return_value = Mock(success=False, failed_checks=[{"name": "test"}])

        # Mock Jules PR
        mock_is_jules.return_value = True

        # Mock comments (1 failure, but 2 hours ago)
        target_message = "🤖 Auto-Coder: CI checks failed. I've sent the error logs to the Jules session and requested a fix. Please wait for the updates."
        two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        comments = [{"body": target_message, "created_at": two_hours_ago}]
        github_client.get_pr_comments.return_value = comments

        # Mock checkout success
        mock_checkout.return_value = True
        mock_cmd.run_command.return_value = Mock(success=True, stdout="feature-branch")  # Already on branch

        # Mock fix issues
        mock_fix_issues.return_value = ["Fixed issues locally"]

        # Execute
        actions = _handle_pr_merge(github_client, repo_name, pr_data, config, {})

        # Assert
        # Should NOT call send feedback
        mock_send_feedback.assert_not_called()

        # Should proceed to checkout and fix
        mock_checkout.assert_called_once()
        mock_fix_issues.assert_called_once()

        # Verify actions contain local fix info
        assert any("Fixed issues locally" in action for action in actions)


class TestShouldSkipWaitingForJulesFallback:
    """Test cases for _should_skip_waiting_for_jules function with time-based fallback."""

    def test_should_skip_waiting_for_jules_timeout(self):
        """Test that it returns False if waiting for > 1 hour."""
        # Setup
        repo_name = "owner/repo"
        pr_data = {"number": 123}
        github_client = Mock()

        # Mock comments
        target_message = "🤖 Auto-Coder: CI checks failed. I've sent the error logs to the Jules session and requested a fix. Please wait for the updates."

        # 2 hours ago
        two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

        comments = [{"body": target_message, "created_at": two_hours_ago}]
        github_client.get_pr_comments.return_value = comments

        # Mock commits (older than comment)
        three_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        commits = [{"commit": {"committer": {"date": three_hours_ago}}}]
        github_client.get_pr_commits.return_value = commits

        # Execute
        result = _should_skip_waiting_for_jules(github_client, repo_name, pr_data)

        # Assert
        assert result is False

    def test_should_skip_waiting_for_jules_no_timeout(self):
        """Test that it returns True if waiting for < 1 hour."""
        # Setup
        repo_name = "owner/repo"
        pr_data = {"number": 123}
        github_client = Mock()

        # Mock comments
        target_message = "🤖 Auto-Coder: CI checks failed. I've sent the error logs to the Jules session and requested a fix. Please wait for the updates."

        # 30 minutes ago
        thirty_mins_ago = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()

        comments = [{"body": target_message, "created_at": thirty_mins_ago}]
        github_client.get_pr_comments.return_value = comments

        # Mock commits (older than comment)
        one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        commits = [{"commit": {"committer": {"date": one_hour_ago}}}]
        github_client.get_pr_commits.return_value = commits

        # Execute
        result = _should_skip_waiting_for_jules(github_client, repo_name, pr_data)

        # Assert
        assert result is True
