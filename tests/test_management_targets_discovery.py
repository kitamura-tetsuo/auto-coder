"""Authenticated target discovery through the production MCP resource."""

from __future__ import annotations

import http.client
import json
import sqlite3
from dataclasses import replace

import pytest
from starlette.testclient import TestClient

from auto_coder.management.action_scopes import READ_SCOPE
from auto_coder.management.app import create_management_app
from auto_coder.management.mcp_runtime_tools import ManagementRuntime
from auto_coder.management.oauth_grants import InvalidScope
from auto_coder.management.worker_network_client import WorkerNetworkClient
from tests.test_management_mcp import _issue, _mcp, real_boundary
from tests.test_management_runtime_mcp import _application, _call, runtime_boundary


def _policy(path, targets: str, profile: str = "observe") -> None:
    path.write_text(f"profile: {profile}\ntargets:\n{targets}", encoding="utf-8")


def test_authenticated_read_only_discovery_lists_actual_aliases_without_worker_io(runtime_boundary) -> None:
    settings, _grants, signed, policy, beta, release = runtime_boundary
    beta.config = replace(beta.config, target_id="north.beta")
    release.config = replace(release.config, target_id="zeta-release")
    _policy(policy, "  zeta-release:\n    channel: release\n    repositories: [fixture/release]\n  north.beta:\n    channel: beta\n    repositories: [fixture/beta]\n")
    app = _application(runtime_boundary)
    with TestClient(app, base_url=settings.oauth.canonical_issuer) as client:
        token = _issue(client, settings, signed, "alice", "alice-client", READ_SCOPE)["access_token"]
        listed = _mcp(client, token, "tools/list", 1).json()["result"]["tools"]
        registered = next(tool for tool in listed if tool["name"] == "auto_coder_targets")
        assert registered["annotations"] == {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
        assert registered["inputSchema"].get("required", []) == []
        assert registered["inputSchema"].get("properties", {}) == {}

        before = app.state.management_runtime.journal.list_operations().records
        response = _mcp(client, token, "tools/call", 2, {"name": "auto_coder_targets", "arguments": {}})
        expected = {
            "status": "OK",
            "reason": "inspectable_targets",
            "targets": [
                {"target_id": "north.beta", "repository": "fixture/beta", "channel": "beta"},
                {"target_id": "zeta-release", "repository": "fixture/release", "channel": "release"},
            ],
        }
        assert response.status_code == 200
        assert response.json()["result"]["structuredContent"] == expected
        assert not any(canary in response.text for canary in ("10.23.10.12", "17890", "aaaaaaaaaaaaaaaaaaaaaaaa"))
        assert (beta.inspect_calls, beta.read_calls, beta.send_calls) == (0, 0, 0)
        assert (release.inspect_calls, release.read_calls, release.send_calls) == (0, 0, 0)
        assert app.state.management_runtime.journal.list_operations().records == before

        invalid = _mcp(client, token, "tools/call", 3, {"name": "auto_coder_targets", "arguments": {"target_id": "north.beta"}})
        assert invalid.status_code == 200
        assert invalid.json() == {"jsonrpc": "2.0", "id": 3, "error": {"code": -32602, "message": "Invalid params"}}
        assert app.state.management_runtime.journal.list_operations().records == before
        release.healthy = False
        assert _call(client, token, "auto_coder_targets", {}, 4) == expected
        assert release.inspect_calls == 0

        _policy(policy, "  north.beta:\n    channel: beta\n    repositories: [fixture/wrong]\n")
        assert _call(client, token, "auto_coder_targets", {}, 5) == {"status": "OK", "reason": "inspectable_targets", "targets": []}
        assert _call(client, token, "auto_coder_status", {"target_id": "north.beta"}, 6) == {
            "status": "DENIED",
            "reason": "target_configuration_mismatch",
        }


def test_discovery_distinguishes_definitive_empty_from_unavailable(runtime_boundary) -> None:
    settings, _grants, signed, policy, beta, release = runtime_boundary
    with TestClient(_application(runtime_boundary), base_url=settings.oauth.canonical_issuer) as client:
        token = _issue(client, settings, signed, "alice", "alice-client")["access_token"]
        expected = [
            {"target_id": "beta-worker", "repository": "fixture/beta", "channel": "beta"},
            {"target_id": "release-worker", "repository": "fixture/release", "channel": "release"},
        ]
        assert _call(client, token, "auto_coder_targets", {}, 1)["targets"] == expected

        _policy(policy, "  no-client:\n    channel: beta\n    repositories: [fixture/beta]\n")
        assert _call(client, token, "auto_coder_targets", {}, 2) == {
            "status": "UNAVAILABLE",
            "reason": "target_classification_unavailable",
        }
        _policy(policy, "  beta-worker:\n    channel: beta\n    repositories: [fixture/beta]\n  no-client:\n    channel: release\n    repositories: [fixture/release]\n")
        assert _call(client, token, "auto_coder_targets", {}, 3)["status"] == "UNAVAILABLE"

        _policy(policy, "  beta-worker:\n    channel: beta\n    repositories: [fixture/wrong]\n")
        assert _call(client, token, "auto_coder_targets", {}, 4)["targets"] == []
        _policy(policy, "  bad alias:\n    channel: beta\n    repositories: [fixture/beta]\n")
        assert _call(client, token, "auto_coder_targets", {}, 5)["targets"] == []
        for body in (
            "profile: observe\ntargets: {}\n",
            "profile: unknown\ntargets:\n  beta-worker:\n    channel: beta\n    repositories: [fixture/beta]\n",
            "profile: beta\ntargets:\n  beta-worker:\n    channel: beta\n    repositories: [fixture/beta]\n  beta-worker:\n    channel: beta\n    repositories: [fixture/beta]\n",
        ):
            policy.write_text(body, encoding="utf-8")
            assert _call(client, token, "auto_coder_targets", {}, 6) == {
                "status": "UNAVAILABLE",
                "reason": "operating_policy_unavailable",
            }
        policy.unlink()
        assert _call(client, token, "auto_coder_targets", {}, 7)["status"] == "UNAVAILABLE"
        assert beta.inspect_calls == release.inspect_calls == 0
        assert beta.send_calls == release.send_calls == 0


def test_discovery_uses_existing_fresh_oauth_refusals(runtime_boundary, real_boundary, monkeypatch: pytest.MonkeyPatch) -> None:
    settings, grants, signed, _policy_path, beta, release = runtime_boundary
    _base, _base_grants, _identity, _signed, operator_policy, state = real_boundary
    with TestClient(_application(runtime_boundary), base_url=settings.oauth.canonical_issuer) as client:
        token = _issue(client, settings, signed, "alice", "alice-client")["access_token"]
        extra = {"name": "auto_coder_targets", "arguments": {"target_id": "beta-worker"}}
        authorized_extra = _mcp(client, token, "tools/call", 0, extra)
        assert authorized_extra.status_code == 200
        assert authorized_extra.json() == {"jsonrpc": "2.0", "id": 0, "error": {"code": -32602, "message": "Invalid params"}}

        invalid = _mcp(client, "not-a-token", "tools/call", 1, extra)
        assert invalid.status_code == 401
        assert invalid.json() == {"error": "invalid_token"}
        assert "resource_metadata" in invalid.headers["www-authenticate"]

        state["now"] += 31
        expired = _mcp(client, token, "tools/call", 2, extra)
        assert expired.status_code == 401
        assert "resource_metadata" in expired.headers["www-authenticate"]
        state["now"] -= 31

        operator_policy.write_text(json.dumps({"project_id": "mcp-test-project", "allowed_uids": []}), encoding="utf-8")
        denied = _mcp(client, token, "tools/call", 3, extra)
        assert denied.status_code == 403 and denied.json() == {"error": "access_denied"}
        operator_policy.write_text(json.dumps({"project_id": "mcp-test-project", "allowed_uids": ["alice", "bob"]}), encoding="utf-8")

        with monkeypatch.context() as patch:
            patch.setattr(grants, "authenticate_access", lambda *_args: (_ for _ in ()).throw(InvalidScope()))
            scope = _mcp(client, token, "tools/call", 4, extra)
            assert scope.status_code == 403 and scope.json() == {"error": "insufficient_scope"}
        with monkeypatch.context() as patch:
            patch.setattr(grants.store, "connect", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("private-path-canary")))
            unavailable = _mcp(client, token, "tools/call", 5, extra)
            assert unavailable.status_code == 503 and unavailable.json() == {"error": "temporarily_unavailable"}
            assert "private-path-canary" not in unavailable.text

        assert client.post("/revoke", data={"client_id": "alice-client", "token": token}).status_code == 200
        revoked = _mcp(client, token, "tools/call", 6, extra)
        assert revoked.status_code == 401
        assert "resource_metadata" in revoked.headers["www-authenticate"]
        assert beta.inspect_calls == release.inspect_calls == 0
        assert beta.send_calls == release.send_calls == 0



def _transport_observed_app(runtime_boundary):
    settings, grants, _signed, _policy_path, beta, release = runtime_boundary
    clients = {"beta": WorkerNetworkClient(beta.config), "release": WorkerNetworkClient(release.config)}
    return create_management_app(settings, grants, worker_clients=clients)


def _observe_worker_http(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    requests: list[tuple[str, str]] = []

    def reject_request(_connection, method: str, url: str, *_args, **_kwargs) -> None:
        requests.append((method, url))
        raise AssertionError("unexpected worker HTTP request")

    monkeypatch.setattr(http.client.HTTPConnection, "request", reject_request)
    return requests


def _assert_no_worker_requests(requests: list[tuple[str, str]]) -> None:
    assert requests == [], "unexpected worker HTTP request"


def test_discovery_has_no_real_worker_http_across_result_classes(runtime_boundary, monkeypatch: pytest.MonkeyPatch) -> None:
    settings, _grants, signed, policy, _beta, _release = runtime_boundary
    app = _transport_observed_app(runtime_boundary)
    requests = _observe_worker_http(monkeypatch)

    with TestClient(app, base_url=settings.oauth.canonical_issuer) as client:
        token = _issue(client, settings, signed, "alice", "alice-client")["access_token"]
        before = app.state.management_runtime.journal.list_operations().records

        assert len(_call(client, token, "auto_coder_targets", {}, 1)["targets"]) == 2
        _assert_no_worker_requests(requests)

        _policy(policy, "  beta-worker:\\n    channel: beta\\n    repositories: [fixture/wrong]\\n")
        assert _call(client, token, "auto_coder_targets", {}, 2)["targets"] == []
        _assert_no_worker_requests(requests)

        policy.unlink()
        assert _call(client, token, "auto_coder_targets", {}, 3)["status"] == "UNAVAILABLE"
        _assert_no_worker_requests(requests)

        extra = _mcp(client, token, "tools/call", 4, {"name": "auto_coder_targets", "arguments": {"target_id": "beta-worker"}})
        assert extra.status_code == 200
        assert extra.json() == {"jsonrpc": "2.0", "id": 4, "error": {"code": -32602, "message": "Invalid params"}}
        _assert_no_worker_requests(requests)
        assert app.state.management_runtime.journal.list_operations().records == before


@pytest.mark.parametrize("probe", ["inspect_supervisor", "observe_worker", "diagnostics"])
def test_discovery_transport_observer_detects_regressed_worker_probes(runtime_boundary, monkeypatch: pytest.MonkeyPatch, probe: str) -> None:
    settings, _grants, signed, _policy_path, _beta, _release = runtime_boundary
    original_targets = ManagementRuntime.targets

    async def regressed_targets(runtime: ManagementRuntime) -> dict[str, object]:
        worker = runtime.clients["beta-worker"]
        if probe == "diagnostics":
            worker.diagnostics("status")
        else:
            getattr(worker, probe)()
        return await original_targets(runtime)

    monkeypatch.setattr(ManagementRuntime, "targets", regressed_targets)
    app = _transport_observed_app(runtime_boundary)
    requests = _observe_worker_http(monkeypatch)
    with TestClient(app, base_url=settings.oauth.canonical_issuer) as client:
        token = _issue(client, settings, signed, "alice", "alice-client")["access_token"]
        _mcp(client, token, "tools/call", 1, {"name": "auto_coder_targets", "arguments": {}})
    assert requests == [("GET", "/v1/supervisor")]
    with pytest.raises(AssertionError, match="unexpected worker HTTP request"):
        _assert_no_worker_requests(requests)
