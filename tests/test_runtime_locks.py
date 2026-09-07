from pathlib import Path

from auto_coder.cloud_run import CloudRun, CloudRunRepository
from auto_coder.runtime_locks import lock_path, runtime_root


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
