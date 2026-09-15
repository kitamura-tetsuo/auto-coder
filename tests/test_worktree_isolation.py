"""Tests for git worktree isolation of local LLM executions."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from src.auto_coder.utils import _COMMAND_EXECUTION_CWD
from src.auto_coder.worktree_utils import (
    is_git_repository,
    is_inside_git_worktree,
    isolated_local_llm_worktree,
    sync_worktree_changes_back,
)


@pytest.fixture(autouse=True)
def _enable_real_commands(_use_real_commands: None) -> None:
    """Ensure real git commands are used instead of stubs."""
    pass


def _init_repo(path: Path) -> Path:
    repo = path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    (repo / "tracked.txt").write_text("initial content\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo, check=True, capture_output=True)
    return repo


def test_is_inside_git_worktree_and_is_git_repository(tmp_path: Path, _use_real_commands) -> None:
    repo = _init_repo(tmp_path)
    non_git = tmp_path / "non_git"
    non_git.mkdir()

    # Main repo has .git directory, so is_inside_git_worktree is False
    assert not is_inside_git_worktree(repo)
    assert is_git_repository(repo)

    # Non-git directory
    assert not is_inside_git_worktree(non_git)
    assert not is_git_repository(non_git)

    # Linked worktree has .git file
    wt_dir = tmp_path / "linked_wt"
    subprocess.run(["git", "worktree", "add", "--detach", str(wt_dir), "HEAD"], cwd=repo, check=True, capture_output=True)
    try:
        assert is_inside_git_worktree(wt_dir)
        assert is_git_repository(wt_dir)
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt_dir)], cwd=repo, capture_output=True)


def test_isolated_worktree_seeds_staged_and_untracked_files(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "tracked.txt").write_text("modified staged\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    (repo / "untracked.py").write_text("print('hello')\n")
    (repo / "empty_dir").mkdir()

    with isolated_local_llm_worktree(repo, is_noedit=True) as wt_path:
        wt = Path(wt_path)
        assert wt != repo
        assert is_inside_git_worktree(wt)
        assert (wt / "tracked.txt").read_text() == "modified staged\n"
        assert (wt / "untracked.py").read_text() == "print('hello')\n"
        assert (wt / "empty_dir").is_dir()
        assert _COMMAND_EXECUTION_CWD.get() == str(wt)

    # After exit, worktree directory is cleaned up
    assert not Path(wt_path).exists()


def test_isolated_worktree_edit_mode_syncs_changes_back(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    with isolated_local_llm_worktree(repo, is_noedit=False) as wt_path:
        wt = Path(wt_path)
        (wt / "tracked.txt").write_text("updated by llm\n")
        (wt / "new_feature.py").write_text("# new feature\n")

    # In target repo, the edits should be synchronized
    assert (repo / "tracked.txt").read_text() == "updated by llm\n"
    assert (repo / "new_feature.py").read_text() == "# new feature\n"
    assert not Path(wt_path).exists()


def test_isolated_worktree_noedit_mode_discards_changes(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    with isolated_local_llm_worktree(repo, is_noedit=True) as wt_path:
        wt = Path(wt_path)
        (wt / "tracked.txt").write_text("forbidden change\n")
        (wt / "unwanted.txt").write_text("should not exist\n")

    # In target repo, original state is preserved
    assert (repo / "tracked.txt").read_text() == "initial content\n"
    assert not (repo / "unwanted.txt").exists()
    assert not Path(wt_path).exists()


def test_isolated_worktree_nested_passthrough(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    wt_dir = tmp_path / "existing_wt"
    subprocess.run(["git", "worktree", "add", "--detach", str(wt_dir), "HEAD"], cwd=repo, check=True, capture_output=True)

    try:
        with isolated_local_llm_worktree(wt_dir, is_noedit=False) as yielded_path:
            # Should not create nested worktree, but yield wt_dir directly
            assert yielded_path == str(wt_dir)
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt_dir)], cwd=repo, capture_output=True)


def test_isolated_worktree_non_git_passthrough(tmp_path: Path) -> None:
    non_git = tmp_path / "non_git"
    non_git.mkdir()

    with isolated_local_llm_worktree(non_git, is_noedit=False) as yielded_path:
        assert yielded_path == str(non_git)


def test_isolated_worktree_cleans_up_on_exception(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    captured_wt: list[str] = []

    with pytest.raises(RuntimeError, match="simulated failure"):
        with isolated_local_llm_worktree(repo, is_noedit=True) as wt_path:
            captured_wt.append(wt_path)
            raise RuntimeError("simulated failure")

    assert len(captured_wt) == 1
    assert not Path(captured_wt[0]).exists()
