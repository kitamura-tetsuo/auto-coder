"""
Tests for git push utilities.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.git_utils import check_unpushed_commits, ensure_pushed, git_push
from src.auto_coder.utils import CommandResult


class TestGitPushUtils:
    """Test git push utility functions."""

    @patch("src.auto_coder.git_info.CommandExecutor")
    def test_check_unpushed_commits_with_unpushed(self, mock_executor_class):
        """Test check_unpushed_commits when there are unpushed commits."""
        mock_executor = MagicMock()
        mock_executor_class.return_value = mock_executor

        # Mock getting current branch
        mock_executor.run_command.side_effect = [
            CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
            CommandResult(success=True, stdout="3\n", stderr="", returncode=0),
        ]

        result = check_unpushed_commits()

        assert result is True
        assert mock_executor.run_command.call_count == 2

    @patch("src.auto_coder.git_info.CommandExecutor")
    def test_check_unpushed_commits_without_unpushed(self, mock_executor_class):
        """Test check_unpushed_commits when there are no unpushed commits."""
        mock_executor = MagicMock()
        mock_executor_class.return_value = mock_executor

        # Mock getting current branch
        mock_executor.run_command.side_effect = [
            CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
            CommandResult(success=True, stdout="0\n", stderr="", returncode=0),
        ]

        result = check_unpushed_commits()

        assert result is False
        assert mock_executor.run_command.call_count == 2

    @patch("src.auto_coder.git_info.CommandExecutor")
    def test_check_unpushed_commits_branch_not_exist(self, mock_executor_class):
        """Test check_unpushed_commits when remote branch doesn't exist."""
        mock_executor = MagicMock()
        mock_executor_class.return_value = mock_executor

        # Mock getting current branch
        mock_executor.run_command.side_effect = [
            CommandResult(success=True, stdout="feature-branch\n", stderr="", returncode=0),
            CommandResult(success=False, stdout="", stderr="unknown revision", returncode=128),
        ]

        result = check_unpushed_commits()

        assert result is False
        assert mock_executor.run_command.call_count == 2

    @patch("src.auto_coder.git_info.CommandExecutor")
    @patch("src.auto_coder.git_commit.CommandExecutor")
    def test_git_push_success(self, mock_commit_executor_class, mock_info_executor_class):
        """Test git_push when push succeeds."""
        mock_executor = MagicMock()
        mock_commit_executor_class.return_value = mock_executor
        mock_info_executor_class.return_value = mock_executor

        mock_executor.run_command.side_effect = [
            # 1) check_unpushed_commits: get current branch
            CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
            # 2) check_unpushed_commits: found unpushed commits
            CommandResult(success=True, stdout="2\n", stderr="", returncode=0),
            # 3) _perform_git_push: get current branch for push
            CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
            # 4) _perform_git_push: push succeeds
            CommandResult(success=True, stdout="Everything up-to-date\n", stderr="", returncode=0),
        ]

        result = git_push()

        assert result.success is True
        assert mock_executor.run_command.call_count == 4

    @patch("src.auto_coder.git_info.CommandExecutor")
    @patch("src.auto_coder.git_commit.CommandExecutor")
    def test_git_push_failure(self, mock_commit_executor_class, mock_info_executor_class):
        """Test git_push when push fails."""
        mock_executor = MagicMock()
        mock_commit_executor_class.return_value = mock_executor
        mock_info_executor_class.return_value = mock_executor

        mock_executor.run_command.side_effect = [
            # 1) check_unpushed_commits: get current branch
            CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
            # 2) check_unpushed_commits: found unpushed commits
            CommandResult(success=True, stdout="2\n", stderr="", returncode=0),
            # 3) _perform_git_push: get current branch for push
            CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
            # 4) _perform_git_push: push fails
            CommandResult(success=False, stdout="", stderr="error: failed to push", returncode=1),
        ]

        result = git_push()

        assert result.success is False
        assert "failed to push" in result.stderr
        assert mock_executor.run_command.call_count == 4

    @patch("src.auto_coder.git_commit.check_unpushed_commits")
    @patch("src.auto_coder.git_commit.git_push")
    def test_ensure_pushed_with_unpushed_commits(self, mock_git_push, mock_check_unpushed):
        """Test ensure_pushed when there are unpushed commits."""
        mock_check_unpushed.return_value = True
        mock_git_push.return_value = CommandResult(success=True, stdout="Pushed successfully\n", stderr="", returncode=0)

        result = ensure_pushed()

        assert result.success is True
        mock_check_unpushed.assert_called_once()
        mock_git_push.assert_called_once()

    @patch("src.auto_coder.git_commit.check_unpushed_commits")
    @patch("src.auto_coder.git_commit.git_push")
    def test_ensure_pushed_without_unpushed_commits(self, mock_git_push, mock_check_unpushed):
        """Test ensure_pushed when there are no unpushed commits."""
        mock_check_unpushed.return_value = False

        result = ensure_pushed()

        assert result.success is True
        assert "No unpushed commits" in result.stdout
        mock_check_unpushed.assert_called_once()
        mock_git_push.assert_not_called()

    @patch("src.auto_coder.git_commit.check_unpushed_commits")
    @patch("src.auto_coder.git_commit.git_push")
    def test_ensure_pushed_push_failure(self, mock_git_push, mock_check_unpushed):
        """Test ensure_pushed when push fails."""
        mock_check_unpushed.return_value = True
        mock_git_push.return_value = CommandResult(success=False, stdout="", stderr="error: failed to push", returncode=1)

        result = ensure_pushed()

        assert result.success is False
        assert "failed to push" in result.stderr
        mock_check_unpushed.assert_called_once()
        mock_git_push.assert_called_once()
