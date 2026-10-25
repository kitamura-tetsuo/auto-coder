import json
import socket
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

from auto_coder.worker_supervisor import SupervisorConfig, WorkerSupervisor
from auto_coder.worker_supervisor_store import SupervisorStore


def config(tmp_path: Path, command: tuple[str, ...] | None = None) -> SupervisorConfig:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return SupervisorConfig("beta-worker", "beta", "owner/repo", tmp_path, workspace, Path(sys.executable), tmp_path / "versions", command)


def request(endpoint: Path, payload: dict[str, object]) -> dict[str, object]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    for _ in range(100):
        try:
            client.connect(str(endpoint))
            break
        except (FileNotFoundError, ConnectionRefusedError):
            time.sleep(0.01)
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


def test_control_socket_survives_client_disconnect_and_remains_queryable(tmp_path: Path) -> None:
    supervisor = WorkerSupervisor(config(tmp_path))
    thread = threading.Thread(target=supervisor.run, daemon=True)
    thread.start()
    first = request(supervisor.control_socket, {"operation_id": "inspect-1", "operation": "INSPECT"})
    assert first["status"] == "UNKNOWN"
    assert thread.is_alive()
    second = request(supervisor.control_socket, {"operation_id": "inspect-2", "operation": "INSPECT"})
    assert second["supervisor_incarnation"] == first["supervisor_incarnation"]
    assert thread.is_alive()


def test_same_id_changed_operation_preserves_original_acceptance(tmp_path: Path) -> None:
    supervisor = WorkerSupervisor(config(tmp_path))
    original = {"operation_id": "K", "operation": "REQUEST_GRACEFUL_STOP"}
    assert supervisor.apply(original) == {"status": "UNKNOWN"}
    assert supervisor.apply(original) == {"status": "UNKNOWN"}
    assert supervisor.apply({"operation_id": "K", "operation": "START"}) == {"status": "CONFLICT"}
    persisted = json.loads((tmp_path / "supervisor" / "state.json").read_text())
    assert persisted["operations"]["K"]["request"] == original


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
