"""Real original SQLite chronology with explicit component Docker state inputs.

Only the separate native VM proves actual stopped Docker/host producers. These
regressions exercise their bounded fixture finalization against real SQLite,
RetainedJournal and QuarantineHistory without changing those production readers.
"""

from __future__ import annotations

import gc
import json
import os
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts import test_management_recovery_epoch_host as fixture

from auto_coder.management.bootstrap_store import BootstrapStore
from auto_coder.management.operations import OperationPhase
from auto_coder.management.quarantine_history import QuarantineHistory, QuarantineUnavailable
from auto_coder.management.readiness_observation import _connect as _readiness_store
from auto_coder.management.recovery_mailbox import RecoveryMailbox
from auto_coder.management.retained_journal import RetainedJournal
from auto_coder.management.self_update_handoff import HandoffStore
from tests.test_management_archived_cutover_container import Chain, Generation
from tests.test_management_quarantine_history import _store


@pytest.fixture
def originals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    epoch = tmp_path / "epoch"
    epoch.mkdir()
    (epoch / "epoch.lock").touch()
    monkeypatch.setattr(fixture, "EPOCH_ROOT", epoch)
    monkeypatch.setattr(fixture, "AUTHORIZATION_FILE", tmp_path / "authorization.json")
    chain = Chain.__new__(Chain)
    chain.root = tmp_path / "deployment"
    chain.root.mkdir()
    chain.generations = []
    cutover = subprocess.Popen([sys.executable, "-I", "-B", "-c", "pass"])
    assert cutover.wait(timeout=10) == 0
    chain.processes = [cutover]
    chain.docker = lambda *_args: json.dumps({"Status": "exited", "Running": False, "Pid": 0, "Restarting": False})
    journals, captures = {}, []
    for name in ("A", "B", "C"):
        root = chain.root / name
        root.mkdir()
        journal, _ = _store(root / "journal", name, management=True, phase=OperationPhase.NEEDS_ATTENTION if name == "C" else OperationPhase.SUCCEEDED)
        journals[name] = journal
        state = root / "state"
        state.mkdir(mode=0o700)
        previous_umask = os.umask(0o077)
        try:
            with closing(BootstrapStore(state, name)._connect()):
                pass
            with closing(HandoffStore(state, name)._connect()):
                pass
            RecoveryMailbox(state, name).initialize()
        finally:
            os.umask(previous_umask)
        assert {path.name for path in state.glob("*.sqlite3")} == {"management-child.sqlite3", "management-self-update.sqlite3", "management-recovery.sqlite3"}
        for path in state.glob("*.sqlite3"):
            Chain.checkpoint(path)
        if name in {"A", "B"}:
            # Exercise the production observation-store constructor. This
            # component payload makes no authenticated-readiness assertion.
            with closing(_readiness_store(SimpleNamespace(state_root=state))) as connection, connection:
                assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
                connection.execute("INSERT INTO proof VALUES (?, ?)", (name, json.dumps({"component_observation": name})))
        generation = Generation(name, root, "manager-" + name, [], proxy="proxy-" + name)
        chain.generations.append(generation)
        original = SimpleNamespace(generation_id=name, instance_id=name, state="started", management_id=generation.container, proxy_id=generation.proxy)
        captures.append(SimpleNamespace(original=original))
    failed = Generation("D", chain.root / "D", "never-created-manager", [], proxy="never-created-proxy")
    failed.root.mkdir()
    (failed.root / "retained-never-created").write_bytes(b"original failed branch")
    chain.generations.append(failed)
    return chain, tuple(captures), journals


def _late_archived_read(journal) -> None:
    # This earlier checkpoint precedes the genuine later archived read.
    Chain.checkpoint(journal.database)
    assert not Path(str(journal.database) + "-shm").exists()
    with RetainedJournal(journal.database, journal.fence, database_identity=(journal.database_device, journal.database_inode), fence_identity=(journal.fence_device, journal.fence_inode), prior_management_instance="A") as reader:
        assert len(reader.read()) == 1
    assert Path(str(journal.database) + "-wal").stat().st_size == 0
    assert Path(str(journal.database) + "-shm").stat().st_size > 0


def _retained_bytes(chain) -> dict[str, tuple[bytes, int, int, int]]:
    result = {}
    for path in sorted(chain.root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            info = path.stat()
            result[str(path)] = (path.read_bytes(), info.st_dev, info.st_ino, info.st_ctime_ns)
    return result


def test_final_checkpoint_follows_last_archived_read_and_preserves_original_authority(originals, monkeypatch: pytest.MonkeyPatch) -> None:
    chain, captures, journals = originals
    actual, checkpoints = Chain.checkpoint, []

    def observed(database: Path) -> None:
        checkpoints.append(database)
        actual(database)

    collection = gc.isenabled()
    gc.disable()
    try:
        _late_archived_read(journals["A"])
        before = _retained_bytes(chain)
        with pytest.raises(QuarantineUnavailable, match="nonempty transaction sidecar"):
            QuarantineHistory((journals["A"],))
        monkeypatch.setattr(Chain, "checkpoint", observed)
        fixture._finalize_original_stores(chain, captures)
        after = _retained_bytes(chain)
        assert {name: value for name, value in before.items() if not name.endswith(("-wal", "-shm", "-journal"))} == after
        assert (chain.root / "D" / "retained-never-created").read_bytes() == b"original failed branch"
        assert len(checkpoints) == 12 and {path.name for path in checkpoints} == {"management-child.sqlite3", "management-self-update.sqlite3", "management-recovery.sqlite3", "management-operations.sqlite3"}
        assert len([name for name in after if name.endswith(".sqlite3")]) == 14
        assert not (chain.root / "C" / "state" / "management-readiness.sqlite3").exists()
        for name, journal in journals.items():
            with QuarantineHistory((journal,)) as history:
                report = history.inspect()
                assert report.historical_keys == 1
                assert report.unresolved_targets == (1 if name == "C" else 0)
                assert report.launch_authority is False
        assert _retained_bytes(chain) == after
    finally:
        if collection:
            gc.enable()


@pytest.mark.parametrize("name", ["management-self-update.sqlite3", "management-recovery.sqlite3"])
def test_all_real_original_mailboxes_are_required_and_never_reconstructed(originals, name: str) -> None:
    chain, captures, _ = originals
    missing = chain.root / "B" / "state" / name
    missing.unlink()
    before = _retained_bytes(chain)
    with pytest.raises(FileNotFoundError):
        fixture._finalize_original_stores(chain, captures)
    assert not missing.exists() and _retained_bytes(chain) == before


@pytest.mark.parametrize("generation", ["A", "B"])
def test_completed_readiness_observations_are_required_and_never_reconstructed(originals, generation: str) -> None:
    chain, captures, journals = originals
    _late_archived_read(journals["A"])
    missing = chain.root / generation / "state" / "management-readiness.sqlite3"
    missing.unlink()
    before = _retained_bytes(chain)
    with pytest.raises(FileNotFoundError):
        fixture._finalize_original_stores(chain, captures)
    assert not missing.exists() and _retained_bytes(chain) == before


def test_unresolved_cannot_acquire_a_readiness_observation_during_finalization(originals) -> None:
    chain, captures, journals = originals
    _late_archived_read(journals["A"])
    with closing(_readiness_store(SimpleNamespace(state_root=chain.root / "C" / "state"))):
        pass
    before = _retained_bytes(chain)
    with pytest.raises(AssertionError, match="unknown store"):
        fixture._finalize_original_stores(chain, captures)
    assert _retained_bytes(chain) == before


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_observation_sidecars_refuse_without_settling_any_original_store(originals, suffix: str) -> None:
    chain, captures, journals = originals
    _late_archived_read(journals["A"])
    sidecar = chain.root / "B" / "state" / ("management-readiness.sqlite3" + suffix)
    sidecar.touch(mode=0o600)
    before = _retained_bytes(chain)
    with pytest.raises(AssertionError, match="readiness observation still has a transaction sidecar"):
        fixture._finalize_original_stores(chain, captures)
    assert _retained_bytes(chain) == before


@pytest.mark.parametrize("fault", ["nonempty-wal", "hot-journal", "missing-store", "unknown-store", "linked-sidecar", "running-manager", "running-proxy", "live-cutover", "published-inventory", "installed-authorization", "target-prepared", "wrong-capture", "missing-capture"])
def test_finalization_refuses_uncertain_scope_before_any_checkpoint(originals, fault: str) -> None:
    chain, captures, journals = originals
    _late_archived_read(journals["A"])
    stopped = chain.docker
    if fault in {"nonempty-wal", "hot-journal"}:
        path = Path(str(journals["B"].database) + ("-wal" if fault == "nonempty-wal" else "-journal"))
        path.write_bytes(b"retained transaction uncertainty")
        path.chmod(0o600)
    elif fault == "missing-store":
        journals["B"].database.unlink()
    elif fault == "unknown-store":
        (journals["B"].database.parent / "unknown.sqlite3").write_bytes(b"unknown original store")
    elif fault == "linked-sidecar":
        Path(str(journals["B"].database) + "-shm").symlink_to(journals["A"].database)
    elif fault in {"running-manager", "running-proxy"}:
        selected = "manager-B" if fault == "running-manager" else "proxy-B"
        chain.docker = lambda *args: json.dumps({"Status": "running", "Running": True, "Pid": 123, "Restarting": False}) if args[-1] == selected else stopped(*args)
    elif fault == "live-cutover":
        chain.processes = [SimpleNamespace(poll=lambda: None)]
    elif fault == "published-inventory":
        (fixture.EPOCH_ROOT / "inventory.json").write_bytes(b"already selected recovery epoch")
    elif fault == "installed-authorization":
        fixture.AUTHORIZATION_FILE.write_bytes(b"already authorized recovery")
    elif fault == "target-prepared":
        (chain.root / "target").mkdir()
    elif fault == "wrong-capture":
        captures[1].original.management_id = "another-manager"
    else:
        captures = captures[:-1]
    before = _retained_bytes(chain)
    with pytest.raises((AssertionError, ValueError, FileNotFoundError)):
        fixture._finalize_original_stores(chain, captures)
    assert _retained_bytes(chain) == before
    assert Path(str(journals["A"].database) + "-shm").stat().st_size > 0


def test_finalization_rejects_any_database_change_even_if_checkpoint_returns(originals, monkeypatch: pytest.MonkeyPatch) -> None:
    chain, captures, _ = originals
    actual = Chain.checkpoint

    def changed(database: Path) -> None:
        actual(database)
        with database.open("ab") as stream:
            stream.write(b"unexpected original database change")

    monkeypatch.setattr(Chain, "checkpoint", changed)
    with pytest.raises(AssertionError, match="changed original database bytes or identity"):
        fixture._finalize_original_stores(chain, captures)
