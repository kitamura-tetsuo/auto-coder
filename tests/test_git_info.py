"Tests for git_info module."

import os
from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.git_info import get_commit_log, get_current_branch, get_current_repo_name, is_git_repository, parse_github_repo_from_url
from src.auto_coder.utils import CommandResult


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


@pytest.mark.usefixtures("_use_custom_subprocess_mock")
class TestGetCurrentBranch:
    """Tests for get_current_branch function."""

    def test_get_current_branch_success(self):
        """Test successful retrieval of current branch."""
        with patch("src.auto_coder.git_info.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(success=True, stdout="main\n", stderr="", returncode=0)

            result = get_current_branch()

            assert result == "main"
            mock_cmd.run_command.assert_called_once_with(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=None)

    def test_get_current_branch_with_cwd(self):
        """Test get_current_branch with custom working directory."""
        with patch("src.auto_coder.git_info.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(success=True, stdout="feature-branch\n", stderr="", returncode=0)

            result = get_current_branch(cwd="/path/to/repo")

            assert result == "feature-branch"
            mock_cmd.run_command.assert_called_once_with(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd="/path/to/repo")

    def test_get_current_branch_failure(self):
        """Test get_current_branch when git command fails."""
        with patch("src.auto_coder.git_info.CommandExecutor") as mock_executor:
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


@pytest.mark.usefixtures("_use_custom_subprocess_mock")
class TestGetCommitLog:
    """Tests for get_commit_log function."""

    def test_get_commit_log_with_commits(self):
        """Test getting commit log with multiple commits."""
        with patch("src.auto_coder.git_info.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                CommandResult(success=True, stdout="feature-branch\n", stderr="", returncode=0),  # get_current_branch
                CommandResult(success=True, stdout="abc123\n", stderr="", returncode=0),  # origin/main check
                CommandResult(success=True, stdout="def456\n", stderr="", returncode=0),  # merge-base
                CommandResult(
                    success=True,
                    stdout="Add new feature\nFix bug in parser\nInitial commit\n",
                    stderr="",
                    returncode=0,
                ),  # git log
            ]

            result = get_commit_log(base_branch="main")

            assert result == "Add new feature\nFix bug in parser\nInitial commit"
            assert mock_cmd.run_command.call_count == 4

    def test_get_commit_log_on_main_branch(self):
        """Test getting commit log when already on main branch."""
        with patch("src.auto_coder.git_info.get_current_branch") as mock_get_current_branch:
            mock_get_current_branch.return_value = "main"
            result = get_commit_log(base_branch="main")
            assert result == ""
            mock_get_current_branch.assert_called_once()

    def test_get_commit_log_no_base_branch(self):
        """Test getting commit log when base branch doesn't exist."""
        with patch("src.auto_coder.git_info.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                CommandResult(success=True, stdout="feature-branch\n", stderr="", returncode=0),  # get_current_branch
                CommandResult(success=False, stdout="", stderr="", returncode=0),  # origin/main check fails
                CommandResult(success=False, stdout="", stderr="", returncode=0),  # main check fails
            ]

            result = get_commit_log(base_branch="nonexistent")

            assert result == ""
            assert mock_cmd.run_command.call_count == 3


class TestIsGitRepository:
    """Tests for is_git_repository function."""

    def test_is_git_repository_true(self):
        """Test is_git_repository returns True when in a git repository."""
        with patch("src.auto_coder.git_info.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(success=True, stdout=".git\n", stderr="", returncode=0)

            assert is_git_repository() is True
            mock_cmd.run_command.assert_called_once()
            args, kwargs = mock_cmd.run_command.call_args
            assert args[0] == ["git", "rev-parse", "--git-dir"]

    def test_is_git_repository_false(self):
        """Test is_git_repository returns False when not in a git repository."""
        with patch("src.auto_coder.git_info.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(success=False, stdout="", stderr="fatal: not a git repository", returncode=128)

            assert is_git_repository() is False

    def test_is_git_repository_with_path(self):
        """Test is_git_repository with custom path."""
        with patch("src.auto_coder.git_info.CommandExecutor") as mock_executor, patch("os.path.exists", return_value=True):
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(success=True, stdout=".git\n", stderr="", returncode=0)

            is_git_repository(path="/custom/path")
            mock_cmd.run_command.assert_called_once_with(["git", "rev-parse", "--git-dir"], cwd="/custom/path")


class TestGetCurrentRepoName:
    """Tests for get_current_repo_name function."""

    def test_get_current_repo_name_success(self):
        """Test successful retrieval of repository name."""
        with patch("src.auto_coder.git_info.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(success=True, stdout="git@github.com:owner/repo.git\n", stderr="", returncode=0)

            result = get_current_repo_name()
            assert result == "owner/repo"
            mock_cmd.run_command.assert_called_once()
            args, kwargs = mock_cmd.run_command.call_args
            assert args[0] == ["git", "config", "--get", "remote.origin.url"]

    def test_get_current_repo_name_failure(self):
        """Test get_current_repo_name when git command fails."""
        with patch("src.auto_coder.git_info.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(success=False, stdout="", stderr="error", returncode=1)

            result = get_current_repo_name()
            assert result is None

    def test_get_current_repo_name_no_remote(self):
        """Test get_current_repo_name when no remote is configured."""
        with patch("src.auto_coder.git_info.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(success=False, stdout="", stderr="", returncode=1)

            result = get_current_repo_name()
            assert result is None
