import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from auto_coder.management.identity import AdmissionResult, AdmissionStatus, VerifiedPrincipal
from auto_coder.management.oauth_config import OAuthClient, OAuthSettings
from auto_coder.management.oauth_grants import OAuthGrantService
from auto_coder.management.oauth_http import create_management_auth_app


class Identity:
    principal = VerifiedPrincipal("project", "operator", 100.0)

    def verify_login(self, token):
        return AdmissionResult(AdmissionStatus.PERMITTED, "allowed", principal=self.principal)

    def recheck_identity(self, principal):
        return AdmissionResult(AdmissionStatus.PERMITTED, "allowed", principal=principal)

    def check_operator_policy(self, principal):
        return AdmissionResult(AdmissionStatus.PERMITTED, "allowed", principal=principal)


def challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def setup(tmp_path):
    settings = OAuthSettings(tmp_path / "oauth.db", "http://localhost", "project", (OAuthClient("client", ("http://127.0.0.1:9876/cb",)),), firebase_web_config={"apiKey": "public", "authDomain": "project.firebaseapp.com", "projectId": "project"})
    grants = OAuthGrantService(settings, Identity())
    return settings, grants, TestClient(create_management_auth_app(settings, grants), base_url="http://localhost")


def begin(client, settings, verifier="a" * 43, state="state"):
    return client.get("/authorize", params={"response_type": "code", "client_id": "client", "redirect_uri": "http://127.0.0.1:9876/cb", "resource": settings.resource, "scope": "auto-coder.read", "state": state, "code_challenge": challenge(verifier), "code_challenge_method": "S256"})


def test_discovery_consent_exchange_refresh_and_revoke(tmp_path):
    settings, grants, client = setup(tmp_path)
    metadata = client.get("/.well-known/oauth-authorization-server").json()
    assert metadata["token_endpoint_auth_methods_supported"] == ["none"]
    assert metadata["authorization_response_iss_parameter_supported"] is True
    page = begin(client, settings)
    assert page.status_code == 200 and "Allow" in page.text
    data_text = page.text.split('<script id="auth-data" type="application/json">', 1)[1].split("</script>", 1)[0]
    import json

    data = json.loads(data_text)
    approved = client.post(f"/authorize/{data['requestId']}/allow", headers={"Origin": settings.canonical_issuer}, json={"csrf": data["csrf"], "firebaseIdToken": "signed"}, follow_redirects=False)
    assert approved.status_code == 303
    query = parse_qs(urlsplit(approved.headers["location"]).query)
    assert query["state"] == ["state"] and query["iss"] == [settings.canonical_issuer]
    token = client.post("/token", data={"grant_type": "authorization_code", "client_id": "client", "code": query["code"][0], "redirect_uri": "http://127.0.0.1:9876/cb", "resource": settings.resource, "code_verifier": "a" * 43})
    assert token.status_code == 200
    credentials = token.json()
    refreshed = client.post("/token", data={"grant_type": "refresh_token", "client_id": "client", "refresh_token": credentials["refresh_token"], "resource": settings.resource})
    assert refreshed.status_code == 200
    revoked = client.post("/revoke", data={"client_id": "client", "token": refreshed.json()["access_token"]})
    assert revoked.status_code == 200 and revoked.content == b""


def test_authorization_rejects_duplicates_and_binding_failures(tmp_path):
    settings, grants, client = setup(tmp_path)
    duplicate = client.get(f"/authorize?response_type=code&client_id=client&client_id=other&redirect_uri=http://127.0.0.1:9876/cb&resource={settings.resource}")
    assert duplicate.status_code == 400 and duplicate.json() == {"error": "invalid_request"}
    page = begin(client, settings)
    import json

    data = json.loads(page.text.split('<script id="auth-data" type="application/json">', 1)[1].split("</script>", 1)[0])
    wrong_origin = client.post(f"/authorize/{data['requestId']}/deny", headers={"Origin": "https://evil.example"}, json={"csrf": data["csrf"]})
    assert wrong_origin.status_code == 400
    denied = client.post(f"/authorize/{data['requestId']}/deny", headers={"Origin": settings.canonical_issuer}, json={"csrf": data["csrf"]}, follow_redirects=False)
    assert denied.status_code == 303
    query = parse_qs(urlsplit(denied.headers["location"]).query)
    assert query == {"error": ["access_denied"], "state": ["state"], "iss": [settings.canonical_issuer]}
    replay = client.post(f"/authorize/{data['requestId']}/allow", headers={"Origin": settings.canonical_issuer}, json={"csrf": data["csrf"], "firebaseIdToken": "signed"})
    assert replay.status_code == 400 and replay.json() == {"error": "invalid_request"}
