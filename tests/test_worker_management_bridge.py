import asyncio
import json
import os
import threading
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

import auto_coder.worker_management_bridge as bridge_module
from auto_coder.automation_config import AutomationConfig
from auto_coder.automation_engine import AutomationEngine, EngineLifecycle
from auto_coder.build_provenance import ArtifactField, ControllerArtifactObservation
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

    async def scenario() -> None:
        identity = configured_identity("owner/repo")
        assert identity is not None
        bridge = WorkerManagementBridge(identity, engine)
        await bridge.start()
        try:
            transport = httpx.AsyncHTTPTransport(uds=str(endpoint))
            async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
                unavailable = await client.get("/v1/observation", params={"nonce": "initial"})
                monitor.heartbeat("producer:first")
                first = await client.get("/v1/observation", params={"nonce": "first"})
                second = await client.get("/v1/observation", params={"nonce": "second"})
                monitor.heartbeat("producer:second")
                advanced = await client.get("/v1/observation", params={"nonce": "advanced"})
                spaced = await client.get("/v1/observation", params={"nonce": "a b"})
                longest = await client.get("/v1/observation", params={"nonce": "x" * 128})
                invalid = await client.get("/v1/observation?nonce=a&nonce=b")
                non_ascii = await client.get("/v1/observation", params={"nonce": "café"})
            assert first.status_code == 200
            assert second.status_code == 200
            assert invalid.status_code == 422
            assert non_ascii.status_code == 422
            assert unavailable.json()["heartbeat"] == {"available": False, "sequence": None, "stage": None, "age_seconds": None}
            payload = first.json()
            assert payload["nonce"] == "first"
            assert payload["repository"] == "owner/repo"
            assert payload["deployment_generation"] == "generation-7"
            assert payload["lifecycle"]["daemon_serving"] is False
            assert payload["heartbeat"]["available"] is True
            assert payload["heartbeat"]["sequence"] == 1
            assert payload["heartbeat"]["stage"] == "producer:first"
            assert first.headers["auto-coder-deployment-generation"] == "generation-7"
            assert second.json()["heartbeat"]["sequence"] == 1
            assert advanced.json()["heartbeat"]["sequence"] == 2
            assert advanced.json()["heartbeat"]["stage"] == "producer:second"
            assert spaced.json()["nonce"] == "a b"
            assert longest.json()["nonce"] == "x" * 128
        finally:
            await bridge.close()
        assert not endpoint.exists()

    asyncio.run(scenario())


def test_conflicting_endpoint_is_never_replaced(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from auto_coder.cli_commands_main import _run_process_issues_daemon

    endpoint = configure(monkeypatch, tmp_path)
    endpoint.write_text("operator-owned")
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    with pytest.raises(ManagementBridgeConfigurationError, match="already exists"):
        asyncio.run(_run_process_issues_daemon(engine, "owner/repo", False, "127.0.0.1", 0, "critical", None, None))
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


def test_missing_process_identity_cannot_create_graceful_receipt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    configure(monkeypatch, tmp_path)
    monkeypatch.setattr(
        bridge_module,
        "observe_controller_artifact",
        lambda: ControllerArtifactObservation(process_run_id=None, process_run=ArtifactField(reason="unavailable")),
    )
    identity = configured_identity("owner/repo")
    assert identity is not None
    engine = ObservedEngine()
    engine.invocation_gate.close_admission("test")
    engine.lifecycle = EngineLifecycle.STOPPED
    bridge = WorkerManagementBridge(identity, engine)

    bridge.persist_graceful_terminal("signal SIGTERM")

    assert not bridge.receipt_path.exists()


def test_actual_daemon_uds_observes_admitted_drain_and_persists_receipt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Disconnecting daemon composition or the STOPPED hook must fail this test."""
    from auto_coder.cli_commands_main import _run_process_issues_daemon

    endpoint = configure(monkeypatch, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    admitted = engine.invocation_gate.try_admit(repository="owner/repo", target="2599", stage="provider")
    assert admitted is not None
    assert engine.request_graceful_shutdown("test signal")

    async def scenario() -> None:
        daemon = asyncio.create_task(_run_process_issues_daemon(engine, "owner/repo", False, "127.0.0.1", 0, "critical", None, None))
        transport = httpx.AsyncHTTPTransport(uds=str(endpoint))
        async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
            for _ in range(100):
                try:
                    response = await client.get("/v1/observation", params={"nonce": "during drain"})
                    break
                except httpx.ConnectError:
                    await asyncio.sleep(0.01)
            else:
                raise AssertionError("production management socket did not become reachable")
            assert response.status_code == 200
            body = response.json()
            assert body["lifecycle"]["state"] == "draining"
            assert body["lifecycle"]["daemon_serving"] is False
            assert body["invocations"]["unsettled_count"] == 1
            assert body["invocations"]["unsettled"][0]["invocation_id"] == admitted.identity.invocation_id
            assert engine.implementation_slots is not None
            monkeypatch.setattr(engine.implementation_slots, "snapshot", MagicMock(side_effect=OSError("store unavailable")))
            status = await client.get("/api/status")
            assert status.status_code == 200
            assert status.headers["auto-coder-deployment-generation"] == "generation-7"
            assert status.json()["implementation_slots"]["error_code"] == "slot_observation_unavailable"

        admitted.begin_checkpointing("result")
        assert admitted.confirm_settled("durable-checkpoint")
        await asyncio.wait_for(daemon, timeout=10)

    asyncio.run(scenario())
    receipt = json.loads(endpoint.with_name("last-terminal.json").read_text())
    assert receipt["lifecycle_state"] == "stopped"
    assert receipt["unsettled_count"] == 0
    assert receipt["process_run_id"]
    assert not endpoint.exists()

    # A later production daemon lifetime cannot relabel the immutable receipt.
    monkeypatch.setenv("AUTO_CODER_DEPLOYMENT_GENERATION", "generation-8")
    monkeypatch.setattr(
        bridge_module,
        "observe_controller_artifact",
        lambda: ControllerArtifactObservation(
            process_run_id="new-process-run",
            process_run=ArtifactField(value="new-process-run", origin="controller_process", available=True),
        ),
    )
    replacement = AutomationEngine(MagicMock(), AutomationConfig())
    assert replacement.request_graceful_shutdown("first signal")
    replacement.request_force_stop("second signal")
    asyncio.run(_run_process_issues_daemon(replacement, "owner/repo", False, "127.0.0.1", 0, "critical", None, None))
    assert json.loads(endpoint.with_name("last-terminal.json").read_text()) == receipt


def test_actual_daemon_forced_stop_never_persists_receipt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from auto_coder.cli_commands_main import _run_process_issues_daemon

    endpoint = configure(monkeypatch, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    assert engine.request_graceful_shutdown("first signal")
    engine.request_force_stop("second signal")

    asyncio.run(_run_process_issues_daemon(engine, "owner/repo", False, "127.0.0.1", 0, "critical", None, None))

    assert engine.lifecycle is EngineLifecycle.FORCED
    assert not endpoint.with_name("last-terminal.json").exists()


def test_actual_daemon_is_observable_during_startup_and_cancellation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Exercise both startup barriers without replacing start_automation."""
    from auto_coder.cli_commands_main import _run_process_issues_daemon

    endpoint = configure(monkeypatch, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    reconciliation_entered = asyncio.Event()
    reconciliation_release = asyncio.Event()
    setup_entered = threading.Event()
    setup_release = threading.Event()
    producer_cleanup_entered = asyncio.Event()
    producer_cleanup_release = asyncio.Event()

    async def reconcile(_repository: str) -> None:
        reconciliation_entered.set()
        await reconciliation_release.wait()
        engine.startup_reconciled = True

    def hold_later_setup(*_args: object, **_kwargs: object) -> None:
        setup_entered.set()
        assert setup_release.wait(5)

    async def supervised_producer(_repository: str) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            producer_cleanup_entered.set()
            await producer_cleanup_release.wait()
            raise

    monkeypatch.setattr(engine, "_perform_startup_reconciliation", reconcile)
    monkeypatch.setattr("auto_coder.codex_work_reconstruction.reconstruct_active_codex_work", hold_later_setup)
    monkeypatch.setattr(engine, "_producer_loop", supervised_producer)

    async def scenario() -> None:
        daemon = asyncio.create_task(_run_process_issues_daemon(engine, "owner/repo", False, "127.0.0.1", 0, "critical", None, None))
        await asyncio.wait_for(reconciliation_entered.wait(), timeout=3)
        transport = httpx.AsyncHTTPTransport(uds=str(endpoint))
        async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
            startup = await client.get("/v1/observation", params={"nonce": "startup"})
            assert startup.json()["lifecycle"] == {
                "state": "running",
                "startup_reconciled": False,
                "startup_reconciliation_error": None,
                "daemon_serving": False,
            }
            reconciliation_release.set()
            assert await asyncio.to_thread(setup_entered.wait, 3)
            later_setup = await client.get("/v1/observation", params={"nonce": "later setup"})
            assert later_setup.json()["lifecycle"]["startup_reconciled"] is True
            assert later_setup.json()["lifecycle"]["daemon_serving"] is False
            setup_release.set()
            for sequence in range(100):
                steady = await client.get("/v1/observation", params={"nonce": f"steady-{sequence}"})
                if steady.json()["lifecycle"]["daemon_serving"] is True:
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("real daemon never entered supervised steady state")
            daemon.cancel()
            await asyncio.wait_for(producer_cleanup_entered.wait(), timeout=3)
            cancelling = await client.get("/v1/observation", params={"nonce": "cancelling"})
            assert cancelling.json()["lifecycle"]["daemon_serving"] is False
            assert not daemon.done()
            producer_cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await daemon

    asyncio.run(scenario())
    assert not endpoint.with_name("last-terminal.json").exists()


def test_actual_daemon_unexpected_loop_exit_never_persists_receipt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from auto_coder.cli_commands_main import _run_process_issues_daemon

    endpoint = configure(monkeypatch, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())

    async def reconcile(_repository: str) -> None:
        engine.startup_reconciled = True

    async def producer_exits(_repository: str) -> None:
        return None

    monkeypatch.setattr(engine, "_perform_startup_reconciliation", reconcile)
    monkeypatch.setattr(engine, "_producer_loop", producer_exits)

    asyncio.run(_run_process_issues_daemon(engine, "owner/repo", False, "127.0.0.1", 0, "critical", None, None))

    assert engine.lifecycle is EngineLifecycle.DRAINING
    assert engine.daemon_serving is False
    assert not endpoint.with_name("last-terminal.json").exists()
