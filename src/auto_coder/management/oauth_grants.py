"""Resource-bound OAuth grants for the management MCP resource.

The service deliberately exposes protocol-neutral typed values.  HTTP adapters map
``OAuthError.code`` to OAuth responses without ever passing request dumps here.
"""

import base64
import binascii
import hashlib
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from .identity import AdmissionResult, AdmissionStatus, VerifiedPrincipal
from .oauth_config import SUPPORTED_SCOPE, OAuthSettings
from .oauth_store import OAuthStore


class IdentityAuthority(Protocol):
    def verify_login(self, id_token: str) -> AdmissionResult: ...
    def recheck_identity(self, principal: VerifiedPrincipal) -> AdmissionResult: ...
    def check_operator_policy(self, principal: VerifiedPrincipal) -> AdmissionResult: ...


class OAuthError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class InvalidRequest(OAuthError):
    def __init__(self) -> None:
        super().__init__("invalid_request")


class InvalidClient(OAuthError):
    def __init__(self) -> None:
        super().__init__("invalid_client")


class InvalidTarget(OAuthError):
    def __init__(self) -> None:
        super().__init__("invalid_target")


class InvalidScope(OAuthError):
    def __init__(self) -> None:
        super().__init__("invalid_scope")


class InvalidGrant(OAuthError):
    def __init__(self) -> None:
        super().__init__("invalid_grant")


class Denied(OAuthError):
    def __init__(self) -> None:
        super().__init__("access_denied")


class Unavailable(OAuthError):
    def __init__(self) -> None:
        super().__init__("temporarily_unavailable")


@dataclass(frozen=True)
class AuthorizationRequest:
    client_id: str
    redirect_uri: str
    resource: str
    code_challenge: str
    scopes: Optional[tuple[str, ...]] = None
    state: Optional[str] = None


@dataclass(frozen=True)
class PendingAuthorization:
    request_id: str
    client_id: str
    resource: str
    scopes: tuple[str, ...]
    expires_at: float


@dataclass(frozen=True)
class AuthorizationRedirect:
    redirect_uri: str
    state: Optional[str]
    issuer: str
    code: Optional[str] = None
    denied: bool = False


@dataclass(frozen=True)
class TokenBundle:
    access_token: str
    refresh_token: str
    token_type: str
    expires_in: int
    scope: str


@dataclass(frozen=True)
class AuthenticatedGrant:
    principal: VerifiedPrincipal
    client_id: str
    resource: str
    scopes: tuple[str, ...]
    expires_at: float


@dataclass(frozen=True)
class CredentialInspection:
    grant_id: Optional[str]
    client_id: Optional[str]
    resource: Optional[str]
    active: bool
    spent: bool = False


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _secret() -> str:
    return secrets.token_urlsafe(32)  # 256 random bits before encoding


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def _valid_s256_challenge(challenge: str) -> bool:
    """Return whether *challenge* is the canonical unpadded encoding of 32 bytes."""

    if not isinstance(challenge, str) or len(challenge) != 43:
        return False
    try:
        digest = base64.b64decode(challenge + "=", altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        return False
    return len(digest) == 32 and base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == challenge


class OAuthGrantService:
    def __init__(self, settings: OAuthSettings, identity: IdentityAuthority, clock: Callable[[], float] = time.time) -> None:
        self.settings = settings
        self.identity = identity
        self.clock = clock
        self.store = OAuthStore(settings)

    def _client(self, client_id: str, redirect_uri: Optional[str] = None) -> None:
        client = self.settings.client(client_id)
        if client is None:
            raise InvalidClient()
        if redirect_uri is not None and redirect_uri not in client.redirect_uris:
            raise InvalidRequest()

    def _resource(self, resource: str) -> None:
        if resource != self.settings.resource:
            raise InvalidTarget()

    @staticmethod
    def _scopes(scopes: Optional[tuple[str, ...]], omitted_default: bool) -> tuple[str, ...]:
        if scopes is None and omitted_default:
            return (SUPPORTED_SCOPE,)
        if not scopes or set(scopes) != {SUPPORTED_SCOPE}:
            raise InvalidScope()
        return (SUPPORTED_SCOPE,)

    def _admit(self, result: AdmissionResult) -> VerifiedPrincipal:
        if result.status is AdmissionStatus.UNAVAILABLE:
            raise Unavailable()
        if result.status is not AdmissionStatus.PERMITTED or result.principal is None:
            raise Denied()
        if result.principal.project_id != self.settings.firebase_project_id:
            raise Denied()
        return result.principal

    def begin_authorization(self, request: AuthorizationRequest, browser_binding_secret: str, csrf_secret: str) -> PendingAuthorization:
        self._client(request.client_id, request.redirect_uri)
        self._resource(request.resource)
        scopes = self._scopes(request.scopes, True)
        if not browser_binding_secret or not csrf_secret or not _valid_s256_challenge(request.code_challenge):
            raise InvalidRequest()
        request_id, now = _secret(), self.clock()
        expires = now + 600
        try:
            with self.store.connect() as db:
                db.execute("INSERT INTO pending VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (request_id, request.client_id, request.redirect_uri, request.resource, scopes[0], request.state, request.code_challenge, _hash(browser_binding_secret), _hash(csrf_secret), expires, "pending", None, None, None))
        except sqlite3.Error as exc:
            raise Unavailable() from exc
        return PendingAuthorization(request_id, request.client_id, request.resource, scopes, expires)

    def deny_authorization(self, request_id: str, browser_binding_secret: str, csrf_secret: str) -> AuthorizationRedirect:
        try:
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM pending WHERE id=?", (request_id,)).fetchone()
                if row is None or row["status"] != "pending" or row["expires"] <= self.clock():
                    raise InvalidGrant()
                if not secrets.compare_digest(row["browser_hash"], _hash(browser_binding_secret)) or not secrets.compare_digest(row["csrf_hash"], _hash(csrf_secret)):
                    raise InvalidRequest()
                changed = db.execute("UPDATE pending SET status='denied' WHERE id=? AND status='pending'", (request_id,)).rowcount
                if changed != 1:
                    raise InvalidGrant()
                return AuthorizationRedirect(row["redirect_uri"], row["state"], self.settings.canonical_issuer, denied=True)
        except OAuthError:
            raise
        except sqlite3.Error as exc:
            raise Unavailable() from exc

    def approve_authorization(self, request_id: str, browser_binding_secret: str, csrf_secret: str, firebase_id_token: str) -> AuthorizationRedirect:
        principal = self._admit(self.identity.verify_login(firebase_id_token))
        code, code_hash = _secret(), None
        code_hash = _hash(code)
        try:
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM pending WHERE id=?", (request_id,)).fetchone()
                if row is None or row["status"] != "pending" or row["expires"] <= self.clock():
                    raise InvalidGrant()
                if not secrets.compare_digest(row["browser_hash"], _hash(browser_binding_secret)) or not secrets.compare_digest(row["csrf_hash"], _hash(csrf_secret)):
                    raise InvalidRequest()
                self._admit(self.identity.check_operator_policy(principal))
                now = self.clock()
                if db.execute("UPDATE pending SET status='approved',project=?,uid=?,auth_time=? WHERE id=? AND status='pending'", (principal.project_id, principal.uid, principal.auth_time, request_id)).rowcount != 1:
                    raise InvalidGrant()
                db.execute(
                    "INSERT INTO codes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                    (
                        code_hash,
                        request_id,
                        row["client_id"],
                        row["redirect_uri"],
                        row["resource"],
                        row["scope"],
                        row["challenge"],
                        principal.project_id,
                        principal.uid,
                        principal.auth_time,
                        now + 120,
                        now + self.settings.grant_lifetime,
                    ),
                )
                return AuthorizationRedirect(row["redirect_uri"], row["state"], self.settings.canonical_issuer, code=code)
        except OAuthError:
            raise
        except sqlite3.Error as exc:
            raise Unavailable() from exc

    def exchange_code(self, client_id: str, code: str, redirect_uri: str, resource: str, code_verifier: str) -> TokenBundle:
        self._client(client_id, redirect_uri)
        self._resource(resource)
        try:
            snapshot = self.inspect_code(client_id, code)
            if not snapshot.active:
                raise InvalidGrant()
            with self.store.connect() as db:
                row = db.execute("SELECT * FROM codes WHERE hash=?", (_hash(code),)).fetchone()
        except OAuthError:
            raise
        except sqlite3.Error as exc:
            raise Unavailable() from exc
        assert row is not None
        principal = VerifiedPrincipal(row["project"], row["uid"], row["auth_time"])
        self._admit(self.identity.recheck_identity(principal))
        return self._issue_from_code(client_id, code, redirect_uri, resource, code_verifier, principal)

    def _issue_from_code(self, client_id: str, code: str, redirect_uri: str, resource: str, verifier: str, principal: VerifiedPrincipal) -> TokenBundle:
        access, refresh, grant_id = _secret(), _secret(), str(uuid.uuid4())
        try:
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM codes WHERE hash=?", (_hash(code),)).fetchone()
                now = self.clock()
                if row is None or row["consumed"] is not None or row["expires"] <= now:
                    raise InvalidGrant()
                if (row["client_id"], row["redirect_uri"], row["resource"]) != (client_id, redirect_uri, resource) or not secrets.compare_digest(row["challenge"], _challenge(verifier)):
                    raise InvalidGrant()
                self._admit(self.identity.check_operator_policy(principal))
                expires = row["grant_expires"]
                db.execute("UPDATE codes SET consumed=? WHERE hash=? AND consumed IS NULL", (now, _hash(code)))
                db.execute("INSERT INTO grants VALUES(?,?,?,?,?,?,?,?,?,NULL,NULL)", (grant_id, client_id, resource, row["scope"], principal.project_id, principal.uid, principal.auth_time, now, expires))
                db.execute("INSERT INTO access_tokens VALUES(?,?,?)", (_hash(access), grant_id, min(now + self.settings.access_lifetime, expires)))
                db.execute("INSERT INTO refresh_tokens VALUES(?,?,?,NULL,NULL)", (_hash(refresh), grant_id, expires))
            return TokenBundle(access, refresh, "Bearer", self.settings.access_lifetime, row["scope"])
        except OAuthError:
            raise
        except sqlite3.Error as exc:
            raise Unavailable() from exc

    def refresh(self, client_id: str, refresh_token: str, resource: str, requested_scopes: Optional[tuple[str, ...]] = None) -> TokenBundle:
        self._client(client_id)
        self._resource(resource)
        if requested_scopes is not None:
            self._scopes(requested_scopes, False)
        try:
            with self.store.connect() as db:
                row = db.execute("SELECT r.*,g.client_id,g.resource,g.project,g.uid,g.auth_time,g.scope,g.revoked FROM refresh_tokens r JOIN grants g ON g.id=r.grant_id WHERE r.hash=?", (_hash(refresh_token),)).fetchone()
        except sqlite3.Error as exc:
            raise Unavailable() from exc
        if row is None or row["client_id"] != client_id or row["resource"] != resource:
            raise InvalidGrant()
        if row["spent"] is not None:
            self._revoke_grant(row["grant_id"], "refresh_replay")
            raise InvalidGrant()
        principal = VerifiedPrincipal(row["project"], row["uid"], row["auth_time"])
        self._admit(self.identity.recheck_identity(principal))
        return self._rotate(client_id, refresh_token, resource, principal)

    def _rotate(self, client_id: str, old: str, resource: str, principal: VerifiedPrincipal) -> TokenBundle:
        access, refresh = _secret(), _secret()
        try:
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT r.*,g.client_id,g.resource,g.scope,g.expires AS grant_expires,g.revoked FROM refresh_tokens r JOIN grants g ON g.id=r.grant_id WHERE r.hash=?", (_hash(old),)).fetchone()
                now = self.clock()
                if row is None or row["client_id"] != client_id or row["resource"] != resource or row["revoked"] is not None or row["expires"] <= now:
                    raise InvalidGrant()
                if row["spent"] is not None:
                    db.execute("UPDATE grants SET revoked=?,revoke_reason='refresh_replay' WHERE id=? AND revoked IS NULL", (now, row["grant_id"]))
                    db.commit()
                    raise InvalidGrant()
                self._admit(self.identity.check_operator_policy(principal))
                db.execute("UPDATE refresh_tokens SET spent=?,replaced_by=? WHERE hash=? AND spent IS NULL", (now, _hash(refresh), _hash(old)))
                db.execute("INSERT INTO refresh_tokens VALUES(?,?,?,NULL,NULL)", (_hash(refresh), row["grant_id"], row["grant_expires"]))
                life = min(now + self.settings.access_lifetime, row["grant_expires"])
                db.execute("INSERT INTO access_tokens VALUES(?,?,?)", (_hash(access), row["grant_id"], life))
            return TokenBundle(access, refresh, "Bearer", max(0, int(life - now)), row["scope"])
        except OAuthError:
            raise
        except sqlite3.Error as exc:
            raise Unavailable() from exc

    def authenticate_access(self, token: str, resource: str, required_scopes: tuple[str, ...]) -> AuthenticatedGrant:
        self._resource(resource)
        try:
            with self.store.connect() as db:
                row = db.execute("SELECT a.expires AS access_expires,g.* FROM access_tokens a JOIN grants g ON g.id=a.grant_id WHERE a.hash=?", (_hash(token),)).fetchone()
            now = self.clock()
            if row is None or row["revoked"] is not None or row["access_expires"] <= now or row["expires"] <= now or row["resource"] != resource or row["project"] != self.settings.firebase_project_id:
                raise InvalidGrant()
            self._client(row["client_id"])
            granted = tuple(row["scope"].split())
            if not set(required_scopes).issubset(granted):
                raise InvalidScope()
            principal = VerifiedPrincipal(row["project"], row["uid"], row["auth_time"])
            self._admit(self.identity.check_operator_policy(principal))
            return AuthenticatedGrant(principal, row["client_id"], resource, granted, row["access_expires"])
        except OAuthError:
            raise
        except sqlite3.Error as exc:
            raise Unavailable() from exc

    def revoke(self, client_id: str, token: str) -> None:
        self._client(client_id)
        try:
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT g.id,g.client_id FROM grants g JOIN access_tokens a ON a.grant_id=g.id WHERE a.hash=? UNION SELECT g.id,g.client_id FROM grants g JOIN refresh_tokens r ON r.grant_id=g.id WHERE r.hash=?", (_hash(token), _hash(token))).fetchone()
                if row is not None and row["client_id"] == client_id:
                    db.execute("UPDATE grants SET revoked=?,revoke_reason='client_revocation' WHERE id=? AND revoked IS NULL", (self.clock(), row["id"]))
        except sqlite3.Error as exc:
            raise Unavailable() from exc

    def _revoke_grant(self, grant_id: str, reason: str) -> None:
        try:
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("UPDATE grants SET revoked=?,revoke_reason=? WHERE id=? AND revoked IS NULL", (self.clock(), reason, grant_id))
        except sqlite3.Error as exc:
            raise Unavailable() from exc

    def inspect_code(self, client_id: str, code: str) -> CredentialInspection:
        self._client(client_id)
        try:
            with self.store.connect() as db:
                row = db.execute("SELECT * FROM codes WHERE hash=?", (_hash(code),)).fetchone()
        except sqlite3.Error as exc:
            raise Unavailable() from exc
        if row is None or row["client_id"] != client_id:
            return CredentialInspection(None, None, None, False)
        return CredentialInspection(None, client_id, row["resource"], row["consumed"] is None and row["expires"] > self.clock(), row["consumed"] is not None)

    def inspect_refresh(self, client_id: str, token: str) -> CredentialInspection:
        return self._inspect_token("refresh_tokens", client_id, token)

    def inspect_access(self, client_id: str, token: str) -> CredentialInspection:
        return self._inspect_token("access_tokens", client_id, token)

    def _inspect_token(self, table: str, client_id: str, token: str) -> CredentialInspection:
        self._client(client_id)
        try:
            with self.store.connect() as db:
                row = db.execute(f"SELECT t.*,g.client_id,g.resource,g.revoked FROM {table} t JOIN grants g ON g.id=t.grant_id WHERE t.hash=?", (_hash(token),)).fetchone()
        except sqlite3.Error as exc:
            raise Unavailable() from exc
        if row is None or row["client_id"] != client_id:
            return CredentialInspection(None, None, None, False)
        spent = bool(row["spent"] is not None) if table == "refresh_tokens" else False
        return CredentialInspection(row["grant_id"], client_id, row["resource"], row["revoked"] is None and row["expires"] > self.clock() and not spent, spent)
