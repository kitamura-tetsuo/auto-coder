"""Tests for git push failure handling with sys.exit()."""

import pytest
from unittest.mock import MagicMock, Mock, patch

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.utils import CommandResult


class TestPRProcessorPushFailure:
    """Test that PR processor exits on git push failure."""

    @patch("src.auto_coder.pr_processor.ensure_pushed")
    @patch("src.auto_coder.pr_processor.sys.exit")
    def test_handle_pr_merge_exits_on_ensure_pushed_failure(
        self, mock_exit, mock_ensure_pushed
    ):
        """Test that _handle_pr_merge exits when ensure_pushed fails."""
        from src.auto_coder.pr_processor import _handle_pr_merge

        # Setup
        mock_ensure_pushed.return_value = CommandResult(
            success=False, stdout="", stderr="Failed to push", returncode=1
        )

        pr_data = {
            "number": 123,
            "title": "Test PR",
            "head": {"ref": "feature-branch"},
            "base": {"ref": "main"},
            "mergeable": True,
        }
        analysis = {"priority": "merge"}
        config = AutomationConfig()

        # Execute
        _handle_pr_merge("test/repo", pr_data, config, False, analysis, None)

        # Assert
        mock_exit.assert_called_once_with(1)




class TestIssueProcessorPushFailure:
    """Test that issue processor exits on git push failure."""

    @patch("src.auto_coder.issue_processor.git_push")
    @patch("src.auto_coder.issue_processor.git_commit_with_retry")
    @patch("src.auto_coder.issue_processor.sys.exit")
    @patch("src.auto_coder.issue_processor.cmd")
    def test_commit_changes_exits_on_push_failure(
        self, mock_cmd, mock_exit, mock_commit, mock_git_push
    ):
        """Test that _commit_changes exits when git push fails after retry and no LLM client is available."""
        from src.auto_coder.issue_processor import _commit_changes

        # Setup - commit succeeds, first push fails, retry also fails
        mock_commit.return_value = CommandResult(
            success=True, stdout="", stderr="", returncode=0
        )
        # First call at line 526, second at 548, third at 557
        mock_git_push.side_effect = [
            CommandResult(success=True, stdout="", stderr="", returncode=0),  # Line 526
            CommandResult(success=False, stdout="", stderr="Push failed", returncode=1),  # Line 548
            CommandResult(success=False, stdout="", stderr="Push failed", returncode=1),  # Line 557
        ]
        # Mock git status to show changes
        mock_cmd.run_command.side_effect = [
            CommandResult(success=True, stdout="M file.py", stderr="", returncode=0),  # status
            CommandResult(success=True, stdout="", stderr="", returncode=0),  # add
        ]

        result_data = {"summary": "Test changes"}

        # Execute - no LLM client provided
        _commit_changes(result_data, "test/repo", 123)

        # Assert
        assert mock_git_push.call_count == 3  # Line 526 + 548 + 557
        mock_exit.assert_called_once_with(1)

    @patch("src.auto_coder.issue_processor.git_push")
    @patch("src.auto_coder.issue_processor.git_commit_with_retry")
    @patch("src.auto_coder.issue_processor.sys.exit")
    @patch("src.auto_coder.issue_processor.cmd")
    @patch("src.auto_coder.issue_processor._try_llm_commit_push")
    def test_commit_changes_uses_llm_on_push_failure(
        self, mock_try_llm, mock_cmd, mock_exit, mock_commit, mock_git_push
    ):
        """Test that _commit_changes uses LLM to resolve push failure when LLM client is available."""
        from src.auto_coder.issue_processor import _commit_changes

        # Setup - commit succeeds, first push fails, retry also fails
        mock_commit.return_value = CommandResult(
            success=True, stdout="", stderr="", returncode=0
        )
        mock_git_push.side_effect = [
            CommandResult(success=True, stdout="", stderr="", returncode=0),  # Line 526
            CommandResult(success=False, stdout="", stderr="Push failed", returncode=1),  # Line 548
            CommandResult(success=False, stdout="", stderr="Push failed", returncode=1),  # Line 557
        ]
        # Mock git status to show changes
        mock_cmd.run_command.side_effect = [
            CommandResult(success=True, stdout="M file.py", stderr="", returncode=0),  # status
            CommandResult(success=True, stdout="", stderr="", returncode=0),  # add
        ]

        # Mock LLM to successfully resolve the issue
        mock_try_llm.return_value = True

        result_data = {"summary": "Test changes"}
        mock_llm_client = Mock()

        # Execute - with LLM client
        result = _commit_changes(result_data, "test/repo", 123, llm_client=mock_llm_client)

        # Assert
        assert mock_git_push.call_count == 3  # Line 526 + 548 + 557
        mock_try_llm.assert_called_once()
        mock_exit.assert_not_called()
        assert "Successfully committed and pushed changes using LLM" in result

    @patch("src.auto_coder.issue_processor.git_push")
    @patch("src.auto_coder.issue_processor.git_commit_with_retry")
    @patch("src.auto_coder.issue_processor.sys.exit")
    @patch("src.auto_coder.issue_processor.cmd")
    @patch("src.auto_coder.issue_processor._try_llm_commit_push")
    def test_commit_changes_exits_when_llm_fails(
        self, mock_try_llm, mock_cmd, mock_exit, mock_commit, mock_git_push
    ):
        """Test that _commit_changes exits when LLM fails to resolve push failure."""
        from src.auto_coder.issue_processor import _commit_changes

        # Setup - commit succeeds, first push fails, retry also fails
        mock_commit.return_value = CommandResult(
            success=True, stdout="", stderr="", returncode=0
        )
        mock_git_push.side_effect = [
            CommandResult(success=True, stdout="", stderr="", returncode=0),  # Line 526
            CommandResult(success=False, stdout="", stderr="Push failed", returncode=1),  # Line 548
            CommandResult(success=False, stdout="", stderr="Push failed", returncode=1),  # Line 557
        ]
        # Mock git status to show changes
        mock_cmd.run_command.side_effect = [
            CommandResult(success=True, stdout="M file.py", stderr="", returncode=0),  # status
            CommandResult(success=True, stdout="", stderr="", returncode=0),  # add
        ]

        # Mock LLM to fail to resolve the issue
        mock_try_llm.return_value = False

        result_data = {"summary": "Test changes"}
        mock_llm_client = Mock()

        # Execute - with LLM client that fails
        _commit_changes(result_data, "test/repo", 123, llm_client=mock_llm_client)

        # Assert
        assert mock_git_push.call_count == 3  # Line 526 + 548 + 557
        mock_try_llm.assert_called_once()
        mock_exit.assert_called_once_with(1)


class TestTryLLMCommitPush:
    """Test _try_llm_commit_push helper function."""

    @patch("src.auto_coder.issue_processor.render_prompt")
    @patch("src.auto_coder.issue_processor.cmd")
    def test_try_llm_commit_push_success(self, mock_cmd, mock_render_prompt):
        """Test that _try_llm_commit_push returns True when LLM successfully resolves the issue."""
        from src.auto_coder.issue_processor import _try_llm_commit_push

        # Setup
        mock_llm_client = Mock()
        mock_llm_client._run_llm_cli.return_value = "COMMIT_PUSH_RESULT: SUCCESS"
        mock_render_prompt.return_value = "Test prompt"

        # Mock git status to show no uncommitted changes
        mock_cmd.run_command.side_effect = [
            CommandResult(success=True, stdout="", stderr="", returncode=0),  # git status
            CommandResult(success=True, stdout="", stderr="", returncode=0),  # git log
        ]

        # Execute
        result = _try_llm_commit_push(
            "Test commit message",
            "Push failed",
            llm_client=mock_llm_client,
        )

        # Assert
        assert result is True
        mock_llm_client._run_llm_cli.assert_called_once()
        mock_render_prompt.assert_called_once_with(
            "tests.commit_and_push",
            commit_message="Test commit message",
            error_message="Push failed",
        )

    @patch("src.auto_coder.issue_processor.render_prompt")
    @patch("src.auto_coder.issue_processor.cmd")
    def test_try_llm_commit_push_failure(self, mock_cmd, mock_render_prompt):
        """Test that _try_llm_commit_push returns False when LLM fails to resolve the issue."""
        from src.auto_coder.issue_processor import _try_llm_commit_push

        # Setup
        mock_llm_client = Mock()
        mock_llm_client._run_llm_cli.return_value = "COMMIT_PUSH_RESULT: FAILED: Could not resolve conflict"
        mock_render_prompt.return_value = "Test prompt"

        # Execute
        result = _try_llm_commit_push(
            "Test commit message",
            "Push failed",
            llm_client=mock_llm_client,
        )

        # Assert
        assert result is False
        mock_llm_client._run_llm_cli.assert_called_once()

    @patch("src.auto_coder.issue_processor.render_prompt")
    def test_try_llm_commit_push_no_client(self, mock_render_prompt):
        """Test that _try_llm_commit_push returns False when no LLM client is available."""
        from src.auto_coder.issue_processor import _try_llm_commit_push

        # Execute
        result = _try_llm_commit_push(
            "Test commit message",
            "Push failed",
            llm_client=None,
            message_backend_manager=None,
        )

        # Assert
        assert result is False
        mock_render_prompt.assert_not_called()

    @patch("src.auto_coder.issue_processor.render_prompt")
    @patch("src.auto_coder.issue_processor.cmd")
    def test_try_llm_commit_push_uncommitted_changes(self, mock_cmd, mock_render_prompt):
        """Test that _try_llm_commit_push returns False when there are still uncommitted changes."""
        from src.auto_coder.issue_processor import _try_llm_commit_push

        # Setup
        mock_llm_client = Mock()
        mock_llm_client._run_llm_cli.return_value = "COMMIT_PUSH_RESULT: SUCCESS"
        mock_render_prompt.return_value = "Test prompt"

        # Mock git status to show uncommitted changes
        mock_cmd.run_command.return_value = CommandResult(
            success=True, stdout="M file.py", stderr="", returncode=0
        )

        # Execute
        result = _try_llm_commit_push(
            "Test commit message",
            "Push failed",
            llm_client=mock_llm_client,
        )

        # Assert
        assert result is False
        mock_llm_client._run_llm_cli.assert_called_once()


class TestFixToPassTestsRunnerPushFailure:
    """Test that fix_to_pass_tests_runner exits on git push failure."""

    @patch("src.auto_coder.fix_to_pass_tests_runner.git_push")
    @patch("src.auto_coder.fix_to_pass_tests_runner.sys.exit")
    @patch("src.auto_coder.fix_to_pass_tests_runner.run_local_tests")
    @patch("src.auto_coder.fix_to_pass_tests_runner.cmd")
    @patch("src.auto_coder.fix_to_pass_tests_runner.git_commit_with_retry")
    def test_fix_to_pass_tests_exits_on_push_failure(
        self, mock_commit, mock_cmd, mock_test, mock_exit, mock_git_push
    ):
        """Test that fix_to_pass_tests exits when git push fails."""
        from src.auto_coder.fix_to_pass_tests_runner import fix_to_pass_tests

        # Setup
        mock_git_push.return_value = CommandResult(
            success=False, stdout="", stderr="Push failed", returncode=1
        )

        config = AutomationConfig()
        mock_llm_backend_manager = Mock()
        mock_message_backend_manager = Mock()

        # Mock LLM response
        mock_backend = Mock()
        mock_backend.fix_workspace.return_value = Mock(
            summary="Fixed the issue",
            raw_response="Fixed",
            backend="codex",
            model="test-model",
        )
        mock_llm_backend_manager.get_current_backend.return_value = mock_backend
        mock_llm_backend_manager.switch_to_default_backend = Mock()

        # First test fails, second test passes
        mock_test.side_effect = [
            {"success": False, "output": "Test failed", "errors": "Error"},
            {"success": True, "output": "All tests passed", "errors": ""},
        ]
        mock_cmd.run_command.return_value = CommandResult(
            success=True, stdout="", stderr="", returncode=0
        )
        mock_commit.return_value = CommandResult(
            success=True, stdout="", stderr="", returncode=0
        )

        # Execute
        fix_to_pass_tests(
            config,
            False,
            mock_llm_backend_manager,
            mock_message_backend_manager,
        )

        # Assert
        mock_exit.assert_called_once_with(1)

