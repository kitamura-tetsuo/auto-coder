"""Tests for git_utils module."""

import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.git_utils import (
    extract_number_from_branch,
    get_current_branch,
    get_current_repo_name,
    git_checkout_branch,
    git_commit_with_retry,
    git_push,
    is_git_repository,
    parse_github_repo_from_url,
    save_commit_failure_history,
)
from src.auto_coder.utils import CommandResult


class TestGitCommitWithRetry:
    """Tests for git_commit_with_retry function."""

    def test_successful_commit(self):
        """Test successful commit without retry."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(
                success=True, stdout="", stderr="", returncode=0
            )

            result = git_commit_with_retry("Test commit message")

            assert result.success is True
            mock_cmd.run_command.assert_called_once()
            call_args = mock_cmd.run_command.call_args
            assert call_args[0][0] == ["git", "commit", "-m", "Test commit message"]

    def test_commit_with_dprint_error_and_retry(self):
        """Test commit with dprint formatting error triggers retry."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd

            # First call: commit fails with dprint error
            # Second call: dprint fmt succeeds
            # Third call: git add succeeds
            # Fourth call: commit succeeds
            mock_cmd.run_command.side_effect = [
                CommandResult(
                    success=False,
                    stdout="",
                    stderr="Formatting issues detected. Run 'npx dprint fmt' to fix.",
                    returncode=1,
                ),
                CommandResult(
                    success=True, stdout="", stderr="", returncode=0
                ),  # dprint fmt
                CommandResult(
                    success=True, stdout="", stderr="", returncode=0
                ),  # git add
                CommandResult(
                    success=True, stdout="", stderr="", returncode=0
                ),  # commit retry
            ]

            result = git_commit_with_retry("Test commit message")

            assert result.success is True
            assert mock_cmd.run_command.call_count == 4

            # Check that dprint fmt was called
            calls = mock_cmd.run_command.call_args_list
            assert calls[1][0][0] == ["npx", "dprint", "fmt"]
            assert calls[2][0][0] == ["git", "add", "-u"]
            assert calls[3][0][0] == ["git", "commit", "-m", "Test commit message"]

    def test_commit_with_dprint_error_fmt_fails(self):
        """Test commit with dprint error but formatter fails."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd

            # First call: commit fails with dprint error (attempt 0)
            # Second call: dprint fmt fails
            # Third call: commit fails again (attempt 1, max_retries reached)
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

    def test_commit_with_non_dprint_error(self):
        """Test commit with non-dprint error does not trigger retry."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(
                success=False,
                stdout="",
                stderr="nothing to commit, working tree clean",
                returncode=1,
            )

            result = git_commit_with_retry("Test commit message")

            assert result.success is False
            # Should only be called once (no retry for non-dprint errors)
            mock_cmd.run_command.assert_called_once()

    def test_commit_with_cwd(self):
        """Test commit with custom working directory."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(
                success=True, stdout="", stderr="", returncode=0
            )

            result = git_commit_with_retry("Test commit", cwd="/custom/path")

            assert result.success is True
            call_args = mock_cmd.run_command.call_args
            assert call_args[1]["cwd"] == "/custom/path"


class TestGitPush:
    """Tests for git_push function."""

    def test_successful_push(self):
        """Test successful push without branch specified."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                # First call: get current branch
                CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
                # Second call: push
                CommandResult(success=True, stdout="", stderr="", returncode=0),
            ]

            result = git_push()

            assert result.success is True
            assert mock_cmd.run_command.call_count == 2
            # Check the second call was the push command
            second_call_args = mock_cmd.run_command.call_args_list[1][0][0]
            assert second_call_args == ["git", "push"]

    def test_push_with_branch(self):
        """Test push with specific branch."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            # When branch is specified, no need to get current branch
            mock_cmd.run_command.return_value = CommandResult(
                success=True, stdout="", stderr="", returncode=0
            )

            result = git_push(branch="feature-branch")

            assert result.success is True
            assert mock_cmd.run_command.call_count == 1
            # Check the call was the push command with branch
            call_args = mock_cmd.run_command.call_args[0][0]
            assert call_args == ["git", "push", "origin", "feature-branch"]

    def test_push_with_custom_remote(self):
        """Test push with custom remote."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            # When branch is specified, no need to get current branch
            mock_cmd.run_command.return_value = CommandResult(
                success=True, stdout="", stderr="", returncode=0
            )

            result = git_push(remote="upstream", branch="main")

            assert result.success is True
            assert mock_cmd.run_command.call_count == 1
            # Check the call was the push command with custom remote
            call_args = mock_cmd.run_command.call_args[0][0]
            assert call_args == ["git", "push", "upstream", "main"]

    def test_push_failure(self):
        """Test push failure."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                # First call: get current branch
                CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
                # Second call: push fails
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
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                # First call: get current branch
                CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
                # Second call: push
                CommandResult(success=True, stdout="", stderr="", returncode=0),
            ]

            result = git_push(cwd="/custom/path")

            assert result.success is True
            assert mock_cmd.run_command.call_count == 2
            # Check that cwd was passed to both calls
            assert mock_cmd.run_command.call_args_list[0][1]["cwd"] == "/custom/path"
            assert mock_cmd.run_command.call_args_list[1][1]["cwd"] == "/custom/path"

    def test_push_no_upstream_auto_retry(self):
        """Test push automatically retries with --set-upstream when upstream is not
        set.
        """
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                # First call: get current branch
                CommandResult(
                    success=True, stdout="issue-733\n", stderr="", returncode=0
                ),
                # Second call: push fails with no upstream error
                CommandResult(
                    success=False,
                    stdout="",
                    stderr=(
                        "fatal: The current branch issue-733 has no upstream branch.\n"
                        "To push the current branch and set the remote as upstream, "
                        "use\n\n"
                        "    git push --set-upstream origin issue-733\n"
                    ),
                    returncode=1,
                ),
                # Third call: push with --set-upstream succeeds
                CommandResult(success=True, stdout="", stderr="", returncode=0),
            ]

            result = git_push()

            assert result.success is True
            assert mock_cmd.run_command.call_count == 3
            # Check the third call used --set-upstream
            third_call_args = mock_cmd.run_command.call_args_list[2][0][0]
            assert third_call_args == [
                "git",
                "push",
                "--set-upstream",
                "origin",
                "issue-733",
            ]

    def test_push_no_upstream_with_branch_specified(self):
        """Test push with branch specified automatically retries with --set-upstream."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                # First call: push fails with no upstream error
                CommandResult(
                    success=False,
                    stdout="",
                    stderr=(
                        "fatal: The current branch feature-branch has no upstream branch.\n"
                        "To push the current branch and set the remote as upstream, "
                        "use\n\n"
                        "    git push --set-upstream origin feature-branch\n"
                    ),
                    returncode=1,
                ),
                # Second call: push with --set-upstream succeeds
                CommandResult(success=True, stdout="", stderr="", returncode=0),
            ]

            result = git_push(branch="feature-branch")

            assert result.success is True
            assert mock_cmd.run_command.call_count == 2
            # Check the second call used --set-upstream with the specified branch
            second_call_args = mock_cmd.run_command.call_args_list[1][0][0]
            assert second_call_args == [
                "git",
                "push",
                "--set-upstream",
                "origin",
                "feature-branch",
            ]

    def test_push_other_error_no_retry(self):
        """Test push does not retry for errors other than missing upstream."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                # First call: get current branch
                CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
                # Second call: push fails with different error
                CommandResult(
                    success=False,
                    stdout="",
                    stderr="error: failed to push some refs to 'origin'",
                    returncode=1,
                ),
            ]

            result = git_push()

            assert result.success is False
            assert mock_cmd.run_command.call_count == 2  # No retry
            assert "failed to push some refs" in result.stderr

    def test_push_with_dprint_error_and_retry(self):
        """Test push with dprint formatting error triggers retry without commit
        message.
        """
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                # First call: get current branch
                CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
                # Second call: push fails with dprint error
                CommandResult(
                    success=False,
                    stdout="",
                    stderr=(
                        "You may want to try using `dprint output-file-paths` to see which "
                        "files it's finding"
                    ),
                    returncode=1,
                ),
                # Third call: dprint fmt succeeds
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Fourth call: git add -A succeeds
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Fifth call: push retry succeeds
                CommandResult(success=True, stdout="", stderr="", returncode=0),
            ]

            result = git_push()

            assert result.success is True
            assert mock_cmd.run_command.call_count == 5
            # Check that dprint fmt was called
            calls = mock_cmd.run_command.call_args_list
            assert calls[2][0][0] == ["npx", "dprint", "fmt"]
            assert calls[3][0][0] == ["git", "add", "-A"]
            assert calls[4][0][0] == ["git", "push"]

    def test_push_with_dprint_error_and_commit_message(self):
        """Test push with dprint formatting error and commit message triggers
        re-commit.
        """
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                # First call: get current branch
                CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
                # Second call: push fails with dprint error
                CommandResult(
                    success=False,
                    stdout="",
                    stderr=(
                        "You may want to try using `dprint output-file-paths` to see which "
                        "files it's finding"
                    ),
                    returncode=1,
                ),
                # Third call: dprint fmt succeeds
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Fourth call: git add -A succeeds
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Fifth call: git commit --amend --no-edit succeeds
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Sixth call: push retry succeeds
                CommandResult(success=True, stdout="", stderr="", returncode=0),
            ]

            result = git_push(commit_message="Fix: automated changes")

            assert result.success is True
            assert mock_cmd.run_command.call_count == 6
            # Check that dprint fmt was called
            calls = mock_cmd.run_command.call_args_list
            assert calls[2][0][0] == ["npx", "dprint", "fmt"]
            assert calls[3][0][0] == ["git", "add", "-A"]
            assert calls[4][0][0] == ["git", "commit", "--amend", "--no-edit"]
            assert calls[5][0][0] == ["git", "push"]

    def test_push_with_dprint_error_fmt_fails(self):
        """Test push with dprint error but formatter fails."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                # First call: get current branch
                CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
                # Second call: push fails with dprint error
                CommandResult(
                    success=False,
                    stdout="",
                    stderr="dprint output-file-paths error detected",
                    returncode=1,
                ),
                # Third call: dprint fmt fails
                CommandResult(
                    success=False,
                    stdout="",
                    stderr="dprint command not found",
                    returncode=1,
                ),
            ]

            result = git_push()

            assert result.success is False
            assert "dprint output-file-paths" in result.stderr
            assert mock_cmd.run_command.call_count == 3

    def test_push_with_dprint_error_commit_amend_fails(self):
        """Test push with dprint error when commit amend fails, falls back to regular
        commit.
        """
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                # First call: get current branch
                CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
                # Second call: push fails with dprint error
                CommandResult(
                    success=False,
                    stdout="",
                    stderr="dprint output-file-paths error detected",
                    returncode=1,
                ),
                # Third call: dprint fmt succeeds
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Fourth call: git add -A succeeds
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Fifth call: git commit --amend --no-edit fails
                CommandResult(
                    success=False,
                    stdout="",
                    stderr="fatal: You are in the middle of a merge -- cannot amend.",
                    returncode=1,
                ),
                # Sixth call: git commit -m succeeds
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Seventh call: push retry succeeds
                CommandResult(success=True, stdout="", stderr="", returncode=0),
            ]

            result = git_push(commit_message="Fix: automated changes")

            assert result.success is True
            assert mock_cmd.run_command.call_count == 7
            # Check that regular commit was called after amend failed
            calls = mock_cmd.run_command.call_args_list
            assert calls[4][0][0] == ["git", "commit", "--amend", "--no-edit"]
            assert calls[5][0][0] == ["git", "commit", "-m", "Fix: automated changes"]
            assert calls[6][0][0] == ["git", "push"]

    def test_push_with_dprint_error_and_upstream_retry(self):
        """Test push with dprint error and then upstream error triggers both retries."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                # First call: get current branch
                CommandResult(
                    success=True, stdout="feature\n", stderr="", returncode=0
                ),
                # Second call: push fails with dprint error
                CommandResult(
                    success=False,
                    stdout="",
                    stderr=(
                        "You may want to try using `dprint output-file-paths` to see which "
                        "files it's finding"
                    ),
                    returncode=1,
                ),
                # Third call: dprint fmt succeeds
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Fourth call: git add -A succeeds
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Fifth call: push retry fails with upstream error
                CommandResult(
                    success=False,
                    stdout="",
                    stderr=(
                        "fatal: The current branch feature has no upstream branch.\n"
                        "To push the current branch and set the remote as upstream, use\n\n"
                        "    git push --set-upstream origin feature\n"
                    ),
                    returncode=1,
                ),
                # Sixth call: push with --set-upstream succeeds
                CommandResult(success=True, stdout="", stderr="", returncode=0),
            ]

            result = git_push()

            assert result.success is True
            assert mock_cmd.run_command.call_count == 6
            # Check the final call used --set-upstream
            final_call_args = mock_cmd.run_command.call_args_list[5][0][0]
            assert final_call_args == [
                "git",
                "push",
                "--set-upstream",
                "origin",
                "feature",
            ]


class TestParseGithubRepoFromUrl:
    """Tests for parse_github_repo_from_url function."""

    def test_https_url(self):
        """Test parsing HTTPS URL."""
        url = "https://github.com/owner/repo"
        result = parse_github_repo_from_url(url)
        assert result == "owner/repo"

    def test_https_url_with_git_suffix(self):
        """Test parsing HTTPS URL with .git suffix."""
        url = "https://github.com/owner/repo.git"
        result = parse_github_repo_from_url(url)
        assert result == "owner/repo"

    def test_ssh_url(self):
        """Test parsing SSH URL."""
        url = "git@github.com:owner/repo"
        result = parse_github_repo_from_url(url)
        assert result == "owner/repo"

    def test_ssh_url_with_git_suffix(self):
        """Test parsing SSH URL with .git suffix."""
        url = "git@github.com:owner/repo.git"
        result = parse_github_repo_from_url(url)
        assert result == "owner/repo"

    def test_ssh_alternative_url(self):
        """Test parsing alternative SSH URL format."""
        url = "ssh://git@github.com/owner/repo"
        result = parse_github_repo_from_url(url)
        assert result == "owner/repo"

    def test_non_github_url(self):
        """Test parsing non-GitHub URL returns None."""
        url = "https://gitlab.com/owner/repo"
        result = parse_github_repo_from_url(url)
        assert result is None

    def test_invalid_url(self):
        """Test parsing invalid URL returns None."""
        url = "not-a-valid-url"
        result = parse_github_repo_from_url(url)
        assert result is None

    def test_empty_url(self):
        """Test parsing empty URL returns None."""
        result = parse_github_repo_from_url("")
        assert result is None

    def test_none_url(self):
        """Test parsing None URL returns None."""
        result = parse_github_repo_from_url(None)
        assert result is None


class TestSaveCommitFailureHistory:
    """Tests for save_commit_failure_history function."""

    def test_save_commit_failure_history_with_repo_name(self, tmp_path):
        """Test saving commit failure history with repo name."""
        # Mock Path.home() to use tmp_path
        with patch("src.auto_coder.git_utils.Path.home") as mock_home:
            mock_home.return_value = tmp_path

            error_message = "Test error message"
            context = {"type": "test", "issue_number": 123}
            repo_name = "owner/repo"

            # This should exit with code 1
            with pytest.raises(SystemExit) as exc_info:
                save_commit_failure_history(error_message, context, repo_name)

            assert exc_info.value.code == 1

            # Check that the history file was created
            history_dir = tmp_path / ".auto-coder" / "owner_repo"
            assert history_dir.exists()

            # Find the history file
            history_files = list(history_dir.glob("commit_failure_*.json"))
            assert len(history_files) == 1

            # Check the content
            with open(history_files[0], "r") as f:
                data = json.load(f)

            assert data["error_message"] == error_message
            assert data["context"] == context
            assert "timestamp" in data

    def test_save_commit_failure_history_without_repo_name(self, tmp_path):
        """Test saving commit failure history without repo name."""
        # Change to tmp_path directory
        original_cwd = os.getcwd()
        try:
            os.chdir(tmp_path)

            error_message = "Test error message"
            context = {"type": "test", "pr_number": 456}

            # This should exit with code 1
            with pytest.raises(SystemExit) as exc_info:
                save_commit_failure_history(error_message, context, None)

            assert exc_info.value.code == 1

            # Check that the history file was created
            history_dir = tmp_path / ".auto-coder"
            assert history_dir.exists()

            # Find the history file
            history_files = list(history_dir.glob("commit_failure_*.json"))
            assert len(history_files) == 1

            # Check the content
            with open(history_files[0], "r") as f:
                data = json.load(f)

            assert data["error_message"] == error_message
            assert data["context"] == context
            assert "timestamp" in data
        finally:
            os.chdir(original_cwd)


class TestGitCheckoutBranch:
    """Tests for git_checkout_branch function."""

    def test_successful_checkout_existing_branch(self):
        """Test successful checkout of an existing branch."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                # First call: git status --porcelain (no changes)
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Second call: git checkout
                CommandResult(
                    success=True,
                    stdout="Switched to branch 'feature'\n",
                    stderr="",
                    returncode=0,
                ),
                # Third call: verify current branch
                CommandResult(
                    success=True, stdout="feature\n", stderr="", returncode=0
                ),
            ]

            result = git_checkout_branch("feature")

            assert result.success is True
            assert mock_cmd.run_command.call_count == 3
            # Verify status command
            assert mock_cmd.run_command.call_args_list[0][0][0] == [
                "git",
                "status",
                "--porcelain",
            ]
            # Verify checkout command
            assert mock_cmd.run_command.call_args_list[1][0][0] == [
                "git",
                "checkout",
                "feature",
            ]
            # Verify verification command
            assert mock_cmd.run_command.call_args_list[2][0][0] == [
                "git",
                "rev-parse",
                "--abbrev-ref",
                "HEAD",
            ]

    def test_successful_checkout_create_new_branch(self):
        """Test successful checkout with creating a new branch."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                # First call: git status --porcelain (has changes, needs commit)
                CommandResult(
                    success=True, stdout="M  test.py", stderr="", returncode=0
                ),
                # Second call: git add -A (from git_commit_with_retry)
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Third call: git commit (from git_commit_with_retry)
                CommandResult(
                    success=True,
                    stdout="WIP: Auto-commit before branch checkout\n",
                    stderr="",
                    returncode=0,
                ),
                # Fourth call: git branch --list new-feature (check if branch exists)
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Fifth call: git checkout -b
                CommandResult(
                    success=True,
                    stdout="Switched to a new branch 'new-feature'\n",
                    stderr="",
                    returncode=0,
                ),
                # Sixth call: verify current branch
                CommandResult(
                    success=True, stdout="new-feature\n", stderr="", returncode=0
                ),
                # Seventh call: git branch --list new-feature_backup
                # (check if was new branch)
                CommandResult(
                    success=True, stdout="", stderr="", returncode=0
                ),
                # Eighth call: git branch --list origin/new-feature
                # (check if remote exists)
                CommandResult(
                    success=True, stdout="", stderr="", returncode=0
                ),
                # Ninth call: git push -u origin new-feature
                CommandResult(
                    success=True,
                    stdout=(
                        "Branch 'new-feature' set up to track remote branch 'new-feature' from "
                        "'origin'.\n"
                    ),
                    stderr="",
                    returncode=0,
                ),
            ]

            result = git_checkout_branch("new-feature", create_new=True)

            assert result.success is True
            assert mock_cmd.run_command.call_count == 9
            # Verify checkout command with -b flag
            assert mock_cmd.run_command.call_args_list[4][0][0] == [
                "git",
                "checkout",
                "-b",
                "new-feature",
            ]
            # Verify push command
            assert mock_cmd.run_command.call_args_list[8][0][0] == [
                "git",
                "push",
                "-u",
                "origin",
                "new-feature",
            ]

    def test_successful_checkout_create_from_base_branch(self):
        """Test successful checkout with creating a new branch from base branch."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                # First call: git status --porcelain (no changes)
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Second call: git branch --list new-feature (check if branch exists)
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Third call: git checkout -B
                CommandResult(
                    success=True,
                    stdout="Switched to branch 'new-feature'\n",
                    stderr="",
                    returncode=0,
                ),
                # Fourth call: verify current branch
                CommandResult(
                    success=True, stdout="new-feature\n", stderr="", returncode=0
                ),
                # Fifth call: git branch --list new-feature_backup (check if was new
                # branch)
                CommandResult(
                    success=True, stdout="", stderr="", returncode=0
                ),
                # Sixth call: git branch --list origin/new-feature
                # (check if remote exists)
                CommandResult(
                    success=True, stdout="", stderr="", returncode=0
                ),
                # Seventh call: git push -u origin new-feature
                CommandResult(
                    success=True,
                    stdout=(
                        "Branch 'new-feature' set up to track remote branch 'new-feature' from "
                        "'origin'.\n"
                    ),
                    stderr="",
                    returncode=0,
                ),
            ]

            result = git_checkout_branch(
                "new-feature", create_new=True, base_branch="main"
            )

            assert result.success is True
            assert mock_cmd.run_command.call_count == 7
            # Verify checkout command with -B flag
            assert mock_cmd.run_command.call_args_list[2][0][0] == [
                "git",
                "checkout",
                "-B",
                "new-feature",
            ]
            # Verify push command
            assert mock_cmd.run_command.call_args_list[6][0][0] == [
                "git",
                "push",
                "-u",
                "origin",
                "new-feature",
            ]

    def test_checkout_failure(self):
        """Test checkout failure."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                # First call: git status --porcelain (no changes)
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Second call: git checkout fails
                CommandResult(
                    success=False,
                    stdout="",
                    stderr=(
                        "error: pathspec 'nonexistent' did not match any file(s) known to "
                        "git"
                    ),
                    returncode=1,
                ),
            ]

            result = git_checkout_branch("nonexistent")

            assert result.success is False
            assert "pathspec 'nonexistent' did not match" in result.stderr
            # Should call status and checkout, not verification
            assert mock_cmd.run_command.call_count == 2

    def test_checkout_success_but_verification_fails(self):
        """Test checkout succeeds but verification command fails."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                # First call: git status --porcelain (no changes)
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Second call: git checkout succeeds
                CommandResult(
                    success=True,
                    stdout="Switched to branch 'feature'\n",
                    stderr="",
                    returncode=0,
                ),
                # Third call: verification fails
                CommandResult(
                    success=False,
                    stdout="",
                    stderr="fatal: not a git repository",
                    returncode=128,
                ),
            ]

            result = git_checkout_branch("feature")

            assert result.success is False
            assert "verification failed" in result.stderr
            assert mock_cmd.run_command.call_count == 3

    def test_checkout_success_but_branch_mismatch(self):
        """Test checkout succeeds but current branch doesn't match expected."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                # First call: git status --porcelain (no changes)
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Second call: git checkout
                CommandResult(
                    success=True,
                    stdout="Switched to branch 'feature'\n",
                    stderr="",
                    returncode=0,
                ),
                # Third call: verify returns different branch
                CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
            ]

            result = git_checkout_branch("feature")

            assert result.success is False
            assert "Branch mismatch" in result.stderr
            assert "expected 'feature'" in result.stderr
            assert "currently on 'main'" in result.stderr
            assert mock_cmd.run_command.call_count == 3

    def test_checkout_with_cwd(self):
        """Test checkout with custom working directory."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                # First call: git status --porcelain (no changes)
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Second call: git checkout
                CommandResult(
                    success=True,
                    stdout="Switched to branch 'feature'\n",
                    stderr="",
                    returncode=0,
                ),
                # Third call: verify current branch
                CommandResult(
                    success=True, stdout="feature\n", stderr="", returncode=0
                ),
            ]

            result = git_checkout_branch("feature", cwd="/custom/path")

            assert result.success is True
            # Check that cwd was passed to all calls
            assert mock_cmd.run_command.call_args_list[0][1]["cwd"] == "/custom/path"
            assert mock_cmd.run_command.call_args_list[1][1]["cwd"] == "/custom/path"
            assert mock_cmd.run_command.call_args_list[2][1]["cwd"] == "/custom/path"

    def test_create_new_branch_push_failure(self):
        """Test creating a new branch when push fails (should still succeed)."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                # First call: git status --porcelain (no changes)
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Second call: git branch --list new-feature (check if branch exists)
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Third call: git checkout -b
                CommandResult(
                    success=True,
                    stdout="Switched to a new branch 'new-feature'\n",
                    stderr="",
                    returncode=0,
                ),
                # Fourth call: verify current branch
                CommandResult(
                    success=True, stdout="new-feature\n", stderr="", returncode=0
                ),
                # Fifth call: git branch --list new-feature_backup (check if was new
                # branch)
                CommandResult(
                    success=True, stdout="", stderr="", returncode=0
                ),
                # Sixth call: git branch --list origin/new-feature
                # (check if remote exists)
                CommandResult(
                    success=True, stdout="", stderr="", returncode=0
                ),
                # Seventh call: git push fails
                CommandResult(
                    success=False,
                    stdout="",
                    stderr="fatal: unable to access remote",
                    returncode=1,
                ),
            ]

            result = git_checkout_branch("new-feature", create_new=True)

            # Should still succeed even if push fails
            assert result.success is True
            assert mock_cmd.run_command.call_count == 7

    def test_create_new_branch_without_publish(self):
        """Test creating a new branch without publishing to remote."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                # First call: git status --porcelain (no changes)
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Second call: git branch --list new-feature (check if branch exists)
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Third call: git checkout -b
                CommandResult(
                    success=True,
                    stdout="Switched to a new branch 'new-feature'\n",
                    stderr="",
                    returncode=0,
                ),
                # Fourth call: verify current branch
                CommandResult(
                    success=True, stdout="new-feature\n", stderr="", returncode=0
                ),
            ]

            result = git_checkout_branch("new-feature", create_new=True, publish=False)

            assert result.success is True
            # Should have 4 calls (status, branch check, checkout, verify), no push or additional branch checks
            assert mock_cmd.run_command.call_count == 4

    def test_checkout_with_uncommitted_changes_auto_commit(self):
        """Test checkout with uncommitted changes automatically commits them."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                # First call: git status --porcelain (has changes)
                CommandResult(
                    success=True, stdout=" M file.txt\n", stderr="", returncode=0
                ),
                # Second call: git add -A
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Third call: git commit (from git_commit_with_retry)
                CommandResult(
                    success=True,
                    stdout="[main abc123] WIP: Auto-commit before branch checkout\n",
                    stderr="",
                    returncode=0,
                ),
                # Fourth call: git checkout
                CommandResult(
                    success=True,
                    stdout="Switched to branch 'feature'\n",
                    stderr="",
                    returncode=0,
                ),
                # Fifth call: verify current branch
                CommandResult(
                    success=True, stdout="feature\n", stderr="", returncode=0
                ),
            ]

            result = git_checkout_branch("feature")

            assert result.success is True
            assert mock_cmd.run_command.call_count == 5
            # Verify that git add was called
            assert mock_cmd.run_command.call_args_list[1][0][0] == ["git", "add", "-A"]
            # Verify that commit was called
            assert mock_cmd.run_command.call_args_list[2][0][0] == [
                "git",
                "commit",
                "-m",
                "WIP: Auto-commit before branch checkout",
            ]

    def test_checkout_with_uncommitted_changes_error_retry(self):
        """Test checkout fails with uncommitted changes error, then retries after commit."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                # First call: git status --porcelain (no changes detected initially)
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Second call: git checkout fails with uncommitted changes error
                CommandResult(
                    success=False,
                    stdout="",
                    stderr=(
                        "error: Your local changes to the following files would be overwritten "
                        "by checkout:\n\tfile.txt\n"
                        "Please commit your changes or stash them before you switch branches.\n"
                        "Aborting"
                    ),
                    returncode=1,
                ),
                # Third call: git add -A
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Fourth call: git commit (from git_commit_with_retry)
                CommandResult(
                    success=True,
                    stdout=(
                        "[main abc123] WIP: Auto-commit before branch checkout (retry)\n"
                    ),
                    stderr="",
                    returncode=0,
                ),
                # Fifth call: git checkout retry
                CommandResult(
                    success=True,
                    stdout="Switched to branch 'feature'\n",
                    stderr="",
                    returncode=0,
                ),
                # Sixth call: verify current branch
                CommandResult(
                    success=True, stdout="feature\n", stderr="", returncode=0
                ),
            ]

            result = git_checkout_branch("feature")

            assert result.success is True
            assert mock_cmd.run_command.call_count == 6
            # Verify that git add was called after error
            assert mock_cmd.run_command.call_args_list[2][0][0] == ["git", "add", "-A"]
            # Verify that commit was called with retry message
            assert mock_cmd.run_command.call_args_list[3][0][0] == [
                "git",
                "commit",
                "-m",
                "WIP: Auto-commit before branch checkout (retry)",
            ]
            # Verify that checkout was retried
            assert mock_cmd.run_command.call_args_list[4][0][0] == [
                "git",
                "checkout",
                "feature",
            ]

    def test_checkout_with_uncommitted_changes_commit_fails(self):
        """Test checkout with uncommitted changes when commit fails."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                # First call: git status --porcelain (no changes detected initially)
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Second call: git checkout fails with uncommitted changes error
                CommandResult(
                    success=False,
                    stdout="",
                    stderr=(
                        "error: Your local changes to the following files would be overwritten "
                        "by checkout:\n\tfile.txt"
                    ),
                    returncode=1,
                ),
                # Third call: git add -A
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Fourth call: git commit fails
                CommandResult(
                    success=False,
                    stdout="",
                    stderr="fatal: unable to create commit",
                    returncode=1,
                ),
            ]

            result = git_checkout_branch("feature")

            assert result.success is False
            assert "would be overwritten by checkout" in result.stderr
            # Should stop after commit fails
            assert mock_cmd.run_command.call_count == 4


class TestGetCurrentBranch:
    """Tests for get_current_branch function."""

    def test_get_current_branch_success(self):
        """Test successful retrieval of current branch."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(
                success=True, stdout="main\n", stderr="", returncode=0
            )

            result = get_current_branch()

            assert result == "main"
            mock_cmd.run_command.assert_called_once_with(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=None
            )

    def test_get_current_branch_with_cwd(self):
        """Test get_current_branch with custom working directory."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(
                success=True, stdout="feature-branch\n", stderr="", returncode=0
            )

            result = get_current_branch(cwd="/path/to/repo")

            assert result == "feature-branch"
            mock_cmd.run_command.assert_called_once_with(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd="/path/to/repo"
            )

    def test_get_current_branch_failure(self):
        """Test get_current_branch when git command fails."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(
                success=False,
                stdout="",
                stderr="fatal: not a git repository",
                returncode=128,
            )

            result = get_current_branch()

            assert result is None


class TestExtractNumberFromBranch:
    """Tests for extract_number_from_branch function."""

    def test_extract_issue_number(self):
        """Test extracting issue number from branch name."""
        assert extract_number_from_branch("issue-123") == 123
        assert extract_number_from_branch("issue-456") == 456

    def test_extract_pr_number(self):
        """Test extracting PR number from branch name."""
        assert extract_number_from_branch("pr-789") == 789
        assert extract_number_from_branch("pr-101") == 101

    def test_extract_number_with_prefix(self):
        """Test extracting number from branch with prefix."""
        assert extract_number_from_branch("feature/issue-123") == 123
        assert extract_number_from_branch("fix/pr-456") == 456

    def test_extract_number_case_insensitive(self):
        """Test case-insensitive extraction."""
        assert extract_number_from_branch("ISSUE-123") == 123
        assert extract_number_from_branch("PR-456") == 456
        assert extract_number_from_branch("Issue-789") == 789

    def test_extract_number_no_match(self):
        """Test extraction when no number pattern found."""
        assert extract_number_from_branch("main") is None
        assert extract_number_from_branch("feature-branch") is None
        assert extract_number_from_branch("develop") is None
        assert extract_number_from_branch("") is None
        assert extract_number_from_branch(None) is None
