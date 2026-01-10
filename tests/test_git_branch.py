"Tests for git_branch module."

from unittest.mock import MagicMock, Mock, patch

import pytest

from src.auto_coder.git_branch import (
    branch_context,
    branch_exists,
    detect_branch_name_conflict,
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
            assert mock_cmd.run_command.call_count == 2
            call_args = mock_cmd.run_command.call_args
            assert call_args[0][0] == ["git", "commit", "-m", "Test commit message"]

    def test_commit_with_dprint_error_and_retry(self):
        """Test commit with dprint formatting error triggers retry."""
        with patch("src.auto_coder.git_branch.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                CommandResult(success=True, stdout="", stderr="", returncode=0),
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
            assert mock_cmd.run_command.call_count == 5
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
                CommandResult(success=True, stdout="", stderr="", returncode=0),
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
            assert "dprint command not found" in result.stderr


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


class TestBackwardCompatibility:
    """Test backward compatibility with old slash format."""

    def test_extract_attempt_legacy_format(self):
        """Legacy slash format should still work."""
        assert extract_attempt_from_branch("issue-100/attempt-5") == 5
        assert extract_attempt_from_branch("issue-200/attempt-1") == 1

    def test_extract_attempt_new_format_preferred(self):
        """New underscore format should be standard."""
        assert extract_attempt_from_branch("issue-100_attempt-5") == 5
        assert extract_attempt_from_branch("issue-200_attempt-1") == 1


@pytest.mark.usefixtures("_use_custom_subprocess_mock")
class TestDetectBranchNameConflict:
    """Tests for detect_branch_name_conflict function."""

    def test_detect_parent_branch_conflict(self):
        """Test detection of conflict when parent branch exists."""
        with patch("src.auto_coder.git_branch.branch_exists") as mock_exists, patch("src.auto_coder.git_branch.get_branches_by_pattern") as mock_pattern:
            # Parent branch 'issue-699' exists
            mock_exists.return_value = True
            mock_pattern.return_value = []

            # Attempting to create 'issue-699/attempt-1' should detect conflict
            result = detect_branch_name_conflict("issue-699/attempt-1")

            assert result == "issue-699"
            mock_exists.assert_called_once_with("issue-699", None)

    def test_detect_child_branch_conflict(self):
        """Test detection of conflict when child branches exist."""
        with patch("src.auto_coder.git_branch.branch_exists") as mock_exists, patch("src.auto_coder.git_branch.get_branches_by_pattern") as mock_pattern:
            mock_exists.return_value = False
            # Child branches 'issue-699/attempt-1' and 'issue-699/attempt-2' exist
            mock_pattern.return_value = ["issue-699/attempt-1", "issue-699/attempt-2"]

            # Attempting to create 'issue-699' should detect conflict
            result = detect_branch_name_conflict("issue-699")

            assert result == "issue-699/attempt-1"
            mock_pattern.assert_called_once_with("issue-699/*", cwd=None, remote=False)

    def test_detect_no_conflict(self):
        """Test that no conflict is detected when branches don't exist."""
        with patch("src.auto_coder.git_branch.branch_exists") as mock_exists, patch("src.auto_coder.git_branch.get_branches_by_pattern") as mock_pattern:
            mock_exists.return_value = False
            mock_pattern.return_value = []

            # No conflict should be detected
            result = detect_branch_name_conflict("issue-123")

            assert result is None
            mock_exists.assert_not_called()
            mock_pattern.assert_called_once_with("issue-123/*", cwd=None, remote=False)

    def test_detect_no_conflict_with_slash_in_name(self):
        """Test that no conflict is detected when parent doesn't exist."""
        with patch("src.auto_coder.git_branch.branch_exists") as mock_exists, patch("src.auto_coder.git_branch.get_branches_by_pattern") as mock_pattern:
            # Parent branch 'issue-699' does not exist
            mock_exists.return_value = False
            mock_pattern.return_value = []

            # No conflict should be detected
            result = detect_branch_name_conflict("issue-699/attempt-1")

            assert result is None
            mock_exists.assert_called_once_with("issue-699", None)
            # Also check for child branches at the same level
            mock_pattern.assert_called_once_with("issue-699/attempt-1/*", cwd=None, remote=False)

    def test_detect_child_branch_conflict_multiple_levels(self):
        """Test detection of conflict with multiple levels of branch hierarchy."""
        with patch("src.auto_coder.git_branch.branch_exists") as mock_exists, patch("src.auto_coder.git_branch.get_branches_by_pattern") as mock_pattern:
            mock_exists.return_value = False
            # Child branches exist
            mock_pattern.return_value = ["feature/issue-123/attempt-1"]

            # Attempting to create 'feature/issue-123' should detect conflict
            result = detect_branch_name_conflict("feature/issue-123")

            assert result == "feature/issue-123/attempt-1"
            mock_pattern.assert_called_once_with("feature/issue-123/*", cwd=None, remote=False)


@pytest.mark.usefixtures("_use_custom_subprocess_mock")
class TestGitCheckoutBranchWithConflictDetection:
    """Tests for git_checkout_branch with conflict detection."""

    def test_checkout_branch_with_parent_conflict_fails(self):
        """Test that creating a branch with parent conflict fails gracefully."""
        with patch("src.auto_coder.git_branch.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            # Simulate that parent branch exists
            mock_cmd.run_command.side_effect = [
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git status
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git branch --list (doesn't exist yet)
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git fetch
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git branch --list (still doesn't exist)
                CommandResult(success=False, stdout="", stderr="fatal: couldn't find remote ref", returncode=1),  # git ls-remote (no remote)
            ]

            with patch("src.auto_coder.git_branch.branch_exists") as mock_exists, patch("src.auto_coder.git_branch.get_branches_by_pattern") as mock_pattern:
                # Parent branch exists
                mock_exists.return_value = True
                mock_pattern.return_value = []

                result = git_checkout_branch("issue-699/attempt-1", create_new=True, base_branch="main")

                assert result.success is False
                assert "conflict" in result.stderr.lower()
                assert "issue-699" in result.stderr

    def test_checkout_branch_with_child_conflict_fails(self):
        """Test that creating a branch with child branches conflict fails gracefully."""
        with patch("src.auto_coder.git_branch.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            # Simulate that child branches exist
            mock_cmd.run_command.side_effect = [
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git status
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git branch --list (doesn't exist yet)
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git fetch
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git branch --list (still doesn't exist)
                CommandResult(success=False, stdout="", stderr="fatal: couldn't find remote ref", returncode=1),  # git ls-remote (no remote)
            ]

            with patch("src.auto_coder.git_branch.branch_exists") as mock_exists, patch("src.auto_coder.git_branch.get_branches_by_pattern") as mock_pattern:
                mock_exists.return_value = False
                # Child branches exist
                mock_pattern.return_value = ["issue-123/attempt-1"]

                result = git_checkout_branch("issue-123", create_new=True, base_branch="main")

                assert result.success is False
                assert "conflict" in result.stderr.lower()
                assert "issue-123/attempt-1" in result.stderr

    def test_checkout_branch_without_conflict_succeeds(self):
        """Test that creating a branch without conflicts succeeds."""
        with patch("src.auto_coder.git_branch.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git status
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git branch --list (doesn't exist)
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git fetch
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git branch --list (after fetch)
                CommandResult(success=False, stdout="", stderr="fatal: couldn't find remote ref", returncode=1),  # git ls-remote (no remote)
                CommandResult(success=False, stdout="", stderr="fatal: ref not found", returncode=1),  # git rev-parse refs/remotes/origin/main (not found)
                CommandResult(success=True, stdout="abc123\n", stderr="", returncode=0),  # git rev-parse main (found)
                CommandResult(success=True, stdout="Switched to branch 'issue-123'\n", stderr="", returncode=0),  # git checkout -B
                CommandResult(success=True, stdout="issue-123\n", stderr="", returncode=0),  # git rev-parse (verify)
                CommandResult(success=True, stdout="Branch 'issue-123' set up to track remote branch", stderr="", returncode=0),  # git push -u origin issue-123
            ]

            with patch("src.auto_coder.git_branch.branch_exists") as mock_exists, patch("src.auto_coder.git_branch.get_branches_by_pattern") as mock_pattern:
                # No conflicts
                mock_exists.return_value = False
                mock_pattern.return_value = []

                result = git_checkout_branch("issue-123", create_new=True, base_branch="main")

                assert result.success is True


class TestBranchExistence:
    """Test branch existence checking."""

    @pytest.mark.usefixtures("_use_custom_subprocess_mock")
    def test_switch_to_existing_local_branch(self):
        """Test switching to a branch that exists locally."""
        with patch("src.auto_coder.git_branch.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git status
                CommandResult(
                    success=True,
                    stdout="Switched to branch 'feature'\n",
                    stderr="",
                    returncode=0,
                ),  # git checkout
                CommandResult(success=True, stdout="feature\n", stderr="", returncode=0),  # git rev-parse
            ]

            result = git_checkout_branch("feature", create_new=False)

            assert result.success is True
            assert mock_cmd.run_command.call_count == 3
            assert mock_cmd.run_command.call_args_list[1][0][0] == [
                "git",
                "checkout",
                "feature",
            ]

    @pytest.mark.usefixtures("_use_custom_subprocess_mock")
    def test_switch_to_existing_remote_branch(self):
        """Test switching to a branch that exists only on remote."""
        with patch("src.auto_coder.git_branch.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            # For create_new=False, it doesn't fetch, so remote branch checkout needs create_new=True
            mock_cmd.run_command.side_effect = [
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git status
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git branch --list (doesn't exist locally)
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git fetch
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git branch --list (still doesn't exist locally)
                CommandResult(success=True, stdout="abc123 refs/heads/feature\n", stderr="", returncode=0),  # git ls-remote (exists remotely)
                CommandResult(
                    success=True,
                    stdout="Switched to branch 'feature'\n",
                    stderr="",
                    returncode=0,
                ),  # git checkout -b origin/feature
                CommandResult(success=True, stdout="feature", stderr="", returncode=0),  # git rev-parse (verify current branch)
            ]

            # Use create_new=True to enable remote branch tracking logic
            result = git_checkout_branch("feature", create_new=True, base_branch="main")

            assert result.success is True
            # Should have created tracking branch from origin/feature
            calls = mock_cmd.run_command.call_args_list
            assert any("checkout" in str(call) and ("-b" in str(call) or "-B" in str(call)) for call in calls)

    @pytest.mark.usefixtures("_use_custom_subprocess_mock")
    def test_switch_to_nonexistent_branch_with_create_new(self):
        """Test switching to a non-existent branch with create_new=True."""
        with patch("src.auto_coder.git_branch.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git status
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git branch --list (doesn't exist)
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git fetch
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git branch --list (after fetch)
                CommandResult(success=False, stdout="", stderr="fatal: couldn't find remote ref", returncode=1),  # git ls-remote
                CommandResult(success=False, stdout="", stderr="fatal: ref not found", returncode=1),  # git rev-parse refs/remotes/origin/main
                CommandResult(success=True, stdout="abc123\n", stderr="", returncode=0),  # git rev-parse main
                CommandResult(
                    success=True,
                    stdout="Switched to branch 'issue-456'\n",
                    stderr="",
                    returncode=0,
                ),  # git checkout -B
                CommandResult(success=True, stdout="issue-456\n", stderr="", returncode=0),  # git rev-parse
                CommandResult(
                    success=True,
                    stdout="Branch 'issue-456' set up to track remote branch",
                    stderr="",
                    returncode=0,
                ),  # git push -u
            ]

            with patch("src.auto_coder.git_branch.branch_exists") as mock_exists, patch("src.auto_coder.git_branch.get_branches_by_pattern") as mock_pattern:
                mock_exists.return_value = False
                mock_pattern.return_value = []

                result = git_checkout_branch("issue-456", create_new=True, base_branch="main")

                assert result.success is True

    @pytest.mark.usefixtures("_use_custom_subprocess_mock")
    def test_switch_to_existing_branch_with_create_new(self):
        """Test switching to an existing branch with create_new=True should not fail."""
        with patch("src.auto_coder.git_branch.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git status
                CommandResult(success=True, stdout="feature\n", stderr="", returncode=0),  # git branch --list (exists)
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git fetch
                CommandResult(success=True, stdout="feature\n", stderr="", returncode=0),  # git branch --list (after fetch)
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git ls-remote
                CommandResult(
                    success=True,
                    stdout="Switched to branch 'feature'\n",
                    stderr="",
                    returncode=0,
                ),  # git checkout
                CommandResult(success=True, stdout="feature", stderr="", returncode=0),  # git rev-parse
            ]

            result = git_checkout_branch("feature", create_new=True, base_branch="main")

            assert result.success is True
            # When branch exists locally, it should just checkout without -B flag
            calls = mock_cmd.run_command.call_args_list
            # Find the checkout command and verify it's a simple checkout (no -B or -b flag)
            # The git checkout command should not have -B or -b in it
            checkout_found = False
            for call in calls:
                cmd_args = call[0][0]
                if cmd_args[0] == "git" and cmd_args[1] == "checkout":
                    checkout_found = True
                    assert "-b" not in cmd_args
                    assert "-B" not in cmd_args
                    assert cmd_args == ["git", "checkout", "feature"]
                    break
            assert checkout_found, "Git checkout command should have been called"


class TestBranchConflicts:
    """Test branch name conflict detection."""

    def test_conflict_parent_exists(self):
        """Test creating child branch when parent exists."""
        with patch("src.auto_coder.git_branch.branch_exists") as mock_exists, patch("src.auto_coder.git_branch.get_branches_by_pattern") as mock_pattern:
            # Setup: create 'issue-123' branch
            mock_exists.return_value = True
            mock_pattern.return_value = []

            # Test: try to create 'issue-123/attempt-1'
            result = detect_branch_name_conflict("issue-123/attempt-1")

            # Verify: fails with conflict detection
            assert result == "issue-123"
            mock_exists.assert_called_once_with("issue-123", None)

    def test_conflict_child_exists(self):
        """Test creating parent branch when child exists."""
        with patch("src.auto_coder.git_branch.branch_exists") as mock_exists, patch("src.auto_coder.git_branch.get_branches_by_pattern") as mock_pattern:
            # Setup: create 'issue-123/attempt-1' branch
            mock_exists.return_value = False
            mock_pattern.return_value = ["issue-123/attempt-1"]

            # Test: try to create 'issue-123'
            result = detect_branch_name_conflict("issue-123")

            # Verify: fails with conflict detection
            assert result == "issue-123/attempt-1"
            mock_pattern.assert_called_once_with("issue-123/*", cwd=None, remote=False)

    def test_detect_branch_name_conflict(self):
        """Test conflict detection function."""
        with patch("src.auto_coder.git_branch.branch_exists") as mock_exists, patch("src.auto_coder.git_branch.get_branches_by_pattern") as mock_pattern:
            # Setup: create 'issue-123'
            mock_exists.return_value = True
            mock_pattern.return_value = []

            # Test: detect_branch_name_conflict('issue-123/attempt-1')
            result = detect_branch_name_conflict("issue-123/attempt-1")

            # Verify: returns 'issue-123'
            assert result == "issue-123"

    def test_detect_conflict_with_multiple_child_branches(self):
        """Test conflict detection when multiple child branches exist."""
        with patch("src.auto_coder.git_branch.branch_exists") as mock_exists, patch("src.auto_coder.git_branch.get_branches_by_pattern") as mock_pattern:
            # Setup: multiple child branches exist
            mock_exists.return_value = False
            mock_pattern.return_value = ["issue-123/attempt-1", "issue-123/attempt-2", "issue-123/attempt-3"]

            # Test: detect conflict for 'issue-123'
            result = detect_branch_name_conflict("issue-123")

            # Verify: returns first conflicting child branch
            assert result == "issue-123/attempt-1"
            mock_pattern.assert_called_once_with("issue-123/*", cwd=None, remote=False)

    def test_detect_conflict_with_nested_branch_hierarchy(self):
        """Test conflict detection with multiple levels of branch hierarchy."""
        with patch("src.auto_coder.git_branch.branch_exists") as mock_exists, patch("src.auto_coder.git_branch.get_branches_by_pattern") as mock_pattern:
            # Setup: nested branch hierarchy
            mock_exists.return_value = False
            mock_pattern.return_value = ["feature/issue-123/attempt-1"]

            # Test: detect conflict for 'feature/issue-123'
            result = detect_branch_name_conflict("feature/issue-123")

            # Verify: returns first conflicting branch at that level
            assert result == "feature/issue-123/attempt-1"
            mock_pattern.assert_called_once_with("feature/issue-123/*", cwd=None, remote=False)


class TestBranchEdgeCases:
    """Test branch edge cases."""

    @pytest.mark.usefixtures("_use_custom_subprocess_mock")
    def test_branch_with_special_characters(self):
        """Test branch with special characters."""
        with patch("src.auto_coder.git_branch.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git status
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git branch --list
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git fetch
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git branch --list
                CommandResult(success=False, stdout="", stderr="fatal: couldn't find remote ref", returncode=1),  # git ls-remote
                CommandResult(success=False, stdout="", stderr="fatal: ref not found", returncode=1),  # git rev-parse refs/remotes/origin/main
                CommandResult(success=True, stdout="abc123\n", stderr="", returncode=0),  # git rev-parse main
                CommandResult(
                    success=True,
                    stdout="Switched to branch 'feature/issue-123_test'\n",
                    stderr="",
                    returncode=0,
                ),  # git checkout -B
                CommandResult(success=True, stdout="feature/issue-123_test\n", stderr="", returncode=0),  # git rev-parse
                CommandResult(success=True, stdout="Branch 'feature/issue-123_test' set up to track remote branch", stderr="", returncode=0),  # git push
            ]

            with patch("src.auto_coder.git_branch.branch_exists") as mock_exists, patch("src.auto_coder.git_branch.get_branches_by_pattern") as mock_pattern:
                mock_exists.return_value = False
                mock_pattern.return_value = []

                result = git_checkout_branch("feature/issue-123_test", create_new=True, base_branch="main")

                assert result.success is True

    @pytest.mark.usefixtures("_use_custom_subprocess_mock")
    def test_very_long_branch_name(self):
        """Test branch with very long name."""
        with patch("src.auto_coder.git_branch.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            # Long branch name
            long_branch_name = "feature/very-long-branch-name-with-many-words-and-numbers-123456789"
            mock_cmd.run_command.side_effect = [
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git status
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git branch --list
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git fetch
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git branch --list
                CommandResult(success=False, stdout="", stderr="fatal: couldn't find remote ref", returncode=1),  # git ls-remote
                CommandResult(success=False, stdout="", stderr="fatal: ref not found", returncode=1),  # git rev-parse refs/remotes/origin/main
                CommandResult(success=True, stdout="abc123\n", stderr="", returncode=0),  # git rev-parse main
                CommandResult(
                    success=True,
                    stdout=f"Switched to branch '{long_branch_name}'\n",
                    stderr="",
                    returncode=0,
                ),  # git checkout -B
                CommandResult(success=True, stdout=f"{long_branch_name}\n", stderr="", returncode=0),  # git rev-parse
                CommandResult(success=True, stdout=f"Branch '{long_branch_name}' set up to track remote branch", stderr="", returncode=0),  # git push
            ]

            with patch("src.auto_coder.git_branch.branch_exists") as mock_exists, patch("src.auto_coder.git_branch.get_branches_by_pattern") as mock_pattern:
                mock_exists.return_value = False
                mock_pattern.return_value = []

                result = git_checkout_branch(long_branch_name, create_new=True, base_branch="main")

                assert result.success is True

    @pytest.mark.usefixtures("_use_custom_subprocess_mock")
    def test_branch_with_multiple_path_separators(self):
        """Test branch names with multiple path separators."""
        with patch("src.auto_coder.git_branch.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            branch_name = "feature/sub/issue-123"
            mock_cmd.run_command.side_effect = [
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git status
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git branch --list
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git fetch
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git branch --list
                CommandResult(success=False, stdout="", stderr="fatal: couldn't find remote ref", returncode=1),  # git ls-remote
                CommandResult(success=False, stdout="", stderr="fatal: ref not found", returncode=1),  # git rev-parse refs/remotes/origin/main
                CommandResult(success=True, stdout="abc123\n", stderr="", returncode=0),  # git rev-parse main
                CommandResult(
                    success=True,
                    stdout=f"Switched to branch '{branch_name}'\n",
                    stderr="",
                    returncode=0,
                ),  # git checkout -B
                CommandResult(success=True, stdout=f"{branch_name}\n", stderr="", returncode=0),  # git rev-parse
                CommandResult(success=True, stdout=f"Branch '{branch_name}' set up to track remote branch", stderr="", returncode=0),  # git push
            ]

            with patch("src.auto_coder.git_branch.branch_exists") as mock_exists, patch("src.auto_coder.git_branch.get_branches_by_pattern") as mock_pattern:
                mock_exists.return_value = False
                mock_pattern.return_value = []

                result = git_checkout_branch(branch_name, create_new=True, base_branch="main")

                assert result.success is True

    @pytest.mark.usefixtures("_use_custom_subprocess_mock")
    def test_detect_conflict_with_nested_paths(self):
        """Test conflict detection with nested path separators."""
        with patch("src.auto_coder.git_branch.branch_exists") as mock_exists, patch("src.auto_coder.git_branch.get_branches_by_pattern") as mock_pattern:
            # Setup: parent branch exists for nested path
            # The function checks the immediate parent using rsplit("/", 1)[0]
            # So for "feature/sub/issue-123/attempt-1", it checks "feature/sub/issue-123"
            mock_exists.side_effect = lambda name, _: name == "feature/sub/issue-123"
            mock_pattern.return_value = []

            # Test: try to create nested child branch
            result = detect_branch_name_conflict("feature/sub/issue-123/attempt-1")

            # Verify: detects immediate parent branch conflict
            assert result == "feature/sub/issue-123"
            mock_exists.assert_called_once_with("feature/sub/issue-123", None)
