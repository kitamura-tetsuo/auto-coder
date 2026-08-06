"""Tests for git pull failure recovery in git_branch.git_pull."""

import subprocess
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest

from src.auto_coder.git_branch import git_pull, reset_branch_to_remote
from src.auto_coder.utils import CommandResult


def _run_git(args: List[str], cwd: Path) -> str:
    """Run a git command in the given directory and return its stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


@pytest.fixture
def git_sandbox(tmp_path: Path, _use_real_commands) -> Path:
    """Create a local clone tracking a bare 'origin' remote with one commit on main."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    clone = tmp_path / "clone"

    _run_git(["init", "--bare", "--initial-branch=main", str(origin)], cwd=tmp_path)

    seed.mkdir()
    _run_git(["init", "--initial-branch=main"], cwd=seed)
    _run_git(["config", "user.email", "test@example.com"], cwd=seed)
    _run_git(["config", "user.name", "Test User"], cwd=seed)
    (seed / "store.ts").write_text("original\n")
    _run_git(["add", "."], cwd=seed)
    _run_git(["commit", "-m", "initial"], cwd=seed)
    _run_git(["remote", "add", "origin", str(origin)], cwd=seed)
    _run_git(["push", "origin", "main"], cwd=seed)

    _run_git(["clone", str(origin), str(clone)], cwd=tmp_path)
    _run_git(["config", "user.email", "test@example.com"], cwd=clone)
    _run_git(["config", "user.name", "Test User"], cwd=clone)

    # Advance origin/main so the clone always has something to pull
    (seed / "store.ts").write_text("remote update\n")
    _run_git(["commit", "-am", "remote update"], cwd=seed)
    _run_git(["push", "origin", "main"], cwd=seed)

    return clone


class TestGitPullDiscardsLocalChanges:
    """Pull must succeed even when local state blocks the merge."""

    def test_local_modification_is_discarded_and_pull_succeeds(self, git_sandbox: Path) -> None:
        (git_sandbox / "store.ts").write_text("local edit that blocks the merge\n")

        result = git_pull(remote="origin", branch="main", cwd=str(git_sandbox))

        assert result.success is True
        assert (git_sandbox / "store.ts").read_text() == "remote update\n"
        assert _run_git(["status", "--porcelain"], cwd=git_sandbox).strip() == ""

    def test_untracked_file_blocking_merge_is_removed(self, git_sandbox: Path, tmp_path: Path) -> None:
        seed = tmp_path / "seed"
        (seed / "added.ts").write_text("from remote\n")
        _run_git(["add", "added.ts"], cwd=seed)
        _run_git(["commit", "-m", "add file"], cwd=seed)
        _run_git(["push", "origin", "main"], cwd=seed)

        # Same path exists locally as an untracked file with different content
        (git_sandbox / "added.ts").write_text("local untracked\n")

        result = git_pull(remote="origin", branch="main", cwd=str(git_sandbox))

        assert result.success is True
        assert (git_sandbox / "added.ts").read_text() == "from remote\n"

    def test_unfinished_merge_state_is_aborted_and_pull_succeeds(self, git_sandbox: Path) -> None:
        # Leave a merge in progress (MERGE_HEAD present) without concluding it
        _run_git(["checkout", "-b", "side"], cwd=git_sandbox)
        (git_sandbox / "side.ts").write_text("side change\n")
        _run_git(["add", "side.ts"], cwd=git_sandbox)
        _run_git(["commit", "-m", "side change"], cwd=git_sandbox)
        _run_git(["checkout", "main"], cwd=git_sandbox)
        _run_git(["merge", "--no-commit", "--no-ff", "side"], cwd=git_sandbox)
        assert (git_sandbox / ".git" / "MERGE_HEAD").exists()

        result = git_pull(remote="origin", branch="main", cwd=str(git_sandbox))

        assert result.success is True
        assert not (git_sandbox / ".git" / "MERGE_HEAD").exists()
        assert _run_git(["status", "--porcelain"], cwd=git_sandbox).strip() == ""
        assert (git_sandbox / "store.ts").read_text() == "remote update\n"

    def test_stale_index_lock_is_removed(self, git_sandbox: Path) -> None:
        (git_sandbox / ".git" / "index.lock").write_text("")
        (git_sandbox / "store.ts").write_text("local edit\n")

        result = git_pull(remote="origin", branch="main", cwd=str(git_sandbox))

        assert result.success is True
        assert not (git_sandbox / ".git" / "index.lock").exists()

    def test_local_changes_are_kept_when_discard_local_is_disabled(self, git_sandbox: Path) -> None:
        (git_sandbox / "store.ts").write_text("local edit that blocks the merge\n")

        result = git_pull(
            remote="origin",
            branch="main",
            cwd=str(git_sandbox),
            discard_local=False,
        )

        assert result.success is False
        assert (git_sandbox / "store.ts").read_text() == "local edit that blocks the merge\n"


class TestResetBranchToRemote:
    """The hard-reset fallback must never destroy committed work."""

    def test_reset_matches_remote_state(self, git_sandbox: Path) -> None:
        (git_sandbox / "store.ts").write_text("local edit\n")
        (git_sandbox / "junk.txt").write_text("untracked\n")

        result = reset_branch_to_remote(remote="origin", branch="main", cwd=str(git_sandbox))

        assert result.success is True
        assert (git_sandbox / "store.ts").read_text() == "remote update\n"
        assert not (git_sandbox / "junk.txt").exists()

    def test_reset_is_refused_when_unpushed_commits_exist(self, git_sandbox: Path) -> None:
        (git_sandbox / "store.ts").write_text("committed locally\n")
        _run_git(["commit", "-am", "local commit"], cwd=git_sandbox)

        result = reset_branch_to_remote(remote="origin", branch="main", cwd=str(git_sandbox))

        assert result.success is False
        assert "unpushed commits" in result.stderr
        assert (git_sandbox / "store.ts").read_text() == "committed locally\n"


class _FakeGit:
    """Records git invocations and replays scripted results for 'git pull'."""

    def __init__(self, pull_results: List[CommandResult]) -> None:
        self.pull_results = list(pull_results)
        self.calls: List[List[str]] = []

    def __call__(self, cmd: List[str], *args: object, **kwargs: object) -> CommandResult:
        self.calls.append(cmd)
        if cmd[:2] == ["git", "pull"]:
            return self.pull_results.pop(0)
        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return CommandResult(success=True, stdout="main\n", stderr="", returncode=0)
        if cmd[:3] == ["git", "rev-parse", "--git-dir"]:
            return CommandResult(success=True, stdout="/nonexistent/.git\n", stderr="", returncode=0)
        if cmd[:2] == ["git", "rev-list"]:
            return CommandResult(success=True, stdout="0\n", stderr="", returncode=0)
        return CommandResult(success=True, stdout="", stderr="", returncode=0)

    def pull_count(self) -> int:
        return sum(1 for call in self.calls if call[:2] == ["git", "pull"])

    def ran(self, prefix: List[str]) -> bool:
        return any(call[: len(prefix)] == prefix for call in self.calls)


def _failure(stderr: str) -> CommandResult:
    return CommandResult(success=False, stdout="", stderr=stderr, returncode=1)


def _success() -> CommandResult:
    return CommandResult(success=True, stdout="Updating abc..def\n", stderr="", returncode=0)


class TestGitPullTransientErrors:
    """Network-level failures are retried before giving up."""

    def test_transient_error_is_retried_and_succeeds(self) -> None:
        fake = _FakeGit([_failure("ssh: connect to host github.com port 22: Connection timed out"), _success()])

        with patch("src.auto_coder.git_branch.CommandExecutor.run_command", side_effect=fake), patch("src.auto_coder.git_branch.time.sleep") as mock_sleep:
            result = git_pull(remote="origin", branch="main")

        assert result.success is True
        assert fake.pull_count() == 2
        mock_sleep.assert_called_once_with(1)

    def test_transient_error_is_not_recovered_by_reset(self) -> None:
        error = _failure("fatal: unable to access 'https://github.com/x/y.git': Could not resolve host: github.com")
        fake = _FakeGit([error, error, error])

        with patch("src.auto_coder.git_branch.CommandExecutor.run_command", side_effect=fake), patch("src.auto_coder.git_branch.time.sleep"):
            result = git_pull(remote="origin", branch="main")

        assert result.success is False
        assert fake.pull_count() == 3
        assert not fake.ran(["git", "reset", "--hard"])


class TestGitPullFallback:
    """Unrecoverable pull failures fall back to a hard reset onto the remote."""

    def test_unknown_error_triggers_hard_reset(self) -> None:
        fake = _FakeGit([_failure("error: something completely unexpected happened")])

        with patch("src.auto_coder.git_branch.CommandExecutor.run_command", side_effect=fake):
            result = git_pull(remote="origin", branch="main")

        assert result.success is True
        assert fake.pull_count() == 1
        assert fake.ran(["git", "fetch", "origin", "main"])
        assert fake.ran(["git", "reset", "--hard", "FETCH_HEAD"])

    def test_unknown_error_is_returned_when_discard_local_is_disabled(self) -> None:
        fake = _FakeGit([_failure("error: something completely unexpected happened")])

        with patch("src.auto_coder.git_branch.CommandExecutor.run_command", side_effect=fake):
            result = git_pull(remote="origin", branch="main", discard_local=False)

        assert result.success is False
        assert "something completely unexpected" in result.stderr
        assert not fake.ran(["git", "reset", "--hard", "FETCH_HEAD"])

    def test_failed_conflict_resolution_falls_back_to_hard_reset(self) -> None:
        fake = _FakeGit([_failure("hint: You have divergent branches and need to specify how to reconcile them.\nfatal: Need to specify how to reconcile diverging branches.")])

        with (
            patch("src.auto_coder.git_branch.CommandExecutor.run_command", side_effect=fake),
            patch(
                "src.auto_coder.git_branch.resolve_pull_conflicts",
                return_value=_failure("CONFLICT (content): Merge conflict in store.ts"),
            ) as mock_resolve,
        ):
            result = git_pull(remote="origin", branch="main")

        assert result.success is True
        mock_resolve.assert_called_once()
        assert fake.ran(["git", "reset", "--hard", "FETCH_HEAD"])

    def test_no_tracking_information_is_treated_as_success(self) -> None:
        fake = _FakeGit([_failure("There is no tracking information for the current branch.")])

        with patch("src.auto_coder.git_branch.CommandExecutor.run_command", side_effect=fake):
            result = git_pull(remote="origin", branch="feature/new")

        assert result.success is True
        assert fake.pull_count() == 1
        assert not fake.ran(["git", "reset", "--hard", "FETCH_HEAD"])


class TestGitPullBranchDetection:
    """Branch resolution failures are reported without further recovery attempts."""

    def test_failure_to_resolve_current_branch_is_reported(self) -> None:
        def fake_run(cmd: List[str], *args: object, **kwargs: object) -> CommandResult:
            if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
                return _failure("fatal: not a git repository")
            raise AssertionError(f"unexpected command: {cmd}")

        with patch("src.auto_coder.git_branch.CommandExecutor.run_command", side_effect=fake_run):
            result = git_pull(remote="origin", branch=None)

        assert result.success is False
        assert "Failed to get current branch" in result.stderr
