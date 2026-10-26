import hashlib
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from auto_coder.worker_supervisor import SupervisorConfig, WorkerSupervisor
from auto_coder.worker_supervisor_store import SupervisorStore
from auto_coder.worker_versions import InstalledProvenance, ReadyReceipt, WorkerVersionPreparer


def config(tmp_path: Path, command: tuple[str, ...] | None = None) -> SupervisorConfig:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return SupervisorConfig("beta-worker", "beta", "owner/repo", tmp_path, workspace, Path(sys.executable), tmp_path / "versions", command)


def request(endpoint: Path, payload: dict[str, object]) -> dict[str, object]:
    client: socket.socket | None = None
    for _ in range(500):
        candidate = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            candidate.connect(str(endpoint))
            client = candidate
            break
        except (FileNotFoundError, ConnectionRefusedError):
            candidate.close()
            time.sleep(0.01)
    if client is None:
        raise AssertionError("supervisor control socket did not become reachable")
    client.sendall(json.dumps(payload).encode() + b"\n")
    response = json.loads(client.makefile("rb").readline())
    client.close()
    return response


def test_store_rejects_corrupt_schema(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"schema_version":2}')
    try:
        SupervisorStore(path).read()
    except ValueError as error:
        assert str(error) == "invalid supervisor state"
    else:
        raise AssertionError("corrupt state was accepted")


def test_corrupt_selected_runtime_pointer_refuses_start_without_replacement(tmp_path: Path) -> None:
    supervisor = WorkerSupervisor(config(tmp_path))
    state = supervisor.store.read()
    state["selected_runtime_id"] = {"unexpected": "object"}
    supervisor.store.write(state)
    persisted = supervisor.store.path.read_bytes()

    with patch("auto_coder.worker_supervisor.subprocess.Popen") as spawn:
        assert supervisor.apply({"operation_id": "new", "operation": "START"}) == {"status": "NEEDS_ATTENTION"}
        spawn.assert_not_called()
    assert supervisor.store.path.read_bytes() == persisted


def test_missing_selected_runtime_pointer_with_prior_worker_refuses_start(tmp_path: Path) -> None:
    supervisor = WorkerSupervisor(config(tmp_path))
    state = supervisor.store.read()
    state["last_worker"] = {"runtime_id": "bootstrap", "deployment_generation": "earlier"}
    state["stopped"] = True
    supervisor.store.write(state)

    with patch("auto_coder.worker_supervisor.subprocess.Popen") as spawn:
        assert supervisor.apply({"operation_id": "new", "operation": "START"}) == {"status": "NEEDS_ATTENTION"}
        spawn.assert_not_called()


def test_corrupt_persisted_runtime_pointer_is_fenced_through_control_socket(tmp_path: Path, _use_real_commands: None, _use_real_sleep: None) -> None:
    environment = os.environ.copy()
    environment.update(
        AUTO_CODER_SUPERVISOR_INSTANCE_ID="beta-worker",
        AUTO_CODER_CHANNEL="beta",
        AUTO_CODER_REPOSITORY="owner/repo",
        AUTO_CODER_RUNTIME_ROOT=str(tmp_path),
        AUTO_CODER_WORKSPACE_ROOT=str(tmp_path),
        AUTO_CODER_SUPERVISOR_AUTO_START="0",
    )
    process = subprocess.Popen([sys.executable, "-m", "auto_coder.worker_supervisor"], env=environment)
    endpoint = tmp_path / "supervisor" / "control.sock"
    try:
        assert request(endpoint, {"operation_id": "initial-inspect", "operation": "INSPECT"})["status"] == "UNKNOWN"
        state_file = tmp_path / "supervisor" / "state.json"
        corrupt_cases = (
            {"schema_version": 1, "operations": {}, "worker": None, "stopped": False, "selected_runtime_id": {"bad": "pointer"}},
            {"schema_version": 1, "operations": {}, "worker": None, "stopped": True, "last_worker": {"runtime_id": "bootstrap"}},
        )
        for index, state in enumerate(corrupt_cases):
            state_file.write_text(json.dumps(state))
            original = state_file.read_bytes()
            assert request(endpoint, {"operation_id": f"start-{index}", "operation": "START"}) == {"status": "NEEDS_ATTENTION"}
            assert request(endpoint, {"operation_id": f"inspect-{index}", "operation": "INSPECT"})["status"] == "NEEDS_ATTENTION"
            assert state_file.read_bytes() == original
            assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_control_socket_survives_client_disconnect_and_remains_queryable(tmp_path: Path, _use_real_commands: None, _use_real_sleep: None) -> None:
    environment = os.environ.copy()
    environment.update(
        AUTO_CODER_SUPERVISOR_INSTANCE_ID="beta-worker",
        AUTO_CODER_CHANNEL="beta",
        AUTO_CODER_REPOSITORY="owner/repo",
        AUTO_CODER_RUNTIME_ROOT=str(tmp_path),
        AUTO_CODER_WORKSPACE_ROOT=str(tmp_path),
        AUTO_CODER_SUPERVISOR_AUTO_START="0",
    )
    endpoint = tmp_path / "supervisor" / "control.sock"
    process = subprocess.Popen([sys.executable, "-m", "auto_coder.worker_supervisor"], env=environment)
    try:
        first = request(endpoint, {"operation_id": "inspect-1", "operation": "INSPECT"})
        assert first["status"] == "UNKNOWN"
        assert process.poll() is None
        second = request(endpoint, {"operation_id": "inspect-2", "operation": "INSPECT"})
        assert second["supervisor_incarnation"] == first["supervisor_incarnation"]
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_aborted_control_client_never_terminates_supervisor(tmp_path: Path, _use_real_commands: None, _use_real_sleep: None) -> None:
    environment = os.environ.copy()
    environment.update(
        AUTO_CODER_SUPERVISOR_INSTANCE_ID="beta-worker",
        AUTO_CODER_CHANNEL="beta",
        AUTO_CODER_REPOSITORY="owner/repo",
        AUTO_CODER_RUNTIME_ROOT=str(tmp_path),
        AUTO_CODER_WORKSPACE_ROOT=str(tmp_path),
        AUTO_CODER_SUPERVISOR_AUTO_START="0",
    )
    endpoint = tmp_path / "supervisor" / "control.sock"
    process = subprocess.Popen([sys.executable, "-m", "auto_coder.worker_supervisor"], env=environment)
    try:
        original = request(endpoint, {"operation_id": "original-inspect", "operation": "INSPECT"})
        for index in range(8):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(endpoint))
                client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                client.sendall(json.dumps({"operation_id": f"aborted-{index}", "operation": "INSPECT"}).encode() + b"\n")
            observed = request(endpoint, {"operation_id": f"after-abort-{index}", "operation": "INSPECT"})
            assert observed["supervisor_incarnation"] == original["supervisor_incarnation"]
            assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_reset_during_response_write_does_not_escape_request_handler(tmp_path: Path) -> None:
    supervisor = WorkerSupervisor(config(tmp_path))

    class ResetStream:
        def readline(self, _limit: int) -> bytes:
            return b'{"operation_id":"inspect","operation":"INSPECT"}\n'

        def write(self, _payload: bytes) -> None:
            raise ConnectionResetError("peer closed before response")

    class ResetConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def settimeout(self, _seconds: int) -> None:
            return None

        def makefile(self, _mode: str) -> ResetStream:
            return ResetStream()

    supervisor._serve(ResetConnection())  # type: ignore[arg-type]


def test_second_supervisor_cannot_take_over_live_control_socket(tmp_path: Path, _use_real_commands: None, _use_real_sleep: None) -> None:
    environment = os.environ.copy()
    environment.update(
        AUTO_CODER_SUPERVISOR_INSTANCE_ID="beta-worker",
        AUTO_CODER_CHANNEL="beta",
        AUTO_CODER_REPOSITORY="owner/repo",
        AUTO_CODER_RUNTIME_ROOT=str(tmp_path),
        AUTO_CODER_WORKSPACE_ROOT=str(tmp_path),
        AUTO_CODER_SUPERVISOR_AUTO_START="0",
    )
    endpoint = tmp_path / "supervisor" / "control.sock"
    first = subprocess.Popen([sys.executable, "-m", "auto_coder.worker_supervisor"], env=environment)
    second: subprocess.Popen[bytes] | None = None
    try:
        original = request(endpoint, {"operation_id": "inspect-first", "operation": "INSPECT"})
        second = subprocess.Popen(
            [sys.executable, "-m", "auto_coder.worker_supervisor"],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        assert second.wait(timeout=10) != 0
        still_owned = request(endpoint, {"operation_id": "inspect-after-contender", "operation": "INSPECT"})
        assert still_owned["supervisor_incarnation"] == original["supervisor_incarnation"]
        assert first.poll() is None
    finally:
        if second is not None and second.poll() is None:
            second.terminate()
            second.wait(timeout=5)
        first.terminate()
        first.wait(timeout=5)


def test_control_socket_survives_child_exit_before_lifetime_capture(
    tmp_path: Path,
    _use_real_commands: None,
    _use_real_sleep: None,
) -> None:
    launches = tmp_path / "launches"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    child_script = f"from pathlib import Path; Path({str(launches)!r}).open('a').write('x')"
    entry = (
        "import sys; from pathlib import Path; "
        "from auto_coder.worker_supervisor import SupervisorConfig, WorkerSupervisor; "
        f"config=SupervisorConfig('beta-worker','beta','owner/repo',Path({str(tmp_path)!r}),Path({str(workspace)!r}),Path(sys.executable),"
        f"Path({str(tmp_path / 'versions')!r}),(sys.executable,'-c',{child_script!r})); "
        "supervisor=WorkerSupervisor(config); original=supervisor._start_token; first=[True]\n"
        "def sample(pid):\n"
        " if first[0]:\n"
        "  first[0]=False; supervisor.child.wait(timeout=5)\n"
        " return original(pid)\n"
        "supervisor._start_token=sample; supervisor.run()"
    )
    process = subprocess.Popen([sys.executable, "-c", entry])
    endpoint = tmp_path / "supervisor" / "control.sock"
    try:
        started = request(endpoint, {"operation_id": "start", "operation": "START"})
        assert started == {
            "status": "UNKNOWN",
            "reason": "child lifetime unavailable during startup",
        }
        inspected = request(endpoint, {"operation_id": "inspect", "operation": "INSPECT"})
        assert inspected["status"] == "UNKNOWN"
        assert process.poll() is None
        assert request(endpoint, {"operation_id": "retry", "operation": "START"}) == {"status": "NEEDS_ATTENTION"}
        assert launches.read_text() == "x"
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_same_id_changed_operation_preserves_original_acceptance(tmp_path: Path) -> None:
    supervisor = WorkerSupervisor(config(tmp_path))
    original = {"operation_id": "K", "operation": "REQUEST_GRACEFUL_STOP"}
    assert supervisor.apply(original) == {"status": "UNKNOWN"}
    assert supervisor.apply(original) == {"status": "UNKNOWN"}
    assert supervisor.apply({"operation_id": "K", "operation": "START"}) == {"status": "CONFLICT"}
    persisted = json.loads((tmp_path / "supervisor" / "state.json").read_text())
    assert persisted["operations"]["K"]["request"] == original


def test_typed_operation_rejects_untrusted_launch_parameters(tmp_path: Path) -> None:
    supervisor = WorkerSupervisor(config(tmp_path))
    malicious_fields = (
        ("command", ["/bin/sh", "-c", "unexpected"]),
        ("interpreter", "/tmp/other-python"),
        ("user", "root"),
        ("network", "host"),
        ("target", "other/repository"),
    )

    class Child:
        pid = 123

    with patch("auto_coder.worker_supervisor.subprocess.Popen", return_value=Child()) as spawn, patch.object(supervisor, "_start_token", return_value="77"):
        for index, (field, value) in enumerate(malicious_fields):
            result = supervisor.apply({"operation_id": f"reject-{index}", "operation": "START", field: value})
            assert result == {"status": "UNSUPPORTED"}
        spawn.assert_not_called()
    assert not supervisor.store.path.exists()


def test_start_uses_fixed_configuration_and_fences_second_start(tmp_path: Path) -> None:
    supervisor = WorkerSupervisor(config(tmp_path))

    class Child:
        pid = 123

    with patch("auto_coder.worker_supervisor.subprocess.Popen", return_value=Child()) as spawn, patch.object(supervisor, "_start_token", return_value="77"):
        result = supervisor.apply({"operation_id": "one", "operation": "START"})
        assert result["status"] == "RUNNING"
        assert result["worker"]["process_run_id"] is None
        assert supervisor.apply({"operation_id": "two", "operation": "START"}) == {"status": "CONFLICT"}
    assert spawn.call_args.args[0][:4] == [sys.executable, "-m", "auto_coder.cli", "process-issues"]
    assert spawn.call_args.kwargs["env"]["AUTO_CODER_DEPLOYMENT_GENERATION"]


def test_stale_worker_fences_do_not_signal_current_real_child(tmp_path: Path) -> None:
    supervisor = WorkerSupervisor(config(tmp_path, (sys.executable, "-c", "import time; time.sleep(60)")))
    started = supervisor.apply({"operation_id": "start", "operation": "START"})
    worker = started["worker"]
    assert isinstance(worker, dict)
    try:
        stale = supervisor.apply({"operation_id": "stale", "operation": "REQUEST_GRACEFUL_STOP", "expected_process_run_id": "old-run", "expected_generation": "old-generation"})
        assert stale == {"status": "CONFLICT"}
        assert supervisor._is_alive(worker)
        exact = supervisor.apply({"operation_id": "exact", "operation": "REQUEST_GRACEFUL_STOP", "expected_generation": worker["deployment_generation"]})
        assert exact["status"] == "DRAINING"
    finally:
        assert supervisor.child is not None
        supervisor.child.wait(timeout=5)


def test_crash_without_terminal_receipt_stays_unknown_and_blocks_start(tmp_path: Path) -> None:
    supervisor = WorkerSupervisor(config(tmp_path, (sys.executable, "-c", "pass")))
    assert supervisor.apply({"operation_id": "start", "operation": "START"})["status"] == "RUNNING"
    assert supervisor.child is not None
    supervisor.child.wait(timeout=5)
    assert supervisor.apply({"operation_id": "stop", "operation": "REQUEST_GRACEFUL_STOP"})["status"] == "UNKNOWN"
    assert supervisor.apply({"operation_id": "new", "operation": "START"}) == {"status": "NEEDS_ATTENTION"}


def test_reconstruction_fences_unresolved_durable_effect(tmp_path: Path) -> None:
    first = WorkerSupervisor(config(tmp_path))
    state = first.store.read()
    state["operations"]["lost"] = {"digest": "digest", "request": {"operation": "REQUEST_GRACEFUL_STOP"}, "result": {"status": "IN_PROGRESS"}}
    first.store.write(state)
    reconstructed = WorkerSupervisor(config(tmp_path))
    assert reconstructed.apply({"operation_id": "new", "operation": "START"}) == {"status": "NEEDS_ATTENTION"}


def test_missing_staged_runtime_never_falls_back_to_bootstrap(tmp_path: Path) -> None:
    supervisor = WorkerSupervisor(config(tmp_path))
    with patch("auto_coder.worker_supervisor.subprocess.Popen") as spawn:
        assert supervisor.apply({"operation_id": "staged", "operation": "START", "runtime_id": "missingruntime"}) == {"status": "UNAVAILABLE"}
    spawn.assert_not_called()


def test_held_staged_verification_does_not_block_inspection(tmp_path: Path) -> None:
    supervisor = WorkerSupervisor(config(tmp_path))
    entered = threading.Event()
    release = threading.Event()
    result: list[dict[str, object]] = []

    def held_resolution(_runtime_id: object) -> None:
        entered.set()
        assert release.wait(timeout=5)
        return None

    with patch.object(supervisor, "_resolve_runtime", side_effect=held_resolution):
        request = threading.Thread(
            target=lambda: result.append(
                supervisor.apply(
                    {
                        "operation_id": "held-stage",
                        "operation": "START",
                        "runtime_id": "preparedruntime",
                    }
                )
            )
        )
        request.start()
        assert entered.wait(timeout=2)

        inspection = supervisor.apply({"operation_id": "inspect-during-stage", "operation": "INSPECT"})
        assert inspection["status"] == "UNKNOWN"
        assert supervisor.store.read()["operations"] == {}

        release.set()
        request.join(timeout=2)
        assert not request.is_alive()
        assert result == [{"status": "UNAVAILABLE"}]
        assert supervisor.store.read()["operations"] == {}


def test_staged_ready_selection_revalidates_exact_receipt_and_profile(tmp_path: Path) -> None:
    supervisor = WorkerSupervisor(config(tmp_path))
    runtime_id = "d" * 32
    attempt = tmp_path / "versions" / "revisions" / ("k" * 64) / "attempt"
    environment = attempt / "venv"
    interpreter = environment / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("test interpreter")
    (environment / ".auto-coder-runtime-id").write_text(runtime_id + "\n")
    source = attempt / "source"
    source.mkdir()
    lock = b"first locked environment"
    (source / "uv.lock").write_bytes(lock)
    provenance = InstalledProvenance("a" * 40, "1.0")
    bindings = {
        "attempt_id": "attempt",
        "runtime_profile": "python-3.12",
        "environment_path": str(environment.resolve()),
        "interpreter_path": str(interpreter),
        "source_sha": provenance.source_revision,
        "distribution_version": provenance.distribution_version,
        "lock_sha256": hashlib.sha256(lock).hexdigest(),
        "installed_runtime_id": runtime_id,
    }
    identity = hashlib.sha256(json.dumps(bindings, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    receipt = ReadyReceipt(1, "a" * 40, "a" * 40, bindings["environment_path"], bindings["interpreter_path"], "1.0", bindings["lock_sha256"], "attempt", "python-3.12", runtime_id, identity, provenance, "2026-10-26T00:00:00+00:00")
    ready_path = attempt / "READY.json"
    ready_path.write_text(json.dumps(asdict(receipt)))

    with patch.object(WorkerVersionPreparer, "_observe", return_value=provenance), patch.object(WorkerVersionPreparer, "_interpreter_prefix", return_value=environment.resolve()):
        selected = supervisor._resolve_runtime(runtime_id)
        assert selected is not None
        assert selected["ready"]["lock_sha256"] == hashlib.sha256(lock).hexdigest()
        assert supervisor._verify_runtime(selected) == selected
        ready_path.write_text(json.dumps(asdict(replace(receipt, installed_at="replaced"))))
        assert supervisor._verify_runtime(selected) is None
        ready_path.write_text(json.dumps(asdict(receipt)))
        (source / "uv.lock").write_bytes(b"changed same-source lock")
        assert supervisor._verify_runtime(selected) is None
        (source / "uv.lock").write_bytes(lock)
        with patch.object(WorkerVersionPreparer, "read_ready", return_value=replace(receipt, runtime_profile="python-3.13")):
            assert supervisor._resolve_runtime(runtime_id) is None


def test_stop_reconciles_only_matching_terminal_receipt_and_exit(tmp_path: Path) -> None:
    supervisor = WorkerSupervisor(config(tmp_path, (sys.executable, "-c", "import time; time.sleep(60)")))
    worker = supervisor.apply({"operation_id": "start", "operation": "START"})["worker"]
    assert isinstance(worker, dict)
    state = supervisor.store.read()
    state["worker"]["process_run_id"] = "real-worker-run"
    supervisor.store.write(state)
    stop = {"operation_id": "stop", "operation": "REQUEST_GRACEFUL_STOP", "expected_process_run_id": "real-worker-run", "expected_generation": worker["deployment_generation"]}
    assert supervisor.apply(stop)["status"] == "DRAINING"
    assert supervisor.child is not None
    supervisor.child.wait(timeout=5)
    assert supervisor.apply(stop)["status"] == "DRAINING"
    receipt = {"schema_version": 1, "repository": "owner/repo", "channel": "beta", "deployment_generation": worker["deployment_generation"], "process_run_id": "real-worker-run", "lifecycle_state": "stopped", "is_graceful_ready": True, "unsettled_count": 0}
    (tmp_path / "last-terminal.json").write_text(json.dumps(receipt))
    assert supervisor.apply(stop) == {"status": "STOPPED"}


def test_restart_refuses_changed_staged_ready_during_held_drain(tmp_path: Path) -> None:
    supervisor = WorkerSupervisor(config(tmp_path, (sys.executable, "-c", "import time; time.sleep(60)")))
    worker = supervisor.apply({"operation_id": "first", "operation": "START"})["worker"]
    assert isinstance(worker, dict)
    state = supervisor.store.read()
    state["worker"]["process_run_id"] = "worker-run"
    supervisor.store.write(state)
    ready = {"installed_runtime_id": "runtimeA", "content_identity": "content1", "source_sha": "a" * 40}
    selected = {"runtime_id": "runtimeA", "interpreter_path": sys.executable, "ready": ready, "ready_path": "/private/READY.json"}
    restart = {"operation_id": "restart", "operation": "RESTART_WORKER", "runtime_id": "runtimeA", "expected_generation": worker["deployment_generation"]}

    with patch.object(supervisor, "_resolve_runtime", return_value=selected) as resolve:
        assert supervisor.apply(restart)["status"] == "DRAINING"
        assert supervisor.child is not None
        supervisor.child.wait(timeout=5)
        receipt = {"schema_version": 1, "repository": "owner/repo", "channel": "beta", "deployment_generation": worker["deployment_generation"], "process_run_id": "worker-run", "lifecycle_state": "stopped", "is_graceful_ready": True, "unsettled_count": 0}
        (tmp_path / "last-terminal.json").write_text(json.dumps(receipt))
        resolve.return_value = {**selected, "ready": {**ready, "content_identity": "content2"}}
        assert supervisor.apply(restart) == {"status": "UNAVAILABLE"}
        assert supervisor.apply(restart) == {"status": "UNAVAILABLE"}

    persisted = supervisor.store.read()
    assert persisted["worker"] is None
    assert persisted["operations"]["restart"]["runtime"] == selected
    assert persisted["last_worker"]["deployment_generation"] == worker["deployment_generation"]


def test_fresh_staged_runtime_binding_never_returns_stale_verified_identity(tmp_path: Path) -> None:
    supervisor = WorkerSupervisor(config(tmp_path, (sys.executable, "-c", "import time; time.sleep(60)")))
    started = supervisor.apply({"operation_id": "start", "operation": "START"})
    worker = started["worker"]
    ready = {"interpreter_path": sys.executable, "source_sha": "a" * 40, "distribution_version": "1.0", "installed_provenance": {"source_revision": "a" * 40, "distribution_version": "1.0"}, "content_identity": "bound-content", "lock_sha256": "b" * 64}
    state = supervisor.store.read()
    state["worker"].update(runtime_id="runtimeA", ready=ready, ready_path="/private/READY.json")
    state["selected_runtime_id"] = "runtimeA"
    supervisor.store.write(state)
    artifact = {"source_revision": {"value": "a" * 40, "available": True}, "distribution_version": {"value": "1.0", "available": True}}
    observation = {"process_run_id": "genuine-run", "artifact": artifact}
    selected = {"runtime_id": "runtimeA", "interpreter_path": sys.executable, "ready": ready, "ready_path": "/private/READY.json"}
    try:
        with patch.object(supervisor, "_verify_runtime", return_value=selected) as verify, patch.object(supervisor, "_observation", return_value=observation) as observe:
            first = supervisor.inspect()["worker"]
            assert first["binding_status"] == "VERIFIED"
            assert first["runtime_binding"]["content_identity"] == "bound-content"

            observe.return_value = {"process_run_id": "genuine-run", "artifact": {**artifact, "distribution_version": {"value": "2.0", "available": True}}}
            second = supervisor.inspect()["worker"]
            assert second["binding_status"] == "CONFLICT"
            assert "runtime_binding" not in second

            observe.return_value = observation
            verify.return_value = None
            third = supervisor.inspect()["worker"]
            assert third["binding_status"] == "UNAVAILABLE"
            assert "runtime_binding" not in third
            assert observe.call_count == 3
    finally:
        assert supervisor.child is not None
        supervisor.child.terminate()
        supervisor.child.wait(timeout=5)

    dead = supervisor.inspect()["worker"]
    assert dead["binding_status"] == "UNAVAILABLE"
    assert "runtime_binding" not in dead
    persisted = supervisor.store.read()["worker"]
    assert persisted["binding_status"] == "UNAVAILABLE"
    assert "runtime_binding" not in persisted

    reconstructed = WorkerSupervisor(config(tmp_path, (sys.executable, "-c", "pass")))
    after_reconstruction = reconstructed.inspect()["worker"]
    assert after_reconstruction["binding_status"] == "UNAVAILABLE"
    assert "runtime_binding" not in after_reconstruction


def test_pid1_start_on_boot_and_sigterm_drains_child(tmp_path: Path, _use_real_commands: None, _use_real_sleep: None) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    child_script = "import time; time.sleep(60)"
    entry = (
        "from pathlib import Path; from auto_coder.worker_supervisor import SupervisorConfig, WorkerSupervisor; "
        f"WorkerSupervisor(SupervisorConfig('beta-worker', 'beta', 'owner/repo', Path({str(tmp_path)!r}), "
        f"Path({str(workspace)!r}), Path({sys.executable!r}), "
        f"child_command=({sys.executable!r}, '-c', {child_script!r}), start_on_boot=True)).run()"
    )
    process = subprocess.Popen([sys.executable, "-c", entry])
    try:
        inspected = request(tmp_path / "supervisor" / "control.sock", {"operation_id": "inspect", "operation": "INSPECT"})
        assert inspected["status"] == "RUNNING"
        assert inspected["worker"]["process_run_id"] is None
        process.terminate()
        assert process.wait(timeout=10) == 0
        state = json.loads((tmp_path / "supervisor" / "state.json").read_text())
        assert state["worker"]["stop_requested"] is True
        assert state["operations"]["container-shutdown-" + inspected["supervisor_incarnation"]]["request"]["operation"] == "REQUEST_GRACEFUL_STOP"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_real_management_bridge_held_drain_and_original_stop_replay(tmp_path: Path, _use_real_commands: None, _use_real_sleep: None) -> None:
    fixture = Path(__file__).parent / "fixtures" / "supervisor_bridge_worker.py"
    supervisor = WorkerSupervisor(config(tmp_path, (sys.executable, str(fixture))))
    started = supervisor.apply({"operation_id": "start", "operation": "START"})
    assert started["status"] == "RUNNING"
    try:
        for _ in range(300):
            live = supervisor.inspect()["worker"]
            if live["process_run_id"]:
                break
            time.sleep(0.02)
        else:
            raise AssertionError("real management bridge never exposed its worker-owned run ID")
        assert live["process_run_id"] != live["deployment_generation"]
        assert live["binding_status"] == "UNAVAILABLE"  # No invented bootstrap lock identity.

        stop = {"operation_id": "stop", "operation": "REQUEST_GRACEFUL_STOP", "expected_process_run_id": live["process_run_id"], "expected_generation": live["deployment_generation"]}
        assert supervisor.apply(stop)["status"] == "DRAINING"
        for _ in range(300):
            if (tmp_path / "fixture-draining").exists():
                break
            time.sleep(0.02)
        else:
            raise AssertionError("real bridge worker never entered held drain")

        assert supervisor.apply({**stop, "operation": "RESTART_WORKER"}) == {"status": "CONFLICT"}
        assert supervisor.apply({**stop, "expected_generation": "stale"}) == {"status": "CONFLICT"}
        assert supervisor.apply(stop)["status"] == "DRAINING"
        assert supervisor.child is not None
        assert supervisor.child.poll() is None
        assert not (tmp_path / "last-terminal.json").exists()

        (tmp_path / "fixture-release").write_text("release")
        supervisor.child.wait(timeout=10)
        assert supervisor.apply(stop) == {"status": "STOPPED"}
        assert supervisor.apply(stop) == {"status": "STOPPED"}
        receipt = json.loads((tmp_path / "last-terminal.json").read_text())
        assert receipt["process_run_id"] == live["process_run_id"]
        assert receipt["deployment_generation"] == live["deployment_generation"]
        assert receipt["terminal_reason"] == "supervisor SIGTERM"
        assert supervisor.store.read()["operations"]["stop"]["request"] == stop
    finally:
        (tmp_path / "fixture-release").write_text("release")
        if supervisor.child is not None and supervisor.child.poll() is None:
            supervisor.child.terminate()
            supervisor.child.wait(timeout=10)


def test_real_held_stop_survives_lost_response_and_supervisor_death(tmp_path: Path, _use_real_commands: None, _use_real_sleep: None) -> None:
    fixture = Path(__file__).parent / "fixtures" / "supervisor_bridge_worker.py"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    entry = (
        "import sys; from pathlib import Path; "
        "from auto_coder.worker_supervisor import SupervisorConfig, WorkerSupervisor; "
        f"config=SupervisorConfig('beta-worker','beta','owner/repo',Path({str(tmp_path)!r}),"
        f"Path({str(workspace)!r}),Path(sys.executable),"
        f"child_command=(sys.executable,{str(fixture)!r}),start_on_boot=True); "
        "WorkerSupervisor(config).run()"
    )
    endpoint = tmp_path / "supervisor" / "control.sock"
    first = subprocess.Popen([sys.executable, "-c", entry])
    second: subprocess.Popen[bytes] | None = None
    worker_pid: int | None = None
    worker_token: str | None = None
    try:
        for _ in range(500):
            status = request(endpoint, {"operation_id": "inspect-live", "operation": "INSPECT"})
            worker = status.get("worker")
            if isinstance(worker, dict) and worker.get("process_run_id"):
                break
            time.sleep(0.02)
        else:
            raise AssertionError("production bridge did not expose its live process-run ID")
        worker_pid = worker["pid"]
        worker_token = worker["start_token"]
        stop = {
            "operation_id": "held-original",
            "operation": "REQUEST_GRACEFUL_STOP",
            "expected_process_run_id": worker["process_run_id"],
            "expected_generation": worker["deployment_generation"],
        }
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(endpoint))
            client.sendall(json.dumps(stop).encode() + b"\n")
            # Close without observing the result, as with a dropped transport.

        for _ in range(300):
            if (tmp_path / "fixture-draining").exists():
                break
            time.sleep(0.02)
        else:
            raise AssertionError("real child did not enter held drain")

        for _ in range(300):
            accepted = json.loads((tmp_path / "supervisor" / "state.json").read_text())["operations"]["held-original"]
            if accepted["result"]["status"] == "DRAINING":
                break
            time.sleep(0.02)
        else:
            raise AssertionError("lost-response stop acceptance did not persist DRAINING")
        assert accepted["request"] == stop
        assert accepted["result"]["status"] == "DRAINING"
        first.kill()
        first.wait(timeout=5)

        second = subprocess.Popen([sys.executable, "-c", entry])
        replay = request(endpoint, stop)
        assert replay["status"] == "DRAINING"
        assert request(endpoint, {**stop, "operation": "RESTART_WORKER"}) == {"status": "CONFLICT"}
        assert request(endpoint, {**stop, "expected_generation": "changed"}) == {"status": "CONFLICT"}
        assert request(endpoint, {"operation_id": "another-start", "operation": "START"}) == {"status": "NEEDS_ATTENTION"}
        assert not (tmp_path / "last-terminal.json").exists()

        (tmp_path / "fixture-release").write_text("release")
        for _ in range(500):
            if request(endpoint, stop) == {"status": "STOPPED"}:
                break
            time.sleep(0.02)
        else:
            raise AssertionError("reconstructed stop did not reconcile terminal receipt and exit")
        saved = json.loads((tmp_path / "supervisor" / "state.json").read_text())
        assert saved["operations"]["held-original"]["request"] == stop
        assert saved["operations"]["held-original"]["result"] == {"status": "STOPPED"}
        assert saved["last_worker"]["pid"] == worker_pid
        assert saved["worker"] is None
    finally:
        (tmp_path / "fixture-release").write_text("release")
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
        if worker_pid is not None and worker_token is not None:
            try:
                if WorkerSupervisor._start_token(worker_pid) == worker_token:
                    os.kill(worker_pid, signal.SIGTERM)
            except (OSError, ValueError):
                pass
