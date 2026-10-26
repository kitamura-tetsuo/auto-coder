"""Production-boundary regressions for the management MCP resource."""

import asyncio
import base64
import hashlib
import io
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
import requests
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from firebase_admin import credentials
from google.auth.credentials import AnonymousCredentials
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import ToolAnnotations
from starlette.testclient import TestClient

from auto_coder.logger_config import setup_logger
from auto_coder.management.action_scopes import DEPLOY_SCOPE, READ_SCOPE, PendingAuthorityStatus
from auto_coder.management.app import create_management_app
from auto_coder.management.identity import FirebaseOperatorIdentity, IdentityConfig
from auto_coder.management.mcp_resource import ManagementTool
from auto_coder.management.oauth_config import ManagementSettings, OAuthClient, OAuthSettings
from auto_coder.management.oauth_grants import InvalidScope, OAuthGrantService


class _OfflineCredential(credentials.Base):
    def get_credential(self):
        return AnonymousCredentials()


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


@pytest.fixture
def real_boundary(tmp_path, monkeypatch):
    """Real SQLite authority and Firebase SDK with only its network edge controlled."""

    project = "mcp-test-project"
    now = int(time.time())
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mcp-test")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
        .sign(key, hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM)
        .decode()
    )
    state = {"unavailable": False, "disabled": set(), "now": float(now)}

    def response(url, payload):
        result = requests.Response()
        result.status_code = 200
        result.url = url
        result.headers.update({"content-type": "application/json", "cache-control": "public, max-age=1"})
        result._content = json.dumps(payload).encode()
        return result

    def controlled_request(_session, _method, url, **kwargs):
        if state["unavailable"]:
            raise requests.Timeout("controlled Firebase outage")
        if "robot/v1/metadata/x509" in url:
            return response(url, {"mcp-key": certificate})
        if url.endswith("/accounts:lookup"):
            payload = kwargs.get("json") or json.loads(kwargs.get("data") or "{}")
            uid = payload.get("localId", [""])[0]
            return response(url, {"users": [{"localId": uid, "disabled": uid in state["disabled"], "validSince": "0"}]})
        raise AssertionError(f"unexpected Firebase request: {url}")

    monkeypatch.setattr("auto_coder.management.identity.credentials.ApplicationDefault", _OfflineCredential)
    monkeypatch.setattr(requests.sessions.Session, "request", controlled_request)
    policy = tmp_path / "allowed_uids.json"
    policy.write_text(json.dumps({"project_id": project, "allowed_uids": ["alice", "bob"]}), encoding="utf-8")
    port = _free_port()
    origin = "https://management.example.com"
    clients = (
        OAuthClient("alice-client", (f"{origin}/alice-callback",)),
        OAuthClient("bob-client", (f"{origin}/bob-callback",)),
    )
    oauth = OAuthSettings(tmp_path / "oauth.sqlite3", origin, project, clients, access_lifetime=30)
    settings = ManagementSettings(tmp_path, oauth, port=port, allowed_browser_origins=("https://console.example.com",))
    identity = FirebaseOperatorIdentity(IdentityConfig(project, policy), clock=lambda: state["now"])
    grants = OAuthGrantService(oauth, identity, clock=lambda: state["now"])

    def signed(uid: str) -> str:
        return jwt.encode(
            {
                "iss": f"https://securetoken.google.com/{project}",
                "aud": project,
                "sub": uid,
                "iat": now - 10,
                "exp": now + 300,
                "auth_time": now - 20,
                "firebase": {"sign_in_provider": "google.com"},
            },
            key,
            algorithm="RS256",
            headers={"kid": "mcp-key"},
        )

    return settings, grants, identity, signed, policy, state


def _authorize_code(client, settings, signed, uid: str, client_id: str, scope: str) -> tuple[str, str, str]:
    verifier = (uid[0] * 43) if len(uid) else "v" * 43
    redirect_uri = f"{settings.oauth.canonical_issuer}/{uid}-callback"
    page = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "resource": settings.oauth.resource,
            "scope": scope,
            "state": f"state-{uid}",
            "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256",
        },
        follow_redirects=True,
    )
    assert page.status_code == 200
    data = json.loads(page.text.split('<script id="auth-data" type="application/json">', 1)[1].split("</script>", 1)[0])
    assert data["scope"] == scope
    assert [item["scope"] for item in data["scopeLabels"]] == scope.split()
    allowed = client.post(
        f"/authorize/{data['requestId']}/allow",
        headers={"Origin": settings.oauth.canonical_issuer},
        data={"csrf": data["csrf"], "firebase_id_token": signed(uid)},
        follow_redirects=False,
    )
    assert allowed.status_code == 303
    code = parse_qs(urlsplit(allowed.headers["location"]).query)["code"][0]
    return code, verifier, redirect_uri


def _issue(client, settings, signed, uid: str, client_id: str, scope: str = "auto-coder.read") -> dict:
    code, verifier, redirect_uri = _authorize_code(client, settings, signed, uid, client_id, scope)
    token = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": redirect_uri,
            "resource": settings.oauth.resource,
            "code_verifier": verifier,
        },
    )
    assert token.status_code == 200
    return token.json()


def test_scoped_consent_metadata_and_tool_admission_use_production_boundaries(real_boundary, tmp_path) -> None:
    _settings, _grants, identity, signed, _policy, state = real_boundary
    root = tmp_path / "scoped"
    origin = "https://management.example.com"
    all_scopes = (READ_SCOPE, DEPLOY_SCOPE)
    broad_client = OAuthClient("alice-client", (f"{origin}/alice-callback",), allowed_scopes=all_scopes)
    oauth = OAuthSettings(root / "oauth.sqlite3", origin, "mcp-test-project", (broad_client,), access_lifetime=30, enabled_scopes=all_scopes)
    settings = ManagementSettings(root, oauth)
    grants = OAuthGrantService(oauth, identity, clock=lambda: state["now"])
    calls = {"deploy": 0}

    async def deploy_probe() -> dict[str, object]:
        calls["deploy"] += 1
        return {"admitted": True}

    tool = ManagementTool(
        "deploy_probe",
        deploy_probe,
        all_scopes,
        ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False),
    )
    with TestClient(create_management_app(settings, grants, (tool,)), base_url=origin) as client:
        authorization_metadata = client.get("/.well-known/oauth-authorization-server").json()
        resource_metadata = client.get("/.well-known/oauth-protected-resource/mcp").json()
        assert authorization_metadata["scopes_supported"] == list(all_scopes)
        assert resource_metadata["scopes_supported"] == list(all_scopes)

        old_read = _issue(client, settings, signed, "alice", "alice-client")
        denied = _mcp(client, old_read["access_token"], "tools/call", 1, {"name": "deploy_probe", "arguments": {}})
        assert denied.status_code == 403
        assert denied.headers["www-authenticate"].endswith(f'scope="{READ_SCOPE} {DEPLOY_SCOPE}"')
        assert calls == {"deploy": 0}
        assert _mcp(client, old_read["access_token"], "tools/list", 2).status_code == 200

        requested = f"{READ_SCOPE} {DEPLOY_SCOPE}"
        deployed = _issue(client, settings, signed, "alice", "alice-client", requested)
        assert deployed["scope"] == requested
        admitted = _mcp(client, deployed["access_token"], "tools/call", 3, {"name": "deploy_probe", "arguments": {}})
        assert admitted.status_code == 200
        assert admitted.json()["result"]["structuredContent"] == {"admitted": True}
        assert calls == {"deploy": 1}

        narrowed = client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "client_id": "alice-client",
                "refresh_token": deployed["refresh_token"],
                "resource": settings.oauth.resource,
                "scope": READ_SCOPE,
            },
        )
        assert narrowed.status_code == 200
        narrowed_tokens = narrowed.json()
        assert narrowed_tokens["scope"] == READ_SCOPE
        assert _mcp(client, narrowed_tokens["access_token"], "tools/call", 4, {"name": "deploy_probe", "arguments": {}}).status_code == 403
        assert _mcp(client, narrowed_tokens["access_token"], "tools/list", 5).status_code == 200
        narrowed_identity = _mcp(
            client,
            narrowed_tokens["access_token"],
            "tools/call",
            6,
            {"name": "auto_coder_whoami", "arguments": {}},
        ).json()[
            "result"
        ]["structuredContent"]
        assert narrowed_identity["scopes"] == [READ_SCOPE]
        assert calls == {"deploy": 1}

        refresh_widening = client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "client_id": "alice-client",
                "refresh_token": old_read["refresh_token"],
                "resource": settings.oauth.resource,
                "scope": f"{READ_SCOPE} {DEPLOY_SCOPE}",
            },
        )
        assert refresh_widening.status_code == 400
        assert refresh_widening.json()["error"] == "invalid_scope"

        read_code, read_verifier, read_redirect = _authorize_code(client, settings, signed, "alice", "alice-client", READ_SCOPE)
        exchange_widening = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "client_id": "alice-client",
                "code": read_code,
                "redirect_uri": read_redirect,
                "resource": settings.oauth.resource,
                "code_verifier": read_verifier,
                "scope": f"{READ_SCOPE} {DEPLOY_SCOPE}",
            },
        )
        assert exchange_widening.status_code == 400
        assert exchange_widening.json()["error"] == "invalid_scope"

    retained = grants.inspect_access("alice-client", deployed["access_token"])
    assert retained.scopes == all_scopes
    narrowed_binding = grants.inspect_access("alice-client", narrowed_tokens["access_token"])
    assert narrowed_binding.grant_id == retained.grant_id
    assert narrowed_binding.scopes == (READ_SCOPE,)
    reconstructed = OAuthGrantService(oauth, identity, grants.clock)
    successor_refresh = reconstructed.inspect_refresh("alice-client", narrowed_tokens["refresh_token"])
    assert successor_refresh.grant_id == retained.grant_id
    assert successor_refresh.scopes == (READ_SCOPE,)
    with TestClient(create_management_app(settings, reconstructed, (tool,)), base_url=origin) as client:
        successor_widening = client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "client_id": "alice-client",
                "refresh_token": narrowed_tokens["refresh_token"],
                "resource": settings.oauth.resource,
                "scope": f"{READ_SCOPE} {DEPLOY_SCOPE}",
            },
        )
        assert successor_widening.status_code == 400
        assert successor_widening.json()["error"] == "invalid_scope"
        successor = client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "client_id": "alice-client",
                "refresh_token": narrowed_tokens["refresh_token"],
                "resource": settings.oauth.resource,
            },
        )
        assert successor.status_code == 200
        successor_tokens = successor.json()
        assert successor_tokens["scope"] == READ_SCOPE
        assert _mcp(client, successor_tokens["access_token"], "tools/call", 7, {"name": "deploy_probe", "arguments": {}}).status_code == 403
        assert _mcp(client, successor_tokens["access_token"], "tools/list", 8).status_code == 200
        assert calls == {"deploy": 1}
    read_only_service_oauth = OAuthSettings(
        oauth.database_path,
        origin,
        oauth.firebase_project_id,
        (broad_client,),
        access_lifetime=30,
        enabled_scopes=(READ_SCOPE,),
    )
    read_only_service = ManagementSettings(root, read_only_service_oauth)
    with TestClient(
        create_management_app(read_only_service, OAuthGrantService(read_only_service_oauth, identity, grants.clock), (tool,)),
        base_url=origin,
    ) as client:
        assert _mcp(client, deployed["access_token"], "tools/call", 4, {"name": "deploy_probe", "arguments": {}}).status_code == 403
        assert _mcp(client, deployed["access_token"], "tools/list", 5).status_code == 200
        assert calls == {"deploy": 1}

    narrow_client = OAuthClient("alice-client", (f"{origin}/alice-callback",))
    narrow_client_oauth = OAuthSettings(
        oauth.database_path,
        origin,
        oauth.firebase_project_id,
        (narrow_client,),
        access_lifetime=30,
        enabled_scopes=all_scopes,
    )
    narrow_settings = ManagementSettings(root, narrow_client_oauth)
    with TestClient(
        create_management_app(narrow_settings, OAuthGrantService(narrow_client_oauth, identity, grants.clock), (tool,)),
        base_url=origin,
    ) as client:
        assert client.get("/.well-known/oauth-protected-resource/mcp").json()["scopes_supported"] == list(all_scopes)
        assert _mcp(client, deployed["access_token"], "tools/call", 6, {"name": "deploy_probe", "arguments": {}}).status_code == 403
        assert _mcp(client, deployed["access_token"], "tools/list", 7).status_code == 200
        assert calls == {"deploy": 1}
    assert grants.inspect_access("alice-client", deployed["access_token"]).scopes == all_scopes


def test_pending_scoped_authority_rereads_real_grant_and_uid_policy(real_boundary, tmp_path) -> None:
    _settings, _grants, identity, signed, policy, state = real_boundary
    root = tmp_path / "pending-scoped"
    origin = "https://management.example.com"
    scopes = (READ_SCOPE, DEPLOY_SCOPE)
    client_config = OAuthClient("alice-client", (f"{origin}/alice-callback",), allowed_scopes=scopes)
    oauth = OAuthSettings(
        root / "oauth.sqlite3",
        origin,
        "mcp-test-project",
        (client_config,),
        access_lifetime=30,
        grant_lifetime=120,
        enabled_scopes=scopes,
    )
    settings = ManagementSettings(root, oauth)
    grants = OAuthGrantService(oauth, identity, clock=lambda: state["now"])
    requested = f"{READ_SCOPE} {DEPLOY_SCOPE}"
    with TestClient(create_management_app(settings, grants), base_url=origin) as browser:
        tokens = _issue(browser, settings, signed, "alice", "alice-client", requested)
    authenticated = grants.authenticate_access(tokens["access_token"], oauth.resource, scopes)
    accepted = grants.accepted_authority(authenticated)

    state["now"] = authenticated.expires_at + 1
    reopened = OAuthGrantService(oauth, identity, clock=lambda: state["now"])
    assert reopened.check_pending_authority(accepted, scopes).status is PendingAuthorityStatus.PERMITTED

    policy.write_text(json.dumps({"project_id": "mcp-test-project", "allowed_uids": []}), encoding="utf-8")
    assert reopened.check_pending_authority(accepted, scopes).status is PendingAuthorityStatus.DENIED
    policy.write_text(json.dumps({"project_id": "mcp-test-project", "allowed_uids": ["alice"]}), encoding="utf-8")
    policy.rename(policy.with_suffix(".unavailable"))
    assert reopened.check_pending_authority(accepted, scopes).status is PendingAuthorityStatus.UNAVAILABLE
    policy.with_suffix(".unavailable").rename(policy)

    state["now"] = accepted.accepted_at + oauth.grant_lifetime + 1
    assert reopened.check_pending_authority(accepted, scopes).status is PendingAuthorityStatus.DENIED
    state["now"] = authenticated.expires_at + 1
    reopened.revoke("alice-client", tokens["refresh_token"])
    assert OAuthGrantService(oauth, identity, clock=lambda: state["now"]).check_pending_authority(accepted, scopes).status is PendingAuthorityStatus.DENIED


def _mcp(client, token: str, method: str, request_id: int, params=None, extra_headers=None):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
    headers.update(extra_headers or {})
    return client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}},
    )


def test_real_discovery_firebase_exchange_and_whoami(real_boundary) -> None:
    settings, grants, _identity, signed, _policy, _state = real_boundary
    with TestClient(create_management_app(settings, grants), base_url=settings.oauth.canonical_issuer) as client:
        challenge = client.post("/mcp", json={}).headers["www-authenticate"]
        assert f"{settings.oauth.canonical_issuer}/.well-known/oauth-protected-resource/mcp" in challenge
        resource = client.get("/.well-known/oauth-protected-resource/mcp").json()
        assert resource == {
            "resource": settings.oauth.resource,
            "authorization_servers": [settings.oauth.canonical_issuer + "/"],
            "scopes_supported": ["auto-coder.read"],
            "bearer_methods_supported": ["header"],
        }
        token = _issue(client, settings, signed, "alice", "alice-client")["access_token"]
        assert _mcp(client, token, "initialize", 1, {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}).status_code == 200
        listed = _mcp(client, token, "tools/list", 2).json()["result"]["tools"]
        assert [tool["name"] for tool in listed] == ["auto_coder_whoami"]
        assert listed[0]["annotations"]["readOnlyHint"] and listed[0]["annotations"]["idempotentHint"]
        identity = _mcp(client, token, "tools/call", 3, {"name": "auto_coder_whoami", "arguments": {}}).json()["result"]["structuredContent"]
        assert identity == {"project_id": "mcp-test-project", "uid": "alice", "client_id": "alice-client", "scopes": ["auto-coder.read"], "resource": settings.oauth.resource}


def test_stateless_get_and_delete_refuse_unauthenticated_requests(real_boundary) -> None:
    settings, grants, _identity, signed, _policy, _state = real_boundary
    with TestClient(create_management_app(settings, grants), base_url=settings.oauth.canonical_issuer) as client:
        token = _issue(client, settings, signed, "alice", "alice-client")["access_token"]
        for method in ("GET", "DELETE"):
            missing = client.request(method, "/mcp")
            assert missing.status_code == 401
            assert "resource_metadata" in missing.headers["www-authenticate"]
            invalid = client.request(method, "/mcp", headers={"Authorization": "Bearer invalid"})
            assert invalid.status_code == 401
        authorized_delete = client.request("DELETE", "/mcp", headers={"Authorization": f"Bearer {token}"})
        assert authorized_delete.status_code == 405


def test_production_origin_transport_accepts_only_configured_host_and_origins(real_boundary, tmp_path) -> None:
    _local, _grants, identity, signed, _policy, state = real_boundary
    root = tmp_path / "production"
    origin = "https://management.example.com"
    oauth = OAuthSettings(
        root / "oauth.sqlite3",
        origin,
        "mcp-test-project",
        (OAuthClient("alice-client", (f"{origin}/alice-callback",)),),
        access_lifetime=30,
    )
    settings = ManagementSettings(
        root,
        oauth,
        bind_host="127.0.0.1",
        port=8081,
        allowed_browser_origins=("https://console.example.com",),
    )
    grants = OAuthGrantService(oauth, identity, clock=lambda: state["now"])
    app = create_management_app(settings, grants)
    with TestClient(app, base_url=origin) as client:
        token = _issue(client, settings, signed, "alice", "alice-client")["access_token"]
        headers = {"Origin": "https://console.example.com"}
        initialized = _mcp(
            client,
            token,
            "initialize",
            1,
            {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "proxy-test", "version": "1"}},
            headers,
        )
        assert initialized.status_code == 200
        assert _mcp(client, token, "tools/list", 2, extra_headers=headers).status_code == 200
        whoami = _mcp(
            client,
            token,
            "tools/call",
            3,
            {"name": "auto_coder_whoami", "arguments": {}},
            headers,
        )
        assert whoami.status_code == 200
        assert whoami.json()["result"]["structuredContent"]["resource"] == f"{origin}/mcp"

        wrong_origin = _mcp(client, token, "tools/list", 4, extra_headers={"Origin": "https://attacker.example"})
        assert wrong_origin.status_code == 403
        wrong_host = _mcp(client, token, "tools/list", 5, extra_headers={"Host": "attacker.example"})
        assert wrong_host.status_code == 421
        metadata = client.get("/.well-known/oauth-protected-resource/mcp", headers={"Host": "attacker.example"}).json()
        assert metadata["resource"] == f"{origin}/mcp"
        assert metadata["authorization_servers"] == [origin + "/"]


def test_real_authority_refusals_revocation_policy_and_restart(real_boundary, monkeypatch, tmp_path) -> None:
    settings, grants, identity, signed, policy, state = real_boundary
    app = create_management_app(settings, grants)
    with TestClient(app, base_url=settings.oauth.canonical_issuer) as client:
        credentials = _issue(client, settings, signed, "alice", "alice-client")
        token = credentials["access_token"]
        for invalid in (signed("alice"), "outliner-token", "arbitrary"):
            assert _mcp(client, invalid, "tools/list", 1).status_code == 401
        foreign_oauth = OAuthSettings(
            tmp_path / "foreign" / "oauth.sqlite3",
            "http://127.0.0.1:49151",
            settings.oauth.firebase_project_id,
            (
                OAuthClient("alice-client", ("http://127.0.0.1:49151/alice-callback",)),
                OAuthClient("bob-client", ("http://127.0.0.1:49151/bob-callback",)),
            ),
            access_lifetime=30,
        )
        foreign_settings = ManagementSettings(tmp_path / "foreign", foreign_oauth, port=49151, local_development=True)
        foreign_grants = OAuthGrantService(foreign_oauth, identity, grants.clock)
        with TestClient(create_management_app(foreign_settings, foreign_grants), base_url=foreign_oauth.canonical_issuer) as foreign:
            foreign_token = _issue(foreign, foreign_settings, signed, "alice", "alice-client")["access_token"]
        assert _mcp(client, foreign_token, "tools/list", 2).status_code == 401
        state["now"] += 31
        assert _mcp(client, token, "tools/list", 3).status_code == 401
        state["now"] -= 31
        policy.write_text(json.dumps({"project_id": "mcp-test-project", "allowed_uids": []}), encoding="utf-8")
        assert _mcp(client, token, "tools/list", 4).status_code == 403
        policy.write_text(json.dumps({"project_id": "mcp-test-project", "allowed_uids": ["alice", "bob"]}), encoding="utf-8")
        original = grants.authenticate_access
        monkeypatch.setattr(grants, "authenticate_access", lambda *_args: (_ for _ in ()).throw(InvalidScope()))
        insufficient = _mcp(client, token, "tools/list", 5)
        assert insufficient.status_code == 403
        assert 'scope="auto-coder.read"' in insufficient.headers["www-authenticate"]
        monkeypatch.setattr(grants, "authenticate_access", original)
        assert client.post("/revoke", data={"client_id": "alice-client", "token": token}).status_code == 200
        assert _mcp(client, token, "tools/list", 6).status_code == 401

    reopened = OAuthGrantService(settings.oauth, identity, grants.clock)
    with TestClient(create_management_app(settings, reopened), base_url=settings.oauth.canonical_issuer) as client:
        assert _mcp(client, token, "tools/list", 4).status_code == 401
        fresh = _issue(client, settings, signed, "alice", "alice-client")["access_token"]
        monkeypatch.setattr(reopened.store, "connect", lambda: (_ for _ in ()).throw(__import__("sqlite3").OperationalError("controlled")))
        assert _mcp(client, fresh, "tools/list", 5).status_code == 503


def test_authentication_outcomes_are_logged_without_credentials(real_boundary, tmp_path) -> None:
    settings, grants, _identity, signed, _policy, _state = real_boundary
    output = io.StringIO()
    log_file = tmp_path / "management.log"
    setup_logger(stream=output, log_file=str(log_file))
    with TestClient(create_management_app(settings, grants), base_url=settings.oauth.canonical_issuer) as client:
        credentials = _issue(client, settings, signed, "alice", "alice-client")
        token = credentials["access_token"]
        unknown = "unknown-token-canary"
        assert client.post("/mcp?secret=query-canary", json={}).status_code == 401
        assert _mcp(client, unknown, "tools/list", 1).status_code == 401
        assert _mcp(client, token, "tools/list", 2).status_code == 200
        client.post("/revoke", data={"client_id": "alice-client", "token": token})
        assert _mcp(client, token, "tools/list", 3).status_code == 401
    text = output.getvalue() + log_file.read_text(encoding="utf-8")
    for outcome in ("invalid_credentials", "authorized"):
        assert f"Management MCP authentication outcome: {outcome}" in text
    for secret in (token, unknown, "query-canary", credentials["refresh_token"]):
        assert secret not in text


def test_real_overlapping_requests_keep_request_local_identity(real_boundary) -> None:
    settings, grants, _identity, signed, _policy, _state = real_boundary
    app = create_management_app(settings, grants)
    admission = app.routes[-1].app
    downstream = admission.app

    class PostAuthenticationBarrier:
        def __init__(self):
            self.entered = 0
            self.release = asyncio.Event()

        async def __call__(self, scope, receive, send):
            self.entered += 1
            if self.entered == 2:
                self.release.set()
            await asyncio.wait_for(self.release.wait(), timeout=5)
            await downstream(scope, receive, send)

    barrier = PostAuthenticationBarrier()
    admission.app = barrier
    with TestClient(app, base_url=settings.oauth.canonical_issuer) as client:
        tokens = {uid: _issue(client, settings, signed, uid, f"{uid}-client")["access_token"] for uid in ("alice", "bob")}

        def call(item):
            uid, token = item
            response = _mcp(client, token, "tools/call", 1, {"name": "auto_coder_whoami", "arguments": {}})
            return uid, response.json()["result"]["structuredContent"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = dict(pool.map(call, tokens.items()))
        assert barrier.entered == 2
        assert results["alice"]["uid"] == "alice" and results["alice"]["client_id"] == "alice-client"
        assert results["bob"]["uid"] == "bob" and results["bob"]["client_id"] == "bob-client"
        assert _mcp(client, "denied-after-overlap", "tools/call", 2, {"name": "auto_coder_whoami", "arguments": {}}).status_code == 401
        after = _mcp(client, tokens["alice"], "tools/call", 3, {"name": "auto_coder_whoami", "arguments": {}})
        assert after.json()["result"]["structuredContent"]["uid"] == "alice"
        assert after.json()["result"]["structuredContent"]["client_id"] == "alice-client"


def test_refresh_replay_revokes_next_mcp_request_and_survives_restart(real_boundary) -> None:
    settings, grants, identity, signed, _policy, _state = real_boundary
    with TestClient(create_management_app(settings, grants), base_url=settings.oauth.canonical_issuer) as client:
        issued = _issue(client, settings, signed, "alice", "alice-client")
        rotated = client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "client_id": "alice-client",
                "refresh_token": issued["refresh_token"],
                "resource": settings.oauth.resource,
            },
        )
        assert rotated.status_code == 200
        replay = client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "client_id": "alice-client",
                "refresh_token": issued["refresh_token"],
                "resource": settings.oauth.resource,
            },
        )
        assert replay.status_code == 400
        assert replay.json()["error"] == "invalid_grant"
        assert _mcp(client, issued["access_token"], "tools/list", 1).status_code == 401
        assert _mcp(client, rotated.json()["access_token"], "tools/list", 2).status_code == 401

    reopened = OAuthGrantService(settings.oauth, identity, grants.clock)
    with TestClient(create_management_app(settings, reopened), base_url=settings.oauth.canonical_issuer) as client:
        assert _mcp(client, issued["access_token"], "tools/list", 3).status_code == 401
        assert _mcp(client, rotated.json()["access_token"], "tools/list", 4).status_code == 401


@pytest.mark.asyncio
async def test_live_sdk_http_client_calls_real_resource(real_boundary) -> None:
    settings, grants, _identity, signed, _policy, _state = real_boundary
    with TestClient(create_management_app(settings, grants), base_url=settings.oauth.canonical_issuer) as browser:
        token = _issue(browser, settings, signed, "alice", "alice-client")["access_token"]
    server = uvicorn.Server(uvicorn.Config(create_management_app(settings, grants), host=settings.bind_host, port=settings.port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    try:
        internal_origin = f"http://{settings.bind_host}:{settings.port}"
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}", "Host": "management.example.com"}, trust_env=False) as authorized:
            async with streamable_http_client(f"{internal_origin}/mcp", http_client=authorized) as (read, write, _):
                async with ClientSession(read, write) as session:
                    initialized = await session.initialize()
                    assert initialized.protocolVersion == "2025-11-25"
                    result = await session.call_tool("auto_coder_whoami", {})
                    assert result.structuredContent["uid"] == "alice"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()
