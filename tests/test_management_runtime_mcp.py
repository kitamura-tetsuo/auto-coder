"""Authenticated HTTP, real grant/SQLite, and dispatcher component regressions.

The physical two-container/UDS transport oracle is a separate Supervisor
integration suite; this file exercises management consumption of its typed API.
"""

from __future__ import annotations

import time
from dataclasses import replace

import pytest
from starlette.testclient import TestClient

from auto_coder.management.action_scopes import OPERATE_SCOPE, READ_SCOPE
from auto_coder.management.app import create_management_app
from auto_coder.management.oauth_config import ManagementSettings, OAuthClient, OAuthSettings
from auto_coder.management.oauth_grants import OAuthGrantService
from auto_coder.management.operations import ActionParameter, AdmissionStatus, ManagedTarget, OperationIntent, OperationPhase
from auto_coder.management.worker_network_client import WorkerEndpointConfig, WorkerNetworkResult
from tests.test_management_mcp import _issue, _mcp, real_boundary


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
    with TestClient(_application(runtime_boundary), base_url=settings.oauth.canonical_issuer) as client:
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
        different_key = _call(client, operate_token, "auto_coder_reevaluate_target", _request(beta, key="key-2"), 6)
        assert different_key["status"] == "accepted_for_reevaluation"
        assert beta.send_calls == 2
        policy.write_text(policy.read_text().replace("profile: beta", "profile: observe"), encoding="utf-8")
        denied = _call(client, operate_token, "auto_coder_reevaluate_target", _request(beta, key="key-3"), 7)
        assert denied["status"] == "DENIED" and beta.send_calls == 2
        # A replay of an already accepted intent remains read-only after a downgrade.
        assert _call(client, operate_token, "auto_coder_reevaluate_target", _request(beta), 8) == first


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
    with TestClient(_application(runtime_boundary), base_url=settings.oauth.canonical_issuer) as client:
        fresh_token = _issue(client, settings, signed, "bob", "bob-client", READ_SCOPE)["access_token"]
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
        assert result == {"status": "CONFLICT", "reason": "supervisor_changed_during_diagnostic"}


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
