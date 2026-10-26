"""Production-image Supervisor integration with disposable, networkless Docker containers."""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

pytestmark = pytest.mark.supervisor_integration

_FIXTURES = Path(__file__).parent / "fixtures"
_COMPOSE_FILE = _FIXTURES / "compose.supervisor-integration.yml"


def _image() -> str:
    image = os.environ.get("AUTOCODER_SUPERVISOR_IMAGE", "")
    if image:
        return image
    if os.environ.get("AUTO_CODER_REQUIRE_SUPERVISOR_INTEGRATION") == "1":
        pytest.fail("Supervisor Integration Tests require a prepared production image")
    pytest.skip("set AUTOCODER_SUPERVISOR_IMAGE to run isolated Docker integration")


def _docker(*arguments: str, environment: dict[str, str] | None = None, timeout: int = 30, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *arguments], capture_output=True, text=True, check=check, timeout=timeout, env=environment)


def _wait_for(predicate, *, timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            last_error = error
        time.sleep(0.1)
    raise AssertionError(f"Supervisor fixture did not become ready: {last_error}")


def _control(container: str, operation: dict[str, str]) -> dict[str, object]:
    result = _docker("exec", container, "python", "/fixtures/supervisor_control_client.py", json.dumps(operation), timeout=10)
    response = json.loads(result.stdout)
    assert isinstance(response, dict)
    return response


def _send_without_reading_response(container: str, operation: dict[str, str]) -> None:
    _docker("exec", container, "python", "/fixtures/supervisor_control_client.py", json.dumps(operation), "--drop-response", timeout=10)


def _inspect_worker(container: str, *, generation: str | None = None) -> dict[str, object] | None:
    result = _control(container, {"operation_id": "inspect-" + uuid.uuid4().hex, "operation": "INSPECT"})
    worker = result.get("worker")
    if result.get("status") != "RUNNING" or not isinstance(worker, dict) or not worker.get("process_run_id"):
        return None
    if generation is not None and worker.get("deployment_generation") == generation:
        return None
    return worker


def _container_facts(container: str) -> dict[str, object]:
    script = (
        "import json,shutil; from pathlib import Path; "
        "print(json.dumps({'pid1':Path('/proc/1/cmdline').read_bytes().decode().split('\\0'), "
        "'socket':Path('/runtime/supervisor/control.sock').is_socket(), "
        "'docker_socket':Path('/var/run/docker.sock').exists(), "
        "'docker_binary':shutil.which('docker')}))"
    )
    result = _docker("exec", container, "python", "-c", script)
    return json.loads(result.stdout)


def test_production_entrypoint_is_private_pid1_without_docker_socket(tmp_path: Path) -> None:
    image = _image()
    entry = json.loads(_docker("image", "inspect", "--format", "{{json .Config.Entrypoint}}", image).stdout)
    assert entry == ["python", "-m", "auto_coder.worker_supervisor"]
    runtime, workspace = tmp_path / "runtime", tmp_path / "workspace"
    runtime.mkdir()
    workspace.mkdir()
    (runtime / "home").mkdir()
    container = "supervisor-entry-" + uuid.uuid4().hex[:12]
    try:
        _docker(
            "run",
            "-d",
            "--rm",
            "--name",
            container,
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--security-opt",
            "no-new-privileges:true",
            "-v",
            f"{runtime}:/runtime",
            "-v",
            f"{workspace}:/workspace",
            "-e",
            "AUTO_CODER_SUPERVISOR_INSTANCE_ID=release-worker",
            "-e",
            "AUTO_CODER_CHANNEL=release",
            "-e",
            "AUTO_CODER_REPOSITORY=fixture/release",
            "-e",
            "AUTO_CODER_RUNTIME_ROOT=/runtime",
            "-e",
            "AUTO_CODER_WORKSPACE_ROOT=/workspace",
            "-e",
            "AUTO_CODER_SUPERVISOR_AUTO_START=0",
            "-e",
            "HOME=/runtime/home",
            image,
        )
        facts = _wait_for(lambda: (value if value["socket"] else None) if (value := _container_facts(container)) else None)
        assert facts["pid1"][:3] == ["python", "-m", "auto_coder.worker_supervisor"]
        assert facts["docker_socket"] is False
        assert facts["docker_binary"] is None
        assert not (runtime / "supervisor" / "state.json").exists()
        assert (runtime / "supervisor" / "control.sock").is_socket()
    finally:
        _docker("rm", "-f", container, check=False)


def test_release_beta_held_drain_replay_restart_and_crash_reconstruction(tmp_path: Path) -> None:
    image = _image()
    run_root = tmp_path / "isolated"
    for name in ("release", "beta", "release-workspace", "beta-workspace"):
        (run_root / name).mkdir(parents=True)
    for channel in ("release", "beta"):
        (run_root / channel / "home").mkdir()
    environment = dict(os.environ)
    environment.update(
        AUTOCODER_SUPERVISOR_IMAGE=image,
        SUPERVISOR_RUN_ROOT=str(run_root),
        SUPERVISOR_FIXTURES_DIR=str(_FIXTURES.resolve()),
        SUPERVISOR_HOST_UID=str(os.getuid()),
        SUPERVISOR_HOST_GID=str(os.getgid()),
    )
    project = "supervisor-" + uuid.uuid4().hex[:12]
    compose = ("compose", "--project-name", project, "-f", str(_COMPOSE_FILE))

    def compose_command(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return _docker(*compose, *arguments, environment=environment, timeout=45, check=check)

    try:
        compose_command("up", "-d", "--no-build")
        release = compose_command("ps", "-q", "release").stdout.strip()
        beta = compose_command("ps", "-q", "beta").stdout.strip()
        assert release and beta and release != beta
        first_release = _wait_for(lambda: _inspect_worker(release))
        first_beta = _wait_for(lambda: _inspect_worker(beta))
        assert first_release["deployment_generation"] != first_beta["deployment_generation"]
        for container in (release, beta):
            facts = _container_facts(container)
            assert facts["pid1"][:2] == ["python", "/fixtures/supervisor_container_entry.py"]
            assert facts["docker_socket"] is False
            assert facts["docker_binary"] is None

        stop = {
            "operation_id": "release-stop",
            "operation": "REQUEST_GRACEFUL_STOP",
            "expected_generation": str(first_release["deployment_generation"]),
            "expected_process_run_id": str(first_release["process_run_id"]),
        }
        assert _control(release, {**stop, "operation_id": "wrong-generation", "expected_generation": "stale"})["status"] == "CONFLICT"
        _send_without_reading_response(release, stop)
        _wait_for(lambda: (run_root / "release" / "fixture-draining").exists())
        assert _control(release, {**stop, "operation": "RESTART_WORKER"})["status"] == "CONFLICT"
        assert _control(release, {**stop, "expected_process_run_id": "stale"})["status"] == "CONFLICT"
        assert _control(release, stop)["status"] == "DRAINING"
        assert not (run_root / "release" / "last-terminal.json").exists()
        assert _inspect_worker(beta) is not None
        (run_root / "release" / "fixture-release").write_text("release")
        _wait_for(lambda: _control(release, stop)["status"] == "STOPPED")
        receipt = json.loads((run_root / "release" / "last-terminal.json").read_text())
        assert receipt["process_run_id"] == first_release["process_run_id"]
        assert receipt["deployment_generation"] == first_release["deployment_generation"]
        assert _inspect_worker(beta) is not None

        with ThreadPoolExecutor(max_workers=2) as pool:
            starts = list(pool.map(lambda index: _control(release, {"operation_id": f"racing-start-{index}", "operation": "START"}), range(2)))
        assert sorted(result["status"] for result in starts) == ["CONFLICT", "RUNNING"]
        second_release = _wait_for(lambda: _inspect_worker(release, generation=str(first_release["deployment_generation"])))
        assert second_release["deployment_generation"] != first_release["deployment_generation"]

        restart = {
            "operation_id": "beta-restart",
            "operation": "RESTART_WORKER",
            "runtime_id": "bootstrap",
            "expected_generation": str(first_beta["deployment_generation"]),
            "expected_process_run_id": str(first_beta["process_run_id"]),
        }
        assert _control(beta, restart)["status"] == "DRAINING"
        _wait_for(lambda: (run_root / "beta" / "fixture-draining").exists())
        assert _control(beta, {**restart, "runtime_id": "another-intended-runtime"})["status"] == "CONFLICT"
        (run_root / "beta" / "fixture-release").write_text("release")
        _wait_for(lambda: _control(beta, restart)["status"] == "RUNNING")
        next_beta = _wait_for(lambda: _inspect_worker(beta, generation=str(first_beta["deployment_generation"])))
        assert next_beta["process_run_id"] != first_beta["process_run_id"]
        replay = _control(beta, restart)
        assert replay["status"] == "RUNNING"
        assert replay["worker"]["deployment_generation"] == next_beta["deployment_generation"]

        old_incarnation = _control(beta, {"operation_id": "before-crash", "operation": "INSPECT"})["supervisor_incarnation"]
        _docker("kill", "--signal", "KILL", beta)
        compose_command("up", "-d", "--no-build", "beta")
        recovered = compose_command("ps", "-q", "beta").stdout.strip()
        assert recovered
        state = _wait_for(lambda: _control(recovered, {"operation_id": "after-crash", "operation": "INSPECT"}))
        assert state["supervisor_incarnation"] != old_incarnation
        assert state["status"] == "UNKNOWN"
        assert state["worker"]["deployment_generation"] == next_beta["deployment_generation"]
        assert _control(recovered, {"operation_id": "no-crash-loop", "operation": "START"})["status"] in {"CONFLICT", "NEEDS_ATTENTION"}
    finally:
        logs = compose_command("logs", "--no-color", check=False)
        if logs.stdout or logs.stderr:
            print(logs.stdout + logs.stderr)
        compose_command("down", "--remove-orphans", "--timeout", "3", check=False)
