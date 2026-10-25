import json
from pathlib import Path
from unittest.mock import patch

from auto_coder.worker_supervisor import SupervisorConfig, WorkerSupervisor
from auto_coder.worker_supervisor_store import SupervisorStore


def config(tmp_path: Path) -> SupervisorConfig:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return SupervisorConfig("beta-worker", "beta", "owner/repo", tmp_path, workspace, Path("/usr/bin/python3"))


def test_store_rejects_corrupt_schema(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"schema_version":2}')
    try:
        SupervisorStore(path).read()
    except ValueError as error:
        assert str(error) == "invalid supervisor state"
    else:
        raise AssertionError("corrupt state was accepted")


def test_same_id_changed_operation_conflicts_without_second_effect(tmp_path: Path) -> None:
    supervisor = WorkerSupervisor(config(tmp_path))
    request = {"operation_id": "K", "operation": "REQUEST_GRACEFUL_STOP"}
    assert supervisor.apply(request) == {"status": "STOPPED"}
    assert supervisor.apply(request) == {"status": "STOPPED"}
    assert supervisor.apply({"operation_id": "K", "operation": "START"}) == {"status": "CONFLICT"}
    persisted = json.loads((tmp_path / "supervisor" / "state.json").read_text())
    assert list(persisted["operations"]) == ["K"]


def test_start_uses_fixed_configuration_and_fences_concurrent_start(tmp_path: Path) -> None:
    supervisor = WorkerSupervisor(config(tmp_path))

    class Child:
        pid = 123

    with patch("auto_coder.worker_supervisor.subprocess.Popen", return_value=Child()) as spawn, patch.object(supervisor, "_start_token", return_value="77"):
        result = supervisor.apply({"operation_id": "one", "operation": "START"})
        assert result["status"] == "RUNNING"
        assert supervisor.apply({"operation_id": "two", "operation": "START"}) == {"status": "CONFLICT"}
    command = spawn.call_args.args[0]
    assert command[:4] == ["/usr/bin/python3", "-m", "auto_coder.cli", "process-issues"]
    assert command[5] == "owner/repo"
    assert spawn.call_args.kwargs["env"]["AUTO_CODER_DEPLOYMENT_GENERATION"]


def test_stop_signals_only_reuse_safe_observed_pid(tmp_path: Path) -> None:
    supervisor = WorkerSupervisor(config(tmp_path))
    state = supervisor.store.read()
    state["worker"] = {"pid": 42, "start_token": "9", "process_run_id": "run", "deployment_generation": "generation"}
    supervisor.store.write(state)
    with patch.object(supervisor, "_start_token", return_value="9"), patch("auto_coder.worker_supervisor.os.kill") as kill:
        assert supervisor.apply({"operation_id": "stop", "operation": "REQUEST_GRACEFUL_STOP"})["status"] == "DRAINING"
    kill.assert_called_once_with(42, 15)
