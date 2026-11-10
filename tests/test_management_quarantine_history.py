"""Real SQLite, protected descriptors and kernel flock regressions for #2823."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from auto_coder.management.operation_store import ManagementOperationStore
from auto_coder.management.operations import AcceptedAuthority, AdmissionStatus, EffectReceipt, ManagedTarget, OperationIntent, OperationPhase
from auto_coder.management.quarantine_history import QuarantineHistory, QuarantineJournal, QuarantineUnavailable, load_manifest

AUTHORITY = AcceptedAuthority("issuer", "resource", "project", "uid", "client", "grant", ("read", "deploy"), 1, 1)


def _store(root: Path, instance: str, *, management: bool = False, phase: OperationPhase = OperationPhase.NEEDS_ATTENTION, key: str = "key") -> tuple[QuarantineJournal, OperationIntent]:
    root.mkdir(parents=True)
    store = ManagementOperationStore(root)
    target = ManagedTarget(instance, "management", "kitamura-tetsuo/auto-coder") if management else ManagedTarget("shared-worker", "beta", "org/repo")
    intent = OperationIntent(target, "management-service-self-update" if management else "start_worker", (), key)
    admitted = store.admit(intent, AUTHORITY, lifecycle_snapshot=None if management else {"schema_version": 1})
    assert admitted.status is AdmissionStatus.CREATED and admitted.record is not None
    if phase != OperationPhase.ACCEPTED:
        evidence = EffectReceipt("correlation", "test-producer", 1.0, "observed") if phase in {OperationPhase.SUCCEEDED, OperationPhase.FAILED} else None
        assert store.transition(admitted.record.operation_id, 0, phase, evidence=evidence).record is not None
    database = root / "management-operations.sqlite3"
    # Close all writers before immutable snapshot inspection.
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    database.chmod(0o600)
    lock = root / "owner.lock"
    lock.write_bytes(b"")
    lock.chmod(0o600)
    db_stat, lock_stat = database.stat(), lock.stat()
    journal = QuarantineJournal(instance, database, lock, db_stat.st_dev, db_stat.st_ino, hashlib.sha256(database.read_bytes()).hexdigest(), db_stat.st_ctime_ns, lock_stat.st_dev, lock_stat.st_ino, lock_stat.st_ctime_ns)
    return journal, intent


def _manifest(path: Path, journals: tuple[QuarantineJournal, ...]) -> Path:
    body = {"version": 1, "journals": []}
    for journal in journals:
        body["journals"].append(
            {
                "instance_id": journal.instance_id,
                "database": str(journal.database),
                "fence": str(journal.fence),
                "database_device": journal.database_device,
                "database_inode": journal.database_inode,
                "database_sha256": journal.database_sha256,
                "database_ctime_ns": journal.database_ctime_ns,
                "fence_device": journal.fence_device,
                "fence_inode": journal.fence_inode,
                "fence_ctime_ns": journal.fence_ctime_ns,
            }
        )
    path.write_text(json.dumps(body))
    path.chmod(0o600)
    return path


def test_real_journal_inspection_preserves_bytes_and_exclusions(tmp_path: Path) -> None:
    journal, old = _store(tmp_path / "old", "m1")
    manifest = _manifest(tmp_path / "manifest.json", (journal,))
    original = journal.database.read_bytes()
    with QuarantineHistory(load_manifest(manifest)) as reader:
        result = reader.inspect()
        assert result.status == "inspection_only"
        assert not result.launch_authority and result.lineage_completeness == "unverified"
        assert result.inspected_journals == 1 and result.historical_keys == 1 and result.unresolved_targets == 1
        assert reader.decide(old, AUTHORITY).status == "key_excluded"
        other = replace(old, idempotency_key="new")
        assert reader.decide(other, AUTHORITY).status == "target_blocked"
        unrelated = replace(old, target=ManagedTarget("unrelated", "release", "org/repo"))
        assert reader.decide(unrelated, AUTHORITY).status == "no_conflict_in_supplied_journals"
        assert reader.decide(old, replace(AUTHORITY, oauth_client_id="other")).status == "target_blocked"
        with journal.fence.open("r") as contender:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert journal.database.read_bytes() == original
    assert not Path(str(journal.database) + "-shm").exists()
    assert not Path(str(journal.database) + "-wal").exists()
    with journal.fence.open("r") as contender:
        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with pytest.raises(QuarantineUnavailable):
        reader.inspect()


def test_all_instances_revalidated_before_key_exclusion(tmp_path: Path) -> None:
    first, old = _store(tmp_path / "old1", "m1", management=True, phase=OperationPhase.SUCCEEDED)
    second, second_intent = _store(tmp_path / "old2", "m2", management=True)
    with QuarantineHistory((first, second)) as reader:
        candidate = replace(old, target=ManagedTarget("new-manager", "management", "kitamura-tetsuo/auto-coder"))
        assert reader.decide(candidate, AUTHORITY).status == "key_excluded"
        assert reader.decide(replace(candidate, idempotency_key="new"), AUTHORITY).status == "target_blocked"
        second.database.unlink()
        with pytest.raises((QuarantineUnavailable, FileNotFoundError)):
            reader.decide(candidate, AUTHORITY)
    assert second_intent.target.instance_id == "m2"


@pytest.mark.parametrize("suffix", ["-wal", "-journal", "-shm"])
def test_nonempty_transaction_sidecar_refuses_without_mutating_it(tmp_path: Path, suffix: str) -> None:
    journal, _ = _store(tmp_path / "old", "m1")
    sidecar = Path(str(journal.database) + suffix)
    sidecar.write_bytes(b"uncheckpointed")
    sidecar.chmod(0o600)
    before = sidecar.read_bytes()
    with pytest.raises(QuarantineUnavailable):
        QuarantineHistory((journal,))
    assert sidecar.read_bytes() == before


def test_empty_sidecars_preserved_and_changed_sidecar_refuses(tmp_path: Path) -> None:
    journal, _ = _store(tmp_path / "old", "m1")
    wal = Path(str(journal.database) + "-wal")
    wal.write_bytes(b"")
    wal.chmod(0o600)
    with QuarantineHistory((journal,)) as reader:
        assert reader.inspect().historical_keys == 1
        assert wal.exists() and wal.stat().st_size == 0
        wal.write_bytes(b"dirty")
        with pytest.raises(QuarantineUnavailable):
            reader.inspect()


def test_replaced_digest_or_corrupt_history_refuses(tmp_path: Path) -> None:
    journal, old = _store(tmp_path / "old", "m1")
    with QuarantineHistory((journal,)) as reader:
        with closing(sqlite3.connect(journal.database)) as connection:
            connection.execute("DELETE FROM operation_history")
            connection.commit()
        with pytest.raises(QuarantineUnavailable):
            reader.decide(old, AUTHORITY)
    # Updating the caller-supplied digest does not make a damaged history valid.
    stat_info = journal.database.stat()
    updated = replace(journal, database_sha256=hashlib.sha256(journal.database.read_bytes()).hexdigest(), database_inode=stat_info.st_ino)
    with pytest.raises((ValueError, sqlite3.Error)):
        QuarantineHistory((updated,))


def test_busy_later_fence_closes_earlier_fence(tmp_path: Path) -> None:
    first, _ = _store(tmp_path / "first", "m1")
    second, _ = _store(tmp_path / "second", "m2")
    with second.fence.open("r") as blocked:
        fcntl.flock(blocked, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            QuarantineHistory((first, second))
        with first.fence.open("r") as contender:
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.parametrize("problem", ["missing_database", "replaced_fence", "wrong_digest", "unsafe_permissions", "symlink", "duplicate"])
def test_missing_replaced_or_duplicate_evidence_refuses(tmp_path: Path, problem: str) -> None:
    journal, _ = _store(tmp_path / "old", "m1")
    journals = (journal,)
    if problem == "missing_database":
        journal.database.unlink()
    elif problem == "replaced_fence":
        journal.fence.unlink()
        journal.fence.write_bytes(b"")
        journal.fence.chmod(0o600)
    elif problem == "wrong_digest":
        journals = (replace(journal, database_sha256="0" * 64),)
    elif problem == "unsafe_permissions":
        journal.database.chmod(0o644)
    elif problem == "symlink":
        journal.fence.unlink()
        journal.fence.symlink_to(journal.database)
    elif problem == "duplicate":
        journals = (journal, journal)
    with pytest.raises((OSError, QuarantineUnavailable)):
        QuarantineHistory(journals)


def test_manifest_is_private_exact_schema_and_duplicate_rejected(tmp_path: Path) -> None:
    journal, _ = _store(tmp_path / "old", "m1")
    manifest = _manifest(tmp_path / "manifest.json", (journal,))
    assert load_manifest(manifest) == (journal,)
    manifest.chmod(0o644)
    with pytest.raises(QuarantineUnavailable):
        load_manifest(manifest)
    manifest.chmod(0o600)
    body = json.loads(manifest.read_text())
    body["unexpected_authority"] = True
    manifest.write_text(json.dumps(body))
    with pytest.raises(QuarantineUnavailable):
        load_manifest(manifest)
    _manifest(manifest, (journal, journal))
    with pytest.raises(QuarantineUnavailable):
        load_manifest(manifest)


def test_command_is_inspection_only_and_fails_closed(tmp_path: Path) -> None:
    journal, _ = _store(tmp_path / "old", "m1")
    manifest = _manifest(tmp_path / "manifest.json", (journal,))
    args = [sys.executable, "-m", "auto_coder.management.quarantine_history", "--manifest", str(manifest)]
    completed = subprocess.run(args, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    parsed = json.loads(completed.stdout)
    assert parsed == {
        "status": "inspection_only",
        "launch_authority": False,
        "lineage_completeness": "unverified",
        "inspected_journals": 1,
        "historical_keys": 1,
        "unresolved_targets": 1,
    }
    journal.database.unlink()
    rejected = subprocess.run(args, capture_output=True, text=True, check=False)
    assert rejected.returncode != 0
    assert json.loads(rejected.stdout)["status"] == "unavailable"
    assert not journal.database.exists()


def test_killed_reader_releases_real_kernel_fence(tmp_path: Path) -> None:
    journal, _ = _store(tmp_path / "old", "m1")
    manifest = _manifest(tmp_path / "manifest.json", (journal,))
    script = "import sys,time;" "from pathlib import Path;" "from auto_coder.management.quarantine_history import QuarantineHistory,load_manifest;" "reader=QuarantineHistory(load_manifest(Path(sys.argv[1])));" "print('locked',flush=True);time.sleep(30)"
    process = subprocess.Popen([sys.executable, "-c", script, str(manifest)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "locked"
        with journal.fence.open("r") as contender:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        process.send_signal(signal.SIGKILL)
        process.wait(timeout=5)
        with journal.fence.open("r") as contender:
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_independent_reader_processes_hold_separate_fences(tmp_path: Path) -> None:
    """Losing the first reader does not release the surviving reader's flock."""
    journal, _ = _store(tmp_path / "old", "m1")
    manifest = _manifest(tmp_path / "manifest.json", (journal,))
    script = "import sys,time;" "from pathlib import Path;" "from auto_coder.management.quarantine_history import QuarantineHistory,load_manifest;" "reader=QuarantineHistory(load_manifest(Path(sys.argv[1])));" "print('ready',flush=True);time.sleep(30)"
    readers: list[subprocess.Popen[str]] = []
    try:
        for _ in range(2):
            process = subprocess.Popen(
                [sys.executable, "-c", script, str(manifest)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            readers.append(process)
            assert process.stdout is not None
            assert process.stdout.readline().strip() == "ready"
        readers[0].kill()
        readers[0].wait(timeout=5)
        assert readers[1].poll() is None
        with journal.fence.open("r") as contender:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        readers[1].kill()
        readers[1].wait(timeout=5)
        with journal.fence.open("r") as contender:
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        for process in readers:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def test_real_midread_mutation_of_earlier_journal_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Schedule a real out-of-process write between original journal reads."""
    from auto_coder.management import quarantine_history

    first, _ = _store(tmp_path / "first", "m1")
    second, _ = _store(tmp_path / "second", "m2", management=True)
    original_read = quarantine_history._read_protected
    mutated = False

    def read_and_schedule(path: Path, *, identity: tuple[int, int] | None = None, ctime_ns: int | None = None, limit: int) -> tuple[bytes, tuple[int, ...]]:
        nonlocal mutated
        if path == second.database and not mutated:
            mutated = True
            subprocess.run(
                [sys.executable, "-c", "import sys; from pathlib import Path; Path(sys.argv[1]).open('ab').write(b'changed')", str(first.database)],
                check=True,
            )
        return original_read(path, identity=identity, ctime_ns=ctime_ns, limit=limit)

    monkeypatch.setattr(quarantine_history, "_read_protected", read_and_schedule)
    with pytest.raises(QuarantineUnavailable):
        QuarantineHistory((first, second))
    assert mutated
