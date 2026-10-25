import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

from auto_coder.automation_engine import EngineLifecycle
from auto_coder.health_monitor import HealthMonitor, get_health_monitor
from auto_coder.invocation_admission import InvocationAdmissionGate
from auto_coder.worker_management_bridge import (
    ManagementBridgeConfigurationError,
    WorkerManagementBridge,
    configured_identity,
)


class ObservedEngine:
    def __init__(self) -> None:
        self.lifecycle = EngineLifecycle.RUNNING
        self.startup_reconciled = False
        self.startup_reconciliation_error = None
        self.daemon_serving = False
        self.invocation_gate = InvocationAdmissionGate("test-lifetime")

    def invocation_admission_snapshot(self):
        return self.invocation_gate.snapshot()


def configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    ownership = tmp_path / "ownership.json"
    ownership.write_text('{"owner/repo":"release"}')
    endpoint = runtime / "worker.sock"
    monkeypatch.setenv("AUTO_CODER_MANAGEMENT_SOCKET", str(endpoint))
    monkeypatch.setenv("AUTO_CODER_DEPLOYMENT_GENERATION", "generation-7")
    monkeypatch.setenv("AUTO_CODER_CHANNEL", "release")
    monkeypatch.setenv("AUTO_CODER_ARTIFACT_DIGEST", "sha256:declared")
    monkeypatch.setenv("AUTO_CODER_RUNTIME_ROOT", str(runtime))
    monkeypatch.setenv("AUTO_CODER_OWNERSHIP_FILE", str(ownership))
    return endpoint


def test_unset_management_socket_preserves_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTO_CODER_MANAGEMENT_SOCKET", raising=False)
    assert configured_identity("owner/repo") is None


def test_real_unix_socket_observes_same_engine_and_read_does_not_heartbeat(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    endpoint = configure(monkeypatch, tmp_path)
    engine = ObservedEngine()
    HealthMonitor.reset_instance()
    monitor = get_health_monitor()
    monitor.heartbeat("producer:real")
    before = monitor.heartbeat_observation()

    async def scenario() -> None:
        identity = configured_identity("owner/repo")
        assert identity is not None
        bridge = WorkerManagementBridge(identity, engine)
        await bridge.start()
        try:
            transport = httpx.AsyncHTTPTransport(uds=str(endpoint))
            async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
                first = await client.get("/v1/observation", params={"nonce": "first"})
                second = await client.get("/v1/observation", params={"nonce": "second"})
                invalid = await client.get("/v1/observation?nonce=a&nonce=b")
            assert first.status_code == 200
            assert second.status_code == 200
            assert invalid.status_code == 422
            payload = first.json()
            assert payload["nonce"] == "first"
            assert payload["repository"] == "owner/repo"
            assert payload["deployment_generation"] == "generation-7"
            assert payload["lifecycle"]["daemon_serving"] is False
            assert payload["heartbeat"]["sequence"] == before["sequence"]
            assert first.headers["auto-coder-deployment-generation"] == "generation-7"
            assert second.json()["heartbeat"]["sequence"] == before["sequence"]
        finally:
            await bridge.close()
        assert not endpoint.exists()

    asyncio.run(scenario())


def test_conflicting_endpoint_is_never_replaced(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    endpoint = configure(monkeypatch, tmp_path)
    endpoint.write_text("operator-owned")
    identity = configured_identity("owner/repo")
    assert identity is not None
    bridge = WorkerManagementBridge(identity, ObservedEngine())
    with pytest.raises(ManagementBridgeConfigurationError, match="already exists"):
        asyncio.run(bridge.start())
    assert endpoint.read_text() == "operator-owned"


def test_graceful_receipt_requires_stopped_closed_and_settled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    configure(monkeypatch, tmp_path)
    identity = configured_identity("owner/repo")
    assert identity is not None
    engine = ObservedEngine()
    bridge = WorkerManagementBridge(identity, engine)
    bridge.persist_graceful_terminal("too early")
    assert not bridge.receipt_path.exists()

    engine.invocation_gate.close_admission("test")
    engine.lifecycle = EngineLifecycle.STOPPED
    bridge.persist_graceful_terminal("signal SIGTERM")
    receipt = json.loads(bridge.receipt_path.read_text())
    assert receipt["lifecycle_state"] == "stopped"
    assert receipt["invocation_gate_state"] == "draining"
    assert receipt["is_graceful_ready"] is True
    assert receipt["unsettled_count"] == 0
    assert receipt["deployment_generation"] == "generation-7"
    assert os.stat(bridge.receipt_path).st_mode & 0o777 == 0o600
