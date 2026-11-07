"""Real-worktree regressions with bounded peers and original failure evidence."""

import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_coder import checkout_lock, pr_processor
from auto_coder.pr_processor import isolated_pr_head_worktree
from auto_coder.utils import CommandExecutor, CommandResult


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=15)
    return result.stdout.strip()


def _repositories(root):
    root.mkdir(exist_ok=True)
    remote, service, ambient = (root / name for name in ("remote.git", "service", "ambient"))
    remote.mkdir()
    _git(remote, "init", "--bare")
    for checkout, identity in ((service, "base"), (ambient, "ambient")):
        checkout.mkdir()
        _git(checkout, "init")
        _git(checkout, "config", "user.email", "test@example.com")
        _git(checkout, "config", "user.name", "Test")
        (checkout / "identity.txt").write_text(identity, encoding="utf-8")
        _git(checkout, "add", ".")
        _git(checkout, "commit", "-m", identity)
    _git(service, "remote", "add", "origin", str(remote))
    heads = {}
    for number, identity in ((101, "alpha"), (202, "beta")):
        (service / "identity.txt").write_text(identity, encoding="utf-8")
        _git(service, "commit", "-am", identity)
        heads[number] = _git(service, "rev-parse", "HEAD")
        _git(service, "push", "origin", f"HEAD:refs/pull/{number}/head")
    return SimpleNamespace(service=service, ambient=ambient, heads=heads)


@pytest.fixture
def worktrees(tmp_path, monkeypatch, _use_real_commands):
    repos = _repositories(tmp_path)
    monkeypatch.setenv("AUTO_CODER_RUNTIME_ROOT", str(tmp_path / "runtime-locks"))
    temporary = tmp_path / "validation-temporary"
    temporary.mkdir()
    created = []
    actual_mkdtemp = pr_processor.tempfile.mkdtemp

    def make_temporary(**kwargs):
        path = actual_mkdtemp(dir=temporary, **kwargs)
        created.append(Path(path))
        return path

    monkeypatch.setattr(pr_processor, "tempfile", SimpleNamespace(mkdtemp=make_temporary))
    command = pr_processor.cmd.run_command

    def bounded_command(args, **kwargs):
        kwargs.setdefault("timeout", 15)
        return command(args, **kwargs)

    monkeypatch.setattr(pr_processor.cmd, "run_command", bounded_command)
    repos.created = created
    repos.body_entered = {number: threading.Event() for number in repos.heads}
    yield repos
    assert all(not path.exists() for path in created)
    # Check only this test's Git registry; unrelated /tmp checkouts are not leaks.
    listed = _git(repos.service, "worktree", "list", "--porcelain")
    assert [line for line in listed.splitlines() if line.startswith("worktree ")] == [f"worktree {repos.service}"]


def _exercise(repos, *, body_failure=None, fail_registration=None):
    entered = threading.Barrier(3)
    release_first, first_cleaned = threading.Event(), threading.Event()
    observations = {}
    original_cwd = Path.cwd()
    errors = []

    def release_peers():
        entered.abort()
        release_first.set()
        first_cleaned.set()

    def validate(number):
        try:
            with isolated_pr_head_worktree("owner/repo", number, repos.heads[number]):
                repos.body_entered[number].set()
                entered.wait(timeout=10)
                first = CommandExecutor.run_command(["cat", "identity.txt"])
                sha = CommandExecutor.run_command(["git", "rev-parse", "HEAD"])
                if body_failure == number:
                    raise RuntimeError("synthetic validation body failure")
                if number == 101:
                    assert release_first.wait(timeout=10), "peer never released first body"
                else:
                    release_first.set()
                    assert first_cleaned.wait(timeout=10), "first peer never completed cleanup"
                second = CommandExecutor.run_command(["cat", "identity.txt"])
                observations[number] = (first.stdout.strip(), sha.stdout.strip(), second.stdout.strip())
            if number == 101:
                first_cleaned.set()
        except BaseException:
            release_peers()
            raise

    os.chdir(repos.service)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = []
            try:
                if fail_registration is None:
                    futures.extend(pool.submit(validate, number) for number in repos.heads)
                else:
                    peer = 202 if fail_registration == 101 else 101
                    futures.append(pool.submit(validate, peer))
                    assert repos.body_entered[peer].wait(timeout=10)
                    futures.append(pool.submit(validate, fail_registration))
                entered.wait(timeout=10)
                ordinary = CommandExecutor.run_command(["cat", "identity.txt"], cwd=str(repos.ambient))
                assert ordinary.stdout.strip() == "ambient"
                assert Path.cwd() == repos.service
            except Exception as exc:
                errors.append(exc)
                release_peers()
            finally:
                # Consume every result even if one peer or the main barrier fails.
                for future in futures:
                    try:
                        future.result(timeout=25)
                    except Exception as exc:
                        errors.append(exc)
                        release_peers()
    finally:
        release_peers()
        os.chdir(original_cwd)
    substantive = [exc for exc in errors if not isinstance(exc, threading.BrokenBarrierError)]
    if errors:
        raise ExceptionGroup("validation peers failed", substantive or errors)
    assert observations[101] == ("alpha", repos.heads[101], "alpha")
    assert observations[202] == ("beta", repos.heads[202], "beta")
    return observations


def test_concurrent_real_validation_worktrees_are_context_isolated(worktrees):
    """Both production body contexts overlap without redirecting process CWD."""
    _exercise(worktrees)


@pytest.mark.parametrize("number", [101, 202])
def test_registration_failure_releases_waiting_peer_and_surfaces_original(worktrees, monkeypatch, number):
    command = pr_processor.cmd.run_command
    injected = []

    def fail_registration(args, **kwargs):
        if args[:3] == ["git", "worktree", "add"] and f"pr{number}_" in args[4]:
            peer = 202 if number == 101 else 101
            assert worktrees.body_entered[peer].wait(timeout=10), "peer did not enter its real context"
            injected.append(True)
            return CommandResult(False, "", "synthetic registration failure", 128)
        return command(args, **kwargs)

    monkeypatch.setattr(pr_processor.cmd, "run_command", fail_registration)
    with pytest.raises(ExceptionGroup) as caught:
        _exercise(worktrees, fail_registration=number)
    assert injected == [True]
    assert len(caught.value.exceptions) == 1
    assert isinstance(caught.value.exceptions[0], RuntimeError)
    assert "synthetic registration failure" in str(caught.value.exceptions[0])


def test_body_failure_releases_waiting_peer_and_joins_both(worktrees):
    with pytest.raises(ExceptionGroup) as caught:
        _exercise(worktrees, body_failure=101)
    assert len(caught.value.exceptions) == 1
    assert str(caught.value.exceptions[0]) == "synthetic validation body failure"


def test_real_git_rejects_transient_empty_peer_commondir(worktrees):
    """Controlled Git metadata state, not a claim that scheduling reproduced it."""
    before = Path.cwd()
    os.chdir(worktrees.service)
    try:
        with isolated_pr_head_worktree("owner/repo", 101, worktrees.heads[101]) as first:
            metadata = Path(_git(Path(first), "rev-parse", "--absolute-git-dir"))
            field = metadata / "commondir"
            original = field.read_bytes()
            locked = metadata / "locked"
            assert not locked.exists()
            try:
                locked.write_text("initializing", encoding="utf-8")
                field.write_bytes(b"")
                result = subprocess.run(["git", "worktree", "add", "--detach", str(worktrees.service.parent / "second"), worktrees.heads[202]], cwd=worktrees.service, capture_output=True, text=True, timeout=15)
                assert result.returncode == 128
                assert f"failed to read {field.relative_to(worktrees.service)}" in result.stderr
                assert "commondir: Success" in result.stderr
            finally:
                field.write_bytes(original)
                locked.unlink()
    finally:
        os.chdir(before)


@pytest.mark.parametrize("action", ["add", "remove"])
def test_registry_commands_serialize_transient_metadata_but_context_bodies_overlap(worktrees, monkeypatch, action):
    """Observe actual lock contention while Git's peer field is held empty."""
    _git(worktrees.service, "push", "origin", f"{worktrees.heads[101]}:refs/pull/303/head")
    command = pr_processor.cmd.run_command
    actual_lock = checkout_lock.file_lock
    field_empty, release_command = threading.Event(), threading.Event()
    second_contended, second_command, second_body = threading.Event(), threading.Event(), threading.Event()
    second_fetch = threading.Event()
    fetch_results = []
    release_bodies, first_body = threading.Event(), threading.Event()
    errors = []

    @contextmanager
    def observed_lock(path, **kwargs):
        # A nonblocking *real* acquisition proves the second contender reached
        # the same held lock; a sleep-based "has not entered yet" is insufficient.
        with ExitStack() as stack:
            immediate = dict(kwargs, timeout=0)
            try:
                stack.enter_context(actual_lock(path, **immediate))
            except checkout_lock.LockAcquisitionTimeout:
                if threading.current_thread().name == "registration-second":
                    second_contended.set()
                stack.enter_context(actual_lock(path, **kwargs))
            yield

    def gated_command(args, **kwargs):
        if args[:2] == ["git", "fetch"] and args[-1] == "pull/303/head":
            second_fetch.set()
            result = command(args, **kwargs)
            fetch_results.append(result.success)
            return result
        if args[:3] == ["git", "worktree", "add"] and "pr303_" in args[4]:
            second_command.set()
        if args[:3] != ["git", "worktree", action] or "pr101_" not in args[4]:
            return command(args, **kwargs)
        target = Path(args[4])
        result = command(args, **kwargs) if action == "add" else None
        if result is not None:
            assert result.success, result.stderr
        metadata = Path(_git(target, "rev-parse", "--absolute-git-dir"))
        field = metadata / "commondir"
        original = field.read_bytes()
        locked = metadata / "locked"
        assert not locked.exists()
        try:
            locked.write_text("initializing", encoding="utf-8")
            field.write_bytes(b"")
            field_empty.set()
            assert release_command.wait(timeout=10), "controller never released metadata command"
        finally:
            field.write_bytes(original)
            locked.unlink()
        return result if result is not None else command(args, **kwargs)

    monkeypatch.setattr(checkout_lock, "file_lock", observed_lock)
    monkeypatch.setattr(pr_processor.cmd, "run_command", gated_command)

    def first():
        with isolated_pr_head_worktree("owner/repo", 101, worktrees.heads[101]):
            first_body.set()
            if action == "add":
                assert release_bodies.wait(timeout=10)

    def second():
        with isolated_pr_head_worktree("owner/repo", 303, worktrees.heads[101]):
            assert CommandExecutor.run_command(["cat", "identity.txt"]).stdout.strip() == "alpha"
            second_body.set()
            assert release_bodies.wait(timeout=10)
            assert CommandExecutor.run_command(["git", "rev-parse", "HEAD"]).stdout.strip() == worktrees.heads[101]

    original_cwd = Path.cwd()
    os.chdir(worktrees.service)
    try:
        # A third body remains active during remove/add too, proving the lease
        # does not serialize long validation bodies or their contextual CWDs.
        with isolated_pr_head_worktree("owner/repo", 202, worktrees.heads[202]):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = []
                try:
                    futures.append(pool.submit(first))
                    assert field_empty.wait(timeout=10), "first command did not reach controlled metadata window"

                    def named_second():
                        threading.current_thread().name = "registration-second"
                        second()

                    futures.append(pool.submit(named_second))
                    assert second_contended.wait(timeout=10), "second command did not contend on the registry lock"
                    assert not second_fetch.is_set() and not second_command.is_set()
                    release_command.set()
                    assert second_body.wait(timeout=10), "second validation never entered its body"
                    assert first_body.wait(timeout=10)
                    assert CommandExecutor.run_command(["cat", "identity.txt"]).stdout.strip() == "beta"
                    assert Path.cwd() == worktrees.service
                except Exception as exc:
                    errors.append(exc)
                finally:
                    release_command.set()
                    release_bodies.set()
                    for future in futures:
                        try:
                            future.result(timeout=25)
                        except Exception as exc:
                            errors.append(exc)
    finally:
        release_command.set()
        release_bodies.set()
        os.chdir(original_cwd)
    if errors:
        raise ExceptionGroup("registry boundary validation failed", errors)
    assert second_command.is_set() and second_contended.is_set()
    assert second_fetch.is_set() and fetch_results == [True]


@pytest.mark.parametrize("failure", ["return-false", "raise"])
def test_unconfirmed_cleanup_preserves_registered_path_original_error_and_releases_lease(worktrees, monkeypatch, failure):
    command = pr_processor.cmd.run_command
    failed_removals = []
    retained = None
    original_cwd = Path.cwd()

    def fail_cleanup(args, **kwargs):
        if args[:3] == ["git", "worktree", "remove"] and "pr101_" in args[4]:
            failed_removals.append(args[4])
            if failure == "raise":
                raise OSError("synthetic cleanup failure")
            return CommandResult(False, "", "synthetic cleanup refusal", 128)
        return command(args, **kwargs)

    def peer_validation():
        with isolated_pr_head_worktree("owner/repo", 202, worktrees.heads[202]):
            return CommandExecutor.run_command(["cat", "identity.txt"]).stdout.strip(), CommandExecutor.run_command(["git", "rev-parse", "HEAD"]).stdout.strip(), Path.cwd()

    monkeypatch.setattr(pr_processor.cmd, "run_command", fail_cleanup)
    os.chdir(worktrees.service)
    try:
        with pytest.raises(RuntimeError, match="original body failure survives cleanup"):
            with isolated_pr_head_worktree("owner/repo", 101, worktrees.heads[101]) as path:
                retained = Path(path)
                raise RuntimeError("original body failure survives cleanup")
        assert retained is not None and retained.is_dir()
        assert failed_removals == [str(retained)]
        assert retained.joinpath("identity.txt").read_text() == "alpha"
        assert _git(retained, "rev-parse", "HEAD") == worktrees.heads[101]
        assert f"worktree {retained}" in _git(worktrees.service, "worktree", "list", "--porcelain")
        # A different thread cannot exploit same-thread lock reentry. Successful
        # peer registration/body/removal demonstrates that cleanup released it.
        with ThreadPoolExecutor(max_workers=1) as pool:
            peer = pool.submit(peer_validation)
            assert peer.result(timeout=25) == ("beta", worktrees.heads[202], worktrees.service)
        assert retained.is_dir() and retained.joinpath("identity.txt").read_text() == "alpha"
        assert _git(worktrees.service, "rev-parse", "HEAD") == worktrees.heads[202]
        assert Path.cwd() == worktrees.service
    finally:
        monkeypatch.setattr(pr_processor.cmd, "run_command", command)
        try:
            if retained is not None and retained.exists():
                with checkout_lock.worktree_registry_lock(str(worktrees.service)):
                    _git(worktrees.service, "worktree", "remove", "--force", str(retained))
        finally:
            os.chdir(original_cwd)
