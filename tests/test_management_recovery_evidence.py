"""Read-only and kernel evidence at the management recovery boundary."""

from __future__ import annotations

import json
import signal
import socket
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from auto_coder.management.bootstrap_supervisor import ManagementBootstrapSupervisor, ResultStatus
from auto_coder.management.operation_store import ManagementOperationStore
from auto_coder.management.recovery_evidence import EvidenceUnavailable, PreviousLifetime, assert_journal_clear, assert_kernel_absence, read_child_evidence
from auto_coder.management.self_update_handoff import HandoffStore
from tests.test_management_bootstrap_supervisor import _await_child_ready, _runtime, _supervisor


@pytest.fixture
def _use_real_sleep() -> None:
    pass


@pytest.fixture
def _use_real_commands() -> None:
    pass


def test_missing_recovery_evidence_does_not_create_databases(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    supervisor = _supervisor(tmp_path, runtime)
    with pytest.raises(EvidenceUnavailable):
        read_child_evidence(supervisor.config)
    with pytest.raises(EvidenceUnavailable):
        assert_journal_clear(supervisor.config)
    assert not supervisor.store.path.exists()
    assert not (supervisor.config.journal_root / "management-operations.sqlite3").exists()
    assert not (supervisor.config.state_root / "management-self-update.sqlite3").exists()


def test_real_previous_lifetime_and_listener_observation(tmp_path: Path, _use_real_commands: None, _use_real_sleep: None) -> None:
    runtime = _runtime(tmp_path)
    supervisor = _supervisor(tmp_path, runtime)
    started = supervisor.start_selected("start", "attempt", runtime.reference)
    _await_child_ready(runtime)
    try:
        with socket.socket() as unused:
            unused.bind(("127.0.0.1", 0))
            free_port = unused.getsockname()[1]
        free_config = replace(supervisor.config, listener_url=f"http://127.0.0.1:{free_port}/mcp")
        live = started.record
        live_evidence = PreviousLifetime(live.pid, live.start_token, live.child_generation, live.selected_runtime, live.supervisor_incarnation, live.drain_nonce, live.runtime_receipt, live.source_receipt)
        assert ManagementBootstrapSupervisor._start_token(live.pid) == live.start_token
        with pytest.raises(EvidenceUnavailable):
            assert_kernel_absence(free_config, live_evidence)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            config = replace(supervisor.config, listener_url=f"http://127.0.0.1:{listener.getsockname()[1]}/mcp")
            supervisor._children[started.record.pid].send_signal(signal.SIGKILL)
            supervisor._children[started.record.pid].wait(timeout=5)
            supervisor.inspect()
            child, previous = read_child_evidence(config)
            assert child.child_generation == started.record.child_generation
            assert (previous.pid, previous.start_token) == (started.record.pid, started.record.start_token)
            assert not ManagementBootstrapSupervisor._start_token(previous.pid)
            with pytest.raises(EvidenceUnavailable):
                assert_kernel_absence(config, previous)
        assert_kernel_absence(config, previous)
        with sqlite3.connect(supervisor.store.path) as connection:
            original_child = connection.execute("SELECT record_json FROM child_state WHERE instance_id=?", (config.instance_id,)).fetchone()[0]
            incomplete = json.loads(original_child)
            del incomplete["recovery_used"]
            connection.execute("UPDATE child_state SET record_json=? WHERE instance_id=?", (json.dumps(incomplete), config.instance_id))
        with pytest.raises(EvidenceUnavailable):
            read_child_evidence(config)
        with sqlite3.connect(supervisor.store.path) as connection:
            connection.execute("UPDATE child_state SET record_json=? WHERE instance_id=?", (original_child, config.instance_id))
        with sqlite3.connect(supervisor.store.path) as connection:
            original = connection.execute("SELECT result_json FROM effects WHERE operation_id='start'").fetchone()[0]
            corrupted = json.loads(original)
            corrupted["record"]["start_token"] = "not-a-kernel-token"
            connection.execute("UPDATE effects SET result_json=? WHERE operation_id='start'", (json.dumps(corrupted),))
        with pytest.raises(EvidenceUnavailable):
            read_child_evidence(config)
        with sqlite3.connect(supervisor.store.path) as connection:
            connection.execute("UPDATE effects SET result_json=? WHERE operation_id='start'", (original,))
        with pytest.raises(EvidenceUnavailable):
            assert_kernel_absence(config, replace(previous, pid=-1))
        for index in range(55):
            operation = f"observed-{index}"
            supervisor.store.begin_effect(operation, "attempt", "INSPECT", runtime.reference, "")
            supervisor._finish(operation, ResultStatus.STOPPED, child)
        supervisor.store.begin_effect("later-prepared", "attempt", "START_SELECTED", runtime.reference, "")
        with pytest.raises(EvidenceUnavailable):
            read_child_evidence(config)
        with sqlite3.connect(supervisor.store.path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM effects").fetchone()[0] == 57
    finally:
        child_process = supervisor._children[started.record.pid]
        if child_process.poll() is None:
            child_process.kill()
            child_process.wait(timeout=5)


def test_existing_store_schema_is_checked_without_repair(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    supervisor = _supervisor(tmp_path, runtime)
    journal = ManagementOperationStore(supervisor.config.journal_root)
    journal_connection = journal._connect()
    journal_connection.close()
    store = HandoffStore(supervisor.config.state_root, supervisor.config.instance_id)
    handoff_connection = store._connect()
    handoff_connection.close()
    assert_journal_clear(supervisor.config)
    mode = store.path.stat().st_mode & 0o777
    store.path.chmod(0)
    try:
        with pytest.raises(EvidenceUnavailable):
            assert_journal_clear(supervisor.config)
    finally:
        store.path.chmod(mode)
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE metadata SET value='wrong' WHERE key='schema_version'")
    with pytest.raises(EvidenceUnavailable):
        assert_journal_clear(supervisor.config)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone() == ("wrong",)
        connection.execute("UPDATE metadata SET value='1' WHERE key='schema_version'")
    with sqlite3.connect(journal._path) as connection:
        connection.execute("UPDATE metadata SET value='wrong' WHERE key='schema_version'")
    with pytest.raises(EvidenceUnavailable):
        assert_journal_clear(supervisor.config)
    with sqlite3.connect(journal._path) as connection:
        assert connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone() == ("wrong",)


def test_unsettled_management_operation_beyond_first_page_refuses(tmp_path: Path) -> None:
    from auto_coder.management.operations import ActionParameter, ManagedTarget, OperationIntent
    from tests.test_management_operation_store import authority

    runtime = _runtime(tmp_path)
    supervisor = _supervisor(tmp_path, runtime)
    journal = ManagementOperationStore(supervisor.config.journal_root)
    for index in range(55):
        intent = OperationIntent(ManagedTarget("other", f"worker-{index}", "owner/repo"), "restart", (ActionParameter("digest", "one"),), f"key-{index}")
        assert journal.admit(intent, authority()).record is not None
    management_intent = OperationIntent(ManagedTarget(supervisor.config.instance_id, "management", "kitamura-tetsuo/auto-coder"), "management-service-self-update", (), "later")
    assert journal.admit(management_intent, authority()).record is not None
    store = HandoffStore(supervisor.config.state_root, supervisor.config.instance_id)
    with store._connect():
        pass
    with pytest.raises(EvidenceUnavailable):
        assert_journal_clear(supervisor.config)
    with sqlite3.connect(journal._path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 56


def test_unsettled_handoff_beyond_first_page_refuses(tmp_path: Path) -> None:
    from auto_coder.management.self_update_handoff import HandoffPhase
    from tests.test_management_self_update_supervisor import _handoff

    runtime = _runtime(tmp_path)
    supervisor = _supervisor(tmp_path, runtime)
    journal = ManagementOperationStore(supervisor.config.journal_root)
    with journal._connect():
        pass
    store = HandoffStore(supervisor.config.state_root, supervisor.config.instance_id)
    for index in range(55):
        entry = store.submit(_handoff(f"completed-{index}"))
        assert store.change(entry, phase=HandoffPhase.DENIED) is not None
    assert store.submit(_handoff("later")).phase is HandoffPhase.SUBMITTED
    with pytest.raises(EvidenceUnavailable):
        assert_journal_clear(supervisor.config)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0] == 56


def test_retained_mailbox_marker_prevents_recreation_or_replacement(tmp_path: Path) -> None:
    from auto_coder.management.recovery_mailbox import RecoveryMailbox

    runtime = _runtime(tmp_path)
    supervisor = _supervisor(tmp_path, runtime)
    supervisor.store.load()
    supervisor.config.state_root.chmod(0o700)
    mailbox = RecoveryMailbox(supervisor.config.state_root, supervisor.config.instance_id)
    mailbox.initialize()
    marker = mailbox._child_marker()
    assert marker is not None
    saved = mailbox.path.with_suffix(".saved")
    mailbox.path.rename(saved)
    with pytest.raises(sqlite3.DatabaseError, match="missing"):
        mailbox.initialize()
    assert not mailbox.path.exists()
    saved.rename(mailbox.path)
    with sqlite3.connect(mailbox.path) as connection:
        connection.execute("UPDATE metadata SET value='00000000-0000-4000-8000-000000000000' WHERE key='mailbox_id'")
    with pytest.raises(sqlite3.DatabaseError, match="match retained ledger"):
        mailbox.connect(readonly=True)
    with sqlite3.connect(mailbox.path) as connection:
        connection.execute("UPDATE metadata SET value=? WHERE key='mailbox_id'", (marker,))
    with sqlite3.connect(supervisor.store.path) as connection:
        connection.execute("DELETE FROM metadata WHERE key='recovery_mailbox_id'")
    with pytest.raises(sqlite3.DatabaseError, match="no retained ledger identity"):
        mailbox.initialize()
    assert mailbox._child_marker() is None


def test_claimed_recovery_fences_self_update_and_preserves_unknown_identity(tmp_path: Path) -> None:
    import fcntl
    import os
    import time
    import uuid

    from auto_coder.management.recovery_mailbox import RecoveryIntent
    from auto_coder.management.recovery_supervisor import RecoveryConsumer

    runtime = _runtime(tmp_path)
    supervisor = _supervisor(tmp_path, runtime)
    supervisor.store.load()
    supervisor.config.state_root.chmod(0o700)
    supervisor.store.publish_supervisor(supervisor.incarnation, os.getpid(), ManagementBootstrapSupervisor._start_token(os.getpid()))
    owner = (supervisor.config.state_root / ".supervisor-owner.lock").open("a+b")
    with pytest.raises(EvidenceUnavailable):
        RecoveryConsumer(supervisor, tmp_path / "bootstrap.json")
    assert not (supervisor.config.state_root / "management-recovery.sqlite3").exists()
    fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    consumer = RecoveryConsumer(supervisor, tmp_path / "bootstrap.json")
    intent = RecoveryIntent(str(uuid.uuid4()), supervisor.config.instance_id, supervisor.incarnation, 1, runtime.reference, runtime.receipt, runtime.source_receipt, str(tmp_path / "management.yml"), "a" * 64)
    consumer.mailbox.submit(intent)
    assert not consumer.blocks_self_update()
    assert consumer._set(intent.request_id, "pending", "claimed", "effect claimed")
    assert consumer.blocks_self_update()
    now = time.time()
    assert consumer._set(intent.request_id, "claimed", "launched", "launched", observation_id="a" * 32, child_identity="child", started=now, deadline=now + 30)
    assert consumer._set(intent.request_id, "launched", "unknown", "unavailable")
    retained = consumer.mailbox.get(intent.request_id)
    assert retained is not None
    assert (retained.status, retained.observation_id, retained.child_identity, retained.observation_started, retained.observation_deadline) == ("unknown", "a" * 32, "child", now, now + 30)
    assert consumer.blocks_self_update()
    with sqlite3.connect(consumer.mailbox.path) as connection:
        connection.execute("UPDATE recovery_requests SET intent_json='invalid'")
    assert consumer.blocks_self_update()

    owner.close()


def test_recovery_requires_current_supervisor_process(tmp_path: Path) -> None:
    import fcntl
    import os

    from auto_coder.management.recovery_evidence import assert_current_owner

    runtime = _runtime(tmp_path)
    supervisor = _supervisor(tmp_path, runtime)
    token = ManagementBootstrapSupervisor._start_token(os.getpid())
    supervisor.store.publish_supervisor(supervisor.incarnation, os.getpid(), token)
    lock_path = supervisor.config.state_root / ".supervisor-owner.lock"
    with lock_path.open("a+b") as owner:
        with pytest.raises(EvidenceUnavailable):
            assert_current_owner(supervisor.config, supervisor.incarnation)
        fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert_current_owner(supervisor.config, supervisor.incarnation)
        with pytest.raises(EvidenceUnavailable):
            assert_current_owner(supervisor.config, "other-incarnation")
        supervisor.store.publish_supervisor(supervisor.incarnation, os.getppid(), ManagementBootstrapSupervisor._start_token(os.getppid()))
        with pytest.raises(EvidenceUnavailable):
            assert_current_owner(supervisor.config, supervisor.incarnation)


def test_corrupt_operation_history_is_unavailable_without_repair(tmp_path: Path) -> None:
    from auto_coder.management.operations import EffectReceipt, MutationStatus, OperationPhase
    from tests.test_management_operation_store import authority, intent

    runtime = _runtime(tmp_path)
    supervisor = _supervisor(tmp_path, runtime)
    journal = ManagementOperationStore(supervisor.config.journal_root)
    entry = journal.admit(intent(), authority()).record
    assert entry is not None
    receipt = EffectReceipt("worker/1", "worker-journal", 3, "accepted_for_reevaluation")
    assert journal.transition(entry.operation_id, 0, OperationPhase.SUCCEEDED, evidence=receipt).status is MutationStatus.UPDATED
    store = HandoffStore(supervisor.config.state_root, supervisor.config.instance_id)
    with store._connect():
        pass
    assert_journal_clear(supervisor.config)
    with sqlite3.connect(journal._path) as connection:
        connection.execute("UPDATE operation_history SET receipt_json='not-json' WHERE revision=1")
    with pytest.raises(EvidenceUnavailable):
        assert_journal_clear(supervisor.config)
    with sqlite3.connect(journal._path) as connection:
        assert connection.execute("SELECT receipt_json FROM operation_history WHERE revision=1").fetchone() == ("not-json",)


def test_strict_child_store_does_not_create_or_repair_missing_ledger(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    supervisor = _supervisor(tmp_path, runtime)
    supervisor.store.load()
    supervisor.store.require_existing = True
    path = supervisor.store.path
    path.unlink()
    with pytest.raises(sqlite3.DatabaseError):
        supervisor.store.load()
    assert not path.exists()


def test_empty_child_record_with_retained_effect_is_not_a_fresh_boot(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    supervisor = _supervisor(tmp_path, runtime)
    empty = supervisor.store.load()
    assert supervisor._pristine_initial_state()
    supervisor.store.begin_effect("old-effect", "attempt", "START_SELECTED", runtime.reference, "")
    supervisor._finish("old-effect", ResultStatus.STOPPED, empty)
    assert not supervisor._pristine_initial_state()
    with sqlite3.connect(supervisor.store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM child_state").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM effects").fetchone()[0] == 1
