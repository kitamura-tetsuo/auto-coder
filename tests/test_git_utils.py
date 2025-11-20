"""Tests for git_utils module."""

import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from auto_coder.git_utils import (
    branch_context,
    extract_number_from_branch,
    get_all_branches,
    get_branches_by_pattern,
    get_commit_log,
    get_current_branch,
    get_current_repo_name,
    git_checkout_branch,
    git_commit_with_retry,
    git_push,
    is_git_repository,
    migrate_pr_branches,
    parse_github_repo_from_url,
    save_commit_failure_history,
    validate_branch_name,
)
from auto_coder.utils import CommandResult


class TestGitCommitWithRetry:
    """Tests for git_commit_with_retry function."""

    def test_successful_commit(self):
        """Test successful commit without retry."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
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
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # dprint fmt
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git add
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # commit retry
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

    def test_commit_with_unknown_error_llm_fallback_and_retry_success(self):
        """Unknown commit error triggers LLM fallback and retry commit succeeds."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor, patch("src.auto_coder.git_utils.try_llm_commit_push") as mock_llm:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_llm.return_value = True
            mock_cmd.run_command.side_effect = [
                CommandResult(success=False, stdout="", stderr="pre-commit hook failed", returncode=1),
                CommandResult(success=True, stdout="", stderr="", returncode=0),
            ]

            result = git_commit_with_retry("Test commit message")

            assert result.success is True
            assert mock_llm.call_count == 1
            assert mock_cmd.run_command.call_count == 2
            # second call should be retry commit
            assert mock_cmd.run_command.call_args_list[1][0][0] == ["git", "commit", "-m", "Test commit message"]

    def test_commit_with_unknown_error_llm_fallback_nothing_to_commit(self):
        """Unknown commit error triggers LLM fallback; treat as success when nothing left to commit."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor, patch("src.auto_coder.git_utils.try_llm_commit_push") as mock_llm:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_llm.return_value = True
            mock_cmd.run_command.side_effect = [
                CommandResult(success=False, stdout="", stderr="some unknown error", returncode=1),
                CommandResult(success=False, stdout="", stderr="nothing to commit, working tree clean", returncode=1),
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git status --porcelain -> clean
            ]

            result = git_commit_with_retry("Test commit message")

            assert result.success is True
            assert mock_llm.call_count == 1
            assert mock_cmd.run_command.call_count == 3
            assert mock_cmd.run_command.call_args_list[2][0][0] == ["git", "status", "--porcelain"]

    def test_commit_with_cwd(self):
        """Test commit with custom working directory."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(success=True, stdout="", stderr="", returncode=0)

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
                # First call in check_unpushed_commits: get current branch
                CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
                # Second call in check_unpushed_commits: check unpushed commits
                CommandResult(success=True, stdout="2\n", stderr="", returncode=0),  # 2 unpushed commits
                # Third call in git_push: get current branch
                CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
                # Fourth call: push
                CommandResult(success=True, stdout="", stderr="", returncode=0),
            ]

            result = git_push()

            assert result.success is True
            assert mock_cmd.run_command.call_count == 4
            # Check the last call was the push command
            last_call_args = mock_cmd.run_command.call_args_list[3][0][0]
            assert last_call_args == ["git", "push", "origin", "main"]

    def test_push_with_branch(self):
        """Test push with specific branch."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            # When branch is specified, no need to get current branch
            mock_cmd.run_command.return_value = CommandResult(success=True, stdout="", stderr="", returncode=0)

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
            mock_cmd.run_command.return_value = CommandResult(success=True, stdout="", stderr="", returncode=0)

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
                # 1) check_unpushed_commits: get current branch
                CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
                # 2) check_unpushed_commits: found unpushed commits
                CommandResult(success=True, stdout="2\n", stderr="", returncode=0),
                # 3) _perform_git_push: get current branch for push
                CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
                # 4) _perform_git_push: push fails
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
                # 1) check_unpushed_commits: get current branch
                CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
                # 2) check_unpushed_commits: found unpushed commits
                CommandResult(success=True, stdout="2\n", stderr="", returncode=0),
                # 3) _perform_git_push: get current branch for push
                CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
                # 4) _perform_git_push: push succeeds
                CommandResult(success=True, stdout="", stderr="", returncode=0),
            ]

            result = git_push(cwd="/custom/path")

            assert result.success is True
            assert mock_cmd.run_command.call_count == 4
            # Check that cwd was passed to all calls
            assert mock_cmd.run_command.call_args_list[0][1]["cwd"] == "/custom/path"
            assert mock_cmd.run_command.call_args_list[1][1]["cwd"] == "/custom/path"
            assert mock_cmd.run_command.call_args_list[2][1]["cwd"] == "/custom/path"
            assert mock_cmd.run_command.call_args_list[3][1]["cwd"] == "/custom/path"

    def test_push_no_upstream_auto_retry(self):
        """Test push automatically retries with --set-upstream when upstream is not set."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                # 1) check_unpushed_commits: get current branch
                CommandResult(success=True, stdout="issue-733\n", stderr="", returncode=0),
                # 2) check_unpushed_commits: found unpushed commits
                CommandResult(success=True, stdout="2\n", stderr="", returncode=0),
                # 3) _perform_git_push: get current branch for push
                CommandResult(success=True, stdout="issue-733\n", stderr="", returncode=0),
                # 4) _perform_git_push: push fails with no upstream error
                CommandResult(
                    success=False,
                    stdout="",
                    stderr="fatal: The current branch issue-733 has no upstream branch.\nTo push the current branch and set the remote as upstream, use\n\n    git push --set-upstream origin issue-733\n",
                    returncode=1,
                ),
                # 5) _retry_with_set_upstream: resolve current branch
                CommandResult(success=True, stdout="issue-733\n", stderr="", returncode=0),
                # 6) push with --set-upstream succeeds
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
                    stderr="fatal: The current branch feature-branch has no upstream branch.\nTo push the current branch and set the remote as upstream, use\n\n    git push --set-upstream origin feature-branch\n",
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
                # 1) check_unpushed_commits: get current branch
                CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
                # 2) check_unpushed_commits: found unpushed commits
                CommandResult(success=True, stdout="2\n", stderr="", returncode=0),
                # 3) _perform_git_push: get current branch for push
                CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
                # 4) _perform_git_push: push fails with different error
                CommandResult(
                    success=False,
                    stdout="",
                    stderr="error: failed to push some refs to 'origin'",
                    returncode=1,
                ),
            ]

            result = git_push()

            assert result.success is False
            assert mock_cmd.run_command.call_count == 4  # No retry
            assert "failed to push some refs" in result.stderr

    def test_push_with_dprint_error_and_retry(self):
        """Test push with dprint formatting error triggers retry without commit message."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                # 1) check_unpushed_commits: get current branch
                CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
                # 2) check_unpushed_commits: found unpushed commits
                CommandResult(success=True, stdout="2\n", stderr="", returncode=0),
                # 3) _perform_git_push: get current branch for push
                CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
                # 4) _perform_git_push: push fails with dprint error
                CommandResult(
                    success=False,
                    stdout="",
                    stderr="You may want to try using `dprint output-file-paths` to see which files it's finding",
                    returncode=1,
                ),
                # 5) dprint fmt succeeds
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # 6) git add -A succeeds
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # 7) push retry succeeds
                CommandResult(success=True, stdout="", stderr="", returncode=0),
            ]

            result = git_push()

            assert result.success is True
            assert mock_cmd.run_command.call_count == 7
            # Check that dprint fmt was called
            calls = mock_cmd.run_command.call_args_list
            assert calls[4][0][0] == ["npx", "dprint", "fmt"]
            assert calls[5][0][0] == ["git", "add", "-A"]
            assert calls[6][0][0] == ["git", "push"]

    def test_push_with_dprint_error_and_commit_message(self):
        """Test push with dprint formatting error and commit message triggers re-commit."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                # 1) check_unpushed_commits: get current branch
                CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
                # 2) check_unpushed_commits: found unpushed commits
                CommandResult(success=True, stdout="2\n", stderr="", returncode=0),
                # 3) _perform_git_push: get current branch for push
                CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
                # 4) _perform_git_push: push fails with dprint error
                CommandResult(
                    success=False,
                    stdout="",
                    stderr="You may want to try using `dprint output-file-paths` to see which files it's finding",
                    returncode=1,
                ),
                # 5) dprint fmt succeeds
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # 6) git add -A succeeds
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # 7) git commit --amend --no-edit succeeds
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # 8) push retry succeeds
                CommandResult(success=True, stdout="", stderr="", returncode=0),
            ]

            result = git_push(commit_message="Fix: automated changes")

            assert result.success is True
            assert mock_cmd.run_command.call_count == 8
            # Check that dprint fmt was called
            calls = mock_cmd.run_command.call_args_list
            assert calls[4][0][0] == ["npx", "dprint", "fmt"]
            assert calls[5][0][0] == ["git", "add", "-A"]
            assert calls[6][0][0] == ["git", "commit", "--amend", "--no-edit"]
            assert calls[7][0][0] == ["git", "push"]

    def test_push_with_dprint_error_fmt_fails(self):
        """Test push with dprint error but formatter fails."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                # 1) check_unpushed_commits: get current branch
                CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
                # 2) check_unpushed_commits: found unpushed commits
                CommandResult(success=True, stdout="2\n", stderr="", returncode=0),
                # 3) _perform_git_push: get current branch for push
                CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
                # 4) _perform_git_push: push fails with dprint error
                CommandResult(
                    success=False,
                    stdout="",
                    stderr="dprint output-file-paths error detected",
                    returncode=1,
                ),
                # 5) dprint fmt fails
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
            assert mock_cmd.run_command.call_count == 5

    def test_push_with_dprint_error_commit_amend_fails(self):
        """Test push with dprint error when commit amend fails, falls back to regular commit."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                # 1) check_unpushed_commits: get current branch
                CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
                # 2) check_unpushed_commits: found unpushed commits
                CommandResult(success=True, stdout="2\n", stderr="", returncode=0),
                # 3) _perform_git_push: get current branch for push
                CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
                # 4) _perform_git_push: push fails with dprint error
                CommandResult(
                    success=False,
                    stdout="",
                    stderr="dprint output-file-paths error detected",
                    returncode=1,
                ),
                # 5) dprint fmt succeeds
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # 6) git add -A succeeds
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # 7) git commit --amend --no-edit fails
                CommandResult(
                    success=False,
                    stdout="",
                    stderr="fatal: You are in the middle of a merge -- cannot amend.",
                    returncode=1,
                ),
                # 8) git commit -m succeeds
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # 9) push retry succeeds
                CommandResult(success=True, stdout="", stderr="", returncode=0),
            ]

            result = git_push(commit_message="Fix: automated changes")

            assert result.success is True
            assert mock_cmd.run_command.call_count == 9
            # Check that regular commit was called after amend failed
            calls = mock_cmd.run_command.call_args_list
            assert calls[6][0][0] == ["git", "commit", "--amend", "--no-edit"]
            assert calls[7][0][0] == ["git", "commit", "-m", "Fix: automated changes"]
            assert calls[8][0][0] == ["git", "push"]

    def test_push_with_dprint_error_and_upstream_retry(self):
        """Test push with dprint error and then upstream error triggers both retries."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                # 1) check_unpushed_commits: get current branch
                CommandResult(success=True, stdout="feature\n", stderr="", returncode=0),
                # 2) check_unpushed_commits: found unpushed commits
                CommandResult(success=True, stdout="2\n", stderr="", returncode=0),
                # 3) _perform_git_push: get current branch for push
                CommandResult(success=True, stdout="feature\n", stderr="", returncode=0),
                # 4) _perform_git_push: push fails with dprint error
                CommandResult(
                    success=False,
                    stdout="",
                    stderr="You may want to try using `dprint output-file-paths` to see which files it's finding",
                    returncode=1,
                ),
                # 5) dprint fmt succeeds
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # 6) git add -A succeeds
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # 7) push retry fails with upstream error
                CommandResult(
                    success=False,
                    stdout="",
                    stderr="fatal: The current branch feature has no upstream branch.\nTo push the current branch and set the remote as upstream, use\n\n    git push --set-upstream origin feature\n",
                    returncode=1,
                ),
                # 8) _retry_with_set_upstream: resolve current branch
                CommandResult(success=True, stdout="feature\n", stderr="", returncode=0),
                # 9) push with --set-upstream succeeds
                CommandResult(success=True, stdout="", stderr="", returncode=0),
            ]

            result = git_push()

            assert result.success is True
            assert mock_cmd.run_command.call_count == 9
            # Check the final call used --set-upstream
            final_call_args = mock_cmd.run_command.call_args_list[8][0][0]
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
                CommandResult(success=True, stdout="feature\n", stderr="", returncode=0),
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
        """Test successful checkout with creating a new branch from origin/main."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                # First call: git branch --list (branch doesn't exist)
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Second call: git status --porcelain (has changes, needs commit)
                CommandResult(success=True, stdout="M  test.py", stderr="", returncode=0),
                # Third call: git add -A (from git_checkout_branch)
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Fourth call: git commit (from git_commit_with_retry)
                CommandResult(
                    success=True,
                    stdout="WIP: Auto-commit before branch checkout\n",
                    stderr="",
                    returncode=0,
                ),
                # Fifth call: git fetch origin --prune --tags
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Sixth call: verify origin/main exists
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Seventh call: git checkout -B new-feature origin/main
                CommandResult(
                    success=True,
                    stdout="Switched to a new branch 'new-feature'\n",
                    stderr="",
                    returncode=0,
                ),
                # Eighth call: verify current branch (git rev-parse --abbrev-ref HEAD)
                CommandResult(success=True, stdout="new-feature\n", stderr="", returncode=0),
                # Ninth call: git push -u origin new-feature
                CommandResult(
                    success=True,
                    stdout="Branch 'new-feature' set up to track remote branch 'new-feature' from 'origin'.\n",
                    stderr="",
                    returncode=0,
                ),
            ]

            result = git_checkout_branch("new-feature", create_new=True, base_branch="main")

            assert result.success is True
            assert mock_cmd.run_command.call_count == 9
            # Verify fetch and base resolution
            assert mock_cmd.run_command.call_args_list[4][0][0] == [
                "git",
                "fetch",
                "origin",
                "--prune",
                "--tags",
            ]
            assert mock_cmd.run_command.call_args_list[5][0][0] == [
                "git",
                "rev-parse",
                "--verify",
                "refs/remotes/origin/main",
            ]
            # Verify checkout command with -B flag and base ref (now at index 6)
            assert mock_cmd.run_command.call_args_list[6][0][0] == [
                "git",
                "checkout",
                "-B",
                "new-feature",
                "refs/remotes/origin/main",
            ]
            # Verify push command (now at index 8)
            assert mock_cmd.run_command.call_args_list[8][0][0] == [
                "git",
                "push",
                "-u",
                "origin",
                "new-feature",
            ]

    def test_successful_checkout_create_from_base_branch(self):
        """Test successful checkout with creating a new branch from base branch with base ref included."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                # First call: git branch --list (branch doesn't exist)
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Second call: git status --porcelain (no changes)
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Third call: git fetch origin --prune --tags
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Fourth call: verify origin/main exists
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Fifth call: git checkout -B new-feature origin/main
                CommandResult(
                    success=True,
                    stdout="Switched to branch 'new-feature'\n",
                    stderr="",
                    returncode=0,
                ),
                # Sixth call: verify current branch
                CommandResult(success=True, stdout="new-feature\n", stderr="", returncode=0),
                # Seventh call: git push -u origin new-feature
                CommandResult(
                    success=True,
                    stdout="Branch 'new-feature' set up to track remote branch 'new-feature' from 'origin'.\n",
                    stderr="",
                    returncode=0,
                ),
            ]

            result = git_checkout_branch("new-feature", create_new=True, base_branch="main")

            assert result.success is True
            assert mock_cmd.run_command.call_count == 7
            # Verify fetch and base resolution
            assert mock_cmd.run_command.call_args_list[2][0][0] == [
                "git",
                "fetch",
                "origin",
                "--prune",
                "--tags",
            ]
            assert mock_cmd.run_command.call_args_list[3][0][0] == [
                "git",
                "rev-parse",
                "--verify",
                "refs/remotes/origin/main",
            ]
            # Verify checkout command with -B flag and base ref (now at index 4)
            assert mock_cmd.run_command.call_args_list[4][0][0] == [
                "git",
                "checkout",
                "-B",
                "new-feature",
                "refs/remotes/origin/main",
            ]
            # Verify push command (now at index 6)
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
                    stderr="error: pathspec 'nonexistent' did not match any file(s) known to git",
                    returncode=1,
                ),
            ]

            result = git_checkout_branch("nonexistent")

            assert result.success is False
            assert "pathspec 'nonexistent' did not match" in result.stderr
            # Should call status and checkout, not verification
            assert mock_cmd.run_command.call_count == 2

    def test_create_new_branch_requires_base_branch(self):
        """Creating a new branch without base_branch should raise ValueError."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            # First call for branch list (branch doesn't exist)
            mock_cmd.run_command.return_value = CommandResult(success=True, stdout="", stderr="", returncode=0)

            with pytest.raises(ValueError):
                git_checkout_branch("new-feature", create_new=True)

    def test_create_new_branch_fallback_to_local_base(self):
        """When origin/<base_branch> is missing, fall back to local <base_branch>."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                # 1) branch list (branch doesn't exist)
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # 2) status
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # 3) fetch origin
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # 4) rev-parse origin/main fails
                CommandResult(success=False, stdout="", stderr="fatal: bad revision 'origin/main'", returncode=128),
                # 5) rev-parse main succeeds
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # 6) checkout -B new-feature main
                CommandResult(success=True, stdout="Switched to branch 'new-feature'\n", stderr="", returncode=0),
                # 7) verify current branch
                CommandResult(success=True, stdout="new-feature\n", stderr="", returncode=0),
                # 8) push
                CommandResult(success=True, stdout="", stderr="", returncode=0),
            ]

            result = git_checkout_branch("new-feature", create_new=True, base_branch="main")

            assert result.success is True
            assert mock_cmd.run_command.call_count == 8
            # Ensure we attempted origin first and then fell back to local
            assert mock_cmd.run_command.call_args_list[3][0][0] == [
                "git",
                "rev-parse",
                "--verify",
                "refs/remotes/origin/main",
            ]
            assert mock_cmd.run_command.call_args_list[4][0][0] == [
                "git",
                "rev-parse",
                "--verify",
                "main",
            ]
            assert mock_cmd.run_command.call_args_list[5][0][0] == [
                "git",
                "checkout",
                "-B",
                "new-feature",
                "main",
            ]

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
                CommandResult(success=True, stdout="feature\n", stderr="", returncode=0),
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
                # First call: git branch --list (branch doesn't exist)
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Second call: git status --porcelain (no changes)
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Third call: git fetch origin --prune --tags
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Fourth call: verify origin/main exists
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Fifth call: git checkout -B new-feature origin/main
                CommandResult(
                    success=True,
                    stdout="Switched to a new branch 'new-feature'\n",
                    stderr="",
                    returncode=0,
                ),
                # Sixth call: verify current branch
                CommandResult(success=True, stdout="new-feature\n", stderr="", returncode=0),
                # Seventh call: git push fails
                CommandResult(
                    success=False,
                    stdout="",
                    stderr="fatal: unable to access remote",
                    returncode=1,
                ),
            ]

            result = git_checkout_branch("new-feature", create_new=True, base_branch="main")

            # Should still succeed even if push fails
            assert result.success is True
            assert mock_cmd.run_command.call_count == 7

    def test_create_new_branch_without_publish(self):
        """Test creating a new branch without publishing to remote."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                # First call: git branch --list (branch doesn't exist)
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Second call: git status --porcelain (no changes)
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Third call: git fetch origin --prune --tags
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Fourth call: verify origin/main exists
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Fifth call: git checkout -B
                CommandResult(
                    success=True,
                    stdout="Switched to a new branch 'new-feature'\n",
                    stderr="",
                    returncode=0,
                ),
                # Sixth call: verify current branch
                CommandResult(success=True, stdout="new-feature\n", stderr="", returncode=0),
            ]

            result = git_checkout_branch("new-feature", create_new=True, base_branch="main", publish=False)

            assert result.success is True
            # Should have 6 calls (branch list, status, fetch, verify base, checkout, verify), no push
            assert mock_cmd.run_command.call_count == 6

    def test_checkout_with_uncommitted_changes_auto_commit(self):
        """Test checkout with uncommitted changes automatically commits them."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                # First call: git status --porcelain (has changes)
                CommandResult(success=True, stdout=" M file.txt\n", stderr="", returncode=0),
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
                CommandResult(success=True, stdout="feature\n", stderr="", returncode=0),
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
                    stderr="error: Your local changes to the following files would be overwritten by checkout:\n\tfile.txt\nPlease commit your changes or stash them before you switch branches.\nAborting",
                    returncode=1,
                ),
                # Third call: git add -A
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Fourth call: git commit (from git_commit_with_retry)
                CommandResult(
                    success=True,
                    stdout="[main abc123] WIP: Auto-commit before branch checkout (retry)\n",
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
                CommandResult(success=True, stdout="feature\n", stderr="", returncode=0),
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
                    stderr="error: Your local changes to the following files would be overwritten by checkout:\n\tfile.txt",
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

    def test_checkout_rejects_invalid_pr_branch_name_when_creating(self):
        """Test that creating new branch with pr-<number> pattern is rejected."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            # Mock that branch doesn't exist
            mock_cmd.run_command.return_value = Mock(success=True, stdout="")

            result = git_checkout_branch("pr-123", create_new=True, base_branch="main")

            assert result.success is False
            assert "prohibited pattern 'pr-<number>'" in result.stderr
            assert "issue-123" in result.stderr
            # Should not have called any git commands except branch list
            mock_cmd.run_command.assert_called_once()

    def test_checkout_accepts_existing_pr_branch(self):
        """Test that checking out existing pr-<number> branch is allowed when not creating a new branch."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            # When create_new=False, no validation is performed; allow pr-<number>
            mock_cmd.run_command.side_effect = [
                # First call: git status --porcelain (no changes)
                CommandResult(success=True, stdout="", stderr="", returncode=0),
                # Second call: git checkout (switch to existing branch)
                CommandResult(success=True, stdout="Switched to branch 'pr-123'\n", stderr="", returncode=0),
                # Third call: git rev-parse --abbrev-ref HEAD (verify current branch)
                CommandResult(success=True, stdout="pr-123\n", stderr="", returncode=0),
            ]

            result = git_checkout_branch("pr-123", create_new=False)

            assert result.success is True
            assert mock_cmd.run_command.call_count == 3

    def test_checkout_rejects_invalid_pr_branch_name_case_insensitive(self):
        """Test that creating pr-<number> pattern is rejected regardless of case."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            # Mock that branch doesn't exist
            mock_cmd.run_command.return_value = Mock(success=True, stdout="")

            result = git_checkout_branch("PR-456", create_new=True, base_branch="main")

            assert result.success is False
            assert "prohibited pattern 'pr-<number>'" in result.stderr
            # Should not have called any git commands except branch list
            mock_cmd.run_command.assert_called_once()


class TestGetCurrentBranch:
    """Tests for get_current_branch function."""

    def test_get_current_branch_success(self):
        """Test successful retrieval of current branch."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(success=True, stdout="main\n", stderr="", returncode=0)

            result = get_current_branch()

            assert result == "main"
            mock_cmd.run_command.assert_called_once_with(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=None)

    def test_get_current_branch_with_cwd(self):
        """Test get_current_branch with custom working directory."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(success=True, stdout="feature-branch\n", stderr="", returncode=0)

            result = get_current_branch(cwd="/path/to/repo")

            assert result == "feature-branch"
            mock_cmd.run_command.assert_called_once_with(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd="/path/to/repo")

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


class TestValidateBranchName:
    """Tests for validate_branch_name function."""

    def test_validate_branch_name_valid_issue_pattern(self):
        """Test that issue-<number> pattern is valid."""
        validate_branch_name("issue-123")
        validate_branch_name("issue-456")
        validate_branch_name("feature/issue-789")  # With prefix
        validate_branch_name("main")
        validate_branch_name("feature-branch")
        validate_branch_name("develop")
        validate_branch_name("")  # Empty string should be valid

    def test_validate_branch_name_invalid_pr_pattern(self):
        """Test that pr-<number> pattern raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            validate_branch_name("pr-123")
        assert "prohibited pattern 'pr-<number>'" in str(exc_info.value)
        assert "issue-<number>" in str(exc_info.value)

        with pytest.raises(ValueError) as exc_info:
            validate_branch_name("PR-456")
        assert "prohibited pattern 'pr-<number>'" in str(exc_info.value)

        with pytest.raises(ValueError) as exc_info:
            validate_branch_name("pr-789")
        assert "prohibited pattern 'pr-<number>'" in str(exc_info.value)

    def test_validate_branch_name_case_insensitive(self):
        """Test that validation is case-insensitive for pr- pattern."""
        with pytest.raises(ValueError):
            validate_branch_name("PR-123")

        with pytest.raises(ValueError):
            validate_branch_name("Pr-456")

    def test_validate_branch_name_suggestion_message(self):
        """Test that error message provides helpful suggestion."""
        with pytest.raises(ValueError) as exc_info:
            validate_branch_name("pr-123")
        error_msg = str(exc_info.value)
        assert "pr-123" in error_msg
        assert "issue-123" in error_msg


class TestGetCommitLog:
    """Tests for get_commit_log function."""

    def test_get_commit_log_with_commits(self):
        """Test getting commit log with multiple commits."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            # Mock responses: get_current_branch, origin/main check (succeeds), merge-base, git log
            # Note: second base check (without 'origin/') is skipped when first succeeds
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
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(success=True, stdout="main\n", stderr="", returncode=0)

            result = get_commit_log(base_branch="main")

            assert result == ""
            # Should only call get_current_branch
            assert mock_cmd.run_command.call_count == 1

    def test_get_commit_log_no_base_branch(self):
        """Test getting commit log when base branch doesn't exist."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            # origin/main fails, then main fails (second check is attempted)
            mock_cmd.run_command.side_effect = [
                CommandResult(success=True, stdout="feature-branch\n", stderr="", returncode=0),  # get_current_branch
                CommandResult(success=False, stdout="", stderr="", returncode=0),  # origin/main check fails
                CommandResult(success=False, stdout="", stderr="", returncode=0),  # main check fails
            ]

            result = get_commit_log(base_branch="nonexistent")

            assert result == ""
            assert mock_cmd.run_command.call_count == 3

    def test_get_commit_log_no_merge_base(self):
        """Test getting commit log when merge base cannot be found."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                CommandResult(success=True, stdout="feature-branch\n", stderr="", returncode=0),  # get_current_branch
                CommandResult(success=True, stdout="abc123\n", stderr="", returncode=0),  # origin/main check
                CommandResult(success=False, stdout="", stderr="fatal: ambiguous argument", returncode=128),  # merge-base fails
            ]

            result = get_commit_log(base_branch="main")

            assert result == ""
            assert mock_cmd.run_command.call_count == 3

    def test_get_commit_log_no_commits(self):
        """Test getting commit log when there are no new commits."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                CommandResult(success=True, stdout="feature-branch\n", stderr="", returncode=0),  # get_current_branch
                CommandResult(success=True, stdout="abc123\n", stderr="", returncode=0),  # origin/main check
                CommandResult(success=True, stdout="def456\n", stderr="", returncode=0),  # merge-base
                CommandResult(success=True, stdout="", stderr="", returncode=0),  # git log (empty)
            ]

            result = get_commit_log(base_branch="main")

            assert result == ""
            assert mock_cmd.run_command.call_count == 4

    def test_get_commit_log_with_cwd(self):
        """Test getting commit log with custom working directory."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                CommandResult(success=True, stdout="feature-branch\n", stderr="", returncode=0),  # get_current_branch
                CommandResult(success=True, stdout="abc123\n", stderr="", returncode=0),  # origin/main check
                CommandResult(success=True, stdout="def456\n", stderr="", returncode=0),  # merge-base
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
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                CommandResult(success=True, stdout="feature-branch\n", stderr="", returncode=0),  # get_current_branch
                CommandResult(success=True, stdout="abc123\n", stderr="", returncode=0),  # origin/main check
                CommandResult(success=True, stdout="def456\n", stderr="", returncode=0),  # merge-base
                CommandResult(success=True, stdout="Commit 5\nCommit 4\nCommit 3\nCommit 2\nCommit 1\n", stderr="", returncode=0),  # git log
            ]

            result = get_commit_log(base_branch="main", max_commits=5)

            assert result == "Commit 5\nCommit 4\nCommit 3\nCommit 2\nCommit 1"
            # Check that --max-count was passed
            log_call = mock_cmd.run_command.call_args_list[3][0][0]
            assert "--max-count=5" in log_call


class TestGetAllBranches:
    """Tests for get_all_branches function."""

    def test_get_all_branches_local(self):
        """Test getting all local branches."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(
                success=True,
                stdout="main\nfeature-branch\nissue-123\n",
                stderr="",
                returncode=0,
            )

            result = get_all_branches(remote=False)

            assert result == ["main", "feature-branch", "issue-123"]
            mock_cmd.run_command.assert_called_once_with(["git", "branch", "--format=%(refname:short)"], cwd=None)

    def test_get_all_branches_remote(self):
        """Test getting all remote branches."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(
                success=True,
                stdout="origin/main\norigin/feature-branch\norigin/issue-123\n",
                stderr="",
                returncode=0,
            )

            result = get_all_branches(remote=True)

            assert result == ["origin/main", "origin/feature-branch", "origin/issue-123"]
            mock_cmd.run_command.assert_called_once_with(["git", "branch", "-r", "--format=%(refname:short)"], cwd=None)

    def test_get_all_branches_empty(self):
        """Test getting branches when none exist."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(success=True, stdout="", stderr="", returncode=0)

            result = get_all_branches()

            assert result == []

    def test_get_all_branches_with_cwd(self):
        """Test getting branches with custom working directory."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(
                success=True,
                stdout="main\n",
                stderr="",
                returncode=0,
            )

            result = get_all_branches(cwd="/custom/path")

            assert result == ["main"]
            mock_cmd.run_command.assert_called_once_with(["git", "branch", "-r", "--format=%(refname:short)"], cwd="/custom/path")

    def test_get_all_branches_failure(self):
        """Test getting branches when git command fails."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(
                success=False,
                stdout="",
                stderr="fatal: not a git repository",
                returncode=128,
            )

            result = get_all_branches()

            assert result == []


class TestGetBranchesByPattern:
    """Tests for get_branches_by_pattern function."""

    def test_get_branches_by_pattern_with_wildcard(self):
        """Test getting branches matching a pattern with wildcard."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            # get_all_branches is called internally
            mock_cmd.run_command.return_value = CommandResult(
                success=True,
                stdout="main\nissue-123\nissue-456\npr-789\nfeature-branch\n",
                stderr="",
                returncode=0,
            )

            result = get_branches_by_pattern("issue-*")

            assert result == ["issue-123", "issue-456"]
            mock_cmd.run_command.assert_called_once()

    def test_get_branches_by_pattern_exact_match(self):
        """Test getting branches with exact match pattern."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(
                success=True,
                stdout="main\nissue-123\n",
                stderr="",
                returncode=0,
            )

            result = get_branches_by_pattern("main")

            assert result == ["main"]
            mock_cmd.run_command.assert_called_once()

    def test_get_branches_by_pattern_case_insensitive(self):
        """Test case-insensitive pattern matching."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(
                success=True,
                stdout="main\nISSUE-123\nIssue-456\n",
                stderr="",
                returncode=0,
            )

            result = get_branches_by_pattern("issue-*")

            assert result == ["ISSUE-123", "Issue-456"]
            mock_cmd.run_command.assert_called_once()

    def test_get_branches_by_pattern_no_matches(self):
        """Test pattern matching when no branches match."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(
                success=True,
                stdout="main\ndevelop\n",
                stderr="",
                returncode=0,
            )

            result = get_branches_by_pattern("issue-*")

            assert result == []
            mock_cmd.run_command.assert_called_once()

    def test_get_branches_by_pattern_with_remote_prefix(self):
        """Test pattern matching with remote branch prefix."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(
                success=True,
                stdout="origin/main\norigin/issue-123\norigin/pr-456\n",
                stderr="",
                returncode=0,
            )

            result = get_branches_by_pattern("pr-*")

            assert result == ["origin/pr-456"]
            mock_cmd.run_command.assert_called_once()

    def test_get_branches_by_pattern_local_only(self):
        """Test pattern matching in local branches only."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            # Mock for local branches (no -r flag)
            mock_cmd.run_command.return_value = CommandResult(
                success=True,
                stdout="main\nissue-123\n",
                stderr="",
                returncode=0,
            )

            result = get_branches_by_pattern("issue-*", remote=False)

            assert result == ["issue-123"]
            # Verify that local branch command was used
            call_args = mock_cmd.run_command.call_args[0][0]
            assert "-r" not in call_args
            mock_cmd.run_command.assert_called_once()


class TestMigratePrBranches:
    """Tests for migrate_pr_branches function."""

    def test_migrate_pr_branches_no_pr_branches(self):
        """Test migration when no pr-<number> branches exist."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            # git branch -r (get_all_branches) - no pr-* branches
            mock_cmd.run_command.return_value = CommandResult(
                success=True,
                stdout="origin/main\norigin/issue-123\norigin/feature-branch\n",
                stderr="",
                returncode=0,
            )

            from auto_coder.automation_config import AutomationConfig

            config = AutomationConfig()
            results = migrate_pr_branches(config, delete_after_merge=True)

            assert results["success"] is True
            assert len(results["migrated"]) == 0
            assert len(results["skipped"]) == 0
            assert len(results["failed"]) == 0
            assert len(results["conflicts"]) == 0
            # No git commands should be called beyond get_branches_by_pattern
            mock_cmd.run_command.assert_called_once()

    def test_migrate_pr_branches_multiple_branches(self, _use_custom_subprocess_mock):
        """Test migration with multiple pr-<number> branches."""
        from types import SimpleNamespace

        from auto_coder.utils import CommandResult

        call_count = [0]  # Use list to make it mutable in closure

        def fake_run_command(cmd, **kwargs):
            """Fake CommandExecutor.run_command that returns appropriate responses."""
            call_idx = call_count[0]
            call_count[0] += 1

            # Handle different git commands
            if cmd == ["git", "branch", "--format=%(refname:short)"]:
                return CommandResult(
                    success=True,
                    stdout="pr-123\npr-456\npr-789\n",
                    stderr="",
                    returncode=0,
                )
            elif cmd == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
                # Determine which branch based on call count
                # Call 1: initial check (main)
                # Call 5: after checkout issue-123
                # Call 8: after merge pr-123 (still issue-123)
                # Call 12: before processing pr-456 (should be issue-123)
                # Call 16: after checkout issue-456
                # Call 19: after merge pr-456 (still issue-456)
                # Call 23: before processing pr-789 (should be issue-456)
                # Call 27: after checkout issue-789
                if call_idx == 1:
                    branch = "main\n"
                elif call_idx in (5, 8, 12):
                    branch = "issue-123\n"
                elif call_idx in (16, 19, 23):
                    branch = "issue-456\n"
                elif call_idx == 27:
                    branch = "issue-789\n"
                else:
                    branch = "main\n"  # Default
                return CommandResult(
                    success=True,
                    stdout=branch,
                    stderr="",
                    returncode=0,
                )
            elif cmd == ["git", "branch", "--list", "issue-123"]:
                return CommandResult(
                    success=True,
                    stdout="issue-123",
                    stderr="",
                    returncode=0,
                )
            elif cmd == ["git", "branch", "--list", "issue-456"]:
                return CommandResult(
                    success=True,
                    stdout="issue-456",
                    stderr="",
                    returncode=0,
                )
            elif cmd == ["git", "branch", "--list", "issue-789"]:
                return CommandResult(
                    success=True,
                    stdout="issue-789",
                    stderr="",
                    returncode=0,
                )
            elif cmd == ["git", "status", "--porcelain"]:
                return CommandResult(
                    success=True,
                    stdout="",
                    stderr="",
                    returncode=0,
                )
            elif cmd == ["git", "checkout", "issue-123"]:
                return CommandResult(
                    success=True,
                    stdout="Switched to branch 'issue-123'",
                    stderr="",
                    returncode=0,
                )
            elif cmd == ["git", "checkout", "issue-456"]:
                return CommandResult(
                    success=True,
                    stdout="Switched to branch 'issue-456'",
                    stderr="",
                    returncode=0,
                )
            elif cmd == ["git", "checkout", "issue-789"]:
                return CommandResult(
                    success=True,
                    stdout="Switched to branch 'issue-789'",
                    stderr="",
                    returncode=0,
                )
            elif cmd == ["git", "add", "-A"]:
                return CommandResult(
                    success=True,
                    stdout="",
                    stderr="",
                    returncode=0,
                )
            elif cmd == ["git", "commit", "-m", "WIP: Auto-commit before branch checkout"]:
                return CommandResult(
                    success=True,
                    stdout="[main abc123] WIP: Auto-commit before branch checkout",
                    stderr="",
                    returncode=0,
                )
            else:
                # Default response
                return CommandResult(
                    success=True,
                    stdout="",
                    stderr="",
                    returncode=0,
                )

        with patch("src.auto_coder.utils.CommandExecutor.run_command", side_effect=fake_run_command):
            from auto_coder.automation_config import AutomationConfig

            config = AutomationConfig()
            results = migrate_pr_branches(config)

            assert results["success"] is True
            assert len(results["migrated"]) == 3
            # Verify all pr-* branches would be migrated
            migrated_from = [item["from"] for item in results["migrated"]]
            assert "pr-123" in migrated_from
            assert "pr-456" in migrated_from
            assert "pr-789" in migrated_from

    def test_migrate_pr_branches_extraction_failure(self):
        """Test migration when number extraction fails for a branch."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            mock_cmd = MagicMock()
            mock_executor.return_value = mock_cmd
            # Branch with invalid pattern (no number)
            mock_cmd.run_command.return_value = CommandResult(
                success=True,
                stdout="origin/main\norigin/pr-invalid\n",
                stderr="",
                returncode=0,
            )

            from auto_coder.automation_config import AutomationConfig

            config = AutomationConfig()
            results = migrate_pr_branches(config)

            assert results["success"] is True  # Overall success (some branches handled)
            assert len(results["migrated"]) == 0
            assert len(results["skipped"]) == 1
            assert results["skipped"][0]["branch"] == "pr-invalid"
            assert "Could not extract issue number" in results["skipped"][0]["reason"]
            assert len(results["failed"]) == 0

    def test_migrate_pr_branches_with_cwd(self, _use_custom_subprocess_mock):
        """Test migration with custom working directory."""
        from auto_coder.utils import CommandResult

        call_count = [0]  # Use list to make it mutable in closure

        def fake_run(cmd, **kwargs):
            """Fake CommandExecutor.run_command that returns appropriate responses."""
            call_idx = call_count[0]
            call_count[0] += 1

            # Verify cwd was passed
            assert kwargs.get("cwd") == "/custom/path", f"Expected cwd=/custom/path, got {kwargs.get('cwd')}"

            # Handle different git commands
            if cmd == ["git", "branch", "--format=%(refname:short)"]:
                return CommandResult(
                    success=True,
                    stdout="pr-123\n",
                    stderr="",
                    returncode=0,
                )
            elif cmd == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
                if call_idx == 1:
                    branch = "main\n"
                else:
                    branch = "issue-123\n"
                return CommandResult(
                    success=True,
                    stdout=branch,
                    stderr="",
                    returncode=0,
                )
            elif cmd == ["git", "branch", "--list", "issue-123"]:
                return CommandResult(
                    success=True,
                    stdout="issue-123",
                    stderr="",
                    returncode=0,
                )
            elif cmd == ["git", "status", "--porcelain"]:
                return CommandResult(
                    success=True,
                    stdout="",
                    stderr="",
                    returncode=0,
                )
            elif cmd == ["git", "checkout", "issue-123"]:
                return CommandResult(
                    success=True,
                    stdout="Switched to branch 'issue-123'",
                    stderr="",
                    returncode=0,
                )
            elif cmd == ["git", "pull", "origin", "issue-123"]:
                return CommandResult(
                    success=True,
                    stdout="",
                    stderr="",
                    returncode=0,
                )
            elif cmd == ["git", "merge", "origin/pr-123", "--no-ff", "-m", "Merge pr-123 into issue-123"]:
                return CommandResult(
                    success=True,
                    stdout="",
                    stderr="",
                    returncode=0,
                )
            elif cmd == ["git", "rev-list", "origin/issue-123..HEAD", "--count"]:
                return CommandResult(
                    success=True,
                    stdout="1",
                    stderr="",
                    returncode=0,
                )
            elif cmd == ["git", "branch", "-D", "pr-123"]:
                return CommandResult(
                    success=True,
                    stdout="",
                    stderr="",
                    returncode=0,
                )
            elif cmd == ["git", "push", "origin", "--delete", "pr-123"]:
                return CommandResult(
                    success=True,
                    stdout="",
                    stderr="",
                    returncode=0,
                )
            else:
                # Default response
                return CommandResult(
                    success=True,
                    stdout="",
                    stderr="",
                    returncode=0,
                )

        with patch("src.auto_coder.utils.CommandExecutor.run_command", side_effect=fake_run):
            from auto_coder.automation_config import AutomationConfig

            config = AutomationConfig()
            results = migrate_pr_branches(config, cwd="/custom/path")

            assert results["success"] is True


class TestBranchContext:
    """Tests for branch_context function."""

    def test_branch_context_successful_switch_and_return(self):
        """Test successful branch switch and return using context manager."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            with patch("src.auto_coder.git_utils.get_current_branch") as mock_get_branch:
                with patch("src.auto_coder.git_utils.switch_to_branch") as mock_switch:
                    with patch("src.auto_coder.git_utils.is_git_repository") as mock_is_repo:
                        mock_cmd = MagicMock()
                        mock_executor.return_value = mock_cmd

                        # Initially on main
                        mock_get_branch.side_effect = ["main", "feature", "main"]
                        mock_is_repo.return_value = True
                        mock_switch.return_value = CommandResult(success=True, stdout="", stderr="", returncode=0)

                        # Execute the context manager
                        with branch_context("feature"):
                            # Inside context, should be on feature branch
                            assert mock_switch.call_count == 1
                            # Verify switch_to_branch was called with correct parameters
                            call_args = mock_switch.call_args
                            assert call_args[1]["branch_name"] == "feature"
                            assert call_args[1]["pull_after_switch"] is True

                        # After context, should have switched back to main
                        assert mock_switch.call_count == 2
                        # Verify return switch was also called correctly
                        return_call_args = mock_switch.call_args_list[1]
                        assert return_call_args[1]["branch_name"] == "main"
                        assert return_call_args[1]["pull_after_switch"] is True

    def test_branch_context_with_exception(self):
        """Test that context manager returns to original branch even when exception occurs."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            with patch("src.auto_coder.git_utils.get_current_branch") as mock_get_branch:
                with patch("src.auto_coder.git_utils.switch_to_branch") as mock_switch:
                    with patch("src.auto_coder.git_utils.is_git_repository") as mock_is_repo:
                        mock_cmd = MagicMock()
                        mock_executor.return_value = mock_cmd

                        # Initially on main
                        mock_get_branch.side_effect = ["main", "feature", "main"]
                        mock_is_repo.return_value = True
                        mock_switch.return_value = CommandResult(success=True, stdout="", stderr="", returncode=0)

                        # Execute the context manager with an exception
                        with pytest.raises(RuntimeError):
                            with branch_context("feature"):
                                # Raise an exception inside the context
                                raise RuntimeError("Test exception")

                        # Even with exception, should have switched back to main
                        assert mock_switch.call_count == 2
                        return_call_args = mock_switch.call_args_list[1]
                        assert return_call_args[1]["branch_name"] == "main"

    def test_branch_context_create_new_branch(self):
        """Test context manager with create_new parameter."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            with patch("src.auto_coder.git_utils.get_current_branch") as mock_get_branch:
                with patch("src.auto_coder.git_utils.switch_to_branch") as mock_switch:
                    with patch("src.auto_coder.git_utils.is_git_repository") as mock_is_repo:
                        mock_cmd = MagicMock()
                        mock_executor.return_value = mock_cmd

                        # Initially on main
                        mock_get_branch.side_effect = ["main", "new-feature", "main"]
                        mock_is_repo.return_value = True
                        mock_switch.return_value = CommandResult(success=True, stdout="", stderr="", returncode=0)

                        with branch_context("new-feature", create_new=True, base_branch="main"):
                            pass

                        # Verify switch_to_branch was called with create_new and base_branch on entry
                        # Check the first call (entry to new-feature)
                        entry_call = mock_switch.call_args_list[0]
                        assert entry_call[1]["branch_name"] == "new-feature"
                        assert entry_call[1]["create_new"] is True
                        assert entry_call[1]["base_branch"] == "main"

                        # Verify return to main on exit
                        return_call = mock_switch.call_args_list[1]
                        assert return_call[1]["branch_name"] == "main"

    def test_branch_context_with_custom_cwd(self):
        """Test context manager with custom working directory."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            with patch("src.auto_coder.git_utils.get_current_branch") as mock_get_branch:
                with patch("src.auto_coder.git_utils.switch_to_branch") as mock_switch:
                    with patch("src.auto_coder.git_utils.is_git_repository") as mock_is_repo:
                        mock_cmd = MagicMock()
                        mock_executor.return_value = mock_cmd

                        # Initially on main
                        mock_get_branch.side_effect = ["main", "feature", "main"]
                        mock_is_repo.return_value = True
                        mock_switch.return_value = CommandResult(success=True, stdout="", stderr="", returncode=0)

                        with branch_context("feature", cwd="/custom/path"):
                            pass

                        # Verify cwd was passed to all function calls
                        calls = mock_switch.call_args_list
                        for call in calls:
                            assert call[1]["cwd"] == "/custom/path"

    def test_branch_context_already_on_target_branch(self):
        """Test context manager when already on the target branch."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            with patch("src.auto_coder.git_utils.get_current_branch") as mock_get_branch:
                with patch("src.auto_coder.git_utils.switch_to_branch") as mock_switch:
                    with patch("src.auto_coder.git_utils.is_git_repository") as mock_is_repo:
                        mock_cmd = MagicMock()
                        mock_executor.return_value = mock_cmd

                        # Already on the target branch
                        mock_get_branch.side_effect = ["feature", "feature"]
                        mock_is_repo.return_value = True
                        mock_switch.return_value = CommandResult(success=True, stdout="", stderr="", returncode=0)

                        with branch_context("feature"):
                            # Should not call switch_to_branch when already on the branch
                            assert mock_switch.call_count == 0

                        # Should still not switch back since we never left
                        assert mock_switch.call_count == 0

    def test_branch_context_switch_failure(self):
        """Test that RuntimeError is raised when switch_to_branch fails."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            with patch("src.auto_coder.git_utils.get_current_branch") as mock_get_branch:
                with patch("src.auto_coder.git_utils.switch_to_branch") as mock_switch:
                    with patch("src.auto_coder.git_utils.is_git_repository") as mock_is_repo:
                        mock_cmd = MagicMock()
                        mock_executor.return_value = mock_cmd

                        # Initially on main
                        mock_get_branch.side_effect = ["main", "feature"]
                        mock_is_repo.return_value = True
                        # Simulate switch failure
                        mock_switch.return_value = CommandResult(
                            success=False,
                            stdout="",
                            stderr="error: pathspec 'nonexistent' did not match any file(s) known to git",
                            returncode=1,
                        )

                        # Should raise RuntimeError
                        with pytest.raises(RuntimeError) as exc_info:
                            with branch_context("nonexistent"):
                                pass

                        assert "Failed to switch to branch" in str(exc_info.value)

    def test_branch_context_get_current_branch_failure(self):
        """Test RuntimeError when get_current_branch fails initially."""
        with patch("src.auto_coder.git_utils.get_current_branch") as mock_get_branch:
            # get_current_branch returns None to simulate failure
            mock_get_branch.return_value = None

            with pytest.raises(RuntimeError) as exc_info:
                with branch_context("feature"):
                    pass

            assert "Failed to get current branch" in str(exc_info.value)

    def test_branch_context_not_git_repository_on_exit(self):
        """Test that context manager handles not being in a git repo during cleanup."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            with patch("src.auto_coder.git_utils.get_current_branch") as mock_get_branch:
                with patch("src.auto_coder.git_utils.switch_to_branch") as mock_switch:
                    with patch("src.auto_coder.git_utils.is_git_repository") as mock_is_repo:
                        mock_cmd = MagicMock()
                        mock_executor.return_value = mock_cmd

                        # Initially on main
                        mock_get_branch.side_effect = ["main", "feature"]
                        # Not a git repository during cleanup
                        mock_is_repo.return_value = False
                        mock_switch.return_value = CommandResult(success=True, stdout="", stderr="", returncode=0)

                        with branch_context("feature"):
                            # Should successfully enter context
                            pass

                        # Should not call switch_to_branch for return since not in git repo
                        assert mock_switch.call_count == 1

    def test_branch_context_return_switch_failure(self):
        """Test that return switch failure is logged but doesn't raise."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            with patch("src.auto_coder.git_utils.get_current_branch") as mock_get_branch:
                with patch("src.auto_coder.git_utils.switch_to_branch") as mock_switch:
                    with patch("src.auto_coder.git_utils.is_git_repository") as mock_is_repo:
                        mock_cmd = MagicMock()
                        mock_executor.return_value = mock_cmd

                        # Initially on main
                        mock_get_branch.side_effect = ["main", "feature", "feature"]
                        mock_is_repo.return_value = True
                        # First call (entry) succeeds, second call (return) fails
                        mock_switch.side_effect = [
                            CommandResult(success=True, stdout="", stderr="", returncode=0),
                            CommandResult(success=False, stdout="", stderr="Failed to switch", returncode=1),
                        ]

                        # Should complete without raising exception
                        with branch_context("feature"):
                            pass

                        # Should have called switch_to_branch twice
                        assert mock_switch.call_count == 2

    def test_branch_context_with_pull_after_switch(self):
        """Test that pull_after_switch is always True for both entry and exit."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            with patch("src.auto_coder.git_utils.get_current_branch") as mock_get_branch:
                with patch("src.auto_coder.git_utils.switch_to_branch") as mock_switch:
                    with patch("src.auto_coder.git_utils.is_git_repository") as mock_is_repo:
                        mock_cmd = MagicMock()
                        mock_executor.return_value = mock_cmd

                        # Initially on main
                        mock_get_branch.side_effect = ["main", "feature", "main"]
                        mock_is_repo.return_value = True
                        mock_switch.return_value = CommandResult(success=True, stdout="", stderr="", returncode=0)

                        with branch_context("feature"):
                            pass

                        # Verify pull_after_switch is always True
                        for call in mock_switch.call_args_list:
                            assert call[1]["pull_after_switch"] is True

    def test_branch_context_checks_unpushed_commits_by_default(self):
        """Test that branch_context checks and pushes unpushed commits by default."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            with patch("src.auto_coder.git_utils.get_current_branch") as mock_get_branch:
                with patch("src.auto_coder.git_utils.switch_to_branch") as mock_switch:
                    with patch("src.auto_coder.git_utils.is_git_repository") as mock_is_repo:
                        with patch("src.auto_coder.git_utils.ensure_pushed") as mock_ensure_pushed:
                            mock_cmd = MagicMock()
                            mock_executor.return_value = mock_cmd

                            # Initially on main
                            mock_get_branch.side_effect = ["main", "feature", "main"]
                            mock_is_repo.return_value = True
                            mock_switch.return_value = CommandResult(success=True, stdout="", stderr="", returncode=0)
                            # ensure_pushed returns success with unpushed commits
                            mock_ensure_pushed.return_value = CommandResult(
                                success=True,
                                stdout="Pushed 2 commit(s)",
                                stderr="",
                                returncode=0,
                            )

                            with branch_context("feature"):
                                pass

                            # Verify ensure_pushed was called
                            assert mock_ensure_pushed.call_count == 1
                            assert mock_ensure_pushed.call_args[1]["remote"] == "origin"

    def test_branch_context_skips_unpushed_commits_when_disabled(self):
        """Test that branch_context skips unpushed commit check when check_unpushed=False."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            with patch("src.auto_coder.git_utils.get_current_branch") as mock_get_branch:
                with patch("src.auto_coder.git_utils.switch_to_branch") as mock_switch:
                    with patch("src.auto_coder.git_utils.is_git_repository") as mock_is_repo:
                        with patch("src.auto_coder.git_utils.ensure_pushed") as mock_ensure_pushed:
                            mock_cmd = MagicMock()
                            mock_executor.return_value = mock_cmd

                            # Initially on main
                            mock_get_branch.side_effect = ["main", "feature", "main"]
                            mock_is_repo.return_value = True
                            mock_switch.return_value = CommandResult(success=True, stdout="", stderr="", returncode=0)

                            with branch_context("feature", check_unpushed=False):
                                pass

                            # Verify ensure_pushed was NOT called
                            assert mock_ensure_pushed.call_count == 0

    def test_branch_context_with_custom_remote(self):
        """Test that branch_context uses custom remote for unpushed commit check."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            with patch("src.auto_coder.git_utils.get_current_branch") as mock_get_branch:
                with patch("src.auto_coder.git_utils.switch_to_branch") as mock_switch:
                    with patch("src.auto_coder.git_utils.is_git_repository") as mock_is_repo:
                        with patch("src.auto_coder.git_utils.ensure_pushed") as mock_ensure_pushed:
                            mock_cmd = MagicMock()
                            mock_executor.return_value = mock_cmd

                            # Initially on main
                            mock_get_branch.side_effect = ["main", "feature", "main"]
                            mock_is_repo.return_value = True
                            mock_switch.return_value = CommandResult(success=True, stdout="", stderr="", returncode=0)
                            mock_ensure_pushed.return_value = CommandResult(
                                success=True,
                                stdout="No unpushed commits",
                                stderr="",
                                returncode=0,
                            )

                            with branch_context("feature", remote="upstream"):
                                pass

                            # Verify ensure_pushed was called with custom remote
                            assert mock_ensure_pushed.call_count == 1
                            assert mock_ensure_pushed.call_args[1]["remote"] == "upstream"

    def test_branch_context_handles_ensure_pushed_failure(self):
        """Test that branch_context continues even if ensure_pushed fails."""
        with patch("src.auto_coder.git_utils.CommandExecutor") as mock_executor:
            with patch("src.auto_coder.git_utils.get_current_branch") as mock_get_branch:
                with patch("src.auto_coder.git_utils.switch_to_branch") as mock_switch:
                    with patch("src.auto_coder.git_utils.is_git_repository") as mock_is_repo:
                        with patch("src.auto_coder.git_utils.ensure_pushed") as mock_ensure_pushed:
                            mock_cmd = MagicMock()
                            mock_executor.return_value = mock_cmd

                            # Initially on main
                            mock_get_branch.side_effect = ["main", "feature", "main"]
                            mock_is_repo.return_value = True
                            mock_switch.return_value = CommandResult(success=True, stdout="", stderr="", returncode=0)
                            # ensure_pushed fails
                            mock_ensure_pushed.return_value = CommandResult(
                                success=False,
                                stdout="",
                                stderr="Failed to push",
                                returncode=1,
                            )

                            # Should not raise exception, just continue
                            with branch_context("feature"):
                                pass

                            # Verify ensure_pushed was called
                            assert mock_ensure_pushed.call_count == 1
