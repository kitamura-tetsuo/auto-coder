"""
Tests for git push utilities.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.git_commit import ensure_pushed, git_push
from src.auto_coder.git_info import check_unpushed_commits
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

    @pytest.mark.usefixtures("_use_custom_subprocess_mock")
    @patch("src.auto_coder.git_commit.CommandExecutor")
    @patch("src.auto_coder.git_info.CommandExecutor")
    def test_git_push_success(self, mock_executor_info_class, mock_executor_commit_class):
        """Test git_push when push succeeds."""
        mock_executor = MagicMock()
        mock_executor_info_class.return_value = mock_executor
        mock_executor_commit_class.return_value = mock_executor

        # Mock responses for check_unpushed_commits (called from git_info module)
        mock_executor.run_command.side_effect = [
            # check_unpushed_commits calls
            CommandResult(success=True, stdout="main\n", stderr="", returncode=0),  # get_current_branch
            CommandResult(success=True, stdout="2\n", stderr="", returncode=0),  # rev-list with unpushed commits
            # _perform_git_push calls
            CommandResult(success=True, stdout="main\n", stderr="", returncode=0),  # get current branch for push
            CommandResult(success=True, stdout="Everything up-to-date\n", stderr="", returncode=0),  # push succeeds
        ]

        result = git_push()

        assert result.success is True
        assert mock_executor.run_command.call_count == 4

    @pytest.mark.usefixtures("_use_custom_subprocess_mock")
    @patch("src.auto_coder.git_commit.CommandExecutor")
    @patch("src.auto_coder.git_info.CommandExecutor")
    def test_git_push_failure(self, mock_executor_info_class, mock_executor_commit_class):
        """Test git_push when push fails."""
        mock_executor = MagicMock()
        mock_executor_info_class.return_value = mock_executor
        mock_executor_commit_class.return_value = mock_executor

        # Mock responses for check_unpushed_commits (called from git_info module)
        mock_executor.run_command.side_effect = [
            # check_unpushed_commits calls
            CommandResult(success=True, stdout="main\n", stderr="", returncode=0),  # get_current_branch
            CommandResult(success=True, stdout="2\n", stderr="", returncode=0),  # rev-list with unpushed commits
            # _perform_git_push calls
            CommandResult(success=True, stdout="main\n", stderr="", returncode=0),  # get current branch for push
            CommandResult(success=False, stdout="", stderr="error: failed to push", returncode=1),  # push fails
        ]

        result = git_push()

        assert result.success is False
        assert "failed to push" in result.stderr
        assert mock_executor.run_command.call_count == 4

    @pytest.mark.usefixtures("_use_custom_subprocess_mock")
    @patch("src.auto_coder.git_commit.CommandExecutor")
    @patch("src.auto_coder.git_info.CommandExecutor")
    def test_ensure_pushed_with_unpushed_commits(self, mock_executor_info_class, mock_executor_commit_class):
        """Test ensure_pushed when there are unpushed commits."""
        mock_executor = MagicMock()
        mock_executor_info_class.return_value = mock_executor
        mock_executor_commit_class.return_value = mock_executor

        # Mock responses for check_unpushed_commits (called from git_info module)
        # ensure_pushed calls check_unpushed_commits (2 commands)
        # then calls git_push which calls _perform_git_push which:
        #   1. calls check_unpushed_commits again (2 commands)
        #   2. calls rev-parse to get branch (1 command)
        #   3. calls git push (1 command)
        mock_executor.run_command.side_effect = [
            # First check_unpushed_commits call from ensure_pushed
            CommandResult(success=True, stdout="main\n", stderr="", returncode=0),  # get_current_branch
            CommandResult(success=True, stdout="2\n", stderr="", returncode=0),  # rev-list with unpushed commits
            # Second check_unpushed_commits call from git_push's _perform_git_push
            CommandResult(success=True, stdout="main\n", stderr="", returncode=0),  # get current branch
            CommandResult(success=True, stdout="2\n", stderr="", returncode=0),  # rev-list (still has unpushed commits)
            # _perform_git_push gets current branch when branch is None
            CommandResult(success=True, stdout="main\n", stderr="", returncode=0),  # rev-parse for branch name
            # git push command succeeds
            CommandResult(success=True, stdout="Everything up-to-date\n", stderr="", returncode=0),
        ]

        result = ensure_pushed()

        assert result.success is True
        assert mock_executor.run_command.call_count == 6

    @pytest.mark.usefixtures("_use_custom_subprocess_mock")
    @patch("src.auto_coder.git_commit.CommandExecutor")
    @patch("src.auto_coder.git_info.CommandExecutor")
    def test_ensure_pushed_without_unpushed_commits(self, mock_executor_info_class, mock_executor_commit_class):
        """Test ensure_pushed when there are no unpushed commits."""
        mock_executor = MagicMock()
        mock_executor_info_class.return_value = mock_executor
        mock_executor_commit_class.return_value = mock_executor

        # Mock responses for check_unpushed_commits (called from git_info module)
        mock_executor.run_command.side_effect = [
            # check_unpushed_commits calls
            CommandResult(success=True, stdout="main\n", stderr="", returncode=0),  # get_current_branch
            CommandResult(success=True, stdout="0\n", stderr="", returncode=0),  # rev-list with no unpushed commits
        ]

        result = ensure_pushed()

        assert result.success is True
        assert "No unpushed commits" in result.stdout
        assert mock_executor.run_command.call_count == 2

    @pytest.mark.usefixtures("_use_custom_subprocess_mock")
    @patch("src.auto_coder.git_commit.CommandExecutor")
    @patch("src.auto_coder.git_info.CommandExecutor")
    def test_ensure_pushed_push_failure(self, mock_executor_info_class, mock_executor_commit_class):
        """Test ensure_pushed when push fails."""
        mock_executor = MagicMock()
        mock_executor_info_class.return_value = mock_executor
        mock_executor_commit_class.return_value = mock_executor

        # Mock responses for check_unpushed_commits (called from git_info module)
        mock_executor.run_command.side_effect = [
            # First check_unpushed_commits call from ensure_pushed
            CommandResult(success=True, stdout="main\n", stderr="", returncode=0),  # get_current_branch
            CommandResult(success=True, stdout="2\n", stderr="", returncode=0),  # rev-list with unpushed commits
            # Second check_unpushed_commits call from git_push's _perform_git_push
            CommandResult(success=True, stdout="main\n", stderr="", returncode=0),  # get current branch
            CommandResult(success=True, stdout="2\n", stderr="", returncode=0),  # rev-list (still has unpushed commits)
            # git push command fails
            CommandResult(success=False, stdout="", stderr="error: failed to push some refs", returncode=1),
        ]

        result = ensure_pushed()

        assert result.success is False
        assert "failed to push" in result.stderr
        assert mock_executor.run_command.call_count == 5
