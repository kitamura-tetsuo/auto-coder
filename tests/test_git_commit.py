"Tests for git_commit module."

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.git_commit import commit_and_push_changes, git_push, save_commit_failure_history
from src.auto_coder.utils import CommandResult


@pytest.mark.usefixtures("_use_custom_subprocess_mock")
class TestGitPush:
    """Tests for git_push function."""

    def test_successful_push(self):
        """Test successful push without branch specified."""
        with patch("src.auto_coder.git_commit.CommandExecutor") as mock_executor_utils, patch("src.auto_coder.git_info.CommandExecutor") as mock_executor_info:
            mock_cmd = MagicMock()
            mock_executor_utils.return_value = mock_cmd
            mock_executor_info.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
                CommandResult(success=True, stdout="2\n", stderr="", returncode=0),  # 2 unpushed commits
                CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
                CommandResult(success=True, stdout="", stderr="", returncode=0),
            ]

            result = git_push()

            assert result.success is True
            assert mock_cmd.run_command.call_count == 4
            last_call_args = mock_cmd.run_command.call_args_list[3][0][0]
            assert last_call_args == ["git", "push", "origin", "main"]

    def test_push_with_branch(self):
        """Test push with specific branch."""
        with patch("src.auto_coder.git_commit.CommandExecutor") as mock_executor_utils, patch("src.auto_coder.git_info.CommandExecutor") as mock_executor_info:
            mock_cmd = MagicMock()
            mock_executor_utils.return_value = mock_cmd
            mock_executor_info.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(success=True, stdout="", stderr="", returncode=0)

            result = git_push(branch="feature-branch")

            assert result.success is True
            assert mock_cmd.run_command.call_count == 1
            call_args = mock_cmd.run_command.call_args[0][0]
            assert call_args == ["git", "push", "origin", "feature-branch"]

    def test_push_with_custom_remote(self):
        """Test push with custom remote."""
        with patch("src.auto_coder.git_commit.CommandExecutor") as mock_executor_utils, patch("src.auto_coder.git_info.CommandExecutor") as mock_executor_info:
            mock_cmd = MagicMock()
            mock_executor_utils.return_value = mock_cmd
            mock_executor_info.return_value = mock_cmd
            mock_cmd.run_command.return_value = CommandResult(success=True, stdout="", stderr="", returncode=0)

            result = git_push(remote="upstream", branch="main")

            assert result.success is True
            assert mock_cmd.run_command.call_count == 1
            call_args = mock_cmd.run_command.call_args[0][0]
            assert call_args == ["git", "push", "upstream", "main"]

    def test_push_failure(self):
        """Test push failure."""
        with patch("src.auto_coder.git_commit.CommandExecutor") as mock_executor_utils, patch("src.auto_coder.git_info.CommandExecutor") as mock_executor_info:
            mock_cmd = MagicMock()
            mock_executor_utils.return_value = mock_cmd
            mock_executor_info.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
                CommandResult(success=True, stdout="2\n", stderr="", returncode=0),
                CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
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
        with patch("src.auto_coder.git_commit.CommandExecutor") as mock_executor_utils, patch("src.auto_coder.git_info.CommandExecutor") as mock_executor_info:
            mock_cmd = MagicMock()
            mock_executor_utils.return_value = mock_cmd
            mock_executor_info.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
                CommandResult(success=True, stdout="2\n", stderr="", returncode=0),
                CommandResult(success=True, stdout="main\n", stderr="", returncode=0),
                CommandResult(success=True, stdout="", stderr="", returncode=0),
            ]

            result = git_push(cwd="/custom/path")

            assert result.success is True
            assert mock_cmd.run_command.call_count == 4
            assert mock_cmd.run_command.call_args_list[0][1]["cwd"] == "/custom/path"
            assert mock_cmd.run_command.call_args_list[1][1]["cwd"] == "/custom/path"
            assert mock_cmd.run_command.call_args_list[2][1]["cwd"] == "/custom/path"
            assert mock_cmd.run_command.call_args_list[3][1]["cwd"] == "/custom/path"

    def test_push_no_upstream_auto_retry(self):
        """Test push automatically retries with --set-upstream when upstream is not set."""
        with patch("src.auto_coder.git_commit.CommandExecutor") as mock_executor_utils, patch("src.auto_coder.git_info.CommandExecutor") as mock_executor_info:
            mock_cmd = MagicMock()
            mock_executor_utils.return_value = mock_cmd
            mock_executor_info.return_value = mock_cmd
            mock_cmd.run_command.side_effect = [
                CommandResult(success=True, stdout="issue-733\n", stderr="", returncode=0),
                CommandResult(success=True, stdout="2\n", stderr="", returncode=0),
                CommandResult(success=True, stdout="issue-733\n", stderr="", returncode=0),
                CommandResult(
                    success=False,
                    stdout="",
                    stderr="fatal: The current branch issue-733 has no upstream branch.",
                    returncode=1,
                ),
                CommandResult(success=True, stdout="issue-733\n", stderr="", returncode=0),
                CommandResult(success=True, stdout="", stderr="", returncode=0),
            ]

            result = git_push()

            assert result.success is True
            assert mock_cmd.run_command.call_count == 6
            final_call_args = mock_cmd.run_command.call_args_list[5][0][0]
            assert final_call_args == [
                "git",
                "push",
                "--set-upstream",
                "origin",
                "issue-733",
            ]


@pytest.mark.usefixtures("_use_custom_subprocess_mock")
class TestSaveCommitFailureHistory:
    """Tests for save_commit_failure_history function."""

    def test_save_commit_failure_history_with_repo_name(self, tmp_path):
        """Test saving commit failure history with repo name."""
        with patch("pathlib.Path.home") as mock_home:
            mock_home.return_value = tmp_path

            error_message = "Test error message"
            context = {"type": "test", "issue_number": 123}
            repo_name = "owner/repo"

            with pytest.raises(SystemExit) as exc_info:
                save_commit_failure_history(error_message, context, repo_name)

            assert exc_info.value.code == 1

            history_dir = tmp_path / ".auto-coder" / "owner_repo"
            assert history_dir.exists()

            history_files = list(history_dir.glob("commit_failure_*.json"))
            assert len(history_files) == 1

            with open(history_files[0], "r") as f:
                data = json.load(f)

            assert data["error_message"] == error_message
            assert data["context"] == context
            assert "timestamp" in data


def test_model_repair_claim_cannot_publish_when_controller_push_still_fails(tmp_path: Path, _use_real_commands: None) -> None:
    """REQ-007/008: only the controller's real remote outcome proves publication."""
    remote = tmp_path / "remote.git"
    repository = tmp_path / "repository"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(repository)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
    (repository / "result.txt").write_text("baseline\n")
    subprocess.run(["git", "add", "result.txt"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repository, check=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=repository, check=True, capture_output=True)
    remote_before = subprocess.check_output(["git", "rev-parse", "refs/heads/main"], cwd=remote, text=True).strip()

    (repository / "result.txt").write_text("unpublished\n")
    subprocess.run(["git", "commit", "-am", "result"], cwd=repository, check=True, capture_output=True)
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)

    with patch("src.auto_coder.git_branch.try_llm_commit_push", return_value=True):
        result = git_push(cwd=str(repository), branch="main", commit_message="repair")

    assert result.success is False
    assert subprocess.check_output(["git", "rev-parse", "refs/heads/main"], cwd=remote, text=True).strip() == remote_before


def test_model_repair_requires_controller_retry_and_exact_remote_verification(tmp_path: Path, _use_real_commands: None) -> None:
    remote = tmp_path / "remote.git"
    repository = tmp_path / "repository"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(repository)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
    (repository / "result.txt").write_text("baseline\n")
    subprocess.run(["git", "add", "result.txt"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repository, check=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=repository, check=True, capture_output=True)
    (repository / "result.txt").write_text("published after retry\n")
    subprocess.run(["git", "commit", "-am", "result"], cwd=repository, check=True, capture_output=True)
    local_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()

    hook = remote / "hooks" / "pre-receive"
    marker = remote / "rejected-once"
    hook.write_text(f'#!/bin/sh\nif [ ! -e "{marker}" ]; then touch "{marker}"; exit 1; fi\nexit 0\n')
    hook.chmod(0o755)
    with patch("src.auto_coder.git_branch.try_llm_commit_push", return_value=True):
        result = git_push(cwd=str(repository), branch="main", commit_message="repair")

    assert result.success is True
    assert result.stdout == f"Verified origin/main at {local_head}"
    assert subprocess.check_output(["git", "rev-parse", "refs/heads/main"], cwd=remote, text=True).strip() == local_head


def test_commit_failure_is_not_reported_as_model_published_success() -> None:
    failure = CommandResult(False, "", "commit rejected", 1)
    with (
        patch("src.auto_coder.git_commit.CommandExecutor") as executor,
        patch("src.auto_coder.git_branch.git_commit_with_retry", return_value=failure),
        patch("src.auto_coder.git_commit.save_commit_failure_history") as save_history,
    ):
        executor.return_value.run_command.side_effect = [
            CommandResult(True, " M result.txt\n", "", 0),
            CommandResult(True, "", "", 0),
        ]
        result = commit_and_push_changes({"summary": "repair"}, repo_name="owner/repo", issue_number=7)

    assert result == "Failed to commit changes: commit rejected"
    save_history.assert_called_once()


def test_save_commit_failure_history_without_repo_name(tmp_path: Path) -> None:
    """Test saving commit failure history without repo name."""
    original_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)

        error_message = "Test error message"
        context = {"type": "test", "pr_number": 456}

        with pytest.raises(SystemExit) as exc_info:
            save_commit_failure_history(error_message, context, None)

        assert exc_info.value.code == 1

        history_dir = tmp_path / ".auto-coder"
        assert history_dir.exists()

        history_files = list(history_dir.glob("commit_failure_*.json"))
        assert len(history_files) == 1

        with open(history_files[0], "r") as f:
            data = json.load(f)

        assert data["error_message"] == error_message
        assert data["context"] == context
        assert "timestamp" in data
    finally:
        os.chdir(original_cwd)
