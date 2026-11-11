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
from auto_coder.management.operations import OperationPhase
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
def _installed_release(system: _SmokeSystem, root: Path, scenario: str, *, fail_candidate: bool = False, reconstruct_supervisor: bool = False, hold_terminal_exit: bool = False) -> Iterator[_InstalledRelease]:
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
        if hold_terminal_exit:
            environment["AUTO_CODER_TEST_HOLD_INSTALLED_TERMINAL"] = "1"
            (runtime / "fixture-hold-selected-runtime").write_text(str(previous["environment_path"]), encoding="utf-8")
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
            # External bootstrap selects the first real installed runtime through
            # the production Supervisor UDS. The authenticated deployment below
            # starts only after this independent incumbent is actually serving.
            started = _control(name, {"operation_id": "fixture-prior-runtime", "operation": "START", "runtime_id": str(previous["installed_runtime_id"])})
            assert started["status"] == "RUNNING", started
            _wait_for(lambda: _ready(client, str(previous["source_sha"])), timeout=90)
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
    return {"id": facts["Id"], "pid": facts["State"]["Pid"], "started": facts["State"]["StartedAt"], "mounts": facts["Mounts"]}


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
def test_installed_release_lost_start_reply_recovers_after_manager_and_supervisor_reconstruction(real_boundary, tmp_path: Path, monkeypatch, _use_real_commands: None, _use_real_sleep: None) -> None:
    _exercise_installed_release_join(real_boundary, tmp_path, monkeypatch, fail_candidate=False, lose_start_reconstruct=True)


@pytest.mark.timeout(2400)
def test_installed_release_rejects_later_same_sha_selection_after_staging_before_drain(real_boundary, tmp_path: Path, monkeypatch, _use_real_commands: None, _use_real_sleep: None) -> None:
    _exercise_installed_release_join(real_boundary, tmp_path, monkeypatch, fail_candidate=False, supersede_after_stage=True)


@pytest.mark.timeout(2400)
def test_installed_release_genuine_terminal_receipt_does_not_bypass_old_os_lifetime(real_boundary, tmp_path: Path, monkeypatch, _use_real_commands: None, _use_real_sleep: None) -> None:
    _exercise_installed_release_join(real_boundary, tmp_path, monkeypatch, fail_candidate=False, hold_terminal_exit=True)


def _exercise_installed_release_join(real_boundary, tmp_path: Path, monkeypatch, *, fail_candidate: bool, lose_start_reconstruct: bool = False, supersede_after_stage: bool = False, hold_terminal_exit: bool = False) -> None:
    assert sum((fail_candidate, lose_start_reconstruct, supersede_after_stage, hold_terminal_exit)) <= 1
    prefix = "release-held-terminal-" if hold_terminal_exit else "release-superseded-" if supersede_after_stage else "release-lost-start-" if lose_start_reconstruct else "release-failed-candidate-" if fail_candidate else "release-installed-join-"
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
            with _installed_release(system, root / "release", scenario, fail_candidate=fail_candidate, reconstruct_supervisor=lose_start_reconstruct, hold_terminal_exit=hold_terminal_exit) as release, _source_http(system.remote) as endpoint:

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
                original_step = app.state.management_runtime.lifecycle._step

                def held_drain_boundary(record, revision, snapshot, name, client, operation, runtime_id):
                    if name == "drain" and postpone_drain["value"]:
                        return record
                    return original_step(record, revision, snapshot, name, client, operation, runtime_id)

                if supersede_after_stage:
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
                        "expected_runtime_id": old["runtime_id"],
                        "expected_process_run_id": old["process_run_id"],
                        "expected_deployment_generation": old["deployment_generation"],
                    }
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
                    competing = _call(http, system.bearer, "auto_coder_deploy_release_source", {**arguments, "idempotency_key": "competing"})
                    assert competing["status"] == "BUSY" and competing["operation_id"] == operation_id

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
                        hold_path.unlink()

                    if supersede_after_stage:

                        def staged_before_drain():
                            pending = journal.lifecycle_snapshot(operation_id)
                            if pending is None:
                                return None
                            state = pending[1]
                            return state if state["stage"] == "DRAINING" and isinstance(state.get("staged"), dict) else None

                        stopped_at_boundary = _wait_for(staged_before_drain, timeout=150)
                        assert stopped_at_boundary["staged"]["source_sha"] == system.revisions[0]
                        assert "drain" not in stopped_at_boundary["steps"]
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

                    independent_samples = []
                    failed_observations = []
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
                        assert result["status"] not in {"SUPERSEDED", "NEEDS_ATTENTION", "SUCCEEDED" if fail_candidate else "FAILED"}, result
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
