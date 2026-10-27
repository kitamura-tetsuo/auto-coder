"""Advisory, fail-closed authorization for an exact management self-update.

This producer has no update, restart, Docker, or policy-writing capability.
Only a trusted dispatcher may turn its decisions into durable admission and,
after separate correctness checks, an effect. No MCP tool is registered here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path

from auto_coder.operating_policy import MANAGEMENT_REPOSITORY, DecisionOutcome, PolicyProfile, load_operating_policy

from .action_scopes import DEPLOY_SCOPE, READ_SCOPE, AcceptedAuthority, PendingAuthorityStatus
from .bootstrap_supervisor import BootstrapConfig
from .oauth_grants import Denied, InvalidScope, OAuthError, OAuthGrantService, Unavailable
from .operation_store import ManagementOperationStore
from .operations import AcceptedAuthority as JournalAuthority
from .operations import ActionParameter, EffectState, ManagedTarget, OperationIntent, OperationPhase, OperationRecord, ReadStatus

REQUIRED_SCOPES = (READ_SCOPE, DEPLOY_SCOPE)
MANAGEMENT_JOURNAL_CHANNEL = "management"
_SHA_PATTERN = re.compile(r"^[a-f0-9]{40}(?:[a-f0-9]{24})?$")
_REVISION_PATTERN = re.compile(r"^sha256:[a-f0-9]{64}$")


class ManagementUpdateAction(str, Enum):
    SELF_UPDATE = "management-service-self-update"


class ManagementUpdateOutcome(str, Enum):
    MANAGER_UPDATE_ALLOWED = "MANAGER_UPDATE_ALLOWED"
    DENIED = "DENIED"
    UNAVAILABLE = "UNAVAILABLE"


class ManagementUpdateReason(str, Enum):
    ALLOWED = "manager_update_allowed"
    POLICY_UNAVAILABLE = "policy_unavailable"
    BOOTSTRAP_UNAVAILABLE = "bootstrap_unavailable"
    PROFILE_NOT_FULL = "profile_not_full"
    SELF_UPDATE_DISABLED = "self_update_disabled"
    INSTANCE_MISMATCH = "instance_mismatch"
    REPOSITORY_MISMATCH = "repository_mismatch"
    ACTION_UNKNOWN = "action_unknown"
    POLICY_REVISION_CHANGED = "policy_revision_changed"
    CONSENT_DENIED = "consent_denied"
    AUTHORITY_DENIED = "authority_denied"
    AUTHORITY_UNAVAILABLE = "authority_unavailable"
    OPERATION_UNKNOWN = "operation_unknown"
    OPERATION_MISMATCH = "operation_mismatch"
    OPERATION_CHANGED = "operation_changed"
    JOURNAL_UNAVAILABLE = "journal_unavailable"
    EFFECT_ALREADY_SENT = "effect_already_sent"


class ReconciliationAccess(str, Enum):
    NONE = "none"
    EXACT_RECEIPT_OBSERVATION_ONLY = "exact_receipt_observation_only"


@dataclass(frozen=True)
class ManagementUpdateIntent:
    instance_id: str
    repository: str
    action: ManagementUpdateAction | str = ManagementUpdateAction.SELF_UPDATE


@dataclass(frozen=True)
class ManagementUpdateDecision:
    outcome: ManagementUpdateOutcome
    reason_code: ManagementUpdateReason
    policy_revision: str
    instance_id: str | None
    repository: str | None
    action: ManagementUpdateAction | None = None
    reconciliation: ReconciliationAccess = ReconciliationAccess.NONE
    journal_revision: int | None = None


@dataclass(frozen=True)
class AcceptedManagementUpdate:
    """Non-secret original admission facts; persist via the operation journal."""

    intent: ManagementUpdateIntent
    policy_revision: str
    original_authority: AcceptedAuthority


def _decision(
    intent: ManagementUpdateIntent,
    outcome: ManagementUpdateOutcome,
    reason: ManagementUpdateReason,
    revision: str,
    reconciliation: ReconciliationAccess = ReconciliationAccess.NONE,
) -> ManagementUpdateDecision:
    verified = outcome is ManagementUpdateOutcome.MANAGER_UPDATE_ALLOWED or reconciliation is ReconciliationAccess.EXACT_RECEIPT_OBSERVATION_ONLY
    return ManagementUpdateDecision(
        outcome,
        reason,
        revision,
        intent.instance_id if verified else None,
        intent.repository if verified else None,
        action=ManagementUpdateAction.SELF_UPDATE if verified else None,
        reconciliation=reconciliation,
    )


def _current_policy(policy_path: Path, bootstrap_path: Path, intent: ManagementUpdateIntent) -> ManagementUpdateDecision:
    loaded = load_operating_policy(policy_path)
    if loaded.policy is None or loaded.outcome is not DecisionOutcome.ALLOWED:
        return _decision(intent, ManagementUpdateOutcome.UNAVAILABLE, ManagementUpdateReason.POLICY_UNAVAILABLE, loaded.revision)

    policy = loaded.policy
    try:
        trusted_instance = BootstrapConfig.read(bootstrap_path).instance_id
    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError):
        return _decision(intent, ManagementUpdateOutcome.UNAVAILABLE, ManagementUpdateReason.BOOTSTRAP_UNAVAILABLE, policy.revision)

    if intent.action != ManagementUpdateAction.SELF_UPDATE:
        return _decision(intent, ManagementUpdateOutcome.DENIED, ManagementUpdateReason.ACTION_UNKNOWN, policy.revision)
    if intent.instance_id != trusted_instance:
        return _decision(intent, ManagementUpdateOutcome.DENIED, ManagementUpdateReason.INSTANCE_MISMATCH, policy.revision)
    if intent.repository != MANAGEMENT_REPOSITORY:
        return _decision(intent, ManagementUpdateOutcome.DENIED, ManagementUpdateReason.REPOSITORY_MISMATCH, policy.revision)
    if policy.profile is not PolicyProfile.FULL:
        return _decision(intent, ManagementUpdateOutcome.DENIED, ManagementUpdateReason.PROFILE_NOT_FULL, policy.revision)
    management = policy.management_self_update
    if management is None or not management.enabled:
        return _decision(intent, ManagementUpdateOutcome.DENIED, ManagementUpdateReason.SELF_UPDATE_DISABLED, policy.revision)
    if management.instance_id != trusted_instance:
        return _decision(intent, ManagementUpdateOutcome.DENIED, ManagementUpdateReason.INSTANCE_MISMATCH, policy.revision)
    if management.repository != intent.repository:
        return _decision(intent, ManagementUpdateOutcome.DENIED, ManagementUpdateReason.REPOSITORY_MISMATCH, policy.revision)
    return _decision(intent, ManagementUpdateOutcome.MANAGER_UPDATE_ALLOWED, ManagementUpdateReason.ALLOWED, policy.revision)


def authorize_management_update(
    policy_path: Path,
    bootstrap_path: Path,
    grants: OAuthGrantService,
    bearer_token: str,
    intent: ManagementUpdateIntent,
) -> tuple[ManagementUpdateDecision, AcceptedManagementUpdate | None]:
    """Use real current bearer admission; no claimed UID, grant ID or scope input."""

    decision = _current_policy(policy_path, bootstrap_path, intent)
    if decision.outcome is not ManagementUpdateOutcome.MANAGER_UPDATE_ALLOWED:
        return decision, None
    try:
        grant = grants.authenticate_access(bearer_token, grants.settings.resource, REQUIRED_SCOPES)
        original = grants.accepted_authority(grant)
    except Unavailable:
        return _decision(intent, ManagementUpdateOutcome.UNAVAILABLE, ManagementUpdateReason.AUTHORITY_UNAVAILABLE, decision.policy_revision), None
    except (Denied, InvalidScope, OAuthError):
        return _decision(intent, ManagementUpdateOutcome.DENIED, ManagementUpdateReason.CONSENT_DENIED, decision.policy_revision), None
    return decision, AcceptedManagementUpdate(intent, decision.policy_revision, original)


def journal_entry_for_update(accepted: AcceptedManagementUpdate, source_sha: str, idempotency_key: str) -> tuple[OperationIntent, JournalAuthority]:
    """Bind the original consent and policy revision to an immutable journal intent.

    The caller must persist these as a single accepted operation before dispatch.
    This function does not admit an operation or certify the candidate source.
    """

    if accepted.intent.action != ManagementUpdateAction.SELF_UPDATE or accepted.intent.repository != MANAGEMENT_REPOSITORY or not accepted.intent.instance_id or not _REVISION_PATTERN.fullmatch(accepted.policy_revision) or not _SHA_PATTERN.fullmatch(source_sha):
        raise ValueError("invalid management update identity or source SHA")
    authority = accepted.original_authority
    if not set(REQUIRED_SCOPES) <= set(authority.accepted_scopes):
        raise ValueError("original grant did not consent to management deployment")
    target = ManagedTarget(accepted.intent.instance_id, MANAGEMENT_JOURNAL_CHANNEL, accepted.intent.repository)
    intent = OperationIntent(target, ManagementUpdateAction.SELF_UPDATE.value, (ActionParameter("policy_revision", accepted.policy_revision), ActionParameter("source_sha", source_sha)), idempotency_key)
    original = JournalAuthority(authority.issuer, authority.resource, authority.project_id, authority.uid, authority.client_id, authority.grant_id, authority.accepted_scopes, authority.auth_time, authority.accepted_at)
    return intent, original


def _recorded_update(record: OperationRecord) -> tuple[ManagementUpdateIntent, AcceptedAuthority, str] | None:
    """Decode only the exact management action and original, journal-owned facts."""

    target = record.intent.target
    if target.channel != MANAGEMENT_JOURNAL_CHANNEL or target.repository != MANAGEMENT_REPOSITORY or record.intent.action != ManagementUpdateAction.SELF_UPDATE.value or record.intent.expected_incarnation or record.intent.expected_artifact:
        return None
    parameters = record.intent.parameters
    if {item.name for item in parameters} != {"policy_revision", "source_sha"} or len(parameters) != 2:
        return None
    values = {item.name: item.value for item in parameters}
    revision, sha = values["policy_revision"], values["source_sha"]
    if not isinstance(revision, str) or not _REVISION_PATTERN.fullmatch(revision) or not isinstance(sha, str) or not _SHA_PATTERN.fullmatch(sha):
        return None
    original = record.authority
    if not set(REQUIRED_SCOPES) <= set(original.accepted_scopes):
        return None
    reference = AcceptedAuthority(original.trusted_grant_id, original.project_id, original.uid, original.authentication_time, original.oauth_client_id, original.issuer, original.resource, original.accepted_scopes, original.accepted_at)
    return ManagementUpdateIntent(target.instance_id, target.repository), reference, revision


def recheck_management_update_before_effect(
    policy_path: Path,
    bootstrap_path: Path,
    grants: OAuthGrantService,
    journal: ManagementOperationStore,
    operation_id: str,
) -> ManagementUpdateDecision:
    """Read the exact durable operation; authorize only its *first unsent* effect.

    An already-possibly-sent operation can be observed by its immutable ID,
    but this result never authorizes a second send, rollback, or recovery.
    """

    lookup = journal.get(operation_id)
    unknown = ManagementUpdateIntent("", MANAGEMENT_REPOSITORY)
    if lookup.status is ReadStatus.UNAVAILABLE:
        return _decision(unknown, ManagementUpdateOutcome.UNAVAILABLE, ManagementUpdateReason.JOURNAL_UNAVAILABLE, "unavailable")
    if lookup.record is None:
        return _decision(unknown, ManagementUpdateOutcome.DENIED, ManagementUpdateReason.OPERATION_UNKNOWN, "unavailable")
    recorded = _recorded_update(lookup.record)
    if recorded is None:
        return _decision(unknown, ManagementUpdateOutcome.DENIED, ManagementUpdateReason.OPERATION_MISMATCH, "unavailable")
    intent, original, accepted_revision = recorded
    decision = _current_policy(policy_path, bootstrap_path, intent)
    if lookup.record.effect.state is not EffectState.NOT_SENT:
        # Policy/grant loss cannot turn an already-sent operation into a fresh
        # authorization. The future dispatcher owns exact receipt observation.
        return _decision(intent, ManagementUpdateOutcome.DENIED, ManagementUpdateReason.EFFECT_ALREADY_SENT, decision.policy_revision, ReconciliationAccess.EXACT_RECEIPT_OBSERVATION_ONLY)
    if lookup.record.phase not in {OperationPhase.ACCEPTED, OperationPhase.RUNNING}:
        return _decision(intent, ManagementUpdateOutcome.DENIED, ManagementUpdateReason.OPERATION_MISMATCH, decision.policy_revision)
    if decision.outcome is not ManagementUpdateOutcome.MANAGER_UPDATE_ALLOWED:
        return decision
    if decision.policy_revision != accepted_revision:
        return _decision(intent, ManagementUpdateOutcome.DENIED, ManagementUpdateReason.POLICY_REVISION_CHANGED, decision.policy_revision)
    if (original.issuer, original.resource, original.project_id) != (grants.settings.canonical_issuer, grants.settings.resource, grants.settings.firebase_project_id):
        return _decision(intent, ManagementUpdateOutcome.DENIED, ManagementUpdateReason.AUTHORITY_DENIED, decision.policy_revision)
    pending = grants.check_pending_authority(original, REQUIRED_SCOPES)
    if pending.status is PendingAuthorityStatus.UNAVAILABLE:
        return _decision(intent, ManagementUpdateOutcome.UNAVAILABLE, ManagementUpdateReason.AUTHORITY_UNAVAILABLE, decision.policy_revision)
    if pending.status is not PendingAuthorityStatus.PERMITTED:
        return _decision(intent, ManagementUpdateOutcome.DENIED, ManagementUpdateReason.AUTHORITY_DENIED, decision.policy_revision)
    # A dispatcher must CAS this exact journal revision when reserving an effect.
    latest = journal.get(operation_id)
    if latest.status is ReadStatus.UNAVAILABLE:
        return _decision(intent, ManagementUpdateOutcome.UNAVAILABLE, ManagementUpdateReason.JOURNAL_UNAVAILABLE, decision.policy_revision)
    if latest.record != lookup.record:
        return _decision(intent, ManagementUpdateOutcome.DENIED, ManagementUpdateReason.OPERATION_CHANGED, decision.policy_revision)
    return replace(decision, journal_revision=lookup.record.revision)
