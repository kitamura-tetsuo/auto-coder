"""Real management child shutdown and owner reconstruction."""

import os
import signal
import sqlite3
import time

import pytest

from auto_coder.management.bootstrap_store import ChildPhase
from tests.test_management_self_update_integration import ManagementSystem, _wait, management_system

pytestmark = pytest.mark.supervisor_integration


@pytest.mark.parametrize("management_system", ["restart_clean"], indirect=True)
def test_clean_container_shutdown_restarts_exactly_once_with_retained_oauth(management_system: ManagementSystem) -> None:
    system = management_system
    token = system.issue()
    old = system.child
    supervisor = system.supervisor
    assert supervisor is not None
    assert system.call(token, "auto_coder_whoami")["uid"] == "operator"
    supervisor.send_signal(signal.SIGTERM)
    assert supervisor.wait(timeout=30) == 0
    stopped = system.child
    assert stopped.phase is ChildPhase.STOPPED and stopped.intended_stop
    assert stopped.effect_operation_id == "container-stop-" + old.supervisor_incarnation
    assert not os.path.exists(f"/proc/{old.pid}")
    system.start()
    new = system.child
    assert new.child_generation == old.child_generation + 1
    assert new.supervisor_incarnation != old.supervisor_incarnation
    assert new.selected_runtime == old.selected_runtime
    assert system.call(token, "auto_coder_whoami")["uid"] == "operator"
    with sqlite3.connect(system.root / "supervisor" / "management-child.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM effects WHERE action='START_SELECTED'").fetchone()[0] == 2
    time.sleep(0.3)
    assert system.child.pid == new.pid
    # An interrupted owner is not a clean stop and must never start another child.
    assert system.supervisor is not None
    system.supervisor.kill()
    system.supervisor.wait(timeout=10)
    os.kill(new.pid, signal.SIGKILL)
    _wait(lambda: not os.path.exists(f"/proc/{new.pid}"), timeout=10, description="killed test child exit")
    system.start(wait=False)
    time.sleep(1)
    assert system.child.phase is ChildPhase.UNKNOWN
    with sqlite3.connect(system.root / "supervisor" / "management-child.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM effects WHERE action='START_SELECTED'").fetchone()[0] == 2


@pytest.mark.parametrize("management_system", ["restart_clean"], indirect=True)
@pytest.mark.parametrize("damage", ["drain_receipt", "handoff_store", "unsettled_operation"])
def test_clean_restart_refuses_missing_or_conflicting_evidence(management_system: ManagementSystem, damage: str) -> None:
    system = management_system
    supervisor = system.supervisor
    assert supervisor is not None
    old = system.child
    supervisor.send_signal(signal.SIGTERM)
    assert supervisor.wait(timeout=30) == 0
    assert system.child.phase is ChildPhase.STOPPED
    if damage == "drain_receipt":
        (system.root / "drain-bootstrap.json").write_text("{}")
    elif damage == "handoff_store":
        (system.root / "supervisor" / "management-self-update.sqlite3").rename(system.root / "supervisor" / "retained-handoff.sqlite3")
    else:
        # An actual nonterminal operation, not a synthetic success flag.
        from auto_coder.management.operations import ManagedTarget, OperationIntent
        from tests.test_management_operation_store import authority
        from tests.test_management_self_update_integration import INSTANCE, REPOSITORY

        pending = OperationIntent(ManagedTarget(INSTANCE, "management", REPOSITORY), "restart", (), "conflict", "generation")
        assert system.journal.admit(pending, authority()).record is not None
    system.start(wait=False)
    time.sleep(2)
    assert system.child.phase is ChildPhase.STOPPED
    with sqlite3.connect(system.root / "supervisor" / "management-child.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM effects WHERE action='START_SELECTED'").fetchone()[0] == 1
    assert not os.path.exists(f"/proc/{old.pid}")


@pytest.mark.parametrize("management_system", ["restart_clean"], indirect=True)
@pytest.mark.parametrize("boundary", ["before_claim", "after_claim", "after_launch"])
def test_clean_restart_crash_boundaries_do_not_duplicate_child(management_system: ManagementSystem, boundary: str) -> None:
    import subprocess
    import sys
    from pathlib import Path

    from auto_coder.management.bootstrap_supervisor import ManagementBootstrapSupervisor

    system = management_system
    assert system.supervisor is not None
    system.supervisor.terminate()
    assert system.supervisor.wait(timeout=30) == 0
    marker = system.root / "claim-barrier"
    bootstrap = Path(__file__).resolve().parents[1] / "src"
    injected = """
import os, sys, time
from pathlib import Path
from auto_coder.management.bootstrap_store import BootstrapStore
from auto_coder.management.bootstrap_supervisor import ManagementBootstrapSupervisor, main
boundary, marker, config = sys.argv[1:]
original_begin = BootstrapStore.begin_effect
original_finish = ManagementBootstrapSupervisor._finish
def wait():
    Path(marker).write_text('held')
    while True: time.sleep(.05)
def begin(self, operation_id, *args, **kwargs):
    selected = operation_id.startswith('container-autostart-')
    if selected and boundary == 'before_claim': wait()
    result = original_begin(self, operation_id, *args, **kwargs)
    if selected and boundary == 'after_claim': wait()
    return result
def finish(self, operation_id, *args, **kwargs):
    if operation_id.startswith('container-autostart-') and boundary == 'after_launch': wait()
    return original_finish(self, operation_id, *args, **kwargs)
BootstrapStore.begin_effect = begin
ManagementBootstrapSupervisor._finish = finish
sys.argv = [sys.argv[0], '--config', config]
main()
"""
    environment = {**os.environ, "PYTHONPATH": str(bootstrap), "PYTHONDONTWRITEBYTECODE": "1"}
    system.supervisor = subprocess.Popen([sys.executable, "-c", injected, boundary, str(marker), str(system.bootstrap_path)], cwd=system.root, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _wait(lambda: marker.exists() or system.supervisor.poll() is not None, timeout=30, description="actual durable launch boundary")
    assert marker.exists()
    observed = system.child
    if boundary == "after_launch":
        assert observed.pid and ManagementBootstrapSupervisor._start_token(observed.pid) == observed.start_token
    system.supervisor.kill()
    system.supervisor.wait(timeout=10)
    if boundary == "after_launch":
        os.kill(observed.pid, signal.SIGKILL)
        _wait(lambda: not ManagementBootstrapSupervisor._start_token(observed.pid), timeout=10, description="test child exit")
    system.start(wait=boundary == "before_claim")
    if boundary == "before_claim":
        assert system.child.phase is ChildPhase.RUNNING
    else:
        time.sleep(1)
        assert system.child.phase in {ChildPhase.STOPPED, ChildPhase.UNKNOWN}
    with sqlite3.connect(system.root / "supervisor" / "management-child.sqlite3") as db:
        attempts = db.execute("SELECT phase FROM effects WHERE operation_id LIKE 'container-autostart-%'").fetchall()
        assert len(attempts) == 1
        assert attempts[0][0] == ("observed" if boundary == "before_claim" else "prepared")


@pytest.mark.parametrize("management_system", ["restart_clean"], indirect=True)
def test_shutdown_waits_for_real_inflight_management_request(management_system: ManagementSystem) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from auto_coder.management.bootstrap_supervisor import ManagementBootstrapSupervisor

    system = management_system
    bearer = system.issue()
    prior = system.child
    assert system.supervisor is not None
    with ThreadPoolExecutor(max_workers=1) as pool:
        held = pool.submit(system.call, bearer, "fixture_hold_request")
        _wait(lambda: (system.root / "oauth" / "request-entered").exists(), timeout=10, description="actual in-flight request")
        system.supervisor.terminate()
        _wait(lambda: system.child.phase is ChildPhase.DRAINING, timeout=10, description="drain intent")
        time.sleep(0.5)
        assert system.supervisor.poll() is None
        assert ManagementBootstrapSupervisor._start_token(prior.pid) == prior.start_token
        assert not held.done()
        (system.root / "oauth" / "release-request").touch()
        assert held.result(timeout=15)["drained"] is True
    assert system.supervisor.wait(timeout=20) == 0
    assert system.child.phase is ChildPhase.STOPPED


def test_clean_restart_configuration_requires_a_fixed_drain_receipt(tmp_path) -> None:
    from dataclasses import replace

    from tests.test_management_bootstrap_supervisor import _runtime, _supervisor

    runtime = _runtime(tmp_path)
    config = _supervisor(tmp_path, runtime).config
    with pytest.raises(ValueError, match="fixed drain receipt"):
        replace(config, runtimes=(replace(runtime, drain_ack=None),), restart_after_clean_shutdown=True).validate()
