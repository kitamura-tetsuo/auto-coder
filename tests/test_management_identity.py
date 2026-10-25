import json
from types import SimpleNamespace

import pytest

from auto_coder.management.identity import AdmissionStatus, FirebaseOperatorIdentity, IdentityConfig, ReasonCode, VerifiedPrincipal


@pytest.fixture
def policy(tmp_path):
    path = tmp_path / "operators.json"
    path.write_text(json.dumps({"project_id": "project", "allowed_uids": ["operator"]}), encoding="utf-8")
    return path


@pytest.fixture
def adapter(policy, monkeypatch):
    value = FirebaseOperatorIdentity(IdentityConfig("project", policy), clock=lambda: 2000)
    value._app = object()  # Avoid credentials/network; verification behavior is patched below.
    monkeypatch.setattr("auto_coder.management.identity.auth.get_user", lambda uid, app: SimpleNamespace(disabled=False, tokens_valid_after_timestamp=1000000))
    return value


def test_login_preserves_original_principal_and_strict_times(adapter, monkeypatch):
    claims = {"sub": "operator", "iat": 1999, "exp": 2001, "auth_time": 1000, "firebase": {"sign_in_provider": "google.com"}}
    monkeypatch.setattr("auto_coder.management.identity.auth.verify_id_token", lambda *args, **kwargs: claims)
    result = adapter.verify_login("signed-token")
    assert result.status is AdmissionStatus.PERMITTED
    assert result.principal == VerifiedPrincipal("project", "operator", 1000)
    assert adapter.recheck_identity(result.principal).principal == result.principal

    for name, invalid in (("iat", 2000), ("exp", 2000), ("auth_time", True)):
        changed = dict(claims, **{name: invalid})
        monkeypatch.setattr("auto_coder.management.identity.auth.verify_id_token", lambda *args, value=changed, **kwargs: value)
        rejected = adapter.verify_login("signed-token")
        assert (rejected.status, rejected.principal) == (AdmissionStatus.INVALID, None)


def test_tenant_and_non_google_tokens_never_admit(adapter, monkeypatch):
    base = {"sub": "operator", "iat": 1999, "exp": 2001, "auth_time": 1000}
    monkeypatch.setattr("auto_coder.management.identity.auth.verify_id_token", lambda *args, **kwargs: {**base, "firebase": {"tenant": "t1", "sign_in_provider": "google.com"}})
    assert adapter.verify_login("token").reason is ReasonCode.INVALID_TOKEN
    monkeypatch.setattr("auto_coder.management.identity.auth.verify_id_token", lambda *args, **kwargs: {**base, "firebase": {"sign_in_provider": "password"}})
    assert adapter.verify_login("token").reason is ReasonCode.NON_GOOGLE_IDENTITY


def test_policy_only_has_no_firebase_access_and_observes_replacement(adapter, policy, monkeypatch):
    principal = VerifiedPrincipal("project", "operator", 1000)
    monkeypatch.setattr("auto_coder.management.identity.auth.get_user", lambda *args, **kwargs: pytest.fail("network access"))
    assert adapter.check_operator_policy(principal).status is AdmissionStatus.PERMITTED
    policy.write_text(json.dumps({"project_id": "project", "allowed_uids": []}), encoding="utf-8")
    assert adapter.check_operator_policy(principal).status is AdmissionStatus.DENIED
    policy.write_text("broken", encoding="utf-8")
    assert adapter.check_operator_policy(principal).status is AdmissionStatus.UNAVAILABLE


def test_recheck_enforces_original_auth_time_and_project(adapter, monkeypatch):
    principal = VerifiedPrincipal("project", "operator", 1000)
    monkeypatch.setattr("auto_coder.management.identity.auth.get_user", lambda uid, app: SimpleNamespace(disabled=False, tokens_valid_after_timestamp=1001000))
    assert adapter.recheck_identity(principal).reason is ReasonCode.SESSION_REVOKED
    assert adapter.recheck_identity(VerifiedPrincipal("other", "operator", 1000)).reason is ReasonCode.WRONG_PROJECT


def test_emulator_configuration_is_rejected(policy, monkeypatch):
    monkeypatch.setenv("FIREBASE_AUTH_EMULATOR_HOST", "localhost:9099")
    adapter = FirebaseOperatorIdentity(IdentityConfig("project", policy))
    assert adapter.verify_login("secret-token").reason is ReasonCode.INVALID_CONFIGURATION
