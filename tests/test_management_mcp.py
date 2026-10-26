"""Production-boundary regressions for the management MCP resource."""

import base64
import hashlib
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
from starlette.testclient import TestClient

from auto_coder.management.app import create_management_app
from auto_coder.management.identity import FirebaseOperatorIdentity, IdentityConfig
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
    origin = f"http://127.0.0.1:{port}"
    clients = (
        OAuthClient("alice-client", (f"{origin}/alice-callback",)),
        OAuthClient("bob-client", (f"{origin}/bob-callback",)),
    )
    oauth = OAuthSettings(tmp_path / "oauth.sqlite3", origin, project, clients, access_lifetime=30)
    settings = ManagementSettings(tmp_path, oauth, port=port, local_development=True, allowed_browser_origins=(origin,))
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


def _issue(client, settings, signed, uid: str, client_id: str) -> dict:
    verifier = (uid[0] * 43) if len(uid) else "v" * 43
    redirect_uri = f"{settings.oauth.canonical_issuer}/{uid}-callback"
    page = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "resource": settings.oauth.resource,
            "scope": "auto-coder.read",
            "state": f"state-{uid}",
            "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256",
        },
        follow_redirects=True,
    )
    assert page.status_code == 200
    data = json.loads(page.text.split('<script id="auth-data" type="application/json">', 1)[1].split("</script>", 1)[0])
    allowed = client.post(
        f"/authorize/{data['requestId']}/allow",
        headers={"Origin": settings.oauth.canonical_issuer},
        data={"csrf": data["csrf"], "firebase_id_token": signed(uid)},
        follow_redirects=False,
    )
    assert allowed.status_code == 303
    code = parse_qs(urlsplit(allowed.headers["location"]).query)["code"][0]
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


def test_real_overlapping_requests_keep_request_local_identity(real_boundary, monkeypatch) -> None:
    settings, grants, _identity, signed, _policy, _state = real_boundary
    with TestClient(create_management_app(settings, grants), base_url=settings.oauth.canonical_issuer) as client:
        tokens = {uid: _issue(client, settings, signed, uid, f"{uid}-client")["access_token"] for uid in ("alice", "bob")}
        original = grants.authenticate_access
        barrier = threading.Barrier(2)
        local = threading.local()

        def authenticate(token, resource, scopes):
            result = original(token, resource, scopes)
            if not getattr(local, "paused", False):
                local.paused = True
                barrier.wait(timeout=5)
            return result

        monkeypatch.setattr(grants, "authenticate_access", authenticate)

        def call(item):
            uid, token = item
            with TestClient(create_management_app(settings, grants), base_url=settings.oauth.canonical_issuer) as concurrent:
                response = _mcp(concurrent, token, "tools/call", 1, {"name": "auto_coder_whoami", "arguments": {}})
                return uid, response.json()["result"]["structuredContent"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = dict(pool.map(call, tokens.items()))
        assert results["alice"]["uid"] == "alice" and results["alice"]["client_id"] == "alice-client"
        assert results["bob"]["uid"] == "bob" and results["bob"]["client_id"] == "bob-client"


@pytest.mark.asyncio
async def test_live_sdk_http_client_calls_real_resource(real_boundary) -> None:
    settings, grants, _identity, signed, _policy, _state = real_boundary
    server = uvicorn.Server(uvicorn.Config(create_management_app(settings, grants), host=settings.bind_host, port=settings.port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    try:
        with httpx.Client(base_url=settings.oauth.canonical_issuer) as browser:
            token = _issue(browser, settings, signed, "alice", "alice-client")["access_token"]
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as authorized:
            async with streamable_http_client(settings.oauth.resource, http_client=authorized) as (read, write, _):
                async with ClientSession(read, write) as session:
                    initialized = await session.initialize()
                    assert initialized.protocolVersion == "2025-11-25"
                    result = await session.call_tool("auto_coder_whoami", {})
                    assert result.structuredContent["uid"] == "alice"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()
