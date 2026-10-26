from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from pathlib import Path

import pytest

from auto_coder.management.bootstrap_store import ChildPhase
from auto_coder.management.bootstrap_supervisor import BootstrapConfig, ManagementBootstrapSupervisor, ResultStatus, RuntimeSpec

CHILD = b"""#!/usr/bin/python3
import os, signal, sys, time
ack = sys.argv[1]
def stop(_signum, _frame):
    open(ack, 'w').write('drained')
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
open(ack + '.ready', 'w').write('ready')
while True: time.sleep(.02)
"""


def _runtime(tmp_path: Path, name: str = "one") -> RuntimeSpec:
    executable = tmp_path / f"child-{name}"
    executable.write_bytes(CHILD)
    executable.chmod(0o755)
    digest = hashlib.sha256(CHILD).hexdigest()
    return RuntimeSpec(name, f"sha256:{digest}", executable, (str(tmp_path / f"{name}.ack"),), (), tmp_path / f"{name}.ack")


def _supervisor(tmp_path: Path, *runtimes: RuntimeSpec, timeout: float = 1) -> ManagementBootstrapSupervisor:
    config = BootstrapConfig("manager", tmp_path / "state", tmp_path / "oauth", tmp_path / "journal", "http://127.0.0.1:8090", runtimes[0].reference, tuple(runtimes), timeout)
    return ManagementBootstrapSupervisor(config, "supervisor-a")


def _stop_process(pid: int) -> None:
    if pid:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass


def _await_child_ready(runtime: RuntimeSpec) -> None:
    assert runtime.drain_ack is not None
    ready = Path(str(runtime.drain_ack) + ".ready")
    deadline = time.monotonic() + 2
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.exists()


def test_real_child_start_replay_graceful_drain_and_reconstruction(tmp_path: Path) -> None:
    supervisor = _supervisor(tmp_path, _runtime(tmp_path))
    started = supervisor.start_selected("start-1", "attempt-1", "one")
    try:
        assert started.status is ResultStatus.OBSERVED_RUNNING
        _await_child_ready(supervisor.config.runtimes[0])
        assert started.process_ready and not started.application_ready
        assert supervisor.start_selected("start-1", "attempt-1", "one").record.pid == started.record.pid
        rebuilt = _supervisor(tmp_path, supervisor.config.runtimes[0])
        observed = rebuilt.inspect()
        assert observed.record.pid == started.record.pid
        stopped = rebuilt.request_graceful_stop("stop-1", "attempt-2", ManagementBootstrapSupervisor._identity(observed.record))
        assert stopped.status is ResultStatus.STOPPED
        assert stopped.record.intended_stop
        assert rebuilt.inspect().status is ResultStatus.STOPPED
    finally:
        _stop_process(started.record.pid)


def test_overlapping_start_and_stale_identity_are_rejected(tmp_path: Path) -> None:
    supervisor = _supervisor(tmp_path, _runtime(tmp_path))
    started = supervisor.start_selected("start", "attempt", "one")
    try:
        assert supervisor.start_selected("other", "other-attempt", "one").status is ResultStatus.CONFLICT
        assert supervisor.request_graceful_stop("stop", "stop-attempt", "stale").status is ResultStatus.CONFLICT
        assert supervisor.inspect().record.pid == started.record.pid
    finally:
        _stop_process(started.record.pid)


def test_deadline_does_not_force_or_duplicate_signal(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    # A process ignoring SIGTERM models an unavailable drain producer.
    runtime.executable.write_bytes(CHILD.replace(b"signal.signal(signal.SIGTERM, stop)", b"signal.signal(signal.SIGTERM, signal.SIG_IGN)"))
    runtime = RuntimeSpec(runtime.reference, f"sha256:{hashlib.sha256(runtime.executable.read_bytes()).hexdigest()}", runtime.executable, runtime.arguments, (), runtime.drain_ack)
    supervisor = _supervisor(tmp_path, runtime, timeout=0.08)
    started = supervisor.start_selected("start", "attempt", "one")
    try:
        _await_child_ready(runtime)
        identity = ManagementBootstrapSupervisor._identity(started.record)
        result = supervisor.request_graceful_stop("stop", "stop-attempt", identity)
        assert result.status is ResultStatus.DRAINING
        assert ManagementBootstrapSupervisor._start_token(started.record.pid) == started.record.start_token
        assert not runtime.drain_ack.exists()
    finally:
        _stop_process(started.record.pid)


def test_stale_pid_and_corrupt_record_never_authorize_start(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    supervisor = _supervisor(tmp_path, runtime)
    record = supervisor.store.load()
    supervisor.store.save(record.__class__("manager", ChildPhase.RUNNING, "one", pid=os.getpid(), start_token="stale"))
    assert supervisor.inspect().status is ResultStatus.UNKNOWN
    assert supervisor.start_selected("start", "attempt", "one").status is ResultStatus.UNKNOWN
    with sqlite3.connect(supervisor.store.path) as connection:
        connection.execute("UPDATE child_state SET record_json='not json'")
    assert supervisor.inspect().status is ResultStatus.UNKNOWN


def test_failed_candidate_recovers_previous_verified_runtime_once(tmp_path: Path) -> None:
    old = _runtime(tmp_path, "old")
    broken_executable = tmp_path / "broken"
    broken_executable.write_bytes(b"not an executable format")
    broken_executable.chmod(0o755)
    bad = RuntimeSpec("bad", f"sha256:{hashlib.sha256(broken_executable.read_bytes()).hexdigest()}", broken_executable, ())
    supervisor = _supervisor(tmp_path, old, bad)
    first = supervisor.start_selected("old-start", "a", "old")
    try:
        _await_child_ready(old)
        stopped = supervisor.request_graceful_stop("old-stop", "b", ManagementBootstrapSupervisor._identity(first.record))
        assert stopped.status is ResultStatus.STOPPED
        failed = supervisor.start_selected("bad-start", "c", "bad")
        assert failed.status is ResultStatus.PROCESS_RESTORED
        assert failed.record.selected_runtime == "old"
        assert failed.record.pid != first.record.pid
        assert failed.record.recovery_used
        _stop_process(failed.record.pid)
    finally:
        _stop_process(first.record.pid)


def test_config_rejects_remote_listener_and_separates_state_roots(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    with pytest.raises(ValueError):
        BootstrapConfig("manager", tmp_path, tmp_path, tmp_path / "journal", "http://0.0.0.0:80", "one", (runtime,)).validate()
