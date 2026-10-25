from pathlib import Path

import pytest

from auto_coder.operating_policy import (
    ActionClass,
    DecisionOutcome,
    OperationClass,
    PolicyIntent,
    PolicyProfile,
    ReasonCode,
    TargetChannel,
    evaluate_operating_policy,
    load_operating_policy,
)

ROOT = Path(__file__).parents[1]
EXAMPLES = ROOT / "examples"
ACTIONS = tuple(ActionClass)


def _intent(target, action, repository="example/widgets", **kwargs):
    return PolicyIntent(target, repository, action, **kwargs)


@pytest.mark.parametrize("action", ACTIONS)
def test_omitted_profile_defaults_to_observe_and_only_allows_inspection(action):
    decision = evaluate_operating_policy(EXAMPLES / "operating-policy-observe.yaml", _intent("beta-service", action))

    expected = DecisionOutcome.ALLOWED if action is ActionClass.INSPECT else DecisionOutcome.DENIED
    assert decision.outcome is expected
    assert decision.profile is PolicyProfile.OBSERVE
    assert decision.policy_revision.startswith("sha256:")


@pytest.mark.parametrize(
    ("target", "action", "expected"),
    [
        ("beta-service", ActionClass.INSPECT, DecisionOutcome.ALLOWED),
        ("beta-service", ActionClass.WORKER_CONTROL, DecisionOutcome.ALLOWED),
        ("beta-service", ActionClass.DEPLOYMENT, DecisionOutcome.ALLOWED),
        ("beta-service", ActionClass.RELEASE_PROMOTION, DecisionOutcome.DENIED),
        ("release-service", ActionClass.INSPECT, DecisionOutcome.ALLOWED),
        ("release-service", ActionClass.WORKER_CONTROL, DecisionOutcome.DENIED),
        ("release-service", ActionClass.DEPLOYMENT, DecisionOutcome.DENIED),
        ("release-service", ActionClass.RELEASE_PROMOTION, DecisionOutcome.DENIED),
    ],
)
def test_beta_profile_matrix(target, action, expected):
    decision = evaluate_operating_policy(EXAMPLES / "operating-policy-beta.yaml", _intent(target, action))

    assert decision.outcome is expected


@pytest.mark.parametrize(
    ("target", "action"),
    [(target, action) for target in ("beta-service", "release-service") for action in ACTIONS if not (target == "beta-service" and action is ActionClass.RELEASE_PROMOTION)],
)
def test_full_profile_allows_reads_and_supported_channel_actions(target, action):
    decision = evaluate_operating_policy(EXAMPLES / "operating-policy-full.yaml", _intent(target, action))

    assert decision.outcome is DecisionOutcome.ALLOWED
    assert decision.channel is TargetChannel(target.removesuffix("-service"))


def test_full_profile_does_not_promote_a_beta_target():
    decision = evaluate_operating_policy(EXAMPLES / "operating-policy-full.yaml", _intent("beta-service", ActionClass.RELEASE_PROMOTION))

    assert decision.outcome is DecisionOutcome.DENIED
    assert decision.reason_code is ReasonCode.PROMOTION_REQUIRES_RELEASE


@pytest.mark.parametrize(
    ("intent", "reason"),
    [
        (_intent("missing", ActionClass.INSPECT), ReasonCode.TARGET_UNKNOWN),
        (_intent("beta-service", "delete-everything"), ReasonCode.ACTION_UNKNOWN),
        (_intent("beta-service", ActionClass.DEPLOYMENT, repository="other/repo"), ReasonCode.REPOSITORY_MISMATCH),
        (_intent("beta-service", ActionClass.DEPLOYMENT, repository=None), ReasonCode.REPOSITORY_REQUIRED),
        (_intent("release-service", ActionClass.DEPLOYMENT, claimed_channel="beta"), ReasonCode.CHANNEL_MISMATCH),
        (_intent("beta-service", ActionClass.DEPLOYMENT, operation="rollback"), ReasonCode.OPERATION_UNKNOWN),
        (_intent("beta-service", ActionClass.DEPLOYMENT, operation=OperationClass.RECONCILIATION), ReasonCode.RECONCILIATION_NOT_AUTHORIZED),
    ],
)
def test_invalid_or_non_authorizing_inputs_fail_closed(intent, reason):
    decision = evaluate_operating_policy(EXAMPLES / "operating-policy-full.yaml", intent)

    assert decision.outcome is DecisionOutcome.DENIED
    assert decision.reason_code is reason


def test_inspection_preserves_unknown_target_facts():
    decision = evaluate_operating_policy(EXAMPLES / "operating-policy-full.yaml", _intent("missing", ActionClass.INSPECT, repository=None))

    assert decision.outcome is DecisionOutcome.DENIED
    assert decision.channel is None
    assert decision.target_id == "missing"


@pytest.mark.parametrize(
    "contents",
    [
        "profile: fastest\ntargets: {}\n",
        "profile: full\ntargets: []\n",
        "profile: full\ntargets:\n  service:\n    channel: canary\n    repositories: [example/widgets]\n",
        "profile: full\ntargets:\n  service:\n    channel: beta\n    repositories: [not-a-repository]\n",
        "[unterminated",
    ],
)
def test_malformed_configuration_is_unavailable(tmp_path, contents):
    path = tmp_path / "policy.yaml"
    path.write_text(contents, encoding="utf-8")

    decision = evaluate_operating_policy(path, _intent("service", ActionClass.DEPLOYMENT))

    assert decision.outcome is DecisionOutcome.UNAVAILABLE
    assert decision.reason_code is ReasonCode.POLICY_MALFORMED
    assert decision.policy_revision.startswith("invalid-sha256:")


@pytest.mark.parametrize(
    "conflicting_declarations",
    [
        "profile: observe\nprofile: full\ntargets: {service: {channel: release, repositories: [example/widgets]}}\n",
        "profile: full\nprofile: observe\ntargets: {service: {channel: release, repositories: [example/widgets]}}\n",
        "profile: full\ntargets:\n  service: {channel: beta, repositories: [example/widgets]}\n  service: {channel: release, repositories: [example/widgets]}\n",
        "profile: full\ntargets:\n  service: {channel: release, repositories: [example/widgets]}\n  service: {channel: beta, repositories: [example/widgets]}\n",
        "profile: full\ntargets:\n  service:\n    channel: beta\n    channel: release\n    repositories: [example/widgets]\n",
        "profile: full\ntargets:\n  service:\n    channel: release\n    channel: beta\n    repositories: [example/widgets]\n",
        "profile: full\ntargets:\n  service:\n    channel: release\n    repositories: [other/widgets]\n    repositories: [example/widgets]\n",
        "profile: full\ntargets:\n  service:\n    channel: release\n    repositories: [example/widgets]\n    repositories: [other/widgets]\n",
    ],
)
def test_conflicting_duplicate_authority_is_unavailable_in_either_order(tmp_path, conflicting_declarations):
    path = tmp_path / "policy.yaml"
    path.write_text(conflicting_declarations, encoding="utf-8")

    decision = evaluate_operating_policy(path, _intent("service", ActionClass.DEPLOYMENT))

    assert decision.outcome is DecisionOutcome.UNAVAILABLE
    assert decision.reason_code is ReasonCode.POLICY_MALFORMED
    assert decision.policy_revision.startswith("invalid-sha256:")


def test_fresh_evaluation_observes_change_and_unreadable_file(tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text((EXAMPLES / "operating-policy-full.yaml").read_text(), encoding="utf-8")
    intent = _intent("beta-service", ActionClass.DEPLOYMENT)

    allowed = evaluate_operating_policy(path, intent)
    path.write_text((EXAMPLES / "operating-policy-observe.yaml").read_text(), encoding="utf-8")
    denied = evaluate_operating_policy(path, intent)
    path.unlink()
    unavailable = evaluate_operating_policy(path, intent)

    assert allowed.outcome is DecisionOutcome.ALLOWED
    assert denied.outcome is DecisionOutcome.DENIED
    assert denied.policy_revision != allowed.policy_revision
    assert unavailable.outcome is DecisionOutcome.UNAVAILABLE
    assert unavailable.reason_code is ReasonCode.POLICY_UNREADABLE
    assert unavailable.policy_revision == "unavailable"


def test_revision_is_canonical_for_equivalent_target_order(tmp_path):
    first = tmp_path / "first.yaml"
    second = tmp_path / "second.yaml"
    first.write_text("profile: beta\ntargets:\n  b: {channel: release, repositories: [o/b]}\n  a: {channel: beta, repositories: [o/a]}\n", encoding="utf-8")
    second.write_text("targets:\n  a: {repositories: [o/a], channel: beta}\n  b: {repositories: [o/b], channel: release}\nprofile: beta\n", encoding="utf-8")

    assert load_operating_policy(first).revision == load_operating_policy(second).revision


@pytest.mark.parametrize("profile", ("observe", "beta", "full"))
def test_documented_examples_are_loaded_by_production_loader(profile):
    loaded = load_operating_policy(EXAMPLES / f"operating-policy-{profile}.yaml")

    assert loaded.outcome is DecisionOutcome.ALLOWED
    assert loaded.policy is not None
    assert loaded.policy.profile.value == profile
    assert {target.target_id for target in loaded.policy.targets} == {"beta-service", "release-service"}
