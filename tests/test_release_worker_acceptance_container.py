"""Installed release acceptance through original authenticated producers.

Git objects, local beta acceptance, version installation, journal, signed bridge,
Supervisor and worker evidence are real. Only the external GitHub Actions and
Release HTTP service is controlled; this is not a live GitHub publication test.
Docker belongs to the external fixture, never either exercising worker.
"""

from __future__ import annotations

import base64
import json
import os
import select
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Iterator

import httpx
import pytest
from starlette.testclient import TestClient

from auto_coder.management.app import create_management_app
from auto_coder.management.beta_runtime_smoke import SmokeProfile
from auto_coder.management.operations import EffectState, OperationPhase
from auto_coder.management.source_acceptance import SourceAcceptancePublisher
from auto_coder.management.worker_network_client import WorkerEndpointConfig, WorkerNetworkClient, WorkerNetworkResult
from auto_coder.source_release_catalog import canonical, digest
from auto_coder.tested_source_refs import TestedSourcePublisher as SourceProofPublisher
from auto_coder.util.gh_cache import GitDataResponse
from tests.fixtures.ci_timing import docker_timing_arguments
from tests.test_beta_runtime_smoke_container import _owned_staging, _smoke_system, _SmokeSystem, _source_http, _SourceService
from tests.test_management_mcp import real_boundary  # noqa: F401
from tests.test_management_runtime_mcp import _call
from tests.test_management_source_release_dispatch import ACTION as PROMOTE_ACTION
from tests.test_management_source_release_dispatch import WORKFLOW_ID, WorkflowBoundary
from tests.test_source_release_promotion import policy
from tests.test_supervisor_container_integration import _container_facts, _control, _docker, _image, _preparation_cache_arguments, _wait_for

pytestmark = pytest.mark.supervisor_integration


class _ReleaseSourceService(WorkflowBoundary, _SourceService):
    """Actual bare Git and production promotion with controlled Actions/Release."""

    def __init__(self, source: Path, root: Path) -> None:
        super().__init__(source, root)
        self.releases: dict[str, dict[str, object]] = {}
        self.configured = policy(SmokeProfile(total_seconds=180, interval_seconds=2).content_revision)
        self.clock = time.time

    def request(self, method: str, path: str, payload: dict[str, object] | None = None) -> GitDataResponse:
        if method == "GET" and path.startswith("releases/tags/"):
            found = self.releases.get(path.removeprefix("releases/tags/"))
            return GitDataResponse(200, found) if found else GitDataResponse(404)
        if method == "POST" and path == "releases" and payload:
            name = str(payload["tag_name"])
            assert self._git("cat-file", "-t", self._git("rev-parse", f"refs/tags/{name}")) == "tag"
            if name in self.releases:
                return GitDataResponse(422)
            self.releases[name] = {"id": len(self.releases) + 1, "tag_name": name, "draft": False, "prerelease": False, "html_url": f"https://github.com/owner/repo/releases/tag/{name}"}
            return GitDataResponse(201, self.releases[name])
        return super().request(method, path, payload)


class _SourceHTTPTransport(httpx.BaseTransport):
    """Route canonical provider HTTP to the disposable real HTTP/Git server."""

    def __init__(self, endpoint: str) -> None:
        self.endpoint = httpx.URL(endpoint)
        self.transport = httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.github.com"
        assert request.url.path.startswith("/repos/owner/repo/")
        local = request.url.copy_with(scheme=self.endpoint.scheme, host=self.endpoint.host, port=self.endpoint.port)
        return self.transport.handle_request(httpx.Request(request.method, local, headers=request.headers, content=request.content, extensions=request.extensions))

    def close(self) -> None:
        self.transport.close()


class _ManagerProcess:
    """Authenticated ASGI requests in a distinct, disposable manager OS process."""

    def __init__(self, configuration: Path, log: Path) -> None:
        self.log = log
        self.stream = log.open("w", encoding="utf-8")
        repository = Path(__file__).resolve().parents[1]
        environment = {**os.environ, "PYTHONPATH": str(repository / "src") + os.pathsep + str(repository)}
        self.process = subprocess.Popen([sys.executable, "-B", str(repository / "tests/fixtures/release_manager_process.py"), str(configuration)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stream, text=True, env=environment, cwd=repository)
        try:
            ready = self._read()
            assert ready == {"pid": self.process.pid, "fresh_worker_client": True}, ready
            self.start_token = Path(f"/proc/{self.process.pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]
        except BaseException:
            self.close()
            raise

    def _read(self) -> dict[str, object]:
        assert self.process.stdout is not None
        available, _, _ = select.select([self.process.stdout], [], [], 60)
        assert available, self.log.read_text()[-12000:]
        line = self.process.stdout.readline()
        assert line, self.log.read_text()[-12000:]
        return json.loads(line)

    def post(self, path: str, *, headers: dict[str, str], json: dict[str, object]) -> httpx.Response:
        assert self.process.stdin is not None and self.process.poll() is None
        # Match TestClient's HTTP interface without bypassing MCP authentication.
        from json import dumps

        self.process.stdin.write(dumps({"path": path, "headers": headers, "json": json}) + "\n")
        self.process.stdin.flush()
        response = self._read()
        return httpx.Response(response["status_code"], json=response["body"])

    def crash(self) -> None:
        assert self.process.poll() is None
        self.process.kill()
        assert self.process.wait(timeout=20) == -9
        assert not Path(f"/proc/{self.process.pid}").exists()

    def close(self) -> None:
        if self.process.poll() is None:
            if self.process.stdin is not None:
                self.process.stdin.close()
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=20)
        self.stream.close()


@dataclass
class _InstalledRelease:
    client: WorkerNetworkClient
    runtime: Path
    container: str
    candidate: dict[str, object]
    previous: dict[str, object]


def _ready(client: WorkerNetworkClient, source_sha: str) -> dict[str, object] | None:
    client.inspect_supervisor()
    result = client.observe_worker()
    if result.result_code != "OK" or not isinstance(result.data, dict):
        return None
    observed = result.data.get("observation", {})
    lifecycle = observed.get("lifecycle", {})
    binding = result.data.get("runtime_binding", {})
    return result.data if result.data.get("binding_status") == "VERIFIED" and binding.get("source_sha") == source_sha and lifecycle.get("startup_reconciled") is True and lifecycle.get("daemon_serving") is True else None


@contextmanager
def _installed_release(system: _SmokeSystem, root: Path, scenario: str, *, fail_candidate: bool = False, reconstruct_supervisor: bool = False, hold_terminal_exit: bool = False, hold_terminal_after_recovery: bool = False, stop_prior_before_deployment: bool = False) -> Iterator[_InstalledRelease]:
    """Independently install both release versions, then start the prior one."""
    image = _image()
    source = Path(os.environ.get("GITHUB_WORKSPACE", str(Path(__file__).parents[1]))).resolve()
    fixtures = Path(__file__).parent / "fixtures"
    runtime, workspace, routing = (root / name for name in ("runtime", "workspace", "routing"))
    for folder in (runtime, workspace, routing, runtime / "home", runtime / "logs"):
        folder.mkdir(parents=True)
    (routing / "ownership.json").write_text(json.dumps({"owner/repo": "release"}), encoding="utf-8")
    (runtime / "llm_config.toml").write_text((system.runtime / "llm_config.toml").read_text(), encoding="utf-8")
    secret = os.urandom(48)
    (runtime / "bridge-secret").write_bytes(base64.b64encode(secret) + b"\n")
    host_user = f"{os.getuid()}:{os.getgid()}"
    stage_name = "release-stage-" + uuid.uuid4().hex[:12]
    with _owned_staging(stage_name, scenario, root):
        staged = _docker(
            "run",
            "--rm",
            "--name",
            stage_name,
            "--network",
            "bridge",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--user",
            host_user,
            "-v",
            f"{source}:/source:ro",
            "-v",
            f"{runtime}:/runtime",
            "-v",
            f"{fixtures}:/fixtures:ro",
            *_preparation_cache_arguments(),
            *docker_timing_arguments(),
            "-e",
            "HOME=/runtime/home",
            "--entrypoint",
            "python",
            image,
            "/fixtures/beta_smoke_stage.py",
            *system.revisions,
            timeout=1200,
        )
        installed = json.loads(staged.stdout.strip().splitlines()[-1])
        assert all(item["installed"]["editable"] is False for item in installed)
        candidate, previous = [item["receipt"] for item in installed[:2]]
        assert candidate["source_sha"] == system.revisions[0] != previous["source_sha"]
        assert previous["installed_runtime_id"] != candidate["installed_runtime_id"]
        assert candidate["content_identity"] != system.installations[0]["receipt"]["content_identity"]
        address = system.client.config.address.rsplit(".", 1)[0] + ".12"
        name = "release-worker-" + uuid.uuid4().hex[:12]
        trust = system.runtime.parent / "ci-trust"
        environment = {
            "AUTO_CODER_SUPERVISOR_INSTANCE_ID": "release-worker",
            "AUTO_CODER_CHANNEL": "release",
            "AUTO_CODER_REPOSITORY": "owner/repo",
            "AUTO_CODER_RUNTIME_ROOT": "/runtime",
            "AUTO_CODER_WORKSPACE_ROOT": "/workspace",
            "AUTO_CODER_OWNERSHIP_FILE": "/routing/ownership.json",
            "AUTO_CODER_ARTIFACT_DIGEST": "ci-local-release",
            "AUTO_CODER_SUPERVISOR_AUTO_START": "0",
            "AUTO_CODER_SOURCE_REPOSITORY_ROOT": "/source",
            "AUTO_CODER_BRIDGE_BIND_ADDRESS": address,
            "AUTO_CODER_BRIDGE_PORT": "8843",
            "AUTO_CODER_BRIDGE_SECRET_FILE": "/runtime/bridge-secret",
            "AUTO_CODER_CONFIG_PATH": "/runtime/llm_config.toml",
            "AUTO_CODER_HEALTH_LOG_ENABLED": "0",
            "AUTO_CODER_DISABLE_AUTO_UPDATE": "1",
            "AUTO_CODER_TEST_LAUNCH_AUDIT": "1",
            "AUTO_CODER_TEST_REQUEST_AUDIT": "1",
            "GITHUB_TOKEN": "nonfunctional-ci-placeholder",
            "SSL_CERT_FILE": "/ci-trust/github.crt",
            "REQUESTS_CA_BUNDLE": "/ci-trust/github.crt",
            "NO_PROXY": "api.github.com,localhost,127.0.0.1",
            "no_proxy": "api.github.com,localhost,127.0.0.1",
            "HOME": "/runtime/home",
        }
        if fail_candidate:
            environment["AUTO_CODER_FIXTURE_FAIL_STAGED_ONCE"] = "1"
        if reconstruct_supervisor:
            environment["AUTO_CODER_TEST_RECONSTRUCT_SUPERVISOR"] = "1"
        if hold_terminal_exit or hold_terminal_after_recovery:
            environment["AUTO_CODER_TEST_HOLD_INSTALLED_TERMINAL"] = "1"
            (runtime / "fixture-hold-selected-runtime").write_text(str(previous["environment_path"]), encoding="utf-8")
        if hold_terminal_exit:
            (runtime / "fixture-hold-terminal-exit").write_text("hold after actual terminal receipt", encoding="utf-8")
        launch = ["run", "-d", "--rm", "--name", name, "--network", system.network, "--ip", address, "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--user", host_user, "--entrypoint", "python"]
        for host, guest, mode in ((runtime, "/runtime", ""), (workspace, "/workspace", ""), (routing, "/routing", ""), (fixtures, "/fixtures", "ro"), (trust, "/ci-trust", "ro"), (source, "/source", "ro")):
            launch.extend(["-v", f"{host}:{guest}" + (f":{mode}" if mode else "")])
        for key, value in environment.items():
            launch.extend(["-e", f"{key}={value}"])
        try:
            _docker(*launch, image, "/fixtures/supervisor_offline_pid1.py", timeout=60)
            facts = _wait_for(lambda: facts if (facts := _container_facts(name))["socket"] else None, timeout=30)
            assert facts["docker_socket"] is False and facts["docker_binary"] is None
            client = WorkerNetworkClient(WorkerEndpointConfig("release-worker", "release", "owner/repo", address, 8843, secret))
            stopped = client.inspect_supervisor()
            assert stopped.result_code == "OK" and stopped.data["alive"] is False
            # External bootstrap selects a real prior installed runtime through
            # the production Supervisor UDS and observes its readiness.
            started = _control(name, {"operation_id": "fixture-prior-runtime", "operation": "START", "runtime_id": str(previous["installed_runtime_id"])})
            assert started["status"] == "RUNNING", started
            _wait_for(lambda: _ready(client, str(previous["source_sha"])), timeout=90)
            if stop_prior_before_deployment:
                # A never-started Supervisor is UNKNOWN, not proven STOPPED.
                # Establish genuine terminal evidence and OS exit before admission.
                prior = client.inspect_supervisor().data["worker"]
                prior_os = json.loads((runtime / "supervisor" / "state.json").read_text())["worker"]
                stopped_operation = "fixture-stopped-prior-runtime"
                request = client.dispatch_worker_operation(
                    stopped_operation,
                    "REQUEST_GRACEFUL_STOP",
                    process_run_id=prior["process_run_id"],
                    deployment_generation=prior["deployment_generation"],
                    selected_runtime_id=prior["runtime_id"],
                )
                assert request.result_code in {"DRAINING", "STOPPED"}, request
                _wait_for(lambda: state if (state := client.inspect_supervisor()).result_code == "OK" and state.data["status"] == "STOPPED" and state.data["alive"] is False and client.read_worker_operation(stopped_operation).result_code == "STOPPED" else None, timeout=90)
                terminal = json.loads((runtime / "last-terminal.json").read_text())
                assert all(terminal[key] == value for key, value in {"process_run_id": prior["process_run_id"], "deployment_generation": prior["deployment_generation"], "lifecycle_state": "stopped", "is_graceful_ready": True, "unsettled_count": 0}.items())
                assert client.inspect_supervisor().data["worker"] is None
                assert client.inspect_supervisor().data["selected_runtime_id"] == prior["runtime_id"]
                _assert_lifetime_gone(name, prior_os)
            yield _InstalledRelease(client, runtime, name, candidate, previous)
        finally:
            logs = _docker("logs", name, check=False)
            destination = Path(os.environ.get("GITHUB_WORKSPACE", str(Path(__file__).parents[1]))) / "supervisor-integration-logs" / scenario
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "release-worker.log").write_text(logs.stdout + logs.stderr, encoding="utf-8")
            _docker("rm", "-f", name, check=False)


def _published_acceptance(system: _SmokeSystem):
    inspected = system.client.inspect_supervisor()
    worker = inspected.data["worker"]
    accepted = _call(
        system.http,
        system.bearer,
        "auto_coder_run_beta_acceptance",
        {
            "target_id": "beta-worker",
            "selected_git_sha": system.revisions[0],
            "installed_runtime_id": worker["runtime_id"],
            "process_run_id": worker["process_run_id"],
            "deployment_generation": worker["deployment_generation"],
            "supervisor_generation": inspected.supervisor_generation,
            "deployment_operation_id": system.deployed_id,
            "idempotency_key": "release-original-beta-acceptance",
        },
    )
    operation_id = str(accepted["operation_id"])
    _wait_for(lambda: result if (result := _call(system.http, system.bearer, "auto_coder_beta_acceptance_get", {"target_id": "beta-worker", "operation_id": operation_id}))["status"] == "PASS_PUBLISHED" else None, timeout=200)
    published = SourceAcceptancePublisher(system.remote, "owner/repo").read(operation_id)
    assert published is not None
    evidence = json.loads(published.annotation)["local_evidence"]
    samples = [sample for sample in evidence["samples"] if sample["qualifying"]]
    assert len(samples) >= 2
    assert all(left["heartbeat"]["sequence"] < right["heartbeat"]["sequence"] for left, right in zip(samples, samples[1:]))
    assert all(sample["artifact"]["source_revision"]["origin"] == "build_embedded" for sample in samples)
    assert all(sample["runtime_binding"]["content_identity"] == system.installations[0]["receipt"]["content_identity"] for sample in samples)
    return published


def _container_identity(name: str) -> dict[str, object]:
    facts = json.loads(_docker("inspect", name).stdout)[0]
    # Docker does not promise a stable enumeration order for bind mounts.
    # Compare every mount attribute while ignoring order-only differences.
    mounts = sorted(facts["Mounts"], key=lambda mount: json.dumps(mount, sort_keys=True))
    return {"id": facts["Id"], "pid": facts["State"]["Pid"], "started": facts["State"]["StartedAt"], "mounts": mounts}


def test_container_identity_ignores_mount_order_but_detects_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    mounts = [{"Destination": "/workspace", "RW": True}, {"Destination": "/fixtures", "RW": False}]
    baseline = {"Id": "fixed", "State": {"Pid": 123, "StartedAt": "fixed-time"}, "Mounts": mounts}
    reordered = {**baseline, "Mounts": list(reversed(mounts))}
    altered = {**baseline, "Mounts": [{**mounts[0], "RW": False}, mounts[1]]}
    inspections = iter((baseline, reordered, altered))

    def inspect_fixture(*args: str) -> subprocess.CompletedProcess[str]:
        assert args == ("inspect", "fixture")
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps([next(inspections)]), stderr="")

    monkeypatch.setattr(sys.modules[__name__], "_docker", inspect_fixture)
    identity = _container_identity("fixture")
    assert _container_identity("fixture") == identity
    assert _container_identity("fixture") != identity


def _assert_lifetime_gone(container: str, lifetime: dict[str, object]) -> None:
    observed = _docker("exec", container, "python", "-c", "from pathlib import Path; import sys; p=Path('/proc/'+sys.argv[1]+'/stat'); print(p.read_text() if p.exists() else 'ABSENT')", str(lifetime["pid"])).stdout.strip()
    assert observed == "ABSENT" or observed.rsplit(") ", 1)[1].split()[19] != lifetime["start_token"]


@pytest.mark.timeout(2400)
def test_authenticated_release_joins_published_acceptance_and_independent_installed_workers(real_boundary, tmp_path: Path, monkeypatch, _use_real_commands: None, _use_real_sleep: None) -> None:
    _exercise_installed_release_join(real_boundary, tmp_path, monkeypatch, fail_candidate=False)


@pytest.mark.timeout(2400)
def test_failed_installed_release_recovers_original_prior_runtime_once(real_boundary, tmp_path: Path, monkeypatch, _use_real_commands: None, _use_real_sleep: None) -> None:
    _exercise_installed_release_join(real_boundary, tmp_path, monkeypatch, fail_candidate=True)


@pytest.mark.timeout(2400)
def test_lost_installed_start_response_survives_manager_process_reconstruction(real_boundary, tmp_path: Path, monkeypatch, _use_real_commands: None, _use_real_sleep: None) -> None:
    _exercise_installed_release_join(real_boundary, tmp_path, monkeypatch, fail_candidate=False, reconstruct_manager=True)


@pytest.mark.timeout(2400)
def test_installed_release_lost_start_reply_recovers_after_manager_and_supervisor_reconstruction(real_boundary, tmp_path: Path, monkeypatch, _use_real_commands: None, _use_real_sleep: None) -> None:
    """Rebuild app/Supervisor objects and listeners, not their OS processes."""
    _exercise_installed_release_join(real_boundary, tmp_path, monkeypatch, fail_candidate=False, lose_start_reconstruct=True)


@pytest.mark.timeout(2400)
def test_installed_release_rejects_later_same_sha_selection_after_staging_before_drain(real_boundary, tmp_path: Path, monkeypatch, _use_real_commands: None, _use_real_sleep: None) -> None:
    _exercise_installed_release_join(real_boundary, tmp_path, monkeypatch, fail_candidate=False, supersede_after_stage=True)


@pytest.mark.timeout(2400)
def test_installed_release_genuine_terminal_receipt_does_not_bypass_old_os_lifetime(real_boundary, tmp_path: Path, monkeypatch, _use_real_commands: None, _use_real_sleep: None) -> None:
    _exercise_installed_release_join(real_boundary, tmp_path, monkeypatch, fail_candidate=False, hold_terminal_exit=True)


@pytest.mark.timeout(2400)
def test_failed_installed_release_retains_uncertain_recovery_across_manager_reconstruction(real_boundary, tmp_path: Path, monkeypatch, _use_real_commands: None, _use_real_sleep: None) -> None:
    """Rebuild manager app/client objects, not their OS process."""
    _exercise_installed_release_join(real_boundary, tmp_path, monkeypatch, fail_candidate=True, lose_recovery_reconstruct_manager=True)


@pytest.mark.timeout(2400)
def test_lost_installed_recovery_survives_manager_process_and_supervisor_reconstruction(real_boundary, tmp_path: Path, monkeypatch, _use_real_commands: None, _use_real_sleep: None) -> None:
    """Restart manager OS process and Supervisor object/listener, retaining child."""
    _exercise_installed_release_join(real_boundary, tmp_path, monkeypatch, fail_candidate=True, lose_recovery_reconstruct_manager=True, recovery_process=True)


@pytest.mark.timeout(2400)
def test_expired_installed_recovery_settles_original_evidence_after_reconstruction(real_boundary, tmp_path: Path, monkeypatch, _use_real_commands: None, _use_real_sleep: None) -> None:
    """Let the actual recovery deadline expire, then release original readback."""
    _exercise_installed_release_join(real_boundary, tmp_path, monkeypatch, fail_candidate=True, lose_recovery_reconstruct_manager=True, recovery_process=True, expire_recovery=True)


@pytest.mark.timeout(2400)
def test_failed_installed_release_rebinds_original_recovery_after_supervisor_reconstruction(real_boundary, tmp_path: Path, monkeypatch, _use_real_commands: None, _use_real_sleep: None) -> None:
    _exercise_installed_release_join(real_boundary, tmp_path, monkeypatch, fail_candidate=True, lose_recovery_reconstruct_manager=True, reconstruct_recovery_supervisor=True)


@pytest.mark.timeout(2400)
def test_stopped_installed_release_rejects_later_same_sha_selection_before_start(real_boundary, tmp_path: Path, monkeypatch, _use_real_commands: None, _use_real_sleep: None) -> None:
    _exercise_installed_release_join(real_boundary, tmp_path, monkeypatch, fail_candidate=False, supersede_stopped_after_stage=True)


@pytest.mark.timeout(2400)
def test_restored_running_prior_holds_genuine_terminal_exit_across_reconstruction(real_boundary, tmp_path: Path, monkeypatch, _use_real_commands: None, _use_real_sleep: None) -> None:
    _exercise_installed_release_join(real_boundary, tmp_path, monkeypatch, fail_candidate=True, hold_terminal_after_recovery=True)


@pytest.mark.timeout(2400)
def test_installed_release_held_terminal_exit_survives_manager_and_supervisor_reconstruction(real_boundary, tmp_path: Path, monkeypatch, _use_real_commands: None, _use_real_sleep: None) -> None:
    """Keep the real old lifetime across manager OS and Supervisor object restart."""
    _exercise_installed_release_join(real_boundary, tmp_path, monkeypatch, fail_candidate=False, hold_terminal_exit=True, reconstruct_held_exit=True)


def _exercise_installed_release_join(
    real_boundary,
    tmp_path: Path,
    monkeypatch,
    *,
    fail_candidate: bool,
    reconstruct_manager: bool = False,
    lose_start_reconstruct: bool = False,
    supersede_after_stage: bool = False,
    hold_terminal_exit: bool = False,
    lose_recovery_reconstruct_manager: bool = False,
    recovery_process: bool = False,
    expire_recovery: bool = False,
    supersede_stopped_after_stage: bool = False,
    hold_terminal_after_recovery: bool = False,
    reconstruct_recovery_supervisor: bool = False,
    reconstruct_held_exit: bool = False,
) -> None:
    assert sum((reconstruct_manager, lose_start_reconstruct, supersede_after_stage, supersede_stopped_after_stage, hold_terminal_exit, hold_terminal_after_recovery, lose_recovery_reconstruct_manager)) <= 1
    assert not lose_recovery_reconstruct_manager or fail_candidate
    assert not reconstruct_recovery_supervisor or (lose_recovery_reconstruct_manager and not recovery_process)
    assert not hold_terminal_after_recovery or fail_candidate
    assert not recovery_process or lose_recovery_reconstruct_manager
    assert not expire_recovery or recovery_process
    assert not reconstruct_held_exit or hold_terminal_exit
    assert not fail_candidate or not any((reconstruct_manager, lose_start_reconstruct, supersede_after_stage, hold_terminal_exit))
    prefix = (
        "release-expired-recovery-"
        if expire_recovery
        else (
            "release-recovery-reconstruction-"
            if recovery_process
            else (
                "release-manager-reconstruction-"
                if reconstruct_manager
                else (
                    "release-uncertain-recovery-"
                    if lose_recovery_reconstruct_manager
                    else "release-held-terminal-" if hold_terminal_exit else "release-superseded-" if supersede_after_stage else "release-lost-start-" if lose_start_reconstruct else "release-failed-candidate-" if fail_candidate else "release-installed-join-"
                )
            )
        )
    )
    if hold_terminal_after_recovery:
        prefix = "release-recovered-held-exit-"
    if supersede_stopped_after_stage:
        prefix = "release-stopped-supersession-"
    if reconstruct_recovery_supervisor:
        prefix = "release-rebound-recovery-"
    if reconstruct_held_exit:
        prefix = "release-held-reconstruction-"
    scenario = prefix + uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix="release-join-", dir=tmp_path) as directory:
        root = Path(directory)
        beta_root = root / "beta"
        beta_root.mkdir()
        stage = "release-beta-stage-" + uuid.uuid4().hex[:12]
        with _owned_staging(stage, scenario, beta_root), _smoke_system(real_boundary, beta_root, scenario, stage, source_service_type=_ReleaseSourceService) as system:
            acceptance = _published_acceptance(system)
            service = system.app.state.management_runtime
            system.http.portal.call(service.stop_monitor)
            system.http.portal.call(service.dispatcher.stop)
            with (
                _installed_release(
                    system,
                    root / "release",
                    scenario,
                    fail_candidate=fail_candidate,
                    reconstruct_supervisor=lose_start_reconstruct or reconstruct_recovery_supervisor or hold_terminal_after_recovery or recovery_process or reconstruct_held_exit,
                    hold_terminal_exit=hold_terminal_exit,
                    hold_terminal_after_recovery=hold_terminal_after_recovery,
                    stop_prior_before_deployment=supersede_stopped_after_stage,
                ) as release,
                _source_http(system.remote) as endpoint,
                ExitStack() as processes,
            ):

                @contextmanager
                def source_http_client(**_kwargs):
                    with httpx.Client(transport=_SourceHTTPTransport(endpoint)) as client:
                        yield client

                monkeypatch.setattr("auto_coder.util.gh_cache.github_http_client", source_http_client)
                policy_path = root / "release-policy.json"
                policy_path.write_text(canonical(asdict(system.remote.configured)), encoding="utf-8")
                token_path = root / "source-token"
                token_path.write_text("controlled-external-source-credential", encoding="utf-8")
                operating = root / "full-policy.yaml"
                operating.write_text("profile: full\ntargets:\n  beta-worker:\n    channel: beta\n    repositories: [fixture/beta]\n  release-worker:\n    channel: release\n    repositories: [owner/repo]\n  release-source:\n    channel: release\n    repositories: [owner/repo]\n", encoding="utf-8")
                settings = replace(service.settings, operating_policy_path=operating, source_release_policy_path=policy_path, source_release_token_file=token_path, source_release_target_id="release-source", source_release_workflow_id=WORKFLOW_ID)
                app = create_management_app(settings, service.grants, worker_clients={"release": release.client})
                original_lifecycle_save = app.state.management_runtime.journal.lifecycle_save
                hold_drain_save = {"value": lose_start_reconstruct}
                denied_drain_saves: list[str] = []

                def guarded_lifecycle_save(operation_id: str, revision: int, snapshot: dict[str, object]) -> bool:
                    steps = snapshot.get("steps")
                    if hold_drain_save["value"] and isinstance(steps, dict) and "drain" in steps:
                        denied_drain_saves.append(operation_id)
                        return False
                    return original_lifecycle_save(operation_id, revision, snapshot)

                if lose_start_reconstruct:
                    monkeypatch.setattr(app.state.management_runtime.journal, "lifecycle_save", guarded_lifecycle_save)
                postpone_drain = {"value": supersede_after_stage}
                postpone_start = {"value": supersede_stopped_after_stage}
                original_step = app.state.management_runtime.lifecycle._step

                def held_drain_boundary(record, revision, snapshot, name, client, operation, runtime_id):
                    if (name == "drain" and postpone_drain["value"]) or (name == "start" and postpone_start["value"]):
                        return record
                    return original_step(record, revision, snapshot, name, client, operation, runtime_id)

                if supersede_after_stage or supersede_stopped_after_stage:
                    monkeypatch.setattr(app.state.management_runtime.lifecycle, "_step", held_drain_boundary)
                start_dispatches: list[str] = []
                if lose_start_reconstruct:
                    original_dispatch = release.client.dispatch_worker_operation
                    original_read = release.client.read_worker_operation

                    def lose_real_start_reply(operation_id: str, operation: str, **kwargs):
                        response = original_dispatch(operation_id, operation, **kwargs)
                        if operation == "START":
                            start_dispatches.append(operation_id)
                            return WorkerNetworkResult(0, "UNKNOWN", None, response.supervisor_generation, response.request_id)
                        return response

                    def withhold_original_start_receipt(operation_id: str):
                        if operation_id in start_dispatches:
                            return WorkerNetworkResult(0, "UNAVAILABLE", None, release.client.supervisor_generation or "", uuid.uuid4().hex)
                        return original_read(operation_id)

                    monkeypatch.setattr(release.client, "dispatch_worker_operation", lose_real_start_reply)
                    monkeypatch.setattr(release.client, "read_worker_operation", withhold_original_start_receipt)
                recovery_dispatches: list[str] = []
                failed_observations = []
                allow_recovery_readback = {"value": False}
                if lose_recovery_reconstruct_manager and not recovery_process:
                    original_dispatch = release.client.dispatch_worker_operation
                    original_read = release.client.read_worker_operation

                    def lose_real_recovery_reply(operation_id: str, operation: str, **kwargs):
                        if operation == "RECOVER_PREVIOUS":
                            failed = WorkerNetworkClient(release.client.config).inspect_supervisor()
                            assert failed.result_code == "OK" and failed.data["alive"] is False
                            assert failed.data["worker"]["runtime_id"] == release.candidate["installed_runtime_id"]
                            failed_observations.append(failed)
                        response = original_dispatch(operation_id, operation, **kwargs)
                        if operation == "RECOVER_PREVIOUS":
                            recovery_dispatches.append(operation_id)
                            return WorkerNetworkResult(0, "UNKNOWN", None, response.supervisor_generation, response.request_id)
                        return response

                    def withhold_original_recovery_receipt(operation_id: str):
                        if operation_id in recovery_dispatches and not allow_recovery_readback["value"]:
                            return WorkerNetworkResult(0, "UNAVAILABLE", None, release.client.supervisor_generation or "", uuid.uuid4().hex)
                        return original_read(operation_id)

                    monkeypatch.setattr(release.client, "dispatch_worker_operation", lose_real_recovery_reply)
                    monkeypatch.setattr(release.client, "read_worker_operation", withhold_original_recovery_receipt)

                with ExitStack() as clients:
                    http = clients.enter_context(TestClient(app, base_url=settings.oauth.canonical_issuer))
                    tested = SourceProofPublisher(system.remote, "owner/repo").read(system.revisions[0])
                    assert tested is not None
                    promotion_arguments = {
                        "target_id": "release-source",
                        "source_sha": system.revisions[0],
                        "ci_run_id": tested.record.run_id,
                        "ci_run_attempt": tested.record.run_attempt,
                        "tested_source_ref": tested.tag_ref,
                        "tested_source_oid": tested.tag_object_oid,
                        "tested_source_record_sha256": digest(asdict(tested.record)),
                        "acceptance_ref": acceptance.tag_ref,
                        "acceptance_oid": acceptance.tag_object_oid,
                        "idempotency_key": "release-promotion",
                    }
                    promoted = _call(
                        http,
                        system.bearer,
                        PROMOTE_ACTION,
                        promotion_arguments,
                    )
                    assert promoted["status"] == "SUCCEEDED", promoted
                    assert promoted["worker_deployed"] is False
                    promotion_id = str(promoted["operation_id"])
                    catalog_ref = f"refs/tags/auto-coder-source-release-{promotion_id}"
                    catalog_oid = system.remote._git("rev-parse", catalog_ref)
                    assert system.remote._git("cat-file", "-t", catalog_oid) == "tag"
                    previous = release.client.inspect_supervisor()
                    old = previous.data["worker"]
                    if supersede_stopped_after_stage:
                        assert old is None and previous.data["alive"] is False and previous.data["status"] == "STOPPED"
                        old_os = None
                        stopped_launches = (release.runtime / "fixture-launches.jsonl").read_bytes()
                        stopped_posts = (release.runtime / "fixture-posts").read_bytes()
                        assert len(stopped_launches.splitlines()) == 1
                    else:
                        assert isinstance(old, dict)
                        old_os = json.loads((release.runtime / "supervisor" / "state.json").read_text())["worker"]
                        assert _ready(release.client, system.revisions[1]) is not None
                        assert old["runtime_id"] == release.previous["installed_runtime_id"]
                    beta_before = system.client.inspect_supervisor()
                    beta_identity = _container_identity(system.worker_name)
                    release_identity = _container_identity(release.container)
                    ownership = [(path, path.read_bytes()) for path in (system.runtime.parent / "routing" / "ownership.json", release.runtime.parent / "routing" / "ownership.json")]
                    refs_before = system.remote._git("for-each-ref", "--format=%(refname) %(objectname)", "refs/tags")
                    arguments = {
                        "target_id": "release-worker",
                        "selected_git_sha": system.revisions[0],
                        "promotion_operation_id": promotion_id,
                        "catalog_ref": catalog_ref,
                        "catalog_oid": catalog_oid,
                        "acceptance_ref": acceptance.tag_ref,
                        "acceptance_oid": acceptance.tag_object_oid,
                        "idempotency_key": "release-deploy",
                        "expected_supervisor_generation": previous.supervisor_generation,
                        "expected_runtime_id": old["runtime_id"] if isinstance(old, dict) else previous.data["selected_runtime_id"],
                        "expected_process_run_id": old["process_run_id"] if isinstance(old, dict) else None,
                        "expected_deployment_generation": old["deployment_generation"] if isinstance(old, dict) else None,
                    }
                    if reconstruct_manager or recovery_process or reconstruct_held_exit:
                        # Only the child managers own the release lifecycle. The
                        # promotion app is stopped before the first admission.
                        http.portal.call(app.state.management_runtime.stop_monitor)
                        http.portal.call(app.state.management_runtime.dispatcher.stop)
                        fault_root = root / "manager-fault"
                        fault_root.mkdir()
                        lost_operation = "RECOVER_PREVIOUS" if recovery_process else "START"
                        hold = fault_root / ("hold-recovery-response" if recovery_process else "hold-start-response")
                        if not reconstruct_held_exit:
                            hold.write_text(f"discard real {lost_operation} transport responses", encoding="utf-8")
                        configuration = fault_root / "configuration.json"
                        worker_config = asdict(release.client.config)
                        worker_config["secret"] = base64.b64encode(worker_config["secret"]).decode()
                        configuration.write_text(
                            json.dumps({"settings": asdict(settings), "worker": worker_config, "uid_policy": str(real_boundary[4]), "oauth_now": real_boundary[5]["now"], "source_endpoint": endpoint, "fault_root": str(fault_root), "lost_operation": lost_operation}, default=str), encoding="utf-8"
                        )
                        first_manager = _ManagerProcess(configuration, fault_root / "manager-first.stderr")
                        processes.callback(first_manager.close)
                        http = first_manager
                    crash_marker = release.runtime / "fixture-crash-next-staged"
                    if fail_candidate:
                        # The original prior process is already serving. The next
                        # actual selected interpreter consumes this external fault
                        # and exits before it can run the production daemon.
                        crash_marker.write_text("crash-once", encoding="utf-8")
                    deployed = _call(http, system.bearer, "auto_coder_deploy_release_source", arguments)
                    assert deployed["status"] in {"ACCEPTED", "PREPARING"}, deployed
                    operation_id = str(deployed["operation_id"])
                    journal = app.state.management_runtime.journal
                    original = journal.get(operation_id).record
                    assert original is not None
                    assert _call(http, system.bearer, "auto_coder_deploy_release_source", arguments)["operation_id"] == operation_id
                    assert _call(http, system.bearer, "auto_coder_deploy_release_source", {**arguments, "selected_git_sha": system.revisions[1]})["status"] == "CONFLICT"
                    assert _call(http, system.bearer, "auto_coder_deploy_release_source", {**arguments, "catalog_oid": system.revisions[1]})["status"] == "CONFLICT"
                    competing = _call(http, system.bearer, "auto_coder_deploy_release_source", {**arguments, "idempotency_key": "competing"})
                    assert competing["status"] == "BUSY" and competing["operation_id"] == operation_id

                    if supersede_stopped_after_stage:

                        def staged_before_stopped_start():
                            pending = journal.lifecycle_snapshot(operation_id)
                            if pending is None:
                                return None
                            state = pending[1]
                            return state if state["stage"] == "STARTING" and isinstance(state.get("staged"), dict) else None

                        stopped_boundary = _wait_for(staged_before_stopped_start, timeout=150)
                        assert stopped_boundary["staged"]["source_sha"] == system.revisions[0]
                        assert "start" not in stopped_boundary["steps"] and "drain" not in stopped_boundary["steps"]
                        preparer_posts = (release.runtime / "fixture-posts").read_bytes()
                        assert preparer_posts == stopped_posts + b"post\n", "only signed PREPARE may follow the verified initial STOP"
                        before_start = release.client.inspect_supervisor()
                        assert before_start.data["alive"] is False and before_start.data["status"] == "STOPPED"
                        assert before_start.data["selected_runtime_id"] == previous.data["selected_runtime_id"]
                        assert (release.runtime / "fixture-launches.jsonl").read_bytes() == stopped_launches
                        later = _call(http, system.bearer, PROMOTE_ACTION, {**promotion_arguments, "idempotency_key": "later-stopped-same-sha-selection"})
                        assert later["status"] == "SUCCEEDED" and later["operation_id"] != promotion_id
                        assert system.remote._git("rev-parse", "refs/tags/auto-coder-release-source-selection-current") != catalog_oid
                        promoted_refs = system.remote._git("for-each-ref", "--format=%(refname) %(objectname)", "refs/tags")
                        postpone_start["value"] = False
                        superseded = _wait_for(lambda: status if (status := _call(http, system.bearer, "auto_coder_operation_get", {"target_id": "release-worker", "operation_id": operation_id}))["status"] == "SUPERSEDED" else None, timeout=50)
                        assert superseded["reason"] == "original_release_operation_superseded"
                        assert (release.runtime / "fixture-posts").read_bytes() == preparer_posts
                        assert (release.runtime / "fixture-launches.jsonl").read_bytes() == stopped_launches
                        after_start = release.client.inspect_supervisor()
                        assert after_start.data["alive"] is False and after_start.data["status"] == "STOPPED"
                        assert after_start.data["selected_runtime_id"] == before_start.data["selected_runtime_id"]
                        assert _call(http, system.bearer, "auto_coder_deploy_release_source", arguments)["operation_id"] == operation_id
                        assert _call(http, system.bearer, "auto_coder_deploy_release_source", {**arguments, "selected_git_sha": system.revisions[1]})["status"] == "CONFLICT"
                        assert _call(http, system.bearer, "auto_coder_deploy_release_source", {**arguments, "catalog_oid": "f" * 40})["status"] == "CONFLICT"
                        assert _container_identity(system.worker_name) == beta_identity
                        assert _container_identity(release.container) == release_identity
                        assert all(path.read_bytes() == original for path, original in ownership)
                        assert [dispatch["inputs"]["operation_id"] for dispatch in system.remote.dispatches] == [promotion_id, later["operation_id"]]
                        beta_after = system.client.inspect_supervisor()
                        assert beta_after.supervisor_generation == beta_before.supervisor_generation
                        assert all(beta_after.data["worker"][key] == beta_before.data["worker"][key] for key in ("process_run_id", "deployment_generation", "runtime_id", "binding_status", "runtime_binding"))
                        assert system.remote._git("for-each-ref", "--format=%(refname) %(objectname)", "refs/tags") == promoted_refs
                        assert system.remote._git("rev-parse", catalog_ref) == catalog_oid
                        assert system.remote._git("rev-parse", acceptance.tag_ref) == acceptance.tag_object_oid
                        return

                    if reconstruct_manager:

                        def lost_start():
                            persisted = journal.lifecycle_snapshot(operation_id)
                            step = persisted[1].get("steps", {}).get("start") if persisted else None
                            return persisted[1] if (fault_root / "lost-start.json").exists() and step and step["state"] == "SENT_UNKNOWN" else None

                        held = _wait_for(lost_start, timeout=150)
                        lost = json.loads((fault_root / "lost-start.json").read_text())
                        assert lost["operation_id"] == held["steps"]["start"]["operation_id"]
                        assert lost["supervisor_generation"] == previous.supervisor_generation
                        independent = WorkerNetworkClient(release.client.config)
                        survivor = _wait_for(lambda: _ready(independent, str(release.candidate["source_sha"])), timeout=90)
                        receipt = independent.read_worker_operation(lost["operation_id"])
                        assert receipt.result_code == "RUNNING" and receipt.data["intent_digest"] == held["steps"]["start"]["expected_digest"]
                        retained_os = json.loads((release.runtime / "supervisor" / "state.json").read_text())["worker"]
                        posts_before_restart = (release.runtime / "fixture-posts").read_bytes()
                        launches_before_restart = (release.runtime / "fixture-launches.jsonl").read_bytes()
                        first_manager.crash()
                        second_manager = _ManagerProcess(configuration, fault_root / "manager-second.stderr")
                        processes.callback(second_manager.close)
                        assert second_manager.process.pid != first_manager.process.pid
                        assert (second_manager.process.pid, second_manager.start_token) != (first_manager.process.pid, first_manager.start_token)
                        http = second_manager
                        for _ in range(3):
                            replay = _call(http, system.bearer, "auto_coder_deploy_release_source", arguments)
                            assert replay["operation_id"] == operation_id and replay["status"] == "STARTING", replay
                            assert _call(http, system.bearer, "auto_coder_deploy_release_source", {**arguments, "selected_git_sha": system.revisions[1]})["status"] == "CONFLICT"
                            assert _call(http, system.bearer, "auto_coder_deploy_release_source", {**arguments, "catalog_oid": "f" * 40})["status"] == "CONFLICT"
                            competing = _call(http, system.bearer, "auto_coder_deploy_release_source", {**arguments, "idempotency_key": "competing-after-restart"})
                            assert competing["status"] == "BUSY" and competing["operation_id"] == operation_id
                            time.sleep(2.1)
                        rebuilt = journal.lifecycle_snapshot(operation_id)[1]
                        for field in ("steps", "original_deadline", "deadline", "accepted_evidence", "release_identity", "incumbent", "staged"):
                            assert rebuilt[field] == held[field], field
                        rebuilt_record = journal.get(operation_id).record
                        assert rebuilt_record.intent == original.intent and rebuilt_record.authority == original.authority
                        assert (release.runtime / "fixture-posts").read_bytes() == posts_before_restart
                        assert (release.runtime / "fixture-launches.jsonl").read_bytes() == launches_before_restart
                        still_live = _ready(independent, str(release.candidate["source_sha"]))
                        assert still_live is not None
                        for field in ("process_run_id", "deployment_generation"):
                            assert still_live["observation"][field] == survivor["observation"][field]
                        live_os = json.loads((release.runtime / "supervisor" / "state.json").read_text())["worker"]
                        assert all(live_os[field] == retained_os[field] for field in ("pid", "start_token", "deployment_generation", "runtime_id"))
                        stat = _docker("exec", release.container, "cat", f"/proc/{retained_os['pid']}/stat").stdout
                        assert stat.rsplit(") ", 1)[1].split()[19] == retained_os["start_token"]
                        audit = [json.loads(line) for line in (fault_root / "lost-response-audit.jsonl").read_text().splitlines()]
                        assert len([entry for entry in audit if entry["event"] == "discarded_start_response"]) == 1
                        assert any(entry["pid"] == second_manager.process.pid and entry["event"] == "discarded_start_readback" and entry["result_code"] == "RUNNING" for entry in audit)
                        # Release transport loss only. The next genuine signed
                        # read must settle the same effect, not dispatch again.
                        hold.unlink()

                    if hold_terminal_exit:
                        hold_path = release.runtime / "fixture-hold-terminal-exit"
                        _wait_for(lambda: (release.runtime / "fixture-terminal-exit-held").exists(), timeout=180)
                        terminal_while_alive = json.loads((release.runtime / "last-terminal.json").read_text())
                        assert all(terminal_while_alive[key] == value for key, value in {"process_run_id": old["process_run_id"], "deployment_generation": old["deployment_generation"], "lifecycle_state": "stopped", "is_graceful_ready": True, "unsettled_count": 0}.items())
                        held = release.client.inspect_supervisor()
                        assert held.data["alive"] is True and held.data["worker"]["process_run_id"] == old["process_run_id"]
                        assert len((release.runtime / "fixture-launches.jsonl").read_text().splitlines()) == 1
                        held_posts = (release.runtime / "fixture-posts").read_bytes()
                        time.sleep(5)
                        assert (release.runtime / "fixture-posts").read_bytes() == held_posts
                        assert len((release.runtime / "fixture-launches.jsonl").read_text().splitlines()) == 1
                        assert _call(http, system.bearer, "auto_coder_deploy_release_source", arguments)["operation_id"] == operation_id
                        assert _call(http, system.bearer, "auto_coder_deploy_release_source", {**arguments, "selected_git_sha": system.revisions[1]})["status"] == "CONFLICT"
                        assert _call(http, system.bearer, "auto_coder_deploy_release_source", {**arguments, "idempotency_key": "competing-held-terminal"})["status"] == "BUSY"
                        if reconstruct_held_exit:
                            first_manager.crash()
                            held_exit_snapshot = journal.lifecycle_snapshot(operation_id)[1]
                            assert held_exit_snapshot["stage"] == "DRAINING"
                            assert set(held_exit_snapshot["steps"]) == {"prepare", "drain"}
                            drain_step = held_exit_snapshot["steps"]["drain"]
                            assert drain_step["operation_id"] == "d-" + operation_id
                            _docker("kill", "--signal=SIGUSR1", release.container)
                            independent = WorkerNetworkClient(release.client.config)
                            changed = _wait_for(lambda: result if (result := independent.inspect_supervisor()).result_code == "OK" and result.supervisor_generation != previous.supervisor_generation else None, timeout=30)
                            assert (release.runtime / "fixture-supervisor-reconstructions").read_text() == "1"
                            second_manager = _ManagerProcess(configuration, fault_root / "manager-second.stderr")
                            processes.callback(second_manager.close)
                            assert second_manager.process.pid != first_manager.process.pid
                            assert (second_manager.process.pid, second_manager.start_token) != (first_manager.process.pid, first_manager.start_token)
                            http = second_manager
                            for _ in range(3):
                                replay = _call(http, system.bearer, "auto_coder_deploy_release_source", arguments)
                                assert replay["status"] == "DRAINING" and replay["operation_id"] == operation_id
                                assert _call(http, system.bearer, "auto_coder_deploy_release_source", {**arguments, "selected_git_sha": system.revisions[1]})["status"] == "CONFLICT"
                                assert _call(http, system.bearer, "auto_coder_deploy_release_source", {**arguments, "catalog_oid": "f" * 40})["status"] == "CONFLICT"
                                competing = _call(http, system.bearer, "auto_coder_deploy_release_source", {**arguments, "idempotency_key": "competing-held-reconstructed"})
                                assert competing["status"] == "BUSY" and competing["operation_id"] == operation_id
                                rebuilt = journal.lifecycle_snapshot(operation_id)[1]
                                for field in ("steps", "original_deadline", "deadline", "accepted_evidence", "release_identity", "incumbent", "staged"):
                                    assert rebuilt[field] == held_exit_snapshot[field], field
                                retained_record = journal.get(operation_id).record
                                assert retained_record.intent == original.intent and retained_record.authority == original.authority
                                assert retained_record.effect.state is EffectState.SENT_UNKNOWN and retained_record.effect.receipt is None
                                actual = independent.inspect_supervisor()
                                assert actual.supervisor_generation == changed.supervisor_generation
                                assert actual.data["alive"] is True and actual.data["selected_runtime_id"] == old["runtime_id"]
                                assert all(actual.data["worker"][field] == old[field] for field in ("process_run_id", "deployment_generation", "runtime_id"))
                                receipt = independent.read_worker_operation(drain_step["operation_id"])
                                assert receipt.result_code == "DRAINING" and receipt.data["intent_digest"] == drain_step["expected_digest"]
                                assert json.loads((release.runtime / "last-terminal.json").read_text()) == terminal_while_alive
                                stat = _docker("exec", release.container, "cat", f"/proc/{old_os['pid']}/stat").stdout
                                assert stat.rsplit(") ", 1)[1].split()[19] == old_os["start_token"]
                                assert (release.runtime / "fixture-posts").read_bytes() == held_posts
                                assert len((release.runtime / "fixture-launches.jsonl").read_text().splitlines()) == 1
                                assert _container_identity(release.container) == release_identity
                                time.sleep(2.1)
                        hold_path.unlink()

                    if supersede_after_stage:

                        def staged_before_replacement():
                            pending = journal.lifecycle_snapshot(operation_id)
                            if pending is None:
                                return None
                            state = pending[1]
                            return state if state["stage"] == "DRAINING" and isinstance(state.get("staged"), dict) else None

                        stopped_at_boundary = _wait_for(staged_before_replacement, timeout=150)
                        assert stopped_at_boundary["staged"]["source_sha"] == system.revisions[0]
                        assert "drain" not in stopped_at_boundary["steps"] and "start" not in stopped_at_boundary["steps"]
                        preparer_posts = (release.runtime / "fixture-posts").read_bytes()
                        assert preparer_posts == b"post\n", "only signed PREPARE may have been dispatched"
                        assert _ready(release.client, system.revisions[1]) is not None
                        next_promotion = _call(
                            http,
                            system.bearer,
                            PROMOTE_ACTION,
                            {**promotion_arguments, "idempotency_key": "later-same-sha-selection"},
                        )
                        assert next_promotion["status"] == "SUCCEEDED", next_promotion
                        assert next_promotion["operation_id"] != promotion_id
                        assert system.remote._git("rev-parse", "refs/tags/auto-coder-release-source-selection-current") != catalog_oid
                        promoted_refs = system.remote._git("for-each-ref", "--format=%(refname) %(objectname)", "refs/tags")
                        postpone_drain["value"] = False
                        superseded = _wait_for(lambda: result if (result := _call(http, system.bearer, "auto_coder_operation_get", {"target_id": "release-worker", "operation_id": operation_id}))["status"] == "SUPERSEDED" else None, timeout=50)
                        assert superseded["reason"] == "original_release_operation_superseded"
                        assert (release.runtime / "fixture-posts").read_bytes() == preparer_posts, "superseded selection must not send a replacement effect"
                        assert len((release.runtime / "fixture-launches.jsonl").read_text().splitlines()) == 1
                        assert _ready(release.client, system.revisions[1]) is not None
                        assert _call(http, system.bearer, "auto_coder_deploy_release_source", arguments)["operation_id"] == operation_id
                        assert _call(http, system.bearer, "auto_coder_deploy_release_source", {**arguments, "selected_git_sha": system.revisions[1]})["status"] == "CONFLICT"
                        assert _container_identity(system.worker_name) == beta_identity
                        assert _container_identity(release.container) == release_identity
                        assert all(path.read_bytes() == original for path, original in ownership)
                        beta_after = system.client.inspect_supervisor()
                        assert beta_after.supervisor_generation == beta_before.supervisor_generation
                        assert all(beta_after.data["worker"][key] == beta_before.data["worker"][key] for key in ("process_run_id", "deployment_generation", "runtime_id", "binding_status", "runtime_binding"))
                        assert system.remote._git("for-each-ref", "--format=%(refname) %(objectname)", "refs/tags") == promoted_refs
                        assert system.remote._git("rev-parse", catalog_ref) == catalog_oid
                        assert system.remote._git("rev-parse", acceptance.tag_ref) == acceptance.tag_object_oid
                        assert len(system.remote.dispatches) == 2
                        assert [dispatch["inputs"]["operation_id"] for dispatch in system.remote.dispatches] == [promotion_id, next_promotion["operation_id"]]
                        assert all(dispatch["inputs"]["source_sha"] == system.revisions[0] for dispatch in system.remote.dispatches)
                        return

                    if lose_start_reconstruct:
                        _wait_for(lambda: denied_drain_saves[0] if denied_drain_saves else None, timeout=150)
                        assert all(item == operation_id for item in denied_drain_saves)
                        not_durable = journal.lifecycle_snapshot(operation_id)
                        assert not_durable is not None and "drain" not in not_durable[1]["steps"]
                        assert (release.runtime / "fixture-posts").read_bytes() == b"post\n", "no signed replacement effect before confirmed durable boundary"
                        assert len((release.runtime / "fixture-launches.jsonl").read_text().splitlines()) == 1
                        assert _ready(release.client, system.revisions[1]) is not None
                        assert _call(http, system.bearer, "auto_coder_deploy_release_source", {**arguments, "idempotency_key": "competing-undurable"})["status"] == "BUSY"
                        hold_drain_save["value"] = False
                        assert not fail_candidate
                        _wait_for(lambda: start_dispatches[0] if start_dispatches else None, timeout=180)
                        assert start_dispatches == ["s-" + operation_id]
                        independent = WorkerNetworkClient(release.client.config)
                        _wait_for(lambda: _ready(independent, str(release.candidate["source_sha"])), timeout=90)
                        unsettled = journal.lifecycle_snapshot(operation_id)
                        assert unsettled is not None
                        assert unsettled[1]["steps"]["start"]["state"] == "SENT_UNKNOWN"
                        assert "recovery" not in unsettled[1]["steps"]
                        assert len((release.runtime / "fixture-launches.jsonl").read_text().splitlines()) == 2
                        assert _call(http, system.bearer, "auto_coder_deploy_release_source", arguments)["operation_id"] == operation_id
                        assert _call(http, system.bearer, "auto_coder_deploy_release_source", {**arguments, "selected_git_sha": system.revisions[1]})["status"] == "CONFLICT"
                        assert _call(http, system.bearer, "auto_coder_deploy_release_source", {**arguments, "idempotency_key": "competing-reconstruct"})["status"] == "BUSY"
                        actual_before = independent.inspect_supervisor()
                        old_dispatch_posts = (release.runtime / "fixture-posts").read_bytes()
                        clients.close()
                        _docker("kill", "--signal=SIGUSR1", release.container)
                        fresh = WorkerNetworkClient(release.client.config)
                        _wait_for(lambda: observed if (observed := fresh.inspect_supervisor()).result_code == "OK" and observed.supervisor_generation != actual_before.supervisor_generation else None, timeout=30)
                        assert (release.runtime / "fixture-supervisor-reconstructions").read_text() == "1"
                        assert (release.runtime / "fixture-posts").read_bytes() == old_dispatch_posts
                        reconstructed = create_management_app(settings, service.grants, worker_clients={"release": fresh})
                        http = clients.enter_context(TestClient(reconstructed, base_url=settings.oauth.canonical_issuer))
                        journal = reconstructed.state.management_runtime.journal
                        recovered = journal.get(operation_id).record
                        assert recovered is not None and recovered.intent == original.intent and recovered.authority == original.authority
                        assert _call(http, system.bearer, "auto_coder_deploy_release_source", arguments)["operation_id"] == operation_id

                    if lose_recovery_reconstruct_manager:
                        failure_observer = WorkerNetworkClient(release.client.config)

                        def recovery_dispatched():
                            inspected = failure_observer.inspect_supervisor()
                            if inspected.result_code == "OK" and isinstance(inspected.data, dict):
                                worker = inspected.data.get("worker")
                                if inspected.data.get("alive") is False and isinstance(worker, dict) and worker.get("runtime_id") == release.candidate["installed_runtime_id"]:
                                    failed_observations.append(inspected)
                            if recovery_process:
                                lost_file = fault_root / "lost-recovery.json"
                                persisted = journal.lifecycle_snapshot(operation_id)
                                step = persisted[1].get("steps", {}).get("recovery") if persisted else None
                                if lost_file.exists() and step and step["state"] == "SENT_UNKNOWN":
                                    lost = json.loads(lost_file.read_text())
                                    assert lost["supervisor_generation"] == previous.supervisor_generation
                                    recovery_dispatches.append(lost["operation_id"])
                            return recovery_dispatches[0] if recovery_dispatches else None

                        _wait_for(recovery_dispatched, timeout=180)
                        assert recovery_dispatches == ["r-" + operation_id]
                        independent = WorkerNetworkClient(release.client.config)
                        survivor = _wait_for(lambda: _ready(independent, str(release.previous["source_sha"])), timeout=90)
                        unsettled = journal.lifecycle_snapshot(operation_id)
                        assert unsettled is not None
                        assert unsettled[1]["stage"] == "RESTORING"
                        assert unsettled[1]["steps"]["recovery"]["state"] == "SENT_UNKNOWN"
                        assert unsettled[1]["recovery_attempts"] == 1
                        assert len((release.runtime / "fixture-launches.jsonl").read_text().splitlines()) == 3
                        preserved_posts = (release.runtime / "fixture-posts").read_bytes()
                        assert _call(http, system.bearer, "auto_coder_deploy_release_source", arguments)["operation_id"] == operation_id
                        assert _call(http, system.bearer, "auto_coder_deploy_release_source", {**arguments, "selected_git_sha": system.revisions[1]})["status"] == "CONFLICT"
                        assert _call(http, system.bearer, "auto_coder_deploy_release_source", {**arguments, "idempotency_key": "competing-recovery"})["status"] == "BUSY"

                        held_recovery = unsettled[1]
                        recovery_step = held_recovery["steps"]["recovery"]
                        receipt_before = independent.read_worker_operation(recovery_dispatches[0])
                        assert receipt_before.result_code == "RUNNING" and receipt_before.data["intent_digest"] == recovery_step["expected_digest"]
                        retained_os = json.loads((release.runtime / "supervisor" / "state.json").read_text())["worker"]
                        preserved_launches = (release.runtime / "fixture-launches.jsonl").read_bytes()
                        if recovery_process:
                            first_manager.crash()
                            # The Supervisor owner/listener is reconstructed in
                            # the same PID1, not an OS-process/container restart.
                            _docker("kill", "--signal=SIGUSR1", release.container)
                            reconstructed_supervisor = _wait_for(lambda: result if (result := independent.inspect_supervisor()).result_code == "OK" and result.supervisor_generation != previous.supervisor_generation else None, timeout=30)
                            assert (release.runtime / "fixture-supervisor-reconstructions").read_text() == "1"
                            assert _container_identity(release.container) == release_identity
                            second_manager = _ManagerProcess(configuration, fault_root / "manager-second.stderr")
                            processes.callback(second_manager.close)
                            assert second_manager.process.pid != first_manager.process.pid
                            assert (second_manager.process.pid, second_manager.start_token) != (first_manager.process.pid, first_manager.start_token)
                            http = second_manager
                        else:
                            clients.close()
                            fresh = WorkerNetworkClient(release.client.config)
                            fresh_read = fresh.read_worker_operation

                            def retain_uncertain_recovery(operation_id: str):
                                if operation_id in recovery_dispatches and not allow_recovery_readback["value"]:
                                    return WorkerNetworkResult(0, "UNAVAILABLE", None, fresh.supervisor_generation or "", uuid.uuid4().hex)
                                return fresh_read(operation_id)

                            monkeypatch.setattr(fresh, "read_worker_operation", retain_uncertain_recovery)
                            reconstructed = create_management_app(settings, service.grants, worker_clients={"release": fresh})
                            http = clients.enter_context(TestClient(reconstructed, base_url=settings.oauth.canonical_issuer))
                            journal = reconstructed.state.management_runtime.journal

                        def recovery_lane_held():
                            recovered = journal.get(operation_id).record
                            assert recovered is not None and recovered.intent == original.intent and recovered.authority == original.authority
                            assert recovered.effect.state is EffectState.SENT_UNKNOWN and recovered.effect.receipt is None
                            held = _call(http, system.bearer, "auto_coder_operation_get", {"target_id": "release-worker", "operation_id": operation_id})
                            expected_statuses = {"RESTORING", "NEEDS_ATTENTION"} if expire_recovery else {"RESTORING"}
                            assert held["status"] in expected_statuses and held["recovery_state"] == "UNKNOWN", held
                            assert _call(http, system.bearer, "auto_coder_deploy_release_source", arguments)["operation_id"] == operation_id
                            assert _call(http, system.bearer, "auto_coder_deploy_release_source", {**arguments, "selected_git_sha": system.revisions[1]})["status"] == "CONFLICT"
                            assert _call(http, system.bearer, "auto_coder_deploy_release_source", {**arguments, "catalog_oid": "f" * 40})["status"] == "CONFLICT"
                            competing = _call(http, system.bearer, "auto_coder_deploy_release_source", {**arguments, "idempotency_key": "competing-recovery-reconstructed"})
                            assert competing["status"] == "BUSY" and competing["operation_id"] == operation_id
                            rebuilt = journal.lifecycle_snapshot(operation_id)[1]
                            for field in ("steps", "original_deadline", "deadline", "accepted_evidence", "release_identity", "incumbent", "staged", "new_worker", "recovery_attempts"):
                                assert rebuilt[field] == held_recovery[field], field
                            assert (release.runtime / "fixture-posts").read_bytes() == preserved_posts
                            assert (release.runtime / "fixture-launches.jsonl").read_bytes() == preserved_launches
                            return held

                        for _ in range(3):
                            recovery_lane_held()
                            time.sleep(2.1)
                        if expire_recovery:
                            # No clock/deadline edits: wait for the original
                            # production 150-second recovery window to expire.
                            remaining = held_recovery["deadline"] - time.time()
                            assert 0 < remaining < 150
                            while time.time() <= held_recovery["deadline"]:
                                recovery_lane_held()
                                time.sleep(min(5, max(0.1, held_recovery["deadline"] - time.time())))
                            expired = _wait_for(lambda: result if (result := recovery_lane_held())["status"] == "NEEDS_ATTENTION" else None, timeout=20)
                            assert expired["reason"] == "original_supervisor_incarnation_changed"
                            assert journal.get(operation_id).record.phase is OperationPhase.NEEDS_ATTENTION
                            assert "recovery_read_only_heartbeats" not in journal.lifecycle_snapshot(operation_id)[1]
                        if reconstruct_recovery_supervisor:
                            previous_supervisor = fresh.inspect_supervisor()
                            assert previous_supervisor.result_code == "OK"
                            _docker("kill", "--signal=SIGUSR1", release.container)
                            reconstructed_supervisor = _wait_for(lambda: now if (now := fresh.inspect_supervisor()).result_code == "OK" and now.supervisor_generation != previous_supervisor.supervisor_generation else None, timeout=30)
                            assert (release.runtime / "fixture-supervisor-reconstructions").read_text() == "1"
                            recovery_lane_held()
                        still_live = _ready(independent, str(release.previous["source_sha"]))
                        assert still_live is not None
                        for field in ("process_run_id", "deployment_generation"):
                            assert still_live["observation"][field] == survivor["observation"][field]
                        live_os = json.loads((release.runtime / "supervisor" / "state.json").read_text())["worker"]
                        assert all(live_os[field] == retained_os[field] for field in ("pid", "start_token", "deployment_generation", "runtime_id"))
                        stat = _docker("exec", release.container, "cat", f"/proc/{retained_os['pid']}/stat").stdout
                        assert stat.rsplit(") ", 1)[1].split()[19] == retained_os["start_token"]
                        if recovery_process:
                            retained_receipt = independent.read_worker_operation(recovery_dispatches[0])
                            assert retained_receipt.supervisor_generation == reconstructed_supervisor.supervisor_generation
                            assert retained_receipt.data == receipt_before.data
                            audit = [json.loads(line) for line in (fault_root / "lost-response-audit.jsonl").read_text().splitlines()]
                            assert len([entry for entry in audit if entry["event"] == "discarded_recovery_response"]) == 1
                            assert any(entry["pid"] == second_manager.process.pid and entry["event"] == "discarded_recovery_readback" and entry["result_code"] == "RUNNING" for entry in audit)
                            hold.unlink()
                        else:
                            allow_recovery_readback["value"] = True

                    independent_samples = []
                    expected = release.previous if fail_candidate else release.candidate
                    expected_source = str(expected["source_sha"])
                    terminal_status = "FAILED" if fail_candidate else "SUCCEEDED"
                    terminal_phase = OperationPhase.FAILED if fail_candidate else OperationPhase.SUCCEEDED
                    observer = WorkerNetworkClient(release.client.config)

                    def completed():
                        inspected = observer.inspect_supervisor()
                        if fail_candidate and inspected.result_code == "OK" and isinstance(inspected.data, dict):
                            worker = inspected.data.get("worker")
                            if inspected.data.get("alive") is False and isinstance(worker, dict) and worker.get("runtime_id") == release.candidate["installed_runtime_id"]:
                                failed_observations.append(inspected)
                        live = _ready(observer, expected_source)
                        if live is not None and live["observation"]["process_run_id"] != old["process_run_id"]:
                            heartbeat = live["observation"]["heartbeat"]
                            if heartbeat["stage"].startswith("producer:") and (not independent_samples or heartbeat["sequence"] > independent_samples[-1]["observation"]["heartbeat"]["sequence"]):
                                independent_samples.append(live)
                        result = _call(http, system.bearer, "auto_coder_operation_get", {"target_id": "release-worker", "operation_id": operation_id})
                        forbidden = {"SUPERSEDED", "SUCCEEDED" if fail_candidate else "FAILED"}
                        if not expire_recovery:
                            forbidden.add("NEEDS_ATTENTION")
                        assert result["status"] not in forbidden, result
                        stored = journal.get(operation_id).record
                        return result if result["status"] == terminal_status and stored is not None and stored.phase is terminal_phase else None

                    done = _wait_for(completed, timeout=210)
                    observed = _ready(release.client, expected_source)
                    assert observed is not None
                    binding = observed["runtime_binding"]
                    for field in ("source_sha", "content_identity", "lock_sha256", "distribution_version"):
                        assert binding[field] == expected[field]
                    assert binding["runtime_id"] == expected["installed_runtime_id"]
                    assert done["release_identity"]["catalog_oid"] == catalog_oid
                    if lose_start_reconstruct:
                        assert done["steps"]["start"]["operation_id"] == start_dispatches[0]
                        assert done["steps"]["start"]["expected_digest"] == journal.lifecycle_snapshot(operation_id)[1]["steps"]["start"]["expected_digest"]
                        assert (release.runtime / "fixture-supervisor-reconstructions").read_text() == "1"
                    assert len(done["heartbeats"]) == 2
                    assert done["heartbeats"][0] < done["heartbeats"][1]
                    assert len(independent_samples) >= 2, "manager success needs independently observed producer progress"
                    assert done["heartbeats"][-1] in {sample["observation"]["heartbeat"]["sequence"] for sample in independent_samples}
                    assert len({(sample["observation"]["process_run_id"], sample["observation"]["deployment_generation"]) for sample in independent_samples}) == 1
                    assert all(sample["observation"]["artifact"]["source_revision"] == {"available": True, "value": expected_source, "origin": "build_embedded", "reason": None} for sample in independent_samples)
                    current = release.client.inspect_supervisor()
                    assert current.data["worker"]["process_run_id"] != old["process_run_id"]
                    assert current.data["worker"]["deployment_generation"] != old["deployment_generation"]
                    terminal = json.loads((release.runtime / "last-terminal.json").read_text())
                    assert all(terminal[key] == value for key, value in {"process_run_id": old["process_run_id"], "deployment_generation": old["deployment_generation"], "lifecycle_state": "stopped", "is_graceful_ready": True, "unsettled_count": 0}.items())
                    _assert_lifetime_gone(release.container, old_os)
                    launches = [json.loads(line) for line in (release.runtime / "fixture-launches.jsonl").read_text().splitlines()]
                    expected_environments = [release.previous["environment_path"], release.candidate["environment_path"]]
                    if fail_candidate:
                        expected_environments.append(release.previous["environment_path"])
                    assert len(launches) == len(expected_environments)
                    assert [item["environment"] for item in launches] == expected_environments
                    assert len({(item["pid"], item["start_token"]) for item in launches}) == len(expected_environments)
                    assert len({item["deployment_generation"] for item in launches}) == len(expected_environments)
                    assert launches[-1]["deployment_generation"] == current.data["worker"]["deployment_generation"]
                    if fail_candidate:
                        assert done["reason"] == "new_worker_failed_previous_runtime_restored"
                        assert done["recovery_attempts"] == 1 and done["recovery_state"] == "CONFIRMED"
                        assert done["steps"]["start"]["state"] == done["steps"]["recovery"]["state"] == "CONFIRMED"
                        assert done["source_sha"] == release.candidate["source_sha"] != binding["source_sha"]
                        assert done["restored_worker"]["deployment_generation"] == launches[2]["deployment_generation"]
                        assert done["new_worker"]["deployment_generation"] == launches[1]["deployment_generation"]
                        assert failed_observations, "recovery requires independent signed observation of the failed child"
                        assert all(item.supervisor_generation == previous.supervisor_generation and item.data["worker"]["deployment_generation"] == launches[1]["deployment_generation"] for item in failed_observations)
                        assert not crash_marker.exists()
                        state = json.loads((release.runtime / "supervisor" / "state.json").read_text())
                        failed = state["failed_worker"]
                        assert all(failed[key] == launches[1][key] for key in ("pid", "start_token", "deployment_generation"))
                        assert failed["runtime_id"] == release.candidate["installed_runtime_id"]
                        assert failed["ready"] == release.candidate
                        assert failed["process_run_id"] is None and failed.get("binding_status") != "VERIFIED"
                        _assert_lifetime_gone(release.container, failed)
                        assert state["prior_recovery_attempted"] is True
                        assert state["prior_recovery_generation"] == failed["deployment_generation"]
                        recovery_id = done["steps"]["recovery"]["operation_id"]
                        assert [key for key, value in state["operations"].items() if value["request"]["operation"] == "RECOVER_PREVIOUS"] == [recovery_id]
                        receipt = observer.read_worker_operation(recovery_id)
                        assert receipt.result_code == "RUNNING" and receipt.data["intent_digest"] == done["steps"]["recovery"]["expected_digest"]
                        assert receipt.data["worker"]["deployment_generation"] == launches[2]["deployment_generation"]
                    if lose_recovery_reconstruct_manager:
                        assert done["original_deadline"] == held_recovery["original_deadline"]
                        for field in ("operation_id", "sent_at", "owner_generation", "attempt_id", "correlation_identity", "expected_digest"):
                            assert done["steps"]["recovery"][field] == recovery_step[field]
                        assert done["restored_worker"]["deployment_generation"] == retained_os["deployment_generation"]
                        assert (release.runtime / "fixture-posts").read_bytes() == preserved_posts
                        assert (release.runtime / "fixture-launches.jsonl").read_bytes() == preserved_launches
                        if recovery_process or reconstruct_recovery_supervisor:
                            assert current.supervisor_generation == reconstructed_supervisor.supervisor_generation != previous.supervisor_generation
                        if expire_recovery:
                            assert done["deadline"] == held_recovery["deadline"] < time.time()
                            assert journal.lifecycle_snapshot(operation_id)[1]["recovery_read_only_heartbeats"] == done["heartbeats"]
                            settled_recovery = journal.get(operation_id).record
                            assert settled_recovery.effect.state is EffectState.CONFIRMED
                            assert settled_recovery.effect.receipt.correlation_identity == settled_recovery.effect.correlation_identity
                            assert settled_recovery.effect.receipt.observation_source == "supervisor-and-worker-lifecycle-journal"
                    if reconstruct_manager:
                        assert done["original_deadline"] == held["original_deadline"]
                        for field in ("operation_id", "sent_at", "owner_generation", "attempt_id", "correlation_identity", "expected_digest"):
                            assert done["steps"]["start"][field] == held["steps"]["start"][field]
                        assert done["steps"]["start"]["state"] == "CONFIRMED"
                        assert done["new_worker"]["deployment_generation"] == retained_os["deployment_generation"]
                        assert current.supervisor_generation == previous.supervisor_generation
                        assert (release.runtime / "fixture-posts").read_bytes() == posts_before_restart
                        assert (release.runtime / "fixture-launches.jsonl").read_bytes() == launches_before_restart
                    if reconstruct_held_exit:
                        assert done["original_deadline"] == held_exit_snapshot["original_deadline"]
                        for field in ("operation_id", "sent_at", "owner_generation", "attempt_id", "correlation_identity", "expected_digest"):
                            assert done["steps"]["drain"][field] == drain_step[field]
                        assert current.supervisor_generation == changed.supervisor_generation != previous.supervisor_generation
                        assert (release.runtime / "fixture-posts").read_bytes() == held_posts + b"post\n", "only the original candidate START follows genuine old exit"
                    terminal_posts = (release.runtime / "fixture-posts").read_bytes()
                    for _ in range(3):
                        replay = _call(http, system.bearer, "auto_coder_deploy_release_source", arguments)
                        assert replay["operation_id"] == operation_id and replay["status"] == terminal_status
                        time.sleep(2.1)
                    assert (release.runtime / "fixture-posts").read_bytes() == terminal_posts
                    settled = journal.get(operation_id).record
                    assert settled is not None and settled.phase is terminal_phase
                    assert settled.intent == original.intent and settled.authority == original.authority
                    assert len((release.runtime / "fixture-launches.jsonl").read_text().splitlines()) == len(expected_environments)
                    beta_after = system.client.inspect_supervisor()
                    assert beta_after.supervisor_generation == beta_before.supervisor_generation
                    assert all(beta_after.data["worker"][key] == beta_before.data["worker"][key] for key in ("process_run_id", "deployment_generation", "runtime_id", "binding_status"))
                    assert all(beta_after.data["worker"]["runtime_binding"][key] == beta_before.data["worker"]["runtime_binding"][key] for key in ("runtime_id", "content_identity", "source_sha", "start_token"))
                    assert _container_identity(system.worker_name) == beta_identity
                    assert _container_identity(release.container) == release_identity
                    assert all(path.read_bytes() == original for path, original in ownership)
                    assert system.remote._git("for-each-ref", "--format=%(refname) %(objectname)", "refs/tags") == refs_before
                    assert len(system.remote.dispatches) == 1
                    if hold_terminal_after_recovery:
                        # The independently installed prior is running again.
                        # Its genuine terminal receipt cannot replace OS-exit proof.
                        restored = release.client.inspect_supervisor()
                        assert restored.data["alive"] is True and restored.data["worker"]["runtime_id"] == release.previous["installed_runtime_id"]
                        restored_worker = restored.data["worker"]
                        restored_os = json.loads((release.runtime / "supervisor" / "state.json").read_text())["worker"]
                        hold_path = release.runtime / "fixture-hold-terminal-exit"
                        hold_path.write_text("hold recovered running prior after genuine terminal", encoding="utf-8")
                        next_args = {
                            **arguments,
                            "idempotency_key": "release-after-restored-prior",
                            "expected_supervisor_generation": restored.supervisor_generation,
                            "expected_runtime_id": restored_worker["runtime_id"],
                            "expected_process_run_id": restored_worker["process_run_id"],
                            "expected_deployment_generation": restored_worker["deployment_generation"],
                        }
                        second = _call(http, system.bearer, "auto_coder_deploy_release_source", next_args)
                        assert second["status"] in {"ACCEPTED", "PREPARING"}, second
                        second_id = str(second["operation_id"])
                        assert second_id != operation_id
                        _wait_for(lambda: (release.runtime / "fixture-terminal-exit-held").exists(), timeout=180)
                        real_terminal = json.loads((release.runtime / "last-terminal.json").read_text())
                        assert all(real_terminal[key] == value for key, value in {"process_run_id": restored_worker["process_run_id"], "deployment_generation": restored_worker["deployment_generation"], "lifecycle_state": "stopped", "is_graceful_ready": True, "unsettled_count": 0}.items())
                        still_alive = release.client.inspect_supervisor()
                        assert still_alive.data["alive"] is True and still_alive.data["worker"]["process_run_id"] == restored_worker["process_run_id"]
                        held_posts = (release.runtime / "fixture-posts").read_bytes()
                        assert len((release.runtime / "fixture-launches.jsonl").read_text().splitlines()) == 3
                        clients.close()
                        fresh = WorkerNetworkClient(release.client.config)
                        reconstructed = create_management_app(settings, service.grants, worker_clients={"release": fresh})
                        http = clients.enter_context(TestClient(reconstructed, base_url=settings.oauth.canonical_issuer))
                        journal = reconstructed.state.management_runtime.journal
                        old_supervisor = fresh.inspect_supervisor()
                        assert old_supervisor.result_code == "OK"
                        _docker("kill", "--signal=SIGUSR1", release.container)
                        _wait_for(lambda: now if (now := fresh.inspect_supervisor()).result_code == "OK" and now.supervisor_generation != old_supervisor.supervisor_generation else None, timeout=30)
                        assert (release.runtime / "fixture-supervisor-reconstructions").read_text() == "1"
                        assert _call(http, system.bearer, "auto_coder_deploy_release_source", next_args)["operation_id"] == second_id
                        assert _call(http, system.bearer, "auto_coder_deploy_release_source", {**next_args, "selected_git_sha": system.revisions[1]})["status"] == "CONFLICT"
                        assert _call(http, system.bearer, "auto_coder_deploy_release_source", {**next_args, "idempotency_key": "competing-while-recovered-held"})["status"] == "BUSY"
                        time.sleep(5)
                        assert (release.runtime / "fixture-posts").read_bytes() == held_posts
                        assert len((release.runtime / "fixture-launches.jsonl").read_text().splitlines()) == 3
                        retained = fresh.inspect_supervisor()
                        assert retained.data["alive"] is True
                        assert all(retained.data["worker"][field] == restored_worker[field] for field in ("process_run_id", "deployment_generation", "runtime_id"))
                        assert json.loads((release.runtime / "last-terminal.json").read_text()) == real_terminal
                        stat = _docker("exec", release.container, "cat", f"/proc/{restored_os['pid']}/stat").stdout
                        assert stat.rsplit(") ", 1)[1].split()[19] == restored_os["start_token"]
                        second_observer = WorkerNetworkClient(release.client.config)
                        second_samples = []

                        def second_completed():
                            live = _ready(second_observer, str(release.candidate["source_sha"]))
                            if live is not None:
                                heartbeat = live["observation"]["heartbeat"]
                                if heartbeat["stage"].startswith("producer:") and (not second_samples or heartbeat["sequence"] > second_samples[-1]["observation"]["heartbeat"]["sequence"]):
                                    second_samples.append(live)
                            result = _call(http, system.bearer, "auto_coder_operation_get", {"target_id": "release-worker", "operation_id": second_id})
                            assert result["status"] not in {"FAILED", "SUPERSEDED", "NEEDS_ATTENTION"}, result
                            return result if result["status"] == "SUCCEEDED" and journal.get(second_id).record.phase is OperationPhase.SUCCEEDED else None

                        hold_path.unlink()
                        second_result = _wait_for(second_completed, timeout=210)
                        assert second_result["new_worker"]["runtime_id"] == release.candidate["installed_runtime_id"]
                        assert len(second_result["heartbeats"]) == 2 and second_result["heartbeats"][0] < second_result["heartbeats"][1]
                        assert len(second_samples) >= 2
                        assert second_result["heartbeats"][-1] in {sample["observation"]["heartbeat"]["sequence"] for sample in second_samples}
                        assert len({(sample["observation"]["process_run_id"], sample["observation"]["deployment_generation"]) for sample in second_samples}) == 1
                        assert all(sample["observation"]["artifact"]["source_revision"] == {"available": True, "value": release.candidate["source_sha"], "origin": "build_embedded", "reason": None} for sample in second_samples)
                        assert all(all(sample["runtime_binding"][field] == release.candidate[field] for field in ("source_sha", "content_identity", "lock_sha256", "distribution_version")) for sample in second_samples)
                        assert second_result["steps"]["drain"]["operation_id"] == "d-" + second_id
                        assert second_result["steps"]["start"]["operation_id"] == "s-" + second_id
                        _assert_lifetime_gone(release.container, restored_os)
                        assert len((release.runtime / "fixture-launches.jsonl").read_text().splitlines()) == 4
                        assert _ready(fresh, str(release.candidate["source_sha"])) is not None
                        assert _call(http, system.bearer, "auto_coder_deploy_release_source", arguments)["status"] == "FAILED"
                        assert _call(http, system.bearer, "auto_coder_deploy_release_source", next_args)["status"] == "SUCCEEDED"
                        assert _container_identity(system.worker_name) == beta_identity
                        assert _container_identity(release.container) == release_identity
                        assert all(path.read_bytes() == original for path, original in ownership)
                        assert system.remote._git("for-each-ref", "--format=%(refname) %(objectname)", "refs/tags") == refs_before
