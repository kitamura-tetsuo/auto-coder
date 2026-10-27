"""Advisory, fail-closed authorization producer for manager self-update.

This is not an MCP tool or an effect dispatcher. Only a trusted consumer may
persist values returned from a real bearer admission into the operation journal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path

import yaml

from ..operating_policy import DecisionOutcome, OperatingPolicy, PolicyProfile, load_operating_policy
from .action_scopes import (
    DEPLOY_SCOPE,
    READ_SCOPE,
    AcceptedAuthority,
    PendingAuthorityStatus,
)
from .oauth_config import ManagementSettings, load_management_settings
from .oauth_grants import OAuthError, OAuthGrantService, Unavailable
from .operation_store import ManagementOperationStore
from .operations import AcceptedAuthority as JournalAuthority
from .operations import ActionParameter, EffectState, ManagedTarget, OperationIntent, OperationRecord, ReadStatus

MANAGEMENT_REPOSITORY = "kitamura-tetsuo/auto-coder"
MANAGEMENT_CHANNEL = "management"
_REQUIRED_SCOPES = (READ_SCOPE, DEPLOY_SCOPE)
_POLICY_REVISION_PARAMETER = "accepted_policy_revision"
_VALID_REVISION = re.compile(r"^sha256:[a-f0-9]{64}$")


class ManagementUpdateAction(str, Enum):
    SELF_UPDATE = "management-service-self-update"


class ManagementUpdateReason(str, Enum):
    ALLOWED = "manager_update_allowed"
    POLICY_UNAVAILABLE = "operator_policy_unavailable"
    CONFIG_UNAVAILABLE = "management_config_unavailable"
    AUTHORITY_SERVICE_MISMATCH = "authority_service_mismatch"
    INSTANCE_UNCONFIGURED = "management_instance_unconfigured"
    TARGET_MISMATCH = "management_target_mismatch"
    REPOSITORY_MISMATCH = "management_repository_mismatch"
    ACTION_UNSUPPORTED = "management_action_unsupported"
    PROFILE_NOT_FULL = "full_profile_required"
    OPT_IN_DISABLED = "management_self_update_disabled"
    POLICY_REVISION_CHANGED = "operator_policy_revision_changed"
    GRANT_DENIED = "original_grant_denied"
    GRANT_UNAVAILABLE = "original_grant_unavailable"
    ORIGINAL_CONTEXT_INVALID = "original_operation_context_invalid"
    JOURNAL_UNAVAILABLE = "operation_journal_unavailable"
    OPERATION_UNKNOWN = "operation_unknown"
    EFFECT_ALREADY_SENT = "forward_effect_already_sent"
    EFFECT_STATE_INVALID = "operation_effect_state_invalid"


@dataclass(frozen=True)
class ManagementUpdateTarget:
    instance_id: str
    repository: str = MANAGEMENT_REPOSITORY


@dataclass(frozen=True)
class ManagementUpdateIntent:
    instance_id: str
    repository: str
    action: ManagementUpdateAction | str = ManagementUpdateAction.SELF_UPDATE


@dataclass(frozen=True)
class AcceptedManagementUpdate:
    """Original immutable consent and target, for trusted durable admission."""

    target: ManagementUpdateTarget
    action: ManagementUpdateAction
    policy_revision: str
    authority: AcceptedAuthority


@dataclass(frozen=True)
class ExactEffectObservation:
    """Read-only receipt lookup key, never permission to send or recover an effect."""

    operation_id: str
    attempt_id: str
    correlation_identity: str
    expected_artifact: str


@dataclass(frozen=True)
class ManagementUpdateDecision:
    outcome: DecisionOutcome
    reason_code: ManagementUpdateReason
    policy_revision: str
    target: ManagementUpdateTarget | None
    action: ManagementUpdateAction = ManagementUpdateAction.SELF_UPDATE
    accepted: AcceptedManagementUpdate | None = None
    observation: ExactEffectObservation | None = None


def _decision(
    outcome: DecisionOutcome,
    reason: ManagementUpdateReason,
    revision: str,
    target: ManagementUpdateTarget | None,
) -> ManagementUpdateDecision:
    return ManagementUpdateDecision(outcome, reason, revision, target)


def _load_current(
    policy_path: Path,
    management_config_path: Path,
    grants: OAuthGrantService,
    intent: ManagementUpdateIntent,
    accepted_revision: str | None = None,
) -> ManagementUpdateDecision:
    """Read both operator-owned files freshly; do not reuse a cached ALLOWED."""

    loaded = load_operating_policy(policy_path)
    if loaded.policy is None:
        return _decision(DecisionOutcome.UNAVAILABLE, ManagementUpdateReason.POLICY_UNAVAILABLE, loaded.revision, None)
    policy: OperatingPolicy = loaded.policy
    try:
        settings: ManagementSettings = load_management_settings(management_config_path)
    except (OSError, UnicodeError, yaml.YAMLError, KeyError, TypeError, ValueError):
        return _decision(DecisionOutcome.UNAVAILABLE, ManagementUpdateReason.CONFIG_UNAVAILABLE, policy.revision, None)
    if settings.oauth != grants.settings:
        return _decision(DecisionOutcome.UNAVAILABLE, ManagementUpdateReason.AUTHORITY_SERVICE_MISMATCH, policy.revision, None)
    if settings.management_instance_id is None:
        return _decision(DecisionOutcome.DENIED, ManagementUpdateReason.INSTANCE_UNCONFIGURED, policy.revision, None)

    target = ManagementUpdateTarget(settings.management_instance_id)
    if intent.action != ManagementUpdateAction.SELF_UPDATE:
        return _decision(DecisionOutcome.DENIED, ManagementUpdateReason.ACTION_UNSUPPORTED, policy.revision, target)
    if intent.instance_id != target.instance_id:
        return _decision(DecisionOutcome.DENIED, ManagementUpdateReason.TARGET_MISMATCH, policy.revision, target)
    if intent.repository != MANAGEMENT_REPOSITORY:
        return _decision(DecisionOutcome.DENIED, ManagementUpdateReason.REPOSITORY_MISMATCH, policy.revision, target)
    configured = policy.management_self_update
    if configured is not None and (configured.instance_id != target.instance_id or configured.repository != MANAGEMENT_REPOSITORY):
        return _decision(DecisionOutcome.DENIED, ManagementUpdateReason.TARGET_MISMATCH, policy.revision, target)
    if policy.profile is not PolicyProfile.FULL:
        return _decision(DecisionOutcome.DENIED, ManagementUpdateReason.PROFILE_NOT_FULL, policy.revision, target)
    if configured is None or not configured.enabled:
        return _decision(DecisionOutcome.DENIED, ManagementUpdateReason.OPT_IN_DISABLED, policy.revision, target)
    if accepted_revision is not None and accepted_revision != policy.revision:
        return _decision(DecisionOutcome.DENIED, ManagementUpdateReason.POLICY_REVISION_CHANGED, policy.revision, target)
    return _decision(DecisionOutcome.ALLOWED, ManagementUpdateReason.ALLOWED, policy.revision, target)


def _pending_decision(grants: OAuthGrantService, original: AcceptedAuthority, current: ManagementUpdateDecision) -> ManagementUpdateDecision:
    if original.issuer != grants.settings.canonical_issuer or original.resource != grants.settings.resource:
        return replace(current, outcome=DecisionOutcome.DENIED, reason_code=ManagementUpdateReason.ORIGINAL_CONTEXT_INVALID)
    status = grants.check_pending_authority(original, _REQUIRED_SCOPES).status
    if status is PendingAuthorityStatus.PERMITTED:
        return current
    if status is PendingAuthorityStatus.UNAVAILABLE:
        return replace(current, outcome=DecisionOutcome.UNAVAILABLE, reason_code=ManagementUpdateReason.GRANT_UNAVAILABLE)
    return replace(current, outcome=DecisionOutcome.DENIED, reason_code=ManagementUpdateReason.GRANT_DENIED)


def authorize_management_update_request(
    policy_path: Path,
    management_config_path: Path,
    grants: OAuthGrantService,
    bearer_token: str,
    intent: ManagementUpdateIntent,
) -> ManagementUpdateDecision:
    """Admit only an actual original OAuth bearer with read AND deploy consent."""

    current = _load_current(policy_path, management_config_path, grants, intent)
    if current.outcome is not DecisionOutcome.ALLOWED:
        return current
    try:
        grant = grants.authenticate_access(bearer_token, grants.settings.resource, _REQUIRED_SCOPES)
    except Unavailable:
        return replace(current, outcome=DecisionOutcome.UNAVAILABLE, reason_code=ManagementUpdateReason.GRANT_UNAVAILABLE)
    except OAuthError:
        return replace(current, outcome=DecisionOutcome.DENIED, reason_code=ManagementUpdateReason.GRANT_DENIED)
    original = grants.accepted_authority(grant)
    rechecked = _pending_decision(grants, original, current)
    if rechecked.outcome is not DecisionOutcome.ALLOWED:
        return rechecked
    assert current.target is not None
    accepted = AcceptedManagementUpdate(current.target, ManagementUpdateAction.SELF_UPDATE, current.policy_revision, original)
    return replace(current, accepted=accepted)


def journal_values_for_management_update(
    accepted: AcceptedManagementUpdate,
    idempotency_key: str,
    expected_artifact: str,
) -> tuple[OperationIntent, JournalAuthority]:
    """Encode an accepted context in the existing durable journal without effects."""

    if accepted.action is not ManagementUpdateAction.SELF_UPDATE or accepted.target.repository != MANAGEMENT_REPOSITORY or not _VALID_REVISION.fullmatch(accepted.policy_revision):
        raise ValueError("invalid accepted management update")
    intent = OperationIntent(
        ManagedTarget(accepted.target.instance_id, MANAGEMENT_CHANNEL, MANAGEMENT_REPOSITORY),
        accepted.action.value,
        (ActionParameter(_POLICY_REVISION_PARAMETER, accepted.policy_revision),),
        idempotency_key,
        expected_artifact=expected_artifact,
    )
    original = accepted.authority
    authority = JournalAuthority(
        original.issuer,
        original.resource,
        original.project_id,
        original.uid,
        original.client_id,
        original.grant_id,
        original.accepted_scopes,
        original.auth_time,
        original.accepted_at,
    )
    return intent, authority


def _from_record(record: OperationRecord) -> AcceptedManagementUpdate | None:
    intent = record.intent
    if (
        intent.target.channel != MANAGEMENT_CHANNEL
        or intent.target.repository != MANAGEMENT_REPOSITORY
        or intent.action != ManagementUpdateAction.SELF_UPDATE.value
        or len(intent.parameters) != 1
        or intent.parameters[0].name != _POLICY_REVISION_PARAMETER
        or not isinstance(intent.parameters[0].value, str)
        or not _VALID_REVISION.fullmatch(intent.parameters[0].value)
    ):
        return None
    authority = record.authority
    original = AcceptedAuthority(
        authority.trusted_grant_id,
        authority.project_id,
        authority.uid,
        authority.authentication_time,
        authority.oauth_client_id,
        authority.issuer,
        authority.resource,
        authority.accepted_scopes,
        authority.accepted_at,
    )
    return AcceptedManagementUpdate(ManagementUpdateTarget(intent.target.instance_id), ManagementUpdateAction.SELF_UPDATE, intent.parameters[0].value, original)


def recheck_management_update_operation(
    policy_path: Path,
    management_config_path: Path,
    grants: OAuthGrantService,
    journal: ManagementOperationStore,
    operation_id: str,
) -> ManagementUpdateDecision:
    """Revalidate original consent and current policy for a *journaled* effect.

    A SENT_UNKNOWN record is conservatively possibly sent, even if the send was
    interrupted. Its exact receipt may be observed; a new send is never allowed.
    """

    lookup = journal.get(operation_id)
    if lookup.status is ReadStatus.UNAVAILABLE:
        return _decision(DecisionOutcome.UNAVAILABLE, ManagementUpdateReason.JOURNAL_UNAVAILABLE, "unavailable", None)
    if lookup.status is not ReadStatus.FOUND or lookup.record is None:
        return _decision(DecisionOutcome.DENIED, ManagementUpdateReason.OPERATION_UNKNOWN, "unavailable", None)
    record = lookup.record
    accepted = _from_record(record)
    if accepted is None:
        return _decision(DecisionOutcome.DENIED, ManagementUpdateReason.ORIGINAL_CONTEXT_INVALID, "unavailable", None)
    intent = ManagementUpdateIntent(accepted.target.instance_id, accepted.target.repository, accepted.action)
    current = _load_current(policy_path, management_config_path, grants, intent, accepted.policy_revision)
    if current.outcome is DecisionOutcome.ALLOWED:
        current = _pending_decision(grants, accepted.authority, current)

    if record.effect.state is EffectState.NOT_SENT:
        return current
    if record.effect.state not in {EffectState.SENT_UNKNOWN, EffectState.CONFIRMED, EffectState.REJECTED} or not record.effect.attempt_id or not record.effect.correlation_identity:
        return replace(current, outcome=DecisionOutcome.DENIED, reason_code=ManagementUpdateReason.EFFECT_STATE_INVALID, accepted=None)
    observation = ExactEffectObservation(record.operation_id, record.effect.attempt_id, record.effect.correlation_identity, record.intent.expected_artifact)
    if current.outcome is DecisionOutcome.ALLOWED:
        current = replace(current, outcome=DecisionOutcome.DENIED, reason_code=ManagementUpdateReason.EFFECT_ALREADY_SENT)
    return replace(current, accepted=None, observation=observation)
