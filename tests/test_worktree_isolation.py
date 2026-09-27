"""Tests for git worktree isolation of local LLM executions."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import threading
from pathlib import Path

import pytest

from src.auto_coder.utils import _COMMAND_EXECUTION_CWD
from src.auto_coder.worktree_utils import (
    WorkspacePreparationError,
    get_current_local_workspace,
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
        assert not is_inside_git_worktree(wt)
        binding = get_current_local_workspace()
        assert binding is not None
        assert binding.workspace == wt
        assert binding.caller_root == repo.resolve()
        assert binding.initial_head == "refs/heads/main"
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


def test_linked_worktree_gets_a_private_repository(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    wt_dir = tmp_path / "existing_wt"
    subprocess.run(["git", "worktree", "add", "--detach", str(wt_dir), "HEAD"], cwd=repo, check=True, capture_output=True)

    try:
        with isolated_local_llm_worktree(wt_dir, is_noedit=False) as yielded_path:
            assert yielded_path != str(wt_dir)
            assert (Path(yielded_path) / ".git").is_dir()
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


def test_private_git_operations_do_not_change_caller_state(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    caller_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    caller_common = subprocess.check_output(["git", "rev-parse", "--git-common-dir"], cwd=repo, text=True).strip()

    with isolated_local_llm_worktree(repo, is_noedit=True) as workspace_text:
        workspace = Path(workspace_text)
        binding = get_current_local_workspace()
        assert binding is not None
        (workspace / "private.txt").write_text("private\n")
        subprocess.run(["git", "add", "private.txt"], cwd=workspace, check=True)
        subprocess.run(["git", "commit", "-m", "private"], cwd=workspace, check=True, capture_output=True)
        subprocess.run(["git", "switch", "-c", "agent-private"], cwd=workspace, check=True, capture_output=True)
        nested = tmp_path / "private-linked"
        subprocess.run(["git", "worktree", "add", str(nested)], cwd=workspace, check=True, capture_output=True)
        subprocess.run(["git", "worktree", "remove", str(nested)], cwd=workspace, check=True, capture_output=True)
        assert binding.caller_common_dir == (repo / caller_common).resolve()

    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip() == caller_head
    assert subprocess.run(["git", "show-ref", "--verify", "refs/heads/agent-private"], cwd=repo).returncode != 0
    assert not (repo / "private.txt").exists()


def test_dirty_index_files_modes_symlinks_and_disposable_paths_are_seeded_exactly(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / ".gitignore").write_text("ignored.bin\n")
    (repo / "delete.txt").write_text("delete me\n")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "tracked.txt").write_text("tracked cache name\n")
    subprocess.run(["git", "add", ".gitignore", "delete.txt", "node_modules/tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "fixtures"], cwd=repo, check=True, capture_output=True)
    (repo / "tracked.txt").write_text("staged\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    (repo / "tracked.txt").write_text("working\n")
    (repo / "added.bin").write_bytes(b"\x00\xff\x10")
    subprocess.run(["git", "add", "added.bin"], cwd=repo, check=True)
    (repo / "delete.txt").unlink()
    subprocess.run(["git", "add", "delete.txt"], cwd=repo, check=True)
    executable = repo / "tool.sh"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    (repo / "link").symlink_to("tracked.txt")
    (repo / "ignored.bin").write_bytes(b"ignored\x00")
    (repo / ".agent-tmp").mkdir()
    (repo / ".agent-tmp" / "discard").write_text("no")
    (repo / "empty").mkdir()

    with isolated_local_llm_worktree(repo, is_noedit=True) as workspace_text:
        workspace = Path(workspace_text)
        assert subprocess.check_output(["git", "show", ":tracked.txt"], cwd=workspace) == b"staged\n"
        assert (workspace / "tracked.txt").read_bytes() == b"working\n"
        assert subprocess.check_output(["git", "show", ":added.bin"], cwd=workspace) == b"\x00\xff\x10"
        assert not (workspace / "delete.txt").exists()
        assert stat.S_IMODE((workspace / "tool.sh").stat().st_mode) == 0o755
        assert os.readlink(workspace / "link") == "tracked.txt"
        assert (workspace / "ignored.bin").read_bytes() == b"ignored\x00"
        assert not (workspace / ".agent-tmp").exists()
        assert (workspace / "node_modules" / "tracked.txt").read_text() == "tracked cache name\n"
        assert (workspace / "empty").is_dir()


@pytest.mark.parametrize("unsupported", ["unmerged", "gitlink"])
def test_unsupported_repository_shapes_fail_before_launch(tmp_path: Path, unsupported: str) -> None:
    repo = _init_repo(tmp_path)
    if unsupported == "unmerged":
        blob = subprocess.check_output(["git", "hash-object", "-w", "tracked.txt"], cwd=repo, text=True).strip()
        index_info = f"100644 {blob} 1\ttracked.txt\n100644 {blob} 2\ttracked.txt\n"
        subprocess.run(["git", "update-index", "--index-info"], cwd=repo, input=index_info, text=True, check=True)
    else:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        subprocess.run(["git", "update-index", "--add", "--cacheinfo", "160000", commit, "module"], cwd=repo, check=True)

    launched = False
    with pytest.raises(WorkspacePreparationError):
        with isolated_local_llm_worktree(repo, is_noedit=True):
            launched = True
    assert not launched


def test_private_clone_has_copied_objects_without_hardlinks_or_alternates(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    object_id = subprocess.check_output(["git", "hash-object", "tracked.txt"], cwd=repo, text=True).strip()
    caller_object = repo / ".git" / "objects" / object_id[:2] / object_id[2:]
    caller_contents = caller_object.read_bytes()

    with isolated_local_llm_worktree(repo, is_noedit=True) as workspace_text:
        workspace = Path(workspace_text)
        private_object = workspace / ".git" / "objects" / object_id[:2] / object_id[2:]
        alternates = workspace / ".git" / "objects" / "info" / "alternates"

        assert private_object.is_file()
        assert caller_object.stat().st_nlink == 1
        assert private_object.stat().st_nlink == 1
        assert not alternates.exists()

        private_contents = private_object.read_bytes()
        private_object.write_bytes(b"deliberately private\n")
        assert caller_object.read_bytes() == caller_contents
        private_object.write_bytes(private_contents)
        subprocess.run(["git", "cat-file", "-e", object_id], cwd=workspace, check=True)


@pytest.mark.parametrize("mutation", ["tracked-file", "index-only"])
def test_preparation_refuses_source_changes_during_seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str) -> None:
    repo = _init_repo(tmp_path)
    clone_finished = threading.Event()
    allow_seed = threading.Event()
    real_run = subprocess.run

    def pausing_run(*args, **kwargs):
        result = real_run(*args, **kwargs)
        command = args[0] if args else kwargs.get("args", ())
        if command[:2] == ["git", "clone"]:
            clone_finished.set()
            assert allow_seed.wait(timeout=10)
        return result

    monkeypatch.setattr("src.auto_coder.worktree_utils.subprocess.run", pausing_run)
    outcome: list[object] = []

    def prepare() -> None:
        try:
            with isolated_local_llm_worktree(repo, is_noedit=True) as workspace:
                outcome.append(workspace)
        except BaseException as exc:
            outcome.append(exc)

    worker = threading.Thread(target=prepare)
    worker.start()
    assert clone_finished.wait(timeout=10)
    if mutation == "tracked-file":
        (repo / "tracked.txt").write_text("changed during preparation\n")
    else:
        new_blob = subprocess.check_output(["git", "hash-object", "-w", "--stdin"], cwd=repo, input=b"index-only\n").decode().strip()
        subprocess.run(["git", "update-index", "--cacheinfo", "100644", new_blob, "tracked.txt"], cwd=repo, check=True)
        assert (repo / "tracked.txt").read_text() == "initial content\n"
    allow_seed.set()
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert len(outcome) == 1
    assert isinstance(outcome[0], WorkspacePreparationError)
    assert "changed during workspace preparation" in str(outcome[0])


def test_disposing_one_private_workspace_preserves_peer_workspace_and_caller_refs(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    initial_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    subprocess.run(["git", "branch", "peer", initial_commit], cwd=repo, check=True)
    subprocess.run(["git", "update-ref", "refs/testing/to-delete", initial_commit], cwd=repo, check=True)
    prepared = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    finished = [threading.Event(), threading.Event()]
    workspaces: list[Path | None] = [None, None]
    failures: list[BaseException] = []

    def hold_workspace(index: int) -> None:
        try:
            with isolated_local_llm_worktree(repo, is_noedit=True) as workspace:
                workspaces[index] = Path(workspace)
                prepared[index].set()
                assert release[index].wait(timeout=10)
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished[index].set()

    workers = [threading.Thread(target=hold_workspace, args=(index,)) for index in range(2)]
    for worker in workers:
        worker.start()
    assert all(event.wait(timeout=10) for event in prepared)
    assert workspaces[0] is not None and workspaces[1] is not None
    assert workspaces[0] != workspaces[1]

    peer_commit = subprocess.check_output(
        ["git", "commit-tree", f"{initial_commit}^{{tree}}", "-p", initial_commit, "-m", "peer advance"],
        cwd=repo,
        text=True,
    ).strip()
    subprocess.run(["git", "update-ref", "refs/heads/peer", peer_commit, initial_commit], cwd=repo, check=True)
    subprocess.run(["git", "update-ref", "refs/testing/created", peer_commit], cwd=repo, check=True)
    subprocess.run(["git", "update-ref", "-d", "refs/testing/to-delete", initial_commit], cwd=repo, check=True)

    first_workspace = workspaces[0]
    second_workspace = workspaces[1]
    release[0].set()
    assert finished[0].wait(timeout=10)
    assert first_workspace is not None and not first_workspace.exists()
    assert second_workspace is not None and second_workspace.exists()
    subprocess.run(["git", "status", "--short"], cwd=second_workspace, check=True, capture_output=True)
    assert subprocess.check_output(["git", "rev-parse", "refs/heads/peer"], cwd=repo, text=True).strip() == peer_commit
    assert subprocess.check_output(["git", "rev-parse", "refs/testing/created"], cwd=repo, text=True).strip() == peer_commit
    assert subprocess.run(["git", "show-ref", "--verify", "refs/testing/to-delete"], cwd=repo).returncode != 0
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip() == initial_commit

    release[1].set()
    assert finished[1].wait(timeout=10)
    for worker in workers:
        worker.join(timeout=10)
    assert failures == []
