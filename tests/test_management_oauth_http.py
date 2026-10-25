import base64
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from auto_coder.management.identity import AdmissionResult, AdmissionStatus, VerifiedPrincipal
from auto_coder.management.oauth_config import OAuthClient, OAuthSettings
from auto_coder.management.oauth_grants import OAuthGrantService
from auto_coder.management.oauth_http import create_management_auth_app
from auto_coder.management.oauth_provider import exchange_context
from tests.test_management_identity_sdk import sdk_boundary  # noqa: F401


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


def approve(client, settings, verifier):
    import json

    page = begin(client, settings, verifier=verifier)
    data = json.loads(page.text.split('<script id="auth-data" type="application/json">', 1)[1].split("</script>", 1)[0])
    result = client.post(
        f"/authorize/{data['requestId']}/allow",
        headers={"Origin": settings.canonical_issuer},
        data={"csrf": data["csrf"], "firebase_id_token": "signed"},
        follow_redirects=False,
    )
    return parse_qs(urlsplit(result.headers["location"]).query)["code"][0]


def test_discovery_consent_exchange_refresh_and_revoke(tmp_path):
    settings, grants, client = setup(tmp_path)
    metadata = client.get("/.well-known/oauth-authorization-server").json()
    assert metadata["token_endpoint_auth_methods_supported"] == ["none"]
    assert metadata["authorization_response_iss_parameter_supported"] is True
    page = begin(client, settings)
    assert page.status_code == 200 and "Allow" in page.text
    assert "script-src 'self' https://www.gstatic.com https://apis.google.com" in page.headers["content-security-policy"]
    data_text = page.text.split('<script id="auth-data" type="application/json">', 1)[1].split("</script>", 1)[0]
    import json

    data = json.loads(data_text)
    approved = client.post(f"/authorize/{data['requestId']}/allow", headers={"Origin": settings.canonical_issuer}, data={"csrf": data["csrf"], "firebase_id_token": "signed"}, follow_redirects=False)
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
    wrong_origin = client.post(f"/authorize/{data['requestId']}/deny", headers={"Origin": "https://evil.example"}, data={"csrf": data["csrf"]})
    assert wrong_origin.status_code == 400
    denied = client.post(f"/authorize/{data['requestId']}/deny", headers={"Origin": settings.canonical_issuer}, data={"csrf": data["csrf"]}, follow_redirects=False)
    assert denied.status_code == 303
    query = parse_qs(urlsplit(denied.headers["location"]).query)
    assert query == {"error": ["access_denied"], "state": ["state"], "iss": [settings.canonical_issuer]}
    replay = client.post(f"/authorize/{data['requestId']}/allow", headers={"Origin": settings.canonical_issuer}, data={"csrf": data["csrf"], "firebase_id_token": "signed"})
    assert replay.status_code == 400 and replay.json() == {"error": "invalid_request"}


def test_browser_decision_requires_unique_form_fields(tmp_path):
    settings, grants, client = setup(tmp_path)
    page = begin(client, settings)
    import json

    data = json.loads(page.text.split('<script id="auth-data" type="application/json">', 1)[1].split("</script>", 1)[0])
    endpoint = f"/authorize/{data['requestId']}/allow"
    headers = {"Origin": settings.canonical_issuer}
    assert client.post(endpoint, headers=headers, json={"csrf": data["csrf"]}).status_code == 400
    duplicate = client.post(
        endpoint,
        headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
        content=f"csrf={data['csrf']}&csrf={data['csrf']}&firebase_id_token=signed",
    )
    assert duplicate.status_code == 400 and duplicate.json() == {"error": "invalid_request"}

    script = client.get("/assets/authorize.js")
    assert script.status_code == 200
    assert "form.submit()" in script.text
    assert "fetch(" not in script.text


def test_real_firebase_verifier_is_reached_from_http_allow(tmp_path, sdk_boundary):
    identity, signed_token, _state, _policy, _now = sdk_boundary
    settings = OAuthSettings(
        tmp_path / "real-verifier.db",
        "http://localhost",
        "test-project",
        (OAuthClient("client", ("http://127.0.0.1:9876/cb",)),),
    )
    client = TestClient(create_management_auth_app(settings, OAuthGrantService(settings, identity)), base_url="http://localhost")
    page = begin(client, settings)
    import json

    data = json.loads(page.text.split('<script id="auth-data" type="application/json">', 1)[1].split("</script>", 1)[0])
    result = client.post(
        f"/authorize/{data['requestId']}/allow",
        headers={"Origin": settings.canonical_issuer},
        data={"csrf": data["csrf"], "firebase_id_token": signed_token()},
        follow_redirects=False,
    )
    assert result.status_code == 303
    assert "code=" in result.headers["location"]


def test_denial_survives_app_reconstruction_with_original_binding(tmp_path):
    settings, grants, client = setup(tmp_path)
    page = begin(client, settings)
    import json

    data = json.loads(page.text.split('<script id="auth-data" type="application/json">', 1)[1].split("</script>", 1)[0])
    cookie_name = f"ac_oauth_{data['requestId']}"
    binding = client.cookies.get(cookie_name)
    denied = client.post(
        f"/authorize/{data['requestId']}/deny",
        headers={"Origin": settings.canonical_issuer},
        data={"csrf": data["csrf"]},
        follow_redirects=False,
    )
    assert denied.status_code == 303

    reconstructed = TestClient(create_management_auth_app(settings, OAuthGrantService(settings, Identity())), base_url="http://localhost")
    reconstructed.cookies.set(cookie_name, binding)
    replay = reconstructed.post(
        f"/authorize/{data['requestId']}/allow",
        headers={"Origin": settings.canonical_issuer},
        data={"csrf": data["csrf"], "firebase_id_token": "signed"},
        follow_redirects=False,
    )
    assert replay.status_code == 400
    assert replay.json() == {"error": "invalid_grant"}
    assert exchange_context.get() is None


def test_overlapping_sdk_exchanges_keep_request_local_values(tmp_path):
    settings, grants, client = setup(tmp_path)
    first_verifier, second_verifier = "a" * 43, "b" * 43
    first_code = approve(client, settings, first_verifier)
    second_code = approve(client, settings, second_verifier)
    original = grants.exchange_code
    barrier = threading.Barrier(2)
    observed = []
    observed_lock = threading.Lock()

    def exchange(client_id, code, redirect_uri, resource, verifier):
        barrier.wait(timeout=5)
        with observed_lock:
            observed.append((code, verifier, resource))
        return original(client_id, code, redirect_uri, resource, verifier)

    grants.exchange_code = exchange

    def redeem(code, verifier):
        with TestClient(create_management_auth_app(settings, grants), base_url="http://localhost") as concurrent_client:
            return concurrent_client.post(
                "/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": "client",
                    "code": code,
                    "redirect_uri": "http://127.0.0.1:9876/cb",
                    "resource": settings.resource,
                    "code_verifier": verifier,
                },
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda item: redeem(*item), [(first_code, first_verifier), (second_code, second_verifier)]))

    assert [response.status_code for response in responses] == [200, 200]
    assert set(observed) == {(first_code, first_verifier, settings.resource), (second_code, second_verifier, settings.resource)}
    assert exchange_context.get() is None
