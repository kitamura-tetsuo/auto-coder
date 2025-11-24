"Tests for git_commit module."

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.git_commit import git_push, save_commit_failure_history
from src.auto_coder.utils import CommandResult


@pytest.mark.usefixtures("_use_custom_subprocess_mock")
class TestGitPush:
    """Tests for git_push function."""

    def test_successful_push(self):
        """Test successful push without branch specified."""
        with patch("src.auto_coder.git_commit.CommandExecutor") as mock_executor_utils, patch("src.auto_coder.git_info.CommandExecutor") as mock_executor_info:
            mock_cmd = MagicMock()
            mock_executor_utils.return_value = mock_cmd
            mock_executor_info.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
                CommandResult(success=True, stdout="2\n", stderr="", returncode=0),  # 2 unpushed commits
                CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
                CommandResult(success=True, stdout="", stderr="", returncode=0),
            ]

            result = git_push()

            assert result.success is True
            assert mock_cmd.run_command.call_count == 4
            last_call_args = mock_cmd.run_command.call_args_list[3][0][0]
            assert last_call_args == ["git", "push", "origin", "main"]

    def test_push_with_branch(self):
        """Test push with specific branch."""
        with patch("src.auto_coder.git_commit.CommandExecutor") as mock_executor_utils, patch("src.auto_coder.git_info.CommandExecutor") as mock_executor_info:
            mock_cmd = MagicMock()
            mock_executor_utils.return_value = mock_cmd
            mock_executor_info.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(success=True, stdout="", stderr="", returncode=0)

            result = git_push(branch="feature-branch")

            assert result.success is True
            assert mock_cmd.run_command.call_count == 1
            call_args = mock_cmd.run_command.call_args[0][0]
            assert call_args == ["git", "push", "origin", "feature-branch"]

    def test_push_with_custom_remote(self):
        """Test push with custom remote."""
        with patch("src.auto_coder.git_commit.CommandExecutor") as mock_executor_utils, patch("src.auto_coder.git_info.CommandExecutor") as mock_executor_info:
            mock_cmd = MagicMock()
            mock_executor_utils.return_value = mock_cmd
            mock_executor_info.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(success=True, stdout="", stderr="", returncode=0)

            result = git_push(remote="upstream", branch="main")

            assert result.success is True
            assert mock_cmd.run_command.call_count == 1
            call_args = mock_cmd.run_command.call_args[0][0]
            assert call_args == ["git", "push", "upstream", "main"]

    def test_push_failure(self):
        """Test push failure."""
        with patch("src.auto_coder.git_commit.CommandExecutor") as mock_executor_utils, patch("src.auto_coder.git_info.CommandExecutor") as mock_executor_info:
            mock_cmd = MagicMock()
            mock_executor_utils.return_value = mock_cmd
            mock_executor_info.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
                CommandResult(success=True, stdout="2\n", stderr="", returncode=0),
                CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
                CommandResult(
                    success=False,
                    stdout="",
                    stderr="error: failed to push some refs",
                    returncode=1,
                ),
            ]

            result = git_push()

            assert result.success is False
            assert "failed to push" in result.stderr

    def test_push_with_cwd(self):
        """Test push with custom working directory."""
        with patch("src.auto_coder.git_commit.CommandExecutor") as mock_executor_utils, patch("src.auto_coder.git_info.CommandExecutor") as mock_executor_info:
            mock_cmd = MagicMock()
            mock_executor_utils.return_value = mock_cmd
            mock_executor_info.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
                CommandResult(success=True, stdout="2\n", stderr="", returncode=0),
                CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
                CommandResult(success=True, stdout="", stderr="", returncode=0),
            ]

            result = git_push(cwd="/custom/path")

            assert result.success is True
            assert mock_cmd.run_command.call_count == 4
            assert mock_cmd.run_command.call_args_list[0][1]["cwd"] == "/custom/path"
            assert mock_cmd.run_command.call_args_list[1][1]["cwd"] == "/custom/path"
            assert mock_cmd.run_command.call_args_list[2][1]["cwd"] == "/custom/path"
            assert mock_cmd.run_command.call_args_list[3][1]["cwd"] == "/custom/path"

    def test_push_no_upstream_auto_retry(self):
        """Test push automatically retries with --set-upstream when upstream is not set."""
        with patch("src.auto_coder.git_commit.CommandExecutor") as mock_executor_utils, patch("src.auto_coder.git_info.CommandExecutor") as mock_executor_info:
            mock_cmd = MagicMock()
            mock_executor_utils.return_value = mock_cmd
            mock_executor_info.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                CommandResult(success=True, stdout="issue-733\n", stderr="", returncode=0),
                CommandResult(success=True, stdout="2\n", stderr="", returncode=0),
                CommandResult(success=True, stdout="issue-733\n", stderr="", returncode=0),
                CommandResult(
                    success=False,
                    stdout="",
                    stderr="fatal: The current branch issue-733 has no upstream branch.",
                    returncode=1,
                ),
                CommandResult(success=True, stdout="issue-733\n", stderr="", returncode=0),
                CommandResult(success=True, stdout="", stderr="", returncode=0),
            ]

            result = git_push()

            assert result.success is True
            assert mock_cmd.run_command.call_count == 6
            final_call_args = mock_cmd.run_command.call_args_list[5][0][0]
            assert final_call_args == [
                "git",
                "push",
                "--set-upstream",
                "origin",
                "issue-733",
            ]


@pytest.mark.usefixtures("_use_custom_subprocess_mock")
class TestSaveCommitFailureHistory:
    """Tests for save_commit_failure_history function."""

    def test_save_commit_failure_history_with_repo_name(self, tmp_path):
        """Test saving commit failure history with repo name."""
        with patch("pathlib.Path.home") as mock_home:
            mock_home.return_value = tmp_path

            error_message = "Test error message"
            context = {"type": "test", "issue_number": 123}
            repo_name = "owner/repo"

            with pytest.raises(SystemExit) as exc_info:
                save_commit_failure_history(error_message, context, repo_name)

            assert exc_info.value.code == 1

            history_dir = tmp_path / ".auto-coder" / "owner_repo"
            assert history_dir.exists()

            history_files = list(history_dir.glob("commit_failure_*.json"))
            assert len(history_files) == 1

            with open(history_files[0], "r") as f:
                data = json.load(f)

            assert data["error_message"] == error_message
            assert data["context"] == context
            assert "timestamp" in data

    def test_save_commit_failure_history_without_repo_name(self, tmp_path):
        """Test saving commit failure history without repo name."""
        original_cwd = os.getcwd()
        try:
            os.chdir(tmp_path)

            error_message = "Test error message"
            context = {"type": "test", "pr_number": 456}

            with pytest.raises(SystemExit) as exc_info:
                save_commit_failure_history(error_message, context, None)

            assert exc_info.value.code == 1

            history_dir = tmp_path / ".auto-coder"
            assert history_dir.exists()

            history_files = list(history_dir.glob("commit_failure_*.json"))
            assert len(history_files) == 1

            with open(history_files[0], "r") as f:
                data = json.load(f)

            assert data["error_message"] == error_message
            assert data["context"] == context
            assert "timestamp" in data
        finally:
            os.chdir(original_cwd)
