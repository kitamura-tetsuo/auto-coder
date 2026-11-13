"""Real SQLite lifetimes; supplied Docker metadata is component input only."""

from __future__ import annotations

import fcntl
import gc
import hashlib
import json
import os
import sqlite3
import stat
import uuid
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from auto_coder.management import fresh_host_observation as observation
from auto_coder.management.quarantine_history import QuarantineHistory
from tests.test_management_archived_cutover_container import Chain, Generation
from tests.test_management_fresh_initialization import _intent
from tests.test_management_quarantine_history import AUTHORITY, _store


@contextmanager
def _without_collection():
    """Expose leaked connection cycles without relying on finalizer timing."""
    enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if enabled:
            gc.enable()


def _open_database_handles(database: Path) -> tuple[str, ...]:
    selected = {str(database), *(str(database) + suffix for suffix in ("-wal", "-shm", "-journal"))}
    result = []
    for descriptor in Path("/proc/self/fd").iterdir():
        try:
            target = os.readlink(descriptor)
        except FileNotFoundError:
            continue
        if target.removesuffix(" (deleted)") in selected:
            result.append(target)
    return tuple(sorted(result))


def _assert_no_sidecars(database: Path) -> None:
    assert not _open_database_handles(database)
    for suffix in ("-wal", "-shm", "-journal"):
        assert not Path(str(database) + suffix).exists()


def test_checkpoint_closes_real_sqlite_before_quarantine_inspection(tmp_path: Path) -> None:
    journal, old = _store(tmp_path / "original", "original-A", management=True)
    before = journal.database.read_bytes()
    identity = journal.database.stat()
    _assert_no_sidecars(journal.database)
    with _without_collection():
        Chain.checkpoint(journal.database)
        _assert_no_sidecars(journal.database)
        current = journal.database.stat()
        assert (current.st_dev, current.st_ino, current.st_uid, stat.S_IMODE(current.st_mode)) == (identity.st_dev, identity.st_ino, identity.st_uid, 0o600)
        assert journal.database.read_bytes() == before
        with QuarantineHistory((journal,)) as history:
            report = history.inspect()
            assert (report.inspected_journals, report.historical_keys, report.unresolved_targets) == (1, 1, 1)
            assert history.decide(old, AUTHORITY).status == "key_excluded"
            assert report.launch_authority is False
        _assert_no_sidecars(journal.database)
        assert journal.database.read_bytes() == before


@pytest.mark.parametrize("reader", ["child", "grants"])
@pytest.mark.parametrize("missing_table", [False, True])
def test_fixture_reads_close_connections_on_success_and_error(tmp_path: Path, reader: str, missing_table: bool) -> None:
    root = tmp_path / reader
    root.mkdir(mode=0o700)
    database = root / ("management-child.sqlite3" if reader == "child" else "oauth.sqlite3")
    with closing(sqlite3.connect(database)) as producer:
        assert producer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        if reader == "child":
            producer.execute("CREATE TABLE " + ("unrelated" if missing_table else "child_state") + " (record_json TEXT)")
            producer.execute("INSERT INTO " + ("unrelated" if missing_table else "child_state") + " VALUES (?)", (json.dumps({"phase": "running", "pid": 17}),))
        else:
            for table in ("authority", "grants", "access_tokens", "refresh_tokens"):
                producer.execute("CREATE TABLE " + ("unrelated" if missing_table and table == "grants" else table) + " (key TEXT)")
        producer.commit()
    database.chmod(0o600)
    before = database.read_bytes()
    _assert_no_sidecars(database)
    if reader == "child":
        generation_root = tmp_path / "generation"
        generation_root.mkdir(mode=0o700)
        root.rename(generation_root / "state")
        database = generation_root / "state" / database.name
        generation = Generation("original-A", generation_root, "a" * 64, [])

        def read():
            return Chain.child(generation)

    else:
        chain = Chain.__new__(Chain)
        chain.oauth = root
        read = chain.grants
    with _without_collection():
        if missing_table:
            with pytest.raises(sqlite3.OperationalError, match="no such table"):
                read()
        else:
            expected = {"phase": "running", "pid": 17} if reader == "child" else {name: [] for name in ("authority", "grants", "access_tokens", "refresh_tokens")}
            assert read() == expected
        assert not _open_database_handles(database)
        assert database.read_bytes() == before
        Chain.checkpoint(database)
        _assert_no_sidecars(database)
        assert database.read_bytes() == before


def test_checkpoint_refuses_a_missing_original_without_creating_it(tmp_path: Path) -> None:
    database = tmp_path / "missing-original.sqlite3"
    with pytest.raises(sqlite3.OperationalError, match="unable to open database file"):
        Chain.checkpoint(database)
    assert list(tmp_path.iterdir()) == []


def test_failed_checkpoint_closes_the_original_connection(tmp_path: Path) -> None:
    database = tmp_path / "corrupt-original.sqlite3"
    database.write_bytes(b"retained invalid original database")
    database.chmod(0o600)
    with _without_collection():
        with pytest.raises(sqlite3.DatabaseError, match="file is not a database"):
            Chain.checkpoint(database)
        _assert_no_sidecars(database)
        assert database.read_bytes() == b"retained invalid original database"


@pytest.mark.parametrize("reader", ["capture", "replacement"])
@pytest.mark.parametrize("missing_table", [False, True])
def test_actual_host_presence_reader_releases_sqlite_on_success_and_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reader: str, missing_table: bool) -> None:
    """Only Docker inspection is supplied; files, process, locks and SQLite are real."""
    intent = replace(_intent(tmp_path), state_root="/management-child-state", journal_root="/management-operations", oauth_root="/management-oauth")
    config_root = tmp_path / "bootstrap"
    config_root.mkdir(mode=0o700)
    config = config_root / "config.json"
    config.write_text(
        json.dumps(
            {
                "instance_id": intent.new_instance,
                "state_root": intent.state_root,
                "journal_root": intent.journal_root,
                "oauth_root": intent.oauth_root,
                "listener_url": "http://127.0.0.1:8081/mcp",
                "initial_runtime": "bootstrap",
                "runtimes": [{"reference": "bootstrap", "receipt": "sha256:" + "b" * 64, "executable": "/usr/bin/python3", "arguments": []}],
            }
        )
    )
    config.chmod(0o600)
    lock = tmp_path / "prior-owner.lock"
    lock.write_bytes(b"")
    lock.chmod(0o600)
    intent = replace(intent, bootstrap_config_sha256=hashlib.sha256(config.read_bytes()).hexdigest(), owner_lock_device=lock.stat().st_dev, owner_lock_inode=lock.stat().st_ino)
    pid = os.getpid()
    token = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    incarnation = str(uuid.uuid4())
    ledger = Path(intent.host_state_root) / "management-child.sqlite3"
    with closing(sqlite3.connect(ledger)) as producer:
        assert producer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        table = "unrelated" if missing_table else "supervisor_presence"
        producer.execute(f"CREATE TABLE {table}(instance_id TEXT, incarnation TEXT, pid INTEGER, start_token TEXT)")
        producer.execute(f"INSERT INTO {table} VALUES (?, ?, ?, ?)", (intent.new_instance, incarnation, 1, token))
        producer.commit()
    ledger.chmod(0o600)
    mounts = [
        (intent.host_state_root, intent.state_root, True),
        (intent.host_journal_root, intent.journal_root, True),
        (intent.host_oauth_root, intent.oauth_root, True),
        (str(config_root), "/bootstrap", False),
        (str(tmp_path / "guard"), "/management-initialization", False),
        (str(lock), "/management-prior-owner.lock", False),
    ]
    values = [
        intent.new_container_id,
        intent.image_digest,
        True,
        pid,
        "running",
        False,
        "",
        None,
        None,
        "bridge",
        None,
        [{"Type": "bind", "Source": source, "Destination": destination, "RW": writable} for source, destination, writable in mounts],
        True,
        "original-start",
        0,
        ["python"],
        [],
        {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(intent.host_listener_port)}]},
    ]
    inspected = []

    def selected_inspection(container_id):
        assert container_id == intent.new_container_id
        inspected.append(container_id)
        return values

    monkeypatch.setattr(observation, "_inspect", selected_inspection)
    before = ledger.read_bytes()
    identity = ledger.stat()
    with lock.open("rb") as fence, _without_collection():
        fcntl.flock(fence, fcntl.LOCK_SH | fcntl.LOCK_NB)

        def read():
            if reader == "replacement":
                return observation.assert_replacement_owner(intent, incarnation, pid, fence.fileno(), tmp_path / "guard")
            prior = observation.PriorContainer("fixture-engine", intent.new_container_id, intent.image_digest, "original-start", 0, pid, token, "pid:[1]", "net:[2]", "bridge", "a" * 64, "/fixture", 1, 1, (observation.PriorTask(pid, token),))
            return observation._capture_supervisor_identity(prior, values, tuple(mounts), config)

        if missing_table:
            with pytest.raises((sqlite3.OperationalError, observation.HostObservationUnavailable)) as error:
                read()
            cause = error.value if reader == "capture" else error.value.__cause__
            assert isinstance(cause, sqlite3.OperationalError) and "no such table" in str(cause)
        else:
            result = read()
            if reader == "capture":
                assert result.instance_id == intent.new_instance and result.pid == pid and result.start_token == token
            else:
                assert result is None and len(inspected) == 2
        assert not _open_database_handles(ledger)
        after = ledger.stat()
        assert (after.st_dev, after.st_ino, after.st_uid, stat.S_IMODE(after.st_mode)) == (identity.st_dev, identity.st_ino, identity.st_uid, 0o600)
        assert ledger.read_bytes() == before
        Chain.checkpoint(ledger)
        _assert_no_sidecars(ledger)
        assert ledger.read_bytes() == before
