"""Tests for closing Jules PRs that fail to pass CI within the configured timeout."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, Mock, patch

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.pr_processor import _close_stale_jules_pr, _handle_pr_merge, _should_skip_waiting_for_jules
from src.auto_coder.util.github_action import DetailedChecksResult, GitHubActionsStatusResult

JULES_PR_BODY = "Fixes the reported bug.\n\nSession ID: 901463134778726610\nhttps://jules.google.com/session/901463134778726610\n\nclose #4636"


def _jules_pr_data(hours_old: float) -> dict:
    """Build Jules PR data created ``hours_old`` hours ago."""
    created_at = (datetime.now(timezone.utc) - timedelta(hours=hours_old)).isoformat().replace("+00:00", "Z")
    return {
        "number": 4643,
        "title": "Fix flaky outline rendering",
        "body": JULES_PR_BODY,
        "created_at": created_at,
        "user": {"login": "google-labs-jules[bot]"},
        "head": {"ref": "jules-fix-4636"},
        "base": {"ref": "main"},
    }


class TestCloseStaleJulesPR:
    """Test cases for _close_stale_jules_pr."""

    @patch("src.auto_coder.pr_processor.increment_attempt")
    def test_closes_pr_and_increments_attempt_after_timeout(self, mock_increment):
        github_client = Mock()
        config = AutomationConfig()
        config.JULES_PR_CI_TIMEOUT_HOURS = 12
        pr_data = _jules_pr_data(hours_old=13)
        checks = MagicMock(spec=GitHubActionsStatusResult, success=False, in_progress=False)
        mock_increment.return_value = 3

        actions = _close_stale_jules_pr(github_client, "owner/repo", pr_data, config, checks)

        github_client.close_pr.assert_called_once()
        close_args = github_client.close_pr.call_args[0]
        assert close_args[0] == "owner/repo"
        assert close_args[1] == 4643
        assert "12 hours" in close_args[2]
        mock_increment.assert_called_once_with("owner/repo", 4636)
        assert any("Closed stale Jules PR #4643" in action for action in actions)
        assert any("Incremented attempt for issue #4636 to 3" in action for action in actions)

    @patch("src.auto_coder.pr_processor.increment_attempt")
    def test_keeps_pr_open_before_timeout(self, mock_increment):
        github_client = Mock()
        config = AutomationConfig()
        config.JULES_PR_CI_TIMEOUT_HOURS = 12
        pr_data = _jules_pr_data(hours_old=11)
        checks = MagicMock(spec=GitHubActionsStatusResult, success=False, in_progress=False)

        actions = _close_stale_jules_pr(github_client, "owner/repo", pr_data, config, checks)

        assert actions == []
        github_client.close_pr.assert_not_called()
        mock_increment.assert_not_called()

    @patch("src.auto_coder.pr_processor.increment_attempt")
    def test_keeps_pr_open_when_ci_passed(self, mock_increment):
        github_client = Mock()
        config = AutomationConfig()
        config.JULES_PR_CI_TIMEOUT_HOURS = 12
        pr_data = _jules_pr_data(hours_old=48)
        checks = MagicMock(spec=GitHubActionsStatusResult, success=True, in_progress=False)

        actions = _close_stale_jules_pr(github_client, "owner/repo", pr_data, config, checks)

        assert actions == []
        github_client.close_pr.assert_not_called()
        mock_increment.assert_not_called()

    @patch("src.auto_coder.pr_processor.increment_attempt")
    def test_keeps_pr_open_while_ci_in_progress(self, mock_increment):
        github_client = Mock()
        config = AutomationConfig()
        config.JULES_PR_CI_TIMEOUT_HOURS = 12
        pr_data = _jules_pr_data(hours_old=48)
        checks = MagicMock(spec=GitHubActionsStatusResult, success=False, in_progress=True)

        actions = _close_stale_jules_pr(github_client, "owner/repo", pr_data, config, checks)

        assert actions == []
        github_client.close_pr.assert_not_called()
        mock_increment.assert_not_called()

    @patch("src.auto_coder.pr_processor.increment_attempt")
    def test_ignores_non_jules_pr(self, mock_increment):
        github_client = Mock()
        config = AutomationConfig()
        config.JULES_PR_CI_TIMEOUT_HOURS = 12
        pr_data = _jules_pr_data(hours_old=48)
        pr_data["user"] = {"login": "human-dev"}
        pr_data["body"] = "A regular PR without a session reference"

        actions = _close_stale_jules_pr(github_client, "owner/repo", pr_data, config, None)

        assert actions == []
        github_client.close_pr.assert_not_called()
        mock_increment.assert_not_called()

    @patch("src.auto_coder.pr_processor._resolve_jules_pr_issue_number")
    @patch("src.auto_coder.pr_processor.increment_attempt")
    def test_resolves_issue_when_body_has_no_link(self, mock_increment, mock_resolve):
        github_client = Mock()
        config = AutomationConfig()
        config.JULES_PR_CI_TIMEOUT_HOURS = 12
        pr_data = _jules_pr_data(hours_old=13)
        pr_data["body"] = "Session ID: 901463134778726610"
        checks = MagicMock(spec=GitHubActionsStatusResult, success=False, in_progress=False)
        mock_resolve.return_value = 4636
        mock_increment.return_value = 2

        actions = _close_stale_jules_pr(github_client, "owner/repo", pr_data, config, checks)

        mock_resolve.assert_called_once_with("owner/repo", pr_data, github_client)
        mock_increment.assert_called_once_with("owner/repo", 4636)
        assert any("Closed stale Jules PR #4643" in action for action in actions)


class TestHandlePrMergeJulesPR:
    """Test cases for _handle_pr_merge with Jules PRs."""

    @patch("src.auto_coder.pr_processor.cmd.run_command")
    @patch("src.auto_coder.pr_processor.increment_attempt")
    @patch("src.auto_coder.pr_processor._send_jules_error_feedback")
    @patch("src.auto_coder.pr_processor._fix_pr_issues_with_testing")
    @patch("src.auto_coder.pr_processor._checkout_pr_branch")
    @patch("src.auto_coder.pr_processor._check_github_actions_status")
    @patch("src.auto_coder.pr_processor.get_detailed_checks_from_history")
    @patch("src.auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress")
    @patch("src.auto_coder.pr_processor._get_mergeable_state")
    def test_stale_jules_pr_is_closed_instead_of_fixed_locally(
        self,
        mock_mergeable,
        mock_check_in_progress,
        mock_detailed_checks,
        mock_check_status,
        mock_checkout,
        mock_fix_issues,
        mock_send_feedback,
        mock_increment,
        mock_run_command,
    ):
        """A Jules PR older than the timeout is closed; no local fixes are applied."""
        from src.auto_coder.utils import CommandResult

        github_client = Mock()
        config = AutomationConfig()
        config.JULES_PR_CI_TIMEOUT_HOURS = 12
        pr_data = _jules_pr_data(hours_old=13)

        mock_check_in_progress.return_value = True
        mock_mergeable.return_value = {"mergeable": True}
        mock_check_status.return_value = MagicMock(spec=GitHubActionsStatusResult, success=False, in_progress=False, error=None, ids=[1])
        mock_detailed_checks.return_value = MagicMock(spec=DetailedChecksResult, success=False, failed_checks=[{"name": "test"}])
        mock_run_command.return_value = CommandResult(success=True, stdout="main", stderr="", returncode=0)
        mock_increment.return_value = 2

        actions = _handle_pr_merge(github_client, "owner/repo", pr_data, config, {})

        github_client.close_pr.assert_called_once()
        mock_increment.assert_called_once_with("owner/repo", 4636)
        mock_send_feedback.assert_not_called()
        mock_fix_issues.assert_not_called()
        mock_checkout.assert_not_called()
        assert any("Closed stale Jules PR #4643" in action for action in actions)

    @patch("src.auto_coder.pr_processor.cmd.run_command")
    @patch("src.auto_coder.pr_processor._send_jules_error_feedback")
    @patch("src.auto_coder.pr_processor._fix_pr_issues_with_testing")
    @patch("src.auto_coder.pr_processor._checkout_pr_branch")
    @patch("src.auto_coder.pr_processor._check_github_actions_status")
    @patch("src.auto_coder.pr_processor.get_detailed_checks_from_history")
    @patch("src.auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress")
    @patch("src.auto_coder.pr_processor._get_mergeable_state")
    def test_fresh_jules_pr_is_delegated_to_jules_regardless_of_failure_count(
        self,
        mock_mergeable,
        mock_check_in_progress,
        mock_detailed_checks,
        mock_check_status,
        mock_checkout,
        mock_fix_issues,
        mock_send_feedback,
        mock_run_command,
    ):
        """Repeated CI failures never trigger local auto-fix commits on a Jules PR."""
        from src.auto_coder.utils import CommandResult

        github_client = Mock()
        config = AutomationConfig()
        config.JULES_PR_CI_TIMEOUT_HOURS = 12
        pr_data = _jules_pr_data(hours_old=1)

        mock_check_in_progress.return_value = True
        mock_mergeable.return_value = {"mergeable": True}
        mock_check_status.return_value = MagicMock(spec=GitHubActionsStatusResult, success=False, in_progress=False, error=None, ids=[1])
        mock_detailed_checks.return_value = MagicMock(spec=DetailedChecksResult, success=False, failed_checks=[{"name": "test"}])
        mock_run_command.return_value = CommandResult(success=True, stdout="main", stderr="", returncode=0)

        # Many previous failure comments used to trigger the removed local fallback
        target_message = "🤖 Auto-Coder: CI checks failed. I've sent the error logs to the Jules session and requested a fix. Please wait for the updates."
        old_time = (datetime.now(timezone.utc) - timedelta(hours=240)).isoformat()
        github_client.get_pr_comments.return_value = [{"body": target_message, "created_at": old_time}] * 20

        actions = _handle_pr_merge(github_client, "owner/repo", pr_data, config, {})

        mock_send_feedback.assert_called_once()
        mock_fix_issues.assert_not_called()
        mock_checkout.assert_not_called()
        github_client.close_pr.assert_not_called()
        assert "Jules will handle fixing PR #4643, skipping local fixes" in actions[-1]


class TestShouldSkipWaitingForJules:
    """Test cases for _should_skip_waiting_for_jules time-based behavior."""

    def _client_with_wait_comment(self, comment_age_hours: float) -> Mock:
        github_client = Mock()
        target_message = "🤖 Auto-Coder: CI checks failed. I've sent the error logs to the Jules session and requested a fix. Please wait for the updates."
        comment_time = (datetime.now(timezone.utc) - timedelta(hours=comment_age_hours)).isoformat()
        github_client.get_pr_comments.return_value = [{"body": target_message, "created_at": comment_time}]
        commit_time = (datetime.now(timezone.utc) - timedelta(hours=comment_age_hours + 1)).isoformat()
        github_client.get_pr_commits.return_value = [{"commit": {"committer": {"date": commit_time}}}]
        return github_client

    def test_returns_false_after_wait_timeout(self):
        config = AutomationConfig()
        config.JULES_WAIT_TIMEOUT_HOURS = 2
        github_client = self._client_with_wait_comment(comment_age_hours=3)

        assert _should_skip_waiting_for_jules(github_client, "owner/repo", {"number": 123}, config) is False

    def test_returns_true_within_wait_timeout(self):
        config = AutomationConfig()
        config.JULES_WAIT_TIMEOUT_HOURS = 2
        github_client = self._client_with_wait_comment(comment_age_hours=0.5)

        assert _should_skip_waiting_for_jules(github_client, "owner/repo", {"number": 123}, config) is True
