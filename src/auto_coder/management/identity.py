"""Firebase identity verification and explicit local operator admission."""

import math
import os
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

import jwt
from firebase_admin import App, auth, credentials, initialize_app

from auto_coder.logger_config import get_logger

from .operator_policy import PolicyUnavailable, read_operator_policy

logger = get_logger(__name__)


class AdmissionStatus(str, Enum):
    PERMITTED = "permitted"
    DENIED = "denied"
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"


class ReasonCode(str, Enum):
    PERMITTED = "permitted"
    INVALID_TOKEN = "invalid_token"
    WRONG_PROJECT = "wrong_project"
    NON_GOOGLE_IDENTITY = "non_google_identity"
    OPERATOR_NOT_ALLOWED = "operator_not_allowed"
    ACCOUNT_DISABLED = "account_disabled"
    ACCOUNT_DELETED = "account_deleted"
    SESSION_REVOKED = "session_revoked"
    POLICY_UNAVAILABLE = "policy_unavailable"
    IDENTITY_SERVICE_UNAVAILABLE = "identity_service_unavailable"
    INVALID_CONFIGURATION = "invalid_configuration"


@dataclass(frozen=True)
class IdentityConfig:
    project_id: str
    allowed_uid_file: Path

    def __post_init__(self) -> None:
        if not self.project_id:
            raise ValueError("project_id must not be empty")


@dataclass(frozen=True)
class VerifiedPrincipal:
    project_id: str
    uid: str
    auth_time: int | float


@dataclass(frozen=True)
class AdmissionResult:
    status: AdmissionStatus
    reason: ReasonCode
    principal: Optional[VerifiedPrincipal] = None

    def __post_init__(self) -> None:
        if (self.status is AdmissionStatus.PERMITTED) != (self.principal is not None):
            raise ValueError("a principal is present only for PERMITTED results")


class FirebaseOperatorIdentity:
    """Production adapter combining Firebase evidence with a fresh UID policy."""

    def __init__(self, config: IdentityConfig, clock: Callable[[], float] = time.time) -> None:
        self._config = config
        self._clock = clock
        self._app: Optional[App] = None

    def verify_login(self, id_token: str) -> AdmissionResult:
        if self._emulator_configured():
            return self._failure(AdmissionStatus.UNAVAILABLE, ReasonCode.INVALID_CONFIGURATION)
        if not isinstance(id_token, str) or not id_token:
            return self._failure(AdmissionStatus.INVALID, ReasonCode.INVALID_TOKEN)
        try:
            claims = auth.verify_id_token(id_token, app=self._firebase_app(), check_revoked=True, clock_skew_seconds=0)
        except auth.RevokedIdTokenError:
            return self._failure(AdmissionStatus.DENIED, ReasonCode.SESSION_REVOKED)
        except auth.UserDisabledError:
            return self._failure(AdmissionStatus.DENIED, ReasonCode.ACCOUNT_DISABLED)
        except auth.UserNotFoundError:
            return self._failure(AdmissionStatus.DENIED, ReasonCode.ACCOUNT_DELETED)
        except (auth.InvalidIdTokenError, auth.ExpiredIdTokenError):
            return self._failure(AdmissionStatus.INVALID, ReasonCode.INVALID_TOKEN)
        except TypeError:
            # google-auth performs arithmetic on iat/exp before Firebase can
            # normalize their types. Inspecting the untrusted payload here is
            # used only to classify a rejection; it never establishes identity.
            if self._has_unusable_time_claims(id_token):
                return self._failure(AdmissionStatus.INVALID, ReasonCode.INVALID_TOKEN)
            return self._failure(AdmissionStatus.UNAVAILABLE, ReasonCode.IDENTITY_SERVICE_UNAVAILABLE)
        except ValueError:
            return self._failure(AdmissionStatus.UNAVAILABLE, ReasonCode.IDENTITY_SERVICE_UNAVAILABLE)
        except Exception:
            return self._failure(AdmissionStatus.UNAVAILABLE, ReasonCode.IDENTITY_SERVICE_UNAVAILABLE)

        now = self._clock()
        iat, expiry, auth_time = claims.get("iat"), claims.get("exp"), claims.get("auth_time")
        if not all(self._finite_number(item) for item in (iat, expiry, auth_time)) or not (iat < now and auth_time < now and expiry > now):
            return self._failure(AdmissionStatus.INVALID, ReasonCode.INVALID_TOKEN)
        firebase = claims.get("firebase")
        if not isinstance(firebase, dict) or "tenant" in firebase:
            return self._failure(AdmissionStatus.INVALID, ReasonCode.INVALID_TOKEN)
        uid = claims.get("uid", claims.get("sub"))
        if not isinstance(uid, str) or not uid:
            return self._failure(AdmissionStatus.INVALID, ReasonCode.INVALID_TOKEN)
        if firebase.get("sign_in_provider") != "google.com":
            return self._failure(AdmissionStatus.DENIED, ReasonCode.NON_GOOGLE_IDENTITY)
        principal = VerifiedPrincipal(self._config.project_id, uid, auth_time)
        return self._check_account_and_policy(principal)

    def recheck_identity(self, principal: VerifiedPrincipal) -> AdmissionResult:
        if not self._valid_principal(principal):
            return self._failure(AdmissionStatus.INVALID, ReasonCode.WRONG_PROJECT)
        if self._emulator_configured():
            return self._failure(AdmissionStatus.UNAVAILABLE, ReasonCode.INVALID_CONFIGURATION)
        return self._check_account_and_policy(principal)

    def check_operator_policy(self, principal: VerifiedPrincipal) -> AdmissionResult:
        if not self._valid_principal(principal):
            return self._failure(AdmissionStatus.INVALID, ReasonCode.WRONG_PROJECT)
        return self._check_policy(principal)

    def _firebase_app(self) -> App:
        if self._emulator_configured():
            raise RuntimeError("Firebase Auth emulator configuration is forbidden")
        if self._app is None:
            credential = credentials.ApplicationDefault()
            self._app = initialize_app(credential, {"projectId": self._config.project_id}, name=f"auto-coder-management-{uuid.uuid4()}")
        return self._app

    def _check_account_and_policy(self, principal: VerifiedPrincipal) -> AdmissionResult:
        try:
            user = auth.get_user(principal.uid, app=self._firebase_app())
        except auth.UserNotFoundError:
            return self._failure(AdmissionStatus.DENIED, ReasonCode.ACCOUNT_DELETED)
        except Exception:
            return self._failure(AdmissionStatus.UNAVAILABLE, ReasonCode.IDENTITY_SERVICE_UNAVAILABLE)
        try:
            disabled = user.disabled
            valid_after = user.tokens_valid_after_timestamp
        except (AttributeError, TypeError, ValueError):
            return self._failure(AdmissionStatus.UNAVAILABLE, ReasonCode.IDENTITY_SERVICE_UNAVAILABLE)
        if disabled:
            return self._failure(AdmissionStatus.DENIED, ReasonCode.ACCOUNT_DISABLED)
        if not self._finite_number(valid_after):
            return self._failure(AdmissionStatus.UNAVAILABLE, ReasonCode.IDENTITY_SERVICE_UNAVAILABLE)
        if principal.auth_time * 1000 < valid_after:
            return self._failure(AdmissionStatus.DENIED, ReasonCode.SESSION_REVOKED)
        return self._check_policy(principal)

    def _check_policy(self, principal: VerifiedPrincipal) -> AdmissionResult:
        try:
            policy = read_operator_policy(self._config.allowed_uid_file, self._config.project_id)
        except PolicyUnavailable:
            return self._failure(AdmissionStatus.UNAVAILABLE, ReasonCode.POLICY_UNAVAILABLE)
        if principal.uid not in policy.allowed_uids:
            return self._failure(AdmissionStatus.DENIED, ReasonCode.OPERATOR_NOT_ALLOWED)
        return AdmissionResult(AdmissionStatus.PERMITTED, ReasonCode.PERMITTED, principal)

    def _failure(self, status: AdmissionStatus, reason: ReasonCode) -> AdmissionResult:
        logger.warning("Management identity decision: status={} reason={}", status.value, reason.value)
        return AdmissionResult(status, reason)

    def _valid_principal(self, principal: object) -> bool:
        return isinstance(principal, VerifiedPrincipal) and principal.project_id == self._config.project_id and bool(principal.uid) and self._finite_number(principal.auth_time)

    @staticmethod
    def _finite_number(value: object) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    @staticmethod
    def _emulator_configured() -> bool:
        return bool(os.environ.get("FIREBASE_AUTH_EMULATOR_HOST"))

    @classmethod
    def _has_unusable_time_claims(cls, id_token: str) -> bool:
        try:
            untrusted_claims = jwt.decode(id_token, options={"verify_signature": False})
        except (jwt.DecodeError, TypeError, ValueError):
            return False
        return not all(cls._finite_number(untrusted_claims.get(claim)) for claim in ("iat", "exp", "auth_time"))
