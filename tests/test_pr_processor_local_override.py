from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.pr_processor import _handle_pr_merge
from src.auto_coder.util.github_action import GitHubActionsStatusResult


@contextmanager
def fake_branch_context(*args, **kwargs):
    """Fake branch context manager that does nothing."""
    yield


class TestPRProcessorLocalOverride:
    """Test cases for local override of Jules PR processing."""

    def test_jules_pr_local_override(self):
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

        # Create mocks
        mock_check_in_progress = MagicMock()
        mock_get_mergeable_state = MagicMock()
        mock_is_jules_pr = MagicMock()
        mock_run_command = MagicMock()
        mock_check_status = MagicMock()
        mock_get_detailed_checks = MagicMock()
        mock_checkout = MagicMock()
        mock_get_logs = MagicMock()
        mock_send_feedback = MagicMock()
        mock_fix_issues = MagicMock()

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

        with patch("src.auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", mock_check_in_progress):
            with patch("src.auto_coder.pr_processor._get_mergeable_state", mock_get_mergeable_state):
                with patch("src.auto_coder.pr_processor._is_jules_pr", mock_is_jules_pr):
                    with patch("src.auto_coder.pr_processor.cmd.run_command", mock_run_command):
                        with patch("src.auto_coder.pr_processor._check_github_actions_status", mock_check_status):
                            with patch("src.auto_coder.pr_processor.get_detailed_checks_from_history", mock_get_detailed_checks):
                                with patch("src.auto_coder.pr_processor._send_jules_error_feedback", mock_send_feedback):
                                    with patch("src.auto_coder.pr_processor._fix_pr_issues_with_testing", mock_fix_issues):
                                        with patch("src.auto_coder.pr_processor._checkout_pr_branch", mock_checkout):
                                            with patch("src.auto_coder.pr_processor._update_with_base_branch", MagicMock()):
                                                with patch("src.auto_coder.pr_processor._get_github_actions_logs", mock_get_logs):
                                                    with patch("src.auto_coder.pr_processor.BranchManager", fake_branch_context):
                                                        # Execute
                                                        actions = _handle_pr_merge(MagicMock(), repo_name, pr_data, config, {})

        # Assert
        # 1. Should NOT send feedback to Jules
        mock_send_feedback.assert_not_called()

        # 2. Should call _fix_pr_issues_with_testing
        mock_fix_issues.assert_called_once()

        # 3. Verify actions contain expected messages
        assert any("Checked out PR #123 branch" in action for action in actions)
