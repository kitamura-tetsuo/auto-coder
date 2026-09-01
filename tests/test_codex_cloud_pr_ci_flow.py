"""
Tests for Codex Cloud PR CI failure handling and continuation workflow.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.cloud_manager import CloudManager
from auto_coder.pr_processor import (
    CodexCloudFeedbackResult,
    _send_codex_cloud_error_feedback,
    process_pull_request,
)


class TestCodexCloudPRCIFlow:
    """Test suite for Codex Cloud PR CI failure -> continue_if_paused flow."""

    @pytest.fixture
    def config(self):
        cfg = AutomationConfig()
        cfg.SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL = True
        return cfg

    @pytest.fixture
    def mock_github_client(self):
        client = MagicMock()
        client.token = "fake-github-token"
        client.get_repository.return_value = MagicMock()
        return client

    def test_send_codex_cloud_error_feedback_success(self, config, mock_github_client):
        """Test _send_codex_cloud_error_feedback triggers continue_if_paused and posts PR comment."""
        pr_data = {
            "number": 101,
            "title": "Fix bug in parser",
            "body": "Implementation details.\n\nhttps://chatgpt.com/codex/tasks/task_e_11223344",
            "user": {"login": "octocat"},
            "head": {"ref": "codex/issue-101", "sha": "sha101"},
            "base": {"ref": "main"},
        }
        failed_checks = [{"name": "PR Tests", "conclusion": "failure"}]

        with patch("auto_coder.codex_cloud_client.CodexCloudClient.continue_if_paused", return_value=True) as mock_cont:
            result = _send_codex_cloud_error_feedback(
                repo_name="owner/repo",
                pr_data=pr_data,
                failed_checks=failed_checks,
                config=config,
                github_client=mock_github_client,
            )

            mock_cont.assert_called_once()
            (task_id,) = mock_cont.call_args.args
            prompt = mock_cont.call_args.kwargs["prompt"]
            assert task_id == "task_e_11223344"
            assert "pull request #101" in prompt
            assert "codex/issue-101" in prompt
            assert "sha101" in prompt
            assert "Do not create a new branch." in prompt
            assert "Do not create a new pull request." in prompt
            assert result.delivered is True
            assert result.retryable is False
            assert any("Sent continuation request to Codex Cloud task 'task_e_11223344'" in a for a in result.actions)
            assert any("Posted comment on PR #101" in a for a in result.actions)
            mock_github_client.add_comment_to_pr.assert_called_once()
            comment = mock_github_client.add_comment_to_pr.call_args[0][2]
            assert "Codex Cloud" in comment

    def test_send_codex_cloud_error_feedback_skips_duplicate_comment(self, config, mock_github_client):
        comment = "🤖 Auto-Coder: CI checks failed. I've requested continuation from Codex Cloud to resolve the failures. Please wait for updates."
        mock_github_client.get_pr_comments.return_value = [{"body": comment}]
        pr_data = {
            "number": 101,
            "body": "https://chatgpt.com/codex/tasks/task_e_11223344",
        }

        with patch("auto_coder.codex_cloud_client.CodexCloudClient.continue_if_paused", return_value=True):
            result = _send_codex_cloud_error_feedback(
                repo_name="owner/repo",
                pr_data=pr_data,
                failed_checks=[{"name": "PR Tests", "conclusion": "failure"}],
                config=config,
                github_client=mock_github_client,
            )

        mock_github_client.add_comment_to_pr.assert_not_called()
        assert result.delivered is True
        assert "Skipped duplicate Codex Cloud fix-request comment on PR #101" in result.actions

    def test_send_codex_cloud_error_feedback_resolves_task_from_cloud_manager(self, config, tmp_path):
        """Test resolving task ID via CloudManager when not directly in PR body."""
        repo = "owner/repo"
        cloud_csv = tmp_path / "cloud.csv"
        cloud_manager = CloudManager(repo, cloud_file_path=cloud_csv)
        cloud_manager.add_session(50, "task_e_cloudmgr_999")

        pr_data = {
            "number": 102,
            "title": "Resolve issue #50",
            "body": "This fixes #50",
            "user": {"login": "octocat"},
        }

        with (
            patch("auto_coder.pr_processor.CloudManager", return_value=cloud_manager),
            patch("auto_coder.codex_cloud_client.CodexCloudClient.continue_if_paused", return_value=True) as mock_cont,
        ):
            result = _send_codex_cloud_error_feedback(
                repo_name=repo,
                pr_data=pr_data,
                failed_checks=[{"name": "Lint", "conclusion": "failure"}],
                config=config,
            )

            mock_cont.assert_called_once_with("task_e_cloudmgr_999")
            assert result.delivered is True
            assert any("Sent continuation request to Codex Cloud task 'task_e_cloudmgr_999'" in a for a in result.actions)

    def test_send_codex_cloud_error_feedback_when_task_cannot_be_resumed(self, config):
        """Test handling when continue_if_paused returns False."""
        pr_data = {
            "number": 103,
            "title": "Feature branch",
            "body": "https://chatgpt.com/codex/tasks/task_e_non_resumable",
            "user": {"login": "octocat"},
        }

        with patch("auto_coder.codex_cloud_client.CodexCloudClient.continue_if_paused", return_value=False) as mock_cont:
            result = _send_codex_cloud_error_feedback(
                repo_name="owner/repo",
                pr_data=pr_data,
                failed_checks=[{"name": "Tests", "conclusion": "failure"}],
                config=config,
            )

            mock_cont.assert_called_once_with("task_e_non_resumable")
            assert result.delivered is False
            assert result.retryable is True
            assert any("could not be resumed" in a for a in result.actions)

    def test_send_codex_cloud_error_feedback_missing_task_id(self, config):
        """Test safe failure when no task ID can be found."""
        pr_data = {
            "number": 104,
            "title": "PR with no task link",
            "body": "Regular description with no task info",
            "user": {"login": "codex-bot"},
        }

        with patch("auto_coder.codex_cloud_client.CodexCloudClient.continue_if_paused") as mock_cont:
            result = _send_codex_cloud_error_feedback(
                repo_name="owner/repo",
                pr_data=pr_data,
                failed_checks=[{"name": "Tests", "conclusion": "failure"}],
                config=config,
            )

            mock_cont.assert_not_called()
            assert result.delivered is False
            assert result.retryable is True
            assert any("no valid Codex task ID found" in a for a in result.actions)

    def test_process_pull_request_routes_codex_pr_ci_failure_to_cloud_continuation(self, config, mock_github_client):
        """End-to-end test: process_pull_request intercepts failing Codex PR and delegates to Codex Cloud continuation."""
        from auto_coder.util.github_action import DetailedChecksResult, GitHubActionsStatusResult

        pr_data = {
            "number": 200,
            "title": "Implement feature",
            "body": "Closes #15\n\nhttps://chatgpt.com/codex/tasks/task_e_pr200",
            "state": "open",
            "user": {"login": "octocat"},
            "head": {"ref": "feature-branch", "sha": "abcdef123456"},
            "base": {"ref": "main"},
            "mergeable": True,
            "draft": False,
        }

        mock_checks = GitHubActionsStatusResult(
            success=False,
            ids=[1001],
            in_progress=False,
        )

        mock_detailed = DetailedChecksResult(
            success=False,
            total_checks=1,
            failed_checks=[{"name": "Tests", "conclusion": "failure"}],
            all_checks=[{"name": "Tests", "conclusion": "failure"}],
            has_in_progress=False,
        )

        with (
            patch("auto_coder.pr_processor.CommandExecutor.run_command") as mock_run_cmd,
            patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True),
            patch("auto_coder.pr_processor._check_github_actions_status", return_value=mock_checks),
            patch("auto_coder.pr_processor.get_detailed_checks_from_history", return_value=mock_detailed),
            patch(
                "auto_coder.pr_processor._send_codex_cloud_error_feedback",
                return_value=CodexCloudFeedbackResult(
                    delivered=True,
                    actions=("Sent continuation request to Codex Cloud task 'task_e_pr200' for PR #200",),
                ),
            ) as mock_feedback,
        ):
            # Mock current branch check
            mock_res = MagicMock(success=True, stdout="main")
            mock_run_cmd.return_value = mock_res

            result = process_pull_request(
                github_client=mock_github_client,
                config=config,
                repo_name="owner/repo",
                pr_data=pr_data,
            )

            mock_feedback.assert_called_once()
            actions = result.actions_taken
            assert any("Codex-created PR, sending continuation request to Codex Cloud" in a for a in actions)
            assert any("Codex Cloud will handle fixing PR #200, skipping local fixes" in a for a in actions)

    @pytest.mark.parametrize(
        ("check_labels", "current_branch"),
        [
            (False, "feature-branch"),
            (True, "feature-branch"),
        ],
        ids=["wip-resumption-or-only", "already-on-pr-branch"],
    )
    def test_codex_cloud_origin_prevents_local_ci_repair(
        self,
        config,
        mock_github_client,
        check_labels,
        current_branch,
    ):
        """Codex Cloud routing is independent of label mode and checkout state."""
        from auto_coder.util.github_action import DetailedChecksResult, GitHubActionsStatusResult

        config.CHECK_LABELS = check_labels
        pr_data = {
            "number": 201,
            "title": "Implement cloud feature",
            "body": "https://chatgpt.com/codex/tasks/task_e_pr201",
            "state": "open",
            "user": {"login": "octocat"},
            "head": {"ref": "feature-branch", "sha": "abcdef123456"},
            "base": {"ref": "main"},
            "mergeable": True,
            "draft": False,
        }
        mock_checks = GitHubActionsStatusResult(success=False, ids=[1002], in_progress=False)
        mock_detailed = DetailedChecksResult(
            success=False,
            total_checks=1,
            failed_checks=[{"name": "Tests", "conclusion": "failure"}],
            all_checks=[{"name": "Tests", "conclusion": "failure"}],
            has_in_progress=False,
        )

        with (
            patch("auto_coder.pr_processor.CommandExecutor.run_command") as run_command,
            patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True),
            patch("auto_coder.pr_processor._check_github_actions_status", return_value=mock_checks),
            patch("auto_coder.pr_processor.get_detailed_checks_from_history", return_value=mock_detailed),
            patch(
                "auto_coder.pr_processor._send_codex_cloud_error_feedback",
                return_value=CodexCloudFeedbackResult(delivered=True, actions=("delegated",)),
            ) as feedback,
            patch("auto_coder.pr_processor._checkout_pr_branch") as checkout,
            patch("auto_coder.pr_processor.BranchManager") as branch_manager,
            patch("auto_coder.pr_processor._fix_pr_issues_with_testing") as local_repair,
        ):
            run_command.return_value = MagicMock(success=True, stdout=current_branch)

            result = process_pull_request(
                github_client=mock_github_client,
                config=config,
                repo_name="owner/repo",
                pr_data=pr_data,
            )

        assert result.actions_taken[-2:] == ["delegated", "Codex Cloud will handle fixing PR #201, skipping local fixes"]
        run_command.assert_not_called()
        feedback.assert_called_once()
        checkout.assert_not_called()
        branch_manager.assert_not_called()
        local_repair.assert_not_called()

    def test_failed_codex_cloud_delivery_remains_retryable_without_local_repair(self, config, mock_github_client):
        """A rejected continuation is explicit and never reported as cloud-handled."""
        from auto_coder.util.github_action import DetailedChecksResult, GitHubActionsStatusResult

        config.CHECK_LABELS = False
        pr_data = {
            "number": 202,
            "title": "Implement cloud feature",
            "body": "https://chatgpt.com/codex/tasks/task_e_pr202",
            "state": "open",
            "user": {"login": "octocat"},
            "head": {"ref": "feature-branch", "sha": "abcdef123456"},
            "base": {"ref": "main"},
            "mergeable": True,
            "draft": False,
        }
        mock_checks = GitHubActionsStatusResult(success=False, ids=[1003], in_progress=False)
        mock_detailed = DetailedChecksResult(
            success=False,
            total_checks=1,
            failed_checks=[{"name": "Tests", "conclusion": "failure"}],
            all_checks=[{"name": "Tests", "conclusion": "failure"}],
            has_in_progress=False,
        )

        with (
            patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True),
            patch("auto_coder.pr_processor._check_github_actions_status", return_value=mock_checks),
            patch("auto_coder.pr_processor.get_detailed_checks_from_history", return_value=mock_detailed),
            patch("auto_coder.codex_cloud_client.CodexCloudClient.continue_if_paused", return_value=False) as continuation,
            patch("auto_coder.pr_processor._checkout_pr_branch") as checkout,
            patch("auto_coder.pr_processor.BranchManager") as branch_manager,
            patch("auto_coder.pr_processor._fix_pr_issues_with_testing") as local_repair,
        ):
            result = process_pull_request(
                github_client=mock_github_client,
                config=config,
                repo_name="owner/repo",
                pr_data=pr_data,
            )

        continuation.assert_called_once()
        assert any("could not be resumed" in action for action in result.actions_taken)
        assert any("retry is required and local fixes remain disabled" in action for action in result.actions_taken)
        assert not any("Codex Cloud will handle fixing" in action for action in result.actions_taken)
        checkout.assert_not_called()
        branch_manager.assert_not_called()
        local_repair.assert_not_called()
