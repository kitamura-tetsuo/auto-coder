from __future__ import annotations

import hashlib
import json
import os
import shutil
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

COUNTING_CHILD = b"""#!/usr/bin/python3
import os, signal, sys, time
ready, signals = sys.argv[1:]
def stop(_signum, _frame):
    with open(signals, 'a') as stream: stream.write('signal\\n')
signal.signal(signal.SIGTERM, stop)
open(ready, 'w').write('ready')
while True: time.sleep(.02)
"""


def _runtime(tmp_path: Path, name: str = "one") -> RuntimeSpec:
    source = tmp_path / f"source-{name}"
    source.mkdir()
    executable = source / f"child-{name}"
    executable.write_bytes(CHILD)
    executable.chmod(0o755)
    digest = hashlib.sha256(CHILD).hexdigest()
    (source / "package.txt").write_text(f"package-{name}")
    source_receipt = RuntimeSpec._source_digest(source)
    ready = tmp_path / f"{name}.ready.json"
    ready.write_text(json.dumps({"ready": True, "reference": name, "source_receipt": source_receipt}))
    return RuntimeSpec(name, f"sha256:{digest}", executable, (str(tmp_path / f"{name}.ack"),), (), tmp_path / f"{name}.ack", source, source_receipt, ready)


def _resign_runtime(runtime: RuntimeSpec) -> RuntimeSpec:
    assert runtime.source_root is not None and runtime.ready_receipt is not None
    source_receipt = RuntimeSpec._source_digest(runtime.source_root)
    runtime.ready_receipt.write_text(json.dumps({"ready": True, "reference": runtime.reference, "source_receipt": source_receipt}))
    return replace(runtime, receipt=f"sha256:{hashlib.sha256(runtime.executable.read_bytes()).hexdigest()}", source_receipt=source_receipt)


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
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            # A child launched by the separate crash harness is reparented and
            # cannot be reaped by this pytest process.
            pass


def _await_child_ready(runtime: RuntimeSpec) -> None:
    assert runtime.drain_ack is not None
    ready = Path(str(runtime.drain_ack) + ".ready")
    deadline = time.monotonic() + 2
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.exists()


def test_linux_start_token_handles_process_name_with_spaces() -> None:
    # Linux comm is parenthesized; whitespace cannot change the starttime field.
    script = "import ctypes, time; " "ctypes.CDLL(None).prctl(15, b'name with spaces', 0, 0, 0); " "print('ready', flush=True); time.sleep(5)"
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "ready"
        raw = Path(f"/proc/{child.pid}/stat").read_text(encoding="utf-8")
        expected = raw.rsplit(")", 1)[1].split()[19]
        assert expected.isdigit()
        assert ManagementBootstrapSupervisor._start_token(child.pid) == expected
    finally:
        _stop_process(child.pid)


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


def test_two_supervisors_atomically_claim_one_graceful_stop_signal(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    ready = tmp_path / "counting.ready"
    signals = tmp_path / "signals.log"
    runtime.executable.write_bytes(COUNTING_CHILD)
    runtime.executable.chmod(0o755)
    runtime = replace(
        runtime,
        receipt=f"sha256:{hashlib.sha256(COUNTING_CHILD).hexdigest()}",
        arguments=(str(ready), str(signals)),
        drain_ack=None,
    )
    starter = _supervisor(tmp_path, runtime, timeout=0.12)
    started = starter.start_selected("start", "start-attempt", "one")
    deadline = time.monotonic() + 2
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.exists()
    identity = ManagementBootstrapSupervisor._identity(started.record)
    barrier = threading.Barrier(2, timeout=5)

    class Stopper(ManagementBootstrapSupervisor):
        def inspect(self) -> Observation:
            observed = super().inspect()
            if observed.status is ResultStatus.OBSERVED_RUNNING:
                barrier.wait()
            return observed

    contenders = [Stopper(starter.config, "stopper-a"), Stopper(starter.config, "stopper-b")]
    results: list[Observation] = []
    threads = [threading.Thread(target=lambda supervisor=supervisor: results.append(supervisor.request_graceful_stop("stop", "stop-attempt", identity))) for supervisor in contenders]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    try:
        assert all(not thread.is_alive() for thread in threads)
        assert len(results) == 2
        assert all(result.status is ResultStatus.DRAINING for result in results)
        deadline = time.monotonic() + 1
        while (not signals.exists() or len(signals.read_text().splitlines()) < 1) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert signals.read_text().splitlines() == ["signal"]
        assert ManagementBootstrapSupervisor._start_token(started.record.pid) == started.record.start_token
    finally:
        _stop_process(started.record.pid)


def test_stale_start_observation_cannot_roll_back_draining_owner(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    ready = tmp_path / "counting.ready"
    signals = tmp_path / "signals.log"
    runtime.executable.write_bytes(COUNTING_CHILD)
    runtime.executable.chmod(0o755)
    runtime = replace(
        runtime,
        receipt=f"sha256:{hashlib.sha256(COUNTING_CHILD).hexdigest()}",
        arguments=(str(ready), str(signals)),
        drain_ack=None,
    )
    starter = _supervisor(tmp_path, runtime, timeout=0.12)
    started = starter.start_selected("start", "start-attempt", "one")
    deadline = time.monotonic() + 2
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.exists()
    identity = ManagementBootstrapSupervisor._identity(started.record)
    both_observed = threading.Barrier(2, timeout=5)

    class Contender(ManagementBootstrapSupervisor):
        def inspect(self) -> Observation:
            observed = super().inspect()
            if observed.status is ResultStatus.OBSERVED_RUNNING:
                both_observed.wait()
            return observed

    winner = Contender(starter.config, "winner")

    class DelayedStale(Contender):
        def _finish(
            self,
            operation_id: str,
            status: ResultStatus,
            record: ChildRecord,
            detail: str = "",
            *,
            persist_state: bool = True,
        ) -> Observation:
            if status is ResultStatus.CONFLICT:
                deadline = time.monotonic() + 2
                while self.store.load().phase is not ChildPhase.DRAINING and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert self.store.load().phase is ChildPhase.DRAINING
            return super()._finish(operation_id, status, record, detail, persist_state=persist_state)

    stale = DelayedStale(starter.config, "stale")
    results: list[Observation] = []
    threads = [
        threading.Thread(target=lambda: results.append(winner.request_graceful_stop("winning-stop", "winner-attempt", identity))),
        threading.Thread(target=lambda: results.append(stale.start_selected("stale-start", "stale-attempt", "one", "stale-identity"))),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    try:
        assert all(not thread.is_alive() for thread in threads)
        assert {result.status for result in results} == {ResultStatus.DRAINING, ResultStatus.CONFLICT}
        assert starter.store.load().phase is ChildPhase.DRAINING
        replay = stale.start_selected("stale-start", "stale-attempt", "one", "stale-identity")
        assert replay.status is ResultStatus.CONFLICT
        third = starter.request_graceful_stop("third-stop", "third-attempt", identity)
        assert third.status is ResultStatus.CONFLICT
        deadline = time.monotonic() + 1
        while (not signals.exists() or not signals.read_text().splitlines()) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert signals.read_text().splitlines() == ["signal"]
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
    prepared_bad = _runtime(tmp_path, "bad")
    # The content is READY-verified, but the shebang interpreter cannot be executed.
    prepared_bad.executable.write_bytes(b"#!/nonexistent-management-interpreter\n")
    bad = replace(_resign_runtime(prepared_bad), arguments=(), drain_ack=None)
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


def test_ready_runtime_rejects_python_content_outside_source_closure(tmp_path: Path) -> None:
    initial = _runtime(tmp_path, "initial")
    candidate = _runtime(tmp_path, "candidate")
    interpreter = Path(sys.executable).resolve()
    external_script = tmp_path / "external-service.py"
    external_module = tmp_path / "external-module"
    external_script.write_text("import time\nwhile True: time.sleep(1)\n")
    external_module.mkdir()
    (external_module / "payload.py").write_text("VALUE = 'ready'\n")
    candidate = replace(
        candidate,
        executable=interpreter,
        receipt=f"sha256:{hashlib.sha256(interpreter.read_bytes()).hexdigest()}",
        arguments=(str(external_script),),
        environment=(("PYTHONPATH", str(external_module)),),
        execution_kind="python-file",
    )
    # Both executed inputs can change without changing the independently
    # persisted READY source receipt; neither may reach process launch.
    external_script.write_text("raise RuntimeError('changed')\n")
    (external_module / "payload.py").write_text("VALUE = 'changed'\n")
    supervisor = _supervisor(tmp_path, initial, candidate)
    result = supervisor.start_selected("candidate", "attempt", "candidate")
    assert result.status is ResultStatus.NEEDS_ATTENTION
    assert result.record.pid == 0


def test_renamed_python_interpreter_cannot_run_unverified_entrypoint(tmp_path: Path) -> None:
    initial = _runtime(tmp_path, "initial")
    candidate = _runtime(tmp_path, "candidate")
    renamed_python = tmp_path / "service-runtime"
    shutil.copy2(Path(sys.executable).resolve(), renamed_python)
    external_script = tmp_path / "external-service.py"
    marker = tmp_path / "unexpected-execution"
    external_script.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('ready')\n")
    candidate = replace(
        candidate,
        executable=renamed_python,
        receipt=f"sha256:{hashlib.sha256(renamed_python.read_bytes()).hexdigest()}",
        arguments=(str(external_script),),
        execution_kind="python-file",
    )
    # Mutating code outside the independently READY-verified tree must not
    # authorize launch, regardless of the verified interpreter's filename.
    external_script.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('changed')\n")
    supervisor = _supervisor(tmp_path, initial, candidate)
    assert not candidate.verify()
    rejected = supervisor.start_selected("candidate", "attempt", "candidate")
    assert rejected.status is ResultStatus.NEEDS_ATTENTION
    assert rejected.record.pid == 0
    assert not marker.exists()


def test_python_interpreter_starts_ready_bound_script(tmp_path: Path) -> None:
    initial = _runtime(tmp_path, "initial")
    candidate = _runtime(tmp_path, "candidate")
    interpreter = Path(sys.executable).resolve()
    assert candidate.source_root is not None
    service_script = candidate.source_root / "service.py"
    marker = tmp_path / "verified-execution"
    service_script.write_text(f"from pathlib import Path\nimport time\nPath({str(marker)!r}).write_text('ready')\nwhile True: time.sleep(.01)\n")
    candidate = _resign_runtime(candidate)
    candidate = replace(
        candidate,
        executable=interpreter,
        receipt=f"sha256:{hashlib.sha256(interpreter.read_bytes()).hexdigest()}",
        arguments=(str(service_script),),
        execution_kind="python-file",
    )
    supervisor = _supervisor(tmp_path, initial, candidate)
    assert candidate.verify()
    started = supervisor.start_selected("candidate", "attempt", "candidate")
    try:
        assert started.status is ResultStatus.OBSERVED_RUNNING
        deadline = time.monotonic() + 2
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.read_text() == "ready"
        assert ManagementBootstrapSupervisor._start_token(started.record.pid) == started.record.start_token
    finally:
        _stop_process(started.record.pid)


def test_run_does_not_bootstrap_with_ledger_only_unresolved_action(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(tmp_path)
    preparer = _supervisor(tmp_path, runtime)
    harness = tmp_path / "prepare-effect.py"
    harness.write_text("from pathlib import Path\n" "from auto_coder.management.bootstrap_store import BootstrapStore\n" f"store = BootstrapStore(Path({str(tmp_path / 'state')!r}), 'manager')\n" "store.begin_effect('replacement', 'attempt', 'START_SELECTED', 'candidate', '')\n")
    subprocess.run([sys.executable, str(harness)], check=True)
    launched = Path(str(runtime.drain_ack) + ".ready")

    class EndRun(Exception):
        pass

    monkeypatch.setattr(time, "sleep", lambda _seconds: (_ for _ in ()).throw(EndRun()))
    with pytest.raises(EndRun):
        preparer.run()
    assert preparer.store.load().phase is ChildPhase.UNKNOWN
    assert preparer.store.load().exit_cause == "unresolved_effect"
    assert not launched.exists()


def test_start_completion_preserves_concurrent_draining_owner(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    ready = tmp_path / "race.ready"
    signals = tmp_path / "race-signals.log"
    runtime.executable.write_bytes(COUNTING_CHILD)
    runtime.executable.chmod(0o755)
    runtime = replace(runtime, receipt=f"sha256:{hashlib.sha256(COUNTING_CHILD).hexdigest()}", arguments=(str(ready), str(signals)), drain_ack=None)
    starter = _supervisor(tmp_path, runtime, timeout=0.05)
    running_saved = threading.Event()
    resume_start = threading.Event()
    original_save = starter.store.save

    def pause_after_running(record: ChildRecord) -> None:
        original_save(record)
        if record.phase is ChildPhase.RUNNING:
            running_saved.set()
            assert resume_start.wait(2)

    starter.store.save = pause_after_running  # type: ignore[method-assign]
    results: list[Observation] = []
    thread = threading.Thread(target=lambda: results.append(starter.start_selected("start", "attempt", "one")))
    thread.start()
    assert running_saved.wait(2)
    running = starter.store.load()
    assert running.phase is ChildPhase.RUNNING
    deadline = time.monotonic() + 2
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert ready.exists()
    stopper = ManagementBootstrapSupervisor(starter.config, "stopper")
    stopped = stopper.request_graceful_stop("stop", "stop-attempt", ManagementBootstrapSupervisor._identity(running))
    resume_start.set()
    thread.join(2)
    try:
        assert stopped.status is ResultStatus.DRAINING
        assert len(results) == 1 and results[0].status is ResultStatus.DRAINING
        durable = starter.store.load()
        assert durable.phase is ChildPhase.DRAINING
        assert durable.effect_operation_id == "stop"
        assert stopper.request_graceful_stop("again", "again-attempt", ManagementBootstrapSupervisor._identity(running)).status is ResultStatus.CONFLICT
        assert signals.read_text().splitlines() == ["signal"]
    finally:
        _stop_process(running.pid)


def test_late_stop_receipt_cannot_overwrite_new_child_generation(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    first_stop_recorded = threading.Event()
    release_stop_receipt = threading.Event()

    class PausedStopReceipt(ManagementBootstrapSupervisor):
        def _finish(
            self,
            operation_id: str,
            status: ResultStatus,
            record: ChildRecord,
            detail: str = "",
            *,
            persist_state: bool = False,
        ) -> Observation:
            if operation_id == "stop" and status is ResultStatus.STOPPED:
                assert self.store.load().phase is ChildPhase.STOPPED
                first_stop_recorded.set()
                assert release_stop_receipt.wait(5)
            return super()._finish(operation_id, status, record, detail, persist_state=persist_state)

    config = _supervisor(tmp_path, runtime).config
    stopper = PausedStopReceipt(config, "stopper")
    first = stopper.start_selected("start-a", "a", "one")
    _await_child_ready(runtime)
    stop_results: list[Observation] = []
    thread = threading.Thread(target=lambda: stop_results.append(stopper.request_graceful_stop("stop", "b", ManagementBootstrapSupervisor._identity(first.record))))
    thread.start()
    second: Observation | None = None
    try:
        assert first_stop_recorded.wait(5)
        other = ManagementBootstrapSupervisor(config, "other")
        second = other.start_selected("start-b", "c", "one")
        assert second.status is ResultStatus.OBSERVED_RUNNING
        release_stop_receipt.set()
        thread.join(5)
        assert len(stop_results) == 1 and stop_results[0].status is ResultStatus.STOPPED
        durable = stopper.store.load()
        assert durable.phase is ChildPhase.RUNNING
        assert durable.pid == second.record.pid
        assert durable.child_generation == second.record.child_generation
        assert other.inspect().status is ResultStatus.OBSERVED_RUNNING
    finally:
        release_stop_receipt.set()
        thread.join(5)
        _stop_process(first.record.pid)
        if second is not None:
            _stop_process(second.record.pid)


def test_replay_waits_for_original_inflight_start_receipt(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    ready_to_finish = threading.Event()
    release_finish = threading.Event()

    class DelayedOriginal(ManagementBootstrapSupervisor):
        def _finish(
            self,
            operation_id: str,
            status: ResultStatus,
            record: ChildRecord,
            detail: str = "",
            *,
            persist_state: bool = False,
        ) -> Observation:
            if operation_id == "same-start" and status is ResultStatus.OBSERVED_RUNNING:
                ready_to_finish.set()
                assert release_finish.wait(5)
            return super()._finish(operation_id, status, record, detail, persist_state=persist_state)

    config = _supervisor(tmp_path, runtime).config
    owner = DelayedOriginal(config, "owner")
    contender = ManagementBootstrapSupervisor(config, "contender")
    completed: list[Observation] = []
    thread = threading.Thread(target=lambda: completed.append(owner.start_selected("same-start", "attempt", "one")))
    thread.start()
    pid = 0
    try:
        assert ready_to_finish.wait(5)
        pid = owner.store.load().pid
        pending = contender.start_selected("same-start", "attempt", "one")
        assert pending.status is ResultStatus.UNKNOWN
        with sqlite3.connect(owner.store.path) as connection:
            assert connection.execute("SELECT phase FROM effects WHERE operation_id='same-start'").fetchone() == ("prepared",)
        release_finish.set()
        thread.join(5)
        assert len(completed) == 1 and completed[0].status is ResultStatus.OBSERVED_RUNNING
        replay = contender.start_selected("same-start", "attempt", "one")
        assert replay.status is ResultStatus.OBSERVED_RUNNING
        assert replay.record.pid == pid
    finally:
        release_finish.set()
        thread.join(5)
        _stop_process(pid)


def test_stop_uses_lifetime_handle_when_child_exits_during_reservation(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    supervisor = _supervisor(tmp_path, runtime)
    started = supervisor.start_selected("start", "attempt", "one")
    _await_child_ready(runtime)
    original_reserve = supervisor.store.reserve

    def exit_during_reserve(expected: ChildRecord, prepared: ChildRecord) -> bool:
        reserved = original_reserve(expected, prepared)
        if prepared.phase is ChildPhase.DRAINING:
            _stop_process(expected.pid)
        return reserved

    supervisor.store.reserve = exit_during_reserve  # type: ignore[method-assign]
    result = supervisor.request_graceful_stop("stop", "stop-attempt", ManagementBootstrapSupervisor._identity(started.record))
    assert result.status is ResultStatus.UNKNOWN
    assert supervisor.store.load().phase is ChildPhase.DRAINING


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
        _stop_process(supervisor.store.load().pid)


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
    _stop_process(started.record.pid)


def test_executed_candidate_nonzero_exit_restores_previous_runtime(tmp_path: Path) -> None:
    old = _runtime(tmp_path, "old")
    candidate = _runtime(tmp_path, "candidate")
    candidate.executable.write_bytes(b"#!/bin/sh\nexit 23\n")
    candidate.executable.chmod(0o755)
    candidate = _resign_runtime(candidate)
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
    # The deliberately orphaned child must not remain in pytest's CI process
    # group after the crash harness dies; otherwise a non-reaping container PID
    # 1 can leave its zombie looking like an active shard member.
    runtime.executable.write_bytes(CHILD.replace(b"import json, os, signal, sys, time\n", b"import json, os, signal, sys, time\nos.setsid()\n"))
    runtime.executable.chmod(0o755)
    runtime = replace(runtime, receipt=f"sha256:{hashlib.sha256(runtime.executable.read_bytes()).hexdigest()}")
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
    # Coverage shards can leave only a small amount of CPU available to these
    # contenders.  Keep the admission rendezvous bounded, but do not make the
    # process-safety oracle depend on both threads being scheduled within two
    # seconds.
    barrier = threading.Barrier(2, timeout=15)

    class Contender(ManagementBootstrapSupervisor):
        def inspect(self) -> Observation:
            observed = super().inspect()
            if observed.status is ResultStatus.STOPPED:
                barrier.wait(timeout=2)
            return observed

    first = Contender(_supervisor(tmp_path, runtime).config, "first")
    second = Contender(_supervisor(tmp_path, runtime).config, "second")
    results: list[Observation] = []
    errors: list[BaseException] = []

    def contend(supervisor: ManagementBootstrapSupervisor, operation_id: str, attempt_id: str) -> None:
        try:
            results.append(supervisor.start_selected(operation_id, attempt_id, "one"))
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=contend, args=(first, "first-start", "a")),
        threading.Thread(target=contend, args=(second, "second-start", "b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    try:
        assert all(not thread.is_alive() for thread in threads)
        assert errors == []
        assert len(results) == 2
        assert sum(result.status is ResultStatus.OBSERVED_RUNNING for result in results) == 1
        assert sum(result.status in {ResultStatus.CONFLICT, ResultStatus.UNKNOWN} for result in results) == 1
        pids = {result.record.pid for result in results if result.status is ResultStatus.OBSERVED_RUNNING}
        assert len(pids) == 1
        assert all(ManagementBootstrapSupervisor._start_token(pid) for pid in pids)
    finally:
        for result in results:
            _stop_process(result.record.pid)


def test_owned_exit_between_poll_and_proc_read_is_reconciled(tmp_path: Path) -> None:
    old = _runtime(tmp_path, "old")
    candidate = _runtime(tmp_path, "candidate")
    supervisor = _supervisor(tmp_path, old, candidate)
    first = supervisor.start_selected("old-start", "a", "old")
    _await_child_ready(old)
    assert supervisor.request_graceful_stop("old-stop", "b", ManagementBootstrapSupervisor._identity(first.record)).status is ResultStatus.STOPPED
    started = supervisor.start_selected("candidate-start", "c", "candidate")
    assert started.status is ResultStatus.OBSERVED_RUNNING
    original_token = supervisor._start_token

    def exit_between_observations(pid: int) -> str:
        if pid == started.record.pid:
            child = supervisor._children[pid]
            child.kill()
            child.wait(2)
        return original_token(pid)

    supervisor._start_token = exit_between_observations  # type: ignore[method-assign]
    restored = supervisor.reconcile()
    try:
        assert restored.status is ResultStatus.PROCESS_RESTORED
        assert restored.record.selected_runtime == "old"
        assert restored.record.pid != started.record.pid
        assert ManagementBootstrapSupervisor._start_token(restored.record.pid) == restored.record.start_token
    finally:
        _stop_process(started.record.pid)
        _stop_process(restored.record.pid)


def test_delayed_candidate_failure_reconciles_one_previous_runtime(tmp_path: Path) -> None:
    old = _runtime(tmp_path, "old")
    candidate = _runtime(tmp_path, "candidate")
    # Leave enough time for start_selected() to return its initial observation
    # even when a coverage shard is under heavy load.  The former 350 ms delay
    # could expire during launch-time receipt verification, making the first
    # observation legitimately report the already-restored previous runtime.
    candidate.executable.write_bytes(b"#!/bin/sh\nsleep 2\nexit 23\n")
    candidate.executable.chmod(0o755)
    candidate = _resign_runtime(candidate)
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
        assert replay.status is ResultStatus.OBSERVED_RUNNING
        assert replay.record.pid == candidate_result.record.pid
        assert replay.record.pid != restored_pid
    finally:
        _stop_process(restored.record.pid)
