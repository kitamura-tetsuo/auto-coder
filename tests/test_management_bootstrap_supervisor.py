from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import replace
from pathlib import Path

import pytest

from auto_coder.management.bootstrap_store import ChildPhase, ChildRecord
from auto_coder.management.bootstrap_supervisor import BootstrapConfig, ManagementBootstrapSupervisor, Observation, ResultStatus, RuntimeSpec

CHILD = b"""#!/usr/bin/python3
import json, os, signal, sys, time
ack = sys.argv[1]
def stop(_signum, _frame):
    fields = open('/proc/self/stat').read().split()
    receipt = {'generation': int(os.environ['AUTO_CODER_CHILD_GENERATION']), 'nonce': os.environ['AUTO_CODER_DRAIN_NONCE'], 'pid': os.getpid(), 'start_token': fields[21]}
    open(ack, 'w').write(json.dumps(receipt, sort_keys=True))
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
open(ack + '.ready', 'w').write('ready')
while True: time.sleep(.02)
"""

HTTP_CHILD = b"""#!/usr/bin/python3
import http.server, json, os, signal, sys, threading, time
ack, port_file, release, held = sys.argv[1:]
active = 0
lock = threading.Lock()
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global active
        with lock: active += 1
        open(held, 'w').write('held')
        try:
            while not os.path.exists(release): time.sleep(.01)
            self.send_response(200); self.end_headers(); self.wfile.write(b'done')
        finally:
            with lock: active -= 1
    def log_message(self, *args): pass
server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
def stop(_signum, _frame):
    while True:
        with lock:
            if active == 0: break
        time.sleep(.01)
    fields = open('/proc/self/stat').read().split()
    receipt = {'generation': int(os.environ['AUTO_CODER_CHILD_GENERATION']), 'nonce': os.environ['AUTO_CODER_DRAIN_NONCE'], 'pid': os.getpid(), 'start_token': fields[21]}
    open(ack, 'w').write(json.dumps(receipt, sort_keys=True))
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
open(port_file, 'w').write(str(server.server_port))
server.serve_forever()
"""


def _runtime(tmp_path: Path, name: str = "one") -> RuntimeSpec:
    executable = tmp_path / f"child-{name}"
    executable.write_bytes(CHILD)
    executable.chmod(0o755)
    digest = hashlib.sha256(CHILD).hexdigest()
    source = tmp_path / f"source-{name}"
    source.mkdir()
    (source / "package.txt").write_text(f"package-{name}")
    source_receipt = RuntimeSpec._source_digest(source)
    ready = tmp_path / f"{name}.ready.json"
    ready.write_text(json.dumps({"ready": True, "reference": name, "source_receipt": source_receipt}))
    return RuntimeSpec(name, f"sha256:{digest}", executable, (str(tmp_path / f"{name}.ack"),), (), tmp_path / f"{name}.ack", source, source_receipt, ready)


def _http_runtime(tmp_path: Path) -> tuple[RuntimeSpec, Path, Path, Path]:
    runtime = _runtime(tmp_path, "http")
    runtime.executable.write_bytes(HTTP_CHILD)
    runtime.executable.chmod(0o755)
    port_file, release, held = tmp_path / "http.port", tmp_path / "http.release", tmp_path / "http.held"
    return replace(runtime, receipt=f"sha256:{hashlib.sha256(HTTP_CHILD).hexdigest()}", arguments=(str(runtime.drain_ack), str(port_file), str(release), str(held))), port_file, release, held


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
    prepared_bad = _runtime(tmp_path, "bad")
    bad = RuntimeSpec("bad", f"sha256:{hashlib.sha256(broken_executable.read_bytes()).hexdigest()}", broken_executable, (), (), None, prepared_bad.source_root, prepared_bad.source_receipt, prepared_bad.ready_receipt)
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


def test_effect_identity_must_be_nonempty(tmp_path: Path) -> None:
    supervisor = _supervisor(tmp_path, _runtime(tmp_path))
    with pytest.raises(ValueError):
        supervisor.start_selected("", "attempt", "one")


def test_unresolved_starting_effect_never_launches_a_second_child(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    supervisor = _supervisor(tmp_path, runtime)
    supervisor.store.begin_effect("lost-start", "attempt", "START_SELECTED", "one", "")
    supervisor.store.save(ChildRecord("manager", ChildPhase.STARTING, "one", child_generation=1, effect_operation_id="lost-start", effect_attempt_id="attempt"))
    assert supervisor.inspect().status is ResultStatus.UNKNOWN
    assert supervisor.start_selected("lost-start", "attempt", "one").status is ResultStatus.UNKNOWN
    assert supervisor.start_selected("new-start", "new-attempt", "one").status is ResultStatus.UNKNOWN


def test_non_initial_runtime_requires_current_ready_source_receipt(tmp_path: Path) -> None:
    initial = _runtime(tmp_path, "initial")
    candidate = _runtime(tmp_path, "candidate")
    supervisor = _supervisor(tmp_path, initial, candidate)
    assert supervisor.start_selected("candidate", "attempt", "candidate").status is ResultStatus.OBSERVED_RUNNING
    running = supervisor.store.load()
    _stop_process(running.pid)

    other_root = tmp_path / "other"
    other_root.mkdir()
    other = _supervisor(other_root, _runtime(other_root, "initial"), _runtime(other_root, "candidate"))
    changed = other.config.runtimes[1]
    assert changed.source_root is not None
    (changed.source_root / "package.txt").write_text("changed after READY")
    rejected = other.start_selected("candidate", "attempt", "candidate")
    assert rejected.status is ResultStatus.NEEDS_ATTENTION
    assert rejected.record.pid == 0


def test_non_initial_runtime_rejects_changed_symlinked_source(tmp_path: Path) -> None:
    initial = _runtime(tmp_path, "initial")
    candidate = _runtime(tmp_path, "candidate")
    assert candidate.source_root is not None
    target = tmp_path / "outside.py"
    target.write_text("VALUE = 'ready'\n")
    linked = candidate.source_root / "payload.py"
    linked.symlink_to(target)
    # Model a legacy READY producer that omitted links, then change executed content.
    linked.unlink()
    receipt = RuntimeSpec._source_digest(candidate.source_root)
    linked.symlink_to(target)
    assert candidate.ready_receipt is not None
    candidate.ready_receipt.write_text(json.dumps({"ready": True, "reference": "candidate", "source_receipt": receipt}))
    candidate = replace(candidate, source_receipt=receipt)
    target.write_text("VALUE = 'changed'\n")
    supervisor = _supervisor(tmp_path, initial, candidate)
    result = supervisor.start_selected("candidate", "attempt", "candidate")
    assert result.status is ResultStatus.NEEDS_ATTENTION
    assert result.record.pid == 0


def test_replay_returns_original_generation_after_later_child(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    supervisor = _supervisor(tmp_path, runtime)
    first = supervisor.start_selected("start-a", "a", "one")
    _await_child_ready(runtime)
    assert supervisor.request_graceful_stop("stop-a", "sa", ManagementBootstrapSupervisor._identity(first.record)).status is ResultStatus.STOPPED
    runtime.drain_ack.unlink()
    second = supervisor.start_selected("start-b", "b", "one")
    try:
        replay = supervisor.start_selected("start-a", "a", "one")
        assert replay.record.pid == first.record.pid
        assert replay.record.child_generation == first.record.child_generation
        assert replay.process_ready
        assert replay.record.pid != second.record.pid
    finally:
        _stop_process(second.record.pid)


def test_stale_drain_receipt_cannot_stop_later_generation(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    supervisor = _supervisor(tmp_path, runtime)
    first = supervisor.start_selected("start-a", "a", "one")
    _await_child_ready(runtime)
    assert supervisor.request_graceful_stop("stop-a", "sa", ManagementBootstrapSupervisor._identity(first.record)).status is ResultStatus.STOPPED
    stale = runtime.drain_ack.read_text() if runtime.drain_ack is not None else ""
    second = supervisor.start_selected("start-b", "b", "one")
    try:
        assert runtime.drain_ack is not None
        runtime.drain_ack.write_text(stale)
        os.kill(second.record.pid, 9)
        deadline = time.monotonic() + 1
        while ManagementBootstrapSupervisor._start_token(second.record.pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        result = supervisor.request_graceful_stop("stop-b", "sb", ManagementBootstrapSupervisor._identity(second.record))
        assert result.status in {ResultStatus.UNKNOWN, ResultStatus.CONFLICT}
    finally:
        _stop_process(second.record.pid)


def test_real_http_request_drains_without_force_or_competing_listener(tmp_path: Path) -> None:
    runtime, port_file, release, held = _http_runtime(tmp_path)
    supervisor = _supervisor(tmp_path, runtime, timeout=0.08)
    started = supervisor.start_selected("http-start", "a", "http")
    deadline = time.monotonic() + 2
    while not port_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert port_file.exists()
    response: list[bytes] = []
    request = threading.Thread(target=lambda: response.append(urllib.request.urlopen(f"http://127.0.0.1:{port_file.read_text()}/held", timeout=2).read()))
    request.start()
    deadline = time.monotonic() + 2
    while not held.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert held.exists()
    draining = supervisor.request_graceful_stop("http-stop", "b", ManagementBootstrapSupervisor._identity(started.record))
    assert draining.status is ResultStatus.DRAINING
    assert ManagementBootstrapSupervisor._start_token(started.record.pid) == started.record.start_token
    release.touch()
    request.join(2)
    deadline = time.monotonic() + 2
    result = supervisor.reconcile()
    while result.status is not ResultStatus.STOPPED and time.monotonic() < deadline:
        time.sleep(0.02)
        result = supervisor.reconcile()
    assert response == [b"done"]
    assert result.status is ResultStatus.STOPPED


def test_executed_candidate_nonzero_exit_restores_previous_runtime(tmp_path: Path) -> None:
    old = _runtime(tmp_path, "old")
    candidate = _runtime(tmp_path, "candidate")
    candidate.executable.write_bytes(b"#!/bin/sh\nexit 23\n")
    candidate.executable.chmod(0o755)
    candidate = replace(candidate, receipt=f"sha256:{hashlib.sha256(candidate.executable.read_bytes()).hexdigest()}")
    supervisor = _supervisor(tmp_path, old, candidate)
    first = supervisor.start_selected("old-start", "a", "old")
    _await_child_ready(old)
    assert supervisor.request_graceful_stop("old-stop", "b", ManagementBootstrapSupervisor._identity(first.record)).status is ResultStatus.STOPPED
    restored = supervisor.start_selected("candidate-start", "c", "candidate")
    try:
        assert restored.status is ResultStatus.PROCESS_RESTORED
        assert restored.record.selected_runtime == "old"
        assert restored.record.recovery_used
    finally:
        _stop_process(restored.record.pid)


def test_supervisor_process_crash_after_spawn_never_duplicates_child(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    marker = tmp_path / "spawned.pid"
    harness = tmp_path / "crash_supervisor.py"
    harness.write_text(
        f"""from pathlib import Path
import time
from auto_coder.management.bootstrap_supervisor import BootstrapConfig, ManagementBootstrapSupervisor, RuntimeSpec
runtime = RuntimeSpec({runtime.reference!r}, {runtime.receipt!r}, Path({str(runtime.executable)!r}), {runtime.arguments!r}, (), Path({str(runtime.drain_ack)!r}), Path({str(runtime.source_root)!r}), {runtime.source_receipt!r}, Path({str(runtime.ready_receipt)!r}))
config = BootstrapConfig('manager', Path({str(tmp_path / 'state')!r}), Path({str(tmp_path / 'oauth')!r}), Path({str(tmp_path / 'journal')!r}), 'http://127.0.0.1:8090', 'one', (runtime,), 1)
class CrashWindowSupervisor(ManagementBootstrapSupervisor):
    def _start_token(self, pid):
        Path({str(marker)!r}).write_text(str(pid))
        time.sleep(30)
        return super()._start_token(pid)
CrashWindowSupervisor(config).start_selected('lost-start', 'attempt', 'one')
"""
    )
    process = subprocess.Popen([sys.executable, str(harness)])
    deadline = time.monotonic() + 5
    while (not marker.exists() or not marker.read_text()) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists()
    child_pid = int(marker.read_text())
    process.kill()
    process.wait(2)
    try:
        rebuilt = _supervisor(tmp_path, runtime)
        assert rebuilt.inspect().status is ResultStatus.UNKNOWN
        assert rebuilt.start_selected("lost-start", "attempt", "one").status is ResultStatus.UNKNOWN
        assert rebuilt.start_selected("other-start", "other-attempt", "one").status is ResultStatus.UNKNOWN
        assert ManagementBootstrapSupervisor._start_token(child_pid)
    finally:
        _stop_process(child_pid)


def test_two_supervisors_atomically_contend_for_one_child(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    barrier = threading.Barrier(2)

    class Contender(ManagementBootstrapSupervisor):
        def inspect(self) -> Observation:
            observed = super().inspect()
            if observed.status is ResultStatus.STOPPED:
                barrier.wait(timeout=2)
            return observed

    first = Contender(_supervisor(tmp_path, runtime).config, "first")
    second = Contender(_supervisor(tmp_path, runtime).config, "second")
    results: list[Observation] = []
    threads = [
        threading.Thread(target=lambda: results.append(first.start_selected("first-start", "a", "one"))),
        threading.Thread(target=lambda: results.append(second.start_selected("second-start", "b", "one"))),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)
    try:
        assert len(results) == 2
        assert sum(result.status is ResultStatus.OBSERVED_RUNNING for result in results) == 1
        assert sum(result.status in {ResultStatus.CONFLICT, ResultStatus.UNKNOWN} for result in results) == 1
        pids = {result.record.pid for result in results if result.status is ResultStatus.OBSERVED_RUNNING}
        assert len(pids) == 1
        assert all(ManagementBootstrapSupervisor._start_token(pid) for pid in pids)
    finally:
        for result in results:
            _stop_process(result.record.pid)


def test_delayed_candidate_failure_reconciles_one_previous_runtime(tmp_path: Path) -> None:
    old = _runtime(tmp_path, "old")
    candidate = _runtime(tmp_path, "candidate")
    candidate.executable.write_bytes(b"#!/bin/sh\nsleep 0.35\nexit 23\n")
    candidate.executable.chmod(0o755)
    candidate = replace(candidate, receipt=f"sha256:{hashlib.sha256(candidate.executable.read_bytes()).hexdigest()}")
    supervisor = _supervisor(tmp_path, old, candidate)
    first = supervisor.start_selected("old-start", "a", "old")
    _await_child_ready(old)
    assert supervisor.request_graceful_stop("old-stop", "b", ManagementBootstrapSupervisor._identity(first.record)).status is ResultStatus.STOPPED
    candidate_result = supervisor.start_selected("candidate-start", "c", "candidate")
    assert candidate_result.status is ResultStatus.OBSERVED_RUNNING
    deadline = time.monotonic() + 2
    restored = supervisor.reconcile()
    while restored.status is ResultStatus.OBSERVED_RUNNING and restored.record.pid == candidate_result.record.pid and time.monotonic() < deadline:
        time.sleep(0.02)
        restored = supervisor.reconcile()
    try:
        assert restored.status is ResultStatus.PROCESS_RESTORED
        assert restored.record.selected_runtime == "old"
        assert restored.record.recovery_used
        restored_pid = restored.record.pid
        replay = supervisor.start_selected("candidate-start", "c", "candidate")
        assert replay.status is ResultStatus.PROCESS_RESTORED
        assert replay.record.pid == restored_pid
    finally:
        _stop_process(restored.record.pid)
