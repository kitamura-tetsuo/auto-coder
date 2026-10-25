"""Production-boundary tests for Firebase management identity admission."""

import io
import json
import time
from datetime import datetime, timedelta, timezone

import jwt
import pytest
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from firebase_admin import credentials
from google.auth.credentials import AnonymousCredentials

from auto_coder.logger_config import setup_logger
from auto_coder.management.identity import AdmissionStatus, FirebaseOperatorIdentity, IdentityConfig, ReasonCode, VerifiedPrincipal


class _OfflineCredential(credentials.Base):
    def get_credential(self):
        return AnonymousCredentials()


@pytest.fixture
def sdk_boundary(tmp_path, monkeypatch):
    """Use the real Admin SDK while replacing only its external HTTP transport."""

    project = "test-project"
    uid = "operator"
    now = int(time.time())
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "firebase-test")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
        .sign(key, hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM)
        .decode()
    )
    state = {"valid_since": "0", "disabled": False, "exists": True, "transport_error": None, "fail_on": None}

    def response(url, status, payload):
        result = requests.Response()
        result.status_code = status
        result.url = url
        result.headers["content-type"] = "application/json"
        result.headers["cache-control"] = "public, max-age=1"
        result._content = json.dumps(payload).encode()
        return result

    def controlled_request(_session, method, url, **_kwargs):
        if state["transport_error"] and (state["fail_on"] is None or state["fail_on"] in url):
            raise requests.Timeout(state["transport_error"])
        if "robot/v1/metadata/x509" in url:
            return response(url, 200, {"test-key": certificate})
        if url.endswith("/accounts:lookup"):
            users = [{"localId": uid, "disabled": state["disabled"], "validSince": state["valid_since"]}] if state["exists"] else []
            return response(url, 200, {"users": users})
        raise AssertionError(f"unexpected Firebase request: {method} {url}")

    monkeypatch.setattr("auto_coder.management.identity.credentials.ApplicationDefault", _OfflineCredential)
    monkeypatch.setattr(requests.sessions.Session, "request", controlled_request)
    policy = tmp_path / "operators.json"
    policy.write_text(json.dumps({"project_id": project, "allowed_uids": [uid]}), encoding="utf-8")
    adapter = FirebaseOperatorIdentity(IdentityConfig(project, policy), clock=lambda: now)

    def token(drop=None, **changes):
        claims = {
            "iss": f"https://securetoken.google.com/{project}",
            "aud": project,
            "sub": uid,
            "iat": now - 10,
            "exp": now + 300,
            "auth_time": now - 20,
            "firebase": {"sign_in_provider": "google.com"},
        }
        claims.update(changes)
        if drop:
            claims.pop(drop)
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-key"})

    return adapter, token, state, policy, now


def test_real_sdk_positive_revoked_and_cryptographic_outcomes(sdk_boundary):
    adapter, token, state, _policy, now = sdk_boundary
    permitted = adapter.verify_login(token())
    assert permitted.status is AdmissionStatus.PERMITTED
    assert permitted.principal == VerifiedPrincipal("test-project", "operator", now - 20)

    state["valid_since"] = str(now - 5)
    revoked = adapter.verify_login(token())
    assert (revoked.status, revoked.reason, revoked.principal) == (AdmissionStatus.DENIED, ReasonCode.SESSION_REVOKED, None)

    state["valid_since"] = "0"
    forged = jwt.encode(jwt.decode(token(), options={"verify_signature": False}), rsa.generate_private_key(public_exponent=65537, key_size=2048), algorithm="RS256", headers={"kid": "test-key"})
    assert adapter.verify_login(forged).status is AdmissionStatus.INVALID
    assert adapter.verify_login(token(exp=now - 1)).status is AdmissionStatus.INVALID
    unknown_key = jwt.encode(jwt.decode(token(), options={"verify_signature": False}), rsa.generate_private_key(public_exponent=65537, key_size=2048), algorithm="RS256", headers={"kid": "unknown"})
    assert adapter.verify_login(unknown_key).status is AdmissionStatus.INVALID
    unsigned = jwt.encode(jwt.decode(token(), options={"verify_signature": False}), key="", algorithm="none")
    assert adapter.verify_login(unsigned).status is AdmissionStatus.INVALID
    assert adapter.verify_login(token(aud="another-project")).status is AdmissionStatus.INVALID


@pytest.mark.parametrize(
    "claim,value,drop",
    [
        ("iat", None, False),
        ("iat", 1, False),
        ("auth_time", None, False),
        ("auth_time", 3600, False),
        ("exp", None, False),
        ("exp", -1, False),
        ("iat", True, False),
        ("auth_time", "1000", False),
        ("exp", float("inf"), False),
        ("auth_time", None, True),
    ],
)
def test_real_sdk_and_adapter_enforce_zero_skew_boundaries(sdk_boundary, claim, value, drop):
    adapter, token, _state, _policy, now = sdk_boundary
    boundary = now if value is None else now + value if isinstance(value, int) and not isinstance(value, bool) else value
    result = adapter.verify_login(token(drop=claim if drop else None, **({} if drop else {claim: boundary})))
    assert (result.status, result.principal) == (AdmissionStatus.INVALID, None)


def test_verified_tenant_provider_operator_and_current_account_denials(sdk_boundary):
    adapter, token, state, policy, _now = sdk_boundary
    tenant = adapter.verify_login(token(firebase={"tenant": "tenant-1", "sign_in_provider": "google.com"}))
    assert (tenant.status, tenant.principal) == (AdmissionStatus.INVALID, None)
    assert adapter.verify_login(token(firebase={"sign_in_provider": "password"})).reason is ReasonCode.NON_GOOGLE_IDENTITY

    policy.write_text(json.dumps({"project_id": "test-project", "allowed_uids": []}), encoding="utf-8")
    assert adapter.verify_login(token()).reason is ReasonCode.OPERATOR_NOT_ALLOWED
    policy.write_text(json.dumps({"project_id": "test-project", "allowed_uids": ["operator"]}), encoding="utf-8")
    state["disabled"] = True
    assert adapter.verify_login(token()).reason is ReasonCode.ACCOUNT_DISABLED
    state["disabled"] = False
    state["exists"] = False
    assert adapter.verify_login(token()).reason is ReasonCode.ACCOUNT_DELETED


def test_transport_failure_diagnostics_are_bounded_and_secret_free(sdk_boundary, tmp_path):
    adapter, token, state, _policy, _now = sdk_boundary
    sentinel = "SECRET-BEARER-AND-SERVICE-ACCOUNT-MATERIAL"
    state["transport_error"] = sentinel
    state["fail_on"] = "accounts:lookup"
    console = io.StringIO()
    log_file = tmp_path / "management.log"
    setup_logger(log_level="WARNING", log_file=str(log_file), stream=console)

    result = adapter.verify_login(token())

    console_value = console.getvalue()
    file_value = log_file.read_text(encoding="utf-8")
    assert (result.status, result.reason, result.principal) == (AdmissionStatus.UNAVAILABLE, ReasonCode.IDENTITY_SERVICE_UNAVAILABLE, None)
    assert "identity_service_unavailable" in console_value
    assert "identity_service_unavailable" in file_value
    assert sentinel not in console_value
    assert sentinel not in file_value
    assert sentinel not in repr(result)
