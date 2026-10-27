"""Production-loader, original-grant and journal regressions for Issue #2625."""

import json
from pathlib import Path

import pytest

from auto_coder.management.action_scopes import DEPLOY_SCOPE, OPERATE_SCOPE, READ_SCOPE
from auto_coder.management.oauth_config import OAuthClient, OAuthSettings
from auto_coder.management.oauth_grants import AuthorizationRequest, OAuthGrantService
from auto_coder.management.operation_store import ManagementOperationStore
from auto_coder.management.operations import AdmissionStatus, EffectState, ManagedTarget, MutationStatus, OperationIntent
from auto_coder.management.self_update_policy import (
    MANAGEMENT_JOURNAL_CHANNEL,
    ManagementUpdateAction,
    ManagementUpdateIntent,
    ManagementUpdateOutcome,
    ManagementUpdateReason,
    ReconciliationAccess,
    authorize_management_update,
    journal_entry_for_update,
    recheck_management_update_before_effect,
)
from auto_coder.operating_policy import ActionClass, DecisionOutcome, PolicyIntent, evaluate_operating_policy, load_operating_policy
from tests.test_management_oauth_grants_production import pkce, production_boundary  # noqa: F401

REPOSITORY = "kitamura-tetsuo/auto-coder"
INSTANCE = "manager-main"
SHA = "a" * 40
ROOT = Path(__file__).parents[1]


def _policy(path: Path, profile: str = "full", enabled: bool | None = True, instance: str = INSTANCE, repository: str = REPOSITORY) -> None:
    content = f"profile: {profile}\ntargets:\n  beta-service: {{channel: beta, repositories: [example/widgets]}}\n  release-service: {{channel: release, repositories: [example/widgets]}}\n"
    if enabled is not None:
        content += f"management_self_update:\n  enabled: {str(enabled).lower()}\n  instance_id: {instance}\n  repository: {repository}\n"
    path.write_text(content, encoding="utf-8")


def _bootstrap(path: Path, instance: str = INSTANCE) -> None:
    path.write_text(
        json.dumps(
            {
                "instance_id": instance,
                "state_root": str(path.parent / "state"),
                "oauth_root": str(path.parent / "oauth"),
                "journal_root": str(path.parent / "journal"),
                "listener_url": "http://127.0.0.1:8081/mcp",
                "initial_runtime": "initial",
                "runtimes": [{"reference": "initial", "receipt": "sha256:" + "0" * 64, "executable": "/usr/bin/python3", "arguments": []}],
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def boundary(tmp_path, production_boundary):
    previous, identity, signed_token, _firebase_state, operator_path, clock = production_boundary
    scopes = (READ_SCOPE, OPERATE_SCOPE, DEPLOY_SCOPE)
    settings = OAuthSettings(
        previous.settings.database_path,
        previous.settings.issuer,
        previous.settings.firebase_project_id,
        (OAuthClient("client", ("https://client.example/callback",), allowed_scopes=scopes),),
        access_lifetime=30,
        grant_lifetime=120,
        enabled_scopes=scopes,
    )
    grants = OAuthGrantService(settings, identity, lambda: clock[0])
    policy_path = tmp_path / "operating.yaml"
    bootstrap_path = tmp_path / "bootstrap.json"
    _policy(policy_path)
    _bootstrap(bootstrap_path)

    def issue(scopes):
        verifier = "v" * 64
        request = grants.begin_authorization(AuthorizationRequest("client", "https://client.example/callback", settings.resource, pkce(verifier), scopes), "browser", "csrf")
        approved = grants.approve_authorization(request.request_id, "browser", "csrf", signed_token())
        return grants.exchange_code("client", approved.code, "https://client.example/callback", settings.resource, verifier)

    return grants, issue, policy_path, bootstrap_path, operator_path, clock, identity


def _intent(instance: str = INSTANCE, repository: str = REPOSITORY, action: ManagementUpdateAction | str = ManagementUpdateAction.SELF_UPDATE):
    return ManagementUpdateIntent(instance, repository, action)


def _accepted_record(boundary, tmp_path):
    grants, issue, policy, bootstrap, *_ = boundary
    token = issue((READ_SCOPE, DEPLOY_SCOPE))
    decision, accepted = authorize_management_update(policy, bootstrap, grants, token.access_token, _intent())
    assert decision.outcome is ManagementUpdateOutcome.MANAGER_UPDATE_ALLOWED
    assert accepted is not None
    operation, original = journal_entry_for_update(accepted, SHA, "original-operation")
    store = ManagementOperationStore(tmp_path / "operations")
    admitted = store.admit(operation, original)
    assert admitted.status is AdmissionStatus.CREATED and admitted.record is not None
    return store, admitted.record.operation_id, token, accepted


@pytest.mark.parametrize("profile", ("observe", "beta", "full"))
@pytest.mark.parametrize("enabled", (None, False, True))
def test_full_and_explicit_opt_in_are_both_required(boundary, profile, enabled):
    grants, issue, policy, bootstrap, *_ = boundary
    _policy(policy, profile, enabled)
    token = issue((READ_SCOPE, DEPLOY_SCOPE))
    decision, accepted = authorize_management_update(policy, bootstrap, grants, token.access_token, _intent())

    allowed = profile == "full" and enabled is True
    assert decision.outcome is (ManagementUpdateOutcome.MANAGER_UPDATE_ALLOWED if allowed else ManagementUpdateOutcome.DENIED)
    assert (accepted is not None) is allowed
    assert decision.reason_code is (ManagementUpdateReason.ALLOWED if allowed else ManagementUpdateReason.PROFILE_NOT_FULL if profile != "full" else ManagementUpdateReason.SELF_UPDATE_DISABLED)
    assert decision.policy_revision == load_operating_policy(policy).revision
    assert decision.instance_id == (INSTANCE if allowed else None)
    assert decision.repository == (REPOSITORY if allowed else None)
    assert decision.action is (ManagementUpdateAction.SELF_UPDATE if allowed else None)


@pytest.mark.parametrize("scopes", ((READ_SCOPE,), (READ_SCOPE, OPERATE_SCOPE), (READ_SCOPE, DEPLOY_SCOPE)))
def test_only_real_original_read_plus_deploy_consent_can_admit(boundary, scopes):
    grants, issue, policy, bootstrap, *_ = boundary
    token = issue(scopes)
    decision, accepted = authorize_management_update(policy, bootstrap, grants, token.access_token, _intent())
    assert decision.outcome is (ManagementUpdateOutcome.MANAGER_UPDATE_ALLOWED if DEPLOY_SCOPE in scopes else ManagementUpdateOutcome.DENIED)
    assert decision.reason_code is (ManagementUpdateReason.ALLOWED if DEPLOY_SCOPE in scopes else ManagementUpdateReason.CONSENT_DENIED)
    assert (accepted is not None) is (DEPLOY_SCOPE in scopes)
    if accepted is not None:
        assert accepted.original_authority.accepted_scopes == scopes
        assert accepted.original_authority.grant_id


@pytest.mark.parametrize(
    ("intent", "reason"),
    [
        (_intent("foreign-manager"), ManagementUpdateReason.INSTANCE_MISMATCH),
        (_intent(repository="example/widgets"), ManagementUpdateReason.REPOSITORY_MISMATCH),
        (_intent(action="deployment-mutation"), ManagementUpdateReason.ACTION_UNKNOWN),
        (_intent(action="release-channel-promotion"), ManagementUpdateReason.ACTION_UNKNOWN),
    ],
)
def test_manager_target_cannot_be_supplied_as_worker_or_another_instance(boundary, intent, reason):
    grants, issue, policy, bootstrap, *_ = boundary
    token = issue((READ_SCOPE, DEPLOY_SCOPE))
    decision, accepted = authorize_management_update(policy, bootstrap, grants, token.access_token, intent)
    assert decision.outcome is ManagementUpdateOutcome.DENIED and decision.reason_code is reason
    assert accepted is None
    for action in ActionClass:
        worker = evaluate_operating_policy(policy, PolicyIntent(INSTANCE, REPOSITORY, action, claimed_channel="beta"))
        assert worker.outcome is DecisionOutcome.DENIED


@pytest.mark.parametrize(
    "management_config",
    [
        'management_self_update: {enabled: "true", instance_id: manager-main, repository: kitamura-tetsuo/auto-coder}\n',
        "management_self_update: {enabled: true, instance_id: manager-main, repository: other/repo}\n",
        "management_self_update: {enabled: true, instance_id: manager-main}\n",
        "management_self_update: {enabled: true, instance_id: manager-main, repository: kitamura-tetsuo/auto-coder, extra: true}\n",
        "management_self_update: {enabled: false, instance_id: manager-main, repository: kitamura-tetsuo/auto-coder}\nmanagement_self_update: {enabled: true, instance_id: manager-main, repository: kitamura-tetsuo/auto-coder}\n",
    ],
)
def test_bad_management_policy_cannot_fall_back_to_worker_authority(boundary, management_config):
    grants, issue, policy, bootstrap, *_ = boundary
    _policy(policy, enabled=None)
    policy.write_text(policy.read_text(encoding="utf-8") + management_config, encoding="utf-8")
    token = issue((READ_SCOPE, DEPLOY_SCOPE))
    decision, accepted = authorize_management_update(policy, bootstrap, grants, token.access_token, _intent())
    assert decision.outcome is ManagementUpdateOutcome.UNAVAILABLE
    assert accepted is None


def test_bootstrap_and_policy_fail_closed_on_missing_or_changed_files(boundary):
    grants, issue, policy, bootstrap, *_ = boundary
    token = issue((READ_SCOPE, DEPLOY_SCOPE))
    _bootstrap(bootstrap, "different")
    mismatch, _ = authorize_management_update(policy, bootstrap, grants, token.access_token, _intent())
    assert mismatch.reason_code is ManagementUpdateReason.INSTANCE_MISMATCH
    bootstrap.unlink()
    missing_bootstrap, _ = authorize_management_update(policy, bootstrap, grants, token.access_token, _intent())
    assert (missing_bootstrap.outcome, missing_bootstrap.reason_code) == (ManagementUpdateOutcome.UNAVAILABLE, ManagementUpdateReason.BOOTSTRAP_UNAVAILABLE)
    _bootstrap(bootstrap)
    policy.unlink()
    missing_policy, _ = authorize_management_update(policy, bootstrap, grants, token.access_token, _intent())
    assert missing_policy.outcome is ManagementUpdateOutcome.UNAVAILABLE
    assert missing_policy.policy_revision == "unavailable"


@pytest.mark.parametrize("profile", ("observe", "beta", "full"))
@pytest.mark.parametrize("enabled", (False, True))
def test_opt_in_does_not_change_any_worker_policy_action(tmp_path, profile, enabled):
    path = tmp_path / "policy.yaml"
    _policy(path, profile, enabled)
    reference = ROOT / "examples" / f"operating-policy-{profile}.yaml"
    for target in ("beta-service", "release-service"):
        for action in ActionClass:
            request = PolicyIntent(target, "example/widgets", action)
            assert evaluate_operating_policy(path, request).outcome is evaluate_operating_policy(reference, request).outcome


def test_recorded_original_consent_and_current_policy_allow_one_unsent_effect(boundary, tmp_path):
    grants, _, policy, bootstrap, _, clock, _ = boundary
    store, op_id, token, accepted = _accepted_record(boundary, tmp_path)
    assert accepted.original_authority.grant_id == store.get(op_id).record.authority.trusted_grant_id
    clock[0] += 31  # Access token expired; the original, unexpired grant still applies.
    decision = recheck_management_update_before_effect(policy, bootstrap, grants, store, op_id)
    assert decision.outcome is ManagementUpdateOutcome.MANAGER_UPDATE_ALLOWED
    assert decision.reconciliation is ReconciliationAccess.NONE
    assert decision.journal_revision == 0
    assert store.get(op_id).record.effect.state is EffectState.NOT_SENT


@pytest.mark.parametrize("change", ("disable", "observe", "revision", "revoke", "uid_remove", "uid_unavailable", "expired_grant", "service_scope", "client_scope"))
def test_pending_unsent_operation_rechecks_original_authority_and_current_policy(boundary, tmp_path, change):
    grants, _, policy, bootstrap, operator_file, clock, identity = boundary
    store, op_id, token, _ = _accepted_record(boundary, tmp_path)
    active = grants
    if change == "disable":
        _policy(policy, enabled=False)
    elif change == "observe":
        _policy(policy, profile="observe")
    elif change == "revision":
        policy.write_text(policy.read_text(encoding="utf-8").replace("example/widgets", "example/another"), encoding="utf-8")
    elif change == "revoke":
        grants.revoke("client", token.refresh_token)
    elif change == "uid_remove":
        operator_file.write_text(json.dumps({"project_id": grants.settings.firebase_project_id, "allowed_uids": []}), encoding="utf-8")
    elif change == "uid_unavailable":
        operator_file.unlink()
    elif change == "expired_grant":
        clock[0] += 121
    elif change in {"service_scope", "client_scope"}:
        client_scopes = (READ_SCOPE,) if change == "client_scope" else (READ_SCOPE, DEPLOY_SCOPE)
        enabled_scopes = (READ_SCOPE,) if change == "service_scope" else (READ_SCOPE, DEPLOY_SCOPE)
        old = grants.settings
        settings = OAuthSettings(old.database_path, old.issuer, old.firebase_project_id, (OAuthClient("client", ("https://client.example/callback",), allowed_scopes=client_scopes),), access_lifetime=30, grant_lifetime=120, enabled_scopes=enabled_scopes)
        active = OAuthGrantService(settings, identity, lambda: clock[0])

    decision = recheck_management_update_before_effect(policy, bootstrap, active, ManagementOperationStore(tmp_path / "operations"), op_id)
    expected = ManagementUpdateOutcome.UNAVAILABLE if change == "uid_unavailable" else ManagementUpdateOutcome.DENIED
    assert decision.outcome is expected
    assert decision.reconciliation is ReconciliationAccess.NONE
    assert store.get(op_id).record.effect.state is EffectState.NOT_SENT
    if change == "revision":
        assert decision.reason_code is ManagementUpdateReason.POLICY_REVISION_CHANGED


def test_unreadable_policy_prevents_unsent_effect(boundary, tmp_path):
    grants, _, policy, bootstrap, *_ = boundary
    store, op_id, _, _ = _accepted_record(boundary, tmp_path)
    policy.unlink()
    decision = recheck_management_update_before_effect(policy, bootstrap, grants, store, op_id)
    assert decision.outcome is ManagementUpdateOutcome.UNAVAILABLE
    assert decision.reason_code is ManagementUpdateReason.POLICY_UNAVAILABLE


def test_possibly_sent_effect_only_exposes_exact_receipt_observation(boundary, tmp_path):
    grants, _, policy, bootstrap, *_ = boundary
    store, op_id, token, _ = _accepted_record(boundary, tmp_path)
    sent = store.begin_effect(op_id, 0, owner_attempt_id="manager", attempt_id="first-effect", correlation_identity="manager/first")
    assert sent.status is MutationStatus.UPDATED
    _policy(policy, enabled=False)
    grants.revoke("client", token.refresh_token)
    before = store.get(op_id).record
    decision = recheck_management_update_before_effect(policy, bootstrap, grants, store, op_id)
    assert decision.outcome is ManagementUpdateOutcome.DENIED
    assert decision.reason_code is ManagementUpdateReason.EFFECT_ALREADY_SENT
    assert decision.reconciliation is ReconciliationAccess.EXACT_RECEIPT_OBSERVATION_ONLY
    assert store.get(op_id).record == before
    assert store.begin_effect(op_id, 1, owner_attempt_id="other", attempt_id="second-effect", correlation_identity="manager/second").status is MutationStatus.INVALID


def test_journal_lookup_never_treats_worker_or_forged_authority_as_manager(boundary, tmp_path):
    grants, _, policy, bootstrap, *_ = boundary
    store, op_id, _, _ = _accepted_record(boundary, tmp_path)
    original = store.get(op_id).record.authority
    worker_intent = OperationIntent(ManagedTarget(INSTANCE, "beta", REPOSITORY), ManagementUpdateAction.SELF_UPDATE.value, (), "worker")
    other = store.admit(worker_intent, original)
    assert other.status is AdmissionStatus.CREATED and other.record is not None
    refused = recheck_management_update_before_effect(policy, bootstrap, grants, store, other.record.operation_id)
    assert (refused.outcome, refused.reason_code) == (ManagementUpdateOutcome.DENIED, ManagementUpdateReason.OPERATION_MISMATCH)
    missing = recheck_management_update_before_effect(policy, bootstrap, grants, store, "missing")
    assert missing.outcome is ManagementUpdateOutcome.DENIED


def test_effect_reserved_during_authority_check_cannot_return_stale_allowance(boundary, tmp_path):
    grants, _, policy, bootstrap, *_ = boundary
    store, op_id, _, _ = _accepted_record(boundary, tmp_path)

    class InterleavingJournal(ManagementOperationStore):
        reads = 0

        def get(self, operation_id):
            self.reads += 1
            if self.reads == 2:
                changed = self.begin_effect(operation_id, 0, owner_attempt_id="other", attempt_id="other-attempt", correlation_identity="manager/other")
                assert changed.status is MutationStatus.UPDATED
            return super().get(operation_id)

    decision = recheck_management_update_before_effect(policy, bootstrap, grants, InterleavingJournal(tmp_path / "operations"), op_id)
    assert decision.outcome is ManagementUpdateOutcome.DENIED
    assert decision.reason_code is ManagementUpdateReason.OPERATION_CHANGED
    assert decision.journal_revision is None
    assert store.get(op_id).record.effect.state is EffectState.SENT_UNKNOWN


def test_unknown_policy_revision_or_unconsented_context_cannot_be_journaled(boundary, tmp_path):
    grants, _, _, _, *_ = boundary
    store, op_id, _, accepted = _accepted_record(boundary, tmp_path)
    assert store.get(op_id).record is not None
    with pytest.raises(ValueError):
        journal_entry_for_update(accepted, "not-a-source-sha", "different")
