"""Exact-source proof and authenticated lifecycle journal regressions."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from auto_coder.beta_source_selection import MutationCertainty
from auto_coder.management.action_scopes import DEPLOY_SCOPE, READ_SCOPE
from auto_coder.management.app import create_management_app
from auto_coder.management.oauth_config import ManagementSettings, OAuthClient, OAuthSettings
from auto_coder.management.oauth_grants import OAuthGrantService
from auto_coder.management.operation_store import ManagementOperationStore
from auto_coder.management.source_evidence import BetaEvidenceReader
from auto_coder.management.worker_network_client import WorkerEndpointConfig, WorkerNetworkClient, WorkerNetworkResult
from auto_coder.util.gh_cache import GitDataResponse
from auto_coder.worker_supervisor import WorkerSupervisor
from tests.test_beta_source_selection import SHA_A, GitHubBoundary, selector
from tests.test_management_mcp import _issue
from tests.test_management_runtime_mcp import _call, _mcp, real_boundary
from tests.test_worker_supervisor_bridge import _bridge, _supervisor


class GitEvidenceBoundary(GitHubBoundary):
    def request(self, method: str, path: str, payload: dict[str, object] | None = None) -> GitDataResponse:
        if method == "PATCH" and path == "git/refs/tags/auto-coder-beta-selection-current" and payload:
            self.refs["refs/tags/auto-coder-beta-selection-current"] = ("tag", str(payload["sha"]))
            return GitDataResponse(200)
        result = super().request(method, path, payload)
        if method == "GET" and (path.startswith("git/tags/") or path.startswith("git/ref/tags/")) and isinstance(result.data, dict):
            # The real GitHub Git-object endpoint includes a URL beside the
            # type and SHA. An exact two-key dict equality rejects production.
            document = dict(result.data)
            target = document.get("object")
            if isinstance(target, dict):
                document["object"] = {**target, "url": "https://api.github.com/repos/owner/repo/git/commits/example"}
                return GitDataResponse(result.status, document)
        if method == "GET" and path.endswith("/jobs?per_page=100") and isinstance(result.data, dict):
            jobs = result.data.get("jobs")
            if isinstance(jobs, list):
                for index, job in enumerate(jobs, start=1):
                    job["id"] = index + 100
        return result


def test_beta_evidence_retains_original_proof_across_later_same_sha_run_and_retry() -> None:
    remote = GitEvidenceBoundary()
    assert selector(remote).advance("r77", None).certainty is MutationCertainty.CONFIRMED
    reader = BetaEvidenceReader(remote, "owner/repo")  # type: ignore[arg-type]
    first = reader.read(SHA_A)
    assert (first.proof_run_id, first.proof_run_attempt) == (101, 1)
    assert (first.selected_run_id, first.selected_run_attempt) == (11, 1)
    assert reader.matches_original(first.canonical())

    remote.executions[(12, 1)] = (SHA_A, "2026-10-02T03:00:00Z")
    assert selector(remote).advance("r78", SHA_A).certainty is MutationCertainty.CONFIRMED
    newer = reader.read(SHA_A)
    assert (newer.selected_run_id, newer.selected_run_attempt) == (12, 1)
    assert newer.proof_run_id == first.proof_run_id
    assert newer.tested_source_oid == first.tested_source_oid
    assert newer.selection_oid != first.selection_oid
    assert not reader.matches_original(first.canonical())

    remote.executions[(12, 2)] = (SHA_A, "2026-10-03T04:00:00Z")
    assert selector(remote).advance("r79", SHA_A).certainty is MutationCertainty.CONFIRMED
    retry = reader.read(SHA_A)
    assert (retry.selected_run_id, retry.selected_run_attempt) == (12, 2)
    assert retry.proof_run_id == first.proof_run_id
    assert not reader.matches_original(newer.canonical())


def test_older_same_sha_selection_cannot_repoint_newer_current_selection() -> None:
    remote = GitEvidenceBoundary()
    assert selector(remote).advance("r77", None).certainty is MutationCertainty.CONFIRMED
    remote.executions[(12, 1)] = (SHA_A, "2026-10-02T03:00:00Z")
    assert selector(remote).advance("r78", SHA_A).certainty is MutationCertainty.CONFIRMED
    previous_oid = remote.refs["refs/tags/auto-coder-beta-selection-current"]
    assert selector(remote).advance("r77", None).certainty is MutationCertainty.SUPERSEDED
    assert remote.refs["refs/tags/auto-coder-beta-selection-current"] == previous_oid


class LifecycleBridge:
    """Typed Supervisor boundary; not an end-to-end child-process oracle."""

    def __init__(self) -> None:
        self.config = WorkerEndpointConfig("beta-worker", "beta", "fixture/beta", "10.23.10.12", 17890, b"a" * 48)
        self.supervisor_generation = "supervisor-a"
        self.worker: dict[str, object] | None = {
            "process_run_id": "run-old",
            "deployment_generation": "generation-old",
            "runtime_id": "runtime-old",
            "binding_status": "VERIFIED",
            "runtime_binding": {"runtime_id": "runtime-old", "content_identity": "a" * 64, "lock_sha256": "b" * 64, "source_sha": "b" * 40, "distribution_version": "1.0.0"},
        }
        self.selected = "runtime-old"
        self.actions: list[tuple[str, str]] = []
        self.receipts: dict[str, dict[str, object]] = {}
        self.preparation: dict[str, object] | None = None
        self.heartbeat = 0
        self.lost_stop_response = False
        self.crash_new = False

    def _result(self, status: str, data: dict[str, object], request_id: str = "x") -> WorkerNetworkResult:
        return WorkerNetworkResult(200, status, data, self.supervisor_generation, request_id)

    def inspect_supervisor(self) -> WorkerNetworkResult:
        crashed = self.crash_new and self.selected == "runtime-new"
        status = "UNKNOWN" if crashed else "RUNNING" if self.worker else "STOPPED"
        return self._result("OK", {"supervisor_incarnation": self.supervisor_generation, "status": status, "alive": bool(self.worker) and not crashed, "worker": self.worker, "selected_runtime_id": self.selected})

    def prepare_version(self, operation_id: str, source_sha: str) -> WorkerNetworkResult:
        self.actions.append(("PREPARE", operation_id))
        self.preparation = {"status": "READY", "operation_id": operation_id, "source_sha": source_sha, "lock_sha256": "e" * 64, "distribution_version": f"1.0+g{source_sha}", "runtime_profile": "python-3.12", "content_identity": "c" * 64, "installed_runtime_id": "runtime-new"}
        return self._result("IN_PROGRESS", {"status": "IN_PROGRESS", "operation_id": operation_id})

    def read_preparation(self, operation_id: str) -> WorkerNetworkResult:
        assert self.preparation is not None
        return self._result(str(self.preparation["status"]), self.preparation)

    def dispatch_worker_operation(self, operation_id: str, operation: str, *, process_run_id: str | None, deployment_generation: str | None, selected_runtime_id: str | None, runtime_id: str | None = None) -> WorkerNetworkResult:
        self.actions.append((operation, operation_id))
        digest = WorkerSupervisor._digest({"operation": operation, "expected_supervisor_incarnation": self.supervisor_generation, "expected_process_run_id": process_run_id, "expected_generation": deployment_generation, "expected_runtime_id": selected_runtime_id, "runtime_id": runtime_id})
        if operation == "REQUEST_GRACEFUL_STOP":
            receipt = {"status": "STOPPED", "operation_id": operation_id, "intent_digest": digest}
            self.worker = None
            result = self._result("UNKNOWN", {}) if self.lost_stop_response else self._result("DRAINING", {"status": "DRAINING", "operation_id": operation_id})
        elif operation == "START":
            self.selected = runtime_id or "runtime-old"
            self.worker = {"process_run_id": "run-new", "deployment_generation": "generation-new", "runtime_id": self.selected, "binding_status": "VERIFIED"}
            receipt = {"status": "RUNNING", "operation_id": operation_id, "worker": self.worker, "intent_digest": digest}
            result = self._result("RUNNING", receipt)
        elif operation == "RECOVER_PREVIOUS":
            self.selected = "runtime-old"
            self.worker = {"process_run_id": "run-restored", "deployment_generation": "generation-restored", "runtime_id": self.selected, "binding_status": "VERIFIED"}
            receipt = {"status": "RUNNING", "operation_id": operation_id, "worker": self.worker, "intent_digest": digest}
            result = self._result("RUNNING", receipt)
        else:
            raise AssertionError(operation)
        self.receipts[operation_id] = receipt
        return result

    def read_worker_operation(self, operation_id: str) -> WorkerNetworkResult:
        data = self.receipts[operation_id]
        return self._result(str(data["status"]), data)

    def observe_worker(self, nonce: str | None = None) -> WorkerNetworkResult:
        assert self.worker is not None
        self.heartbeat += 1
        restored = self.selected == "runtime-old"
        source, version = ("b" * 40, "1.0.0") if restored else (SHA_A, f"1.0+g{SHA_A}")
        binding = {
            "runtime_id": self.selected,
            "content_identity": ("a" if restored else "c") * 64,
            "lock_sha256": ("b" if restored else "e") * 64,
            "source_sha": source,
            "distribution_version": version,
            "process_run_id": self.worker["process_run_id"],
            "deployment_generation": self.worker["deployment_generation"],
        }
        observation = {
            "repository": self.config.repository,
            "channel": self.config.channel,
            "nonce": nonce,
            "process_run_id": self.worker["process_run_id"],
            "deployment_generation": self.worker["deployment_generation"],
            "lifecycle": {"state": "running", "startup_reconciled": True, "daemon_serving": True},
            "invocations": {"state": "running"},
            "heartbeat": {"available": True, "sequence": self.heartbeat, "stage": "producer:maintenance-sleep"},
            "artifact": {"source_revision": {"available": True, "value": source}, "distribution_version": {"available": True, "value": version}},
        }
        return self._result("OK", {"observation": observation, "binding_status": "VERIFIED", "runtime_binding": binding})


def _drive(app, operation_id: str, count: int = 16) -> None:
    runtime = app.state.management_runtime
    for _ in range(count):
        record = runtime.journal.get(operation_id).record
        assert record is not None
        if record.phase.value in {"succeeded", "failed"}:
            return
        runtime.lifecycle.process(record)


def test_authenticated_beta_source_readback_uses_current_production_reader_without_deploy(real_boundary, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base, _base_grants, identity, signed, _uids, state = real_boundary
    origin = base.oauth.canonical_issuer
    oauth = OAuthSettings(base.oauth.database_path, origin, base.oauth.firebase_project_id, (OAuthClient("alice-client", (origin + "/alice-callback",), allowed_scopes=(READ_SCOPE, DEPLOY_SCOPE)),), enabled_scopes=(READ_SCOPE, DEPLOY_SCOPE))
    policy = tmp_path / "operator.yaml"
    policy.write_text("profile: beta\ntargets:\n  beta-worker:\n    channel: beta\n    repositories: [fixture/beta]\n", encoding="utf-8")
    token_path = tmp_path / "source-token"
    token_path.write_text("x" * 40, encoding="utf-8")
    settings = ManagementSettings(tmp_path, oauth, port=base.port, operating_policy_path=policy, operation_state_root=tmp_path / "journal", source_repository="owner/repo", source_evidence_token_file=token_path)
    grants = OAuthGrantService(oauth, identity, clock=lambda: state["now"])
    remote = GitEvidenceBoundary()
    assert selector(remote).advance("r77", None).certainty is MutationCertainty.CONFIRMED
    reader = BetaEvidenceReader(remote, "owner/repo")  # type: ignore[arg-type]
    monkeypatch.setattr("auto_coder.management.worker_lifecycle.WorkerLifecycle._source", lambda _self: reader)
    bridge = LifecycleBridge()
    app = create_management_app(settings, grants, worker_clients={"beta": bridge})  # type: ignore[dict-item]
    with TestClient(app, base_url=origin) as client:
        bearer = _issue(client, settings, signed, "alice", "alice-client", READ_SCOPE)["access_token"]
        observed = _call(client, bearer, "auto_coder_beta_source_readback", {"target_id": "beta-worker"})
        exact = reader.read_current()
        assert observed["status"] == "OK" and observed["reason"] == "configured_beta_source_verified"
        assert observed["source_sha"] == exact.source_sha
        assert observed["tested_source_oid"] == exact.tested_source_oid
        assert observed["proof_run_id"] == exact.proof_run_id
        assert observed["selection_oid"] == exact.selection_oid
        assert observed["selection_operation_id"] == exact.selection_operation_id
        assert observed["selected_run_id"] == exact.selected_run_id
        assert bridge.actions == []

        remote.refs.pop("refs/tags/auto-coder-beta-selection-current")
        missing = _call(client, bearer, "auto_coder_beta_source_readback", {"target_id": "beta-worker"})
        assert missing == {"status": "UNAVAILABLE", "reason": "configured_beta_source_evidence_unavailable"}
        assert bridge.actions == []


@pytest.mark.parametrize("intervention", ("none", "selection_changed", "source_unavailable", "policy_downgrade", "lost_stop_response", "supervisor_changed_after_unknown", "crash_after_start"))
def test_authenticated_beta_deploy_replays_exact_evidence_and_only_stops_after_ready(real_boundary, tmp_path: Path, monkeypatch, intervention: str) -> None:
    base, _base_grants, identity, signed, _uids, state = real_boundary
    origin = base.oauth.canonical_issuer
    oauth = OAuthSettings(base.oauth.database_path, origin, base.oauth.firebase_project_id, (OAuthClient("alice-client", (origin + "/alice-callback",), allowed_scopes=(READ_SCOPE, DEPLOY_SCOPE)),), enabled_scopes=(READ_SCOPE, DEPLOY_SCOPE))
    policy = tmp_path / "operator.yaml"
    policy.write_text("profile: beta\ntargets:\n  beta-worker:\n    channel: beta\n    repositories: [fixture/beta]\n", encoding="utf-8")
    token_path = tmp_path / "source-token"
    token_path.write_text("x" * 40, encoding="utf-8")
    settings = ManagementSettings(tmp_path, oauth, port=base.port, operating_policy_path=policy, operation_state_root=tmp_path / "journal", source_repository="owner/repo", source_evidence_token_file=token_path)
    grants = OAuthGrantService(oauth, identity, clock=lambda: state["now"])
    remote = GitEvidenceBoundary()
    assert selector(remote).advance("r77", None).certainty is MutationCertainty.CONFIRMED
    reader = BetaEvidenceReader(remote, "owner/repo")  # type: ignore[arg-type]
    monkeypatch.setattr("auto_coder.management.worker_lifecycle.WorkerLifecycle._source", lambda _self: reader)
    bridge = LifecycleBridge()
    bridge.lost_stop_response = intervention in {"lost_stop_response", "supervisor_changed_after_unknown"}
    bridge.crash_new = intervention == "crash_after_start"
    app = create_management_app(settings, grants, worker_clients={"beta": bridge})  # type: ignore[dict-item]

    async def no_background_monitor() -> None:
        # This test applies each source/policy intervention exactly at the
        # drain boundary and explicitly drives every later lifecycle step.
        pass

    monkeypatch.setattr(app.state.management_runtime, "start_monitor", no_background_monitor)
    with TestClient(app, base_url=origin) as client:
        bearer = _issue(client, settings, signed, "alice", "alice-client", READ_SCOPE + " " + DEPLOY_SCOPE)["access_token"]
        arguments = {"target_id": "beta-worker", "selected_git_sha": SHA_A, "idempotency_key": "deploy-1", "expected_supervisor_generation": bridge.supervisor_generation, "expected_process_run_id": "run-old", "expected_deployment_generation": "generation-old", "expected_runtime_id": "runtime-old"}
        first = _call(client, bearer, "auto_coder_deploy_beta_source", arguments)
        operation_id = str(first["operation_id"])
        runtime = app.state.management_runtime
        for _ in range(5):
            record = runtime.journal.get(operation_id).record
            assert record is not None
            runtime.lifecycle.process(record)
            snapshot = runtime.journal.lifecycle_snapshot(operation_id)
            if snapshot and snapshot[1]["stage"] == "DRAINING":
                break
        else:
            raise AssertionError("worker preparation never reached the drain boundary")
        if intervention == "selection_changed":
            remote.executions[(12, 1)] = (SHA_A, "2026-10-02T03:00:00Z")
            assert selector(remote).advance("r78", SHA_A).certainty is MutationCertainty.CONFIRMED
        elif intervention == "source_unavailable":
            remote.refs.pop("refs/tags/auto-coder-beta-selection-current")
        elif intervention == "policy_downgrade":
            policy.write_text(policy.read_text().replace("profile: beta", "profile: observe"), encoding="utf-8")
        elif intervention == "supervisor_changed_after_unknown":
            original_read = bridge.read_worker_operation

            def unreadable_until_restart(operation_id: str) -> WorkerNetworkResult:
                if bridge.supervisor_generation == "supervisor-a":
                    return bridge._result("UNKNOWN", {})
                return original_read(operation_id)

            monkeypatch.setattr(bridge, "read_worker_operation", unreadable_until_restart)
            outstanding = runtime.journal.get(operation_id).record
            assert outstanding is not None
            runtime.lifecycle.process(outstanding)
            assert [name for name, _id in bridge.actions] == ["PREPARE", "REQUEST_GRACEFUL_STOP"]
            bridge.supervisor_generation = "supervisor-restarted"
        _drive(app, operation_id)
        completed = _call(client, bearer, "auto_coder_operation_get", {"target_id": "beta-worker", "operation_id": operation_id})
        # Consumers must retain the durable effect's uncertainty even when a
        # lifecycle snapshot has advanced or a terminal reply was interrupted.
        original_record = runtime.journal.get(operation_id).record
        assert original_record is not None
        assert completed["effect_state"] == original_record.effect.state.value
        if completed["status"] == "SUCCEEDED":
            assert completed["effect_state"] == "confirmed"
        if intervention == "supervisor_changed_after_unknown":
            assert completed["effect_state"] == "sent_unknown"
        if intervention == "selection_changed":
            assert completed["status"] == "SUPERSEDED"
            assert [name for name, _id in bridge.actions] == ["PREPARE"]
            snapshot = runtime.journal.lifecycle_snapshot(operation_id)
            assert snapshot is not None and snapshot[1]["revalidation_observation"]["matched"] is False
            return
        if intervention == "source_unavailable":
            assert completed["status"] == "NEEDS_ATTENTION"
            assert [name for name, _id in bridge.actions] == ["PREPARE"]
            return
        if intervention == "policy_downgrade":
            assert completed["status"] == "NEEDS_ATTENTION"
            assert [name for name, _id in bridge.actions] == ["PREPARE"]
            return
        if intervention == "supervisor_changed_after_unknown":
            assert completed["status"] == "NEEDS_ATTENTION"
            assert completed["reason"] == "original_supervisor_incarnation_changed"
            assert [name for name, _id in bridge.actions] == ["PREPARE", "REQUEST_GRACEFUL_STOP"]
            assert completed["steps"]["drain"]["state"] == "SENT_UNKNOWN"
            return
        if intervention == "crash_after_start":
            assert completed["status"] == "FAILED" and completed["reason"] == "new_beta_worker_failed_previous_runtime_restored"
            assert completed["recovery_attempts"] == 1
            assert [name for name, _id in bridge.actions] == ["PREPARE", "REQUEST_GRACEFUL_STOP", "START", "RECOVER_PREVIOUS"]
            return
        assert completed["status"] == "SUCCEEDED", completed
        assert [name for name, _id in bridge.actions] == ["PREPARE", "REQUEST_GRACEFUL_STOP", "START"]
        assert len(completed["heartbeats"]) == 2
        assert _call(client, bearer, "auto_coder_deploy_beta_source", arguments)["operation_id"] == operation_id
        assert len(bridge.actions) == 3
        data = app.state.management_runtime.journal.lifecycle_snapshot(operation_id)
        assert data is not None
        assert json.loads(data[1]["accepted_evidence"])["selection_operation_id"] == "r77"
        assert data[1]["revalidation_observation"]["matched"] is True
        assert data[1]["original_deadline"] > data[1]["accepted_at"]


def test_superseded_same_sha_selection_refuses_unsent_drain(real_boundary, tmp_path: Path, monkeypatch) -> None:
    # This exercise calls the same evidence reader consumed by the MCP adapter.
    remote = GitEvidenceBoundary()
    assert selector(remote).advance("r77", None).certainty is MutationCertainty.CONFIRMED
    reader = BetaEvidenceReader(remote, "owner/repo")  # type: ignore[arg-type]
    accepted = reader.read(SHA_A).canonical()
    remote.executions[(12, 1)] = (SHA_A, "2026-10-02T03:00:00Z")
    assert selector(remote).advance("r78", SHA_A).certainty is MutationCertainty.CONFIRMED
    assert not reader.matches_original(accepted)
    assert reader.read(SHA_A).source_sha == SHA_A


@pytest.mark.parametrize("interruption", ("monitor", "manager_reconstruction"))
def test_authenticated_lifecycle_admission_is_atomic_before_ack(real_boundary, tmp_path: Path, monkeypatch, interruption: str) -> None:
    """An independent dispatcher must never see an unbound ACCEPTED lane."""
    base, _base_grants, identity, signed, _uids, state = real_boundary
    origin = base.oauth.canonical_issuer
    oauth = OAuthSettings(base.oauth.database_path, origin, base.oauth.firebase_project_id, (OAuthClient("alice-client", (origin + "/alice-callback",), allowed_scopes=(READ_SCOPE, DEPLOY_SCOPE)),), enabled_scopes=(READ_SCOPE, DEPLOY_SCOPE))
    policy = tmp_path / "operator.yaml"
    policy.write_text("profile: beta\ntargets:\n  beta-worker:\n    channel: beta\n    repositories: [fixture/beta]\n", encoding="utf-8")
    token_path = tmp_path / "source-token"
    token_path.write_text("x" * 40, encoding="utf-8")
    settings = ManagementSettings(tmp_path, oauth, port=base.port, operating_policy_path=policy, operation_state_root=tmp_path / "journal", source_repository="owner/repo", source_evidence_token_file=token_path)
    grants = OAuthGrantService(oauth, identity, clock=lambda: state["now"])
    remote = GitEvidenceBoundary()
    assert selector(remote).advance("r77", None).certainty is MutationCertainty.CONFIRMED
    reader = BetaEvidenceReader(remote, "owner/repo")  # type: ignore[arg-type]
    original_evidence = reader.read(SHA_A).canonical()
    monkeypatch.setattr("auto_coder.management.worker_lifecycle.WorkerLifecycle._source", lambda _self: reader)
    bridge = LifecycleBridge()
    app = create_management_app(settings, grants, worker_clients={"beta": bridge})  # type: ignore[dict-item]
    entered, release = threading.Event(), threading.Event()

    def pause_after_atomic_commit() -> None:
        entered.set()
        assert release.wait(timeout=15), "lifecycle admission acknowledgement was never released"

    with TestClient(app, base_url=origin) as client:
        bearer = _issue(client, settings, signed, "alice", "alice-client", READ_SCOPE + " " + DEPLOY_SCOPE)["access_token"]
        runtime = app.state.management_runtime
        runtime.journal._after_admission_commit = pause_after_atomic_commit
        arguments = {
            "target_id": "beta-worker",
            "selected_git_sha": SHA_A,
            "idempotency_key": "atomic-boundary",
            "expected_supervisor_generation": bridge.supervisor_generation,
            "expected_process_run_id": "run-old",
            "expected_deployment_generation": "generation-old",
            "expected_runtime_id": "runtime-old",
        }
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(_call, client, bearer, "auto_coder_deploy_beta_source", arguments)
                assert entered.wait(timeout=5)
                restarted_store = ManagementOperationStore(settings.operation_state_root or settings.state_root)
                active = restarted_store.list_operations(outstanding_only=True).records
                assert len(active) == 1
                operation_id = active[0].operation_id
                accepted = restarted_store.lifecycle_snapshot(operation_id)
                assert accepted is not None
                assert accepted[1]["accepted_evidence"] == original_evidence
                assert accepted[1]["original_deadline"] >= accepted[1]["accepted_at"] + 900
                if interruption == "monitor":
                    assert client.portal is not None
                    client.portal.call(runtime._poll_lifecycle_once)
                else:
                    reconstructed = create_management_app(settings, grants, worker_clients={"beta": bridge})  # type: ignore[dict-item]
                    with TestClient(reconstructed, base_url=origin):
                        assert reconstructed.state.management_runtime.journal.lifecycle_snapshot(operation_id) is not None
                assert restarted_store.lifecycle_snapshot(operation_id) is not None
                release.set()
                response = pending.result(timeout=10)
        finally:
            release.set()
        assert response["operation_id"] == operation_id
        assert _call(client, bearer, "auto_coder_deploy_beta_source", arguments)["operation_id"] == operation_id
        _drive(app, operation_id, 32)
        finished = _call(client, bearer, "auto_coder_operation_get", {"target_id": "beta-worker", "operation_id": operation_id})
        assert finished["status"] == "SUCCEEDED", finished
        assert len(bridge.actions) == len(set(bridge.actions)) == 3


@pytest.mark.parametrize("expire_drain,reconstruct_supervisor,tamper_digest,downgrade", ((False, False, False, False), (True, False, False, False), (False, True, False, False), (False, True, True, False), (False, True, False, True)))
def test_authenticated_real_bridge_held_drain_lost_reply_and_management_reconstruction(real_boundary, tmp_path: Path, monkeypatch, _use_real_commands: None, _use_real_sleep: None, expire_drain: bool, reconstruct_supervisor: bool, tamper_digest: bool, downgrade: bool) -> None:
    """Exercise real PID/UDS/HTTP signals from OAuth through a persisted MCP operation."""
    base, _base_grants, identity, signed, _uids, state = real_boundary
    origin = base.oauth.canonical_issuer
    oauth = OAuthSettings(base.oauth.database_path, origin, base.oauth.firebase_project_id, (OAuthClient("alice-client", (origin + "/alice-callback",), allowed_scopes=(READ_SCOPE, DEPLOY_SCOPE)),), enabled_scopes=(READ_SCOPE, DEPLOY_SCOPE))
    policy = tmp_path / "operator.yaml"
    policy.write_text("profile: beta\ntargets:\n  beta-worker:\n    channel: beta\n    repositories: [fixture/beta]\n", encoding="utf-8")
    settings = ManagementSettings(tmp_path, oauth, port=base.port, operating_policy_path=policy, operation_state_root=tmp_path / "journal")
    grants = OAuthGrantService(oauth, identity, clock=lambda: state["now"])
    supervisor = _supervisor(tmp_path / "child")
    listener, worker_client = _bridge(supervisor, b"s" * 48)
    original_dispatch = worker_client.dispatch_worker_operation
    requests: list[str] = []

    def disconnect_after_effect(operation_id, operation, **kwargs):
        result = original_dispatch(operation_id, operation, **kwargs)
        requests.append(operation_id)
        if operation == "REQUEST_GRACEFUL_STOP":
            return WorkerNetworkResult(0, "UNKNOWN", None, result.supervisor_generation, result.request_id)
        return result

    monkeypatch.setattr(worker_client, "dispatch_worker_operation", disconnect_after_effect)
    first_app = create_management_app(settings, grants, worker_clients={"beta": worker_client})
    supervisor.apply({"operation_id": "initial-real-worker", "operation": "START"})
    try:
        for _ in range(250):
            actual = worker_client.inspect_supervisor()
            worker = actual.data.get("worker") if isinstance(actual.data, dict) else None
            if isinstance(worker, dict) and isinstance(worker.get("process_run_id"), str):
                break
            time.sleep(0.02)
        else:
            raise AssertionError("actual worker did not register its run identity")
        assert worker is not None
        arguments = {
            "target_id": "beta-worker",
            "idempotency_key": "real-stop-after-disconnect",
            "expected_supervisor_generation": actual.supervisor_generation,
            "expected_process_run_id": worker["process_run_id"],
            "expected_deployment_generation": worker["deployment_generation"],
            "expected_runtime_id": worker["runtime_id"],
        }
        with TestClient(first_app, base_url=origin) as client:
            bearer = _issue(client, settings, signed, "alice", "alice-client", READ_SCOPE + " " + DEPLOY_SCOPE)["access_token"]
            tool = "auto_coder_restart_worker" if downgrade else "auto_coder_stop_worker"
            admitted = _call(client, bearer, tool, arguments)
            operation_id = str(admitted["operation_id"])
            for _ in range(100):
                _drive(first_app, operation_id, 1)
                if (tmp_path / "child" / "fixture-draining").exists():
                    break
                time.sleep(0.02)
            else:
                raise AssertionError("real invocation never entered cooperative drain")
            assert supervisor.child is not None and supervisor.child.poll() is None
            assert _call(client, bearer, "auto_coder_operation_get", {"target_id": "beta-worker", "operation_id": operation_id})["status"] == "DRAINING"
            if expire_drain:
                journal = first_app.state.management_runtime.journal
                persisted = journal.lifecycle_snapshot(operation_id)
                assert persisted is not None
                revision, snapshot = persisted
                snapshot["deadline"] = time.time() - 1
                assert journal.lifecycle_save(operation_id, revision, snapshot)
                _drive(first_app, operation_id, 1)
                assert _call(client, bearer, "auto_coder_operation_get", {"target_id": "beta-worker", "operation_id": operation_id})["status"] == "NEEDS_ATTENTION"
                assert supervisor.child.poll() is None

        # A new management process starts with no cached signed Supervisor
        # identity. It must bootstrap before reading the original receipt.
        if reconstruct_supervisor:
            listener.close()
            supervisor = WorkerSupervisor(supervisor.config)
            if tamper_digest:
                saved = supervisor.store.read()
                saved["operations"][requests[0]]["digest"] = "0" * 64
                supervisor.store.write(saved)
            listener, reconstructed_client = _bridge(supervisor, b"s" * 48, port=worker_client.config.port)
            assert reconstructed_client.config.binding_digest == worker_client.config.binding_digest
        else:
            reconstructed_client = WorkerNetworkClient(worker_client.config)
        assert reconstructed_client.supervisor_generation is None
        next_app = create_management_app(settings, grants, worker_clients={"beta": reconstructed_client})
        with TestClient(next_app, base_url=origin) as client:
            if downgrade:
                policy.write_text(policy.read_text().replace("profile: beta", "profile: observe"), encoding="utf-8")
            (tmp_path / "child" / "fixture-release").write_text("release", encoding="utf-8")
            for _ in range(200):
                observed = next_app.state.management_runtime.journal.lifecycle_snapshot(operation_id)
                if observed and (
                    (not expire_drain and not tamper_digest and not downgrade and observed[1]["stage"] == "SUCCEEDED")
                    or ((tamper_digest or downgrade) and observed[1]["stage"] == "NEEDS_ATTENTION")
                    or (expire_drain and observed[1]["stage"] == "NEEDS_ATTENTION" and observed[1]["steps"]["drain"]["state"] == "CONFIRMED")
                ):
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("real Supervisor stop receipt was not reconciled")
            assert reconstructed_client.supervisor_generation == supervisor.incarnation
            assert _call(client, bearer, "auto_coder_operation_get", {"target_id": "beta-worker", "operation_id": operation_id})["status"] == ("NEEDS_ATTENTION" if expire_drain or tamper_digest or downgrade else "SUCCEEDED")
            assert _call(client, bearer, tool, arguments)["operation_id"] == operation_id
            assert len(requests) == 1
            if not tamper_digest:
                assert reconstructed_client.inspect_supervisor().data["status"] == "STOPPED"
            if downgrade:
                snapshot = next_app.state.management_runtime.journal.lifecycle_snapshot(operation_id)
                assert snapshot is not None and "start" not in snapshot[1]["steps"]
    finally:
        (tmp_path / "child" / "fixture-release").write_text("release", encoding="utf-8")
        listener.close()
        if supervisor.child is not None and supervisor.child.poll() is None:
            supervisor.child.terminate()
            supervisor.child.wait(timeout=5)


@pytest.mark.parametrize("channel", ("beta", "release"))
@pytest.mark.parametrize("restart", (False, True))
def test_authenticated_bootstrap_start_reply_loss_survives_supervisor_reconstruction(real_boundary, tmp_path: Path, monkeypatch, _use_real_commands: None, _use_real_sleep: None, restart: bool, channel: str) -> None:
    base, _base_grants, identity, signed, _uids, state = real_boundary
    origin = base.oauth.canonical_issuer
    oauth = OAuthSettings(base.oauth.database_path, origin, base.oauth.firebase_project_id, (OAuthClient("alice-client", (origin + "/alice-callback",), allowed_scopes=(READ_SCOPE, DEPLOY_SCOPE)),), enabled_scopes=(READ_SCOPE, DEPLOY_SCOPE))
    policy = tmp_path / "operator.yaml"
    profile = "full" if channel == "release" else "beta"
    target = f"{channel}-worker"
    policy.write_text(f"profile: {profile}\ntargets:\n  {target}:\n    channel: {channel}\n    repositories: [fixture/{channel}]\n", encoding="utf-8")
    settings = ManagementSettings(tmp_path, oauth, port=base.port, operating_policy_path=policy, operation_state_root=tmp_path / "journal")
    grants = OAuthGrantService(oauth, identity, clock=lambda: state["now"])
    monkeypatch.setenv("AUTO_CODER_TEST_CONFIG_REPORT", "1")
    monkeypatch.delenv("AUTO_CODER_ISSUE_ALLOWLIST", raising=False)
    monkeypatch.delenv("AUTO_CODER_PR_ALLOWLIST", raising=False)
    supervisor = _supervisor(tmp_path / "child", channel)
    config_dir = supervisor.config.workspace_root / ".auto-coder"
    config_dir.mkdir()
    config_file = config_dir / "config.toml"
    config_file.write_text("[github]\nissue_allowlist = [11]\npr_allowlist = [12]\n")
    listener, worker_client = _bridge(supervisor, b"s" * 48)
    assert supervisor.apply({"operation_id": "initial", "operation": "START"})["status"] == "RUNNING"
    original_child = supervisor.child
    new_child = None
    try:
        for _ in range(300):
            worker = supervisor.inspect().get("worker")
            if isinstance(worker, dict) and worker.get("process_run_id"):
                break
            time.sleep(0.02)
        assert isinstance(worker, dict) and worker.get("process_run_id")
        report = tmp_path / "child" / "fixture-config-observed.json"
        assert json.loads(report.read_text()) == {"issue": [11], "pr": [12]}
        assert original_child is not None
        if not restart:
            assert supervisor.apply({"operation_id": "initial-stop", "operation": "REQUEST_GRACEFUL_STOP"})["status"] == "DRAINING"
            (tmp_path / "child" / "fixture-release").write_text("release")
            original_child.wait(timeout=10)
            assert supervisor.apply({"operation_id": "initial-stop", "operation": "REQUEST_GRACEFUL_STOP"})["status"] == "STOPPED"
            (tmp_path / "child" / "fixture-release").unlink()
        config_file.write_text("[github]\nissue_allowlist = [21]\npr_allowlist = [22, 23]\n")
        original_dispatch = worker_client.dispatch_worker_operation
        dispatches = []

        def lose_start(operation_id, operation, **kwargs):
            result = original_dispatch(operation_id, operation, **kwargs)
            if operation == "START":
                dispatches.append(operation_id)
                return WorkerNetworkResult(0, "UNKNOWN", None, result.supervisor_generation, result.request_id)
            return result

        monkeypatch.setattr(worker_client, "dispatch_worker_operation", lose_start)
        app = create_management_app(settings, grants, worker_clients={channel: worker_client})
        with TestClient(app, base_url=origin) as client:
            bearer = _issue(client, settings, signed, "alice", "alice-client", READ_SCOPE + " " + DEPLOY_SCOPE)["access_token"]
            arguments = {"target_id": target, "idempotency_key": "lost-start", "expected_supervisor_generation": supervisor.incarnation, "expected_runtime_id": "bootstrap"}
            tool = "auto_coder_restart_worker" if restart else "auto_coder_start_worker"
            if restart:
                arguments.update(expected_process_run_id=worker["process_run_id"], expected_deployment_generation=worker["deployment_generation"])
            accepted = _call(client, bearer, tool, arguments)
            operation_id = str(accepted["operation_id"])
            for _ in range(100):
                _drive(app, operation_id, 1)
                if restart and (tmp_path / "child" / "fixture-draining").exists() and original_child.poll() is None:
                    hold = tmp_path / "child" / "fixture-hold-terminal-exit"
                    if channel == "release":
                        hold.write_text("hold actual OS exit")
                    (tmp_path / "child" / "fixture-release").write_text("release")
                    if channel == "release":
                        for _ in range(200):
                            if (tmp_path / "child" / "fixture-terminal-exit-held").exists():
                                break
                            time.sleep(0.02)
                        else:
                            raise AssertionError("real worker did not persist terminal receipt before held exit")
                        terminal = json.loads((tmp_path / "child" / "last-terminal.json").read_text())
                        assert terminal["process_run_id"] == worker["process_run_id"]
                        assert terminal["deployment_generation"] == worker["deployment_generation"]
                        _drive(app, operation_id, 8)
                        assert original_child.poll() is None
                        assert dispatches == []
                        snapshot = app.state.management_runtime.journal.lifecycle_snapshot(operation_id)
                        assert snapshot is not None and "start" not in snapshot[1]["steps"]
                        assert _call(client, bearer, tool, arguments)["operation_id"] == operation_id
                        competing = _call(client, bearer, tool, {**arguments, "idempotency_key": "competing-held-exit"})
                        assert competing["status"] == "BUSY"
                        hold.unlink()
                    original_child.wait(timeout=10)
                    (tmp_path / "child" / "fixture-release").unlink()
                if dispatches:
                    break
                time.sleep(0.02)
            assert len(dispatches) == 1
            saved = supervisor.store.read()["worker"]
            pid, generation = saved["pid"], saved["deployment_generation"]
            new_child = supervisor.child
        listener.close()
        supervisor = WorkerSupervisor(supervisor.config)
        listener, reconstructed_client = _bridge(supervisor, b"s" * 48, port=worker_client.config.port)
        assert reconstructed_client.config.binding_digest == worker_client.config.binding_digest
        reconstructed = create_management_app(settings, grants, worker_clients={channel: reconstructed_client})
        with TestClient(reconstructed, base_url=origin) as client:
            for _ in range(300):
                _drive(reconstructed, operation_id, 1)
                snapshot = reconstructed.state.management_runtime.journal.lifecycle_snapshot(operation_id)
                if snapshot and snapshot[1]["stage"] == "RECONCILING":
                    break
                time.sleep(0.02)
            assert snapshot is not None and snapshot[1]["stage"] == "RECONCILING"
            assert json.loads(report.read_text()) == {"issue": [21], "pr": [22, 23]}
            current = supervisor.inspect()["worker"]
            assert current["pid"] == pid and current["deployment_generation"] == generation
            assert _call(client, bearer, tool, arguments)["operation_id"] == operation_id
            assert len(dispatches) == 1
            assert _call(client, bearer, "auto_coder_operation_get", {"target_id": target, "operation_id": operation_id})["status"] != "SUCCEEDED"
    finally:
        (tmp_path / "child" / "fixture-hold-terminal-exit").unlink(missing_ok=True)
        (tmp_path / "child" / "fixture-release").write_text("release")
        listener.close()
        if new_child is not None and new_child.poll() is None:
            new_child.terminate()
            new_child.wait(timeout=10)


def test_concurrent_status_cannot_rebind_client_during_lifecycle_effect(real_boundary, tmp_path: Path, monkeypatch) -> None:
    from auto_coder.operating_policy import ActionClass

    base, _base_grants, identity, signed, _uids, state = real_boundary
    origin = base.oauth.canonical_issuer
    oauth = OAuthSettings(base.oauth.database_path, origin, base.oauth.firebase_project_id, (OAuthClient("alice-client", (origin + "/alice-callback",), allowed_scopes=(READ_SCOPE, DEPLOY_SCOPE)),), enabled_scopes=(READ_SCOPE, DEPLOY_SCOPE))
    policy = tmp_path / "operator.yaml"
    policy.write_text("profile: beta\ntargets:\n  beta-worker:\n    channel: beta\n    repositories: [fixture/beta]\n")
    settings = ManagementSettings(tmp_path, oauth, port=base.port, operating_policy_path=policy, operation_state_root=tmp_path / "journal")
    bridge = LifecycleBridge()
    app = create_management_app(settings, OAuthGrantService(oauth, identity, clock=lambda: state["now"]), worker_clients={"beta": bridge})  # type: ignore[dict-item]

    async def no_background_monitor() -> None:
        # Admission is awaited below; this test drives its only later lifecycle step.
        pass

    runtime = app.state.management_runtime
    monkeypatch.setattr(runtime, "start_monitor", no_background_monitor)
    with TestClient(app, base_url=origin) as client:
        bearer = _issue(client, settings, signed, "alice", "alice-client", READ_SCOPE + " " + DEPLOY_SCOPE)["access_token"]
        arguments = {"target_id": "beta-worker", "idempotency_key": "concurrent-fence", "expected_supervisor_generation": "supervisor-a", "expected_process_run_id": "run-old", "expected_deployment_generation": "generation-old", "expected_runtime_id": "runtime-old"}
        operation = _call(client, bearer, "auto_coder_restart_worker", arguments)
        record = runtime.journal.get(operation["operation_id"]).record
        assert record is not None
        resolved = runtime._resolve("beta-worker", ActionClass.INSPECT)
        entered, release, status_attempted, status_contended, status_entered = (threading.Event() for _ in range(5))
        original_inspect = bridge.inspect_supervisor
        original_lock = runtime._worker_locks["beta-worker"]
        status_thread: threading.Thread | None = None

        class ObservedLock:
            def __enter__(self):
                if threading.current_thread() is status_thread:
                    acquired = original_lock.acquire(blocking=False)
                    if not acquired:
                        status_contended.set()
                    status_attempted.set()
                    if not acquired:
                        original_lock.acquire()
                else:
                    original_lock.acquire()
                return self

            def __exit__(self, *_args):
                original_lock.release()

        def held_process(_record):
            entered.set()
            assert release.wait(timeout=10)
            return _record

        def observed_inspect():
            if threading.current_thread() is status_thread:
                status_entered.set()
            return original_inspect()

        def observe_status():
            nonlocal status_thread
            status_thread = threading.current_thread()
            return runtime._observe(resolved)

        monkeypatch.setitem(runtime._worker_locks, "beta-worker", ObservedLock())
        monkeypatch.setattr(runtime.lifecycle, "_process_locked", held_process)
        monkeypatch.setattr(bridge, "inspect_supervisor", observed_inspect)
        # An unrelated client observation must not satisfy this oracle.
        unrelated = threading.Thread(target=bridge.inspect_supervisor)
        unrelated.start()
        unrelated.join(timeout=5)
        assert not unrelated.is_alive()
        assert not status_entered.is_set()
        with ThreadPoolExecutor(max_workers=2) as executor:
            pending = executor.submit(runtime.lifecycle.process, record)
            try:
                assert entered.wait(timeout=5)
                status = executor.submit(observe_status)
                assert status_attempted.wait(timeout=5)
                assert status_contended.is_set()
                assert not status_entered.is_set()
            finally:
                release.set()
            assert pending.result(timeout=5) is record
            status.result(timeout=5)
            assert status_entered.is_set()


@pytest.mark.parametrize("receipt_state", ("complete", "pending", "digest-conflict", "missing", "expired-pending"))
def test_supervisor_reconstruction_between_identity_checks_retains_original_effect(real_boundary, tmp_path: Path, monkeypatch, receipt_state: str) -> None:
    """Control typed Supervisor observations around the real dispatcher and SQLite journal."""
    base, _base_grants, identity, signed, _uids, state = real_boundary
    origin = base.oauth.canonical_issuer
    oauth = OAuthSettings(base.oauth.database_path, origin, base.oauth.firebase_project_id, (OAuthClient("alice-client", (origin + "/alice-callback",), allowed_scopes=(READ_SCOPE, DEPLOY_SCOPE)),), enabled_scopes=(READ_SCOPE, DEPLOY_SCOPE))
    policy = tmp_path / "operator.yaml"
    policy.write_text("profile: beta\ntargets:\n  beta-worker:\n    channel: beta\n    repositories: [fixture/beta]\n", encoding="utf-8")
    settings = ManagementSettings(tmp_path, oauth, port=base.port, operating_policy_path=policy, operation_state_root=tmp_path / "journal")
    bridge = LifecycleBridge()
    bridge.selected = "bootstrap"
    assert bridge.worker is not None
    bridge.worker["runtime_id"] = "bootstrap"
    app = create_management_app(settings, OAuthGrantService(oauth, identity, clock=lambda: state["now"]), worker_clients={"beta": bridge})  # type: ignore[dict-item]
    runtime = app.state.management_runtime

    async def no_background_monitor() -> None:
        # Every lifecycle iteration is driven explicitly at the identity boundary.
        pass

    monkeypatch.setattr(runtime, "start_monitor", no_background_monitor)
    with TestClient(app, base_url=origin) as client:
        bearer = _issue(client, settings, signed, "alice", "alice-client", READ_SCOPE + " " + DEPLOY_SCOPE)["access_token"]
        arguments = {"target_id": "beta-worker", "idempotency_key": "owner-changes-between-observations", "expected_supervisor_generation": "supervisor-a", "expected_process_run_id": "run-old", "expected_deployment_generation": "generation-old", "expected_runtime_id": "bootstrap"}
        admitted = _call(client, bearer, "auto_coder_stop_worker", arguments)
        operation_id = str(admitted["operation_id"])
        effect_id = "d-" + operation_id
        for _ in range(8):
            _drive(app, operation_id, 1)
            if effect_id in bridge.receipts:
                break
        else:
            raise AssertionError("original drain was never dispatched")
        original = runtime.journal.lifecycle_snapshot(operation_id)
        original_record = runtime.journal.get(operation_id).record
        assert original is not None and original[1]["stage"] == "DRAINING"
        assert original[1]["steps"]["drain"]["state"] == "SENT_UNKNOWN"
        original_receipt = dict(bridge.receipts[effect_id])
        actions = list(bridge.actions)
        original_inspect = bridge.inspect_supervisor
        inspections = 0

        def reconstruct_between_checks() -> WorkerNetworkResult:
            nonlocal inspections
            inspections += 1
            observed = original_inspect()
            if inspections == 1:
                # The first check saw the old owner; the step sees its replacement.
                bridge.supervisor_generation = "supervisor-reconstructed"
            return observed

        monkeypatch.setattr(bridge, "inspect_supervisor", reconstruct_between_checks)
        _drive(app, operation_id, 1)
        assert inspections == 2
        assert runtime.journal.lifecycle_snapshot(operation_id) == original
        assert runtime.journal.get(operation_id).record == original_record
        assert bridge.actions == actions
        monkeypatch.setattr(bridge, "inspect_supervisor", original_inspect)

        if receipt_state in {"pending", "expired-pending"}:
            bridge.receipts[effect_id] = {**original_receipt, "status": "DRAINING"}
        elif receipt_state == "digest-conflict":
            bridge.receipts[effect_id] = {**original_receipt, "intent_digest": "0" * 64}
        elif receipt_state == "missing":
            monkeypatch.setattr(bridge, "read_worker_operation", lambda _operation: bridge._result("UNKNOWN", {}))
        if receipt_state == "expired-pending":
            monkeypatch.setattr("auto_coder.management.worker_lifecycle.time", SimpleNamespace(time=lambda: original[1]["deadline"] + 1))

        _drive(app, operation_id, 8)
        if receipt_state == "pending":
            assert runtime.journal.lifecycle_snapshot(operation_id) == original
            assert bridge.actions == actions
            bridge.receipts[effect_id] = original_receipt
            _drive(app, operation_id, 8)
        result = _call(client, bearer, "auto_coder_operation_get", {"target_id": "beta-worker", "operation_id": operation_id})
        final = runtime.journal.lifecycle_snapshot(operation_id)
        assert final is not None
        assert final[1]["deadline"] == original[1]["deadline"]
        assert final[1]["original_deadline"] == original[1]["original_deadline"]
        assert all(final[1]["steps"]["drain"][field] == original[1]["steps"]["drain"][field] for field in ("operation_id", "sent_at", "owner_generation", "attempt_id", "correlation_identity", "expected_digest"))
        assert bridge.actions == actions == [("REQUEST_GRACEFUL_STOP", effect_id)]
        if receipt_state in {"complete", "pending"}:
            assert result["status"] == "SUCCEEDED", result
            assert final[1]["current_supervisor_generation"] == "supervisor-reconstructed"
        else:
            assert result["status"] == "NEEDS_ATTENTION", result
            assert result["reason"] == "original_supervisor_incarnation_changed"
            assert _call(client, bearer, "auto_coder_stop_worker", {**arguments, "idempotency_key": "competing-after-owner-change"})["status"] == "BUSY"
        assert _call(client, bearer, "auto_coder_stop_worker", arguments)["operation_id"] == operation_id


def test_full_policy_allows_release_process_restart_but_never_beta_source_update(real_boundary, tmp_path: Path) -> None:
    base, _base_grants, identity, signed, _uids, state = real_boundary
    origin = base.oauth.canonical_issuer
    oauth = OAuthSettings(base.oauth.database_path, origin, base.oauth.firebase_project_id, (OAuthClient("alice-client", (origin + "/alice-callback",), allowed_scopes=(READ_SCOPE, DEPLOY_SCOPE)),), enabled_scopes=(READ_SCOPE, DEPLOY_SCOPE))
    policy = tmp_path / "operator.yaml"
    policy.write_text("profile: full\ntargets:\n  release-worker:\n    channel: release\n    repositories: [fixture/release]\n", encoding="utf-8")
    settings = ManagementSettings(tmp_path, oauth, port=base.port, operating_policy_path=policy, operation_state_root=tmp_path / "journal")
    bridge = LifecycleBridge()
    bridge.config = WorkerEndpointConfig("release-worker", "release", "fixture/release", "10.23.10.11", 17890, b"r" * 48)
    app = create_management_app(settings, OAuthGrantService(oauth, identity, clock=lambda: state["now"]), worker_clients={"release": bridge})  # type: ignore[dict-item]
    with TestClient(app, base_url=origin) as client:
        bearer = _issue(client, settings, signed, "alice", "alice-client", READ_SCOPE + " " + DEPLOY_SCOPE)["access_token"]
        arguments = {"target_id": "release-worker", "idempotency_key": "release-restart", "expected_supervisor_generation": "supervisor-a", "expected_process_run_id": "run-old", "expected_deployment_generation": "generation-old", "expected_runtime_id": "runtime-old"}
        denied = _call(client, bearer, "auto_coder_deploy_beta_source", {**arguments, "selected_git_sha": SHA_A})
        assert denied["status"] == "DENIED"
        before = _call(client, bearer, "auto_coder_deployment_status", {"target_id": "release-worker"})
        assert before["status"] == "OK" and before["process_state"] == "RUNNING"
        accepted = _call(client, bearer, "auto_coder_restart_worker", arguments)
        operation_id = str(accepted["operation_id"])
        _drive(app, operation_id)
        completed = _call(client, bearer, "auto_coder_operation_get", {"target_id": "release-worker", "operation_id": operation_id})
        assert completed["status"] == "SUCCEEDED", completed
        assert [name for name, _id in bridge.actions] == ["REQUEST_GRACEFUL_STOP", "START"]


def test_two_beta_targets_deploy_independently_and_keep_distinct_journal_lanes(real_boundary, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Use authenticated MCP admission and the production lifecycle dispatcher."""
    base, _base_grants, identity, signed, _uids, state = real_boundary
    origin = base.oauth.canonical_issuer
    oauth = OAuthSettings(base.oauth.database_path, origin, base.oauth.firebase_project_id, (OAuthClient("alice-client", (origin + "/alice-callback",), allowed_scopes=(READ_SCOPE, DEPLOY_SCOPE)),), enabled_scopes=(READ_SCOPE, DEPLOY_SCOPE))
    policy = tmp_path / "operator.yaml"
    policy.write_text(
        "profile: full\ntargets:\n  release-worker:\n    channel: release\n    repositories: [fixture/release]\n  beta-worker:\n    channel: beta\n    repositories: [fixture/beta]\n  beta-agent-table-worker:\n    channel: beta\n    repositories: [fixture/agent-table]\n",
        encoding="utf-8",
    )
    source_token = tmp_path / "source-token"
    source_token.write_text("x" * 40, encoding="utf-8")
    settings = ManagementSettings(tmp_path, oauth, port=base.port, operating_policy_path=policy, operation_state_root=tmp_path / "journal", source_repository="owner/repo", source_evidence_token_file=source_token)
    grants = OAuthGrantService(oauth, identity, clock=lambda: state["now"])
    remote = GitEvidenceBoundary()
    assert selector(remote).advance("r77", None).certainty is MutationCertainty.CONFIRMED
    reader = BetaEvidenceReader(remote, "owner/repo")  # type: ignore[arg-type]
    monkeypatch.setattr("auto_coder.management.worker_lifecycle.WorkerLifecycle._source", lambda _self: reader)
    outliner = LifecycleBridge()
    agent_table = LifecycleBridge()
    agent_table.config = WorkerEndpointConfig("beta-agent-table-worker", "beta", "fixture/agent-table", "10.23.10.13", 17890, b"c" * 48)
    release = LifecycleBridge()
    release.config = WorkerEndpointConfig("release-worker", "release", "fixture/release", "10.23.10.11", 17890, b"r" * 48)
    app = create_management_app(settings, grants, worker_clients={"outliner": outliner, "agent-table": agent_table, "release": release})  # type: ignore[dict-item]

    async def no_background_monitor() -> None:
        pass

    monkeypatch.setattr(app.state.management_runtime, "start_monitor", no_background_monitor)
    with TestClient(app, base_url=origin) as client:
        bearer = _issue(client, settings, signed, "alice", "alice-client", READ_SCOPE + " " + DEPLOY_SCOPE)["access_token"]
        for target_id, repository in (("beta-worker", "fixture/beta"), ("beta-agent-table-worker", "fixture/agent-table"), ("release-worker", "fixture/release")):
            status = _call(client, bearer, "auto_coder_deployment_status", {"target_id": target_id})
            assert status["status"] == "OK" and status["target_id"] == target_id and status["repository"] == repository

        def deploy(target_id: str) -> str:
            response = _call(
                client,
                bearer,
                "auto_coder_deploy_beta_source",
                {"target_id": target_id, "selected_git_sha": SHA_A, "idempotency_key": "shared-client-key", "expected_supervisor_generation": "supervisor-a", "expected_process_run_id": "run-old", "expected_deployment_generation": "generation-old", "expected_runtime_id": "runtime-old"},
            )
            assert isinstance(response.get("operation_id"), str), response
            operation_id = str(response["operation_id"])
            _drive(app, operation_id)
            result = _call(client, bearer, "auto_coder_operation_get", {"target_id": target_id, "operation_id": operation_id})
            assert result["status"] == "SUCCEEDED" and result["target_id"] == target_id, result
            return operation_id

        agent_operation = deploy("beta-agent-table-worker")
        assert agent_table.selected == "runtime-new" and outliner.selected == release.selected == "runtime-old"
        assert not outliner.actions and not release.actions
        assert _call(client, bearer, "auto_coder_operation_get", {"target_id": "beta-worker", "operation_id": agent_operation})["status"] == "NOT_FOUND"

        outliner_operation = deploy("beta-worker")
        assert outliner_operation != agent_operation
        assert agent_table.selected == outliner.selected == "runtime-new" and release.selected == "runtime-old"
        assert [name for name, _ in agent_table.actions] == ["PREPARE", "REQUEST_GRACEFUL_STOP", "START"]
        assert [name for name, _ in outliner.actions] == ["PREPARE", "REQUEST_GRACEFUL_STOP", "START"]
        assert not release.actions

        agent_table.inspect_supervisor = lambda: WorkerNetworkResult(0, "UNKNOWN", None, "", "unreachable")  # type: ignore[method-assign]
        assert _call(client, bearer, "auto_coder_deployment_status", {"target_id": "beta-agent-table-worker"})["status"] == "UNAVAILABLE"
        assert _call(client, bearer, "auto_coder_deployment_status", {"target_id": "beta-worker"})["status"] == "OK"
        assert _call(client, bearer, "auto_coder_deployment_status", {"target_id": "release-worker"})["status"] == "OK"

    changed_bridge = LifecycleBridge()
    changed_bridge.config = WorkerEndpointConfig("beta-agent-table-worker", "beta", "fixture/agent-table", "10.23.10.19", 17890, b"z" * 48)
    restarted = create_management_app(settings, grants, worker_clients={"beta-agent-table-worker": changed_bridge})  # type: ignore[dict-item]
    original = restarted.state.management_runtime.journal.get(agent_operation).record
    assert original is not None
    assert restarted.state.management_runtime._record_client(original) is None
    assert changed_bridge.actions == []


@pytest.mark.parametrize("unavailable_target", ("beta-worker", "beta-agent-table-worker"))
def test_monitor_recovers_other_workers_after_one_beta_bridge_is_lost(real_boundary, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unavailable_target: str) -> None:
    """Run the real monitor, dispatcher and lifecycle handlers on three lanes."""
    base, _base_grants, identity, signed, _uids, state = real_boundary
    origin = base.oauth.canonical_issuer
    oauth = OAuthSettings(base.oauth.database_path, origin, base.oauth.firebase_project_id, (OAuthClient("alice-client", (origin + "/alice-callback",), allowed_scopes=(READ_SCOPE, DEPLOY_SCOPE)),), enabled_scopes=(READ_SCOPE, DEPLOY_SCOPE))
    policy = tmp_path / "operator.yaml"
    policy.write_text(
        "profile: full\ntargets:\n  release-worker:\n    channel: release\n    repositories: [fixture/release]\n  beta-worker:\n    channel: beta\n    repositories: [fixture/beta]\n  beta-agent-table-worker:\n    channel: beta\n    repositories: [fixture/agent-table]\n",
        encoding="utf-8",
    )
    settings = ManagementSettings(tmp_path, oauth, port=base.port, operating_policy_path=policy, operation_state_root=tmp_path / "journal")
    bridges = {target_id: LifecycleBridge() for target_id in ("release-worker", "beta-worker", "beta-agent-table-worker")}
    for target_id, channel, repository, address, secret in (
        ("release-worker", "release", "fixture/release", "10.23.10.11", b"r" * 48),
        ("beta-worker", "beta", "fixture/beta", "10.23.10.12", b"b" * 48),
        ("beta-agent-table-worker", "beta", "fixture/agent-table", "10.23.10.13", b"a" * 48),
    ):
        bridge = bridges[target_id]
        bridge.config = WorkerEndpointConfig(target_id, channel, repository, address, 17890, secret, target_id)
        assert bridge.worker is not None
        bridge.worker["process_run_id"] = f"{target_id}-original-run"

    app = create_management_app(settings, OAuthGrantService(oauth, identity, clock=lambda: state["now"]), worker_clients=bridges)  # type: ignore[arg-type]
    runtime = app.state.management_runtime

    # Keep scheduling deterministic; the real poll enters the real dispatcher
    # and WorkerLifecycle handler below. Neither is stubbed or replaced.
    async def manual_monitor() -> None:
        pass

    monkeypatch.setattr(runtime, "start_monitor", manual_monitor)
    with TestClient(app, base_url=origin) as http:
        bearer = _issue(http, settings, signed, "alice", "alice-client", READ_SCOPE + " " + DEPLOY_SCOPE)["access_token"]
        operation_ids: dict[str, str] = {}
        for target_id, bridge in bridges.items():
            response = _call(
                http,
                bearer,
                "auto_coder_restart_worker",
                {
                    "target_id": target_id,
                    "idempotency_key": "same-client-key",
                    "expected_supervisor_generation": bridge.supervisor_generation,
                    "expected_process_run_id": f"{target_id}-original-run",
                    "expected_deployment_generation": "generation-old",
                    "expected_runtime_id": "runtime-old",
                },
            )
            assert isinstance(response.get("operation_id"), str), response
            operation_ids[target_id] = str(response["operation_id"])
            record = runtime.journal.get(operation_ids[target_id]).record
            assert record is not None and record.intent.target.repository == bridge.config.repository
            assert runtime.journal.lifecycle_snapshot(record.operation_id)[1]["stage"] == "DRAINING"  # type: ignore[index]

        unavailable = bridges[unavailable_target]
        unavailable.inspect_supervisor = lambda: WorkerNetworkResult(503, "UNAVAILABLE", None, "", "bridge-down")  # type: ignore[method-assign]
        healthy_targets = set(bridges) - {unavailable_target}
        assert http.portal is not None
        for _ in range(18):
            http.portal.call(runtime._poll_lifecycle_once)
            if all(runtime.journal.get(operation_ids[target_id]).record.phase.value == "succeeded" for target_id in healthy_targets):  # type: ignore[union-attr]
                break

        for target_id, bridge in bridges.items():
            operation_id = operation_ids[target_id]
            record = runtime.journal.get(operation_id).record
            assert record is not None
            assert record.intent.target.repository == bridge.config.repository
            projection = _call(http, bearer, "auto_coder_operation_get", {"target_id": target_id, "operation_id": operation_id})
            assert projection["target_id"] == target_id and projection["repository"] == bridge.config.repository
            observed = _call(http, bearer, "auto_coder_deployment_status", {"target_id": target_id})
            if target_id == unavailable_target:
                assert record.phase.value != "succeeded" and projection["status"] != "SUCCEEDED"
                assert observed["status"] == "UNAVAILABLE" and observed["target_id"] == target_id
                assert runtime.journal.lifecycle_snapshot(operation_id)[1]["stage"] == "DRAINING"  # type: ignore[index]
                assert bridge.actions == [] and bridge.selected == "runtime-old"
                assert bridge.worker is not None and bridge.worker["process_run_id"] == f"{target_id}-original-run"
            else:
                assert record.phase.value == "succeeded" and projection["status"] == "SUCCEEDED", projection
                assert observed["status"] == "OK" and observed["target_id"] == target_id
                assert [action for action, _ in bridge.actions] == ["REQUEST_GRACEFUL_STOP", "START"]
                assert all(operation_id in step_id for _, step_id in bridge.actions)
                assert bridge.selected == "runtime-old"
                assert bridge.worker is not None and bridge.worker["process_run_id"] == "run-new"
