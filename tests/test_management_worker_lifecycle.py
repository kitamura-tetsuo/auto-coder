"""Exact-source proof and authenticated lifecycle journal regressions."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from auto_coder.beta_source_selection import MutationCertainty
from auto_coder.management.action_scopes import DEPLOY_SCOPE, READ_SCOPE
from auto_coder.management.app import create_management_app
from auto_coder.management.oauth_config import ManagementSettings, OAuthClient, OAuthSettings
from auto_coder.management.oauth_grants import OAuthGrantService
from auto_coder.management.source_evidence import BetaEvidenceReader
from auto_coder.management.worker_network_client import WorkerEndpointConfig, WorkerNetworkResult
from auto_coder.util.gh_cache import GitDataResponse
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


@pytest.mark.parametrize("intervention", ("none", "selection_changed", "source_unavailable", "policy_downgrade", "lost_stop_response", "crash_after_start"))
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
    bridge.lost_stop_response = intervention == "lost_stop_response"
    bridge.crash_new = intervention == "crash_after_start"
    app = create_management_app(settings, grants, worker_clients={"beta": bridge})  # type: ignore[dict-item]
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


@pytest.mark.parametrize("expire_drain", (False, True))
def test_authenticated_real_bridge_held_drain_lost_reply_and_management_reconstruction(real_boundary, tmp_path: Path, monkeypatch, _use_real_commands: None, _use_real_sleep: None, expire_drain: bool) -> None:
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
            admitted = _call(client, bearer, "auto_coder_stop_worker", arguments)
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

        # Rebuild the management service, but retain its original journal and
        # actual running Supervisor; the lost POST must never be dispatched twice.
        next_app = create_management_app(settings, grants, worker_clients={"beta": worker_client})
        with TestClient(next_app, base_url=origin) as client:
            (tmp_path / "child" / "fixture-release").write_text("release", encoding="utf-8")
            for _ in range(250):
                _drive(next_app, operation_id, 1)
                observed = next_app.state.management_runtime.journal.lifecycle_snapshot(operation_id)
                if observed and ((not expire_drain and observed[1]["stage"] == "SUCCEEDED") or (expire_drain and observed[1]["stage"] == "NEEDS_ATTENTION" and observed[1]["steps"]["drain"]["state"] == "CONFIRMED")):
                    break
                time.sleep(0.02)
            else:
                raise AssertionError("real Supervisor stop receipt was not reconciled")
            assert _call(client, bearer, "auto_coder_operation_get", {"target_id": "beta-worker", "operation_id": operation_id})["status"] == ("NEEDS_ATTENTION" if expire_drain else "SUCCEEDED")
            assert _call(client, bearer, "auto_coder_stop_worker", arguments)["operation_id"] == operation_id
            assert len(requests) == 1
            assert worker_client.inspect_supervisor().data["status"] == "STOPPED"
    finally:
        (tmp_path / "child" / "fixture-release").write_text("release", encoding="utf-8")
        listener.close()
        if supervisor.child is not None and supervisor.child.poll() is None:
            supervisor.child.terminate()
            supervisor.child.wait(timeout=5)


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
