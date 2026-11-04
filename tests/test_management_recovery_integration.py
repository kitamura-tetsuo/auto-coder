"""Local CLI and real Supervisor recovery with a protected HTTP child."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from auto_coder.management.bootstrap_store import ChildPhase
from auto_coder.management.bootstrap_supervisor import ManagementBootstrapSupervisor
from auto_coder.management.recovery_mailbox import RecoveryMailbox
from tests.test_management_self_update_integration import ManagementSystem, _effect_counts, _run, _wait, management_system  # noqa: F401

pytestmark = [pytest.mark.supervisor_integration, pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")]


def _launch_history(system: ManagementSystem) -> list[int]:
    return [int(value) for value in (system.root / "oauth" / "fixture-launch-history").read_text().splitlines()]


@pytest.mark.parametrize("management_system", ["recovery", "recovery_installed"], indirect=True)
def test_real_local_cli_competing_recoveries_and_authenticated_readback(management_system: ManagementSystem) -> None:
    system = management_system
    token = system.issue(uid="other")
    old = system.child
    assert old.selected_runtime == system.bootstrap.initial_runtime
    assert _launch_history(system) == [old.pid]
    os.kill(old.pid, signal.SIGKILL)
    stopped = _wait(lambda: (child if (child := system.child).phase is ChildPhase.NEEDS_ATTENTION else None), timeout=20, description="actual child exit and retained stopped state")
    assert not ManagementBootstrapSupervisor._start_token(old.pid)
    assert stopped.child_generation == old.child_generation
    mailbox = RecoveryMailbox(system.bootstrap.state_root, system.bootstrap.instance_id)
    assert mailbox.path.exists()
    requests = [str(uuid.uuid4()), str(uuid.uuid4())]

    def submit(request_id: str, stopped=stopped) -> dict[str, object]:
        response = _run(
            [
                sys.executable,
                "-m",
                "auto_coder.cli",
                "management",
                "recover",
                "--config",
                str(system.config_path),
                "--request-id",
                request_id,
                "--expected-supervisor",
                stopped.supervisor_incarnation,
                "--expected-generation",
                str(stopped.child_generation),
                "--expected-runtime",
                stopped.selected_runtime,
                "--expected-receipt",
                stopped.runtime_receipt,
                "--expected-source-receipt",
                stopped.source_receipt,
            ],
            system.root,
            timeout=20,
        )
        return json.loads(response.splitlines()[-1])

    # Both real CLI requests are durably pending before the journal fence is
    # released, so admission ordering is witnessed rather than inferred.
    journal = sqlite3.connect(system.root / "operations" / "management-operations.sqlite3", timeout=5, isolation_level=None)
    journal.execute("BEGIN IMMEDIATE")
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            submitted = list(pool.map(submit, requests))
        assert all(item["status"] == "pending" for item in submitted)
        assert all(mailbox.get(request_id).status == "pending" for request_id in requests)
    finally:
        journal.rollback()
        journal.close()
    claimed = _wait(lambda: next((item for request_id in requests if (item := mailbox.get(request_id)).status == "launched"), None), timeout=30, description="one recovered child launch")
    status_output = _run([sys.executable, "-m", "auto_coder.cli", "management", "recovery-status", "--config", str(system.config_path), "--request-id", claimed.intent.request_id], system.root, timeout=20)
    status = json.loads(status_output.splitlines()[-1])
    assert status["status"] == "launched" and status["observation_id"] == claimed.observation_id
    other_id = next(request_id for request_id in requests if request_id != claimed.intent.request_id)
    assert _wait(lambda: mailbox.get(other_id).status == "refused", timeout=20, description="competing request refusal")
    system.wait_serving(runtime=old.selected_runtime)
    new = system.child
    assert new.child_generation == old.child_generation + 1
    assert (new.pid, new.start_token) != (old.pid, old.start_token)
    assert ManagementBootstrapSupervisor._start_token(new.pid) == new.start_token
    assert _launch_history(system) == [old.pid, new.pid]
    assert claimed.child_identity == ManagementBootstrapSupervisor._identity(new)
    assert len([row for row in _effect_counts(system) if row[0] == "RECOVER_SELECTED"]) == 1
    before = _effect_counts(system)
    assert system.request(None, "auto_coder_whoami", {"observation_id": claimed.observation_id}).status_code == 401
    assert system.call(token, "auto_coder_whoami", {"observation_id": claimed.observation_id})["uid"] == "other"
    completed = _wait(lambda: (item if (item := mailbox.get(claimed.intent.request_id)).status == "succeeded" else None), timeout=30, description="authenticated readiness")
    assert completed.child_identity == ManagementBootstrapSupervisor._identity(new)
    message = "Management local recovery outcome request=" + completed.intent.request_id + " status=succeeded"
    assert _wait(lambda: message in (system.root / "supervisor.log").read_text(), timeout=10, description="local recovery outcome log")
    assert submit(completed.intent.request_id)["status"] == "succeeded"
    assert _effect_counts(system) == before
    assert system.child == new
    assert _launch_history(system) == [old.pid, new.pid]
    assert not system.journal.list_operations().records
    with sqlite3.connect(system.root / "supervisor" / "management-self-update.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0] == 0

    # A proven recovery closes the old episode; a later death of that exact
    # new lifetime may be recovered once under its own generation.
    os.kill(new.pid, signal.SIGKILL)
    later = _wait(lambda: (child if (child := system.child).phase is ChildPhase.NEEDS_ATTENTION and child.child_generation == new.child_generation else None), timeout=20, description="recovered child exit")
    assert not ManagementBootstrapSupervisor._start_token(new.pid)
    assert submit(completed.intent.request_id)["status"] == "succeeded"
    stale_id = str(uuid.uuid4())
    assert submit(stale_id)["status"] in {"pending", "refused"}
    assert _wait(lambda: mailbox.get(stale_id).status == "refused", timeout=20, description="old generation refusal")
    assert _launch_history(system) == [old.pid, new.pid]
    next_id = str(uuid.uuid4())
    assert submit(next_id, later)["status"] in {"pending", "claimed", "launched"}
    next_launch = _wait(lambda: (item if (item := mailbox.get(next_id)).status == "launched" else None), timeout=30, description="later generation launch")
    system.wait_serving(runtime=old.selected_runtime)
    latest = system.child
    assert latest.child_generation == new.child_generation + 1
    assert _launch_history(system) == [old.pid, new.pid, latest.pid]
    assert [row for row in _effect_counts(system) if row[0] == "RECOVER_SELECTED"] == [("RECOVER_SELECTED", 2)]
    assert system.call(token, "auto_coder_whoami", {"observation_id": next_launch.observation_id})["uid"] == "other"
    assert _wait(lambda: mailbox.get(next_id).status == "succeeded", timeout=30, description="later authenticated readiness")
    assert submit(next_id, later)["status"] == "succeeded"
    assert _launch_history(system) == [old.pid, new.pid, latest.pid]


@pytest.mark.parametrize("boundary", ["before_claim", "after_claim", "after_launch"])
@pytest.mark.parametrize("management_system", ["recovery"], indirect=True)
def test_real_owner_crash_and_replay_never_launch_twice(management_system: ManagementSystem, boundary: str) -> None:
    system = management_system
    token = system.issue()
    old = system.child
    assert _launch_history(system) == [old.pid]
    os.kill(old.pid, signal.SIGKILL)
    stopped = _wait(lambda: (child if (child := system.child).phase is ChildPhase.NEEDS_ATTENTION else None), timeout=20, description="stopped child")
    assert not ManagementBootstrapSupervisor._start_token(old.pid)
    mailbox = RecoveryMailbox(system.bootstrap.state_root, system.bootstrap.instance_id)
    request_id = str(uuid.uuid4())

    def submit(key: str, incarnation: str) -> dict[str, object]:
        response = _run(
            [
                sys.executable,
                "-m",
                "auto_coder.cli",
                "management",
                "recover",
                "--config",
                str(system.config_path),
                "--request-id",
                key,
                "--expected-supervisor",
                incarnation,
                "--expected-generation",
                str(stopped.child_generation),
                "--expected-runtime",
                stopped.selected_runtime,
                "--expected-receipt",
                stopped.runtime_receipt,
                "--expected-source-receipt",
                stopped.source_receipt,
            ],
            system.root,
            timeout=20,
        )
        return json.loads(response.splitlines()[-1])

    journal_lock = None
    child_path = system.root / "supervisor" / "management-child.sqlite3"
    if boundary == "before_claim":
        journal_lock = sqlite3.connect(system.root / "operations" / "management-operations.sqlite3", isolation_level=None)
        journal_lock.execute("BEGIN IMMEDIATE")
    if boundary == "after_claim":
        with sqlite3.connect(child_path) as connection:
            connection.execute("CREATE TRIGGER reject_recovery_effect BEFORE INSERT ON effects WHEN NEW.action='RECOVER_SELECTED' BEGIN SELECT RAISE(ABORT, 'effect barrier'); END")
    try:
        assert submit(request_id, stopped.supervisor_incarnation)["status"] in {"pending", "claimed", "launched", "unknown"}
        expected = {"before_claim": {"pending"}, "after_claim": {"claimed", "unknown"}, "after_launch": {"launched"}}[boundary]
        record = _wait(lambda: (item if (item := mailbox.get(request_id)).status in expected else None), timeout=30, description=f"durable {boundary} barrier")
        if boundary == "after_launch":
            system.wait_serving(runtime="bootstrap")
            assert system.child.child_generation == old.child_generation + 1
            assert ManagementBootstrapSupervisor._start_token(system.child.pid) == system.child.start_token
        else:
            assert system.child.child_generation == old.child_generation
        before = _effect_counts(system)
        assert system.supervisor is not None
        system.supervisor.kill()
        system.supervisor.wait(timeout=10)
    finally:
        if journal_lock is not None:
            journal_lock.rollback()
            journal_lock.close()
        if boundary == "after_claim":
            with sqlite3.connect(child_path) as connection:
                connection.execute("DROP TRIGGER reject_recovery_effect")
    system.supervisor_log.close()
    system.start(wait=False)
    with sqlite3.connect(child_path) as connection:
        previous_owner = stopped.supervisor_incarnation
        new_owner = _wait(lambda: (row[0] if (row := connection.execute("SELECT incarnation FROM supervisor_presence").fetchone()) and row[0] != previous_owner else None), timeout=20, description="reconstructed Supervisor")
    replay = submit(request_id, stopped.supervisor_incarnation)
    assert replay["status"] in {record.status, "refused", "unknown"}
    alternate = str(uuid.uuid4())
    assert submit(alternate, new_owner)["status"] in {"pending", "refused"}
    assert _wait(lambda: mailbox.get(alternate).status == "refused", timeout=20, description="alternate request refusal")
    assert _effect_counts(system) == before
    if boundary == "after_launch":
        current = system.child
        assert current.child_generation == old.child_generation + 1
        assert ManagementBootstrapSupervisor._start_token(current.pid) == current.start_token
        assert _launch_history(system) == [old.pid, current.pid]
        assert system.request(None, "auto_coder_whoami").status_code == 401
        assert system.call(token, "auto_coder_whoami")["uid"] == "operator"
    else:
        assert system.child.child_generation == old.child_generation
        assert _launch_history(system) == [old.pid]
        assert not [item for item in before if item[0] == "RECOVER_SELECTED"]


@pytest.mark.parametrize("management_system", ["recovery_plain"], indirect=True)
def test_real_cli_refuses_live_stale_missing_corrupt_listener_and_changed_runtime(management_system: ManagementSystem) -> None:
    import socket

    system = management_system
    mailbox = RecoveryMailbox(system.bootstrap.state_root, system.bootstrap.instance_id)
    old = system.child
    initial_effects = _effect_counts(system)
    assert _launch_history(system) == [old.pid]

    def refuse(*, incarnation: str | None = None, source_receipt: str | None = None) -> None:
        request_id = str(uuid.uuid4())
        response = _run(
            [
                sys.executable,
                "-m",
                "auto_coder.cli",
                "management",
                "recover",
                "--config",
                str(system.config_path),
                "--request-id",
                request_id,
                "--expected-supervisor",
                incarnation or old.supervisor_incarnation,
                "--expected-generation",
                str(old.child_generation),
                "--expected-runtime",
                old.selected_runtime,
                "--expected-receipt",
                old.runtime_receipt,
                "--expected-source-receipt",
                old.source_receipt if source_receipt is None else source_receipt,
            ],
            system.root,
            timeout=20,
        )
        assert json.loads(response.splitlines()[-1])["status"] in {"pending", "refused"}
        assert _wait(lambda: mailbox.get(request_id).status == "refused", timeout=20, description="recovery refusal")
        assert _effect_counts(system) == initial_effects
        assert _launch_history(system) == [old.pid]
        assert not [item for item in _effect_counts(system) if item[0] == "RECOVER_SELECTED"]

    assert ManagementBootstrapSupervisor._start_token(old.pid) == old.start_token
    refuse()  # a real live child and listener remain present
    assert ManagementBootstrapSupervisor._start_token(old.pid) == old.start_token
    os.kill(old.pid, signal.SIGKILL)
    _wait(lambda: system.child.phase is ChildPhase.NEEDS_ATTENTION, timeout=20, description="child stopped")
    assert not ManagementBootstrapSupervisor._start_token(old.pid)
    refuse(incarnation="stale-owner")
    refuse(source_receipt="different-source")
    journal = system.root / "operations" / "management-operations.sqlite3"
    handoff = system.root / "supervisor" / "management-self-update.sqlite3"
    for path in (journal, handoff):
        backup = path.with_suffix(".saved")
        path.rename(backup)
        try:
            refuse()
            assert not path.exists()
        finally:
            backup.rename(path)
    with sqlite3.connect(handoff) as connection:
        connection.execute("UPDATE metadata SET value='wrong' WHERE key='schema_version'")
    try:
        refuse()
        with sqlite3.connect(handoff) as connection:
            assert connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone() == ("wrong",)
    finally:
        with sqlite3.connect(handoff) as connection:
            connection.execute("UPDATE metadata SET value='1' WHERE key='schema_version'")
    port = int(system.origin.rsplit(":", 1)[1])
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen()
        refuse()
        assert listener.getsockname()[1] == port
    initial = next(item for item in system.bootstrap.runtimes if item.reference == "bootstrap")
    original = initial.executable.read_bytes()
    try:
        initial.executable.write_bytes(original + b"changed")
        refuse()
    finally:
        initial.executable.write_bytes(original)
    assert system.child.child_generation == old.child_generation
    assert not system.journal.list_operations().records


@pytest.mark.parametrize("boundary", ["startup_failure", "unverified_readiness", "bind_race"])
@pytest.mark.parametrize("management_system", ["recovery_plain"], indirect=True)
def test_real_start_failure_or_missing_authenticated_proof_never_retries(management_system: ManagementSystem, boundary: str) -> None:
    system = management_system
    old = system.child
    assert _launch_history(system) == [old.pid]
    os.kill(old.pid, signal.SIGKILL)
    stopped = _wait(lambda: (child if (child := system.child).phase is ChildPhase.NEEDS_ATTENTION else None), timeout=20, description="child exit")
    if boundary == "startup_failure":
        (system.root / "oauth" / "fixture-fail-start").touch()
    if boundary == "bind_race":
        (system.root / "oauth" / "fixture-hold-bind").touch()
    mailbox = RecoveryMailbox(system.bootstrap.state_root, system.bootstrap.instance_id)

    def submit(request_id: str) -> dict[str, object]:
        response = _run(
            [
                sys.executable,
                "-m",
                "auto_coder.cli",
                "management",
                "recover",
                "--config",
                str(system.config_path),
                "--request-id",
                request_id,
                "--expected-supervisor",
                stopped.supervisor_incarnation,
                "--expected-generation",
                str(stopped.child_generation),
                "--expected-runtime",
                stopped.selected_runtime,
                "--expected-receipt",
                stopped.runtime_receipt,
                "--expected-source-receipt",
                stopped.source_receipt,
            ],
            system.root,
            timeout=20,
        )
        return json.loads(response.splitlines()[-1])

    request_id = str(uuid.uuid4())
    assert submit(request_id)["status"] in {"pending", "claimed", "launched", "unknown"}
    if boundary == "startup_failure":
        _wait(lambda: (system.root / "oauth" / "fixture-failed-launches").exists(), timeout=30, description="actual failed child start")
        result = _wait(lambda: (item if (item := mailbox.get(request_id)).status in {"claimed", "unknown"} else None), timeout=20, description="non-successful start")
        assert (system.root / "oauth" / "fixture-failed-launches").read_text().splitlines() == ["launch"]
    elif boundary == "bind_race":
        import socket

        launched = _wait(lambda: (item if (item := mailbox.get(request_id)).status == "launched" else None), timeout=30, description="recovered child launched")
        entered = system.root / "oauth" / "fixture-entered-bind"
        _wait(entered.exists, timeout=20, description="child held before bind")
        child_pid = int(entered.read_text())
        assert ManagementBootstrapSupervisor._start_token(child_pid)
        port = int(system.origin.rsplit(":", 1)[1])
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", port))
            listener.listen()
            (system.root / "oauth" / "fixture-hold-bind").unlink()
            result = _wait(lambda: (item if (item := mailbox.get(request_id)).status == "unknown" else None), timeout=30, description="failed bind observed")
            assert not ManagementBootstrapSupervisor._start_token(child_pid)
            assert listener.getsockname()[1] == port
        assert result.child_identity == launched.child_identity
    else:
        launched = _wait(lambda: (item if (item := mailbox.get(request_id)).status == "launched" else None), timeout=30, description="recovered child launched")
        system.wait_serving(runtime="bootstrap")
        assert system.request(None, "auto_coder_whoami", {"observation_id": launched.observation_id}).status_code == 401
        result = _wait(lambda: (item if (item := mailbox.get(request_id)).status == "unknown" else None), timeout=50, description="unverified readback deadline")
        assert result.child_identity == launched.child_identity
    before = _effect_counts(system)
    replay_status = submit(request_id)["status"]
    assert replay_status in ({"claimed", "unknown"} if boundary == "startup_failure" else {"unknown"})
    second_id = str(uuid.uuid4())
    assert submit(second_id)["status"] in {"pending", "refused"}
    assert _wait(lambda: mailbox.get(second_id).status == "refused", timeout=20, description="second request blocked")
    assert _effect_counts(system) == before
    if boundary == "startup_failure":
        assert (system.root / "oauth" / "fixture-failed-launches").read_text().splitlines() == ["launch"]
        assert len(_launch_history(system)) == 2
    elif boundary == "bind_race":
        assert result.status == "unknown"
        assert _launch_history(system) == [old.pid, child_pid]
    else:
        assert result.status == "unknown"
        assert _launch_history(system) == [old.pid, system.child.pid]


@pytest.mark.parametrize("management_system", ["recovery"], indirect=True)
def test_running_owner_does_not_recreate_missing_handoff_evidence(management_system: ManagementSystem) -> None:
    system = management_system
    old = system.child
    assert _launch_history(system) == [old.pid]
    os.kill(old.pid, signal.SIGKILL)
    stopped = _wait(lambda: (child if (child := system.child).phase is ChildPhase.NEEDS_ATTENTION else None), timeout=20, description="child exit")
    assert not ManagementBootstrapSupervisor._start_token(old.pid)
    before = _effect_counts(system)
    path = system.root / "supervisor" / "management-self-update.sqlite3"
    backup = path.with_suffix(".saved")
    path.rename(backup)
    try:
        _wait(lambda: "handoff evidence is unavailable" in (system.root / "supervisor.log").read_text(), timeout=20, description="owner noticed missing handoff evidence")
        assert not path.exists()
        request_id = str(uuid.uuid4())
        response = _run(
            [
                sys.executable,
                "-m",
                "auto_coder.cli",
                "management",
                "recover",
                "--config",
                str(system.config_path),
                "--request-id",
                request_id,
                "--expected-supervisor",
                stopped.supervisor_incarnation,
                "--expected-generation",
                str(stopped.child_generation),
                "--expected-runtime",
                stopped.selected_runtime,
                "--expected-receipt",
                stopped.runtime_receipt,
                "--expected-source-receipt",
                stopped.source_receipt,
            ],
            system.root,
            timeout=20,
        )
        assert json.loads(response.splitlines()[-1])["status"] in {"pending", "refused"}
        mailbox = RecoveryMailbox(system.bootstrap.state_root, system.bootstrap.instance_id)
        assert _wait(lambda: mailbox.get(request_id).status == "refused", timeout=20, description="missing handoff refusal")
        assert not path.exists()
        assert _effect_counts(system) == before
        assert system.child.child_generation == old.child_generation
    finally:
        backup.rename(path)


@pytest.mark.parametrize("management_system", ["recovery"], indirect=True)
def test_reconstructed_owner_does_not_replace_missing_claimed_mailbox(management_system: ManagementSystem) -> None:
    system = management_system
    old = system.child
    assert _launch_history(system) == [old.pid]
    os.kill(old.pid, signal.SIGKILL)
    stopped = _wait(lambda: (child if (child := system.child).phase is ChildPhase.NEEDS_ATTENTION else None), timeout=20, description="child exit")
    child_path = system.root / "supervisor" / "management-child.sqlite3"
    with sqlite3.connect(child_path) as connection:
        connection.execute("CREATE TRIGGER reject_recovery_effect BEFORE INSERT ON effects WHEN NEW.action='RECOVER_SELECTED' BEGIN SELECT RAISE(ABORT, 'effect barrier'); END")
    request_id = str(uuid.uuid4())
    response = _run(
        [
            sys.executable,
            "-m",
            "auto_coder.cli",
            "management",
            "recover",
            "--config",
            str(system.config_path),
            "--request-id",
            request_id,
            "--expected-supervisor",
            stopped.supervisor_incarnation,
            "--expected-generation",
            str(stopped.child_generation),
            "--expected-runtime",
            stopped.selected_runtime,
            "--expected-receipt",
            stopped.runtime_receipt,
            "--expected-source-receipt",
            stopped.source_receipt,
        ],
        system.root,
        timeout=20,
    )
    assert json.loads(response.splitlines()[-1])["status"] in {"pending", "claimed", "unknown"}
    mailbox = RecoveryMailbox(system.bootstrap.state_root, system.bootstrap.instance_id)
    assert _wait(lambda: mailbox.get(request_id).status in {"claimed", "unknown"}, timeout=20, description="durable recovery claim")
    before = _effect_counts(system)
    assert system.supervisor is not None
    system.supervisor.kill()
    system.supervisor.wait(timeout=10)
    system.supervisor_log.close()
    with sqlite3.connect(child_path) as connection:
        connection.execute("DROP TRIGGER reject_recovery_effect")
    backup = mailbox.path.with_suffix(".saved")
    mailbox.path.rename(backup)
    try:
        system.start(wait=False)
        _wait(lambda: "Management local recovery mailbox unavailable" in (system.root / "supervisor.log").read_text(), timeout=20, description="missing retained mailbox refusal")
        assert not mailbox.path.exists()
        assert system.supervisor is not None and system.supervisor.poll() is None
        assert system.child.child_generation == old.child_generation
        assert _launch_history(system) == [old.pid]
        assert _effect_counts(system) == before
        with sqlite3.connect(backup) as connection:
            assert connection.execute("SELECT status FROM recovery_requests WHERE request_id=?", (request_id,)).fetchone()[0] in {"claimed", "unknown"}
    finally:
        backup.rename(mailbox.path)


@pytest.mark.parametrize("management_system", ["recovery"], indirect=True)
def test_missing_child_ledger_is_not_recreated_by_owner_or_reconstruction(management_system: ManagementSystem) -> None:
    import subprocess

    system = management_system
    old = system.child
    assert _launch_history(system) == [old.pid]
    os.kill(old.pid, signal.SIGKILL)
    stopped = _wait(lambda: (child if (child := system.child).phase is ChildPhase.NEEDS_ATTENTION else None), timeout=20, description="child exit")
    assert not ManagementBootstrapSupervisor._start_token(old.pid)
    path = system.root / "supervisor" / "management-child.sqlite3"
    backup = path.with_suffix(".saved")
    path.rename(backup)
    try:
        _wait(lambda: "Management child ledger is unavailable" in (system.root / "supervisor.log").read_text(), timeout=20, description="owner observed missing ledger")
        assert not path.exists()
        request_id = str(uuid.uuid4())
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "auto_coder.cli",
                "management",
                "recover",
                "--config",
                str(system.config_path),
                "--request-id",
                request_id,
                "--expected-supervisor",
                stopped.supervisor_incarnation,
                "--expected-generation",
                str(stopped.child_generation),
                "--expected-runtime",
                stopped.selected_runtime,
                "--expected-receipt",
                stopped.runtime_receipt,
                "--expected-source-receipt",
                stopped.source_receipt,
            ],
            cwd=system.root,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode != 0
        assert not path.exists()
        assert system.supervisor is not None
        system.supervisor.kill()
        system.supervisor.wait(timeout=10)
        system.supervisor_log.close()
        system.start(wait=False)
        assert system.supervisor is not None
        assert system.supervisor.wait(timeout=20) != 0
        assert not path.exists()
        assert _launch_history(system) == [old.pid]
    finally:
        backup.rename(path)


@pytest.mark.parametrize("management_system", ["recovery_installed"], indirect=True)
def test_real_self_update_effect_waits_for_recovery_observation(management_system: ManagementSystem) -> None:
    from auto_coder.management.self_update_handoff import HandoffPhase, HandoffStore
    from tests.test_management_self_update_integration import INSTANCE, _update

    system = management_system
    token = system.issue()
    old = system.child
    assert _launch_history(system) == [old.pid]
    os.kill(old.pid, signal.SIGKILL)
    stopped = _wait(lambda: (child if (child := system.child).phase is ChildPhase.NEEDS_ATTENTION else None), timeout=20, description="child exit")
    request_id = str(uuid.uuid4())
    response = _run(
        [
            sys.executable,
            "-m",
            "auto_coder.cli",
            "management",
            "recover",
            "--config",
            str(system.config_path),
            "--request-id",
            request_id,
            "--expected-supervisor",
            stopped.supervisor_incarnation,
            "--expected-generation",
            str(stopped.child_generation),
            "--expected-runtime",
            stopped.selected_runtime,
            "--expected-receipt",
            stopped.runtime_receipt,
            "--expected-source-receipt",
            stopped.source_receipt,
        ],
        system.root,
        timeout=20,
    )
    assert json.loads(response.splitlines()[-1])["status"] in {"pending", "claimed", "launched"}
    mailbox = RecoveryMailbox(system.bootstrap.state_root, system.bootstrap.instance_id)
    launched = _wait(lambda: (item if (item := mailbox.get(request_id)).status == "launched" else None), timeout=60, description="recovery launch")
    system.wait_serving(runtime=old.selected_runtime)
    current = system.child
    assert _launch_history(system) == [old.pid, current.pid]
    assert current.child_generation == old.child_generation + 1
    operation = _update(system, token, "concurrent-self-update")
    assert operation["status"] == "ACCEPTED", operation
    operation_id = operation["operation_id"]
    store = HandoffStore(system.bootstrap.state_root, INSTANCE)
    handoff = _wait(lambda: store.read(operation_id), timeout=90, description="committed self-update handoff")
    assert handoff.phase is HandoffPhase.SUBMITTED
    time.sleep(0.5)
    assert store.read(operation_id).phase is HandoffPhase.SUBMITTED
    assert system.child.pid == current.pid
    assert ManagementBootstrapSupervisor._start_token(current.pid) == current.start_token
    assert not system.child.intended_stop
    assert _launch_history(system) == [old.pid, current.pid]
    effects = _effect_counts(system)
    handoff_lock = sqlite3.connect(store.path, isolation_level=None)
    handoff_lock.execute("BEGIN IMMEDIATE")
    try:
        system.call(token, "auto_coder_whoami", {"observation_id": launched.observation_id})
        assert _wait(lambda: mailbox.get(request_id).status == "succeeded", timeout=30, description="recovery readiness")
        assert _effect_counts(system) == effects
    finally:
        handoff_lock.rollback()
        handoff_lock.close()
