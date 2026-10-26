import time

from fastapi.testclient import TestClient

from auto_coder.management.app import create_management_app
from auto_coder.management.identity import VerifiedPrincipal
from auto_coder.management.oauth_config import ManagementSettings, OAuthClient, OAuthSettings
from auto_coder.management.oauth_grants import AuthenticatedGrant, Denied, InvalidGrant, Unavailable


class Grants:
    def __init__(self, settings: OAuthSettings) -> None:
        self.settings = settings
        self.outcome = "valid"

    def authenticate_access(self, token: str, resource: str, required_scopes: tuple[str, ...]) -> AuthenticatedGrant:
        if self.outcome == "denied":
            raise Denied()
        if self.outcome == "unavailable":
            raise Unavailable()
        if token != "valid":
            raise InvalidGrant()
        return AuthenticatedGrant(VerifiedPrincipal("project", "operator", 1), "desktop", resource, required_scopes, time.time() + 60)


def settings(tmp_path) -> ManagementSettings:
    oauth = OAuthSettings(tmp_path / "oauth.sqlite3", "http://127.0.0.1:8081", "project", (OAuthClient("desktop", ("http://127.0.0.1:9191/callback",)),))
    return ManagementSettings(tmp_path, oauth, local_development=True, allowed_browser_origins=("http://127.0.0.1:3000",))


def test_resource_refuses_before_dispatch_and_publishes_canonical_metadata(tmp_path) -> None:
    configured = settings(tmp_path)
    app = create_management_app(configured, Grants(configured.oauth))
    with TestClient(app, base_url=configured.oauth.canonical_issuer) as client:
        refused = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert refused.status_code == 401
        assert "/.well-known/oauth-protected-resource/mcp" in refused.headers["www-authenticate"]
        metadata = client.get("/.well-known/oauth-protected-resource/mcp")
        assert metadata.status_code == 200
        assert metadata.json()["resource"] == configured.oauth.resource
        assert metadata.json()["authorization_servers"] == [configured.oauth.canonical_issuer + "/"]
        assert client.post("/mcp?access_token=valid", json={}).status_code == 401


def test_typed_refusals_and_preflight(tmp_path) -> None:
    configured = settings(tmp_path)
    grants = Grants(configured.oauth)
    app = create_management_app(configured, grants)
    with TestClient(app, base_url=configured.oauth.canonical_issuer) as client:
        grants.outcome = "denied"
        denied = client.post("/mcp", headers={"Authorization": "Bearer valid"}, json={})
        assert denied.status_code == 403
        assert 'scope="auto-coder.read"' in denied.headers["www-authenticate"]
        grants.outcome = "unavailable"
        assert client.post("/mcp", headers={"Authorization": "Bearer valid"}, json={}).status_code == 503
        preflight = client.options("/mcp", headers={"Origin": "http://127.0.0.1:3000"})
        assert preflight.status_code == 204
        assert preflight.headers["access-control-allow-origin"] == "http://127.0.0.1:3000"


def test_mounted_sdk_calls_only_management_tool(tmp_path) -> None:
    # TestClient validates the actual mounted SDK lifecycle and protocol routes;
    # the transport-specific SDK client is covered against a live server in CI.
    configured = settings(tmp_path)
    app = create_management_app(configured, Grants(configured.oauth))
    with TestClient(app, base_url=configured.oauth.canonical_issuer) as client:
        initialize = client.post(
            "/mcp",
            headers={"Authorization": "Bearer valid", "Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}},
        )
        assert initialize.status_code == 200
        listed = client.post(
            "/mcp",
            headers={"Authorization": "Bearer valid", "Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        tools = listed.json()["result"]["tools"]
        assert [tool["name"] for tool in tools] == ["auto_coder_whoami"]
        assert tools[0]["annotations"]["readOnlyHint"] is True
        called = client.post(
            "/mcp",
            headers={"Authorization": "Bearer valid", "Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "auto_coder_whoami", "arguments": {}}},
        )
        assert called.status_code == 200
        assert called.json()["result"]["structuredContent"] == {
            "project_id": "project",
            "uid": "operator",
            "client_id": "desktop",
            "scopes": ["auto-coder.read"],
            "resource": configured.oauth.resource,
        }
