"""Tests for git_utils module."""

import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.git_utils import (
    get_current_repo_name,
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
            mock_cmd.run_command.return_value = CommandResult(
                success=True, stdout="", stderr="", returncode=0
            )

            result = git_push()

            assert result.success is True
            call_args = mock_cmd.run_command.call_args
            assert call_args[0][0] == ["git", "push"]

    def test_push_with_branch(self):
        """Test push with specific branch."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(
                success=True, stdout="", stderr="", returncode=0
            )

            result = git_push(branch="feature-branch")

            assert result.success is True
            call_args = mock_cmd.run_command.call_args
            assert call_args[0][0] == ["git", "push", "origin", "feature-branch"]

    def test_push_with_custom_remote(self):
        """Test push with custom remote."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(
                success=True, stdout="", stderr="", returncode=0
            )

            result = git_push(remote="upstream", branch="main")

            assert result.success is True
            call_args = mock_cmd.run_command.call_args
            assert call_args[0][0] == ["git", "push", "upstream", "main"]

    def test_push_failure(self):
        """Test push failure."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(
                success=False,
                stdout="",
                stderr="error: failed to push some refs",
                returncode=1,
            )

            result = git_push()

            assert result.success is False
            assert "failed to push" in result.stderr

    def test_push_with_cwd(self):
        """Test push with custom working directory."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(
                success=True, stdout="", stderr="", returncode=0
            )

            result = git_push(cwd="/custom/path")

            assert result.success is True
            call_args = mock_cmd.run_command.call_args
            assert call_args[1]["cwd"] == "/custom/path"


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
