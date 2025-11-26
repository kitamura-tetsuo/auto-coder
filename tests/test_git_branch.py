"Tests for git_branch module."

from unittest.mock import MagicMock, Mock, patch

import pytest

from src.auto_coder.git_branch import (
    branch_context,
    branch_exists,
    extract_attempt_from_branch,
    extract_number_from_branch,
    get_all_branches,
    get_branches_by_pattern,
    git_checkout_branch,
    git_commit_with_retry,
    migrate_pr_branches,
    validate_branch_name,
)
from src.auto_coder.utils import CommandResult


@pytest.mark.usefixtures("_use_custom_subprocess_mock")
class TestGitCommitWithRetry:
    """Tests for git_commit_with_retry function."""

    def test_successful_commit(self):
        """Test successful commit without retry."""
        with patch("src.auto_coder.git_branch.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(success=True, stdout="", stderr="", returncode=0)

            result = git_commit_with_retry("Test commit message")

            assert result.success is True
            mock_cmd.run_command.assert_called_once()
            call_args = mock_cmd.run_command.call_args
            assert call_args[0][0] == ["git", "commit", "-m", "Test commit message"]

    def test_commit_with_dprint_error_and_retry(self):
        """Test commit with dprint formatting error triggers retry."""
        with patch("src.auto_coder.git_branch.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                CommandResult(
                    success=False,
                    stdout="",
                    stderr="Formatting issues detected. Run 'npx dprint fmt' to fix.",
                    returncode=1,
                ),
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # dprint fmt
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git add
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # commit retry
            ]

            result = git_commit_with_retry("Test commit message")

            assert result.success is True
            assert mock_cmd.run_command.call_count == 4
            calls = mock_cmd.run_command.call_args_list
            assert calls[1][0][0] == ["npx", "dprint", "fmt"]
            assert calls[2][0][0] == ["git", "add", "-u"]
            assert calls[3][0][0] == ["git", "commit", "-m", "Test commit message"]

    def test_commit_with_dprint_error_fmt_fails(self):
        """Test commit with dprint error but formatter fails."""
        with patch("src.auto_coder.git_branch.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                CommandResult(
                    success=False,
                    stdout="",
                    stderr="Formatting issues detected. Run 'npx dprint fmt' to fix.",
                    returncode=1,
                ),
                CommandResult(
                    success=False,
                    stdout="",
                    stderr="dprint command not found",
                    returncode=1,
                ),
                CommandResult(
                    success=False,
                    stdout="",
                    stderr="Formatting issues detected. Run 'npx dprint fmt' to fix.",
                    returncode=1,
                ),
            ]

            result = git_commit_with_retry("Test commit message")

            assert result.success is False
            assert "Formatting issues detected" in result.stderr


@pytest.mark.usefixtures("_use_custom_subprocess_mock")
class TestGitCheckoutBranch:
    """Tests for git_checkout_branch function."""

    def test_successful_checkout_existing_branch(self):
        """Test successful checkout of an existing branch."""
        with patch("src.auto_coder.git_branch.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                CommandResult(
                    success=True,
                    stdout="Switched to branch 'feature'\n",
                    stderr="",
                    returncode=0,
                ),
                CommandResult(success=True, stdout="feature\n", stderr="", returncode=0),
            ]

            result = git_checkout_branch("feature")

            assert result.success is True
            assert mock_cmd.run_command.call_count == 3
            assert mock_cmd.run_command.call_args_list[0][0][0] == [
                "git",
                "status",
                "--porcelain",
            ]
            assert mock_cmd.run_command.call_args_list[1][0][0] == [
                "git",
                "checkout",
                "feature",
            ]
            assert mock_cmd.run_command.call_args_list[2][0][0] == [
                "git",
                "rev-parse",
                "--abbrev-ref",
                "HEAD",
            ]


class TestExtractAttemptFromBranch:
    """Tests for extract_attempt_from_branch function."""

    def test_extract_attempt_from_branch_with_attempt(self):
        """Test extracting attempt number from branch with attempt suffix."""
        assert extract_attempt_from_branch("issue-123_attempt-1") == 1
        assert extract_attempt_from_branch("issue-456_attempt-2") == 2
        assert extract_attempt_from_branch("issue-789_attempt-10") == 10
        assert extract_attempt_from_branch("feature/issue-123_attempt-3") == 3

    def test_extract_attempt_from_branch_without_attempt(self):
        """Test extracting attempt number from branch without attempt suffix."""
        assert extract_attempt_from_branch("issue-123") is None
        assert extract_attempt_from_branch("main") is None
        assert extract_attempt_from_branch("feature-branch") is None
        assert extract_attempt_from_branch("pr-456") is None

    def test_extract_attempt_from_branch_empty_or_none(self):
        """Test extracting attempt number from empty or None branch name."""
        assert extract_attempt_from_branch("") is None
        assert extract_attempt_from_branch(None) is None  # type: ignore

    def test_extract_attempt_from_branch_case_insensitive(self):
        """Test that attempt extraction is case-insensitive."""
        assert extract_attempt_from_branch("issue-123_Attempt-1") == 1
        assert extract_attempt_from_branch("issue-456_ATTEMPT-2") == 2

    def test_extract_attempt_from_branch_legacy_slash_format(self):
        """Test extracting attempt number from legacy slash format."""
        assert extract_attempt_from_branch("issue-123/attempt-1") == 1
        assert extract_attempt_from_branch("issue-456/attempt-2") == 2
        assert extract_attempt_from_branch("issue-789/attempt-10") == 10
        assert extract_attempt_from_branch("feature/issue-123/attempt-3") == 3

    def test_extract_attempt_from_branch_legacy_slash_case_insensitive(self):
        """Test that legacy slash format attempt extraction is case-insensitive."""
        assert extract_attempt_from_branch("issue-123/Attempt-1") == 1
        assert extract_attempt_from_branch("issue-456/ATTEMPT-2") == 2
