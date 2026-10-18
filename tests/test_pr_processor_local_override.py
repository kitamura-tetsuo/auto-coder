from contextlib import nullcontext
from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.ci_repair_authority import CIRepairAuthority
from src.auto_coder.pr_processor import PRActionList, ProcessedPRResult, PRProcessingOutcome, _handle_pr_merge
from src.auto_coder.pr_repair_dispatch import PRRepairResult, RepairDisposition
from tests.ci_observation_fakes import authoritative_ci_status


def _client():
    client = MagicMock()
    client.get_pr_review_threads_strict.return_value = []
    return client


from src.auto_coder.util.github_action import DetailedChecksResult, GitHubActionsStatusResult


@pytest.fixture(autouse=True)
def admitted_ci_repair():
    """Local override tests exercise behavior after exact-head admission."""
    authority = CIRepairAuthority(True, "current exact-head CI failure", "test-head")
    with patch(
        "src.auto_coder.pr_processor.current_ci_failure_authority",
        return_value=nullcontext(authority),
    ):
        yield


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
    @patch("src.auto_coder.pr_processor.BranchManager")
    @patch("src.auto_coder.pr_processor._get_github_actions_logs")
    def test_jules_pr_local_override(
        self,
        mock_get_logs,
        mock_branch_manager,
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
            "head": {"ref": pr_branch, "sha": "head-local"},
            "base": {"ref": "main"},
        }
        config = AutomationConfig()
        config.SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL = True  # Simplify flow

        # Mock Jules PR
        mock_is_jules_pr.return_value = True

        # Mock BranchManager
        mock_branch_manager.return_value.__enter__.return_value = MagicMock()

        # Mock current branch to match PR branch (Simulate being on the branch)
        mock_run_command.return_value = MagicMock(success=True, stdout=f"{pr_branch}\n")

        # Mock failed checks
        mock_check_in_progress.return_value = True
        mock_get_mergeable_state.return_value = {"mergeable": True}
        mock_check_status.side_effect = authoritative_ci_status(success=False, ids=[1])
        mock_get_detailed_checks.return_value = MagicMock(spec=DetailedChecksResult, success=False, failed_checks=["test_check"])

        # Mock checkout success
        mock_checkout.return_value = True

        # Mock logs
        mock_get_logs.return_value = "Error logs"

        # Execute
        actions = _handle_pr_merge(_client(), repo_name, pr_data, config, {})

        # Assert
        # 1. Should NOT send feedback to Jules
        mock_send_feedback.assert_not_called()

        # 2. Should call _fix_pr_issues_with_testing
        mock_fix_issues.assert_called_once()

        # 3. Resumption on the PR branch must not drop the CI evidence
        call_args = mock_fix_issues.call_args
        assert "skip_github_actions_fix" not in call_args.kwargs

        # 4. Verify actions contain expected messages
        assert any("Checked out PR #123 branch" in action for action in actions)


@pytest.mark.parametrize("disposition", [RepairDisposition.NO_UPDATE_NEEDED, RepairDisposition.FAILED, RepairDisposition.QUALITY_REFUSED])
@pytest.mark.parametrize("include_degrading_flag", [False, True])
def test_ci_base_update_consumer_ignores_identical_misleading_actions(disposition, include_degrading_flag):
    """REQ-003: the CI repair transition is authorized only by typed evidence."""
    repo_name = "test/repo"
    pr_data = {"number": 2540, "head": {"ref": "feature", "sha": "head"}, "base": {"ref": "main"}}
    config = AutomationConfig()
    config.SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL = False
    display = ["up to date with main branch", "Pushed updated branch", "ACTION_FLAG:SKIP_ANALYSIS"]
    if include_degrading_flag:
        display.append("ACTION_FLAG:DEGRADING_MERGE_SKIP_MERGE")
    produced = PRActionList(display)
    produced.repair_result = PRRepairResult(disposition, reason="comparison failed")
    status = ProcessedPRResult(pr_data=pr_data, actions_taken=[], priority="fix", analysis=None)
    client = _client()

    with (
        patch("src.auto_coder.pr_processor.GitHubClient.get_instance", return_value=client),
        patch("src.auto_coder.pr_processor._reject_unsafe_codex_cloud_pr") as unsafe,
        patch("src.auto_coder.pr_processor._is_pr_review_thread_gate_enabled", return_value=False),
        patch("src.auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True),
        patch("src.auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True}),
        patch("src.auto_coder.pr_processor._check_github_actions_status", side_effect=authoritative_ci_status(success=False, ids=[1])),
        patch("src.auto_coder.pr_processor.get_detailed_checks_from_history", return_value=MagicMock(failed_checks=["PR Tests"])),
        patch("src.auto_coder.pr_processor.check_pr_repair_exhaustion", return_value=None),
        patch("src.auto_coder.pr_processor._is_jules_pr", return_value=False),
        patch("src.auto_coder.pr_processor._is_codex_pr", return_value=False),
        patch("src.auto_coder.pr_processor._is_dependabot_pr", return_value=False),
        patch("src.auto_coder.pr_processor._is_local_llm_pr", return_value=True),
        patch("src.auto_coder.pr_processor._checkout_pr_branch", return_value=True),
        patch("src.auto_coder.pr_processor.BranchManager", return_value=nullcontext()),
        patch("src.auto_coder.pr_processor._update_with_base_branch", return_value=produced) as update,
        patch("src.auto_coder.pr_processor._is_automatic_test_fix_enabled", return_value=True),
        patch("src.auto_coder.pr_processor._get_github_actions_logs", return_value="logs"),
        patch("src.auto_coder.pr_processor._fix_pr_issues_with_testing", return_value=PRActionList()) as fix,
        patch("src.auto_coder.pr_processor._merge_pr") as merge,
    ):
        unsafe.return_value = MagicMock(metadata_error=None, actions=[], authoritative_pr_data=pr_data, closed=False)
        actions = _handle_pr_merge(client, repo_name, pr_data, config, {}, status)

    update.assert_called_once()
    merge.assert_not_called()
    if disposition is RepairDisposition.QUALITY_REFUSED:
        client.close_pr.assert_called_once()
        fix.assert_not_called()
        return
    assert "ACTION_FLAG:SKIP_ANALYSIS" not in actions
    client.close_pr.assert_not_called()
    if disposition is RepairDisposition.NO_UPDATE_NEEDED:
        fix.assert_called_once()
        assert status.outcome is not PRProcessingOutcome.FAILED
    else:
        fix.assert_not_called()
        assert status.outcome is PRProcessingOutcome.FAILED
