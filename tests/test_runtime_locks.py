import fcntl
import os
import stat
import threading
from pathlib import Path

import pytest

from auto_coder.cloud_run import CloudRun, CloudRunRepository
from auto_coder.runtime_locks import LockAcquisitionTimeout, ensure_lock_directory, file_lock, lock_path, runtime_root


def test_runtime_root_unset_and_empty_use_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("AUTO_CODER_RUNTIME_ROOT", raising=False)
    assert runtime_root() == (tmp_path / ".auto-coder/runtime").resolve()
    monkeypatch.setenv("AUTO_CODER_RUNTIME_ROOT", "")
    assert runtime_root() == (tmp_path / ".auto-coder/runtime").resolve()


def test_lock_identity_uses_repository_store_purpose_and_key(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTO_CODER_RUNTIME_ROOT", str(tmp_path / "runtime"))
    state = tmp_path / "state" / "records.json"
    equivalent = state.parent / ".." / "state" / "records.json"
    first = lock_path("owner/repo", state, "purpose", "key")

    assert first == lock_path("owner/repo", equivalent, "purpose", "key")
    assert first != lock_path("owner/repo", state, "other-purpose", "key")
    assert first != lock_path("owner/repo", state, "purpose", "other-key")
    assert first != lock_path("owner/repo", tmp_path / "other/records.json", "purpose", "key")
    assert first.parent == (tmp_path / "runtime/locks/owner/repo").resolve()


def test_production_store_keeps_explicit_state_separate_from_runtime(monkeypatch, tmp_path):
    runtime = tmp_path / "shared-runtime"
    state = tmp_path / "configuration" / "runs.json"
    monkeypatch.setenv("AUTO_CODER_RUNTIME_ROOT", str(runtime))
    repository = CloudRunRepository("owner/repo", state)

    repository.save(CloudRun(repo_name="owner/repo", issue_number=1, attempt=0, provider="provider"))

    assert state.is_file()
    assert repository.lock_path.is_file()
    assert repository.lock_path.parent == runtime / "locks/owner/repo"
    assert list(state.parent.glob("*.lock")) == []


@pytest.mark.parametrize("incoming_umask", [0o022, 0o077])
def test_overlapping_shared_directory_creation_never_changes_process_umask(monkeypatch, tmp_path, incoming_umask):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    probe_directory = tmp_path / "unrelated"
    probe_directory.mkdir()
    monkeypatch.setenv("AUTO_CODER_RUNTIME_ROOT", str(runtime))
    original_mkdir = Path.mkdir
    both_creators_entered = threading.Barrier(3)
    release_creators = threading.Event()

    def controlled_mkdir(path, *args, **kwargs):
        if path.parent == runtime / "locks":
            both_creators_entered.wait(timeout=2)
            assert release_creators.wait(timeout=2)
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", controlled_mkdir)
    previous_umask = os.umask(incoming_umask)
    failures = []

    def establish(name):
        try:
            ensure_lock_directory(runtime / "locks" / name / "state.lock", os.getgid())
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=establish, args=(name,)) for name in ("first", "second")]
    try:
        for thread in threads:
            thread.start()
        both_creators_entered.wait(timeout=2)
        during = probe_directory / "during"
        descriptor = os.open(during, os.O_CREAT | os.O_WRONLY, 0o666)
        os.close(descriptor)
        release_creators.set()
        for thread in threads:
            thread.join(timeout=2)
            assert not thread.is_alive()
        after = probe_directory / "after"
        descriptor = os.open(after, os.O_CREAT | os.O_WRONLY, 0o666)
        os.close(descriptor)
    finally:
        release_creators.set()
        for thread in threads:
            thread.join(timeout=2)
        observed_umask = os.umask(previous_umask)

    assert failures == []
    assert observed_umask == incoming_umask
    expected_mode = 0o666 & ~incoming_umask
    assert stat.S_IMODE(during.stat().st_mode) == expected_mode
    assert stat.S_IMODE(after.stat().st_mode) == expected_mode
    assert stat.S_IMODE((runtime / "locks" / "first").stat().st_mode) == 0o2770
    assert stat.S_IMODE((runtime / "locks" / "second").stat().st_mode) == 0o2770


def test_shared_directory_existing_paths_and_creation_failure_preserve_permissions_and_umask(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    existing = runtime / "locks"
    existing.mkdir(parents=True, mode=0o700)
    existing.chmod(0o700)
    monkeypatch.setenv("AUTO_CODER_RUNTIME_ROOT", str(runtime))
    previous_umask = os.umask(0o027)
    try:
        ensure_lock_directory(existing / "owner" / "state.lock", os.getgid())
        observed_after_existing = os.umask(0o027)

        original_mkdir = Path.mkdir

        def failing_mkdir(path, *args, **kwargs):
            if path == existing / "failure":
                raise PermissionError("denied")
            return original_mkdir(path, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", failing_mkdir)
        with pytest.raises(RuntimeError, match="Cannot establish shared runtime lock directory"):
            ensure_lock_directory(existing / "failure" / "state.lock", os.getgid())
        observed_after_failure = os.umask(0o027)
    finally:
        os.umask(previous_umask)

    assert observed_after_existing == 0o027
    assert observed_after_failure == 0o027
    assert stat.S_IMODE(existing.stat().st_mode) == 0o700
    assert stat.S_IMODE((existing / "owner").stat().st_mode) == 0o2770


def test_shared_directory_waits_for_peer_to_finish_published_level(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    locks = runtime / "locks"
    runtime.mkdir()
    locks.mkdir()
    monkeypatch.setenv("AUTO_CODER_RUNTIME_ROOT", str(runtime))
    original_access = os.access
    checks = 0

    def delayed_access(path, mode):
        nonlocal checks
        if path == locks and mode == os.W_OK | os.X_OK:
            checks += 1
            return checks > 1
        return original_access(path, mode)

    monkeypatch.setattr(os, "access", delayed_access)

    ensure_lock_directory(locks / "owner" / "state.lock", os.getgid())

    assert checks == 2
    assert stat.S_IMODE(locks.stat().st_mode) != 0o2770
    assert stat.S_IMODE((locks / "owner").stat().st_mode) == 0o2770


def test_file_lock_reentry_keeps_outer_file_locked(tmp_path):
    path = tmp_path / "shared.lock"
    with file_lock(path):
        with file_lock(path.parent / "." / path.name, timeout=0):
            probe = os.open(path, os.O_RDWR)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(probe)
        # Another thread cannot inherit the first thread's reentry.
        result = []

        def contend():
            try:
                with file_lock(path, timeout=0):
                    result.append("unexpected acquisition")
            except LockAcquisitionTimeout as exc:
                result.append(exc)

        thread = threading.Thread(target=contend)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert len(result) == 1
        assert isinstance(result[0], LockAcquisitionTimeout)
    with file_lock(path, timeout=0):
        assert path.is_file()


def test_file_lock_external_contention_times_out_and_releases_registry(tmp_path):
    path = tmp_path / "external.lock"
    with path.open("a+") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        with pytest.raises(LockAcquisitionTimeout, match="Timed out acquiring runtime lock"):
            with file_lock(path, timeout=0.02):
                pytest.fail("external holder was bypassed")
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
    with file_lock(path, timeout=0):
        assert path.is_file()
