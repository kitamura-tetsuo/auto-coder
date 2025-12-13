from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.pr_processor import _handle_pr_merge


class TestPRProcessorLocalOverride:
    """Test cases for local override of Jules PR processing."""

    @patch("src.auto_coder.pr_processor._is_jules_pr")
    @patch("src.auto_coder.pr_processor.cmd.run_command")
    @patch("src.auto_coder.pr_processor._check_github_actions_status")
    @patch("src.auto_coder.pr_processor.get_detailed_checks_from_history")
    @patch("src.auto_coder.pr_processor._send_jules_error_feedback")
    @patch("src.auto_coder.pr_processor._fix_pr_issues_with_testing")
    @patch("src.auto_coder.pr_processor._checkout_pr_branch")
    @patch("src.auto_coder.pr_processor._update_with_base_branch")
    @patch("src.auto_coder.pr_processor._get_github_actions_logs")
    def test_jules_pr_local_override(
        self,
        mock_get_logs,
        mock_update,
        mock_checkout,
        mock_fix_issues,
        mock_send_feedback,
        mock_get_detailed_checks,
        mock_check_status,
        mock_run_command,
        mock_is_jules_pr,
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
        mock_check_status.return_value = MagicMock(success=False)
        mock_get_detailed_checks.return_value = MagicMock(success=False, failed_checks=["test_check"])

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
