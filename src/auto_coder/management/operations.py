"""Safe, typed values used by the durable management-operation journal."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum

MAX_TEXT = 512
MAX_PARAMETERS = 32


class OperationPhase(str, Enum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    RECONCILING = "reconciling"
    SUCCEEDED = "succeeded"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"
    NEEDS_ATTENTION = "needs_attention"


class EffectState(str, Enum):
    NOT_SENT = "not_sent"
    SENT_UNKNOWN = "sent_unknown"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class AdmissionStatus(str, Enum):
    CREATED = "created"
    REPLAY = "replay"
    CONFLICT = "conflict"
    BUSY = "busy"
    UNAVAILABLE = "unavailable"


class MutationStatus(str, Enum):
    UPDATED = "updated"
    STALE = "stale"
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"


class ReadStatus(str, Enum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"


class QueryStatus(str, Enum):
    OK = "ok"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ManagedTarget:
    instance_id: str
    channel: str
    repository: str

    def key(self) -> str:
        return _canonical((self.instance_id, self.channel, self.repository))


@dataclass(frozen=True)
class ActionParameter:
    name: str
    value: str | int | bool


@dataclass(frozen=True)
class AcceptedAuthority:
    issuer: str
    resource: str
    project_id: str
    uid: str
    oauth_client_id: str
    trusted_grant_id: str
    accepted_scopes: tuple[str, ...]
    authentication_time: float
    accepted_at: float


@dataclass(frozen=True)
class OperationIntent:
    target: ManagedTarget
    action: str
    parameters: tuple[ActionParameter, ...]
    idempotency_key: str
    expected_incarnation: str = ""
    expected_artifact: str = ""

    def normalized_parameters(self) -> tuple[ActionParameter, ...]:
        _bounded(self.action, "action")
        _bounded(self.idempotency_key, "idempotency_key")
        _bounded(self.target.instance_id, "instance_id")
        _bounded(self.target.channel, "channel")
        _bounded(self.target.repository, "repository")
        _bounded(self.expected_incarnation, "expected_incarnation", empty=True)
        _bounded(self.expected_artifact, "expected_artifact", empty=True)
        if len(self.parameters) > MAX_PARAMETERS:
            raise ValueError("too many action parameters")
        names: set[str] = set()
        result = []
        for parameter in self.parameters:
            _bounded(parameter.name, "parameter name")
            if parameter.name.lower() in {
                "authorization_code",
                "bearer_token",
                "command",
                "credential",
                "path",
                "refresh_token",
                "token",
            }:
                raise ValueError("credential, command, and path parameters are not journal-safe")
            if parameter.name in names:
                raise ValueError("duplicate action parameter")
            names.add(parameter.name)
            if isinstance(parameter.value, str):
                _bounded(parameter.value, "parameter value", empty=True)
            elif not isinstance(parameter.value, (int, bool)):
                raise ValueError("parameter values must be bounded scalars")
            result.append(parameter)
        return tuple(sorted(result, key=lambda item: item.name))

    def fingerprint(self) -> str:
        payload = (
            self.target.instance_id,
            self.target.channel,
            self.target.repository,
            self.action,
            tuple((item.name, item.value) for item in self.normalized_parameters()),
            self.expected_incarnation,
            self.expected_artifact,
        )
        return hashlib.sha256(_canonical(payload).encode()).hexdigest()


@dataclass(frozen=True)
class EffectReceipt:
    correlation_identity: str
    observation_source: str
    observed_at: float
    safe_result: str


@dataclass(frozen=True)
class EffectAttempt:
    state: EffectState = EffectState.NOT_SENT
    owner_generation: int = 0
    attempt_id: str = ""
    correlation_identity: str = ""
    receipt: EffectReceipt | None = None


@dataclass(frozen=True)
class OperationRecord:
    operation_id: str
    intent: OperationIntent
    intent_fingerprint: str
    authority: AcceptedAuthority
    phase: OperationPhase
    revision: int
    owner_attempt_id: str = ""
    safe_error_category: str = ""
    effect: EffectAttempt = field(default_factory=EffectAttempt)


@dataclass(frozen=True)
class AdmissionResult:
    status: AdmissionStatus
    record: OperationRecord | None = None
    detail: str = ""


@dataclass(frozen=True)
class MutationResult:
    status: MutationStatus
    record: OperationRecord | None = None
    detail: str = ""


@dataclass(frozen=True)
class OperationLookup:
    status: ReadStatus
    record: OperationRecord | None = None
    detail: str = ""


@dataclass(frozen=True)
class OperationPage:
    records: tuple[OperationRecord, ...]
    next_cursor: int | None
    status: QueryStatus = QueryStatus.OK
    detail: str = ""


@dataclass(frozen=True)
class HistoryEntry:
    sequence: int
    operation_id: str
    phase: OperationPhase
    revision: int
    owner_attempt_id: str
    effect_state: EffectState
    effect_owner_generation: int
    effect_attempt_id: str
    correlation_identity: str
    safe_category: str
    receipt: EffectReceipt | None
    recorded_at: float


@dataclass(frozen=True)
class HistoryPage:
    entries: tuple[HistoryEntry, ...]
    next_cursor: int | None
    status: QueryStatus = QueryStatus.OK
    detail: str = ""


def _bounded(value: str, name: str, *, empty: bool = False) -> None:
    if (not value and not empty) or len(value) > MAX_TEXT or "\x00" in value:
        raise ValueError(f"invalid {name}")


def validate_authority(authority: AcceptedAuthority) -> None:
    for name, value in (
        ("issuer", authority.issuer),
        ("resource", authority.resource),
        ("project_id", authority.project_id),
        ("uid", authority.uid),
        ("oauth_client_id", authority.oauth_client_id),
        ("trusted_grant_id", authority.trusted_grant_id),
    ):
        _bounded(value, name)
    if len(authority.accepted_scopes) > MAX_PARAMETERS:
        raise ValueError("too many accepted scopes")
    for scope in authority.accepted_scopes:
        _bounded(scope, "accepted scope")


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
