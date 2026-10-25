import base64
import hashlib
import sqlite3

import pytest

from auto_coder.management.identity import AdmissionResult, AdmissionStatus, ReasonCode, VerifiedPrincipal
from auto_coder.management.oauth_config import OAuthClient, OAuthSettings
from auto_coder.management.oauth_grants import (
    AuthorizationRequest,
    Denied,
    InvalidGrant,
    InvalidRequest,
    OAuthGrantService,
)


class IdentityBoundary:
    """Deterministic boundary double; identity.py has separate signed-token tests."""

    def __init__(self) -> None:
        self.allowed = True
        self.principal = VerifiedPrincipal("project", "operator", 100)

    def result(self):
        if self.allowed:
            return AdmissionResult(AdmissionStatus.PERMITTED, ReasonCode.PERMITTED, self.principal)
        return AdmissionResult(AdmissionStatus.DENIED, ReasonCode.OPERATOR_NOT_ALLOWED)

    def verify_login(self, token):
        return self.result()

    def recheck_identity(self, principal):
        return self.result()

    def check_operator_policy(self, principal):
        return self.result()


def challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


@pytest.fixture
def service(tmp_path):
    now = [1000.0]
    identity = IdentityBoundary()
    settings = OAuthSettings(
        tmp_path / "management" / "oauth.sqlite",
        "https://management.example",
        "project",
        (OAuthClient("client", ("https://client.example/callback",)),),
    )
    return OAuthGrantService(settings, identity, lambda: now[0]), identity, now


def issue(service):
    grants, _, _ = service
    verifier = "v" * 64
    pending = grants.begin_authorization(
        AuthorizationRequest("client", "https://client.example/callback", grants.settings.resource, challenge(verifier)),
        "browser",
        "csrf",
    )
    redirect = grants.approve_authorization(pending.request_id, "browser", "csrf", "signed-token")
    return grants.exchange_code("client", redirect.code, "https://client.example/callback", grants.settings.resource, verifier)


def test_issue_authenticate_rotate_replay_revoke_and_reopen(service):
    grants, identity, _ = service
    tokens = issue(service)
    authenticated = grants.authenticate_access(tokens.access_token, grants.settings.resource, ("auto-coder.read",))
    assert authenticated.principal.uid == "operator"
    reopened = OAuthGrantService(grants.settings, identity, grants.clock)
    rotated = reopened.refresh("client", tokens.refresh_token, grants.settings.resource)
    with pytest.raises(InvalidGrant):
        reopened.refresh("client", tokens.refresh_token, grants.settings.resource)
    with pytest.raises(InvalidGrant):
        reopened.authenticate_access(rotated.access_token, grants.settings.resource, ("auto-coder.read",))
    reopened.revoke("client", rotated.refresh_token)  # idempotent after family replay


def test_binding_mismatch_does_not_consume_pending_or_code(service):
    grants, _, _ = service
    verifier = "x" * 64
    pending = grants.begin_authorization(
        AuthorizationRequest("client", "https://client.example/callback", grants.settings.resource, challenge(verifier)),
        "browser",
        "csrf",
    )
    with pytest.raises(InvalidRequest):
        grants.deny_authorization(pending.request_id, "wrong", "csrf")
    approved = grants.approve_authorization(pending.request_id, "browser", "csrf", "token")
    with pytest.raises(InvalidGrant):
        grants.exchange_code("client", approved.code, "https://client.example/callback", grants.settings.resource, "wrong")
    assert grants.exchange_code("client", approved.code, "https://client.example/callback", grants.settings.resource, verifier).access_token


def test_denial_is_durable_and_policy_is_fresh(service):
    grants, identity, _ = service
    verifier = "z" * 64
    request = AuthorizationRequest("client", "https://client.example/callback", grants.settings.resource, challenge(verifier))
    pending = grants.begin_authorization(request, "browser", "csrf")
    assert grants.deny_authorization(pending.request_id, "browser", "csrf").denied
    with pytest.raises(InvalidGrant):
        OAuthGrantService(grants.settings, identity, grants.clock).approve_authorization(pending.request_id, "browser", "csrf", "token")
    identity.allowed = False
    pending2 = grants.begin_authorization(request, "browser2", "csrf2")
    with pytest.raises(Denied):
        grants.approve_authorization(pending2.request_id, "browser2", "csrf2", "token")


def test_database_contains_hashes_not_secrets(service):
    grants, _, _ = service
    verifier = "q" * 64
    browser_secret = "plaintext-browser-value-which-must-not-survive"
    pending = grants.begin_authorization(
        AuthorizationRequest(
            "client",
            "https://client.example/callback",
            grants.settings.resource,
            challenge(verifier),
        ),
        browser_secret,
        "plaintext-csrf-value-which-must-not-survive",
    )
    approved = grants.approve_authorization(pending.request_id, browser_secret, "plaintext-csrf-value-which-must-not-survive", "signed-token")
    tokens = grants.exchange_code("client", approved.code, "https://client.example/callback", grants.settings.resource, verifier)
    with sqlite3.connect(grants.settings.database_path) as db:
        dump = "\n".join(db.iterdump())
    assert tokens.access_token not in dump
    assert tokens.refresh_token not in dump
    assert "signed-token" not in dump
    assert browser_secret not in dump
    assert "plaintext-csrf-value-which-must-not-survive" not in dump


def test_authority_identity_prevents_reuse(service):
    grants, identity, _ = service
    incompatible = OAuthSettings(grants.settings.database_path, "https://other.example", "project", grants.settings.clients)
    with pytest.raises(ValueError, match="another authority"):
        OAuthGrantService(incompatible, identity)
