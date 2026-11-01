"""Exact-source proof and authenticated lifecycle journal regressions."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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
        if operation == "REQUEST_GRACEFUL_STOP":
            receipt = {"status": "STOPPED", "operation_id": operation_id}
            self.worker = None
            result = self._result("UNKNOWN", {}) if self.lost_stop_response else self._result("DRAINING", {"status": "DRAINING", "operation_id": operation_id})
        elif operation == "START":
            self.selected = runtime_id or "runtime-old"
            self.worker = {"process_run_id": "run-new", "deployment_generation": "generation-new", "runtime_id": self.selected, "binding_status": "VERIFIED"}
            receipt = {"status": "RUNNING", "operation_id": operation_id, "worker": self.worker}
            result = self._result("RUNNING", receipt)
        elif operation == "RECOVER_PREVIOUS":
            self.selected = "runtime-old"
            self.worker = {"process_run_id": "run-restored", "deployment_generation": "generation-restored", "runtime_id": self.selected, "binding_status": "VERIFIED"}
            receipt = {"status": "RUNNING", "operation_id": operation_id, "worker": self.worker}
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
            listener, reconstructed_client = _bridge(supervisor, b"s" * 48)
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


@pytest.mark.parametrize("restart", (False, True))
def test_authenticated_bootstrap_start_reply_loss_survives_supervisor_reconstruction(real_boundary, tmp_path: Path, monkeypatch, _use_real_commands: None, _use_real_sleep: None, restart: bool) -> None:
    base, _base_grants, identity, signed, _uids, state = real_boundary
    origin = base.oauth.canonical_issuer
    oauth = OAuthSettings(base.oauth.database_path, origin, base.oauth.firebase_project_id, (OAuthClient("alice-client", (origin + "/alice-callback",), allowed_scopes=(READ_SCOPE, DEPLOY_SCOPE)),), enabled_scopes=(READ_SCOPE, DEPLOY_SCOPE))
    policy = tmp_path / "operator.yaml"
    policy.write_text("profile: beta\ntargets:\n  beta-worker:\n    channel: beta\n    repositories: [fixture/beta]\n", encoding="utf-8")
    settings = ManagementSettings(tmp_path, oauth, port=base.port, operating_policy_path=policy, operation_state_root=tmp_path / "journal")
    grants = OAuthGrantService(oauth, identity, clock=lambda: state["now"])
    monkeypatch.setenv("AUTO_CODER_TEST_CONFIG_REPORT", "1")
    monkeypatch.delenv("AUTO_CODER_ISSUE_ALLOWLIST", raising=False)
    monkeypatch.delenv("AUTO_CODER_PR_ALLOWLIST", raising=False)
    supervisor = _supervisor(tmp_path / "child")
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
        app = create_management_app(settings, grants, worker_clients={"beta": worker_client})
        with TestClient(app, base_url=origin) as client:
            bearer = _issue(client, settings, signed, "alice", "alice-client", READ_SCOPE + " " + DEPLOY_SCOPE)["access_token"]
            arguments = {"target_id": "beta-worker", "idempotency_key": "lost-start", "expected_supervisor_generation": supervisor.incarnation, "expected_runtime_id": "bootstrap"}
            tool = "auto_coder_restart_worker" if restart else "auto_coder_start_worker"
            if restart:
                arguments.update(expected_process_run_id=worker["process_run_id"], expected_deployment_generation=worker["deployment_generation"])
            accepted = _call(client, bearer, tool, arguments)
            operation_id = str(accepted["operation_id"])
            for _ in range(100):
                _drive(app, operation_id, 1)
                if restart and (tmp_path / "child" / "fixture-draining").exists() and original_child.poll() is None:
                    (tmp_path / "child" / "fixture-release").write_text("release")
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
        listener, reconstructed_client = _bridge(supervisor, b"s" * 48)
        reconstructed = create_management_app(settings, grants, worker_clients={"beta": reconstructed_client})
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
            assert _call(client, bearer, "auto_coder_operation_get", {"target_id": "beta-worker", "operation_id": operation_id})["status"] != "SUCCEEDED"
    finally:
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
        original_lock = runtime._worker_locks["beta"]
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

        monkeypatch.setitem(runtime._worker_locks, "beta", ObservedLock())
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
