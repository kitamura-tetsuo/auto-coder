"""Tests for git worktree isolation of local LLM executions."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import threading
from pathlib import Path

import pytest

import src.auto_coder.worktree_utils as worktree_utils
from src.auto_coder.utils import _COMMAND_EXECUTION_CWD
from src.auto_coder.worktree_utils import (
    LocalWorkspaceOwnership,
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


def test_clean_private_commit_is_promoted_from_final_files(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    with isolated_local_llm_worktree(repo, is_noedit=False) as wt_path:
        wt = Path(wt_path)
        (wt / "tracked.txt").write_text("committed privately\n")
        (wt / "private-addition.bin").write_bytes(b"\x00result\xff")
        subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
        subprocess.run(["git", "commit", "-m", "private result"], cwd=wt, check=True, capture_output=True)
        assert subprocess.check_output(["git", "status", "--porcelain"], cwd=wt) == b""

    assert (repo / "tracked.txt").read_text() == "committed privately\n"
    assert (repo / "private-addition.bin").read_bytes() == b"\x00result\xff"
    assert subprocess.check_output(["git", "log", "-1", "--format=%s"], cwd=repo, text=True).strip() == "initial commit"


def test_stale_caller_checkpoint_refuses_entire_private_result(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    workspace_path: Path | None = None

    with pytest.raises(WorkspacePreparationError, match="caller checkpoint changed"):
        with isolated_local_llm_worktree(repo, is_noedit=False) as wt_path:
            workspace_path = Path(wt_path)
            (workspace_path / "tracked.txt").write_text("private result\n")
            (workspace_path / "addition.txt").write_text("new result\n")
            (repo / "tracked.txt").write_text("newer caller work\n")

    assert (repo / "tracked.txt").read_text() == "newer caller work\n"
    assert not (repo / "addition.txt").exists()


def test_rebound_caller_git_identity_refuses_matching_filesystem(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    with pytest.raises(WorkspacePreparationError, match="caller Git identity changed"):
        with isolated_local_llm_worktree(repo, is_noedit=False) as wt_path:
            (Path(wt_path) / "tracked.txt").write_text("private result\n")
            original_git = tmp_path / "original-git"
            (repo / ".git").rename(original_git)
            # Recreate matching metadata at the same path with a distinct
            # directory identity.
            shutil.copytree(original_git, repo / ".git")

    assert (repo / "tracked.txt").read_text() == "initial content\n"


def test_failing_replacement_path_is_included_in_handoff_rollback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _init_repo(tmp_path)
    original_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    original_index = (repo / ".git" / "index").read_bytes()
    real_copyfile = shutil.copyfile
    failed = False

    def fail_once(source: Path, path: Path, *, follow_symlinks: bool = True) -> Path:
        nonlocal failed
        if path == repo / "tracked.txt" and not failed:
            failed = True
            raise OSError("disk full")
        return real_copyfile(source, path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(shutil, "copyfile", fail_once)
    with pytest.raises(WorkspacePreparationError, match="rolled back"):
        with isolated_local_llm_worktree(repo, is_noedit=False) as wt_path:
            (Path(wt_path) / "tracked.txt").write_text("private result\n")

    assert (repo / "tracked.txt").read_text() == "initial content\n"
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip() == original_head
    assert (repo / ".git" / "index").read_bytes() == original_index


def test_changed_result_requires_positive_generation_authority(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    ownership = LocalWorkspaceOwnership()

    with pytest.raises(WorkspacePreparationError, match="handoff evidence is missing"):
        with isolated_local_llm_worktree(
            repo,
            is_noedit=False,
            ownership=ownership,
            require_handoff_authorization=True,
        ) as wt_path:
            (Path(wt_path) / "tracked.txt").write_text("unauthorized\n")
            ownership.release_execution()

    assert (repo / "tracked.txt").read_text() == "initial content\n"
    ownership.release_handoff()


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
    ownership = LocalWorkspaceOwnership()

    with pytest.raises(RuntimeError, match="simulated failure"):
        with isolated_local_llm_worktree(repo, is_noedit=True, ownership=ownership) as wt_path:
            captured_wt.append(wt_path)
            raise RuntimeError("simulated failure")

    assert len(captured_wt) == 1
    assert Path(captured_wt[0]).exists()
    assert ownership.handoff_released is True
    ownership.release_execution()
    assert not Path(captured_wt[0]).exists()


def test_unowned_exception_removes_workspace_and_snapshot(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "context.txt").write_text("context\n")
    with pytest.raises(RuntimeError, match="provider failed"):
        with isolated_local_llm_worktree(repo) as text:
            parent = Path(text).parent
            assert (parent / "source-snapshot").is_dir()
            raise RuntimeError("provider failed")
    assert not parent.exists()
    assert (repo / "tracked.txt").read_text() == "initial content\n"


@pytest.mark.parametrize("directory", ["coverage", "coverage-backups", "htmlcov", "playwright-report", "test-results"])
def test_generated_reports_are_not_copied_but_tracked_files_are_preserved(tmp_path: Path, directory: str) -> None:
    repo = _init_repo(tmp_path)
    (repo / directory).mkdir()
    (repo / directory / "source.txt").write_text("tracked source\n")
    subprocess.run(["git", "add", directory], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "tracked fixture"], cwd=repo, check=True, capture_output=True)
    (repo / ".gitignore").write_text(f"{directory}/\ncontext.bin\n")
    (repo / directory / "report.json").write_bytes(b"large generated output" * 10000)
    (repo / "context.bin").write_bytes(b"preserved context")
    with isolated_local_llm_worktree(repo) as text:
        root = Path(text)
        assert (root / directory / "source.txt").read_text() == "tracked source\n"
        assert not (root / directory / "report.json").exists()
        assert (root / "context.bin").read_bytes() == b"preserved context"
        snapshots = list((root.parent / "source-snapshot").iterdir())
        assert sum(p.stat().st_size for p in snapshots) < 1024
        # Disposable report changes do not stale a source handoff.
        (repo / directory / "report.json").write_bytes(b"new report")
        (root / directory / "source.txt").write_text("updated source\n")
    assert (repo / directory / "source.txt").read_text() == "updated source\n"
    assert (repo / directory / "report.json").read_bytes() == b"new report"


def test_insufficient_capacity_refuses_before_context_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    repo = _init_repo(tmp_path)
    (repo / "context.bin").write_bytes(b"context")
    parents: list[Path] = []
    original_mkdtemp = worktree_utils.tempfile.mkdtemp

    def create_parent(**kwargs: str) -> str:
        result = original_mkdtemp(dir=tmp_path, **kwargs)
        parents.append(Path(result))
        return result

    monkeypatch.setattr(worktree_utils.tempfile, "mkdtemp", create_parent)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: SimpleNamespace(free=worktree_utils._WORKSPACE_FREE_RESERVE + 1))
    with pytest.raises(WorkspacePreparationError, match="insufficient disk space"):
        with isolated_local_llm_worktree(repo):
            pytest.fail("must not launch the provider")
    assert len(parents) == 1
    assert not parents[0].exists()
    assert (repo / "context.bin").read_bytes() == b"context"


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


@pytest.mark.parametrize("mutation", ["tracked-file", "index-only", "symbolic-head"])
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
    elif mutation == "index-only":
        new_blob = subprocess.check_output(["git", "hash-object", "-w", "--stdin"], cwd=repo, input=b"index-only\n").decode().strip()
        subprocess.run(["git", "update-index", "--cacheinfo", "100644", new_blob, "tracked.txt"], cwd=repo, check=True)
        assert (repo / "tracked.txt").read_text() == "initial content\n"
    else:
        subprocess.run(["git", "branch", "other", "HEAD"], cwd=repo, check=True)
        subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/other"], cwd=repo, check=True)
    allow_seed.set()
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert len(outcome) == 1
    assert isinstance(outcome[0], WorkspacePreparationError)
    assert "changed during workspace preparation" in str(outcome[0])


def test_handoff_failure_retains_workspace_until_explicit_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _init_repo(tmp_path)
    ownership = LocalWorkspaceOwnership()
    ownership.retain_session()
    workspace_path: Path | None = None

    def fail_handoff(*args, **kwargs) -> None:
        raise WorkspacePreparationError("simulated handoff failure")

    monkeypatch.setattr(worktree_utils, "sync_worktree_changes_back", fail_handoff)
    with pytest.raises(WorkspacePreparationError, match="simulated handoff failure"):
        with isolated_local_llm_worktree(repo, is_noedit=False, ownership=ownership) as workspace:
            workspace_path = Path(workspace)
            (workspace_path / "recoverable.txt").write_text("retain me\n")
            ownership.release_execution()

    assert workspace_path is not None
    assert (workspace_path / "recoverable.txt").read_text() == "retain me\n"
    assert ownership.handoff_released is True
    ownership.release_session()
    assert not workspace_path.exists()


def test_workspace_waits_for_explicit_child_writer_settlement(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    ownership = LocalWorkspaceOwnership()
    allow_write = threading.Event()
    writer_finished = threading.Event()
    workspace_path: Path | None = None

    def delayed_writer(path: Path) -> None:
        assert allow_write.wait(timeout=10)
        (path / "late-result.txt").write_text("complete\n")
        writer_finished.set()

    with isolated_local_llm_worktree(repo, is_noedit=True, ownership=ownership) as workspace:
        workspace_path = Path(workspace)
        writer = threading.Thread(target=delayed_writer, args=(workspace_path,))
        writer.start()

    assert workspace_path.exists()
    allow_write.set()
    assert writer_finished.wait(timeout=10)
    writer.join(timeout=10)
    assert (workspace_path / "late-result.txt").read_text() == "complete\n"
    ownership.release_execution()
    assert not workspace_path.exists()


def test_tracked_file_permissions_survive_preparation_and_noop_handoff(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    tracked = repo / "tracked.txt"
    tracked.chmod(0o600)

    previous_umask = os.umask(0o022)
    try:
        with isolated_local_llm_worktree(repo, is_noedit=False) as workspace:
            assert stat.S_IMODE((Path(workspace) / "tracked.txt").stat().st_mode) == 0o600
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(tracked.stat().st_mode) == 0o600


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


@pytest.mark.parametrize("artifact", ["build/generated.js", "coverage/report.json", "runtime.log"])
def test_retained_checkpoint_ignores_new_ignored_build_artifacts(tmp_path: Path, artifact: str) -> None:
    repo = _init_repo(tmp_path)
    (repo / ".gitignore").write_text("build/\ncoverage/\n*.log\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    ownership = LocalWorkspaceOwnership()
    ownership.retain_session()
    try:
        with isolated_local_llm_worktree(repo, ownership=ownership) as private:
            binding = get_current_local_workspace()
            assert binding is not None
            workspace = Path(private)
            (workspace / "tracked.txt").write_text("implementation result\n")
            generated = workspace / artifact
            generated.parent.mkdir(parents=True, exist_ok=True)
            generated.write_text("private build output\n")
            ownership.release_execution()
        assert (repo / "tracked.txt").read_text() == "implementation result\n"
        assert not (repo / artifact).exists()
        advanced = worktree_utils.refresh_local_workspace_binding(binding)
        assert advanced.workspace == workspace
        assert advanced.invocation_id == binding.invocation_id
        assert advanced.file_snapshot_checksum == worktree_utils.current_local_caller_checkpoint(advanced)
        assert {state.relative_path for state in advanced.initial_files} == {".gitignore", "tracked.txt"}
        assert generated.read_text() == "private build output\n"
        # The next handoff must still detect edits against the advanced source baseline.
        (workspace / "tracked.txt").write_text("next implementation result\n")
        sync_worktree_changes_back(workspace, repo, advanced)
        assert (repo / "tracked.txt").read_text() == "next implementation result\n"
        assert not (repo / artifact).exists()
    finally:
        ownership.release_session()


@pytest.mark.parametrize("tracking", ["baseline-now-ignored", "private-tracked-ignored", "ordinary-untracked"])
def test_retained_checkpoint_preserves_source_scope_after_git_changes(tmp_path: Path, tracking: str) -> None:
    repo = _init_repo(tmp_path)
    (repo / "source.txt").write_text("baseline\n")
    if tracking != "baseline-now-ignored":
        (repo / "source.txt").unlink()
    ownership = LocalWorkspaceOwnership()
    ownership.retain_session()
    try:
        with isolated_local_llm_worktree(repo, ownership=ownership) as private:
            binding = get_current_local_workspace()
            assert binding is not None
            workspace = Path(private)
            (workspace / "source.txt").write_text("result\n")
            if tracking != "ordinary-untracked":
                (workspace / ".gitignore").write_text("source.txt\n")
            if tracking == "private-tracked-ignored":
                subprocess.run(["git", "update-index", "--add", "source.txt"], cwd=workspace, check=True)
            ownership.release_execution()
        assert (repo / "source.txt").read_text() == "result\n"
        advanced = worktree_utils.refresh_local_workspace_binding(binding)
        assert "source.txt" in {state.relative_path for state in advanced.initial_files}
        # Even a source path now ignored by Git remains part of retained validation.
        (workspace / "source.txt").write_text("unhanded source change\n")
        with pytest.raises(worktree_utils.WorkspaceHandoffError, match="retained private root does not match"):
            worktree_utils.refresh_local_workspace_binding(advanced)
        assert (repo / "source.txt").read_text() == "result\n"
    finally:
        ownership.release_session()


def test_retained_checkpoint_keeps_ignored_caller_context_in_staleness_guard(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / ".gitignore").write_text("context.log\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    (repo / "context.log").write_text("caller context\n")
    ownership = LocalWorkspaceOwnership()
    ownership.retain_session()
    try:
        with isolated_local_llm_worktree(repo, ownership=ownership) as private:
            binding = get_current_local_workspace()
            assert binding is not None
            workspace = Path(private)
            (workspace / "context.log").write_text("private context\n")
            ownership.release_execution()
        advanced = worktree_utils.refresh_local_workspace_binding(binding)
        assert (repo / "context.log").read_text() == "caller context\n"
        assert "context.log" not in {state.relative_path for state in advanced.initial_files}
        (repo / "context.log").write_text("concurrent caller change\n")
        (workspace / "tracked.txt").write_text("implementation result\n")
        with pytest.raises(worktree_utils.WorkspaceHandoffError, match="caller checkpoint changed"):
            sync_worktree_changes_back(workspace, repo, advanced)
        assert (repo / "tracked.txt").read_text() == "initial content\n"
    finally:
        ownership.release_session()


def test_large_workspace_context_and_handoff_use_bounded_memory(tmp_path: Path) -> None:
    """Real Git capture and handoff must not retain whole source files in RAM."""
    import tracemalloc

    repo = _init_repo(tmp_path)
    (repo / ".gitignore").write_text("runtime/\n")
    large_size = 16 * 1024 * 1024
    for relative in ("tracked.bin", "untracked.bin", "runtime/ignored.bin"):
        path = repo / relative
        path.parent.mkdir(exist_ok=True)
        with path.open("wb") as stream:
            stream.truncate(large_size)
    subprocess.run(["git", "add", ".gitignore", "tracked.bin"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "large baseline"], cwd=repo, check=True, capture_output=True)

    tracemalloc.start()
    try:
        with isolated_local_llm_worktree(repo) as workspace_path:
            workspace = Path(workspace_path)
            for relative in ("tracked.bin", "untracked.bin", "runtime/ignored.bin"):
                assert (workspace / relative).stat().st_size == large_size
                assert worktree_utils._file_checksum(workspace / relative) == worktree_utils._file_checksum(repo / relative)
            # Change a large non-ignored file without allocating its contents.
            with (workspace / "untracked.bin").open("r+b") as stream:
                stream.seek(large_size - 1)
                stream.write(b"X")
            binding = get_current_local_workspace()
            assert binding is not None
            assert worktree_utils._result_has_file_delta(workspace, binding)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 4 * 1024 * 1024, f"48 MiB source caused {peak / 1024**2:.2f} MiB Python allocations"
    assert (repo / "untracked.bin").stat().st_size == large_size
    with (repo / "untracked.bin").open("rb") as stream:
        stream.seek(large_size - 2)
        assert stream.read() == b"\0X"
    assert not Path(workspace_path).parent.exists()


def test_ignored_context_same_size_change_refuses_large_file_handoff(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / ".gitignore").write_text("context.bin\n")
    context = repo / "context.bin"
    with context.open("wb") as stream:
        stream.truncate(8 * 1024 * 1024)
    original_stat = context.stat()

    with pytest.raises(WorkspacePreparationError, match="caller checkpoint changed"):
        with isolated_local_llm_worktree(repo) as workspace_path:
            (Path(workspace_path) / "tracked.txt").write_text("private result\n")
            with context.open("r+b") as stream:
                stream.seek(original_stat.st_size - 1)
                stream.write(b"X")
            os.utime(context, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    assert (repo / "tracked.txt").read_text() == "initial content\n"
    with context.open("rb") as stream:
        stream.seek(original_stat.st_size - 1)
        assert stream.read() == b"X"


def test_large_binary_staged_and_unstaged_patches_use_bounded_memory(tmp_path: Path) -> None:
    import tracemalloc

    repo = _init_repo(tmp_path)
    binary = repo / "binary.dat"
    with binary.open("wb") as stream:
        for _ in range(32):
            stream.write(os.urandom(256 * 1024))
    subprocess.run(["git", "add", "binary.dat"], cwd=repo, check=True)
    staged_checksum = worktree_utils._file_checksum(binary)
    with binary.open("r+b") as stream:
        stream.write(b"unstaged bytes\0")
    working_checksum = worktree_utils._file_checksum(binary)
    assert staged_checksum != working_checksum
    caller_index_checksum = worktree_utils._file_checksum(repo / ".git" / "index")

    tracemalloc.start()
    try:
        with isolated_local_llm_worktree(repo, is_noedit=True) as workspace_path:
            workspace = Path(workspace_path)
            assert worktree_utils._file_checksum(workspace / "binary.dat") == working_checksum
            # Spool the private index blob too, so the test oracle stays bounded.
            exported_index = tmp_path / "index-blob"
            with exported_index.open("wb") as stream:
                subprocess.run(["git", "show", ":binary.dat"], cwd=workspace, stdout=stream, check=True)
            assert worktree_utils._file_checksum(exported_index) == staged_checksum
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 4 * 1024 * 1024, f"Binary patches caused {peak / 1024**2:.2f} MiB Python allocations"
    assert worktree_utils._file_checksum(binary) == working_checksum
    assert worktree_utils._file_checksum(repo / ".git" / "index") == caller_index_checksum
