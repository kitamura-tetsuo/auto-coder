"""Signed HTTP boundary against the real PID-owned Unix-socket worker."""

from __future__ import annotations

import base64
import http.client
import json
import os
import socket
import sys
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from auto_coder.management.worker_network_client import WorkerEndpointConfig, WorkerNetworkClient, configured_worker_bridge_environment
from auto_coder.worker_network_protocol import load_machine_secret, request_mac
from auto_coder.worker_supervisor import SupervisorConfig, WorkerSupervisor
from auto_coder.worker_supervisor_bridge import SupervisorBridgeConfig, SupervisorBridgeServer
from auto_coder.worker_versions import InstalledProvenance, PreparedEnvironment, ReadyReceipt


def _supervisor(tmp_path: Path, channel: str = "beta") -> WorkerSupervisor:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    fixture = Path(__file__).parent / "fixtures" / "supervisor_bridge_worker.py"
    return WorkerSupervisor(
        SupervisorConfig(
            f"{channel}-worker",
            channel,
            f"fixture/{channel}",
            tmp_path,
            workspace,
            Path(sys.executable),
            tmp_path / "versions",
            (sys.executable, str(fixture)),
            False,
            source_repository=workspace / "source",
        )
    )


def _bridge(supervisor: WorkerSupervisor, key: bytes) -> tuple[SupervisorBridgeServer, WorkerNetworkClient]:
    # The loopback address is used only in this disposable local test fixture;
    # production environment configuration requires a private Docker IP.
    config = supervisor.config
    listener = SupervisorBridgeServer(SupervisorBridgeConfig(config.instance_id, config.channel, config.repository, "127.0.0.1", 0, key), supervisor)
    port = listener._http.server_port
    client = WorkerNetworkClient(WorkerEndpointConfig(config.instance_id, config.channel, config.repository, "127.0.0.1", port, key))
    listener.start()
    return listener, client


def _raw(client: WorkerNetworkClient, path: str, *, timestamp: str | None = None, body: bytes = b"", headers: dict[str, str] | None = None) -> tuple[int, dict[str, object]]:
    nonce, request_id = uuid.uuid4().hex, uuid.uuid4().hex
    generation = client.supervisor_generation or "-"
    timestamp = timestamp or str(int(time.time()))
    signed = {
        "X-AC-Version": "1",
        "X-AC-Instance": client.config.instance_id,
        "X-AC-Supervisor-Generation": generation,
        "X-AC-Timestamp": timestamp,
        "X-AC-Nonce": nonce,
        "X-AC-Request-Id": request_id,
        "X-AC-Signature": request_mac(client.config.secret, method="GET", target=path, instance=client.config.instance_id, generation=generation, timestamp=timestamp, nonce=nonce, request_id=request_id, body=body),
    }
    signed.update(headers or {})
    conn = http.client.HTTPConnection(client.config.address, client.config.port, timeout=5)
    try:
        conn.request("GET", path, headers=signed)
        response = conn.getresponse()
        return response.status, json.loads(response.read())
    finally:
        conn.close()


def test_bridge_configuration_fails_closed_and_has_distinct_machine_key(tmp_path: Path) -> None:
    supervisor = _supervisor(tmp_path)
    key_path = tmp_path / "key"
    key_path.write_bytes(base64.b64encode(os.urandom(48)) + b"\n")
    key = load_machine_secret(key_path)
    assert len(key) == 48
    config = supervisor.config
    assert SupervisorBridgeConfig.from_supervisor(supervisor) is None
    for address in ("0.0.0.0", "127.0.0.1", "8.8.8.8", "169.254.1.2"):
        supervisor.config = replace(config, bridge_bind_address=address, bridge_port=8432, bridge_secret_file=key_path)
        with pytest.raises(ValueError):
            SupervisorBridgeConfig.from_supervisor(supervisor)
    supervisor.config = replace(config, bridge_bind_address="10.31.5.10", bridge_port=8432, bridge_secret_file=key_path)
    result = SupervisorBridgeConfig.from_supervisor(supervisor)
    assert result is not None and result.secret == key and result.instance_id == "beta-worker"
    supervisor.config = replace(config, bridge_bind_address="10.31.5.10")
    with pytest.raises(ValueError):
        SupervisorBridgeConfig.from_supervisor(supervisor)


def test_manager_requires_two_distinct_configured_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for channel, ip in (("RELEASE", "10.31.2.11"), ("BETA", "10.31.2.12")):
        path = tmp_path / f"{channel}.secret"
        path.write_bytes(base64.b64encode(os.urandom(48)))
        prefix = f"AUTO_CODER_{channel}_"
        for suffix, value in {
            "SUPERVISOR_INSTANCE_ID": channel.lower() + "-worker",
            "REPOSITORY": "fixture/" + channel.lower(),
            "BRIDGE_ADDRESS": ip,
            "BRIDGE_PORT": "8843",
            "BRIDGE_SECRET_FILE": str(path),
        }.items():
            monkeypatch.setenv(prefix + suffix, value)
    configured = configured_worker_bridge_environment()
    assert len(configured) == 10
    assert WorkerEndpointConfig.environment("beta").instance_id == "beta-worker"
    monkeypatch.setenv("AUTO_CODER_BETA_BRIDGE_SECRET_FILE", configured["AUTO_CODER_RELEASE_BRIDGE_SECRET_FILE"])
    with pytest.raises(ValueError, match="distinct"):
        configured_worker_bridge_environment()


def test_real_http_signature_replay_identity_and_wrong_generation(tmp_path: Path, _use_real_commands: None, _use_real_sleep: None) -> None:
    supervisor = _supervisor(tmp_path)
    key = os.urandom(48)
    listener, client = _bridge(supervisor, key)
    try:
        result = client.inspect_supervisor()
        assert result.result_code == "OK"
        assert result.data is not None and result.data["status"] == "UNKNOWN"
        assert client.supervisor_generation == supervisor.incarnation
        assert client.observe_worker().result_code == "UNAVAILABLE"
        assert _raw(client, "/v1/supervisor")[0] == 200
        assert _raw(client, "/v1/supervisor", timestamp=str(int(time.time()) - 1000))[0] == 401

        wrong = WorkerNetworkClient(WorkerEndpointConfig("release-worker", "release", "fixture/release", "127.0.0.1", client.config.port, key))
        assert wrong.inspect_supervisor().result_code == "UNKNOWN"
        assert WorkerNetworkClient(WorkerEndpointConfig("beta-worker", "beta", "fixture/beta", "127.0.0.1", client.config.port, os.urandom(48))).inspect_supervisor().result_code == "UNKNOWN"

        nonce, request_id = uuid.uuid4().hex, uuid.uuid4().hex
        timestamp = str(int(time.time()))
        headers = {
            "X-AC-Version": "1",
            "X-AC-Instance": "beta-worker",
            "X-AC-Supervisor-Generation": supervisor.incarnation,
            "X-AC-Timestamp": timestamp,
            "X-AC-Nonce": nonce,
            "X-AC-Request-Id": request_id,
            "X-AC-Signature": request_mac(key, method="GET", target="/v1/supervisor", instance="beta-worker", generation=supervisor.incarnation, timestamp=timestamp, nonce=nonce, request_id=request_id, body=b""),
        }
        statuses = []
        for _ in range(2):
            connection = http.client.HTTPConnection("127.0.0.1", client.config.port, timeout=5)
            try:
                connection.request("GET", "/v1/supervisor", headers=headers)
                response = connection.getresponse()
                statuses.append(response.status)
                response.read()
            finally:
                connection.close()
        assert statuses == [200, 401]
        client.supervisor_generation = "stale-supervisor"
        assert client._call("GET", "/v1/worker-operations/nonexistent").result_code == "UNKNOWN"
        assert client.inspect_supervisor().result_code == "OK"
    finally:
        listener.close()


def test_worker_lifecycle_live_uds_loss_recovery_and_stopped_observation(tmp_path: Path, _use_real_commands: None, _use_real_sleep: None) -> None:
    supervisor = _supervisor(tmp_path)
    listener, client = _bridge(supervisor, os.urandom(48))
    worker_pid = None
    try:
        client.inspect_supervisor()
        started = supervisor.apply({"operation_id": "start-worker", "operation": "START"})
        assert started["status"] == "RUNNING"
        worker_pid = started["worker"]["pid"]
        observation = None
        for _ in range(100):
            result = client.observe_worker("nonce-bridge-acceptance")
            if result.result_code == "OK":
                observation = result.data
                break
            threading.Event().wait(0.05)
        assert observation is not None
        seen = observation["observation"]
        assert seen["nonce"] == "nonce-bridge-acceptance"
        assert seen["process_run_id"]
        assert seen["deployment_generation"] == started["worker"]["deployment_generation"]
        assert observation["binding_status"] == "UNAVAILABLE"  # Bootstrap has no staged READY.
        assert observation["runtime_binding"] is None

        fresh = client.inspect_supervisor()
        assert fresh.data is not None and fresh.data["status"] == "RUNNING"
        assert "interpreter_path" not in json.dumps(fresh.data)
        assert "environment_path" not in json.dumps(fresh.data)
        client.worker_process_run_id = "stale-process-run"
        assert client.diagnostics("status").result_code == "CONFLICT"
        client.inspect_supervisor()

        generation = seen["deployment_generation"]
        run_id = seen["process_run_id"]
        assert client.dispatch_worker_operation("bad-stop", "REQUEST_GRACEFUL_STOP", process_run_id=run_id, deployment_generation="stale-generation", selected_runtime_id="bootstrap").result_code == "CONFLICT"
        accepted = client.dispatch_worker_operation("held-stop", "REQUEST_GRACEFUL_STOP", process_run_id=run_id, deployment_generation=generation, selected_runtime_id="bootstrap")
        assert accepted.result_code == "DRAINING"
        assert (tmp_path / "fixture-draining").exists() or client.read_worker_operation("held-stop").result_code == "DRAINING"
        assert client.read_worker_operation("held-stop").result_code == "DRAINING"
        assert client.dispatch_worker_operation("held-stop", "START", process_run_id=run_id, deployment_generation=generation, selected_runtime_id="bootstrap").result_code == "CONFLICT"

        (tmp_path / "fixture-release").write_text("release")
        for _ in range(100):
            if client.read_worker_operation("held-stop").result_code == "STOPPED":
                break
            threading.Event().wait(0.05)
        else:
            pytest.fail("real child did not gracefully finish")
        state = client.inspect_supervisor()
        assert state.data is not None and state.data["status"] == "STOPPED"
        assert client.observe_worker().result_code == "UNAVAILABLE"
        assert client.read_worker_operation("held-stop").result_code == "STOPPED"
        assert client.read_worker_operation("unknown-operation").result_code == "NOT_FOUND"
    finally:
        if worker_pid is not None and supervisor.child is not None and supervisor.child.poll() is None:
            supervisor.child.terminate()
            supervisor.child.wait(timeout=5)
        listener.close()


def test_async_stage_operation_has_one_intent_and_only_producer_ready_evidence(tmp_path: Path, _use_real_commands: None, _use_real_sleep: None) -> None:
    supervisor = _supervisor(tmp_path)
    listener, client = _bridge(supervisor, os.urandom(48))
    source_sha = "a" * 40
    entered, release = threading.Event(), threading.Event()
    receipt = ReadyReceipt(
        schema_version=1,
        source_sha=source_sha,
        resolved_commit=source_sha,
        environment_path="/private/installed/venv",
        interpreter_path="/private/installed/venv/bin/python",
        distribution_version="1.0",
        lock_sha256="b" * 64,
        attempt_id="prepared-attempt",
        runtime_profile="python-3.12",
        installed_runtime_id="c" * 32,
        content_identity="d" * 64,
        installed_provenance=InstalledProvenance(source_sha, "1.0"),
        installed_at="2026-10-27T00:00:00Z",
    )

    class ControlledPreparer:
        def prepare(self, intent):
            assert intent.source_sha == source_sha
            assert entered.set() is None
            assert release.wait(timeout=5)
            return PreparedEnvironment(replace(receipt, attempt_id=intent.attempt_id))

    try:
        client.inspect_supervisor()
        with patch.object(supervisor, "_version_preparer", return_value=ControlledPreparer()):
            accepted = client.prepare_version("prepare-1", source_sha)
            assert accepted.result_code == "IN_PROGRESS"
            assert entered.wait(timeout=5)
            assert client.read_preparation("prepare-1").result_code == "IN_PROGRESS"
            assert client.prepare_version("prepare-1", "e" * 40).result_code == "CONFLICT"
            assert client.prepare_version("prepare-2", source_sha).result_code == "CONFLICT"
            assert client.prepare_version("prepare-3", "branch/main").result_code == "UNSUPPORTED"
            assert client._call("POST", "/v1/worker-version-preparations", {"operation_id": "bad-fields", "source_sha": source_sha, "command": "anything"}).result_code == "UNSUPPORTED"
            release.set()
            for _ in range(100):
                ready = client.read_preparation("prepare-1")
                if ready.result_code == "READY":
                    break
                threading.Event().wait(0.05)
            else:
                pytest.fail("no terminal stage receipt")
            assert ready.data is not None
            assert ready.data["source_sha"] == source_sha
            assert ready.data["content_identity"] == receipt.content_identity
            assert ready.data["lock_sha256"] == receipt.lock_sha256
            assert ready.data["operation_id"] == "prepare-1"
            assert "/private/" not in json.dumps(ready.data)
            assert client.prepare_version("prepare-1", source_sha).result_code == "READY"
            assert WorkerSupervisor(supervisor.config).read_preparation("prepare-1")["status"] == "READY"
    finally:
        release.set()
        listener.close()
