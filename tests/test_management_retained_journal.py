import fcntl
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from auto_coder.management.operation_store import ManagementOperationStore
from auto_coder.management.operations import AcceptedAuthority, ActionParameter, AdmissionStatus, ManagedTarget, OperationIntent, OperationPhase
from auto_coder.management.retained_journal import RetainedJournal


def identity(path: Path) -> tuple[int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino


def retained(tmp_path: Path) -> tuple[RetainedJournal, Path, Path]:
    store = ManagementOperationStore(tmp_path)
    intent = OperationIntent(ManagedTarget("worker", "beta", "org/repo"), "restart_worker", (ActionParameter("target_id", "beta"),), "key")
    authority = AcceptedAuthority("issuer", "resource", "project", "uid", "client", "grant", ("read", "deploy"), 1, 1)
    result = store.admit(intent, authority, lifecycle_snapshot={"schema_version": 1})
    assert result.record is not None
    assert store.transition(result.record.operation_id, 0, OperationPhase.NEEDS_ATTENTION).record is not None
    database = tmp_path / "management-operations.sqlite3"
    lock = tmp_path / "owner.lock"
    lock.touch()
    return RetainedJournal(database, lock, database_identity=identity(database), fence_identity=identity(lock)), database, lock


def test_retained_records_and_shared_owner_fence(tmp_path: Path) -> None:
    reader, database, lock = retained(tmp_path)
    before = database.read_bytes()
    with reader:
        records = reader.read()
        assert len(records) == 1 and records[0].phase is OperationPhase.NEEDS_ATTENTION
        with lock.open("r") as contender:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert database.read_bytes() == before
    with pytest.raises(ValueError):
        reader.read()


def test_replaced_database_or_fence_fails_closed(tmp_path: Path) -> None:
    reader, database, lock = retained(tmp_path)
    with reader:
        os.rename(database, tmp_path / "old.sqlite3")
        database.touch()
        with pytest.raises(ValueError):
            reader.read()
    database.unlink()
    os.rename(tmp_path / "old.sqlite3", database)
    reader = RetainedJournal(database, lock, database_identity=identity(database), fence_identity=identity(lock))
    with reader:
        os.rename(lock, tmp_path / "old.lock")
        lock.touch()
        with pytest.raises(ValueError):
            reader.read()


@pytest.mark.parametrize("mutation", ["history", "lifecycle", "fingerprint", "schema"])
def test_incomplete_or_inconsistent_evidence_fails_closed(tmp_path: Path, mutation: str) -> None:
    reader, database, _ = retained(tmp_path)
    with sqlite3.connect(database) as connection:
        if mutation == "history":
            connection.execute("DELETE FROM operation_history")
        elif mutation == "lifecycle":
            connection.execute("DELETE FROM worker_lifecycle")
        elif mutation == "fingerprint":
            connection.execute("UPDATE operations SET fingerprint='wrong'")
        else:
            connection.execute("UPDATE metadata SET value='wrong' WHERE key='schema_version'")
    with reader, pytest.raises((ValueError, sqlite3.Error)):
        reader.read()


def test_existing_exclusive_owner_prevents_reader(tmp_path: Path) -> None:
    reader, database, lock = retained(tmp_path)
    reader.close()
    with lock.open("r") as old:
        fcntl.flock(old, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            RetainedJournal(database, lock, database_identity=identity(database), fence_identity=identity(lock))


def test_retained_admission_preserves_replay_conflict_and_target_lane(tmp_path: Path) -> None:
    reader, _, _ = retained(tmp_path)
    with reader:
        original = reader.read()[0]
        intent, authority = original.intent, original.authority
        replay = reader.admission(intent, authority)
        assert replay is not None and replay.status is AdmissionStatus.REPLAY and replay.record == original
        changed = OperationIntent(intent.target, "stop_worker", intent.parameters, intent.idempotency_key)
        conflict = reader.admission(changed, authority)
        assert conflict is not None and conflict.status is AdmissionStatus.CONFLICT
        different = OperationIntent(intent.target, intent.action, intent.parameters, "another-key")
        busy = reader.admission(different, authority)
        assert busy is not None and busy.status is AdmissionStatus.BUSY and busy.record == original
        independent = OperationIntent(ManagedTarget("other", "release", "org/repo"), intent.action, intent.parameters, "another-key")
        assert reader.admission(independent, authority) is None


def test_all_retained_records_are_scanned_beyond_first_page(tmp_path: Path) -> None:
    reader, database, lock = retained(tmp_path)
    reader.close()
    store = ManagementOperationStore(tmp_path)
    authority = AcceptedAuthority("issuer", "resource", "project", "uid", "client", "grant", ("read", "deploy"), 1, 1)
    last = None
    for number in range(205):
        last = OperationIntent(ManagedTarget(f"worker-{number}", "beta", "org/repo"), "restart_worker", (ActionParameter("target_id", "beta"),), "key")
        assert store.admit(last, authority, lifecycle_snapshot={"schema_version": 1}).record is not None
    assert last is not None
    with RetainedJournal(database, lock, database_identity=identity(database), fence_identity=identity(lock)) as reader:
        assert len(reader.read()) == 206
        other_key = OperationIntent(last.target, last.action, last.parameters, "new-key")
        result = reader.admission(other_key, authority)
        assert result is not None and result.status is AdmissionStatus.BUSY


@pytest.mark.parametrize("field", ["issuer", "resource", "project_id", "uid", "oauth_client_id"])
def test_prior_operation_requires_matching_current_actor_and_target(tmp_path: Path, field: str) -> None:
    reader, _, _ = retained(tmp_path)
    with reader:
        original = reader.read()[0]
        assert reader.owned(original.operation_id, original.intent.target, "beta", original.authority) == original
        assert reader.owned(original.operation_id, original.intent.target, "beta", replace(original.authority, **{field: "different"})) is None
        assert reader.owned(original.operation_id, original.intent.target, "release", original.authority) is None
        assert reader.owned(original.operation_id, ManagedTarget("other", "beta", "org/repo"), "beta", original.authority) is None
