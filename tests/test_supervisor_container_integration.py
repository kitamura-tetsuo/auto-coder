"""Production-image Supervisor integration with disposable, networkless Docker containers."""

from __future__ import annotations

import hashlib
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


def _worker_observation(container: str) -> dict[str, object]:
    """Read fresh #2599 evidence directly from the worker, not cached state."""
    nonce = uuid.uuid4().hex
    result = _docker("exec", container, "python", "/fixtures/supervisor_observation_client.py", nonce, timeout=10)
    observation = json.loads(result.stdout)
    assert observation["schema_version"] == 1
    assert observation["nonce"] == nonce
    return observation


def test_production_entrypoint_is_private_pid1_without_docker_socket(tmp_path: Path) -> None:
    image = _image()
    entry = json.loads(_docker("image", "inspect", "--format", "{{json .Config.Entrypoint}}", image).stdout)
    assert entry == ["auto-coder"]
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
            "--entrypoint",
            "python",
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
            "-m",
            "auto_coder.worker_supervisor",
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


def test_real_process_issues_bootstrap_and_staged_ready_binding(tmp_path: Path, _use_real_commands: None) -> None:
    """REQ-011: run the real Supervisor and CLI in bootstrap and #2610 runtime.

    Only the isolated preparation container can download dependencies. A
    fixture uses supported process-issues flags to avoid a live credential
    preflight; the real worker and Supervisor run without external network.
    """
    image = _image()
    source = Path(os.environ.get("GITHUB_WORKSPACE", str(Path(__file__).parents[1]))).resolve()
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source, check=True, capture_output=True, text=True).stdout.strip()
    runtime, workspace, routing = (tmp_path / name for name in ("runtime", "workspace", "routing"))
    for directory in (runtime, workspace, routing, runtime / "home", runtime / "logs"):
        directory.mkdir(exist_ok=True)
    repository = "fixture/production"
    (routing / "ownership.json").write_text(json.dumps({repository: "release"}))
    (runtime / "llm_config.toml").write_text('[backend]\norder = ["jules"]\n\n[backends.jules]\nbackend_type = "jules"\nenabled = true\napi_key = "ci-offline-unused"\n')
    fixtures = _FIXTURES.resolve()
    user = f"{os.getuid()}:{os.getgid()}"

    try:
        prepared = _docker(
            "run",
            "--rm",
            "--network",
            "bridge",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--user",
            user,
            "-v",
            f"{source}:/source:ro",
            "-v",
            f"{runtime}:/runtime",
            "-v",
            f"{fixtures}:/fixtures:ro",
            "-e",
            "HOME=/runtime/home",
            "--entrypoint",
            "python",
            image,
            "/fixtures/supervisor_stage_ready.py",
            revision,
            timeout=1200,
        )
    except subprocess.CalledProcessError as error:
        pytest.fail(f"Real staged preparation failed:\n{error.stdout[-8000:]}\n{error.stderr[-8000:]}")
    staged = json.loads(prepared.stdout.strip().splitlines()[-1])
    ready = staged["receipt"]
    assert ready["source_sha"] == ready["installed_provenance"]["source_revision"] == revision
    assert ready["installed_runtime_id"] != "bootstrap"
    ready_path = runtime / Path(staged["ready_path"]).relative_to("/runtime")
    marker_path = ready_path.parent / "venv" / ".auto-coder-runtime-id"
    lock_path = ready_path.parent / "source" / "uv.lock"
    original_ready, original_marker, original_lock = ready_path.read_bytes(), marker_path.read_bytes(), lock_path.read_bytes()

    container = "supervisor-production-" + uuid.uuid4().hex[:12]
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
            "--security-opt",
            "no-new-privileges:true",
            "--user",
            user,
            "--entrypoint",
            "python",
            "-v",
            f"{runtime}:/runtime",
            "-v",
            f"{workspace}:/workspace",
            "-v",
            f"{routing}:/routing",
            "-v",
            f"{fixtures}:/fixtures:ro",
            "-e",
            "AUTO_CODER_SUPERVISOR_INSTANCE_ID=release-worker",
            "-e",
            "AUTO_CODER_CHANNEL=release",
            "-e",
            f"AUTO_CODER_REPOSITORY={repository}",
            "-e",
            "AUTO_CODER_RUNTIME_ROOT=/runtime",
            "-e",
            "AUTO_CODER_WORKSPACE_ROOT=/workspace",
            "-e",
            "AUTO_CODER_OWNERSHIP_FILE=/routing/ownership.json",
            "-e",
            "AUTO_CODER_ARTIFACT_DIGEST=ci-isolated",
            "-e",
            "AUTO_CODER_SUPERVISOR_AUTO_START=1",
            "-e",
            "AUTO_CODER_CONFIG_PATH=/runtime/llm_config.toml",
            "-e",
            "AUTO_CODER_HEALTH_LOG_ENABLED=0",
            "-e",
            "AUTO_CODER_DISABLE_AUTO_UPDATE=1",
            "-e",
            "GITHUB_TOKEN=nonfunctional-ci-placeholder",
            "-e",
            "HOME=/runtime/home",
            image,
            "/fixtures/supervisor_offline_pid1.py",
        )
        facts = _wait_for(lambda: value if (value := _container_facts(container))["socket"] else None)
        assert facts["pid1"][:2] == ["python", "/fixtures/supervisor_offline_pid1.py"]
        assert facts["docker_socket"] is False
        assert facts["docker_binary"] is None

        bootstrap = _wait_for(lambda: _inspect_worker(container), timeout=60)
        assert bootstrap["runtime_id"] == "bootstrap"
        assert bootstrap["binding_status"] == "UNAVAILABLE"
        initial_observation = _worker_observation(container)
        assert initial_observation["process_run_id"] == bootstrap["process_run_id"]
        assert initial_observation["artifact"]["source_revision"]["value"] == revision
        initial_stop = {"operation_id": "bootstrap-stop", "operation": "REQUEST_GRACEFUL_STOP", "expected_generation": str(bootstrap["deployment_generation"]), "expected_process_run_id": str(bootstrap["process_run_id"])}
        assert _control(container, initial_stop)["status"] in {"DRAINING", "STOPPED"}
        _wait_for(lambda: _control(container, initial_stop)["status"] == "STOPPED", timeout=90)

        staged_id = ready["installed_runtime_id"]
        assert _control(container, {"operation_id": "staged-start", "operation": "START", "runtime_id": staged_id})["status"] == "RUNNING"
        bound = _wait_for(lambda: worker if (worker := _inspect_worker(container)) and worker.get("binding_status") == "VERIFIED" else None, timeout=90)
        binding = bound["runtime_binding"]
        assert bound["runtime_id"] == staged_id
        assert binding["content_identity"] == ready["content_identity"]
        assert binding["lock_sha256"] == ready["lock_sha256"]
        assert binding["source_sha"] == revision
        assert binding["process_run_id"] == bound["process_run_id"]
        assert binding["deployment_generation"] == bound["deployment_generation"]
        assert binding["start_token"] == bound["start_token"]
        live = _worker_observation(container)
        assert live["process_run_id"] == bound["process_run_id"]
        assert live["deployment_generation"] == binding["deployment_generation"]
        assert live["artifact"]["distribution_version"]["value"] == ready["distribution_version"]
        assert live["artifact"]["source_revision"]["value"] == revision

        # Independently valid replacement READY, identical source SHA, distinct
        # runtime/content identity: it must not inherit the old live binding.
        alternative = dict(ready)
        alternative_id = uuid.uuid4().hex
        alternative["installed_runtime_id"] = alternative_id
        fields = ("attempt_id", "runtime_profile", "environment_path", "interpreter_path", "source_sha", "distribution_version", "lock_sha256", "installed_runtime_id")
        alternative["content_identity"] = hashlib.sha256(json.dumps({name: alternative[name] for name in fields}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        try:
            marker_path.write_text(alternative_id + "\n")
            ready_path.write_text(json.dumps(alternative))
            checked = json.loads(_docker("exec", container, "python", "/fixtures/supervisor_stage_ready.py", "--verify", staged["ready_path"], timeout=20).stdout)
            assert checked["installed_runtime_id"] == alternative_id
            assert checked["source_sha"] == revision
            assert checked["content_identity"] != binding["content_identity"]
            unbound = _control(container, {"operation_id": "substituted-ready", "operation": "INSPECT"})
            assert unbound["status"] == "RUNNING"
            assert unbound["worker"]["binding_status"] == "UNAVAILABLE"
            assert "runtime_binding" not in unbound["worker"]
        finally:
            marker_path.write_bytes(original_marker)
            ready_path.write_bytes(original_ready)
        _wait_for(lambda: worker if (worker := _inspect_worker(container)) and worker.get("binding_status") == "VERIFIED" else None)

        try:
            lock_path.write_bytes(original_lock + b"\nsubstituted lock for the same revision\n")
            unbound_lock = _control(container, {"operation_id": "substituted-lock", "operation": "INSPECT"})
            assert unbound_lock["worker"]["binding_status"] == "UNAVAILABLE"
            assert "runtime_binding" not in unbound_lock["worker"]
        finally:
            lock_path.write_bytes(original_lock)
        _wait_for(lambda: worker if (worker := _inspect_worker(container)) and worker.get("binding_status") == "VERIFIED" else None)

        stop = {"operation_id": "staged-stop", "operation": "REQUEST_GRACEFUL_STOP", "expected_generation": str(bound["deployment_generation"]), "expected_process_run_id": str(bound["process_run_id"])}
        assert _control(container, stop)["status"] in {"DRAINING", "STOPPED"}
        _wait_for(lambda: _control(container, stop)["status"] == "STOPPED", timeout=90)
        assert _control(container, {**stop, "operation": "RESTART_WORKER"})["status"] == "CONFLICT"

        assert _control(container, {"operation_id": "staged-next", "operation": "START", "runtime_id": staged_id})["status"] == "RUNNING"
        successor = _wait_for(lambda: worker if (worker := _inspect_worker(container)) and worker.get("binding_status") == "VERIFIED" else None, timeout=90)
        assert successor["runtime_binding"]["content_identity"] == binding["content_identity"]
        assert successor["process_run_id"] != bound["process_run_id"]
        assert successor["deployment_generation"] != bound["deployment_generation"]
        assert _control(container, {"operation_id": "stale-stop", "operation": "REQUEST_GRACEFUL_STOP", "expected_process_run_id": str(bound["process_run_id"]), "expected_generation": str(bound["deployment_generation"])})["status"] == "CONFLICT"
        final_stop = {"operation_id": "successor-stop", "operation": "REQUEST_GRACEFUL_STOP", "expected_generation": str(successor["deployment_generation"]), "expected_process_run_id": str(successor["process_run_id"])}
        assert _control(container, final_stop)["status"] in {"DRAINING", "STOPPED"}
        _wait_for(lambda: _control(container, final_stop)["status"] == "STOPPED", timeout=90)
    finally:
        logs = _docker("logs", container, timeout=10, check=False)
        if logs.stdout or logs.stderr:
            print(logs.stdout + logs.stderr)
        _docker("rm", "-f", container, check=False)


def test_independently_prepared_staged_restart_preserves_held_intent(tmp_path: Path, _use_real_commands: None) -> None:
    """A dropped-response staged restart cannot be rebound to another valid READY."""
    image = _image()
    source = Path(os.environ.get("GITHUB_WORKSPACE", str(Path(__file__).parents[1]))).resolve()
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source, check=True, capture_output=True, text=True).stdout.strip()
    runtime, workspace = tmp_path / "runtime", tmp_path / "workspace"
    for directory in (runtime, workspace, runtime / "home"):
        directory.mkdir(exist_ok=True)
    fixtures = _FIXTURES.resolve()
    user = f"{os.getuid()}:{os.getgid()}"

    prepared = []
    for attempt in ("held-staged-a", "held-staged-b"):
        try:
            result = _docker(
                "run",
                "--rm",
                "--network",
                "bridge",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--user",
                user,
                "-v",
                f"{source}:/source:ro",
                "-v",
                f"{runtime}:/runtime",
                "-v",
                f"{fixtures}:/fixtures:ro",
                "-e",
                "HOME=/runtime/home",
                "--entrypoint",
                "python",
                image,
                "/fixtures/supervisor_stage_ready.py",
                revision,
                attempt,
                timeout=1200,
            )
        except subprocess.CalledProcessError as error:
            pytest.fail(f"Independent staged preparation {attempt} failed:\n{error.stdout[-8000:]}\n{error.stderr[-8000:]}")
        prepared.append(json.loads(result.stdout.strip().splitlines()[-1])["receipt"])
    original, destination = prepared
    assert original["source_sha"] == destination["source_sha"] == revision
    assert original["installed_runtime_id"] != destination["installed_runtime_id"]
    assert original["content_identity"] != destination["content_identity"]

    container = "supervisor-held-staged-" + uuid.uuid4().hex[:12]
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
            user,
            "--security-opt",
            "no-new-privileges:true",
            "--entrypoint",
            "python",
            "-v",
            f"{runtime}:/runtime",
            "-v",
            f"{workspace}:/workspace",
            "-v",
            f"{fixtures}:/fixtures:ro",
            "-e",
            "HOME=/runtime/home",
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
            image,
            "/fixtures/supervisor_container_entry.py",
        )
        _wait_for(lambda: value if (value := _container_facts(container))["socket"] else None)
        first = _control(container, {"operation_id": "first-staged", "operation": "START", "runtime_id": original["installed_runtime_id"]})
        assert first["status"] == "RUNNING"
        first_worker = _wait_for(lambda: worker if (worker := _inspect_worker(container)) and worker.get("binding_status") == "VERIFIED" else None)
        assert first_worker["runtime_binding"]["content_identity"] == original["content_identity"]

        restart = {
            "operation_id": "accepted-staged-restart",
            "operation": "RESTART_WORKER",
            "runtime_id": str(destination["installed_runtime_id"]),
            "expected_generation": str(first_worker["deployment_generation"]),
            "expected_process_run_id": str(first_worker["process_run_id"]),
        }
        _send_without_reading_response(container, restart)
        _wait_for(lambda: (runtime / "fixture-draining").exists())
        assert _control(container, restart)["status"] == "DRAINING"
        assert _control(container, {**restart, "runtime_id": original["installed_runtime_id"]})["status"] == "CONFLICT"
        assert _control(container, {**restart, "expected_generation": "stale"})["status"] == "CONFLICT"
        assert _control(container, {**restart, "operation": "REQUEST_GRACEFUL_STOP"})["status"] == "CONFLICT"
        assert not (runtime / "last-terminal.json").exists()
        saved = json.loads((runtime / "supervisor" / "state.json").read_text())["operations"][restart["operation_id"]]
        assert saved["request"] == restart
        assert saved["result"]["status"] == "DRAINING"

        (runtime / "fixture-release").write_text("release")
        _wait_for(lambda: _control(container, restart)["status"] == "RUNNING", timeout=90)
        successor = _wait_for(
            lambda: worker if (worker := _inspect_worker(container, generation=str(first_worker["deployment_generation"]))) and worker.get("binding_status") == "VERIFIED" else None,
            timeout=90,
        )
        assert successor["runtime_id"] == destination["installed_runtime_id"]
        assert successor["runtime_binding"]["content_identity"] == destination["content_identity"]
        assert successor["process_run_id"] != first_worker["process_run_id"]
        assert _control(container, restart)["worker"]["deployment_generation"] == successor["deployment_generation"]
        assert _control(container, {**restart, "runtime_id": original["installed_runtime_id"]})["status"] == "CONFLICT"
        assert _control(container, {"operation_id": "stale-stop-after-restart", "operation": "REQUEST_GRACEFUL_STOP", "expected_generation": str(first_worker["deployment_generation"]), "expected_process_run_id": str(first_worker["process_run_id"])})["status"] == "CONFLICT"

        stop = {"operation_id": "final-staged-stop", "operation": "REQUEST_GRACEFUL_STOP", "expected_generation": str(successor["deployment_generation"]), "expected_process_run_id": str(successor["process_run_id"])}
        assert _control(container, stop)["status"] in {"DRAINING", "STOPPED"}
        _wait_for(lambda: _control(container, stop)["status"] == "STOPPED", timeout=90)
        assert json.loads((runtime / "supervisor" / "state.json").read_text())["operations"][restart["operation_id"]]["request"] == restart
    finally:
        logs = _docker("logs", container, timeout=10, check=False)
        if logs.stdout or logs.stderr:
            print(logs.stdout + logs.stderr)
        _docker("rm", "-f", container, check=False)
