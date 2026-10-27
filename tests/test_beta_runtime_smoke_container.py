"""Real installed beta workers produce durable local smoke evidence.

The external test orchestrator owns Docker and fault injection. The production
smoke producer has only the existing journal, a signed read client, and a
read-only Git/Actions transport. No observation or verdict is synthesized here.
"""

from __future__ import annotations

import base64
import json
import os
import random
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from auto_coder.beta_source_selection import BetaSourceSelector, MutationCertainty
from auto_coder.management.action_scopes import DEPLOY_SCOPE, READ_SCOPE
from auto_coder.management.app import create_management_app
from auto_coder.management.beta_runtime_smoke import BetaRuntimeSmokeProducer, SmokeProfile
from auto_coder.management.oauth_config import ManagementSettings, OAuthClient, OAuthSettings
from auto_coder.management.oauth_grants import OAuthGrantService
from auto_coder.management.operation_executor import ManagementOperationDispatcher, OperationHandler
from auto_coder.management.operation_store import ManagementOperationStore
from auto_coder.management.operations import ActionParameter, AdmissionStatus, EffectReceipt, ManagedTarget, MutationStatus, OperationIntent, OperationPhase
from auto_coder.management.source_evidence import BetaEvidenceReader
from auto_coder.management.worker_lifecycle import LIFECYCLE_ACTIONS, WorkerLifecycle
from auto_coder.management.worker_network_client import WorkerEndpointConfig, WorkerNetworkClient
from auto_coder.tested_source_refs import serialize_record
from auto_coder.util.gh_cache import GitDataResponse, GitHubGitDataClient
from tests.test_management_beta_deploy_container import _argument, _await_stage, _result
from tests.test_management_mcp import _issue, real_boundary
from tests.test_management_runtime_mcp import _call
from tests.test_management_worker_lifecycle import GitEvidenceBoundary
from tests.test_supervisor_container_integration import _container_facts, _docker, _image, _inspect_worker, _wait_for

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
        subprocess.run(["git", "clone", "--bare", str(source), str(root)], check=True, capture_output=True, text=True)
        self.read_only_mutations: list[tuple[str, str]] = []
        self.fail_reads = False

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
            return GitDataResponse(201)
        if method == "PATCH" and path.startswith("git/refs/tags/") and payload:
            ref = "refs/tags/" + path.removeprefix("git/refs/tags/")
            self._git("update-ref", ref, str(payload["sha"]))
            return GitDataResponse(200)
        return super().request(method, path, payload)


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


def _accepted(journal: ManagementOperationStore, client: WorkerNetworkClient, deployed: str, source: str, key: str):
    previous = journal.get(deployed).record
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

    return _wait_for(completed, timeout=timeout)


def _host_path(root: Path, path: str) -> Path:
    return root / Path(path).relative_to("/runtime")


def _fresh_process(configuration: Path, operation_id: str, mode: str = "advance") -> dict[str, object]:
    result = subprocess.run([sys.executable, str(Path(__file__).parent / "fixtures" / "beta_smoke_reconstruct.py"), str(configuration), operation_id, mode], check=True, capture_output=True, text=True, timeout=45)
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.timeout(1800)
def test_local_smoke_uses_installed_worker_real_progress_and_original_journal(real_boundary, tmp_path: Path, _use_real_commands: None, _use_real_sleep: None) -> None:
    """REQ-008: authoritative worker evidence remains local and reconstructable."""
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
        "-e",
        "HOME=/runtime/home",
        "--entrypoint",
        "python",
        image,
        "/fixtures/beta_smoke_stage.py",
        *revisions,
        timeout=1200,
    )
    installations = json.loads(staged.stdout.strip().splitlines()[-1])
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
    _docker("network", "create", "--driver", "bridge", "--internal", "--subnet", f"10.250.{octet}.0/24", network)
    try:
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
            oauth = OAuthSettings(base.oauth.database_path, origin, base.oauth.firebase_project_id, (OAuthClient("alice-client", (origin + "/alice-callback",), allowed_scopes=(READ_SCOPE, DEPLOY_SCOPE)),), enabled_scopes=(READ_SCOPE, DEPLOY_SCOPE))
            policy = tmp_path / "operator.yaml"
            policy.write_text("profile: beta\ntargets:\n  beta-worker:\n    channel: beta\n    repositories: [fixture/beta]\n", encoding="utf-8")
            settings = ManagementSettings(tmp_path, oauth, port=base.port, operating_policy_path=policy, operation_state_root=tmp_path / "journal", source_repository="owner/repo")
            grants = OAuthGrantService(oauth, identity, clock=lambda: state["now"])
            app = create_management_app(settings, grants, worker_clients={"beta": client})
            _configure_lifecycle(app, reader)
            configuration = tmp_path / "reconstruction.json"
            configuration.write_text(json.dumps({"journal_root": str(tmp_path / "journal"), "address": address, "secret": base64.b64encode(secret).decode(), "source_api": api_url}), encoding="utf-8")
            with TestClient(app, base_url=origin) as http:
                bearer = _issue(http, settings, signed, "alice", "alice-client", READ_SCOPE + " " + DEPLOY_SCOPE)["access_token"]
                deployed = _call(http, bearer, "auto_coder_deploy_beta_source", _argument(client, revisions[0], "smoke-deploy-a"))
                deployment_id = str(deployed["operation_id"])
                _await_stage(app, deployment_id, "SUCCEEDED", timeout=210)
                journal = app.state.management_runtime.journal
                _exercise_smoke(journal, client, reader, selecting, remote, app, http, bearer, runtime, installations, revisions, deployment_id, configuration)
            assert remote.read_only_mutations == []
    finally:
        log_root = Path(os.environ.get("GITHUB_WORKSPACE", str(Path(__file__).parents[1]))) / "supervisor-integration-logs"
        log_root.mkdir(parents=True, exist_ok=True)
        for name, destination in ((worker_name, "local-smoke-worker.log"), (github_name, "local-smoke-github.log")):
            result = _docker("logs", name, check=False)
            (log_root / destination).write_text(result.stdout + result.stderr, encoding="utf-8")
            _docker("rm", "-f", name, check=False)
        _docker("network", "rm", network, check=False)


def _exercise_smoke(journal, client, reader, selecting, remote, app, http, bearer, runtime, installations, revisions, deployed_id, reconstruction) -> None:
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

    first = accept("local-pass-original")
    first_capture = captured(first)
    assert first_capture["capture"]["source"]["proof_run_id"] == 101
    assert first_capture["capture"]["source"]["selected_run_id"] == 11
    assert first_capture["capture"]["worker"]["ready_content_identity"] == ready_a["content_identity"]
    reconstructed = _fresh_process(reconstruction, first.operation_id)
    assert reconstructed["state"] in {"IN_PROGRESS", "COMPLETE"}
    after_restart = journal.smoke_snapshot(first.operation_id)[1]
    assert after_restart["window"] == first_capture["window"]
    assert after_restart["profile"] == first_capture["profile"]
    assert after_restart["binding"] == first_capture["binding"]
    assert after_restart["capture"] == first_capture["capture"]
    passed = _finish(producer, first.operation_id)
    document = result(passed, "PASS")
    qualifying = [sample for sample in document["samples"] if sample["qualifying"]]
    assert len(qualifying) >= 2
    assert all(left["heartbeat"]["sequence"] < right["heartbeat"]["sequence"] for left, right in zip(qualifying, qualifying[1:]))
    assert all(sample["runtime_binding"]["process_run_id"] == first.intent.expected_incarnation for sample in qualifying)
    assert all(sample["artifact"]["source_revision"]["origin"] == "build_embedded" for sample in qualifying)
    assert all(sample["artifact"]["distribution_version"]["origin"] == "installed_distribution_metadata" for sample in qualifying)
    assert all(sample["runtime_binding"]["lock_sha256"] == ready_a["lock_sha256"] for sample in qualifying)
    assert journal.get(first.operation_id).record.phase is OperationPhase.ACCEPTED
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
    _release_local_lane(journal, first.operation_id)

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
    stuck = accept("stuck-real-heartbeat")
    short = BetaRuntimeSmokeProducer(journal, client, reader, SmokeProfile(total_seconds=8, interval_seconds=1))
    assert short.advance(stuck.operation_id).state.value == "IN_PROGRESS"
    diagnostics = []
    until = time.monotonic() + 15
    while time.monotonic() < until:
        diagnostics.append(client.diagnostics("status").result_code)
        observed = short.advance(stuck.operation_id)
        if observed.state.value == "COMPLETE":
            break
        time.sleep(0.3)
    blocked = result(observed, "BLOCKED")
    assert diagnostics and all(code == "OK" for code in diagnostics)
    assert blocked["reason"] == "original_deadline_without_progress"
    assert len(blocked["samples"]) >= 2
    assert {sample["heartbeat"]["sequence"] for sample in blocked["samples"]} == {original_beat["sequence"]}
    assert len({sample["nonce"] for sample in blocked["samples"]}) == len(blocked["samples"])
    _release_local_lane(journal, stuck.operation_id)

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
    _release_local_lane(journal, operation.operation_id)

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

    remote.executions[(12, 1)] = (revisions[0], "2026-10-02T02:00:00Z")
    assert selecting.advance("r78", revisions[0]).certainty is MutationCertainty.CONFIRMED
    mismatch = accept("selection-deployment-mismatch")
    assert result(producer.advance(mismatch.operation_id), "BLOCKED")["reason"] == "deployment_selection_or_candidate_mismatch"
    _release_local_lane(journal, mismatch.operation_id)

    latest = _call(http, bearer, "auto_coder_deploy_beta_source", _argument(client, revisions[0], "smoke-deploy-new-run"))
    new_run_deployment = str(latest["operation_id"])
    _await_stage(app, new_run_deployment, "SUCCEEDED", timeout=210)
    later = accept("later-successful-same-sha", deployment=new_run_deployment)
    later_capture = captured(later)
    assert later_capture["capture"]["source"]["tested_source_oid"] == first_capture["capture"]["source"]["tested_source_oid"]
    assert later_capture["capture"]["source"]["proof_run_id"] == 101
    assert (later_capture["capture"]["source"]["selected_run_id"], later_capture["capture"]["source"]["selected_run_attempt"]) == (12, 1)
    assert result(_finish(producer, later.operation_id), "PASS")["capture"] == later_capture["capture"]
    _release_local_lane(journal, later.operation_id)

    remote.executions[(12, 2)] = (revisions[0], "2026-10-03T03:00:00Z")
    assert selecting.advance("r79", revisions[0]).certainty is MutationCertainty.CONFIRMED
    retry = _call(http, bearer, "auto_coder_deploy_beta_source", _argument(client, revisions[0], "smoke-deploy-retry"))
    retry_deployment = str(retry["operation_id"])
    _await_stage(app, retry_deployment, "SUCCEEDED", timeout=210)
    accepted_retry = accept("successful-retry-original-proof", deployment=retry_deployment)
    retry_capture = captured(accepted_retry)
    assert (retry_capture["capture"]["source"]["selected_run_id"], retry_capture["capture"]["source"]["selected_run_attempt"]) == (12, 2)
    assert retry_capture["capture"]["source"]["tested_source_oid"] == first_capture["capture"]["source"]["tested_source_oid"]
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

    # Deploy the second actual revision, then prove its own local window.
    remote.executions[(14, 1)] = (revisions[1], "2026-10-05T05:00:00Z")
    assert selecting.advance("r81", revisions[0]).certainty is MutationCertainty.CONFIRMED
    second = _call(http, bearer, "auto_coder_deploy_beta_source", _argument(client, revisions[1], "smoke-deploy-b"))
    second_deployment = str(second["operation_id"])
    _await_stage(app, second_deployment, "SUCCEEDED", timeout=210)
    second_local = accept("second-installed-revision", deployment=second_deployment, sha=revisions[1])
    second_document = result(_finish(producer, second_local.operation_id), "PASS")
    assert second_document["source_sha"] == revisions[1]
    assert second_document["capture"]["worker"]["installed_runtime_id"] == ready_b["installed_runtime_id"]
    assert second_document["capture"]["worker"]["source_revision"]["value"] != document["capture"]["worker"]["source_revision"]["value"]
    _release_local_lane(journal, second_local.operation_id)

    replacement = accept("worker-replaced-between-samples", deployment=second_deployment, sha=revisions[1])
    replacement_capture = captured(replacement)
    _replace_running_worker(client, ready_b["installed_runtime_id"])
    time.sleep(2.1)
    replaced = result(producer.advance(replacement.operation_id), "SUPERSEDED")
    assert replaced["capture"] == replacement_capture["capture"]
    assert replaced["window"] == replacement_capture["window"]
    assert len([sample for sample in replaced["samples"] if sample["qualifying"]]) == 1
    _release_local_lane(journal, replacement.operation_id)

    # A genuine lifecycle predicate violation is FAIL, not unavailable evidence.
    refreshed = _call(http, bearer, "auto_coder_deploy_beta_source", _argument(client, revisions[1], "smoke-deploy-b-refreshed"))
    refreshed_id = str(refreshed["operation_id"])
    _await_stage(app, refreshed_id, "SUCCEEDED", timeout=210)
    stopped = accept("worker-stopped-is-fail", deployment=refreshed_id, sha=revisions[1])
    captured(stopped)
    _stop_running_worker(client)
    time.sleep(2.1)
    assert result(producer.advance(stopped.operation_id), "FAIL")["reason"] == "worker_lifecycle_not_running"
    _release_local_lane(journal, stopped.operation_id)


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
