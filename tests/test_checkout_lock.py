"""Real-checkout regressions for concurrent controller branch processing."""

import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

from auto_coder import checkout_lock as checkout_locks
from auto_coder.automation_config import AutomationConfig
from auto_coder.branch_manager import BranchManager
from auto_coder.checkout_lock import checkout_lock, worktree_registry_lock
from auto_coder.git_utils import git_commit_with_retry
from auto_coder.pr_processor import _checkout_pr_branch
from auto_coder.shutdown_context import install_admission_check, reset_admission_check
from auto_coder.utils import bind_command_execution_cwd, reset_command_execution_cwd
from auto_coder.worktree_utils import isolated_local_llm_worktree

pytestmark = pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=True).stdout.strip()


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_CODER_RUNTIME_ROOT", str(tmp_path / "runtime"))
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Checkout Regression")
    git(repo, "config", "user.email", "checkout@example.invalid")
    (repo / "source.txt").write_text("baseline\n")
    git(repo, "add", "source.txt")
    assert git_commit_with_retry("Seed checkout", cwd=str(repo)).success
    git(repo, "branch", "issue-5458")
    git(repo, "branch", "pr-test")
    origin = tmp_path / "origin.git"
    git(tmp_path, "clone", "--bare", str(repo), str(origin))
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "fetch", "origin")
    return repo


def test_pr_preparation_waits_through_private_result_handoff(checkout):
    """Reset/clean without checkout must not invalidate an Issue result."""
    started = threading.Event()
    finished = threading.Event()
    config = AutomationConfig()
    config.FORCE_CLEAN_BEFORE_CHECKOUT = False

    def prepare_pr():
        token = bind_command_execution_cwd(str(checkout))
        try:
            started.set()
            result = _checkout_pr_branch("test/repo", {"number": 1, "head_branch": "pr-test"}, config, perform_checkout=False)
            finished.set()
            return result
        finally:
            reset_command_execution_cwd(token)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with BranchManager("issue-5458", cwd=str(checkout), check_unpushed=False):
            token = bind_command_execution_cwd(str(checkout))
            try:
                with isolated_local_llm_worktree(is_noedit=False) as private:
                    (Path(private) / "source.txt").write_text("implemented\n")
                    future = pool.submit(prepare_pr)
                    assert started.wait(5)
                    assert not finished.wait(0.2)
                    assert git(checkout, "branch", "--show-current") == "issue-5458"
                assert (checkout / "source.txt").read_text() == "implemented\n"
                git(checkout, "add", "source.txt")
                assert git_commit_with_retry("Persist Issue implementation", cwd=str(checkout)).success
                assert not finished.is_set()
            finally:
                reset_command_execution_cwd(token)
        assert future.result(timeout=5) is True
    assert git(checkout, "branch", "--show-current") == "main"
    assert git(checkout, "show", "issue-5458:source.txt") == "implemented"


def test_branch_context_waits_and_restores_original_checkout(checkout):
    started = threading.Event()
    entered = threading.Event()

    def use_pr():
        started.set()
        with BranchManager("pr-test", cwd=str(checkout), check_unpushed=False):
            entered.set()
            assert git(checkout, "branch", "--show-current") == "pr-test"

    with ThreadPoolExecutor(max_workers=1) as pool:
        with BranchManager("issue-5458", cwd=str(checkout), check_unpushed=False):
            future = pool.submit(use_pr)
            assert started.wait(5)
            assert not entered.wait(0.2)
            assert git(checkout, "branch", "--show-current") == "issue-5458"
        future.result(timeout=5)
    assert entered.is_set()
    assert git(checkout, "branch", "--show-current") == "main"


def test_checkout_alias_and_subdirectory_share_reentrant_lease(checkout, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(checkout, target_is_directory=True)
    subdirectory = checkout / "nested"
    subdirectory.mkdir()
    with checkout_lock(str(checkout)), checkout_lock(str(alias)), checkout_lock(str(subdirectory)):
        assert git(checkout, "branch", "--show-current") == "main"


def test_physical_checkout_lease_survives_a_different_absolute_path(checkout, tmp_path):
    moved = tmp_path / "renamed-checkout"
    started = threading.Event()
    acquired = threading.Event()

    def use_moved_checkout():
        started.set()
        with checkout_lock(str(moved)):
            acquired.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with checkout_lock(str(checkout)):
            checkout.rename(moved)
            future = pool.submit(use_moved_checkout)
            assert started.wait(5)
            assert not acquired.wait(0.2)
        future.result(timeout=5)
    assert acquired.is_set()


def test_independent_checkout_can_progress_while_first_is_busy(checkout, tmp_path):
    other = tmp_path / "independent"
    git(tmp_path, "clone", str(checkout), str(other))
    with ThreadPoolExecutor(max_workers=1) as pool:
        with checkout_lock(str(checkout)):
            assert pool.submit(_acquire, other).result(timeout=5) is True


def test_same_branch_name_in_another_checkout_is_not_reentry(checkout, tmp_path):
    other = tmp_path / "independent"
    git(tmp_path, "clone", str(checkout), str(other))
    with BranchManager("issue-5458", cwd=str(checkout), check_unpushed=False):
        with BranchManager("issue-5458", cwd=str(other), check_unpushed=False):
            assert git(other, "branch", "--show-current") == "issue-5458"
            assert git(checkout, "branch", "--show-current") == "issue-5458"
        assert git(other, "branch", "--show-current") == "main"


def test_branch_body_failure_restores_branch_and_releases_lease(checkout):
    with pytest.raises(ValueError, match="implementation failed"):
        with BranchManager("issue-5458", cwd=str(checkout), check_unpushed=False):
            raise ValueError("implementation failed")
    assert git(checkout, "branch", "--show-current") == "main"
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(_acquire, checkout).result(timeout=5) is True


def test_entry_failure_releases_lease(checkout):
    with pytest.raises(RuntimeError, match="Failed to switch to branch"):
        with BranchManager("missing-branch", cwd=str(checkout), check_unpushed=False):
            pytest.fail("Missing branch must not enter")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(lambda: _acquire(checkout))
        assert future.result(timeout=5) is True


def _acquire(root):
    with checkout_lock(str(root)):
        return True


def test_shutdown_interrupts_contended_checkout_wait(checkout):
    started = threading.Event()

    def wait_during_shutdown():
        token = install_admission_check(lambda: False)
        try:
            started.set()
            with checkout_lock(str(checkout)):
                pytest.fail("A draining waiter must not enter the occupied checkout")
        finally:
            reset_admission_check(token)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with checkout_lock(str(checkout)):
            future = pool.submit(wait_during_shutdown)
            assert started.wait(5)
            with pytest.raises(RuntimeError, match="graceful shutdown is draining"):
                future.result(timeout=5)


def test_checkout_lease_coordinates_another_process(checkout):
    command = [
        sys.executable,
        "-c",
        "import sys; from auto_coder.checkout_lock import checkout_lock; " "print('waiting', flush=True); " "\nwith checkout_lock(sys.argv[1]): print('acquired', flush=True)",
        str(checkout),
    ]
    with checkout_lock(str(checkout)):
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            assert child.stdout is not None
            assert child.stdout.readline().strip() == "waiting"
            with pytest.raises(subprocess.TimeoutExpired):
                child.wait(timeout=0.2)
        except BaseException:
            child.kill()
            child.communicate(timeout=5)
            raise
    try:
        stdout, stderr = child.communicate(timeout=5)
        assert child.returncode == 0, stderr
        assert stdout.strip() == "acquired"
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)


@pytest.mark.parametrize("caller", ["main", "linked", "alias"])
def test_registry_lease_coordinates_linked_callers_in_another_process(checkout, tmp_path, caller):
    linked, alias = tmp_path / "linked", tmp_path / "alias"
    git(checkout, "worktree", "add", "--detach", str(linked), "HEAD")
    alias.symlink_to(linked, target_is_directory=True)
    target = {"main": checkout, "linked": linked, "alias": alias}[caller]
    code = """import sys
from contextlib import contextmanager
from auto_coder import checkout_lock as locks
actual = locks.file_lock
@contextmanager
def observed(path, **kwargs):
    try:
        with actual(path, timeout=0):
            pass
    except locks.LockAcquisitionTimeout:
        print('contended', flush=True)
    with actual(path, **kwargs):
        yield
locks.file_lock = observed
with locks.worktree_registry_lock(sys.argv[1]):
    print('acquired', flush=True)
"""
    child = None
    try:
        with worktree_registry_lock(str(checkout)):
            child = subprocess.Popen([sys.executable, "-c", code, str(target)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            assert child.stdout is not None
            # This reports an actual nonblocking kernel-lock failure, rather
            # than inferring contention from a startup sleep.
            assert child.stdout.readline().strip() == "contended"
        output, error = child.communicate(timeout=10)
        assert child.returncode == 0, error
        assert output.strip() == "acquired"
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        git(checkout, "worktree", "remove", "--force", str(linked))


@pytest.mark.parametrize("retarget", ["gitfile", "alias"])
def test_registry_lease_refuses_changed_caller_common_directory(checkout, tmp_path, monkeypatch, retarget):
    other, linked, alias = tmp_path / "other", tmp_path / "linked", tmp_path / "alias"
    git(tmp_path, "clone", str(checkout), str(other))
    git(checkout, "worktree", "add", "--detach", str(linked), "HEAD")
    alias.symlink_to(checkout, target_is_directory=True)
    gitfile = linked / ".git"
    original = gitfile.read_bytes()
    actual_lock = checkout_locks.file_lock

    @contextmanager
    def change_after_acquisition(path, **kwargs):
        with actual_lock(path, **kwargs):
            if retarget == "gitfile":
                gitfile.write_text("gitdir: " + str(other / ".git") + "\n")
            else:
                alias.unlink()
                alias.symlink_to(other, target_is_directory=True)
            yield

    target = linked if retarget == "gitfile" else alias
    try:
        with monkeypatch.context() as patch:
            patch.setattr(checkout_locks, "file_lock", change_after_acquisition)
            with pytest.raises(RuntimeError, match="common directory changed"):
                with worktree_registry_lock(str(target)):
                    pytest.fail("Changed caller was admitted under its old registry lock")
    finally:
        gitfile.write_bytes(original)
        alias.unlink()
        alias.symlink_to(checkout, target_is_directory=True)
        git(checkout, "worktree", "remove", "--force", str(linked))
    # The failure released the lease and left the original repository usable.
    with worktree_registry_lock(str(checkout)):
        assert git(checkout, "branch", "--show-current") == "main"


def test_registry_lease_refuses_unidentifiable_repository(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_CODER_RUNTIME_ROOT", str(tmp_path / "runtime"))
    with pytest.raises(RuntimeError, match="Cannot identify Git common directory"):
        with worktree_registry_lock(str(tmp_path)):
            pytest.fail("A non-repository cannot own a Git registry lease")
