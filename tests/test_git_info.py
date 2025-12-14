"Tests for git_info module."

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


@patch("src.auto_coder.git_info.GIT_AVAILABLE", True)
@patch("src.auto_coder.git_info.Repo")
class TestIsGitRepository:
    """Tests for is_git_repository function."""

    def test_is_git_repository_true(self, mock_repo):
        """Test is_git_repository returns True when in a git repository."""
        assert is_git_repository() is True
        mock_repo.assert_called_once()

    def test_is_git_repository_false(self, mock_repo):
        """Test is_git_repository returns False when not in a git repository."""
        from git import InvalidGitRepositoryError

        mock_repo.side_effect = InvalidGitRepositoryError
        assert is_git_repository() is False

    def test_is_git_repository_with_path(self, mock_repo):
        """Test is_git_repository with custom path."""
        is_git_repository(path="/custom/path")
        mock_repo.assert_called_once_with("/custom/path", search_parent_directories=True)


@patch("src.auto_coder.git_info.GIT_AVAILABLE", True)
@patch("src.auto_coder.git_info.Repo")
class TestGetCurrentRepoName:
    """Tests for get_current_repo_name function."""

    def test_get_current_repo_name_success(self, mock_repo):
        """Test successful retrieval of repository name."""
        mock_remotes = MagicMock()
        mock_origin = MagicMock()
        mock_origin.url = "git@github.com:owner/repo.git"
        mock_remotes.__contains__.return_value = True
        mock_remotes.origin = mock_origin
        mock_repo.return_value.remotes = mock_remotes

        result = get_current_repo_name()
        assert result == "owner/repo"

    def test_get_current_repo_name_failure(self, mock_repo):
        """Test get_current_repo_name when git command fails."""
        from git import InvalidGitRepositoryError

        mock_repo.side_effect = InvalidGitRepositoryError
        result = get_current_repo_name()
        assert result is None

    def test_get_current_repo_name_no_remote(self, mock_repo):
        """Test get_current_repo_name when no remote is configured."""
        mock_remotes = MagicMock()
        mock_remotes.__contains__.return_value = False
        mock_repo.return_value.remotes = mock_remotes
        result = get_current_repo_name()
        assert result is None
