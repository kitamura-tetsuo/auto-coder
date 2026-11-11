"""Controller-origin startup abandonment and its closed observation contract."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import asdict, replace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.automation_engine import AutomationEngine, EngineLifecycle
from auto_coder.cli_commands_main import _run_process_issues_daemon
from auto_coder.github_pending_work import PendingWorkPersistenceError
from auto_coder.health_monitor import HealthMonitor
from auto_coder.implementation_slots import ImplementationSlotUnavailable
from auto_coder.local_job_handoff import LocalJobStoreUnavailable
from auto_coder.util.github_request_outcome import GitHubApiOutcome, GitHubReadCancelled
from auto_coder.worker_management_bridge import WorkerManagementBridge, configured_identity
from auto_coder.worker_startup_failure import observed_startup_failure
from tests.test_pending_work_resumption import _github_error
from tests.test_worker_management_bridge import configure


def test_actual_release_daemon_abandons_startup_but_owns_checkpoint_until_graceful_terminal(monkeypatch, tmp_path):
    endpoint = configure(monkeypatch, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    admitted = engine.invocation_gate.try_admit(repository="owner/repo", target="issue:7", stage="original-submission")
    assert admitted is not None
    admitted.begin_checkpointing("unknown external effect")
    admitted.record_checkpoint_attempt_failed("checkpoint temporarily unavailable")
    # Inject only the dependency failure. The actual engine must decide to
    # abandon startup, produce the fact and perform its existing owned drain.
    monkeypatch.setattr(engine, "_attempt_startup_reconciliation", AsyncMock(side_effect=RuntimeError("credential-canary")))
    monitor = HealthMonitor(log_dir=tmp_path / "health", enabled=True)
    monkeypatch.setattr("auto_coder.automation_engine.get_health_monitor", lambda: monitor)

    async def scenario():
        daemon = asyncio.create_task(_run_process_issues_daemon(engine, "owner/repo", False, "127.0.0.1", 0, "critical", None, None))
        transport = httpx.AsyncHTTPTransport(uds=str(endpoint))
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
                for number in range(200):
                    try:
                        response = await client.get("/v1/observation", params={"nonce": f"failure-{number}"})
                        body = response.json()
                        if body["lifecycle"]["state"] == "failed_awaiting_drain":
                            break
                    except httpx.ConnectError:
                        pass
                    await asyncio.sleep(0.01)
                else:
                    raise AssertionError("actual daemon did not retain abandoned startup")
                failure = observed_startup_failure(body)
                assert failure is not None and asdict(failure) == asdict(engine.startup_failure)
                assert "credential-canary" not in json.dumps(body["lifecycle"]["startup_failure"])
                assert response.headers["auto-coder-process-run-id"] == body["process_run_id"]
                assert body["deployment_generation"] == "generation-7"
                assert body["invocations"]["unsettled_count"] == 1
                assert body["invocations"]["unsettled"][0]["invocation_id"] == admitted.identity.invocation_id
                assert not daemon.done() and not endpoint.with_name("last-terminal.json").exists()
                assert engine.invocation_gate.try_admit(repository="owner/repo", target="issue:8", stage="new-work") is None
                assert engine.request_graceful_shutdown("authenticated Supervisor SIGTERM")
                await asyncio.sleep(0.08)
                during = (await client.get("/v1/observation", params={"nonce": "owned-drain"})).json()
                assert during["lifecycle"]["state"] == "draining"
                assert observed_startup_failure(during) == failure
                assert not daemon.done() and not endpoint.with_name("last-terminal.json").exists()
                assert admitted.confirm_settled("original-durable-checkpoint")
            await asyncio.wait_for(daemon, 10)
            receipt = json.loads(endpoint.with_name("last-terminal.json").read_text())
            assert receipt["lifecycle_state"] == "stopped" and receipt["unsettled_count"] == 0
            assert receipt["process_run_id"] == body["process_run_id"]
            assert receipt["deployment_generation"] == body["deployment_generation"]
            assert engine.lifecycle is EngineLifecycle.STOPPED and not engine.daemon_serving
        finally:
            if not daemon.done():
                admitted.confirm_settled("test-cleanup")
                engine.request_graceful_shutdown("test-cleanup")
                await asyncio.wait_for(daemon, 10)

    asyncio.run(scenario())
    records = [json.loads(line) for path in (tmp_path / "health").glob("*.jsonl") for line in path.read_text().splitlines()]
    failures = [record for record in records if record.get("category") == "engine_startup_failure"]
    assert len(failures) == 1 and failures[0]["message"] == "startup_reconciliation_abandoned"
    assert "credential-canary" not in json.dumps(records)


@pytest.mark.parametrize(
    "error",
    [
        ConnectionError("temporary"),
        httpx.ConnectError("temporary"),
        sqlite3.OperationalError("locked"),
        PendingWorkPersistenceError("locked"),
        ImplementationSlotUnavailable("locked"),
        LocalJobStoreUnavailable("locked"),
        GitHubReadCancelled("cancelled"),
        asyncio.CancelledError(),
        _github_error(GitHubApiOutcome.AUTHENTICATION_FAILURE),
    ],
)
@pytest.mark.parametrize("wrapped", ["none", "explicit", "implicit", "group", "cycle"])
def test_operational_or_cancelled_startup_never_becomes_terminal_failure(monkeypatch, tmp_path, error, wrapped):
    monkeypatch.setenv("HOME", str(tmp_path))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    engine.startup_failure_recovery_enabled = True
    if wrapped in {"explicit", "implicit", "cycle"}:
        try:
            raise error
        except BaseException as operational:
            try:
                if wrapped == "explicit":
                    raise RuntimeError("adapter wrapper") from operational
                raise RuntimeError("adapter wrapper")
            except RuntimeError as outer:
                error = outer
        if wrapped == "cycle":
            error.__cause__ = error
    elif wrapped == "group":
        error = BaseExceptionGroup("parallel startup failures", [RuntimeError("other defect"), error])
    monkeypatch.setattr(engine, "_perform_startup_reconciliation", AsyncMock(side_effect=error))
    with pytest.raises(type(error)):
        asyncio.run(engine._reconcile_startup_or_retain_failure("owner/repo", []))
    assert engine.startup_failure is None and engine.lifecycle is EngineLifecycle.RUNNING
    assert engine.invocation_gate.snapshot().state.value == "running"


@pytest.mark.parametrize("channel", ["beta", "unmanaged"])
def test_beta_and_unmanaged_startup_keep_original_failure_behavior(monkeypatch, tmp_path, channel):
    configure(monkeypatch, tmp_path)
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    if channel == "beta":
        identity = configured_identity("owner/repo")
        WorkerManagementBridge(replace(identity, channel="beta"), engine)
    monkeypatch.setattr(engine, "_perform_startup_reconciliation", AsyncMock(side_effect=RuntimeError("startup defect")))
    with pytest.raises(RuntimeError, match="startup defect"):
        asyncio.run(engine._reconcile_startup_or_retain_failure("owner/repo", []))
    assert engine.startup_failure is None and engine.lifecycle is EngineLifecycle.RUNNING


def test_governed_startup_authentication_deferral_stays_pending_without_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    engine.startup_failure_recovery_enabled = True
    monkeypatch.setattr(engine, "_attempt_startup_reconciliation", AsyncMock(side_effect=_github_error(GitHubApiOutcome.AUTHENTICATION_FAILURE)))

    async def scenario():
        engine._loop = asyncio.get_running_loop()
        engine._shutdown_event = asyncio.Event()
        engine._startup_reconciliation_event = asyncio.Event()
        waiting = asyncio.create_task(engine._reconcile_startup_or_retain_failure("owner/repo", []))
        await asyncio.sleep(0.1)
        assert not waiting.done() and engine.startup_failure is None
        assert engine.lifecycle is EngineLifecycle.RUNNING and not engine.startup_reconciled
        engine.request_graceful_shutdown("operator")
        assert await asyncio.wait_for(waiting, 3) is True

    asyncio.run(scenario())
