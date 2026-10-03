import fcntl
import os
import threading
from pathlib import Path

import pytest

from auto_coder.cloud_run import CloudRun, CloudRunRepository
from auto_coder.runtime_locks import LockAcquisitionTimeout, file_lock, lock_path, runtime_root


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
