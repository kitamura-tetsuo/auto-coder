"""
Test for ensure_pushed_with_fallback function to test non-fast-forward error handling.
"""

import pytest
from unittest.mock import MagicMock, patch

from src.auto_coder.git_utils import ensure_pushed_with_fallback, check_unpushed_commits
from src.auto_coder.utils import CommandResult


class TestEnsurePushedWithFallback:
    """Test ensure_pushed_with_fallback function."""

    @patch("src.auto_coder.git_utils.check_unpushed_commits")
    @patch("src.auto_coder.git_utils.git_push")
    def test_non_fast_forward_error_handling_success(self, mock_git_push, mock_check_unpushed):
        """Test that non-fast-forward error is handled by pulling and retrying push successfully."""
        mock_check_unpushed.return_value = True
        
        # First push fails with non-fast-forward error
        # Second push (after pull) succeeds
        mock_git_push.side_effect = [
            CommandResult(
                success=False, 
                stdout="", 
                stderr="To github.com:kitamura-tetsuo/outliner.git\n ! [rejected] issue-752 -> issue-752 (non-fast-forward)",
                returncode=1
            ),
            CommandResult(
                success=True, 
                stdout="Everything up-to-date", 
                stderr="", 
                returncode=0
            )
        ]

        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor_class:
            mock_executor = MagicMock()
            mock_executor_class.return_value = mock_executor
            
            # Mock branch detection and successful pull
            mock_executor.run_command.side_effect = [
                CommandResult(success=True, stdout="issue-752\n", stderr="", returncode=0),  # git rev-parse
                CommandResult(success=True, stdout="Updating abc123..def456", stderr="", returncode=0)  # git pull
            ]

            result = ensure_pushed_with_fallback()

        # Assert
        assert result.success is True
        assert mock_executor.run_command.call_count == 2  # branch detection + pull call
        mock_executor.run_command.assert_any_call(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=None)
        mock_executor.run_command.assert_any_call(["git", "pull", "origin", "issue-752"], cwd=None)
        assert mock_git_push.call_count == 2  # Initial push + retry push

    @patch("src.auto_coder.git_utils.check_unpushed_commits")
    @patch("src.auto_coder.git_utils.git_push")
    @patch("src.auto_coder.git_utils.resolve_pull_conflicts")
    def test_non_fast_forward_error_with_conflicts(self, mock_resolve_conflicts, mock_git_push, mock_check_unpushed):
        """Test that non-fast-forward error with merge conflicts is handled properly."""
        mock_check_unpushed.return_value = True
        
        # First push fails with non-fast-forward error
        # Retry push still fails
        mock_git_push.side_effect = [
            CommandResult(
                success=False,
                stdout="",
                stderr="non-fast-forward",
                returncode=1
            ),
            CommandResult(
                success=False,
                stdout="",
                stderr="Push failed",
                returncode=1
            )
        ]

        # Conflict resolution fails
        mock_resolve_conflicts.return_value = CommandResult(
            success=False,
            stdout="",
            stderr="Failed to resolve conflicts",
            returncode=1
        )

        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor_class:
            mock_executor = MagicMock()
            mock_executor_class.return_value = mock_executor
            
            # Mock branch detection and pull with conflicts
            mock_executor.run_command.side_effect = [
                CommandResult(success=True, stdout="issue-752\n", stderr="", returncode=0),  # git rev-parse
                CommandResult(success=False, stdout="", stderr="Automatic merge failed; fix conflicts and then commit the result.", returncode=1)  # git pull
            ]

            result = ensure_pushed_with_fallback()

        # Assert
        assert result.success is False
        assert mock_git_push.call_count == 2  # Initial push + retry push
        mock_resolve_conflicts.assert_called_once_with(cwd=None, merge_method="merge")

    @patch("src.auto_coder.git_utils.check_unpushed_commits")
    @patch("src.auto_coder.git_utils.git_push")
    def test_non_fast_forward_error_pull_fails(self, mock_git_push, mock_check_unpushed):
        """Test that when pull fails during non-fast-forward error handling, original push failure is returned."""
        mock_check_unpushed.return_value = True
        
        # First push fails with non-fast-forward error
        mock_git_push.return_value = CommandResult(
            success=False, 
            stdout="", 
            stderr="non-fast-forward",
            returncode=1
        )

        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor_class:
            mock_executor = MagicMock()
            mock_executor_class.return_value = mock_executor
            
            # Mock branch detection and pull failure
            mock_executor.run_command.side_effect = [
                CommandResult(success=True, stdout="issue-752\n", stderr="", returncode=0),  # git rev-parse
                CommandResult(success=False, stdout="", stderr="error: could not read from remote repository", returncode=1)  # git pull
            ]

            result = ensure_pushed_with_fallback()

        # Assert
        assert result.success is False
        assert "non-fast-forward" in result.stderr
        assert mock_git_push.call_count == 2  # Initial push + retry push
        assert mock_executor.run_command.call_count == 2  # branch detection + pull

    @patch("src.auto_coder.git_utils.check_unpushed_commits")
    def test_no_unpushed_commits(self, mock_check_unpushed):
        """Test when there are no unpushed commits."""
        mock_check_unpushed.return_value = False

        result = ensure_pushed_with_fallback()

        assert result.success is True
        assert "No unpushed commits" in result.stdout

    @patch("src.auto_coder.git_utils.check_unpushed_commits")
    @patch("src.auto_coder.git_utils.git_push")
    def test_regular_push_failure(self, mock_git_push, mock_check_unpushed):
        """Test that non non-fast-forward failures are not handled with pull."""
        mock_check_unpushed.return_value = True
        
        # Push fails with non-pull-related error
        mock_git_push.return_value = CommandResult(
            success=False,
            stdout="",
            stderr="error: could not read from remote repository",
            returncode=1
        )

        result = ensure_pushed_with_fallback()

        # Assert no pull was attempted
        assert result.success is False
        assert mock_git_push.call_count == 1  # Only initial push

    @patch("src.auto_coder.git_utils.check_unpushed_commits")
    @patch("src.auto_coder.git_utils.git_push")
    @patch("src.auto_coder.git_utils.resolve_pull_conflicts")
    def test_non_fast_forward_error_with_successful_conflict_resolution(self, mock_resolve_conflicts, mock_git_push, mock_check_unpushed):
        """Test that non-fast-forward error with successful conflict resolution works."""
        mock_check_unpushed.return_value = True
        
        # First push fails with non-fast-forward error
        # Retry push succeeds after conflict resolution
        mock_git_push.side_effect = [
            CommandResult(
                success=False,
                stdout="",
                stderr="non-fast-forward",
                returncode=1
            ),
            CommandResult(
                success=True,
                stdout="Everything up-to-date",
                stderr="",
                returncode=0
            )
        ]

        # Conflict resolution succeeds
        mock_resolve_conflicts.return_value = CommandResult(
            success=True,
            stdout="Conflicts resolved",
            stderr="",
            returncode=0
        )

        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor_class:
            mock_executor = MagicMock()
            mock_executor_class.return_value = mock_executor
            
            # Mock branch detection and pull with conflicts
            mock_executor.run_command.side_effect = [
                CommandResult(success=True, stdout="issue-752\n", stderr="", returncode=0),  # git rev-parse
                CommandResult(success=False, stdout="", stderr="Automatic merge failed; fix conflicts and then commit the result.", returncode=1)  # git pull
            ]

            result = ensure_pushed_with_fallback()

        # Assert
        assert result.success is True
        assert mock_git_push.call_count == 2  # Initial push + retry push
        mock_resolve_conflicts.assert_called_once_with(cwd=None, merge_method="merge")