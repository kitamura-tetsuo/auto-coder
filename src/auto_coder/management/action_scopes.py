"""Canonical management action scopes and trusted delayed-work authority."""

from dataclasses import dataclass
from enum import Enum

READ_SCOPE = "auto-coder.read"
OPERATE_SCOPE = "auto-coder.operate"
DEPLOY_SCOPE = "auto-coder.deploy"
KNOWN_SCOPES = frozenset({READ_SCOPE, OPERATE_SCOPE, DEPLOY_SCOPE})


def validate_scope_set(scopes: tuple[str, ...]) -> tuple[str, ...]:
    """Return a canonical non-empty scope set which always includes read."""

    if not scopes or len(scopes) != len(set(scopes)) or not set(scopes) <= KNOWN_SCOPES or READ_SCOPE not in scopes:
        raise ValueError("invalid management scope set")
    return tuple(scope for scope in (READ_SCOPE, OPERATE_SCOPE, DEPLOY_SCOPE) if scope in scopes)


@dataclass(frozen=True)
class AcceptedAuthority:
    grant_id: str
    project_id: str
    uid: str
    auth_time: float
    client_id: str
    issuer: str
    resource: str
    accepted_scopes: tuple[str, ...]
    accepted_at: float


class PendingAuthorityStatus(Enum):
    PERMITTED = "permitted"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class PendingAuthorityDecision:
    status: PendingAuthorityStatus
    observed_at: float
    reason: str
