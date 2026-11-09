"""Real installed beta workers produce durable local smoke evidence.

The external test orchestrator owns Docker and fault injection. The production
smoke producer has only the existing journal, a signed read client, and a
read-only Git/Actions transport. No observation or verdict is synthesized here.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from textwrap import dedent
from typing import Iterator

import pytest
from starlette.testclient import TestClient

from auto_coder.beta_source_selection import BetaSourceSelector, MutationCertainty
from auto_coder.management.action_scopes import DEPLOY_SCOPE, READ_SCOPE
from auto_coder.management.app import create_management_app
from auto_coder.management.beta_runtime_smoke import BetaRuntimeSmokeProducer, SmokeProfile
from auto_coder.management.mcp_runtime_tools import ManagementRuntime
from auto_coder.management.oauth_config import ManagementSettings, OAuthClient, OAuthSettings
from auto_coder.management.oauth_grants import OAuthGrantService
from auto_coder.management.operation_executor import ManagementOperationDispatcher, OperationHandler
from auto_coder.management.operation_store import ManagementOperationStore
from auto_coder.management.operations import ActionParameter, AdmissionStatus, EffectReceipt, ManagedTarget, MutationStatus, OperationIntent, OperationPhase
from auto_coder.management.source_acceptance import SourceAcceptancePublisher
from auto_coder.management.source_evidence import BetaEvidenceReader
from auto_coder.management.worker_lifecycle import LIFECYCLE_ACTIONS, WorkerLifecycle
from auto_coder.management.worker_network_client import WorkerEndpointConfig, WorkerNetworkClient
from auto_coder.tested_source_refs import serialize_record
from auto_coder.util.gh_cache import GitDataResponse, GitHubGitDataClient
from tests.fixtures.ci_timing import docker_timing_arguments, observe_nested_install_evidence, recorder
from tests.test_management_beta_deploy_container import _argument, _await_stage, _result
from tests.test_management_mcp import _issue, real_boundary
from tests.test_management_runtime_mcp import _call
from tests.test_management_worker_lifecycle import GitEvidenceBoundary
from tests.test_supervisor_container_integration import _container_facts, _docker, _image, _inspect_worker, _preparation_cache_arguments, _wait_for

pytestmark = pytest.mark.supervisor_integration


class _SourceService(GitEvidenceBoundary):
    """Disposable real Git objects with externally controlled Actions receipts."""

    def __init__(self, source: Path, root: Path) -> None:
        super().__init__()
        self.proofs.clear()
        self.tags.clear()
        self.refs.clear()
        self.executions.clear()
        self.repository = root
        # Import source commits, not the checkout's live selection/evidence tags.
        # The fixture publishes its own proof and selection refs below.
        subprocess.run(["git", "clone", "--bare", "--no-tags", str(source), str(root)], check=True, capture_output=True, text=True)
        # Keep defensive cleanup in case a future transport imports tags
        # despite the clone boundary.
        inherited = self._git("for-each-ref", "--format=%(refname)", "refs/tags/auto-coder-beta-*")
        for ref in inherited.splitlines():
            self._git("update-ref", "-d", ref)
        self.read_only_mutations: list[tuple[str, str]] = []
        self.fail_reads = False
        self.lose_acceptance_ref_response = False
        self.block_acceptance_readback = False
        self.acceptance_writes = 0

    def _git(self, *args: str, input: str | None = None) -> str:
        return subprocess.run(["git", f"--git-dir={self.repository}", *args], input=input, check=True, capture_output=True, text=True).stdout.strip()

    def publish_proof(self, ready: dict[str, object], run_id: int) -> None:
        sha = str(ready["source_sha"])
        super().add_proof(sha, "", run_id, 1)
        original = self.proofs[sha][1]
        record = replace(original, distribution_version=str(ready["distribution_version"]), lock_sha256=str(ready["lock_sha256"]))
        name = f"auto-coder-tested-{sha}"
        tag = f"object {sha}\ntype commit\ntag {name}\ntagger fixture <fixture@example.com> 0 +0000\n\n{serialize_record(record)}\n"
        oid = self._git("mktag", input=tag)
        self._git("update-ref", f"refs/tags/{name}", oid)
        self.proofs[sha] = oid, record

    def request(self, method: str, path: str, payload: dict[str, object] | None = None) -> GitDataResponse:
        if method == "POST" and payload and (str(payload.get("tag", "")).startswith("auto-coder-beta-acceptance-") or str(payload.get("ref", "")).startswith("refs/tags/auto-coder-beta-acceptance-")):
            self.acceptance_writes += 1
        if self.block_acceptance_readback and method == "GET" and path.startswith("git/ref/tags/auto-coder-beta-acceptance-"):
            return GitDataResponse(503)
        if self.fail_reads and method == "GET":
            return GitDataResponse(503, {"message": "controlled evidence outage"})
        if method == "GET" and path.startswith("git/ref/tags/"):
            ref = "refs/tags/" + path.removeprefix("git/ref/tags/")
            result = subprocess.run(["git", f"--git-dir={self.repository}", "rev-parse", "--verify", ref], capture_output=True, text=True)
            if result.returncode:
                return GitDataResponse(404)
            oid = result.stdout.strip()
            return GitDataResponse(200, {"ref": ref, "object": {"type": self._git("cat-file", "-t", oid), "sha": oid}})
        if method == "GET" and path.startswith("git/tags/"):
            raw = self._git("cat-file", "-p", path.removeprefix("git/tags/"))
            headers, message = raw.split("\n\n", 1)
            fields = dict(line.split(" ", 1) for line in headers.splitlines() if line.split(" ", 1)[0] in {"object", "type", "tag"})
            return GitDataResponse(200, {"tag": fields["tag"], "message": message, "object": {"type": fields["type"], "sha": fields["object"]}})
        if method == "POST" and path == "git/tags" and payload:
            tag = f"object {payload['object']}\ntype commit\ntag {payload['tag']}\ntagger fixture <fixture@example.com> 0 +0000\n\n{payload['message']}\n"
            return GitDataResponse(201, {"sha": self._git("mktag", input=tag)})
        if method == "POST" and path == "git/refs" and payload:
            ref = str(payload["ref"])
            if subprocess.run(["git", f"--git-dir={self.repository}", "show-ref", "--verify", "--quiet", ref]).returncode == 0:
                return GitDataResponse(422)
            self._git("update-ref", ref, str(payload["sha"]))
            if self.lose_acceptance_ref_response and ref.startswith("refs/tags/auto-coder-beta-acceptance-"):
                self.lose_acceptance_ref_response = False
                self.block_acceptance_readback = True
                raise RuntimeError("controlled lost acceptance ref response")
            return GitDataResponse(201)
        if method == "PATCH" and path.startswith("git/refs/tags/") and payload:
            ref = "refs/tags/" + path.removeprefix("git/refs/tags/")
            self._git("update-ref", ref, str(payload["sha"]))
            return GitDataResponse(200)
        return super().request(method, path, payload)


def test_source_service_does_not_inherit_checkout_beta_selection_refs(tmp_path: Path, _use_real_commands: None) -> None:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.name", "fixture"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.com"], cwd=source, check=True)
    (source / "tracked").write_text("source", encoding="utf-8")
    subprocess.run(["git", "add", "tracked"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", "source"], cwd=source, check=True)
    for name in ("auto-coder-beta-current", "auto-coder-beta-selection-r77"):
        subprocess.run(["git", "tag", name], cwd=source, check=True)

    service = _SourceService(source, tmp_path / "source.git")

    for name in ("auto-coder-beta-current", "auto-coder-beta-selection-r77"):
        assert (
            subprocess.run(
                ["git", f"--git-dir={service.repository}", "rev-parse", "--verify", f"refs/tags/{name}"],
                capture_output=True,
                text=True,
            ).returncode
            != 0
        )
    assert service._git("rev-parse", "HEAD") == subprocess.run(["git", "rev-parse", "HEAD"], cwd=source, check=True, capture_output=True, text=True).stdout.strip()


@contextmanager
def _source_http(service: _SourceService):
    class Handler(BaseHTTPRequestHandler):
        def dispatch(self) -> None:
            prefix = "/repos/owner/repo/"
            if not self.path.startswith(prefix):
                self.send_error(404)
                return
            if self.headers.get("Authorization") == "Bearer read-only-fixture" and self.command != "GET":
                service.read_only_mutations.append((self.command, self.path))
                self.send_error(403)
                return
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0")))) if self.headers.get("Content-Length") not in (None, "0") else None
            result = service.request(self.command, self.path.removeprefix(prefix), payload)
            encoded = json.dumps(result.data).encode()
            self.send_response(result.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        do_GET = dispatch
        do_POST = dispatch
        do_PATCH = dispatch

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


class _SourceConfiguredLifecycle(WorkerLifecycle):
    """Use a real REST source reader pointed at the disposable Git service."""

    def __init__(self, runtime, source: BetaEvidenceReader) -> None:
        super().__init__(runtime)
        self.reader = source

    def _source(self) -> BetaEvidenceReader:
        return self.reader


def _configure_lifecycle(app, reader: BetaEvidenceReader) -> None:
    runtime = app.state.management_runtime
    runtime.lifecycle = _SourceConfiguredLifecycle(runtime, reader)
    runtime.dispatcher = ManagementOperationDispatcher(runtime.journal)
    for action in sorted(LIFECYCLE_ACTIONS):
        runtime.dispatcher.register(action, OperationHandler(runtime.lifecycle.process, runtime.lifecycle.process))
    if runtime.beta_acceptance is not None:
        runtime.dispatcher.register("run_beta_acceptance", OperationHandler(runtime.beta_acceptance.forward, runtime.beta_acceptance.reconcile))


def _accepted(journal: ManagementOperationStore, client: WorkerNetworkClient, deployed: str, source: str, key: str):
    def settled_deployment():
        record = journal.get(deployed).record
        snapshot = journal.lifecycle_snapshot(deployed)
        assert record is not None and snapshot is not None
        return record if record.phase is OperationPhase.SUCCEEDED and snapshot[1].get("stage") == "SUCCEEDED" else None

    # Lifecycle completion and operation settlement use separate transactions.
    previous = _wait_for(settled_deployment, timeout=20)
    assert previous is not None and previous.phase is OperationPhase.SUCCEEDED
    observed = client.inspect_supervisor()
    assert observed.result_code == "OK" and observed.data is not None
    worker = observed.data["worker"]
    intent = OperationIntent(
        ManagedTarget(client.config.instance_id, client.config.channel, client.config.repository),
        "run_beta_acceptance",
        (
            ActionParameter("target_id", "beta-worker"),
            ActionParameter("source_sha", source),
            ActionParameter("deployment_operation_id", deployed),
            ActionParameter("supervisor_generation", observed.supervisor_generation),
            ActionParameter("deployment_generation", worker["deployment_generation"]),
        ),
        key,
        expected_incarnation=worker["process_run_id"],
        expected_artifact=worker["runtime_id"],
    )
    accepted = journal.admit(intent, previous.authority)
    assert accepted.status is AdmissionStatus.CREATED and accepted.record is not None
    return accepted.record


def _release_local_lane(journal: ManagementOperationStore, operation_id: str) -> None:
    """The external acceptance consumer owns journal settlement, never sampling."""
    for _ in range(3):
        record = journal.get(operation_id).record
        assert record is not None
        receipt = EffectReceipt(record.effect.correlation_identity or operation_id, "integration-acceptance-consumer", time.time(), "local-only-integration-finished")
        if record.phase is OperationPhase.NEEDS_ATTENTION:
            result = journal.resolve_needs_attention(operation_id, record.revision, OperationPhase.FAILED, evidence=receipt)
        else:
            result = journal.transition(operation_id, record.revision, OperationPhase.FAILED, evidence=receipt)
        if result.status is MutationStatus.UPDATED:
            return
        assert result.status is MutationStatus.STALE, result
    raise AssertionError("acceptance consumer could not settle the original lane")


def _finish(producer: BetaRuntimeSmokeProducer, operation_id: str, *, timeout: float = 190):
    def completed():
        observed = producer.advance(operation_id)
        assert observed.state.value not in {"CONFLICT", "UNAVAILABLE"}, observed
        return observed if observed.state.value == "COMPLETE" else None

    with recorder().span("beta_smoke.progress_observation", operation_id=operation_id, configured_timeout_seconds=timeout):
        return _wait_for(completed, timeout=timeout)


def _host_path(root: Path, path: str) -> Path:
    return root / Path(path).relative_to("/runtime")


def _fresh_process(configuration: Path, operation_id: str, mode: str = "advance") -> dict[str, object]:
    result = subprocess.run([sys.executable, str(Path(__file__).parent / "fixtures" / "beta_smoke_reconstruct.py"), str(configuration), operation_id, mode], check=True, capture_output=True, text=True, timeout=45)
    return json.loads(result.stdout.strip().splitlines()[-1])


@dataclass
class _SmokeSystem:
    """Case-owned real source, management journal, and installed worker."""

    journal: ManagementOperationStore
    client: WorkerNetworkClient
    reader: BetaEvidenceReader
    selecting: BetaSourceSelector
    remote: _SourceService
    app: object
    http: TestClient
    bearer: str
    runtime: Path
    installations: list[dict[str, object]]
    revisions: list[str]
    deployed_id: str
    reconstruction: Path


@contextmanager
def _smoke_system(real_boundary, tmp_path: Path, scenario_id: str, stage_name: str) -> Iterator[_SmokeSystem]:
    """Provision and dispose independent real boundaries for one pytest case."""
    image = _image()
    source = Path(os.environ.get("GITHUB_WORKSPACE", str(Path(__file__).parents[1]))).resolve()
    revisions = [subprocess.run(["git", "rev-parse", ref], cwd=source, check=True, capture_output=True, text=True).stdout.strip() for ref in ("HEAD", "HEAD^")]
    assert revisions[0] != revisions[1]
    runtime, workspace, routing, trust = (tmp_path / name for name in ("runtime", "workspace", "routing", "ci-trust"))
    for folder in (runtime, workspace, routing, trust, runtime / "home", runtime / "logs"):
        folder.mkdir()
    (routing / "ownership.json").write_text(json.dumps({"fixture/beta": "beta"}), encoding="utf-8")
    (runtime / "llm_config.toml").write_text('[backend]\norder = ["jules"]\n\n[backends.jules]\nbackend_type = "jules"\nenabled = true\napi_key = "ci-offline-unused"\n', encoding="utf-8")
    secret = os.urandom(48)
    (runtime / "bridge-secret").write_bytes(base64.b64encode(secret) + b"\n")
    fixtures = Path(__file__).parent / "fixtures"
    host_user = f"{os.getuid()}:{os.getgid()}"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(trust / "github.key"), "-out", str(trust / "github.crt"), "-subj", "/CN=api.github.com", "-addext", "subjectAltName=DNS:api.github.com", "-days", "1"], check=True, capture_output=True, text=True)
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
        *revisions,
        timeout=1200,
    )
    stage_logs = Path(os.environ.get("GITHUB_WORKSPACE", str(Path(__file__).parents[1]))) / "supervisor-integration-logs" / scenario_id
    stage_logs.mkdir(parents=True, exist_ok=True)
    (stage_logs / "stage.log").write_text(staged.stdout + staged.stderr, encoding="utf-8")
    if (runtime / "staging.log").exists():
        shutil.copyfile(runtime / "staging.log", stage_logs / "stage-internal.log")
    installations = json.loads(staged.stdout.strip().splitlines()[-1])
    for role, revision in zip(("source-a", "source-b", "source-a-alternate"), (revisions[0], revisions[1], revisions[0])):
        observe_nested_install_evidence(role, revision)
    ready_a, ready_b, alternate = [item["receipt"] for item in installations]
    assert all(item["installed"]["editable"] is False for item in installations)
    assert ready_a["source_sha"] == alternate["source_sha"] != ready_b["source_sha"]
    assert ready_a["content_identity"] != alternate["content_identity"]
    remote = _SourceService(source, tmp_path / "source.git")
    remote.publish_proof(ready_a, 101)
    remote.publish_proof(ready_b, 102)
    remote.executions[(11, 1)] = (revisions[0], "2026-10-01T01:00:00Z")
    octet = random.SystemRandom().randrange(64, 239)
    network, worker_name, github_name = (prefix + uuid.uuid4().hex[:12] for prefix in ("smoke-net-", "smoke-worker-", "smoke-github-"))
    address = f"10.250.{octet}.11"
    try:
        _docker("network", "create", "--driver", "bridge", "--internal", "--subnet", f"10.250.{octet}.0/24", network)
        _docker(
            "run",
            "-d",
            "--rm",
            "--name",
            github_name,
            "--network",
            network,
            "--network-alias",
            "api.github.com",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--sysctl",
            "net.ipv4.ip_unprivileged_port_start=0",
            "--user",
            host_user,
            "--entrypoint",
            "python",
            "-v",
            f"{trust}:/ci-trust:ro",
            "-v",
            f"{fixtures}:/fixtures:ro",
            image,
            "/fixtures/supervisor_fake_github_https.py",
            timeout=60,
        )
        _wait_for(lambda: "fixture GitHub HTTPS ready" in _docker("logs", github_name).stdout, timeout=20)
        environment = {
            "AUTO_CODER_SUPERVISOR_INSTANCE_ID": "beta-worker",
            "AUTO_CODER_CHANNEL": "beta",
            "AUTO_CODER_REPOSITORY": "fixture/beta",
            "AUTO_CODER_RUNTIME_ROOT": "/runtime",
            "AUTO_CODER_WORKSPACE_ROOT": "/workspace",
            "AUTO_CODER_OWNERSHIP_FILE": "/routing/ownership.json",
            "AUTO_CODER_ARTIFACT_DIGEST": "ci-local-smoke",
            "AUTO_CODER_SUPERVISOR_AUTO_START": "1",
            "AUTO_CODER_SOURCE_REPOSITORY_ROOT": "/source",
            "AUTO_CODER_BRIDGE_BIND_ADDRESS": address,
            "AUTO_CODER_BRIDGE_PORT": "8843",
            "AUTO_CODER_BRIDGE_SECRET_FILE": "/runtime/bridge-secret",
            "AUTO_CODER_CONFIG_PATH": "/runtime/llm_config.toml",
            "AUTO_CODER_HEALTH_LOG_ENABLED": "0",
            "AUTO_CODER_DISABLE_AUTO_UPDATE": "1",
            "GITHUB_TOKEN": "nonfunctional-ci-placeholder",
            "SSL_CERT_FILE": "/ci-trust/github.crt",
            "REQUESTS_CA_BUNDLE": "/ci-trust/github.crt",
            "NO_PROXY": "api.github.com,localhost,127.0.0.1",
            "no_proxy": "api.github.com,localhost,127.0.0.1",
            "HOME": "/runtime/home",
        }
        launch = ["run", "-d", "--rm", "--name", worker_name, "--network", network, "--ip", address, "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--user", host_user, "--entrypoint", "python"]
        for host, guest, mode in ((runtime, "/runtime", ""), (workspace, "/workspace", ""), (routing, "/routing", ""), (fixtures, "/fixtures", "ro"), (trust, "/ci-trust", "ro"), (source, "/source", "ro")):
            launch.extend(["-v", f"{host}:{guest}" + (f":{mode}" if mode else "")])
        for key, value in environment.items():
            launch.extend(["-e", f"{key}={value}"])
        _docker(*launch, image, "/fixtures/supervisor_offline_pid1.py", timeout=60)
        facts = _wait_for(lambda: facts if (facts := _container_facts(worker_name))["socket"] else None, timeout=30)
        assert facts["docker_socket"] is False and facts["docker_binary"] is None
        _wait_for(lambda: _inspect_worker(worker_name), timeout=90)
        config = WorkerEndpointConfig("beta-worker", "beta", "fixture/beta", address, 8843, secret)
        client = WorkerNetworkClient(config)

        def bootstrap_ready():
            client.inspect_supervisor()
            observed = client.observe_worker()
            lifecycle = observed.data.get("observation", {}).get("lifecycle", {}) if observed.data else {}
            return lifecycle if observed.result_code == "OK" and lifecycle.get("startup_reconciled") is True and lifecycle.get("daemon_serving") is True else None

        _wait_for(bootstrap_ready, timeout=90)
        with _source_http(remote) as api_url:
            selecting = BetaSourceSelector(GitHubGitDataClient("external-selector", "owner/repo", api_url=api_url), "owner/repo")
            reader = BetaEvidenceReader(GitHubGitDataClient("read-only-fixture", "owner/repo", api_url=api_url), "owner/repo")
            assert selecting.advance("r77", None).certainty is MutationCertainty.CONFIRMED
            base, _grants, identity, signed, _uids, state = real_boundary
            origin = base.oauth.canonical_issuer
            oauth = OAuthSettings(tmp_path / "oauth.sqlite3", origin, base.oauth.firebase_project_id, (OAuthClient("alice-client", (origin + "/alice-callback",), allowed_scopes=(READ_SCOPE, DEPLOY_SCOPE)),), enabled_scopes=(READ_SCOPE, DEPLOY_SCOPE))
            policy = tmp_path / "operator.yaml"
            policy.write_text("profile: beta\ntargets:\n  beta-worker:\n    channel: beta\n    repositories: [fixture/beta]\n", encoding="utf-8")
            settings = ManagementSettings(tmp_path, oauth, port=base.port, operating_policy_path=policy, operation_state_root=tmp_path / "journal", source_repository="owner/repo")
            grants = OAuthGrantService(oauth, identity, clock=lambda: state["now"])
            publisher = SourceAcceptancePublisher(GitHubGitDataClient("external-selector", "owner/repo", api_url=api_url), "owner/repo")
            app = create_management_app(settings, grants, worker_clients={"beta": client}, beta_acceptance_source=reader, beta_acceptance_publisher=publisher, beta_acceptance_profile=SmokeProfile(total_seconds=180, interval_seconds=2))
            _configure_lifecycle(app, reader)
            configuration = tmp_path / "reconstruction.json"
            configuration.write_text(json.dumps({"journal_root": str(tmp_path / "journal"), "address": address, "secret": base64.b64encode(secret).decode(), "source_api": api_url}), encoding="utf-8")
            with TestClient(app, base_url=origin) as http:
                bearer = _issue(http, settings, signed, "alice", "alice-client", READ_SCOPE + " " + DEPLOY_SCOPE)["access_token"]
                deployed = _call(http, bearer, "auto_coder_deploy_beta_source", _argument(client, revisions[0], "smoke-deploy-a"))
                deployment_id = str(deployed["operation_id"])
                _await_stage(app, deployment_id, "SUCCEEDED", timeout=210)
                journal = app.state.management_runtime.journal
                yield _SmokeSystem(journal, client, reader, selecting, remote, app, http, bearer, runtime, installations, revisions, deployment_id, configuration)
            assert remote.read_only_mutations == []
    finally:
        _cleanup_smoke_docker(worker_name, github_name, network, scenario_id)


def _cleanup_smoke_docker(worker_name: str, github_name: str, network: str, scenario_id: str) -> None:
    """Always remove both owned containers and their network after a failure."""
    log_root = Path(os.environ.get("GITHUB_WORKSPACE", str(Path(__file__).parents[1]))) / "supervisor-integration-logs" / scenario_id
    for name, destination in ((worker_name, "worker.log"), (github_name, "github.log")):
        try:
            log_root.mkdir(parents=True, exist_ok=True)
            result = _docker("logs", name, check=False)
            (log_root / destination).write_text(result.stdout + result.stderr, encoding="utf-8")
        except (OSError, subprocess.SubprocessError) as error:
            print(f"{scenario_id}: could not capture {destination}: {error}", file=sys.stderr)
        try:
            _docker("rm", "-f", name, check=False)
        except (OSError, subprocess.SubprocessError) as error:
            print(f"{scenario_id}: could not clean up {name}: {error}", file=sys.stderr)
    try:
        _docker("network", "rm", network, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        print(f"{scenario_id}: could not clean up {network}: {error}", file=sys.stderr)


def _cleanup_staging_container(stage_name: str, scenario_id: str, case_root: Path) -> None:
    """Collect available installer evidence and terminate its exact Docker owner."""
    log_root = Path(os.environ.get("GITHUB_WORKSPACE", str(Path(__file__).parents[1]))) / "supervisor-integration-logs" / scenario_id
    errors: list[str] = []
    original_error = sys.exc_info()[1]
    if isinstance(original_error, subprocess.TimeoutExpired):
        # TimeoutExpired can retain bytes even when subprocess used text=True.
        def output(value: str | bytes | None) -> str:
            return value.decode(errors="replace") if isinstance(value, bytes) else value or ""

        available = output(original_error.stdout) + output(original_error.stderr)
        if available:
            try:
                log_root.mkdir(parents=True, exist_ok=True)
                (log_root / "stage-cli-timeout.log").write_text(available, encoding="utf-8")
            except OSError as error:
                errors.append(f"could not retain staging CLI output: {error}")
    try:
        logs = _docker("logs", stage_name, check=False, timeout=20)
        if logs.returncode == 0:
            log_root.mkdir(parents=True, exist_ok=True)
            (log_root / "stage-container.log").write_text(logs.stdout + logs.stderr, encoding="utf-8")
    except (OSError, subprocess.SubprocessError) as error:
        print(f"{scenario_id}: staging log unavailable: {error}", file=sys.stderr)
    try:
        internal_log = case_root / "runtime" / "staging.log"
        if internal_log.is_file():
            log_root.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(internal_log, log_root / "stage-internal.log")
    except OSError as error:
        errors.append(f"could not retain staging file log: {error}")
    try:
        _docker("rm", "-f", stage_name, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        errors.append(f"could not remove staging container: {error}")
    try:
        inspected = _docker("inspect", stage_name, check=False, timeout=15)
        if inspected.returncode == 0:
            errors.append(f"staging container still exists: {stage_name}")
        elif "no such object" not in inspected.stderr.lower() and "no such container" not in inspected.stderr.lower():
            errors.append(f"staging removal could not be verified: {inspected.stderr.strip()}")
    except (OSError, subprocess.SubprocessError) as error:
        errors.append(f"could not verify staging container removal: {error}")
    if errors:
        if sys.exc_info()[0] is None:
            raise AssertionError("; ".join(errors))
        print(f"{scenario_id}: staging cleanup issues: {'; '.join(errors)}", file=sys.stderr)


@contextmanager
def _owned_staging(stage_name: str, scenario_id: str, case_root: Path) -> Iterator[None]:
    """Register the installer cleanup before starting the attached Docker CLI."""
    try:
        yield
    finally:
        _cleanup_staging_container(stage_name, scenario_id, case_root)


@pytest.fixture
def smoke_system(real_boundary, tmp_path: Path, request: pytest.FixtureRequest, _use_real_commands: None, _use_real_sleep: None) -> Iterator[_SmokeSystem]:
    # The owned directory closes even if staging fails before Docker starts.
    # Keep the real_boundary OAuth fixture outside this per-scenario root.
    stage_name = "smoke-stage-" + uuid.uuid4().hex[:16]
    with tempfile.TemporaryDirectory(prefix="smoke-", dir=tmp_path) as case_root:
        with _owned_staging(stage_name, request.node.name, Path(case_root)):
            with _smoke_system(real_boundary, Path(case_root), request.node.name, stage_name) as system:
                yield system


@pytest.mark.timeout(90)
def test_staging_timeout_reaps_real_container_and_descendant(tmp_path: Path, _use_real_commands: None, _use_real_sleep: None) -> None:
    """A timed-out attached Docker CLI leaves no installer or owned child."""
    image = _image()
    stage_name = "smoke-stage-timeout-" + uuid.uuid4().hex[:12]
    scenario_id = "staging-timeout-" + uuid.uuid4().hex[:12]
    descendant = dedent("""\
        import pathlib
        import time

        pathlib.Path('/owned/descendant-started').write_text('started')
        heartbeat = pathlib.Path('/owned/descendant-heartbeat')
        n = 0
        while True:
            n += 1
            heartbeat.write_text(str(n))
            time.sleep(0.1)
        """)
    program = "import pathlib, subprocess, sys, time; " "pathlib.Path('/owned/stage-started').write_text('started'); " f"subprocess.Popen([sys.executable, '-c', {descendant!r}]); " "print('real staging child entered', flush=True); time.sleep(45)"
    with tempfile.TemporaryDirectory(prefix="stage-timeout-", dir=tmp_path) as case_root:
        owned = Path(case_root)
        with pytest.raises(subprocess.TimeoutExpired):
            with _owned_staging(stage_name, scenario_id, owned):
                _docker(
                    "run",
                    "--rm",
                    "--name",
                    stage_name,
                    "--network",
                    "none",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges:true",
                    "--user",
                    f"{os.getuid()}:{os.getgid()}",
                    "-v",
                    f"{owned}:/owned",
                    "--entrypoint",
                    "python",
                    image,
                    "-c",
                    program,
                    timeout=8,
                )
        assert (owned / "stage-started").read_text() == "started"
        assert (owned / "descendant-started").read_text() == "started"
        heartbeat = owned / "descendant-heartbeat"
        before = heartbeat.read_text()
        time.sleep(0.6)
        assert heartbeat.read_text() == before
        inspected = _docker("inspect", stage_name, check=False)
        assert inspected.returncode != 0 and "no such object" in inspected.stderr.lower()
        remaining = _docker("ps", "-a", "--filter", f"name=^/{stage_name}$", "--format", "{{.Names}}")
        assert remaining.stdout.strip() == ""
        log_root = Path(os.environ.get("GITHUB_WORKSPACE", str(Path(__file__).parents[1]))) / "supervisor-integration-logs" / scenario_id
        retained = [path.read_text(encoding="utf-8") for path in (log_root / "stage-container.log", log_root / "stage-cli-timeout.log") if path.exists()]
        assert retained and any("real staging child entered" in text for text in retained)


@pytest.mark.timeout(1800)
def test_local_smoke_uses_installed_worker_real_progress_and_original_journal(smoke_system: _SmokeSystem) -> None:
    """Recover the original journal and publish only its proven real PASS."""
    _exercise_smoke(smoke_system, "core")


@pytest.mark.timeout(1800)
def test_local_smoke_diagnostics_do_not_advance_real_worker_progress(smoke_system: _SmokeSystem) -> None:
    """Authenticated diagnostics cannot advance an unchanged real heartbeat."""
    _exercise_smoke(smoke_system, "diagnostics")


@pytest.mark.timeout(1800)
def test_local_smoke_preserves_original_evidence_across_real_faults(smoke_system: _SmokeSystem) -> None:
    """Refuse invalid/faulted evidence without rewriting the original window."""
    _exercise_smoke(smoke_system, "faults")


@pytest.mark.timeout(1800)
def test_local_smoke_later_selection_preserves_proof_and_publication_authority(smoke_system: _SmokeSystem) -> None:
    """Later same-SHA selection cannot bypass original or current authority."""
    _exercise_smoke(smoke_system, "later_selection")


@pytest.mark.timeout(1800)
def test_local_smoke_selected_retry_and_same_sha_supersession(smoke_system: _SmokeSystem) -> None:
    """Only a selected retry can establish or supersede local source evidence."""
    _exercise_smoke(smoke_system, "retry")


@pytest.mark.timeout(1800)
def test_local_smoke_distinct_revision_and_worker_lifecycle_outcomes(smoke_system: _SmokeSystem) -> None:
    """A second installed revision and genuine child replacement are distinct."""
    _exercise_smoke(smoke_system, "lifecycle")


def _exercise_smoke(system: _SmokeSystem, scenario: str) -> None:
    journal, client, reader, selecting, remote, app, http, bearer, runtime, installations, revisions, deployed_id, reconstruction = (
        system.journal,
        system.client,
        system.reader,
        system.selecting,
        system.remote,
        system.app,
        system.http,
        system.bearer,
        system.runtime,
        system.installations,
        system.revisions,
        system.deployed_id,
        system.reconstruction,
    )
    profile = SmokeProfile(total_seconds=180, interval_seconds=2)
    producer = BetaRuntimeSmokeProducer(journal, client, reader, profile)
    ready_a, ready_b, alternate = [item["receipt"] for item in installations]

    def accept(key: str, *, deployment: str = deployed_id, sha: str = revisions[0]):
        return _accepted(journal, client, deployment, sha, key)

    def result(observation, expected: str):
        assert observation.state.value == "COMPLETE", observation
        assert observation.evidence is not None
        assert observation.evidence.local_result.value == expected, observation.evidence.document()
        document = observation.evidence.document()
        assert document["operation_id"] == observation.operation_id
        assert document["local_result"] != "PASS_PUBLISHED"
        return document

    def captured(record):
        observation = producer.advance(record.operation_id)
        assert observation.state.value == "IN_PROGRESS", observation
        snapshot = journal.smoke_snapshot(record.operation_id)
        assert snapshot is not None and snapshot[1]["capture"] is not None
        assert len(snapshot[1]["samples"]) == 1 and snapshot[1]["samples"][0]["qualifying"] is True
        return snapshot[1]

    def reject_local_result(record):
        current = journal.get(record.operation_id).record
        assert current is not None
        settled = app.state.management_runtime.beta_acceptance.forward(current)
        assert settled.phase is OperationPhase.FAILED
        return settled

    service = app.state.management_runtime

    def run(key):
        worker = client.inspect_supervisor().data["worker"]
        return _call(
            http,
            bearer,
            "auto_coder_run_beta_acceptance",
            {
                "target_id": "beta-worker",
                "selected_git_sha": revisions[0],
                "installed_runtime_id": worker["runtime_id"],
                "process_run_id": worker["process_run_id"],
                "deployment_generation": worker["deployment_generation"],
                "supervisor_generation": client.inspect_supervisor().supervisor_generation,
                "deployment_operation_id": deployed_id,
                "idempotency_key": key,
            },
        )

    def read(operation_id):
        return _call(http, bearer, "auto_coder_beta_acceptance_get", {"target_id": "beta-worker", "operation_id": operation_id})

    @contextmanager
    def reconstructed_service():
        rebuilt = ManagementRuntime(service.settings, service.grants, service.clients, beta_acceptance_source=reader, beta_acceptance_publisher=service.beta_acceptance.publisher)
        http.portal.call(rebuilt.dispatcher.start)
        http.portal.call(rebuilt.start_monitor)
        try:
            yield rebuilt
        finally:
            http.portal.call(rebuilt.stop_monitor)
            http.portal.call(rebuilt.dispatcher.stop)

    def deploy_while_monitored(source_sha: str, idempotency_key: str) -> str:
        """Run every later deployment with its service-owned continuation active."""
        http.portal.call(service.start_monitor)
        try:
            admitted = _call(
                http,
                bearer,
                "auto_coder_deploy_beta_source",
                _argument(client, source_sha, idempotency_key),
            )
            operation_id = str(admitted["operation_id"])
            _await_stage(app, operation_id, "SUCCEEDED", timeout=210)
            return operation_id
        finally:
            # Direct producer and authority-fault cases need stable journal reads.
            http.portal.call(service.stop_monitor)

    if scenario == "core":
        invalid = _call(
            http,
            bearer,
            "auto_coder_run_beta_acceptance",
            {
                "target_id": "beta-worker",
                "selected_git_sha": "main",
                "installed_runtime_id": "runtime",
                "process_run_id": "process",
                "deployment_generation": "deployment",
                "supervisor_generation": "supervisor",
                "deployment_operation_id": "operation",
                "idempotency_key": "invalid-source",
            },
        )
        assert invalid == {"status": "INVALID_REQUEST", "reason": "invalid_beta_acceptance_request"}
        remote.lose_acceptance_ref_response = True
        started = run("local-pass-original")
        first = journal.get(started["operation_id"]).record
        first_capture = journal.smoke_snapshot(first.operation_id)[1]
        assert first_capture["result"] is None
        assert len(first_capture["samples"]) == 1
        assert first_capture["capture"]["source"]["proof_run_id"] == 101
        assert first_capture["capture"]["source"]["selected_run_id"] == 11
        assert first_capture["capture"]["worker"]["ready_content_identity"] == ready_a["content_identity"]
        http.portal.call(service.stop_monitor)
        http.portal.call(service.dispatcher.stop)
        with reconstructed_service():
            # Only authenticated reads drive the caller; the service owns continuation.
            def publication_uncertain():
                projection = read(first.operation_id)
                return projection if journal.get(first.operation_id).record.phase is OperationPhase.NEEDS_ATTENTION else None

            _wait_for(publication_uncertain, timeout=190)
        after_restart = journal.smoke_snapshot(first.operation_id)[1]
        for key in ("window", "profile", "binding", "capture"):
            assert after_restart[key] == first_capture[key]
        passed = producer.read(first.operation_id)
        document = result(passed, "PASS")
        qualifying = [sample for sample in document["samples"] if sample["qualifying"]]
        assert len(qualifying) >= 2
        assert all(left["heartbeat"]["sequence"] < right["heartbeat"]["sequence"] for left, right in zip(qualifying, qualifying[1:]))
        assert all(sample["runtime_binding"]["process_run_id"] == first.intent.expected_incarnation for sample in qualifying)
        assert all(sample["artifact"]["source_revision"]["origin"] == "build_embedded" for sample in qualifying)
        assert all(sample["artifact"]["distribution_version"]["origin"] == "installed_distribution_metadata" for sample in qualifying)
        assert all(sample["runtime_binding"]["lock_sha256"] == ready_a["lock_sha256"] for sample in qualifying)
        assert journal.get(first.operation_id).record.phase is OperationPhase.NEEDS_ATTENTION
        writes = remote.acceptance_writes
        assert writes == 2
        original_snapshot = journal.smoke_snapshot(first.operation_id)
        remote.block_acceptance_readback = False
        ref = f"refs/tags/auto-coder-beta-acceptance-{first.operation_id}"
        original_oid = remote._git("rev-parse", ref)
        # A canonical conflicting record must not settle the original operation.
        original_tag = remote._git("cat-file", "-p", original_oid)
        headers, annotation_text = original_tag.split("\n\n", 1)
        conflict = json.loads(annotation_text)
        conflict["local_evidence"]["capture"]["worker"]["process_run_id"] = "different-worker"
        conflict_content = json.dumps(conflict["local_evidence"], sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        conflict["local_evidence_content_sha256"] = hashlib.sha256(conflict_content.encode()).hexdigest()
        conflicting_oid = remote._git("mktag", input=headers + "\n\n" + json.dumps(conflict, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n")
        remote._git("update-ref", ref, conflicting_oid)
        with reconstructed_service() as rebuilt:
            http.portal.call(rebuilt.dispatcher.drain)
            assert journal.get(first.operation_id).record.phase is OperationPhase.NEEDS_ATTENTION
            assert read(first.operation_id)["status"] != "PASS_PUBLISHED"
        remote._git("update-ref", ref, original_oid)
        with reconstructed_service() as rebuilt:
            http.portal.call(rebuilt.dispatcher.drain)
            assert read(first.operation_id)["status"] == "PASS_PUBLISHED"
        assert remote.acceptance_writes == writes
        assert journal.smoke_snapshot(first.operation_id) == original_snapshot
        http.portal.call(service.dispatcher.start)
        http.portal.call(service.start_monitor)
        readback = SourceAcceptancePublisher(remote, "owner/repo").read(first.operation_id)
        assert readback is not None and readback.direct_commit_sha == revisions[0]
        annotation = json.loads(readback.annotation)
        assert annotation["local_evidence"] == document
        assert annotation["local_evidence_content_sha256"] == passed.evidence.content_sha256
        assert annotation["local_evidence"]["capture"] == first_capture["capture"]
        assert _call(http, bearer, "auto_coder_beta_acceptance_get", {"target_id": "beta-worker", "operation_id": first.operation_id})["status"] == "PASS_PUBLISHED"
        replay = journal.admit(first.intent, first.authority)
        assert replay.status is AdmissionStatus.REPLAY and replay.record.operation_id == first.operation_id
        assert replay.record.authority == first.authority
        changed = replace(first.intent, expected_artifact=alternate["installed_runtime_id"])
        assert journal.admit(changed, first.authority).status is AdmissionStatus.CONFLICT
        remote.fail_reads = True
        try:
            # Completed readback needs neither Git availability nor a publisher.
            immutable = _fresh_process(reconstruction, first.operation_id, "read")
            assert immutable["state"] == "COMPLETE" and immutable["content"] == passed.evidence.content
            assert producer.advance(first.operation_id).evidence.content == passed.evidence.content
        finally:
            remote.fail_reads = False
        return

    if scenario == "diagnostics":
        # The real maintenance loop sleeps for 60 seconds. Diagnostics inside a
        # bounded eight-second window must not manufacture a second progress event.
        def recent_sleep():
            client.inspect_supervisor()
            observed = client.observe_worker()
            if observed.result_code != "OK" or observed.data is None:
                return None
            heartbeat = observed.data["observation"]["heartbeat"]
            return heartbeat if heartbeat["stage"] == "producer:maintenance-sleep" and heartbeat["age_seconds"] < 5 else None

        original_beat = _wait_for(recent_sleep, timeout=90)
        service.beta_acceptance.producer = BetaRuntimeSmokeProducer(journal, client, reader, SmokeProfile(total_seconds=8, interval_seconds=1))
        started = run("stuck-real-heartbeat")
        stuck = journal.get(started["operation_id"]).record
        original_window = journal.smoke_snapshot(stuck.operation_id)[1]["window"]
        diagnostics = []
        with recorder().span("beta_smoke.blocked_progress_observation", operation_id=stuck.operation_id, configured_window_seconds=8):
            until = time.monotonic() + 15
            while time.monotonic() < until:
                diagnostics.append(client.diagnostics("status").result_code)
                read(stuck.operation_id)
                observed = producer.read(stuck.operation_id)
                if observed.state.value == "COMPLETE" and journal.get(stuck.operation_id).record.phase is OperationPhase.FAILED:
                    break
                time.sleep(0.3)
        assert journal.smoke_snapshot(stuck.operation_id)[1]["window"] == original_window
        assert journal.get(stuck.operation_id).record.phase is OperationPhase.FAILED
        assert SourceAcceptancePublisher(remote, "owner/repo").read(stuck.operation_id) is None
        # Remaining cases explicitly exercise lower-level producer faults.
        http.portal.call(service.stop_monitor)
        service.beta_acceptance.producer = producer
        blocked = result(observed, "BLOCKED")
        assert diagnostics and all(code == "OK" for code in diagnostics)
        assert blocked["reason"] == "original_deadline_without_progress"
        assert len(blocked["samples"]) >= 2
        assert {sample["heartbeat"]["sequence"] for sample in blocked["samples"]} == {original_beat["sequence"]}
        assert len({sample["nonce"] for sample in blocked["samples"]}) == len(blocked["samples"])
        return

    if scenario == "faults":
        http.portal.call(service.stop_monitor)
        first = accept("fault-family-original")
        _release_local_lane(journal, first.operation_id)

        for key, invalid in (("missing-profile", None), ("invalid-profile", SmokeProfile(total_seconds=3, interval_seconds=0))):
            operation = accept(key)
            invalid_producer = BetaRuntimeSmokeProducer(journal, client, reader, invalid)
            invalid_document = result(invalid_producer.advance(operation.operation_id), "BLOCKED")
            assert invalid_document["samples"] == [] and invalid_document["capture"] is None
            _release_local_lane(journal, operation.operation_id)

        def unavailable_clock() -> float:
            raise OSError("clock source unavailable")

        operation = accept("unavailable-clock")
        no_clock = BetaRuntimeSmokeProducer(journal, client, reader, profile, clock=unavailable_clock)
        assert result(no_clock.advance(operation.operation_id), "BLOCKED")["samples"] == []
        _release_local_lane(journal, operation.operation_id)

        operation = accept("reconstruct-expired")
        expiring = BetaRuntimeSmokeProducer(journal, client, reader, SmokeProfile(total_seconds=8, interval_seconds=1))
        assert expiring.advance(operation.operation_id).state.value == "IN_PROGRESS"
        original = journal.smoke_snapshot(operation.operation_id)[1]
        time.sleep(8.1)
        expired = _fresh_process(reconstruction, operation.operation_id)
        assert expired["state"] == "COMPLETE" and expired["evidence"]["local_result"] == "BLOCKED"
        assert expired["evidence"]["window"] == original["window"]
        assert expired["evidence"]["samples"] == original["samples"]
        assert expired["evidence"]["capture"] == original["capture"]
        _release_local_lane(journal, operation.operation_id)

        # Force a real SQLite transaction failure after the first durable sample.
        import sqlite3

        operation = accept("store-failure")
        failing = BetaRuntimeSmokeProducer(journal, client, reader, SmokeProfile(total_seconds=8, interval_seconds=1))
        assert failing.advance(operation.operation_id).state.value == "IN_PROGRESS"
        before_failure = journal.smoke_snapshot(operation.operation_id)[1]
        database = Path(json.loads(reconstruction.read_text())["journal_root"]) / "management-operations.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TRIGGER smoke_write_failure BEFORE UPDATE ON beta_runtime_smoke BEGIN SELECT RAISE(ABORT, 'simulated I/O uncertainty'); END")
        try:
            time.sleep(8.1)
            assert failing.advance(operation.operation_id).state.value == "UNAVAILABLE"
            assert failing.read(operation.operation_id).state.value == "IN_PROGRESS"
            assert journal.smoke_snapshot(operation.operation_id)[1] == before_failure
        finally:
            with sqlite3.connect(database) as connection:
                connection.execute("DROP TRIGGER smoke_write_failure")
        recovered = result(failing.advance(operation.operation_id), "BLOCKED")
        assert recovered["window"] == before_failure["window"]
        assert recovered["samples"] == before_failure["samples"]
        _release_local_lane(journal, operation.operation_id)

        # A real second same-SHA installation has a distinct READY identity. Its
        # receipt was never used to launch this child and cannot qualify it.
        ready_path = _host_path(runtime, installations[0]["ready_path"])
        alternate_path = _host_path(runtime, installations[2]["ready_path"])
        operation = accept("wrong-same-sha-ready")
        original_ready = ready_path.read_bytes()
        try:
            ready_path.write_bytes(alternate_path.read_bytes())
            wrong_ready = result(producer.advance(operation.operation_id), "BLOCKED")
            assert wrong_ready["capture"]["worker"] is None
            assert wrong_ready["capture"]["source"]["source_sha"] == revisions[0]
            assert wrong_ready["samples"] and wrong_ready["samples"][0]["qualifying"] is False
        finally:
            ready_path.write_bytes(original_ready)
        assert producer.advance(operation.operation_id).evidence.local_result.value == "BLOCKED"
        reject_local_result(operation)
        assert SourceAcceptancePublisher(remote, "owner/repo").read(operation.operation_id) is None

        provenance = _host_path(runtime, installations[0]["installed"]["module"]).parent / "build_provenance.json"
        operation = accept("missing-installed-provenance")
        original_provenance = provenance.read_bytes()
        try:
            provenance.unlink()
            no_provenance = result(producer.advance(operation.operation_id), "BLOCKED")
            assert no_provenance["capture"]["worker"] is None
            assert no_provenance["capture"]["deployment"]["operation_id"] == deployed_id
        finally:
            provenance.write_bytes(original_provenance)
        _release_local_lane(journal, operation.operation_id)

        for evidence_fault in ("unavailable", "failed-attempt", "incomplete-jobs"):
            operation = accept("source-" + evidence_fault)
            remote.fail_reads = evidence_fault == "unavailable"
            if evidence_fault == "failed-attempt":
                remote.failed_attempts.add((11, 1))
            if evidence_fault == "incomplete-jobs":
                remote.missing_jobs.add((11, 1))
            try:
                unavailable = result(producer.advance(operation.operation_id), "BLOCKED")
                assert unavailable["samples"] == []
            finally:
                remote.fail_reads = False
                remote.failed_attempts.discard((11, 1))
                remote.missing_jobs.discard((11, 1))
            _release_local_lane(journal, operation.operation_id)

        # The same journal rejects acceptance while a target's update lane is
        # unresolved. Passing that operation to the producer is also a conflict.
        previous = journal.get(deployed_id).record
        pending = journal.admit(OperationIntent(previous.intent.target, "deploy_beta_source", previous.intent.parameters, "held-unresolved-update", previous.intent.expected_incarnation, previous.intent.expected_artifact), previous.authority)
        assert pending.status is AdmissionStatus.CREATED and pending.record is not None
        # Exercise the real monitor's missing-evidence hold deterministically;
        # cleanup must use its evidence-bearing NEEDS_ATTENTION resolution path.
        app.state.management_runtime.lifecycle.process(pending.record)
        assert journal.get(pending.record.operation_id).record.phase is OperationPhase.NEEDS_ATTENTION
        attempted = journal.admit(replace(first.intent, idempotency_key="blocked-by-update"), first.authority)
        assert attempted.status is AdmissionStatus.BUSY
        assert producer.advance(pending.record.operation_id).state.value == "CONFLICT"
        assert journal.smoke_snapshot(pending.record.operation_id) is None
        _release_local_lane(journal, pending.record.operation_id)
        return

    if scenario == "later_selection":
        http.portal.call(service.stop_monitor)
        original = accept("later-original-proof")
        first_capture = captured(original)
        _release_local_lane(journal, original.operation_id)

        remote.executions[(12, 1)] = (revisions[0], "2026-10-02T02:00:00Z")
        assert selecting.advance("r78", revisions[0]).certainty is MutationCertainty.CONFIRMED
        mismatch = accept("selection-deployment-mismatch")
        assert result(producer.advance(mismatch.operation_id), "BLOCKED")["reason"] == "deployment_selection_or_candidate_mismatch"
        _release_local_lane(journal, mismatch.operation_id)

        # The explicit producer-fault cases run with lifecycle polling stopped.
        # Resume it while sending each later deployment through the real manager.
        new_run_deployment = deploy_while_monitored(revisions[0], "smoke-deploy-new-run")
        later = accept("later-successful-same-sha", deployment=new_run_deployment)
        later_capture = captured(later)
        assert later_capture["capture"]["source"]["tested_source_oid"] == first_capture["capture"]["source"]["tested_source_oid"]
        assert later_capture["capture"]["source"]["proof_run_id"] == 101
        assert (later_capture["capture"]["source"]["selected_run_id"], later_capture["capture"]["source"]["selected_run_attempt"]) == (12, 1)
        assert result(_finish(producer, later.operation_id), "PASS")["capture"] == later_capture["capture"]
        grant_service = app.state.management_runtime.grants
        original_grant_settings = grant_service.settings
        grant_service.settings = replace(original_grant_settings, enabled_scopes=(READ_SCOPE,))
        try:
            held = app.state.management_runtime.beta_acceptance.forward(journal.get(later.operation_id).record)
            assert held.phase is OperationPhase.NEEDS_ATTENTION
            assert held.safe_error_category == "original_authority_denied"
            assert SourceAcceptancePublisher(remote, "owner/repo").read(later.operation_id) is None
        finally:
            grant_service.settings = original_grant_settings
        _release_local_lane(journal, later.operation_id)

        # A durable local PASS does not authorize publication after the current
        # operator policy is downgraded.  Exercise the real policy file and
        # production handler so the pre-publish fence, rather than a synthetic
        # resolver result, proves that no Git tag object or ref is created.
        policy_denied = accept("current-policy-denied", deployment=new_run_deployment)
        captured(policy_denied)
        policy_evidence = result(_finish(producer, policy_denied.operation_id), "PASS")
        policy_snapshot = journal.smoke_snapshot(policy_denied.operation_id)
        policy_path = service.settings.operating_policy_path
        assert policy_path is not None
        original_policy = policy_path.read_text(encoding="utf-8")
        writes_before_policy_denial = remote.acceptance_writes
        try:
            policy_path.write_text(original_policy.replace("profile: beta", "profile: observe"), encoding="utf-8")
            held = service.beta_acceptance.forward(journal.get(policy_denied.operation_id).record)
            assert held.phase is OperationPhase.NEEDS_ATTENTION
            assert held.safe_error_category == "current_policy_denied"
            assert remote.acceptance_writes == writes_before_policy_denial
            assert SourceAcceptancePublisher(remote, "owner/repo").read(policy_denied.operation_id) is None
            assert journal.smoke_snapshot(policy_denied.operation_id) == policy_snapshot
            preserved = producer.read(policy_denied.operation_id)
            assert preserved.evidence is not None
            assert preserved.evidence.document() == policy_evidence
        finally:
            policy_path.write_text(original_policy, encoding="utf-8")
        _release_local_lane(journal, policy_denied.operation_id)
        return

    if scenario == "retry":
        http.portal.call(service.stop_monitor)
        original = accept("retry-original-proof")
        original_capture = captured(original)
        _release_local_lane(journal, original.operation_id)

        # The retry is a real later attempt of an already observable run, not
        # an invented attempt 2 with a missing attempt 1.
        remote.executions[(12, 1)] = (revisions[0], "2026-10-02T02:00:00Z")
        assert selecting.advance("r78", revisions[0]).certainty is MutationCertainty.CONFIRMED
        remote.executions[(12, 2)] = (revisions[0], "2026-10-03T03:00:00Z")
        assert selecting.advance("r79", revisions[0]).certainty is MutationCertainty.CONFIRMED
        retry_deployment = deploy_while_monitored(revisions[0], "smoke-deploy-retry")
        accepted_retry = accept("successful-retry-original-proof", deployment=retry_deployment)
        retry_capture = captured(accepted_retry)
        assert (retry_capture["capture"]["source"]["selected_run_id"], retry_capture["capture"]["source"]["selected_run_attempt"]) == (12, 2)
        assert retry_capture["capture"]["source"]["tested_source_oid"] == original_capture["capture"]["source"]["tested_source_oid"]
        # A newer CI execution by itself is not a new beta selection.
        remote.executions[(13, 1)] = (revisions[0], "2026-10-04T04:00:00Z")
        time.sleep(2.1)
        unselected = producer.advance(accepted_retry.operation_id)
        assert unselected.state.value in {"IN_PROGRESS", "COMPLETE"}
        if unselected.evidence is not None:
            assert unselected.evidence.local_result.value == "PASS"
        assert journal.smoke_snapshot(accepted_retry.operation_id)[1]["capture"] == retry_capture["capture"]
        retry_pass = result(_finish(producer, accepted_retry.operation_id), "PASS")
        assert retry_pass["capture"]["source"]["selected_run_attempt"] == 2
        _release_local_lane(journal, accepted_retry.operation_id)

        same_sha = accept("same-sha-selection-supersedes", deployment=retry_deployment)
        old_capture = captured(same_sha)
        assert selecting.advance("r80", revisions[0]).certainty is MutationCertainty.CONFIRMED
        time.sleep(2.1)
        superseded = _fresh_process(reconstruction, same_sha.operation_id)
        assert superseded["state"] == "COMPLETE" and superseded["evidence"]["local_result"] == "SUPERSEDED"
        assert superseded["evidence"]["capture"] == old_capture["capture"]
        assert superseded["evidence"]["window"] == old_capture["window"]
        assert superseded["evidence"]["samples"] == old_capture["samples"]
        assert client.observe_worker().data["observation"]["process_run_id"] == same_sha.intent.expected_incarnation
        _release_local_lane(journal, same_sha.operation_id)
        return

    if scenario == "lifecycle":
        http.portal.call(service.stop_monitor)
        original = accept("lifecycle-original-pass")
        original_document = result(_finish(producer, original.operation_id), "PASS")
        _release_local_lane(journal, original.operation_id)

        # Deploy the second actual revision, then prove its own local window.
        remote.executions[(14, 1)] = (revisions[1], "2026-10-05T05:00:00Z")
        assert selecting.advance("r81", revisions[0]).certainty is MutationCertainty.CONFIRMED
        second_deployment = deploy_while_monitored(revisions[1], "smoke-deploy-b")
        second_local = accept("second-installed-revision", deployment=second_deployment, sha=revisions[1])
        second_document = result(_finish(producer, second_local.operation_id), "PASS")
        assert second_document["source_sha"] == revisions[1]
        assert second_document["capture"]["worker"]["installed_runtime_id"] == ready_b["installed_runtime_id"]
        assert second_document["capture"]["worker"]["source_revision"]["value"] != original_document["capture"]["worker"]["source_revision"]["value"]
        _release_local_lane(journal, second_local.operation_id)

        replacement = accept("worker-replaced-between-samples", deployment=second_deployment, sha=revisions[1])
        replacement_capture = captured(replacement)
        _replace_running_worker(client, ready_b["installed_runtime_id"])
        time.sleep(2.1)
        replaced = result(producer.advance(replacement.operation_id), "SUPERSEDED")
        assert replaced["capture"] == replacement_capture["capture"]
        assert replaced["window"] == replacement_capture["window"]
        assert len([sample for sample in replaced["samples"] if sample["qualifying"]]) == 1
        reject_local_result(replacement)
        assert SourceAcceptancePublisher(remote, "owner/repo").read(replacement.operation_id) is None

        # A genuine lifecycle predicate violation is FAIL, not unavailable evidence.
        refreshed_id = deploy_while_monitored(revisions[1], "smoke-deploy-b-refreshed")
        stopped = accept("worker-stopped-is-fail", deployment=refreshed_id, sha=revisions[1])
        captured(stopped)
        _stop_running_worker(client)
        time.sleep(2.1)
        assert result(producer.advance(stopped.operation_id), "FAIL")["reason"] == "worker_lifecycle_not_running"
        _release_local_lane(journal, stopped.operation_id)
        return

    raise AssertionError(f"unknown smoke scenario: {scenario}")


def _stop_running_worker(client: WorkerNetworkClient) -> None:
    inspected = client.inspect_supervisor()
    assert inspected.result_code == "OK" and inspected.data is not None
    worker = inspected.data["worker"]
    identifier = "external-stop-" + uuid.uuid4().hex
    client.dispatch_worker_operation(identifier, "REQUEST_GRACEFUL_STOP", process_run_id=worker["process_run_id"], deployment_generation=worker["deployment_generation"], selected_runtime_id=worker["runtime_id"])
    _wait_for(lambda: result if (result := client.read_worker_operation(identifier)).result_code == "STOPPED" else None, timeout=90)


def _replace_running_worker(client: WorkerNetworkClient, runtime_id: str) -> None:
    before = client.inspect_supervisor()
    old = before.data["worker"]["process_run_id"]
    _stop_running_worker(client)
    stopped = client.inspect_supervisor()
    assert stopped.data is not None and stopped.data["status"] == "STOPPED"
    identifier = "external-start-" + uuid.uuid4().hex
    client.dispatch_worker_operation(identifier, "START", process_run_id=None, deployment_generation=None, selected_runtime_id=stopped.data["selected_runtime_id"], runtime_id=runtime_id)

    def replacement():
        inspected = client.inspect_supervisor()
        worker = inspected.data.get("worker") if inspected.data else None
        return worker if inspected.result_code == "OK" and isinstance(worker, dict) and worker.get("process_run_id") not in (None, old) else None

    _wait_for(replacement, timeout=90)
