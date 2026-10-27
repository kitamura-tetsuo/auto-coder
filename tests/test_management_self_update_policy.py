"""Real loader, OAuth grant, and durable journal tests for management opt-in."""

import base64
import hashlib
from dataclasses import replace

import pytest
import yaml

from auto_coder.management.action_scopes import DEPLOY_SCOPE, OPERATE_SCOPE, READ_SCOPE
from auto_coder.management.identity import AdmissionResult, AdmissionStatus, ReasonCode, VerifiedPrincipal
from auto_coder.management.oauth_config import load_management_settings
from auto_coder.management.oauth_grants import AuthorizationRequest, OAuthGrantService
from auto_coder.management.operation_store import ManagementOperationStore
from auto_coder.management.operations import AdmissionStatus as OperationAdmissionStatus
from auto_coder.management.operations import MutationStatus
from auto_coder.management.self_update_policy import (
    MANAGEMENT_REPOSITORY,
    ManagementUpdateAction,
    ManagementUpdateIntent,
    ManagementUpdateReason,
    authorize_management_update_request,
    journal_values_for_management_update,
    recheck_management_update_operation,
)
from auto_coder.operating_policy import (
    ActionClass,
    DecisionOutcome,
    PolicyIntent,
    PolicyProfile,
    evaluate_operating_policy,
    load_operating_policy,
)


class OperatorIdentity:
    def __init__(self):
        self.allowed = True
        self.available = True
        self.principal = VerifiedPrincipal("project", "operator", 10)

    def _result(self):
        if not self.available:
            return AdmissionResult(AdmissionStatus.UNAVAILABLE, ReasonCode.POLICY_UNAVAILABLE)
        if not self.allowed:
            return AdmissionResult(AdmissionStatus.DENIED, ReasonCode.OPERATOR_NOT_ALLOWED)
        return AdmissionResult(AdmissionStatus.PERMITTED, ReasonCode.PERMITTED, self.principal)

    def verify_login(self, id_token):
        return self._result()

    def recheck_identity(self, principal):
        return self._result()

    def check_operator_policy(self, principal):
        return self._result()


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    config = tmp_path / "management.yaml"
    policy = tmp_path / "operating.yaml"
    clock = [1000.0]
    identity = OperatorIdentity()
    environment = {"root": root, "config": config, "policy": policy, "clock": clock, "identity": identity}
    _write_settings(environment)
    _write_policy(environment)
    environment["grants"] = _service(environment)
    environment["token"] = _issue_token(environment, (READ_SCOPE, DEPLOY_SCOPE))
    return environment


def _write_settings(env, scopes=(READ_SCOPE, DEPLOY_SCOPE, OPERATE_SCOPE), client_scopes=None, instance="manager-1"):
    document = {
        "state_root": str(env["root"]),
        "public_origin": "https://manager.example",
        "firebase_project_id": "project",
        "management_instance_id": instance,
        "enabled_scopes": list(scopes),
        "clients": [
            {
                "client_id": "operator-console",
                "redirect_uris": ["https://console.example/callback"],
                "allowed_scopes": list(client_scopes if client_scopes is not None else scopes),
            }
        ],
    }
    env["config"].write_text(yaml.safe_dump(document), encoding="utf-8")


def _write_policy(env, profile="full", enabled=True, configured=True, instance="manager-1", repository=MANAGEMENT_REPOSITORY):
    document = {
        "profile": profile,
        "targets": {
            "beta-worker": {"channel": "beta", "repositories": ["sample/worker"]},
            "release-worker": {"channel": "release", "repositories": ["sample/worker"]},
        },
    }
    if configured:
        manager = {"instance_id": instance, "repository": repository}
        if enabled is not None:
            manager["enabled"] = enabled
        document["management_self_update"] = manager
    env["policy"].write_text(yaml.safe_dump(document), encoding="utf-8")


def _service(env):
    settings = load_management_settings(env["config"])
    return OAuthGrantService(settings.oauth, env["identity"], clock=lambda: env["clock"][0])


def _issue_token(env, scopes):
    service = env["grants"]
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    request = AuthorizationRequest("operator-console", "https://console.example/callback", service.settings.resource, challenge, scopes)
    pending = service.begin_authorization(request, "browser", "csrf")
    approval = service.approve_authorization(pending.request_id, "browser", "csrf", "identity")
    issued = service.exchange_code("operator-console", approval.code, "https://console.example/callback", service.settings.resource, verifier)
    return issued


def _intent(instance="manager-1", repository=MANAGEMENT_REPOSITORY, action=ManagementUpdateAction.SELF_UPDATE):
    return ManagementUpdateIntent(instance, repository, action)


def _authorize(env, token=None, intent=None):
    credential = env["token"].access_token if token is None else token
    return authorize_management_update_request(env["policy"], env["config"], env["grants"], credential, intent or _intent())


def _journaled(env):
    accepted = _authorize(env)
    assert accepted.outcome is DecisionOutcome.ALLOWED and accepted.accepted is not None
    journal_intent, authority = journal_values_for_management_update(accepted.accepted, "client-key", "tested-sha-123")
    journal = ManagementOperationStore(env["root"])
    admitted = journal.admit(journal_intent, authority)
    assert admitted.status is OperationAdmissionStatus.CREATED and admitted.record is not None
    return journal, admitted.record, accepted


@pytest.mark.parametrize("profile", ("observe", "beta", "full"))
@pytest.mark.parametrize("opt_in", ("absent", "omitted_false", False, True))
def test_profiles_and_explicit_opt_in_from_production_loader(setup, profile, opt_in):
    _write_policy(setup, profile=profile, configured=opt_in != "absent", enabled=None if opt_in == "omitted_false" else opt_in)
    decision = _authorize(setup)
    permitted = profile == "full" and opt_in is True

    assert decision.outcome is (DecisionOutcome.ALLOWED if permitted else DecisionOutcome.DENIED)
    assert decision.reason_code is (ManagementUpdateReason.ALLOWED if permitted else ManagementUpdateReason.PROFILE_NOT_FULL if profile != "full" else ManagementUpdateReason.OPT_IN_DISABLED)
    assert (decision.accepted is not None) is permitted
    assert decision.policy_revision.startswith("sha256:")
    assert load_operating_policy(setup["policy"]).policy.profile is PolicyProfile(profile)


@pytest.mark.parametrize(
    ("intent", "reason"),
    [
        (_intent(instance="other-instance"), ManagementUpdateReason.TARGET_MISMATCH),
        (_intent(repository="other/management"), ManagementUpdateReason.REPOSITORY_MISMATCH),
        (_intent(action="restart"), ManagementUpdateReason.ACTION_UNSUPPORTED),
        (_intent(action="bootstrap-supervisor-self-update"), ManagementUpdateReason.ACTION_UNSUPPORTED),
        (_intent(action="release-channel-promotion"), ManagementUpdateReason.ACTION_UNSUPPORTED),
    ],
)
def test_only_exact_management_instance_repository_and_action_are_eligible(setup, intent, reason):
    decision = _authorize(setup, intent=intent)
    assert decision.outcome is DecisionOutcome.DENIED
    assert decision.reason_code is reason
    assert decision.accepted is None
    assert decision.target.instance_id == "manager-1"


@pytest.mark.parametrize(
    ("settings_instance", "policy_instance", "policy_repository", "expected"),
    [
        (None, "manager-1", MANAGEMENT_REPOSITORY, ManagementUpdateReason.INSTANCE_UNCONFIGURED),
        ("manager-1", "other-manager", MANAGEMENT_REPOSITORY, ManagementUpdateReason.TARGET_MISMATCH),
        ("manager-1", "manager-1", "other/repo", ManagementUpdateReason.TARGET_MISMATCH),
    ],
)
def test_local_instance_binding_is_not_a_caller_worker_target(setup, settings_instance, policy_instance, policy_repository, expected):
    _write_settings(setup, instance=settings_instance)
    setup["grants"] = _service(setup)
    _write_policy(setup, instance=policy_instance, repository=policy_repository)

    assert _authorize(setup).reason_code is expected


@pytest.mark.parametrize("malformed", ("enabled: 'true'", "enabled: 1", "enabled: null", "enabled: true\n  forbidden: true"))
def test_malformed_opt_in_fails_closed_without_a_worker_target_alias(setup, malformed):
    policy = setup["policy"]
    policy.write_text(
        "profile: full\ntargets: {beta-worker: {channel: beta, repositories: [sample/worker]}}\n" f"management_self_update:\n  instance_id: manager-1\n  repository: {MANAGEMENT_REPOSITORY}\n  {malformed}\n",
        encoding="utf-8",
    )
    decision = _authorize(setup)
    assert decision.outcome is DecisionOutcome.UNAVAILABLE
    assert decision.reason_code is ManagementUpdateReason.POLICY_UNAVAILABLE
    assert decision.policy_revision.startswith("invalid-sha256:")


def test_missing_unreadable_and_conflicting_operator_config_is_unavailable(setup):
    setup["policy"].unlink()
    assert _authorize(setup).reason_code is ManagementUpdateReason.POLICY_UNAVAILABLE
    _write_policy(setup)
    setup["config"].unlink()
    assert _authorize(setup).reason_code is ManagementUpdateReason.CONFIG_UNAVAILABLE
    _write_settings(setup)
    setup["config"].write_text("management_instance_id: [invalid]\n", encoding="utf-8")
    assert _authorize(setup).reason_code is ManagementUpdateReason.CONFIG_UNAVAILABLE
    _write_settings(setup, scopes=(READ_SCOPE,))
    assert _authorize(setup).reason_code is ManagementUpdateReason.AUTHORITY_SERVICE_MISMATCH


@pytest.mark.parametrize("scopes", ((READ_SCOPE,), (READ_SCOPE, OPERATE_SCOPE), (READ_SCOPE, DEPLOY_SCOPE), (READ_SCOPE, OPERATE_SCOPE, DEPLOY_SCOPE)))
def test_actual_original_oauth_consent_requires_read_and_deploy(setup, scopes):
    tokens = _issue_token(setup, scopes)
    decision = _authorize(setup, token=tokens.access_token)
    has_deploy = DEPLOY_SCOPE in scopes
    assert decision.outcome is (DecisionOutcome.ALLOWED if has_deploy else DecisionOutcome.DENIED)
    assert decision.reason_code is (ManagementUpdateReason.ALLOWED if has_deploy else ManagementUpdateReason.GRANT_DENIED)


def test_request_grant_id_or_firebase_uid_does_not_authenticate_and_operator_must_be_current(setup):
    assert _authorize(setup, token="operator").reason_code is ManagementUpdateReason.GRANT_DENIED
    assert _authorize(setup, token="arbitrary-grant-id").outcome is DecisionOutcome.DENIED
    setup["identity"].allowed = False
    assert _authorize(setup).reason_code is ManagementUpdateReason.GRANT_DENIED
    setup["identity"].allowed = True
    setup["identity"].available = False
    decision = _authorize(setup)
    assert decision.outcome is DecisionOutcome.UNAVAILABLE
    assert decision.reason_code is ManagementUpdateReason.GRANT_UNAVAILABLE


def test_accepted_original_context_is_durable_and_rechecked_before_first_effect(setup):
    journal, record, admission = _journaled(setup)
    assert record.authority.trusted_grant_id == admission.accepted.authority.grant_id
    assert record.authority.accepted_scopes == (READ_SCOPE, DEPLOY_SCOPE)
    assert record.intent.target.channel == "management"
    assert record.intent.target.repository == MANAGEMENT_REPOSITORY
    assert record.intent.action == ManagementUpdateAction.SELF_UPDATE.value
    assert record.intent.expected_artifact == "tested-sha-123"

    reopened = ManagementOperationStore(setup["root"])
    initial = recheck_management_update_operation(setup["policy"], setup["config"], setup["grants"], reopened, record.operation_id)
    assert initial.outcome is DecisionOutcome.ALLOWED
    assert initial.policy_revision == admission.policy_revision

    # The access token may expire; delayed work rechecks the original durable grant.
    setup["clock"][0] = 2000.0
    assert recheck_management_update_operation(setup["policy"], setup["config"], setup["grants"], journal, record.operation_id).outcome is DecisionOutcome.ALLOWED
    setup["grants"].revoke("operator-console", setup["token"].refresh_token)
    revoked = recheck_management_update_operation(setup["policy"], setup["config"], setup["grants"], journal, record.operation_id)
    assert revoked.outcome is DecisionOutcome.DENIED
    assert revoked.reason_code is ManagementUpdateReason.GRANT_DENIED
    assert revoked.observation is None


@pytest.mark.parametrize("change", ("observe", "opt_out", "unrelated_revision", "unreadable"))
def test_first_effect_refuses_changed_profile_opt_in_revision_or_unreadable_policy(setup, change):
    journal, record, accepted = _journaled(setup)
    if change == "observe":
        _write_policy(setup, profile="observe")
    elif change == "opt_out":
        _write_policy(setup, enabled=False)
    elif change == "unrelated_revision":
        document = yaml.safe_load(setup["policy"].read_text(encoding="utf-8"))
        document["targets"]["beta-worker"]["repositories"] = ["different/worker"]
        setup["policy"].write_text(yaml.safe_dump(document), encoding="utf-8")
    else:
        setup["policy"].unlink()

    rejected = recheck_management_update_operation(setup["policy"], setup["config"], setup["grants"], journal, record.operation_id)
    assert rejected.outcome is (DecisionOutcome.UNAVAILABLE if change == "unreadable" else DecisionOutcome.DENIED)
    assert (
        rejected.reason_code
        is {
            "observe": ManagementUpdateReason.PROFILE_NOT_FULL,
            "opt_out": ManagementUpdateReason.OPT_IN_DISABLED,
            "unrelated_revision": ManagementUpdateReason.POLICY_REVISION_CHANGED,
            "unreadable": ManagementUpdateReason.POLICY_UNAVAILABLE,
        }[change]
    )
    assert rejected.accepted is None
    assert rejected.observation is None
    assert rejected.policy_revision != accepted.policy_revision


def test_original_consent_is_not_rescued_by_a_new_bearer_or_service_scope_expansion(setup):
    journal, record, admission = _journaled(setup)
    original = admission.accepted.authority
    setup["grants"].revoke("operator-console", setup["token"].refresh_token)
    new_tokens = _issue_token(setup, (READ_SCOPE, DEPLOY_SCOPE))
    assert _authorize(setup, token=new_tokens.access_token).outcome is DecisionOutcome.ALLOWED
    assert recheck_management_update_operation(setup["policy"], setup["config"], setup["grants"], journal, record.operation_id).reason_code is ManagementUpdateReason.GRANT_DENIED
    assert record.authority.trusted_grant_id == original.grant_id


def test_original_deploy_consent_can_be_disabled_by_service_and_client(setup):
    journal, record, _ = _journaled(setup)
    _write_settings(setup, scopes=(READ_SCOPE,), client_scopes=(READ_SCOPE,))
    setup["grants"] = _service(setup)
    denied = recheck_management_update_operation(setup["policy"], setup["config"], setup["grants"], journal, record.operation_id)
    assert denied.outcome is DecisionOutcome.DENIED
    assert denied.reason_code is ManagementUpdateReason.GRANT_DENIED
    _write_settings(setup, scopes=(READ_SCOPE, DEPLOY_SCOPE), client_scopes=(READ_SCOPE,))
    setup["grants"] = _service(setup)
    assert recheck_management_update_operation(setup["policy"], setup["config"], setup["grants"], journal, record.operation_id).reason_code is ManagementUpdateReason.GRANT_DENIED


@pytest.mark.parametrize("change", ("uid_revoked", "uid_unavailable", "grant_expired", "instance_changed", "config_unreadable"))
def test_fresh_pre_effect_check_rejects_original_authority_or_instance_loss(setup, change):
    journal, record, _ = _journaled(setup)
    if change == "uid_revoked":
        setup["identity"].allowed = False
    elif change == "uid_unavailable":
        setup["identity"].available = False
    elif change == "grant_expired":
        setup["clock"][0] += setup["grants"].settings.grant_lifetime + 1
    elif change == "instance_changed":
        _write_settings(setup, instance="new-manager")
    else:
        setup["config"].unlink()

    current = recheck_management_update_operation(setup["policy"], setup["config"], setup["grants"], journal, record.operation_id)
    expected = {
        "uid_revoked": (DecisionOutcome.DENIED, ManagementUpdateReason.GRANT_DENIED),
        "uid_unavailable": (DecisionOutcome.UNAVAILABLE, ManagementUpdateReason.GRANT_UNAVAILABLE),
        "grant_expired": (DecisionOutcome.DENIED, ManagementUpdateReason.GRANT_DENIED),
        "instance_changed": (DecisionOutcome.DENIED, ManagementUpdateReason.TARGET_MISMATCH),
        "config_unreadable": (DecisionOutcome.UNAVAILABLE, ManagementUpdateReason.CONFIG_UNAVAILABLE),
    }[change]
    assert (current.outcome, current.reason_code) == expected
    assert current.observation is None


def test_possibly_sent_effect_returns_exact_observation_but_never_new_forward_authority(setup):
    journal, record, _ = _journaled(setup)
    assert journal.begin_effect(record.operation_id, 0, owner_attempt_id="owner", attempt_id="attempt-A", correlation_identity="effect-123").status is MutationStatus.UPDATED
    _write_policy(setup, enabled=False)
    setup["grants"].revoke("operator-console", setup["token"].refresh_token)

    reopened = ManagementOperationStore(setup["root"])
    decision = recheck_management_update_operation(setup["policy"], setup["config"], setup["grants"], reopened, record.operation_id)
    assert decision.outcome is DecisionOutcome.DENIED
    assert decision.reason_code is ManagementUpdateReason.OPT_IN_DISABLED
    assert decision.observation is not None
    assert (decision.observation.operation_id, decision.observation.attempt_id, decision.observation.correlation_identity, decision.observation.expected_artifact) == (
        record.operation_id,
        "attempt-A",
        "effect-123",
        "tested-sha-123",
    )
    assert decision.accepted is None

    _write_policy(setup)
    still_sent = recheck_management_update_operation(setup["policy"], setup["config"], setup["grants"], reopened, record.operation_id)
    assert still_sent.outcome is DecisionOutcome.DENIED
    assert still_sent.observation == decision.observation
    assert journal.begin_effect(record.operation_id, 1, owner_attempt_id="new", attempt_id="attempt-B", correlation_identity="effect-456").status is MutationStatus.INVALID


def test_already_sent_with_valid_consent_still_cannot_be_reissued(setup):
    journal, record, _ = _journaled(setup)
    journal.begin_effect(record.operation_id, 0, owner_attempt_id="owner", attempt_id="attempt-A", correlation_identity="effect-123")
    blocked = recheck_management_update_operation(setup["policy"], setup["config"], setup["grants"], journal, record.operation_id)
    assert blocked.outcome is DecisionOutcome.DENIED
    assert blocked.reason_code is ManagementUpdateReason.EFFECT_ALREADY_SENT
    assert blocked.observation.attempt_id == "attempt-A"


def test_invalid_or_unknown_durable_operation_is_never_a_management_authority(setup):
    journal = ManagementOperationStore(setup["root"])
    assert recheck_management_update_operation(setup["policy"], setup["config"], setup["grants"], journal, "unknown").reason_code is ManagementUpdateReason.OPERATION_UNKNOWN
    _, _, accepted = _journaled(setup)
    journal_intent, auth = journal_values_for_management_update(accepted.accepted, "other-key", "other-sha")
    forged = replace(journal_intent, target=replace(journal_intent.target, channel="beta"))
    another_root = setup["root"] / "other-journal"
    admitted = ManagementOperationStore(another_root).admit(forged, auth)
    assert admitted.status is OperationAdmissionStatus.CREATED
    invalid = recheck_management_update_operation(setup["policy"], setup["config"], setup["grants"], ManagementOperationStore(another_root), admitted.record.operation_id)
    assert invalid.reason_code is ManagementUpdateReason.ORIGINAL_CONTEXT_INVALID
    assert invalid.outcome is DecisionOutcome.DENIED


@pytest.mark.parametrize("profile", ("observe", "beta", "full"))
@pytest.mark.parametrize("channel", ("beta", "release"))
@pytest.mark.parametrize("action", tuple(ActionClass))
def test_worker_policy_matrix_unchanged_by_management_namespace(setup, profile, channel, action):
    _write_policy(setup, profile=profile, configured=False)
    before = evaluate_operating_policy(setup["policy"], PolicyIntent(f"{channel}-worker", "sample/worker", action))
    baseline_revision = before.policy_revision
    _write_policy(setup, profile=profile, enabled=True)
    after = evaluate_operating_policy(setup["policy"], PolicyIntent(f"{channel}-worker", "sample/worker", action))

    assert (after.outcome, after.reason_code, after.channel, after.repository) == (before.outcome, before.reason_code, before.channel, before.repository)
    assert after.policy_revision != baseline_revision
    _write_policy(setup, profile=profile, configured=False)
    assert load_operating_policy(setup["policy"]).revision == baseline_revision
