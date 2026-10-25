import asyncio
import io
import json
import os
import sqlite3
import threading
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from loguru import logger

import auto_coder.worker_management_bridge as bridge_module
from auto_coder.automation_config import AutomationConfig
from auto_coder.automation_engine import AutomationEngine, EngineLifecycle
from auto_coder.build_provenance import ArtifactField, ControllerArtifactObservation
from auto_coder.entity_invalidation import EntityIdentity, InvalidationDisposition
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


def test_real_socket_reevaluation_accepts_replays_conflicts_and_survives_replacement(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    endpoint = configure(monkeypatch, tmp_path)
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    engine.startup_reconciled = True
    engine.daemon_serving = True
    engine._enqueue_pending_invalidations = AsyncMock()
    engine.review_adjudications.store.affected_prs = MagicMock(return_value=[])

    async def scenario() -> None:
        identity = configured_identity("owner/repo")
        assert identity is not None
        bridge = WorkerManagementBridge(identity, engine)
        await bridge.start()
        payload = {
            "operation_id": "management:2602:1",
            "repository": "owner/repo",
            "channel": "release",
            "expected_process_run_id": bridge.artifact.process_run_id,
            "expected_deployment_generation": "generation-7",
            "target": {"type": "issue", "number": 2602},
        }
        try:
            transport = httpx.AsyncHTTPTransport(uds=str(endpoint))
            async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
                accepted = await client.post("/v1/reevaluations", json=payload)
                replay = await client.post("/v1/reevaluations", json=payload)
                fetched = await client.get("/v1/reevaluations/management:2602:1")
                conflict = await client.post("/v1/reevaluations", json={**payload, "target": {"type": "pr", "number": 2602}})
                boolean = await client.post("/v1/reevaluations", json={**payload, "operation_id": "bad-number", "target": {"type": "issue", "number": True}})
                forced = await client.post("/v1/reevaluations", json={**payload, "operation_id": "bad-field", "force": True})
            assert accepted.status_code == 202
            assert replay.status_code == fetched.status_code == 200
            assert accepted.json() == replay.json() == fetched.json()
            assert accepted.json()["outcome"] == "accepted_for_reevaluation"
            assert accepted.json()["invalidation"] == {"disposition": "new_pending", "generation": 1}
            assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "operation_payload_conflict"
            assert boolean.status_code == forced.status_code == 422
            assert engine.invalidations.pending_count("owner/repo") == 1
            engine._enqueue_pending_invalidations.assert_awaited_once_with("owner/repo")
        finally:
            await bridge.close()

        # Historical lookup and identical replay do not require the old
        # incarnation to remain RUNNING, while a new stale command is refused.
        engine.request_graceful_shutdown("replacement")
        replacement = WorkerManagementBridge(identity, engine)
        replacement.artifact = replace(replacement.artifact, process_run_id="replacement-run")
        await replacement.start()
        try:
            transport = httpx.AsyncHTTPTransport(uds=str(endpoint))
            async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
                fetched_after = await client.get("/v1/reevaluations/management:2602:1")
                replay_after = await client.post("/v1/reevaluations", json=payload)
                stale_new = await client.post("/v1/reevaluations", json={**payload, "operation_id": "management:2602:2"})
            assert fetched_after.status_code == replay_after.status_code == 200
            assert fetched_after.json()["accepting_process_run_id"] == payload["expected_process_run_id"]
            assert stale_new.status_code == 409
            assert stale_new.json()["error"]["code"] == "not_ready_or_draining"
        finally:
            await replacement.close()

    asyncio.run(scenario())


def test_overlapping_socket_duplicates_and_distinct_operations_use_real_queue_transitions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    endpoint = configure(monkeypatch, tmp_path)
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    engine.startup_reconciled = engine.daemon_serving = True
    engine._enqueue_pending_invalidations = AsyncMock()
    entered = threading.Event()
    release = threading.Event()
    original = engine.invalidations.accept_management_reevaluation

    def held_accept(**kwargs):
        entered.set()
        assert release.wait(5)
        return original(**kwargs)

    monkeypatch.setattr(engine.invalidations, "accept_management_reevaluation", held_accept)

    async def scenario() -> None:
        identity = configured_identity("owner/repo")
        assert identity is not None
        bridge = WorkerManagementBridge(identity, engine)
        await bridge.start()
        payload = {
            "operation_id": "overlap-1",
            "repository": "owner/repo",
            "channel": "release",
            "expected_process_run_id": bridge.artifact.process_run_id,
            "expected_deployment_generation": "generation-7",
            "target": {"type": "pr", "number": 90},
        }
        try:
            transport = httpx.AsyncHTTPTransport(uds=str(endpoint))
            async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
                first = asyncio.create_task(client.post("/v1/reevaluations", json=payload))
                assert await asyncio.to_thread(entered.wait, 5)
                second = asyncio.create_task(client.post("/v1/reevaluations", json=payload))
                await asyncio.sleep(0.05)
                assert not second.done()
                release.set()
                one, two = await asyncio.gather(first, second)
                different = await client.post("/v1/reevaluations", json={**payload, "operation_id": "overlap-2"})
            assert {one.status_code, two.status_code} == {200, 202}
            assert one.json() == two.json()
            assert different.status_code == 202
            assert different.json()["invalidation"] == {"disposition": "coalesced", "generation": 1}
            assert engine.invalidations.pending_count("owner/repo") == 1

            claim = engine.invalidations.claim("owner/repo")
            assert claim is not None and engine.invalidations.begin_processing(claim)
            transport = httpx.AsyncHTTPTransport(uds=str(endpoint))
            async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
                followup = await client.post("/v1/reevaluations", json={**payload, "operation_id": "overlap-3"})
            assert followup.json()["invalidation"]["disposition"] == InvalidationDisposition.FOLLOWUP_REQUIRED.value
            assert engine.invalidations.complete(claim) is True
            next_claim = engine.invalidations.claim("owner/repo")
            assert next_claim is not None and next_claim.generation == 2
        finally:
            await bridge.close()

    asyncio.run(scenario())


def test_socket_acceptance_reaches_worker_deferral_without_provider_dispatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    endpoint = configure(monkeypatch, tmp_path)
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    engine.startup_reconciled = engine.daemon_serving = True
    deadline = MagicMock(not_before=10_000_000_000.0)
    deadline.reason.value = "ordinary_admission_wait"
    monkeypatch.setattr(engine, "_future_issue_admission_deferral", lambda *_args: deadline)
    provider = MagicMock(side_effect=AssertionError("acceptance must not dispatch a provider"))
    monkeypatch.setattr(engine, "_process_single_candidate", provider)

    async def scenario() -> None:
        identity = configured_identity("owner/repo")
        assert identity is not None
        bridge = WorkerManagementBridge(identity, engine)
        await bridge.start()
        payload = {
            "operation_id": "deferred-1",
            "repository": "owner/repo",
            "channel": "release",
            "expected_process_run_id": bridge.artifact.process_run_id,
            "expected_deployment_generation": "generation-7",
            "target": {"type": "issue", "number": 91},
        }
        try:
            transport = httpx.AsyncHTTPTransport(uds=str(endpoint))
            async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
                response = await client.post("/v1/reevaluations", json=payload)
            assert response.status_code == 202
            worker = asyncio.create_task(engine._worker_loop("owner/repo", 1))
            for _ in range(200):
                deferred = engine.invalidations.get_deferred(EntityIdentity("owner/repo", "issue", 91))
                if deferred is not None:
                    break
                await asyncio.sleep(0.01)
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            assert deferred is not None and deferred.reason == "ordinary_admission_wait"
            provider.assert_not_called()
        finally:
            await bridge.close()

    asyncio.run(scenario())


def test_socket_drain_and_acceptance_share_one_lifecycle_order(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    endpoint = configure(monkeypatch, tmp_path)
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    engine.startup_reconciled = engine.daemon_serving = True
    engine._enqueue_pending_invalidations = AsyncMock()
    commit_entered, commit_release = threading.Event(), threading.Event()
    original_accept = engine.invalidations.accept_management_reevaluation

    def hold_after_acceptance_ownership(**kwargs):
        commit_entered.set()
        assert commit_release.wait(5)
        return original_accept(**kwargs)

    monkeypatch.setattr(engine.invalidations, "accept_management_reevaluation", hold_after_acceptance_ownership)

    async def scenario() -> None:
        identity = configured_identity("owner/repo")
        assert identity is not None
        bridge = WorkerManagementBridge(identity, engine)
        await bridge.start()
        payload = {
            "operation_id": "drain-winner",
            "repository": "owner/repo",
            "channel": "release",
            "expected_process_run_id": bridge.artifact.process_run_id,
            "expected_deployment_generation": "generation-7",
            "target": {"type": "pr", "number": 92},
        }
        try:
            transport = httpx.AsyncHTTPTransport(uds=str(endpoint))
            async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
                accepting = asyncio.create_task(client.post("/v1/reevaluations", json=payload))
                assert await asyncio.to_thread(commit_entered.wait, 5)
                draining = asyncio.create_task(asyncio.to_thread(engine.request_graceful_shutdown, "raced drain"))
                await asyncio.sleep(0.05)
                assert not draining.done()
                commit_release.set()
                accepted, drained = await asyncio.gather(accepting, draining)
                refused = await client.post("/v1/reevaluations", json={**payload, "operation_id": "drain-loser", "target": {"type": "pr", "number": 93}})
            assert accepted.status_code == 202 and drained is True
            assert refused.status_code == 409 and refused.json()["error"]["code"] == "not_ready_or_draining"
            assert engine.invalidations.pending_count("owner/repo") == 1
        finally:
            await bridge.close()

    asyncio.run(scenario())


def test_socket_drain_wins_before_acceptance_lifecycle_boundary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    endpoint = configure(monkeypatch, tmp_path)
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    engine.startup_reconciled = engine.daemon_serving = True
    intake_entered, intake_release = threading.Event(), threading.Event()
    original_commit = engine._commit_private_reevaluation

    def hold_before_lifecycle(*args, **kwargs):
        intake_entered.set()
        assert intake_release.wait(5)
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(engine, "_commit_private_reevaluation", hold_before_lifecycle)

    async def scenario() -> None:
        identity = configured_identity("owner/repo")
        assert identity is not None
        bridge = WorkerManagementBridge(identity, engine)
        await bridge.start()
        payload = {
            "operation_id": "drain-first",
            "repository": "owner/repo",
            "channel": "release",
            "expected_process_run_id": bridge.artifact.process_run_id,
            "expected_deployment_generation": "generation-7",
            "target": {"type": "pr", "number": 96},
        }
        try:
            transport = httpx.AsyncHTTPTransport(uds=str(endpoint))
            async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
                request = asyncio.create_task(client.post("/v1/reevaluations", json=payload))
                assert await asyncio.to_thread(intake_entered.wait, 5)
                assert engine.request_graceful_shutdown("drain won race") is True
                intake_release.set()
                response = await request
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "not_ready_or_draining"
            assert engine.invalidations.pending_count("owner/repo") == 0
            assert engine.invalidations.get_reevaluation_receipt("drain-first") is None
        finally:
            await bridge.close()

    asyncio.run(scenario())


def test_lost_response_recovers_primary_associated_pr_and_old_receipt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    endpoint = configure(monkeypatch, tmp_path)
    invalidations = tmp_path / "invalidations.sqlite3"
    adjudications = tmp_path / "adjudications.sqlite3"
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(invalidations))
    monkeypatch.setenv("AUTO_CODER_REVIEW_ADJUDICATION_DB", str(adjudications))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    engine.startup_reconciled = engine.daemon_serving = True
    engine.review_adjudications.store.associate_issues("owner/repo", 194, (94,))
    unavailable = MagicMock(wraps=engine.review_adjudications.mark_unavailable)
    monkeypatch.setattr(engine.review_adjudications, "mark_unavailable", unavailable)
    engine._enqueue_pending_invalidations = AsyncMock(side_effect=ConnectionError("response path lost after commit"))

    async def scenario() -> None:
        identity = configured_identity("owner/repo")
        assert identity is not None
        bridge = WorkerManagementBridge(identity, engine)
        await bridge.start()
        payload = {
            "operation_id": "lost-response",
            "repository": "owner/repo",
            "channel": "release",
            "expected_process_run_id": bridge.artifact.process_run_id,
            "expected_deployment_generation": "generation-7",
            "target": {"type": "issue", "number": 94},
        }
        try:
            transport = httpx.AsyncHTTPTransport(uds=str(endpoint))
            async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
                ambiguous = await client.post("/v1/reevaluations", json=payload)
            assert ambiguous.status_code == 503
        finally:
            await bridge.close()
        unavailable.assert_called_once_with("owner/repo", 194, "contributing Issue invalidated")

        replacement = AutomationEngine(MagicMock(), AutomationConfig())
        recovered = replacement.invalidations.recover("owner/repo")
        assert recovered == []  # committed dirty rows need no processing-claim repair
        assert replacement.invalidations.pending_count("owner/repo") == 2
        claims = [replacement.invalidations.claim("owner/repo") for _ in range(2)]
        assert all(claim is not None for claim in claims)
        recovered_claims = {claim.identity for claim in claims if claim is not None}
        assert recovered_claims == {EntityIdentity("owner/repo", "issue", 94), EntityIdentity("owner/repo", "pr", 194)}
        replacement.startup_reconciled = replacement.daemon_serving = True
        replacement_bridge = WorkerManagementBridge(identity, replacement)
        replacement_bridge.artifact = replace(replacement_bridge.artifact, process_run_id="new-run")
        await replacement_bridge.start()
        try:
            transport = httpx.AsyncHTTPTransport(uds=str(endpoint))
            async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
                receipt = await client.get("/v1/reevaluations/lost-response")
                replay = await client.post("/v1/reevaluations", json=payload)
            assert receipt.status_code == replay.status_code == 200
            assert receipt.json() == replay.json()
            assert receipt.json()["accepting_process_run_id"] == payload["expected_process_run_id"]
            assert replacement.invalidations.pending_count("owner/repo") == 2
        finally:
            await replacement_bridge.close()

    asyncio.run(scenario())


def test_socket_storage_failures_never_fabricate_acceptance_or_receipts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    endpoint = configure(monkeypatch, tmp_path)
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    engine.startup_reconciled = engine.daemon_serving = True

    async def scenario() -> None:
        identity = configured_identity("owner/repo")
        assert identity is not None
        bridge = WorkerManagementBridge(identity, engine)
        await bridge.start()
        payload = {
            "operation_id": "storage-down",
            "repository": "owner/repo",
            "channel": "release",
            "expected_process_run_id": bridge.artifact.process_run_id,
            "expected_deployment_generation": "generation-7",
            "target": {"type": "pr", "number": 95},
        }
        try:
            monkeypatch.setattr(engine.invalidations, "get_reevaluation_receipt", MagicMock(side_effect=sqlite3.OperationalError("unavailable")))
            transport = httpx.AsyncHTTPTransport(uds=str(endpoint))
            async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
                acceptance = await client.post("/v1/reevaluations", json=payload)
                lookup = await client.get("/v1/reevaluations/storage-down")
            assert acceptance.status_code == lookup.status_code == 503
            assert acceptance.json()["error"]["code"] == lookup.json()["error"]["code"] == "storage_unavailable"
            assert engine.invalidations.pending_count("owner/repo") == 0
        finally:
            await bridge.close()

    asyncio.run(scenario())


def test_socket_distinguishes_foreign_worker_from_malformed_request(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    endpoint = configure(monkeypatch, tmp_path)
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    engine.startup_reconciled = engine.daemon_serving = True
    engine._enqueue_pending_invalidations = AsyncMock()

    async def scenario() -> None:
        identity = configured_identity("owner/repo")
        assert identity is not None
        bridge = WorkerManagementBridge(identity, engine)
        await bridge.start()
        payload = {
            "operation_id": "wrong-worker",
            "repository": "owner/repo",
            "channel": "release",
            "expected_process_run_id": bridge.artifact.process_run_id,
            "expected_deployment_generation": "generation-7",
            "target": {"type": "pr", "number": 97},
        }
        try:
            transport = httpx.AsyncHTTPTransport(uds=str(endpoint))
            async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
                malformed = await client.post("/v1/reevaluations", json={**payload, "force": True})
                foreign_repository = await client.post("/v1/reevaluations", json={**payload, "repository": "owner/other"})
                foreign_channel = await client.post("/v1/reevaluations", json={**payload, "channel": "beta"})
            assert malformed.status_code == 422 and malformed.json()["error"]["code"] == "invalid_request"
            for response in (foreign_repository, foreign_channel):
                assert response.status_code == 409
                assert response.json()["error"]["code"] == "wrong_target"
            assert engine.invalidations.pending_count("owner/repo") == 0
            assert engine.invalidations.get_reevaluation_receipt("wrong-worker") is None
            engine._enqueue_pending_invalidations.assert_not_awaited()
        finally:
            await bridge.close()

    asyncio.run(scenario())


def test_socket_logs_correlated_stale_and_draining_refusals_without_caller_secrets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    endpoint = configure(monkeypatch, tmp_path)
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    engine.startup_reconciled = engine.daemon_serving = True
    captured = io.StringIO()
    sink = logger.add(captured, format="{message}")

    async def scenario() -> None:
        identity = configured_identity("owner/repo")
        assert identity is not None
        bridge = WorkerManagementBridge(identity, engine)
        await bridge.start()
        actual_process = bridge.artifact.process_run_id
        payload = {
            "operation_id": "refusal-stale",
            "repository": "owner/repo",
            "channel": "release",
            "expected_process_run_id": "credential-canary-/private/operator",
            "expected_deployment_generation": "generation-7",
            "target": {"type": "issue", "number": 98},
        }
        try:
            transport = httpx.AsyncHTTPTransport(uds=str(endpoint))
            async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
                stale = await client.post("/v1/reevaluations", json=payload)
                assert engine.request_graceful_shutdown("test drain") is True
                draining = await client.post(
                    "/v1/reevaluations",
                    json={**payload, "operation_id": "refusal-draining", "expected_process_run_id": actual_process, "target": {"type": "pr", "number": 99}},
                )
            assert stale.json()["error"]["code"] == "wrong_incarnation"
            assert draining.json()["error"]["code"] == "not_ready_or_draining"
            output = captured.getvalue()
            assert f"code=wrong_incarnation operation_id=refusal-stale target=issue#98 process_run_id={actual_process}" in output
            assert f"code=not_ready_or_draining operation_id=refusal-draining target=pr#99 process_run_id={actual_process}" in output
            assert "credential-canary" not in output
            assert "/private/operator" not in output
            monkeypatch.setattr(bridge_module.logger, "warning", MagicMock(side_effect=OSError("diagnostic sink failed")))
            async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(endpoint)), base_url="http://worker") as client:
                sink_failure = await client.post(
                    "/v1/reevaluations",
                    json={**payload, "operation_id": "refusal-sink-failure", "expected_process_run_id": actual_process},
                )
            assert sink_failure.status_code == 409
            assert sink_failure.json()["error"]["code"] == "not_ready_or_draining"
            assert engine.invalidations.pending_count("owner/repo") == 0
        finally:
            await bridge.close()

    try:
        asyncio.run(scenario())
    finally:
        logger.remove(sink)


def test_conflicting_endpoint_is_never_replaced(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from auto_coder.cli_commands_main import _run_process_issues_daemon

    endpoint = configure(monkeypatch, tmp_path)
    endpoint.write_text("operator-owned")
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    with pytest.raises(ManagementBridgeConfigurationError, match="already exists"):
        asyncio.run(_run_process_issues_daemon(engine, "owner/repo", False, "127.0.0.1", 0, "critical", None, None))
    assert endpoint.read_text() == "operator-owned"


def test_concurrent_socket_acquisition_has_one_lifetime_owner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    endpoint = configure(monkeypatch, tmp_path)
    identity = configured_identity("owner/repo")
    assert identity is not None
    first = WorkerManagementBridge(replace(identity, deployment_generation="generation-first"), ObservedEngine())
    second = WorkerManagementBridge(replace(identity, deployment_generation="generation-second"), ObservedEngine())

    async def scenario() -> None:
        results = await asyncio.gather(first.start(), second.start(), return_exceptions=True)
        winners = [bridge for bridge, result in zip((first, second), results) if result is None]
        losers = [bridge for bridge, result in zip((first, second), results) if isinstance(result, ManagementBridgeConfigurationError)]
        assert len(winners) == len(losers) == 1
        winner, loser = winners[0], losers[0]
        await loser.close()
        assert endpoint.is_socket()
        transport = httpx.AsyncHTTPTransport(uds=str(endpoint))
        async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
            response = await client.get("/v1/observation", params={"nonce": "winner"})
        assert response.status_code == 200
        assert response.headers["auto-coder-deployment-generation"] == winner.identity.deployment_generation
        await winner.close()
        assert not endpoint.exists()

    asyncio.run(scenario())


def test_observation_sanitizes_free_text_before_clipping(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    endpoint = configure(monkeypatch, tmp_path)
    engine = ObservedEngine()
    secret = "ghp_" + "a" * 2100
    engine.startup_reconciliation_error = f"failed /private/operator/db https://user:pass@example.test/x Bearer bearer-secret {secret}"

    async def scenario() -> None:
        identity = configured_identity("owner/repo")
        assert identity is not None
        bridge = WorkerManagementBridge(identity, engine)
        await bridge.start()
        try:
            transport = httpx.AsyncHTTPTransport(uds=str(endpoint))
            async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
                response = await client.get("/v1/observation", params={"nonce": "redaction"})
            projected = response.json()["lifecycle"]["startup_reconciliation_error"]
            assert "[REDACTED_PATH]" in projected
            assert "[REDACTED_URL]" in projected
            assert "Bearer [REDACTED]" in projected
            assert len(projected) < 2000
            for sensitive in ("/private/operator/db", "user:pass", "bearer-secret", secret):
                assert sensitive not in projected
        finally:
            await bridge.close()

    asyncio.run(scenario())


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
    sensitive_failure = "database /private/operator/slots.sqlite Bearer credential-canary"
    captured_logs = io.StringIO()
    sink = logger.add(captured_logs, format="{message}")
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
            for _ in range(100):
                if engine.implementation_slots is not None:
                    break
                await asyncio.sleep(0.01)
            assert engine.implementation_slots is not None
            monkeypatch.setattr(engine.implementation_slots, "snapshot", MagicMock(side_effect=OSError(sensitive_failure)))
            status = await client.get("/api/status")
            assert status.status_code == 200
            assert status.headers["auto-coder-deployment-generation"] == "generation-7"
            assert status.json()["implementation_slots"]["error_code"] == "slot_observation_unavailable"

        admitted.begin_checkpointing("result")
        assert admitted.confirm_settled("durable-checkpoint")
        await asyncio.wait_for(daemon, timeout=10)

    try:
        asyncio.run(scenario())
    finally:
        logger.remove(sink)
    assert "public API status: slot_observation_unavailable" in captured_logs.getvalue()
    assert "credential-canary" not in captured_logs.getvalue()
    assert "/private/operator/slots.sqlite" not in captured_logs.getvalue()
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


def test_actual_daemon_bridge_responds_while_startup_store_recovery_blocks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from auto_coder.cli_commands_main import _run_process_issues_daemon

    endpoint = configure(monkeypatch, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    routing_db = tmp_path / "routing.sqlite3"
    monkeypatch.setenv("AUTO_CODER_ISSUE_STAGE_ROUTING_DB", str(routing_db))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    blocker = sqlite3.connect(routing_db)
    blocker.execute("BEGIN IMMEDIATE")
    blocker.execute("UPDATE issue_lane_arrivals SET state = state")

    async def scenario() -> None:
        daemon = asyncio.create_task(_run_process_issues_daemon(engine, "owner/repo", False, "127.0.0.1", 0, "critical", None, None))
        transport = httpx.AsyncHTTPTransport(uds=str(endpoint))
        async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
            for _ in range(100):
                try:
                    response = await asyncio.wait_for(client.get("/v1/observation", params={"nonce": "store recovery"}), timeout=1)
                    break
                except httpx.ConnectError:
                    await asyncio.sleep(0.01)
            else:
                raise AssertionError("management socket did not become reachable during store recovery")
        assert response.status_code == 200
        assert response.json()["lifecycle"]["daemon_serving"] is False
        assert not daemon.done()
        engine.request_graceful_shutdown("test cleanup")
        blocker.commit()
        await asyncio.wait_for(daemon, timeout=10)

    try:
        asyncio.run(scenario())
    finally:
        blocker.close()


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
