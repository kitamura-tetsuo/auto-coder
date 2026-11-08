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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from auto_coder.management.worker_network_client import WorkerEndpointConfig, WorkerNetworkClient, configured_worker_bridge_environment, configured_worker_registry
from auto_coder.worker_network_protocol import MAX_RESPONSE_BYTES, load_machine_secret, request_mac
from auto_coder.worker_supervisor import SupervisorConfig, WorkerSupervisor
from auto_coder.worker_supervisor_bridge import SupervisorBridgeConfig, SupervisorBridgeServer, _BridgeHandler
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


def _bridge(supervisor: WorkerSupervisor, key: bytes, *, port: int = 0) -> tuple[SupervisorBridgeServer, WorkerNetworkClient]:
    # The loopback address is used only in this disposable local test fixture;
    # production environment configuration requires a private Docker IP.
    config = supervisor.config
    listener = SupervisorBridgeServer(SupervisorBridgeConfig(config.instance_id, config.channel, config.repository, "127.0.0.1", port, key), supervisor)
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


def test_manager_requires_distinct_registry_target_identities_and_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entries = []
    for name, channel, ip in (("release-worker", "release", "10.31.2.11"), ("beta-worker", "beta", "10.31.2.12"), ("beta-agent-table-worker", "beta", "10.31.2.13")):
        path = tmp_path / f"{name}.secret"
        path.write_bytes(base64.b64encode(os.urandom(48)))
        entries.append({"target_id": name, "supervisor_instance_id": name, "channel": channel, "repository": "fixture/" + name, "bridge_address": ip, "bridge_port": 8843, "bridge_secret_file": str(path)})

    def configure(values: list[dict[str, object]]) -> None:
        monkeypatch.setenv("AUTO_CODER_WORKER_REGISTRY", json.dumps({"workers": values}))

    configure(entries)
    assert len(configured_worker_bridge_environment()) == 1
    assert set(configured_worker_registry()) == {"release-worker", "beta-worker", "beta-agent-table-worker"}
    assert WorkerEndpointConfig.environment("beta-agent-table-worker").channel == "beta"
    assert len({entry.binding_digest for entry in configured_worker_registry().values()}) == 3
    with pytest.raises(KeyError):
        WorkerEndpointConfig.environment("unknown")

    for field, replacement in (
        ("target_id", entries[0]["target_id"]),
        ("supervisor_instance_id", entries[0]["supervisor_instance_id"]),
        ("repository", entries[0]["repository"]),
        ("bridge_address", entries[0]["bridge_address"]),
        ("bridge_secret_file", entries[0]["bridge_secret_file"]),
        ("channel", "staging"),
        ("bridge_port", 123),
    ):
        altered = [dict(entry) for entry in entries]
        altered[1][field] = replacement
        configure(altered)
        with pytest.raises(ValueError):
            configured_worker_bridge_environment()

    for changed in (dict(entries[1], bridge_address="0.0.0.0"), dict(entries[1], repository="bad\nowner/repo"), {key: value for key, value in entries[1].items() if key != "channel"}):
        configure([entries[0], changed, entries[2]])
        with pytest.raises(ValueError):
            configured_worker_bridge_environment()

    configure(entries)
    monkeypatch.setenv("AUTO_CODER_BETA_BRIDGE_ADDRESS", "10.31.2.12")
    with pytest.raises(ValueError, match="legacy"):
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
        assert _raw(client, "/v1/supervisor", headers={"X-AC-Version": "2"})[0] == 401
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


def test_authenticated_log_envelope_budget_keeps_real_worker_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands: None, _use_real_sleep: None) -> None:
    monkeypatch.setenv("AUTO_CODER_TEST_SEEDED_LOGS", "500")
    monkeypatch.setenv("AUTO_CODER_TEST_OMITTED_LOGS", "3")
    encoded_responses: list[bytes] = []
    original_read = http.client.HTTPResponse.read

    def record_encoded_response(response: http.client.HTTPResponse, amount: int | None = None) -> bytes:
        raw = original_read(response, amount)
        encoded_responses.append(raw)
        return raw

    monkeypatch.setattr(http.client.HTTPResponse, "read", record_encoded_response)
    supervisor = _supervisor(tmp_path)
    supervisor.config = replace(
        supervisor.config,
        instance_id="i" * 200,
        repository="owner/" + "r" * 194,
    )
    listener, client = _bridge(supervisor, os.urandom(48))
    worker_pid = None
    try:
        client.inspect_supervisor()
        started = supervisor.apply({"operation_id": "start-budget-worker", "operation": "START"})
        worker_pid = started["worker"]["pid"]
        result = None
        for _ in range(100):
            candidate = client.diagnostics("logs", {"limit": "500"})
            if candidate.result_code == "OK":
                result = candidate
                break
            threading.Event().wait(0.05)
        assert result is not None and result.http_status == 200 and result.data is not None
        assert result.data["result"] == "events"
        assert result.data["response"]["truncated"] is True
        assert result.data["events"][-1]["sequence"] == 500
        assert "fixture-secret" not in json.dumps(result.data)

        # A one-record page is not over-fitted merely because the private
        # transport reserves its envelope; it is the same newest source record.
        small = client.diagnostics("logs", {"limit": "1"})
        assert small.result_code == "OK" and small.data is not None
        assert small.data["events"] == [result.data["events"][-1]]

        run_id = result.data["process_run_id"]
        pages = []
        params = {"after_sequence": "0", "process_run_id": run_id, "limit": "500"}
        while True:
            page_result = client.diagnostics("logs", params)
            assert page_result.result_code == "OK" and page_result.data is not None
            page = page_result.data
            pages.append(page)
            if not page["has_more"]:
                break
            params = {"cursor": page["next_cursor"], "limit": "500"}
        delivered = [event["sequence"] for page in pages for event in page["events"]]
        assert delivered == list(range(1, 501))
        assert pages[-1]["omissions"] == [{"reason": "identity_unrepresentable", "count": 3, "first_sequence": 501, "last_sequence": 503}]
        assert pages[-1]["next_after_sequence"] == 503 and pages[-1]["has_more"] is False
        for previous, following in zip(pages, pages[1:]):
            assert previous["next_after_sequence"] == previous["events"][-1]["sequence"]
            assert following["after_sequence"] == previous["next_after_sequence"]

        omitted_result = client.diagnostics("logs", {"after_sequence": "500", "process_run_id": run_id, "limit": "1"})
        assert omitted_result.result_code == "OK" and omitted_result.data is not None
        assert omitted_result.data["result"] == "omitted_only" and omitted_result.data["events"] == []
        assert omitted_result.data["omissions"] == pages[-1]["omissions"]
        assert omitted_result.data["next_after_sequence"] == 503 and omitted_result.data["has_more"] is False

        no_match = client.diagnostics("logs", {"item_type": "pr", "item_number": "999"})
        assert no_match.result_code == "OK" and no_match.data is not None
        assert no_match.data["result"] == "no_retained_match" and no_match.data["events"] == []
        changed = client.diagnostics("logs", {"after_sequence": "0", "process_run_id": "stale-run"})
        assert changed.http_status == 409 and changed.data is not None
        assert changed.data["error"]["code"] == "process_run_changed"

        # Reproduce the old source-only budgeting: the otherwise valid page is
        # replaced by UNAVAILABLE once its unreserved envelope crosses the cap.
        with patch.object(_BridgeHandler, "_response_data_budget", return_value=MAX_RESPONSE_BYTES):
            prior_behavior = client.diagnostics("logs", {"limit": "500"})
        assert prior_behavior.http_status == 503 and prior_behavior.result_code == "UNAVAILABLE"

        invalid_signature = _raw(
            client,
            "/v1/diagnostics/logs?limit=1",
            headers={
                "X-AC-Worker-Process-Run-Id": client.worker_process_run_id or "",
                "X-AC-Worker-Deployment-Generation": client.worker_deployment_generation or "",
            },
        )
        assert invalid_signature[0] == 401

        private_envelopes = []
        for raw in encoded_responses:
            try:
                envelope = json.loads(raw)
            except (TypeError, ValueError, UnicodeDecodeError):
                continue
            if isinstance(envelope, dict) and envelope.get("schema_version") == 1 and envelope.get("instance_id") == client.config.instance_id:
                private_envelopes.append((raw, envelope))
        successful = [(raw, envelope) for raw, envelope in private_envelopes if envelope.get("result_code") == "OK"]
        assert successful and all(len(raw) <= MAX_RESPONSE_BYTES for raw, _ in successful)
        assert any(envelope.get("data", {}).get("result") == "omitted_only" for _, envelope in successful)
    finally:
        if worker_pid is not None:
            (tmp_path / "fixture-release").write_text("release")
            try:
                os.kill(worker_pid, 15)
            except ProcessLookupError:
                pass
        listener.close()


def test_private_incremental_retention_gap_remains_typed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands: None, _use_real_sleep: None) -> None:
    monkeypatch.setenv("AUTO_CODER_TEST_SEEDED_LOGS", "4005")
    monkeypatch.setenv("AUTO_CODER_TEST_SEEDED_LOG_SIZE", "1")
    supervisor = _supervisor(tmp_path)
    listener, client = _bridge(supervisor, os.urandom(48))
    worker_pid = None
    try:
        client.inspect_supervisor()
        started = supervisor.apply({"operation_id": "start-gap-worker", "operation": "START"})
        worker_pid = started["worker"]["pid"]
        recent = None
        for _ in range(100):
            candidate = client.diagnostics("logs", {"limit": "1"})
            if candidate.result_code == "OK":
                recent = candidate
                break
            threading.Event().wait(0.05)
        assert recent is not None and recent.data is not None
        gap = client.diagnostics("logs", {"after_sequence": "0", "process_run_id": recent.data["process_run_id"]})
        assert gap.http_status == 409 and gap.result_code == "CONFLICT" and gap.data is not None
        assert gap.data["error"]["code"] == "retention_gap"
        assert "events" not in gap.data and "next_cursor" not in gap.data
    finally:
        if worker_pid is not None:
            (tmp_path / "fixture-release").write_text("release")
            try:
                os.kill(worker_pid, 15)
            except ProcessLookupError:
                pass
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
        escaped_nonce = client.observe_worker("nonce with & separator")
        assert escaped_nonce.result_code == "OK"
        assert escaped_nonce.data is not None
        assert escaped_nonce.data["observation"]["nonce"] == "nonce with & separator"
        assert observation["binding_status"] == "UNAVAILABLE"  # Bootstrap has no staged READY.
        assert observation["runtime_binding"] is None

        fresh = client.inspect_supervisor()
        assert fresh.data is not None and fresh.data["status"] == "RUNNING"
        assert "interpreter_path" not in json.dumps(fresh.data)
        assert "environment_path" not in json.dumps(fresh.data)
        # The upstream reevaluation UDS producer accepts namespaced IDs; its
        # test engine has no reevaluation sink, so forwarding yields 503 rather
        # than a transport-level 422 for a valid existing protocol identity.
        forwarded = client.accept_reevaluation(
            "management:2602:1",
            process_run_id=seen["process_run_id"],
            deployment_generation=seen["deployment_generation"],
            target_type="issue",
            number=2611,
        )
        assert forwarded.result_code == "UNAVAILABLE"
        assert client.read_reevaluation("management:2602:1").result_code == "UNAVAILABLE"
        with pytest.raises(ValueError, match="reevaluation operation"):
            client.read_reevaluation("management/invalid")
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


def test_observation_keeps_signed_worker_generation_across_authentication_dispatch_race(tmp_path: Path, _use_real_commands: None, _use_real_sleep: None) -> None:
    supervisor = _supervisor(tmp_path)
    listener, client = _bridge(supervisor, os.urandom(48))
    release = threading.Event()
    entered = threading.Event()
    original_dispatch = _BridgeHandler._dispatch
    try:
        started = supervisor.apply({"operation_id": "race-first-start", "operation": "START"})
        assert started["status"] == "RUNNING"
        first = None
        for _ in range(100):
            inspected = client.inspect_supervisor()
            if inspected.data is not None and isinstance(inspected.data.get("worker"), dict) and inspected.data["worker"].get("process_run_id"):
                first = inspected.data["worker"]
                break
            time.sleep(0.02)
        assert first is not None

        def pause_after_authentication(handler: _BridgeHandler, method: str, body: bytes):
            if handler.path.startswith("/v1/worker-observation?"):
                entered.set()
                assert release.wait(timeout=8), "observation dispatch barrier was not released"
            return original_dispatch(handler, method, body)

        with patch.object(_BridgeHandler, "_dispatch", pause_after_authentication), ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(client.observe_worker, "signed-first-run")
            assert entered.wait(timeout=5)
            try:
                stop = supervisor.apply({"operation_id": "race-first-stop", "operation": "REQUEST_GRACEFUL_STOP", "expected_process_run_id": first["process_run_id"], "expected_generation": first["deployment_generation"]})
                assert stop["status"] in {"DRAINING", "STOPPED"}
                (tmp_path / "fixture-release").write_text("release")
                deadline = time.monotonic() + 3
                while supervisor.inspect()["status"] != "STOPPED" and time.monotonic() < deadline:
                    time.sleep(0.02)
                assert supervisor.inspect()["status"] == "STOPPED"
                assert supervisor.read_operation("race-first-stop")["status"] == "STOPPED"
                successor = supervisor.apply({"operation_id": "race-second-start", "operation": "START"})
                assert successor["status"] == "RUNNING"
                assert successor["worker"]["deployment_generation"] != first["deployment_generation"]
                assert supervisor.inspect()["worker"]["deployment_generation"] != first["deployment_generation"]
            finally:
                release.set()
            stale = pending.result(timeout=5)
            assert stale.result_code == "CONFLICT"
            assert stale.http_status == 409
        for _ in range(100):
            assert client.inspect_supervisor().result_code == "OK"
            if client.observe_worker("new-run-observation").result_code == "OK":
                break
            time.sleep(0.02)
        else:
            pytest.fail("new worker did not expose its own real observation")
    finally:
        release.set()
        if supervisor.child is not None and supervisor.child.poll() is None:
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
