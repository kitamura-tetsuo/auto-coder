from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.branch_manager import BranchManager
from src.auto_coder.pr_processor import _handle_pr_merge
from src.auto_coder.util.github_action import GitHubActionsStatusResult


class TestPRProcessorLocalOverride:
    """Test cases for local override of Jules PR processing."""

    @patch("src.auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress")
    @patch("src.auto_coder.pr_processor._get_mergeable_state")
    @patch("src.auto_coder.pr_processor._is_jules_pr")
    @patch("src.auto_coder.pr_processor.cmd.run_command")
    @patch("src.auto_coder.pr_processor._check_github_actions_status")
    @patch("src.auto_coder.pr_processor.get_detailed_checks_from_history")
    @patch("src.auto_coder.pr_processor._send_jules_error_feedback")
    @patch("src.auto_coder.pr_processor._fix_pr_issues_with_testing")
    @patch("src.auto_coder.pr_processor._checkout_pr_branch")
    @patch("src.auto_coder.pr_processor._update_with_base_branch")
    @patch("src.auto_coder.pr_processor._get_github_actions_logs")
    @patch.object(BranchManager, "__enter__", return_value=MagicMock())
    @patch.object(BranchManager, "__exit__", return_value=None)
    def test_jules_pr_local_override(
        self,
        mock_exit,
        mock_enter,
        mock_get_logs,
        mock_update,
        mock_checkout,
        mock_fix_issues,
        mock_send_feedback,
        mock_get_detailed_checks,
        mock_check_status,
        mock_run_command,
        mock_is_jules_pr,
        mock_get_mergeable_state,
        mock_check_in_progress,
    ):
        """Test that local processing overrides Jules feedback when on PR branch."""
        # Setup
        repo_name = "test/repo"
        pr_branch = "feature/test-branch"
        pr_data = {
            "number": 123,
            "head": {"ref": pr_branch},
            "base": {"ref": "main"},
        }
        config = AutomationConfig()
        config.SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL = True  # Simplify flow

        # Mock Jules PR
        mock_is_jules_pr.return_value = True

        # Mock current branch to match PR branch (Simulate being on the branch)
        mock_run_command.return_value = MagicMock(success=True, stdout=f"{pr_branch}\n")

        # Mock failed checks
        mock_check_in_progress.return_value = True
        mock_get_mergeable_state.return_value = {"mergeable": True}
        mock_check_status.return_value = MagicMock(spec=GitHubActionsStatusResult, success=False, error=None, ids=[1])
        mock_get_detailed_checks.return_value = MagicMock(spec=GitHubActionsStatusResult, success=False, failed_checks=["test_check"])

        # Mock checkout success
        mock_checkout.return_value = True

        # Mock logs
        mock_get_logs.return_value = "Error logs"

        # Execute
        actions = _handle_pr_merge(MagicMock(), repo_name, pr_data, config, {})

        # Assert
        # 1. Should NOT send feedback to Jules
        mock_send_feedback.assert_not_called()

        # 2. Should call _fix_pr_issues_with_testing
        mock_fix_issues.assert_called_once()

        # 3. Should pass skip_github_actions_fix=True because we are on the branch
        call_args = mock_fix_issues.call_args
        assert call_args.kwargs.get("skip_github_actions_fix") is True

        # 4. Verify actions contain expected messages
        assert any("Checked out PR #123 branch" in action for action in actions)
