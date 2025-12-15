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
                CommandResult(
                    success=True,
                    stdout="Add new feature\nFix bug in parser\nInitial commit\n",
                    stderr="",
                    returncode=0,
                ),  # git log
            ]

            result = get_commit_log(base_branch="main")

            assert result == "Add new feature\nFix bug in parser\nInitial commit"
            assert mock_cmd.run_command.call_count == 2
            # Verify the log command
            mock_cmd.run_command.assert_called_with(["git", "log", "refs/remotes/origin/main..HEAD", "--max-count=50", "--pretty=format:%s"], cwd=None, stream_output=False)

    def test_get_commit_log_on_main_branch(self):
        """Test getting commit log when already on main branch."""
        with patch("src.auto_coder.git_info.get_current_branch") as mock_get_current_branch:
            mock_get_current_branch.return_value = "main"
            result = get_commit_log(base_branch="main")
            assert result == ""
            mock_get_current_branch.assert_called_once()

    def test_get_commit_log_fallback_to_local(self):
        """Test getting commit log falls back to local branch if remote fails."""
        with patch("src.auto_coder.git_info.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                CommandResult(success=True, stdout="feature-branch\n", stderr="", returncode=0),  # get_current_branch
                CommandResult(success=False, stdout="", stderr="", returncode=128),  # git log remote fails
                CommandResult(
                    success=True,
                    stdout="Local commit\n",
                    stderr="",
                    returncode=0,
                ),  # git log local succeeds
            ]

            result = get_commit_log(base_branch="main")

            assert result == "Local commit"
            assert mock_cmd.run_command.call_count == 3

    def test_get_commit_log_no_base_branch(self):
        """Test getting commit log when base branch doesn't exist."""
        with patch("src.auto_coder.git_info.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                CommandResult(success=True, stdout="feature-branch\n", stderr="", returncode=0),  # get_current_branch
                CommandResult(success=False, stdout="", stderr="", returncode=128),  # git log remote fails
                CommandResult(success=False, stdout="", stderr="", returncode=128),  # git log local fails
            ]

            result = get_commit_log(base_branch="nonexistent")

            assert result == ""
            assert mock_cmd.run_command.call_count == 3

    def test_get_commit_log_no_commits(self):
        """Test getting commit log when there are no new commits."""
        with patch("src.auto_coder.git_info.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                CommandResult(success=True, stdout="feature-branch\n", stderr="", returncode=0),  # get_current_branch
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git log (empty)
            ]

            result = get_commit_log(base_branch="main")

            assert result == ""
            assert mock_cmd.run_command.call_count == 2

    def test_get_commit_log_with_cwd(self):
        """Test getting commit log with custom working directory."""
        with patch("src.auto_coder.git_info.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                CommandResult(success=True, stdout="feature-branch\n", stderr="", returncode=0),  # get_current_branch
                CommandResult(
                    success=True,
                    stdout="Add new feature\n",
                    stderr="",
                    returncode=0,
                ),  # git log
            ]

            result = get_commit_log(base_branch="main", cwd="/custom/path")

            assert result == "Add new feature"
            # Check that cwd was passed to all calls
            for call in mock_cmd.run_command.call_args_list:
                assert call[1]["cwd"] == "/custom/path"

    def test_get_commit_log_max_commits(self):
        """Test getting commit log respects max_commits limit."""
        with patch("src.auto_coder.git_info.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                CommandResult(success=True, stdout="feature-branch\n", stderr="", returncode=0),  # get_current_branch
                CommandResult(
                    success=True,
                    stdout="Commit 5\nCommit 4\nCommit 3\nCommit 2\nCommit 1\n",
                    stderr="",
                    returncode=0,
                ),  # git log
            ]

            result = get_commit_log(base_branch="main", max_commits=5)

            assert result == "Commit 5\nCommit 4\nCommit 3\nCommit 2\nCommit 1"
            # Check that --max-count was passed
            log_call = mock_cmd.run_command.call_args_list[1][0][0]
            assert "--max-count=5" in log_call


class TestIsGitRepository:
    """Tests for is_git_repository function."""

    def test_is_git_repository_true(self):
        """Test is_git_repository returns True when in a git repository."""
        with patch("src.auto_coder.git_info.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(success=True, stdout="true\n", stderr="", returncode=0)

            assert is_git_repository() is True
            mock_cmd.run_command.assert_called_once_with(["git", "rev-parse", "--is-inside-work-tree"], cwd=os.getcwd(), stream_output=False)

    def test_is_git_repository_false(self):
        """Test is_git_repository returns False when not in a git repository."""
        with patch("src.auto_coder.git_info.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(success=False, stdout="", stderr="Not a git repository", returncode=128)

            assert is_git_repository() is False

    def test_is_git_repository_with_path(self):
        """Test is_git_repository with custom path."""
        with patch("src.auto_coder.git_info.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(success=True, stdout="true\n", stderr="", returncode=0)

            is_git_repository(path="/custom/path")
            mock_cmd.run_command.assert_called_once_with(["git", "rev-parse", "--is-inside-work-tree"], cwd="/custom/path", stream_output=False)


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
            mock_cmd.run_command.assert_called_once_with(["git", "remote", "get-url", "origin"], cwd=os.getcwd(), stream_output=False)

    def test_get_current_repo_name_failure(self):
        """Test get_current_repo_name when git command fails."""
        with patch("src.auto_coder.git_info.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(success=False, stdout="", stderr="No such remote 'origin'", returncode=1)

            result = get_current_repo_name()
            assert result is None
