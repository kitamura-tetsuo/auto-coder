"""Authenticated HTTP, real grant/SQLite, and dispatcher component regressions.

The physical two-container/UDS transport oracle is a separate Supervisor
integration suite; this file exercises management consumption of its typed API.
"""

from __future__ import annotations

import fcntl
import http.client
import json
import os
import sqlite3
import threading
import time
from dataclasses import replace

import pytest
from starlette.testclient import TestClient

from auto_coder.github_pending_work import WorkIdentity, resolve_pending_work_store
from auto_coder.management.action_scopes import OPERATE_SCOPE, READ_SCOPE
from auto_coder.management.app import create_management_app
from auto_coder.management.oauth_config import ManagementSettings, OAuthClient, OAuthSettings
from auto_coder.management.oauth_grants import OAuthGrantService
from auto_coder.management.operations import ActionParameter, AdmissionStatus, ManagedTarget, OperationIntent, OperationPhase
from auto_coder.management.worker_network_client import WorkerEndpointConfig, WorkerNetworkResult
from auto_coder.pr_review_cycle import (
    FIXED,
    VERDICT_FINDINGS,
    ContractSnapshot,
    Finding,
    FindingDisposition,
    PrReviewCycleRepository,
    RoundProvenance,
    StrongPolicyIdentity,
)
from auto_coder.pr_review_effects import AcceptedReviewPayload, EffectAttempt, ReviewEffectExecutor, ReviewEffectRepository
from auto_coder.runtime_locks import ensure_lock_directory, lock_path
from auto_coder.worker_network_protocol import MAX_RESPONSE_BYTES
from tests.test_management_mcp import _issue, _mcp, real_boundary
from tests.test_worker_supervisor_bridge import _bridge, _supervisor


class FixedWorkerBoundary:
    """Typed transport fixture; never an authority for end-to-end UDS claims."""

    def __init__(self, channel: str) -> None:
        self.config = WorkerEndpointConfig(f"{channel}-worker", channel, f"fixture/{channel}", "10.23.10.12", 17890, b"a" * 48)
        self.generation = f"{channel}-supervisor-1"
        self.run = f"{channel}-process-1"
        self.deployment = f"{channel}-deployment-1"
        self.ready = True
        self.healthy = True
        self.lost_response = False
        self.drop_before_receipt = False
        self.wrong_lock = False
        self.diagnostic_generation: str | None = None
        self.receipts: dict[str, dict[str, object]] = {}
        self.send_calls = 0
        self.read_calls = 0
        self.inspect_calls = 0

    def binding(self) -> dict[str, str]:
        return {
            "runtime_id": f"{self.config.channel}-runtime-1",
            "content_identity": "c" * 64,
            "lock_sha256": "d" * 64,
            "source_sha": "a" * 40,
            "distribution_version": "1.2.3",
            "process_run_id": self.run,
            "deployment_generation": self.deployment,
        }

    def inspect_supervisor(self) -> WorkerNetworkResult:
        self.inspect_calls += 1
        worker = {"process_run_id": self.run, "deployment_generation": self.deployment, "runtime_id": self.binding()["runtime_id"], "binding_status": "VERIFIED", "runtime_binding": self.binding()}
        data: dict[str, object] = {
            "supervisor_incarnation": self.generation,
            "status": "RUNNING" if self.healthy else "STOPPED",
            "alive": self.healthy,
            "selected_runtime_id": self.binding()["runtime_id"],
            "worker": worker if self.healthy else None,
        }
        return WorkerNetworkResult(200, "OK", data, self.generation, "inspection")

    def observe_worker(self, nonce: str | None = None) -> WorkerNetworkResult:
        if not self.healthy:
            return WorkerNetworkResult(503, "UNAVAILABLE", None, self.generation, "observation")
        value: dict[str, object] = {
            "repository": self.config.repository,
            "channel": self.config.channel,
            "process_run_id": self.run,
            "deployment_generation": self.deployment,
            "nonce": nonce,
            "artifact": {"source_revision": {"available": True, "value": "a" * 40}, "distribution_version": {"available": True, "value": "1.2.3"}},
            "lifecycle": {"state": "running" if self.ready else "draining", "daemon_serving": self.ready, "startup_reconciled": self.ready},
            "invocations": {"state": "running" if self.ready else "draining"},
            "heartbeat": {"available": True, "sequence": 12, "age_seconds": 0.2},
        }
        binding = self.binding()
        if self.wrong_lock:
            binding["lock_sha256"] = "e" * 64
        return WorkerNetworkResult(200, "OK", {"observation": value, "binding_status": "VERIFIED", "runtime_binding": binding}, self.generation, "observation")

    def diagnostics(self, kind: str, params: dict[str, str] | None = None) -> WorkerNetworkResult:
        if not self.healthy:
            return WorkerNetworkResult(503, "UNAVAILABLE", None, self.generation, "diagnostics")
        return WorkerNetworkResult(200, "OK", {"kind": kind, "parameters": params or {}, "repository": self.config.repository}, self.diagnostic_generation or self.generation, "diagnostics")

    def accept_reevaluation(self, operation_id: str, *, process_run_id: str, deployment_generation: str, target_type: str, number: int) -> WorkerNetworkResult:
        self.send_calls += 1
        if process_run_id != self.run or deployment_generation != self.deployment or not self.healthy:
            return WorkerNetworkResult(409, "CONFLICT", None, self.generation, "forward")
        if self.drop_before_receipt:
            return WorkerNetworkResult(0, "UNKNOWN", None, self.generation, "forward")
        if operation_id not in self.receipts:
            self.receipts[operation_id] = {
                "schema_version": 1,
                "operation_id": operation_id,
                "repository": self.config.repository,
                "channel": self.config.channel,
                "target": {"type": target_type, "number": number},
                "requested_process_run_id": process_run_id,
                "requested_deployment_generation": deployment_generation,
                "accepting_process_run_id": process_run_id,
                "accepting_deployment_generation": deployment_generation,
                "outcome": "accepted_for_reevaluation",
                "invalidation": {"disposition": "queued", "generation": 1},
            }
        if self.lost_response:
            return WorkerNetworkResult(0, "UNKNOWN", None, self.generation, "forward")
        return WorkerNetworkResult(202, "ACCEPTED", self.receipts[operation_id], self.generation, "forward")

    def read_reevaluation(self, operation_id: str) -> WorkerNetworkResult:
        self.read_calls += 1
        if not self.healthy:
            return WorkerNetworkResult(503, "UNAVAILABLE", None, self.generation, "receipt")
        receipt = self.receipts.get(operation_id)
        return WorkerNetworkResult(200, "OK", receipt, self.generation, "receipt") if receipt else WorkerNetworkResult(404, "NOT_FOUND", None, self.generation, "receipt")


@pytest.fixture
def runtime_boundary(real_boundary, tmp_path):
    base, _base_grants, identity, signed, _uids, state = real_boundary
    origin = base.oauth.canonical_issuer
    clients = (
        OAuthClient("alice-client", (origin + "/alice-callback",), allowed_scopes=(READ_SCOPE, OPERATE_SCOPE)),
        OAuthClient("bob-client", (origin + "/bob-callback",), allowed_scopes=(READ_SCOPE, OPERATE_SCOPE)),
    )
    oauth = OAuthSettings(base.oauth.database_path, origin, base.oauth.firebase_project_id, clients, access_lifetime=30, enabled_scopes=(READ_SCOPE, OPERATE_SCOPE))
    policy = tmp_path / "operating-policy.yaml"
    policy.write_text("profile: beta\ntargets:\n  beta-worker:\n    channel: beta\n    repositories: [fixture/beta]\n  release-worker:\n    channel: release\n    repositories: [fixture/release]\n", encoding="utf-8")
    settings = ManagementSettings(tmp_path, oauth, port=base.port, operating_policy_path=policy, operation_state_root=tmp_path / "operations")
    grants = OAuthGrantService(oauth, identity, clock=lambda: state["now"])
    beta, release = FixedWorkerBoundary("beta"), FixedWorkerBoundary("release")
    return settings, grants, signed, policy, beta, release


def _application(boundary):
    settings, grants, _signed, _policy, beta, release = boundary
    return create_management_app(settings, grants, worker_clients={"beta": beta, "release": release})


def _call(client: TestClient, bearer: str, name: str, arguments: dict[str, object], sequence: int = 1) -> dict[str, object]:
    response = _mcp(client, bearer, "tools/call", sequence, {"name": name, "arguments": arguments})
    assert response.status_code == 200, response.text
    payload = response.json()["result"]
    assert payload.get("isError") is not True, payload
    result = payload["structuredContent"]
    assert isinstance(result, dict)
    return result


def _request(beta: FixedWorkerBoundary, *, key: str = "key-1", target_id: str = "beta-worker") -> dict[str, object]:
    return {
        "target_id": target_id,
        "target_type": "issue",
        "number": 2603,
        "idempotency_key": key,
        "expected_process_run_id": beta.run,
        "expected_deployment_generation": beta.deployment,
    }


def test_real_oauth_http_tool_scopes_policy_and_idempotent_journal(runtime_boundary) -> None:
    settings, _grants, signed, policy, beta, _release = runtime_boundary
    app = _application(runtime_boundary)
    with TestClient(app, base_url=settings.oauth.canonical_issuer) as client:
        read_token = _issue(client, settings, signed, "alice", "alice-client", READ_SCOPE)["access_token"]
        operate_token = _issue(client, settings, signed, "bob", "bob-client", READ_SCOPE + " " + OPERATE_SCOPE)["access_token"]
        status = _call(client, read_token, "auto_coder_status", {"target_id": "beta-worker"})
        assert status["status"] == "OK"
        assert status["runtime_binding"]["lock_sha256"] == "d" * 64
        assert status["worker_observation"]["heartbeat"]["sequence"] == 12
        assert _call(client, read_token, "auto_coder_logs", {"target_id": "beta-worker", "limit": 12})["projection"]["kind"] == "logs"
        assert _call(client, read_token, "auto_coder_validation_attempt", {"target_id": "beta-worker", "pr_number": 14, "attempt_id": "a" * 32})["projection"]["kind"] == "validation-attempts"
        before_forbidden = beta.inspect_calls
        assert _mcp(client, read_token, "tools/call", 2, {"name": "auto_coder_reevaluate_target", "arguments": _request(beta)}).status_code == 403
        assert beta.inspect_calls == before_forbidden
        forbidden = _call(client, operate_token, "auto_coder_reevaluate_target", _request(beta, target_id="release-worker"))
        assert forbidden["status"] == "DENIED"
        assert beta.send_calls == 0
        first = _call(client, operate_token, "auto_coder_reevaluate_target", _request(beta), 3)
        assert first["status"] == "accepted_for_reevaluation"
        assert first["effect_state"] == "confirmed"
        assert beta.send_calls == 1
        replay = _call(client, operate_token, "auto_coder_reevaluate_target", _request(beta), 4)
        assert replay == first
        assert beta.send_calls == 1
        queried = _call(client, operate_token, "auto_coder_operation_get", {"target_id": "beta-worker", "operation_id": first["operation_id"]}, 5)
        assert queried == first
        original_record = app.state.management_runtime.journal.get(first["operation_id"]).record
        read_calls = beta.read_calls
        inspected = _call(client, operate_token, "auto_coder_operation_inspect", {"target_id": "beta-worker", "operation_id": first["operation_id"]})
        assert inspected == first
        # Real authenticated MCP journal projection must satisfy the fixture consumer's receipt contract.
        from scripts.dot_mcp_acceptance import verified_reevaluation_receipt

        assert verified_reevaluation_receipt(inspected, operation_id=first["operation_id"], target_id="beta-worker", process_run_id=beta.run)
        assert app.state.management_runtime.journal.get(first["operation_id"]).record == original_record
        assert beta.read_calls == read_calls and beta.send_calls == 1
        assert _call(client, read_token, "auto_coder_operation_inspect", {"target_id": "beta-worker", "operation_id": first["operation_id"]})["status"] == "NOT_FOUND"
        different_key = _call(client, operate_token, "auto_coder_reevaluate_target", _request(beta, key="key-2"), 6)
        assert different_key["status"] == "accepted_for_reevaluation"
        assert beta.send_calls == 2
        policy.write_text(policy.read_text().replace("profile: beta", "profile: observe"), encoding="utf-8")
        denied = _call(client, operate_token, "auto_coder_reevaluate_target", _request(beta, key="key-3"), 7)
        assert denied["status"] == "DENIED" and beta.send_calls == 2
        # A replay of an already accepted intent remains read-only after a downgrade.
        assert _call(client, operate_token, "auto_coder_reevaluate_target", _request(beta), 8) == first


def test_authenticated_logs_cross_real_private_transport_for_overflow_and_incremental_pages(runtime_boundary, tmp_path, monkeypatch: pytest.MonkeyPatch, _use_real_commands: None, _use_real_sleep: None) -> None:
    settings, grants, signed, _policy, _beta, release = runtime_boundary
    monkeypatch.setenv("AUTO_CODER_TEST_SEEDED_LOGS", "500")
    encoded_responses: list[bytes] = []
    original_read = http.client.HTTPResponse.read

    def record_encoded_response(response: http.client.HTTPResponse, amount: int | None = None) -> bytes:
        raw = original_read(response, amount)
        encoded_responses.append(raw)
        return raw

    monkeypatch.setattr(http.client.HTTPResponse, "read", record_encoded_response)
    supervisor = _supervisor(tmp_path / "real-worker")
    listener, worker = _bridge(supervisor, os.urandom(48))
    worker_pid = None
    try:
        worker.inspect_supervisor()
        started = supervisor.apply({"operation_id": "start-mcp-budget-worker", "operation": "START"})
        worker_pid = started["worker"]["pid"]
        for _ in range(100):
            if worker.observe_worker("mcp-budget-ready").result_code == "OK":
                break
            threading.Event().wait(0.05)
        else:
            pytest.fail("collector-backed worker did not become observable")

        app = create_management_app(settings, grants, worker_clients={"beta": worker, "release": release})
        with TestClient(app, base_url=settings.oauth.canonical_issuer) as client:
            token = _issue(client, settings, signed, "alice", "alice-client", READ_SCOPE)["access_token"]
            recent = _call(client, token, "auto_coder_logs", {"target_id": "beta-worker", "limit": 500})
            assert recent["status"] == "UNAVAILABLE" and recent["binding_status"] == "UNAVAILABLE"
            assert recent["runtime_binding"] is None
            projection = recent["projection"]
            assert projection["result"] == "events" and projection["events"][-1]["sequence"] == 500

            delivered = []
            previous_after = 0
            arguments: dict[str, object] = {
                "target_id": "beta-worker",
                "limit": 500,
                "after_sequence": 0,
                "process_run_id": projection["process_run_id"],
            }
            while True:
                result = _call(client, token, "auto_coder_logs", arguments)
                assert result["status"] == "UNAVAILABLE" and result["binding_status"] == "UNAVAILABLE"
                page = result["projection"]
                assert page["after_sequence"] == previous_after
                delivered.extend(event["sequence"] for event in page["events"])
                if not page["has_more"]:
                    assert page["next_after_sequence"] == 500 and page["next_cursor"] is None
                    break
                assert page["next_after_sequence"] == page["events"][-1]["sequence"]
                previous_after = page["next_after_sequence"]
                arguments = {"target_id": "beta-worker", "limit": 500, "cursor": page["next_cursor"]}
            assert delivered == list(range(1, 501))

        envelopes = []
        for raw in encoded_responses:
            try:
                value = json.loads(raw)
            except (TypeError, ValueError, UnicodeDecodeError):
                continue
            if isinstance(value, dict) and value.get("instance_id") == worker.config.instance_id and value.get("result_code") == "OK":
                envelopes.append(raw)
        assert envelopes and all(len(raw) <= MAX_RESPONSE_BYTES for raw in envelopes)
    finally:
        if worker_pid is not None:
            (tmp_path / "real-worker" / "fixture-release").write_text("release")
            try:
                os.kill(worker_pid, 15)
            except ProcessLookupError:
                pass
        listener.close()


def test_authenticated_pr_diagnostics_cross_real_supervisor_worker_and_retained_stores(runtime_boundary, tmp_path, monkeypatch: pytest.MonkeyPatch, _use_real_commands: None, _use_real_sleep: None) -> None:
    """Exercise MCP -> signed Supervisor HTTP -> worker UDS -> actual stores."""
    settings, grants, signed, _policy, _beta, release = runtime_boundary
    home = tmp_path / "worker-home"
    monkeypatch.setenv("HOME", str(home))
    repository = "fixture/beta"
    retained = home / ".auto-coder" / repository
    retained.mkdir(parents=True)
    cycle_path = retained / "pr_review_cycle.json"
    decision_path = retained / "effective_review_decisions.json"
    effects_path = retained / "pr_review_effects.json"
    cycle_repository = PrReviewCycleRepository(repository, cycle_path)
    contract = ContractSnapshot(("2753",), "REQ-002: retain review-cycle correlation")
    policy = StrongPolicyIdentity("backend_strong_pr_adversarial_validation", "model=fixture", "v1")
    original = RoundProvenance("a" * 40, "b" * 40)
    cycle_repository.record_ordinary_pass(17, original, contract)
    claim = cycle_repository.claim_strong_audit(17, original, contract, policy)
    finding = Finding(
        finding_id="finding-17",
        origin_round_id=claim.claim_id,
        requirement_ids=("REQ-002",),
        requirement_texts=("REQ-002: retain review-cycle correlation",),
        counterexample="Given retained evidence, correlation is requested",
        expected_behavior="The producer identities remain visible",
        actual_behavior="The identities are absent",
        evidence="producer fixture",
        affected_boundary="review cycle projection",
        focused_regression_scenario="Compare producer records with the MCP response",
    )
    strong_round = cycle_repository.record_strong_result(17, claim.claim_id, VERDICT_FINDINGS, "fixture/reviewer", (finding,))
    cycle_repository.acknowledge_publication(17, strong_round.round_id)
    repaired = RoundProvenance("c" * 40, "b" * 40)
    cycle_repository.record_ordinary_pass(17, repaired, contract)
    closed = cycle_repository.certify_closure(
        17,
        repaired,
        contract,
        policy,
        strong_round.round_id,
        strong_round.finding_set_revision,
        (FindingDisposition(finding.finding_id, FIXED, "Focused regression now preserves correlation", repaired.head_sha),),
        True,
        "Only the diagnosed boundary changed",
    )
    assert closed.accepted_closure is not None
    active_provenance = RoundProvenance("d" * 40, "b" * 40)
    cycle_repository.record_ordinary_pass(18, active_provenance, contract)
    active_claim = cycle_repository.claim_strong_audit(18, active_provenance, contract, policy)
    waiting_provenance = RoundProvenance("9" * 40, "8" * 40)
    cycle_repository.record_ordinary_pass(19, waiting_provenance, contract)
    waiting_claim = cycle_repository.claim_strong_audit(19, waiting_provenance, contract, policy)
    waiting_finding = replace(finding, finding_id="finding-19", origin_round_id=waiting_claim.claim_id)
    cycle_repository.record_strong_result(19, waiting_claim.claim_id, VERDICT_FINDINGS, "fixture/reviewer", (waiting_finding,))
    waiting_snapshot = cycle_repository.snapshot(19)
    assert waiting_snapshot.pending_effect and waiting_snapshot.waiting_reason
    for sequence in range(300):
        provenance = RoundProvenance(f"{sequence:040x}", "7" * 40)
        cycle_repository.record_ordinary_pass(20, provenance, contract)
        claim_for_page = cycle_repository.claim_strong_audit(20, provenance, contract, policy)
        finding_for_page = replace(finding, finding_id=f"page-finding-{sequence}", origin_round_id=claim_for_page.claim_id)
        round_for_page = cycle_repository.record_strong_result(20, claim_for_page.claim_id, VERDICT_FINDINGS, "fixture/reviewer", (finding_for_page,))
        cycle_repository.acknowledge_publication(20, round_for_page.round_id)
    assert cycle_path.stat().st_size > 256 * 1024
    decision_path.write_text(
        json.dumps(
            {
                "17": {
                    "pr_number": 17,
                    "head_sha": "e" * 40,
                    "base_sha": "f" * 40,
                    "contract_identity": "decision-contract-17",
                    "finding_set_revision": 11,
                    "attempt_id": "attempt-17",
                    "attempt_sequence": 5,
                    "publication": "CONFIRMED",
                    "handoff": "WAITING",
                    "wait_reason": "Bearer decision-secret at /private/path",
                    "updated_at": 100.0,
                }
            }
        ),
        encoding="utf-8",
    )
    effect_repository = ReviewEffectRepository(repository, effects_path)
    effect_payload = AcceptedReviewPayload.strong(repository, 17, strong_round, (finding,))

    class ControlledExternalReviewService:
        """Controlled provider boundary observed independently from retained state."""

        def __init__(self):
            self.mutation_count = 0
            self.threads = {"resolved-github-thread-17": "RESOLVED"}
            self.receipts: list[str] = []

        def publish(self, destination: str, outcome: EffectAttempt) -> EffectAttempt:
            self.mutation_count += 1
            if outcome.receipt:
                self.receipts.append(outcome.receipt)
            # The controlled GitHub observation is deliberately independent:
            # an uncertain journal result cannot rewrite a resolved thread.
            assert self.threads.get(destination, "UNOBSERVED") in {"RESOLVED", "UNOBSERVED"}
            return outcome

        def observe_thread(self, destination: str) -> str:
            return self.threads[destination]

    external_service = ControlledExternalReviewService()

    class ControlledEffectTransport:
        def __init__(self, service: ControlledExternalReviewService, outcome: EffectAttempt):
            self.service = service
            self.outcome = outcome

        def send(self, operation):
            return self.service.publish(operation.destination, self.outcome)

        def reconcile(self, _operation):
            raise AssertionError("new operations do not reconcile")

    executor = ReviewEffectExecutor(effect_repository, owner_token="seed-owner")
    confirmed_operation = executor.apply(
        effect_payload,
        "review-publication",
        "review-17",
        ControlledEffectTransport(external_service, EffectAttempt("CONFIRMED", receipt="review-receipt-17")),
        lambda: True,
    )
    uncertain_operation = executor.apply(
        effect_payload,
        "thread-publication",
        "resolved-github-thread-17",
        ControlledEffectTransport(external_service, EffectAttempt("UNCERTAIN", reason="Authorization: Bearer thread-secret")),
        lambda: True,
    )
    retained_effect_history = {operation.operation_id: effect_repository.get(operation.operation_id) for operation in (confirmed_operation, uncertain_operation)}
    assert all(retained_effect_history.values())
    effects_before = external_service.mutation_count
    resolved_thread_before = external_service.observe_thread("resolved-github-thread-17")
    assert resolved_thread_before == "RESOLVED"
    resolution = resolve_pending_work_store(repository, home=home)
    assert resolution.store is not None
    resolution.store.ensure_reevaluation(WorkIdentity(repository, "pr:17", "pr-processing", "rev-a"), ("thread-a",), now=10)
    resolution.store.ensure_reevaluation(WorkIdentity(repository, "pr:17", "pr-processing", "rev-b"), ("thread-b",), now=20)
    resolution.store.ensure_reevaluation(WorkIdentity(repository, "pr:171", "pr-processing", "other-pr"), ("foreign",), now=30)
    source_before = {path: path.read_bytes() for path in (cycle_path, decision_path, effects_path)}
    with sqlite3.connect(resolution.destination) as connection:
        pending_before = connection.execute("SELECT repository,entity,stage,revision,reason,not_before,unfinished_effects,throttle_attempts,last_error,status FROM github_pending_work ORDER BY work_key").fetchall()

    supervisor_root = tmp_path / "real-pr-worker"
    monkeypatch.setenv("AUTO_CODER_RUNTIME_ROOT", str(supervisor_root))
    supervisor = _supervisor(supervisor_root)
    listener, worker = _bridge(supervisor, os.urandom(48))
    worker_pid = None
    try:
        worker.inspect_supervisor()
        started = supervisor.apply({"operation_id": "start-mcp-pr-diagnostic-worker", "operation": "START"})
        worker_pid = started["worker"]["pid"]
        for sequence in range(100):
            if worker.observe_worker(f"pr-diagnostic-ready-{sequence}").result_code == "OK":
                break
            threading.Event().wait(0.05)
        else:
            pytest.fail("private diagnostic worker did not become observable")

        app = create_management_app(settings, grants, worker_clients={"beta": worker, "release": release})
        with TestClient(app, base_url=settings.oauth.canonical_issuer) as client:
            token = _issue(client, settings, signed, "alice", "alice-client", READ_SCOPE)["access_token"]
            pending_backup = resolution.destination.with_suffix(".saved")
            resolution.destination.replace(pending_backup)
            missing = _call(client, token, "auto_coder_pr_diagnostics", {"target_id": "beta-worker", "pr_number": 17}, 1)
            assert missing["projection"]["sections"]["pending_work"]["availability"] == "ABSENT"
            pending_backup.replace(resolution.destination)
            first = _call(client, token, "auto_coder_pr_diagnostics", {"target_id": "beta-worker", "pr_number": 17, "limit": 1, "cursor": 0})
            second = _call(
                client,
                token,
                "auto_coder_pr_diagnostics",
                {"target_id": "beta-worker", "pr_number": 17, "limit": 1, "cursor": 1, "expected_revision": first["projection"]["evidence_revision"]},
                2,
            )
            assert first["status"] == "UNAVAILABLE" and first["binding_status"] == "UNAVAILABLE"
            assert first["process_run_id"] == worker.worker_process_run_id
            assert isinstance(first["artifact"], dict)
            assert first["artifact"] != first["runtime_binding"]
            projected = first["projection"]
            cycle = projected["sections"]["review_cycle"]["records"][0]
            cycle_source = json.loads(source_before[cycle_path])["prs"]["17"]
            round_source = cycle_source["strong_rounds"][0]
            closure_source = cycle_source["closures"][0]
            assert cycle["rounds"][0]["claim_id"] == claim.claim_id
            assert cycle["rounds"][0]["open_epoch"] == strong_round.open_epoch
            for key in ("head_sha", "base_sha", "contract_identity", "finding_set_revision"):
                assert cycle["rounds"][0][key] == round_source[key]
            assert cycle["closures"][0]["references_round_id"] == strong_round.round_id
            assert cycle["closures"][0]["closure_id"] == closed.accepted_closure.closure_id
            for key in ("head_sha", "base_sha", "contract_identity", "finding_set_revision"):
                assert cycle["closures"][0][key] == closure_source[key]
            decision_source = json.loads(source_before[decision_path])["17"]
            projected_decision = projected["sections"]["effective_decision"]["records"][0]
            assert projected_decision["publication"] == "CONFIRMED"
            for key in ("head_sha", "base_sha", "contract_identity", "finding_set_revision"):
                assert projected_decision[key] == decision_source[key]
            projected_history = {record["operation_id"]: record for record in projected["sections"]["review_effects"]["records"]}
            retained_confirmed = retained_effect_history[confirmed_operation.operation_id]
            assert retained_confirmed is not None
            projected_effect = projected_history[confirmed_operation.operation_id]
            for key in ("payload_identity", "round_id", "target_head", "contract_identity", "finding_set_revision", "purpose", "destination", "status", "receipt"):
                assert projected_effect[key] == getattr(retained_confirmed, key)
            assert second["projection"]["sections"]["review_effects"]["records"][0]["status"] == "UNCERTAIN"
            pending_first = projected["sections"]["pending_work"]
            pending_second = second["projection"]["sections"]["pending_work"]
            assert pending_first["records"][0]["identity"]["revision"] == "rev-a" and pending_first["next_cursor"] == 1
            assert pending_second["records"][0]["identity"]["revision"] == "rev-b" and pending_second["next_cursor"] is None
            effect_page = _call(client, token, "auto_coder_pr_diagnostics", {"target_id": "beta-worker", "pr_number": 17, "limit": 1}, 10)
            effect_repository.record(uncertain_operation.operation_id, "seed-owner", EffectAttempt("CONFIRMED", receipt="thread-receipt-17"))
            changed_effect_page = _call(
                client,
                token,
                "auto_coder_pr_diagnostics",
                {"target_id": "beta-worker", "pr_number": 17, "limit": 1, "cursor": 1, "expected_revision": effect_page["projection"]["evidence_revision"]},
                11,
            )
            assert changed_effect_page["projection"]["evidence_changed"] is True
            assert changed_effect_page["projection"]["sections"]["review_effects"]["availability"] == "CONFLICT"
            effects_path.write_bytes(source_before[effects_path])

            pending_page = _call(client, token, "auto_coder_pr_diagnostics", {"target_id": "beta-worker", "pr_number": 17, "limit": 1}, 12)
            with sqlite3.connect(resolution.destination) as connection:
                connection.execute("UPDATE github_pending_work SET updated_at=999 WHERE revision='rev-a'")
            changed_pending_page = _call(
                client,
                token,
                "auto_coder_pr_diagnostics",
                {"target_id": "beta-worker", "pr_number": 17, "limit": 1, "cursor": 1, "expected_revision": pending_page["projection"]["evidence_revision"]},
                13,
            )
            assert changed_pending_page["projection"]["evidence_changed"] is True
            assert changed_pending_page["projection"]["sections"]["pending_work"]["availability"] == "CONFLICT"
            encoded = json.dumps((first, second))
            assert "other-pr" not in encoded and "owner-must-not-leak" not in encoded and "thread-secret" not in encoded and "/private/path" not in encoded

            active = _call(client, token, "auto_coder_pr_diagnostics", {"target_id": "beta-worker", "pr_number": 18}, 7)
            active_projected = active["projection"]["sections"]["review_cycle"]["records"][0]["active_claim"]
            assert active_projected["claim_id"] == active_claim.claim_id
            assert active_projected["phase"] == active_claim.phase
            assert active_projected["open_epoch"] == active_claim.open_epoch
            assert "owner_id" not in active_projected

            waiting = _call(client, token, "auto_coder_pr_diagnostics", {"target_id": "beta-worker", "pr_number": 19}, 9)
            waiting_cycle = waiting["projection"]["sections"]["review_cycle"]["records"][0]
            assert waiting_cycle["phase"] == waiting_snapshot.phase
            assert waiting_cycle["waiting_reason"] == waiting_snapshot.waiting_reason
            assert waiting_cycle["pending_effect"] == waiting_snapshot.pending_effect

            bounded = _call(client, token, "auto_coder_pr_diagnostics", {"target_id": "beta-worker", "pr_number": 20, "limit": 1}, 14)
            bounded_cycle = bounded["projection"]["sections"]["review_cycle"]
            assert len(bounded_cycle["records"][0]["rounds"]) == 1
            assert bounded_cycle["records"][0]["rounds_truncated"] is True
            assert bounded_cycle["records"][0]["rounds_next_cursor"] == 1
            assert bounded["projection"]["sections"]["effective_decision"]["availability"] == "COMPLETE"
            assert len(json.dumps(bounded["projection"]).encode()) < 256 * 1024

            malformed_cycle = json.loads(source_before[cycle_path])
            malformed_cycle["prs"]["17"]["findings"] = []
            cycle_path.write_text(json.dumps(malformed_cycle), encoding="utf-8")
            malformed_cycle_bytes = cycle_path.read_bytes()
            effects_before_corrupt_read = external_service.mutation_count
            corrupt_cycle = _call(client, token, "auto_coder_pr_diagnostics", {"target_id": "beta-worker", "pr_number": 17}, 15)
            assert corrupt_cycle["projection"]["sections"]["review_cycle"]["availability"] == "CORRUPT"
            assert corrupt_cycle["projection"]["sections"]["effective_decision"]["records"][0]["publication"] == "CONFIRMED"
            assert corrupt_cycle["projection"]["sections"]["review_effects"]["records"]
            assert corrupt_cycle["projection"]["sections"]["pending_work"]["records"]
            assert cycle_path.read_bytes() == malformed_cycle_bytes
            assert external_service.mutation_count == effects_before_corrupt_read
            cycle_path.write_bytes(source_before[cycle_path])

            decision_path.write_text("{broken", encoding="utf-8")
            corrupt = _call(client, token, "auto_coder_pr_diagnostics", {"target_id": "beta-worker", "pr_number": 17}, 3)
            assert corrupt["projection"]["sections"]["effective_decision"]["availability"] == "CORRUPT"
            assert corrupt["projection"]["sections"]["review_effects"]["availability"] == "COMPLETE"

            decision_path.write_bytes(source_before[decision_path])
            effect_lock = lock_path(repository, effects_path, "pr-review-effects")
            ensure_lock_directory(effect_lock)
            with effect_lock.open("a+") as held:
                fcntl.flock(held.fileno(), fcntl.LOCK_EX)
                contended = _call(client, token, "auto_coder_pr_diagnostics", {"target_id": "beta-worker", "pr_number": 17}, 4)
                fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            assert contended["projection"]["sections"]["review_effects"]["availability"] == "UNAVAILABLE"
            assert contended["projection"]["sections"]["review_cycle"]["availability"] == "COMPLETE"

            old_run = worker.worker_process_run_id
            assert old_run is not None and worker.worker_deployment_generation is not None
            diagnostic_entered = threading.Event()
            release_diagnostic = threading.Event()
            paused_result: list[dict[str, object]] = []
            original_diagnostics = worker.diagnostics

            def pause_at_worker_boundary(kind: str, params: dict[str, str] | None = None) -> WorkerNetworkResult:
                diagnostic_entered.set()
                assert release_diagnostic.wait(10), "replacement did not release paused diagnostic"
                return original_diagnostics(kind, params)

            monkeypatch.setattr(worker, "diagnostics", pause_at_worker_boundary)

            def call_while_replacing() -> None:
                paused_result.append(_call(client, token, "auto_coder_pr_diagnostics", {"target_id": "beta-worker", "pr_number": 17}, 8))

            paused_thread = threading.Thread(target=call_while_replacing)
            paused_thread.start()
            assert diagnostic_entered.wait(10), "diagnostic did not reach the worker boundary"
            stopping = supervisor.apply(
                {
                    "operation_id": "stop-old-mcp-pr-diagnostic-worker",
                    "operation": "REQUEST_GRACEFUL_STOP",
                    "expected_process_run_id": old_run,
                    "expected_generation": worker.worker_deployment_generation,
                }
            )
            assert stopping["status"] in {"DRAINING", "STOPPED"}
            (supervisor_root / "fixture-release").write_text("release")
            for _ in range(100):
                if supervisor.inspect()["status"] == "STOPPED":
                    break
                threading.Event().wait(0.05)
            else:
                pytest.fail("old private diagnostic worker did not stop")
            assert supervisor.read_operation("stop-old-mcp-pr-diagnostic-worker")["status"] == "STOPPED"
            (supervisor_root / "fixture-release").unlink()
            restarted = supervisor.apply({"operation_id": "restart-mcp-pr-diagnostic-worker", "operation": "START"})
            worker_pid = restarted["worker"]["pid"]
            assert worker.inspect_supervisor().result_code == "OK"
            for sequence in range(100):
                if worker.observe_worker(f"replacement-pr-diagnostic-ready-{sequence}").result_code == "OK":
                    break
                threading.Event().wait(0.05)
            else:
                pytest.fail("replacement private diagnostic worker did not become observable")
            release_diagnostic.set()
            paused_thread.join(10)
            assert not paused_thread.is_alive()
            assert paused_result[0]["status"] == "CONFLICT"
            assert paused_result[0]["reason"] == "worker_changed_during_diagnostic"
            assert "projection" not in paused_result[0]
            monkeypatch.setattr(worker, "diagnostics", original_diagnostics)
            assert worker.worker_process_run_id != old_run
            assert worker.worker_process_run_id is not None

            denied = _call(client, token, "auto_coder_pr_diagnostics", {"target_id": "unknown-worker", "pr_number": 17}, 5)
            assert denied["status"] == "DENIED"
    finally:
        if worker_pid is not None:
            (supervisor_root / "fixture-release").write_text("release")
            try:
                supervisor.apply(
                    {
                        "operation_id": "stop-mcp-pr-diagnostic-worker",
                        "operation": "REQUEST_GRACEFUL_STOP",
                        "expected_process_run_id": worker.worker_process_run_id,
                        "expected_generation": worker.worker_deployment_generation,
                    }
                )
            finally:
                for _ in range(100):
                    if supervisor.inspect()["status"] == "STOPPED":
                        break
                    threading.Event().wait(0.05)
                else:
                    try:
                        os.kill(worker_pid, 15)
                    except ProcessLookupError:
                        pass
        listener.close()

    decision_path.write_bytes(source_before[decision_path])
    assert {path: path.read_bytes() for path in source_before} == source_before
    with sqlite3.connect(resolution.destination) as connection:
        pending_after = connection.execute("SELECT repository,entity,stage,revision,reason,not_before,unfinished_effects,throttle_attempts,last_error,status FROM github_pending_work ORDER BY work_key").fetchall()
    assert pending_after == pending_before
    assert external_service.mutation_count == effects_before
    assert external_service.observe_thread("resolved-github-thread-17") == resolved_thread_before


def test_original_grant_revoked_after_commit_and_before_dispatch(runtime_boundary) -> None:
    settings, grants, signed, _policy, beta, _release = runtime_boundary
    app = _application(runtime_boundary)
    with TestClient(app, base_url=settings.oauth.canonical_issuer) as client:
        token = _issue(client, settings, signed, "bob", "bob-client", READ_SCOPE + " " + OPERATE_SCOPE)["access_token"]

        def revoke_after_commit() -> None:
            grants.revoke("bob-client", token)

        app.state.management_runtime.journal._after_admission_commit = revoke_after_commit
        refused = _call(client, token, "auto_coder_reevaluate_target", _request(beta))
        assert refused["status"] == "NEEDS_ATTENTION"
        assert refused["safe_reason"] == "original_authority_denied"
        assert beta.send_calls == 0
    # A new service reconstructs the same journal and cannot revive the grant.
    with TestClient(_application(runtime_boundary), base_url=settings.oauth.canonical_issuer):
        assert beta.send_calls == 0


def test_lost_response_reconciles_exact_receipt_without_resend_after_restart(runtime_boundary) -> None:
    settings, grants, signed, _policy, beta, _release = runtime_boundary
    beta.lost_response = True
    app = _application(runtime_boundary)
    with TestClient(app, base_url=settings.oauth.canonical_issuer) as client:
        token = _issue(client, settings, signed, "bob", "bob-client", READ_SCOPE + " " + OPERATE_SCOPE)["access_token"]
        first = _call(client, token, "auto_coder_reevaluate_target", _request(beta))
        assert first["status"] == "RECONCILING"
        assert first["effect_state"] == "sent_unknown"
        assert beta.send_calls == 1
        op_id = first["operation_id"]
        grants.revoke("bob-client", token)
    # The original worker receipt was committed; only a GET is now allowed.
    next_app = _application(runtime_boundary)
    with TestClient(next_app, base_url=settings.oauth.canonical_issuer) as client:
        fresh_token = _issue(client, settings, signed, "bob", "bob-client", READ_SCOPE)["access_token"]
        before = next_app.state.management_runtime.journal.get(op_id).record
        reads_before = beta.read_calls
        inspected = _call(client, fresh_token, "auto_coder_operation_inspect", {"target_id": "beta-worker", "operation_id": op_id})
        assert before is not None
        assert inspected["effect_state"] == before.effect.state.value
        assert inspected["phase"] == before.phase.value
        assert next_app.state.management_runtime.journal.get(op_id).record == before
        assert beta.send_calls == 1 and beta.read_calls == reads_before
        recovered = _call(client, fresh_token, "auto_coder_operation_get", {"target_id": "beta-worker", "operation_id": op_id})
        assert recovered["status"] == "accepted_for_reevaluation"
        assert beta.send_calls == 1 and beta.read_calls >= 1


def test_reconstructed_unsent_operation_checks_original_revoked_grant(runtime_boundary) -> None:
    settings, grants, signed, _policy, beta, _release = runtime_boundary
    app = _application(runtime_boundary)
    with TestClient(app, base_url=settings.oauth.canonical_issuer) as client:
        token = _issue(client, settings, signed, "bob", "bob-client", READ_SCOPE + " " + OPERATE_SCOPE)["access_token"]
        verified = grants.authenticate_access(token, settings.oauth.resource, (READ_SCOPE, OPERATE_SCOPE))
        original = app.state.management_runtime._journal_authority(verified, grants.accepted_authority(verified))
        intent = OperationIntent(
            ManagedTarget(beta.config.instance_id, beta.config.channel, beta.config.repository),
            "reevaluate_target",
            (ActionParameter("target_id", "beta-worker"), ActionParameter("target_type", "issue"), ActionParameter("number", 2603)),
            "durable-before-manager-restart",
            beta.run,
            beta.deployment,
        )
        admitted = app.state.management_runtime.journal.admit(intent, original)
        assert admitted.status is AdmissionStatus.CREATED and admitted.record is not None
        operation_id = admitted.record.operation_id
        grants.revoke("bob-client", token)
        assert beta.send_calls == 0
    # Lifespan restoration must not treat a queued operation as a new grant.
    restarted = _application(runtime_boundary)
    with TestClient(restarted, base_url=settings.oauth.canonical_issuer):
        deadline = time.monotonic() + 5
        while True:
            result = restarted.state.management_runtime.journal.get(operation_id)
            if result.record is not None and result.record.phase is OperationPhase.NEEDS_ATTENTION:
                break
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert result.record is not None and result.record.phase is OperationPhase.NEEDS_ATTENTION
        assert result.record.safe_error_category == "original_authority_denied"
        assert beta.send_calls == 0


def test_misleading_supervisor_health_and_invalid_request_never_admit(runtime_boundary) -> None:
    settings, _grants, signed, _policy, beta, release = runtime_boundary
    beta.healthy = False
    with TestClient(_application(runtime_boundary), base_url=settings.oauth.canonical_issuer) as client:
        token = _issue(client, settings, signed, "bob", "bob-client", READ_SCOPE + " " + OPERATE_SCOPE)["access_token"]
        assert _call(client, token, "auto_coder_status", {"target_id": "beta-worker"})["status"] == "UNAVAILABLE"
        assert _call(client, token, "auto_coder_reevaluate_target", _request(beta))["status"] == "UNAVAILABLE"
        assert beta.send_calls == 0
        malformed = _mcp(client, token, "tools/call", 11, {"name": "auto_coder_reevaluate_target", "arguments": {**_request(beta), "number": True}})
        assert malformed.status_code == 200 and malformed.json()["result"]["isError"] is True
        assert _call(client, token, "auto_coder_reevaluate_target", {**_request(beta), "number": 0})["status"] == "INVALID_REQUEST"
        assert _call(client, token, "auto_coder_logs", {"target_id": "beta-worker", "item_type": "pr", "item_number": 12, "cursor": "alternate"})["status"] == "INVALID_REQUEST"
        assert _call(client, token, "auto_coder_status", {"target_id": "release-worker"})["status"] == "OK"
        assert release.send_calls == 0


def test_same_source_wrong_locked_receipt_and_replaced_worker_are_rejected(runtime_boundary) -> None:
    settings, _grants, signed, _policy, beta, _release = runtime_boundary
    with TestClient(_application(runtime_boundary), base_url=settings.oauth.canonical_issuer) as client:
        token = _issue(client, settings, signed, "bob", "bob-client", READ_SCOPE + " " + OPERATE_SCOPE)["access_token"]
        beta.wrong_lock = True
        observation = _call(client, token, "auto_coder_status", {"target_id": "beta-worker"})
        assert observation["status"] == "CONFLICT" and observation["runtime_binding"] is None
        assert _call(client, token, "auto_coder_reevaluate_target", _request(beta))["status"] == "CONFLICT"
        beta.wrong_lock = False
        previous = _request(beta)
        beta.run = "beta-replaced-process"
        assert _call(client, token, "auto_coder_reevaluate_target", previous)["status"] == "CONFLICT"
        assert beta.send_calls == 0


def test_diagnostic_does_not_join_observation_from_replaced_supervisor(runtime_boundary) -> None:
    settings, _grants, signed, _policy, beta, _release = runtime_boundary
    beta.diagnostic_generation = "replacement-supervisor"
    with TestClient(_application(runtime_boundary), base_url=settings.oauth.canonical_issuer) as client:
        token = _issue(client, settings, signed, "alice", "alice-client", READ_SCOPE)["access_token"]
        result = _call(client, token, "auto_coder_logs", {"target_id": "beta-worker"})
        assert result == {
            "status": "CONFLICT",
            "reason": "supervisor_changed_during_diagnostic",
            "target_id": "beta-worker",
            "instance_id": "beta-worker",
            "channel": "beta",
            "repository": "fixture/beta",
        }


def test_unknown_external_effect_holds_lane_across_new_keys_and_restart(runtime_boundary) -> None:
    settings, _grants, signed, _policy, beta, _release = runtime_boundary
    beta.drop_before_receipt = True
    with TestClient(_application(runtime_boundary), base_url=settings.oauth.canonical_issuer) as client:
        token = _issue(client, settings, signed, "bob", "bob-client", READ_SCOPE + " " + OPERATE_SCOPE)["access_token"]
        unknown = _call(client, token, "auto_coder_reevaluate_target", _request(beta))
        assert unknown["effect_state"] == "sent_unknown" and unknown["status"] == "RECONCILING"
        assert _call(client, token, "auto_coder_reevaluate_target", _request(beta, key="different-key"))["status"] == "BUSY"
        assert beta.send_calls == 1
    with TestClient(_application(runtime_boundary), base_url=settings.oauth.canonical_issuer) as client:
        query = _call(client, token, "auto_coder_operation_get", {"target_id": "beta-worker", "operation_id": unknown["operation_id"]})
        assert query["status"] == "RECONCILING"
        assert beta.send_calls == 1


@pytest.mark.parametrize(
    ("intervention", "expected_reason"),
    [
        ("downgrade", "current_policy_denied"),
        ("unavailable_policy", "current_policy_unavailable"),
        ("withdraw_consent", "original_authority_denied"),
        ("grant_expired", "original_authority_denied"),
    ],
)
def test_original_policy_and_consent_are_rechecked_after_durable_admission(runtime_boundary, intervention: str, expected_reason: str) -> None:
    settings, grants, signed, policy, beta, _release = runtime_boundary
    app = _application(runtime_boundary)
    with TestClient(app, base_url=settings.oauth.canonical_issuer) as client:
        token = _issue(client, settings, signed, "bob", "bob-client", READ_SCOPE + " " + OPERATE_SCOPE)["access_token"]

        def change_after_commit() -> None:
            if intervention == "downgrade":
                policy.write_text(policy.read_text().replace("profile: beta", "profile: observe"), encoding="utf-8")
            elif intervention == "unavailable_policy":
                policy.write_text("profile: [broken", encoding="utf-8")
            elif intervention == "withdraw_consent":
                grants.settings = replace(grants.settings, enabled_scopes=(READ_SCOPE,))
            else:
                grants.clock = lambda: 10**12

        app.state.management_runtime.journal._after_admission_commit = change_after_commit
        outcome = _call(client, token, "auto_coder_reevaluate_target", _request(beta))
        assert outcome["status"] == "NEEDS_ATTENTION"
        assert outcome["safe_reason"] == expected_reason
        assert beta.send_calls == 0


def test_access_token_expiry_alone_does_not_revoke_accepted_operation(runtime_boundary) -> None:
    settings, grants, signed, _policy, beta, _release = runtime_boundary
    app = _application(runtime_boundary)
    with TestClient(app, base_url=settings.oauth.canonical_issuer) as client:
        token = _issue(client, settings, signed, "bob", "bob-client", READ_SCOPE + " " + OPERATE_SCOPE)["access_token"]
        accepted_clock = grants.clock()
        app.state.management_runtime.journal._after_admission_commit = lambda: setattr(grants, "clock", lambda: accepted_clock + 60)
        outcome = _call(client, token, "auto_coder_reevaluate_target", _request(beta))
        assert outcome["status"] == "accepted_for_reevaluation"
        assert beta.send_calls == 1
