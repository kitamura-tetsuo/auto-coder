"""Production-boundary lifecycle evidence for management OAuth grants."""

import base64
import hashlib
import io
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
from auto_coder.management.identity import FirebaseOperatorIdentity, IdentityConfig
from auto_coder.management.oauth_config import OAuthClient, OAuthSettings
from auto_coder.management.oauth_grants import (
    AuthorizationRequest,
    Denied,
    InvalidClient,
    InvalidGrant,
    InvalidRequest,
    OAuthGrantService,
    Unavailable,
)


class _OfflineCredential(credentials.Base):
    def get_credential(self):
        return AnonymousCredentials()


@pytest.fixture
def production_boundary(tmp_path, monkeypatch):
    """Real Firebase SDK verification with only external HTTP under test control."""

    project, uid = "oauth-test-project", "operator"
    firebase_now = int(time.time())
    clock = [float(firebase_now)]
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "oauth-test")])
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
    state = {"valid_since": "0", "disabled": False, "exists": True, "transport_error": False}

    def response(url, payload):
        result = requests.Response()
        result.status_code = 200
        result.url = url
        result.headers.update({"content-type": "application/json", "cache-control": "public, max-age=1"})
        result._content = json.dumps(payload).encode()
        return result

    def controlled_request(_session, _method, url, **_kwargs):
        if state["transport_error"] and url.endswith("/accounts:lookup"):
            raise requests.Timeout("controlled account outage")
        if "robot/v1/metadata/x509" in url:
            return response(url, {"oauth-key": certificate})
        if url.endswith("/accounts:lookup"):
            users = [{"localId": uid, "disabled": state["disabled"], "validSince": state["valid_since"]}] if state["exists"] else []
            return response(url, {"users": users})
        raise AssertionError(f"unexpected Firebase request: {url}")

    monkeypatch.setattr("auto_coder.management.identity.credentials.ApplicationDefault", _OfflineCredential)
    monkeypatch.setattr(requests.sessions.Session, "request", controlled_request)
    policy = tmp_path / "operators.json"
    policy.write_text(json.dumps({"project_id": project, "allowed_uids": [uid]}), encoding="utf-8")
    identity = FirebaseOperatorIdentity(IdentityConfig(project, policy), clock=lambda: firebase_now)
    settings = OAuthSettings(
        tmp_path / "management" / "oauth.sqlite",
        "https://management.example",
        project,
        (OAuthClient("client", ("https://client.example/callback",)),),
        access_lifetime=30,
        grant_lifetime=120,
    )

    def signed_token():
        claims = {
            "iss": f"https://securetoken.google.com/{project}",
            "aud": project,
            "sub": uid,
            "iat": firebase_now - 10,
            "exp": firebase_now + 300,
            "auth_time": firebase_now - 20,
            "firebase": {"sign_in_provider": "google.com"},
        }
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "oauth-key"})

    service = OAuthGrantService(settings, identity, lambda: clock[0])
    return service, identity, signed_token, state, policy, clock


def pkce(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def begin(service, suffix=""):
    verifier = ("v" + suffix) * 32
    pending = service.begin_authorization(
        AuthorizationRequest("client", "https://client.example/callback", service.settings.resource, pkce(verifier), state=f"state-{suffix}"),
        f"browser-{suffix}",
        f"csrf-{suffix}",
    )
    return pending, verifier


def issue(boundary, suffix=""):
    service, _identity, signed_token, _state, _policy, _clock = boundary
    pending, verifier = begin(service, suffix)
    redirect = service.approve_authorization(pending.request_id, f"browser-{suffix}", f"csrf-{suffix}", signed_token())
    return service.exchange_code("client", redirect.code, "https://client.example/callback", service.settings.resource, verifier)


def test_signed_identity_lifecycle_reconstructs_and_crosses_expiries(production_boundary):
    service, identity, signed_token, _state, _policy, clock = production_boundary
    pending, verifier = begin(service, "code")
    redirect = service.approve_authorization(pending.request_id, "browser-code", "csrf-code", signed_token())
    clock[0] += 121
    reopened = OAuthGrantService(service.settings, identity, lambda: clock[0])
    with pytest.raises(InvalidGrant):
        reopened.exchange_code("client", redirect.code, "https://client.example/callback", reopened.settings.resource, verifier)

    clock[0] -= 121
    tokens = issue(production_boundary, "access")
    clock[0] += 31
    reopened = OAuthGrantService(service.settings, identity, lambda: clock[0])
    with pytest.raises(InvalidGrant):
        reopened.authenticate_access(tokens.access_token, reopened.settings.resource, ("auto-coder.read",))
    clock[0] += 90
    with pytest.raises(InvalidGrant):
        OAuthGrantService(service.settings, identity, lambda: clock[0]).refresh("client", tokens.refresh_token, service.settings.resource)

    pending, _ = begin(service, "pending")
    clock[0] += 601
    with pytest.raises(InvalidGrant):
        OAuthGrantService(service.settings, identity, lambda: clock[0]).approve_authorization(pending.request_id, "browser-pending", "csrf-pending", signed_token())


def test_current_account_policy_and_authority_outages_are_typed(production_boundary, monkeypatch):
    service, _identity, signed_token, state, policy, _clock = production_boundary
    pending, _ = begin(service, "authority")
    state["disabled"] = True
    with pytest.raises(Denied):
        service.approve_authorization(pending.request_id, "browser-authority", "csrf-authority", signed_token())
    state["disabled"] = False
    state["transport_error"] = True
    with pytest.raises(Unavailable):
        service.approve_authorization(pending.request_id, "browser-authority", "csrf-authority", signed_token())
    state["transport_error"] = False
    policy.write_text("not-json", encoding="utf-8")
    with pytest.raises(Unavailable):
        service.approve_authorization(pending.request_id, "browser-authority", "csrf-authority", signed_token())

    monkeypatch.setattr(service.store, "connect", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    with pytest.raises(Unavailable):
        service.begin_authorization(
            AuthorizationRequest("client", "https://client.example/callback", service.settings.resource, pkce("s" * 64)),
            "browser-store",
            "csrf-store",
        )


def test_production_bindings_and_current_authority_cannot_be_bypassed(production_boundary):
    service, _identity, signed_token, state, policy, _clock = production_boundary
    pending, verifier = begin(service, "bindings")
    with pytest.raises(InvalidRequest):
        service.approve_authorization(pending.request_id, "wrong-browser", "csrf-bindings", signed_token())
    redirect = service.approve_authorization(pending.request_id, "browser-bindings", "csrf-bindings", signed_token())
    with pytest.raises(InvalidGrant):
        service.exchange_code("client", redirect.code, "https://client.example/callback", service.settings.resource, "wrong-verifier")
    policy.write_text(json.dumps({"project_id": "oauth-test-project", "allowed_uids": []}), encoding="utf-8")
    with pytest.raises(Denied):
        service.exchange_code("client", redirect.code, "https://client.example/callback", service.settings.resource, verifier)

    policy.write_text(json.dumps({"project_id": "oauth-test-project", "allowed_uids": ["operator"]}), encoding="utf-8")
    tokens = service.exchange_code("client", redirect.code, "https://client.example/callback", service.settings.resource, verifier)
    state["valid_since"] = str(int(time.time()) + 1)
    with pytest.raises(Denied):
        service.refresh("client", tokens.refresh_token, service.settings.resource)


@pytest.mark.parametrize(
    "malformed_challenge",
    (
        "!" * 43,
        "A" * 42,
        "A" * 44,
        "A" * 42 + "B",  # decodes to 32 bytes but is not canonical base64url
    ),
)
def test_malformed_s256_challenge_is_rejected_before_persistence(production_boundary, malformed_challenge):
    service, identity, signed_token, _state, _policy, clock = production_boundary
    with pytest.raises(InvalidRequest):
        service.begin_authorization(
            AuthorizationRequest(
                "client",
                "https://client.example/callback",
                service.settings.resource,
                malformed_challenge,
            ),
            "browser-malformed",
            "csrf-malformed",
        )

    reopened = OAuthGrantService(service.settings, identity, lambda: clock[0])
    with sqlite3.connect(reopened.settings.database_path) as db:
        assert db.execute("SELECT count(*) FROM pending").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM codes").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM grants").fetchone()[0] == 0

    pending, verifier = begin(reopened, "valid-s256")
    code = reopened.approve_authorization(pending.request_id, "browser-valid-s256", "csrf-valid-s256", signed_token()).code
    tokens = reopened.exchange_code("client", code, "https://client.example/callback", reopened.settings.resource, verifier)
    assert reopened.authenticate_access(tokens.access_token, reopened.settings.resource, ("auto-coder.read",)).principal.uid == "operator"


def test_access_admission_rechecks_current_registered_client(production_boundary):
    service, identity, signed_token, _state, _policy, clock = production_boundary
    old_tokens = issue(production_boundary, "client-a")
    replacement_settings = OAuthSettings(
        service.settings.database_path,
        service.settings.issuer,
        service.settings.firebase_project_id,
        (OAuthClient("client-b", ("https://client-b.example/callback",)),),
        access_lifetime=service.settings.access_lifetime,
        grant_lifetime=service.settings.grant_lifetime,
    )
    reopened = OAuthGrantService(replacement_settings, identity, lambda: clock[0])
    with pytest.raises(InvalidClient):
        reopened.authenticate_access(old_tokens.access_token, reopened.settings.resource, ("auto-coder.read",))

    verifier = "b" * 64
    pending = reopened.begin_authorization(
        AuthorizationRequest("client-b", "https://client-b.example/callback", reopened.settings.resource, pkce(verifier)),
        "browser-b",
        "csrf-b",
    )
    code = reopened.approve_authorization(pending.request_id, "browser-b", "csrf-b", signed_token()).code
    new_tokens = reopened.exchange_code("client-b", code, "https://client-b.example/callback", reopened.settings.resource, verifier)
    assert reopened.authenticate_access(new_tokens.access_token, reopened.settings.resource, ("auto-coder.read",)).client_id == "client-b"


def test_mismatched_firebase_project_cannot_create_authority(production_boundary, tmp_path):
    service, identity, signed_token, _state, _policy, clock = production_boundary
    settings = OAuthSettings(
        tmp_path / "wrong-project.sqlite",
        service.settings.issuer,
        "different-project",
        service.settings.clients,
    )
    mismatched = OAuthGrantService(settings, identity, lambda: clock[0])
    pending, _ = begin(mismatched, "wrong-project")
    with pytest.raises(Denied):
        mismatched.approve_authorization(pending.request_id, "browser-wrong-project", "csrf-wrong-project", signed_token())
    with sqlite3.connect(settings.database_path) as db:
        assert db.execute("SELECT count(*) FROM codes").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM grants").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM access_tokens").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM refresh_tokens").fetchone()[0] == 0


def test_grant_absolute_expiry_starts_at_approval(production_boundary, tmp_path):
    service, identity, signed_token, _state, _policy, clock = production_boundary
    settings = OAuthSettings(
        tmp_path / "default-lifetime.sqlite",
        service.settings.issuer,
        service.settings.firebase_project_id,
        service.settings.clients,
    )
    grants = OAuthGrantService(settings, identity, lambda: clock[0])
    approved_at = clock[0]
    pending, verifier = begin(grants, "absolute")
    code = grants.approve_authorization(pending.request_id, "browser-absolute", "csrf-absolute", signed_token()).code
    clock[0] += 100
    reopened = OAuthGrantService(settings, identity, lambda: clock[0])
    tokens = reopened.exchange_code("client", code, "https://client.example/callback", reopened.settings.resource, verifier)
    with sqlite3.connect(settings.database_path) as db:
        deadline = db.execute("SELECT expires FROM grants").fetchone()[0]
    assert deadline == approved_at + settings.grant_lifetime
    clock[0] = approved_at + settings.grant_lifetime + 50
    with pytest.raises(InvalidGrant):
        OAuthGrantService(settings, identity, lambda: clock[0]).refresh("client", tokens.refresh_token, settings.resource)


def test_final_policy_observation_wins_without_consuming_state(production_boundary, monkeypatch):
    service, identity, signed_token, _state, policy, clock = production_boundary
    allowed = json.dumps({"project_id": service.settings.firebase_project_id, "allowed_uids": ["operator"]})
    denied = json.dumps({"project_id": service.settings.firebase_project_id, "allowed_uids": []})

    pending, verifier = begin(service, "fresh-policy")
    original_verify = identity.verify_login

    def verify_then_remove(token):
        result = original_verify(token)
        assert result.principal is not None
        policy.write_text(denied, encoding="utf-8")
        return result

    monkeypatch.setattr(identity, "verify_login", verify_then_remove)
    with pytest.raises(Denied):
        service.approve_authorization(pending.request_id, "browser-fresh-policy", "csrf-fresh-policy", signed_token())
    with sqlite3.connect(service.settings.database_path) as db:
        assert db.execute("SELECT status FROM pending WHERE id=?", (pending.request_id,)).fetchone()[0] == "pending"
        assert db.execute("SELECT count(*) FROM codes WHERE pending_id=?", (pending.request_id,)).fetchone()[0] == 0

    monkeypatch.setattr(identity, "verify_login", original_verify)
    policy.write_text(allowed, encoding="utf-8")
    code = service.approve_authorization(pending.request_id, "browser-fresh-policy", "csrf-fresh-policy", signed_token()).code
    original_recheck = identity.recheck_identity

    def recheck_then_remove(principal):
        result = original_recheck(principal)
        assert result.principal is not None
        policy.write_text(denied, encoding="utf-8")
        return result

    monkeypatch.setattr(identity, "recheck_identity", recheck_then_remove)
    with pytest.raises(Denied):
        service.exchange_code("client", code, "https://client.example/callback", service.settings.resource, verifier)
    reopened = OAuthGrantService(service.settings, identity, lambda: clock[0])
    assert reopened.inspect_code("client", code).active

    monkeypatch.setattr(identity, "recheck_identity", original_recheck)
    policy.write_text(allowed, encoding="utf-8")
    tokens = reopened.exchange_code("client", code, "https://client.example/callback", reopened.settings.resource, verifier)
    monkeypatch.setattr(identity, "recheck_identity", recheck_then_remove)
    with pytest.raises(Denied):
        reopened.refresh("client", tokens.refresh_token, reopened.settings.resource)
    monkeypatch.setattr(identity, "recheck_identity", original_recheck)
    policy.write_text(allowed, encoding="utf-8")
    reconstructed = OAuthGrantService(service.settings, identity, lambda: clock[0])
    assert reconstructed.inspect_refresh("client", tokens.refresh_token).active


class _CommitUncertainConnection:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, exc_type, exc, traceback):
        result = self.connection.__exit__(exc_type, exc, traceback)
        if exc_type is None:
            raise sqlite3.OperationalError("commit acknowledgement lost")
        return result

    def execute(self, *args, **kwargs):
        return self.connection.execute(*args, **kwargs)


class _SelectFailureConnection:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, exc_type, exc, traceback):
        return self.connection.__exit__(exc_type, exc, traceback)

    def execute(self, sql, *args, **kwargs):
        if sql.lstrip().upper().startswith("SELECT"):
            raise sqlite3.OperationalError("controlled SELECT failure")
        return self.connection.execute(sql, *args, **kwargs)


def test_all_public_read_paths_translate_connection_and_select_failures(production_boundary, monkeypatch):
    service, _identity, signed_token, _state, _policy, _clock = production_boundary
    tokens = issue(production_boundary, "read-failure-token")
    pending, verifier = begin(service, "read-failure-code")
    code = service.approve_authorization(pending.request_id, "browser-read-failure-code", "csrf-read-failure-code", signed_token()).code
    operations = (
        lambda: service.exchange_code("client", code, "https://client.example/callback", service.settings.resource, verifier),
        lambda: service.refresh("client", tokens.refresh_token, service.settings.resource),
        lambda: service.inspect_code("client", code),
        lambda: service.inspect_refresh("client", tokens.refresh_token),
        lambda: service.inspect_access("client", tokens.access_token),
    )
    real_connect = service.store.connect
    for failure in (
        lambda: (_ for _ in ()).throw(sqlite3.OperationalError("controlled connection failure")),
        lambda: _SelectFailureConnection(real_connect()),
    ):
        monkeypatch.setattr(service.store, "connect", failure)
        for operation in operations:
            with pytest.raises(Unavailable):
                operation()
        monkeypatch.setattr(service.store, "connect", real_connect)

    assert service.inspect_code("client", code).active
    assert service.inspect_refresh("client", tokens.refresh_token).active
    assert service.inspect_access("client", tokens.access_token).active
    with sqlite3.connect(service.settings.database_path) as db:
        assert db.execute("SELECT count(*) FROM grants").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM refresh_tokens").fetchone()[0] == 1


def test_uncertain_commit_returns_no_credentials_and_reconstruction_is_authoritative(production_boundary, monkeypatch):
    service, identity, signed_token, _state, _policy, clock = production_boundary
    pending, _ = begin(service, "commit")
    real_connect = service.store.connect
    monkeypatch.setattr(service.store, "connect", lambda: _CommitUncertainConnection(real_connect()))
    with pytest.raises(Unavailable):
        service.approve_authorization(pending.request_id, "browser-commit", "csrf-commit", signed_token())
    monkeypatch.setattr(service.store, "connect", real_connect)
    reopened = OAuthGrantService(service.settings, identity, lambda: clock[0])
    with sqlite3.connect(service.settings.database_path) as db:
        row = db.execute("SELECT status FROM pending WHERE id=?", (pending.request_id,)).fetchone()
        codes = db.execute("SELECT count(*) FROM codes WHERE pending_id=?", (pending.request_id,)).fetchone()[0]
    assert (row[0], codes) == ("approved", 1)
    with pytest.raises(InvalidGrant):
        reopened.approve_authorization(pending.request_id, "browser-commit", "csrf-commit", signed_token())


def test_barrier_ordered_allow_deny_both_directions_and_reopen(production_boundary, monkeypatch):
    service, identity, signed_token, _state, _policy, clock = production_boundary
    pending, _ = begin(service, "deny-first")
    inspected = threading.Barrier(2)
    release = threading.Event()
    original_verify = identity.verify_login

    def paused_verify(token):
        with sqlite3.connect(service.settings.database_path) as db:
            assert db.execute("SELECT status FROM pending WHERE id=?", (pending.request_id,)).fetchone()[0] == "pending"
        inspected.wait(timeout=5)
        assert release.wait(timeout=5)
        return original_verify(token)

    monkeypatch.setattr(identity, "verify_login", paused_verify)
    with ThreadPoolExecutor(max_workers=1) as pool:
        allowing = pool.submit(service.approve_authorization, pending.request_id, "browser-deny-first", "csrf-deny-first", signed_token())
        inspected.wait(timeout=5)
        assert service.deny_authorization(pending.request_id, "browser-deny-first", "csrf-deny-first").denied
        release.set()
        with pytest.raises(InvalidGrant):
            allowing.result(timeout=5)
    monkeypatch.setattr(identity, "verify_login", original_verify)
    reopened = OAuthGrantService(service.settings, identity, lambda: clock[0])
    with pytest.raises(InvalidGrant):
        reopened.approve_authorization(pending.request_id, "browser-deny-first", "csrf-deny-first", signed_token())

    pending, _ = begin(service, "allow-first")
    redirect = service.approve_authorization(pending.request_id, "browser-allow-first", "csrf-allow-first", signed_token())
    assert redirect.code
    with pytest.raises(InvalidGrant):
        service.deny_authorization(pending.request_id, "browser-allow-first", "csrf-allow-first")


def test_barrier_proves_refresh_single_consumption_and_family_replay_revocation(production_boundary, monkeypatch):
    service, identity, _signed_token, _state, _policy, clock = production_boundary
    tokens = issue(production_boundary, "refresh-race")
    inspected = threading.Barrier(3)
    release = threading.Event()
    original_recheck = identity.recheck_identity

    def paused_recheck(principal):
        inspected.wait(timeout=5)
        assert release.wait(timeout=5)
        return original_recheck(principal)

    monkeypatch.setattr(identity, "recheck_identity", paused_recheck)
    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = [pool.submit(service.refresh, "client", tokens.refresh_token, service.settings.resource) for _ in range(2)]
        inspected.wait(timeout=5)
        release.set()
        outcomes = []
        for attempt in attempts:
            try:
                outcomes.append(attempt.result(timeout=5))
            except InvalidGrant:
                outcomes.append(None)
    assert sum(outcome is not None for outcome in outcomes) == 1
    successor = next(outcome for outcome in outcomes if outcome is not None)
    with pytest.raises(InvalidGrant):
        service.authenticate_access(successor.access_token, service.settings.resource, ("auto-coder.read",))
    reopened = OAuthGrantService(service.settings, identity, lambda: clock[0])
    with pytest.raises(InvalidGrant):
        reopened.authenticate_access(successor.access_token, reopened.settings.resource, ("auto-coder.read",))
    with sqlite3.connect(service.settings.database_path) as db:
        assert db.execute("SELECT revoke_reason FROM grants").fetchone()[0] == "refresh_replay"


def test_two_code_exchanges_observe_then_revoke_wins_before_stale_exchange(production_boundary, monkeypatch):
    service, identity, signed_token, _state, _policy, _clock = production_boundary
    pending, verifier = begin(service, "exchange-race")
    redirect = service.approve_authorization(pending.request_id, "browser-exchange-race", "csrf-exchange-race", signed_token())
    both_inspected = threading.Barrier(3)
    first_release, second_release = threading.Event(), threading.Event()
    original_recheck = identity.recheck_identity

    def ordered_recheck(principal):
        both_inspected.wait(timeout=5)
        assert threading.current_thread().oauth_release.wait(timeout=5)
        return original_recheck(principal)

    monkeypatch.setattr(identity, "recheck_identity", ordered_recheck)

    def exchange(release_event):
        threading.current_thread().oauth_release = release_event
        return service.exchange_code("client", redirect.code, "https://client.example/callback", service.settings.resource, verifier)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(exchange, first_release), pool.submit(exchange, second_release)
        both_inspected.wait(timeout=5)
        first_release.set()
        first_tokens = first.result(timeout=5)
        service.revoke("client", first_tokens.access_token)
        second_release.set()
        with pytest.raises(InvalidGrant):
            second.result(timeout=5)
    with pytest.raises(InvalidGrant):
        service.authenticate_access(first_tokens.access_token, service.settings.resource, ("auto-coder.read",))


def test_production_boundary_database_and_logs_never_contain_credentials(production_boundary, tmp_path):
    service, _identity, signed_token, _state, _policy, _clock = production_boundary
    console = io.StringIO()
    log_file = tmp_path / "oauth.log"
    setup_logger(log_level="WARNING", log_file=str(log_file), stream=console)
    raw_identity = signed_token()
    pending, verifier = begin(service, "leak")
    redirect = service.approve_authorization(pending.request_id, "browser-leak", "csrf-leak", raw_identity)
    tokens = service.exchange_code("client", redirect.code, "https://client.example/callback", service.settings.resource, verifier)
    with sqlite3.connect(service.settings.database_path) as db:
        persisted = "\n".join(db.iterdump())
    logs = console.getvalue() + log_file.read_text(encoding="utf-8")
    for secret in (raw_identity, redirect.code, tokens.access_token, tokens.refresh_token, "browser-leak", "csrf-leak"):
        assert secret not in persisted
        assert secret not in logs
