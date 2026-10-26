import base64
import hashlib

import pytest

from auto_coder.management.action_scopes import DEPLOY_SCOPE, OPERATE_SCOPE, READ_SCOPE, PendingAuthorityStatus
from auto_coder.management.identity import AdmissionResult, AdmissionStatus, ReasonCode, VerifiedPrincipal
from auto_coder.management.oauth_config import OAuthClient, OAuthSettings
from auto_coder.management.oauth_grants import AuthorizationRequest, InvalidScope, OAuthGrantService


class Identity:
    def __init__(self):
        self.allowed = True
        self.principal = VerifiedPrincipal("project", "operator", 10)

    def _result(self):
        if self.allowed:
            return AdmissionResult(AdmissionStatus.PERMITTED, ReasonCode.PERMITTED, self.principal)
        return AdmissionResult(AdmissionStatus.DENIED, ReasonCode.OPERATOR_NOT_ALLOWED)

    verify_login = lambda self, token: self._result()
    recheck_identity = lambda self, principal: self._result()
    check_operator_policy = lambda self, principal: self._result()


def _challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def _service(tmp_path, scopes=(READ_SCOPE, OPERATE_SCOPE, DEPLOY_SCOPE)):
    clock = [1000.0]
    identity = Identity()
    client = OAuthClient("client", ("https://client.example/cb",), allowed_scopes=scopes)
    settings = OAuthSettings(tmp_path / "oauth.db", "https://management.example", "project", (client,), enabled_scopes=scopes)
    return OAuthGrantService(settings, identity, lambda: clock[0]), identity, clock


def _issue(service, scopes):
    verifier = "v" * 64
    pending = service.begin_authorization(
        AuthorizationRequest("client", "https://client.example/cb", service.settings.resource, _challenge(verifier), scopes),
        "browser",
        "csrf",
    )
    approved = service.approve_authorization(pending.request_id, "browser", "csrf", "identity")
    return service.exchange_code("client", approved.code, "https://client.example/cb", service.settings.resource, verifier)


def test_exact_scope_validation_and_refresh_narrowing(tmp_path):
    service, _, _ = _service(tmp_path)
    with pytest.raises(InvalidScope):
        _issue(service, (DEPLOY_SCOPE,))
    with pytest.raises(InvalidScope):
        _issue(service, (READ_SCOPE, "unknown"))

    issued = _issue(service, (READ_SCOPE, OPERATE_SCOPE))
    narrowed = service.refresh("client", issued.refresh_token, service.settings.resource, (READ_SCOPE,))
    assert narrowed.scope == READ_SCOPE
    with pytest.raises(InvalidScope):
        service.authenticate_access(narrowed.access_token, service.settings.resource, (READ_SCOPE, OPERATE_SCOPE))


def test_reconstruction_does_not_upgrade_grant_and_current_config_restricts_admission(tmp_path):
    read_service, identity, _ = _service(tmp_path, (READ_SCOPE,))
    token = _issue(read_service, (READ_SCOPE,))
    expanded_client = OAuthClient("client", ("https://client.example/cb",), allowed_scopes=(READ_SCOPE, DEPLOY_SCOPE))
    expanded = OAuthSettings(read_service.settings.database_path, read_service.settings.issuer, "project", (expanded_client,), enabled_scopes=(READ_SCOPE, DEPLOY_SCOPE))
    reconstructed = OAuthGrantService(expanded, identity, read_service.clock)
    with pytest.raises(InvalidScope):
        reconstructed.authenticate_access(token.access_token, expanded.resource, (READ_SCOPE, DEPLOY_SCOPE))
    assert reconstructed.authenticate_access(token.access_token, expanded.resource, (READ_SCOPE,)).scopes == (READ_SCOPE,)


def test_pending_authority_uses_token_scope_and_fresh_grant_and_policy(tmp_path):
    service, identity, clock = _service(tmp_path)
    tokens = _issue(service, (READ_SCOPE, DEPLOY_SCOPE))
    grant = service.authenticate_access(tokens.access_token, service.settings.resource, (READ_SCOPE, DEPLOY_SCOPE))
    accepted = service.accepted_authority(grant)
    clock[0] = grant.expires_at + 1
    assert service.check_pending_authority(accepted, (READ_SCOPE, DEPLOY_SCOPE)).status is PendingAuthorityStatus.PERMITTED
    identity.allowed = False
    assert service.check_pending_authority(accepted, (READ_SCOPE, DEPLOY_SCOPE)).status is PendingAuthorityStatus.DENIED
    identity.allowed = True
    service.revoke("client", tokens.refresh_token)
    assert service.check_pending_authority(accepted, (READ_SCOPE, DEPLOY_SCOPE)).status is PendingAuthorityStatus.DENIED
