"""Fail-closed operating policy for staged management capabilities.

This module deliberately has no management-server or credential dependencies.  It
answers only the operator-controlled phase question; consumers remain responsible
for authentication, scopes, correctness gates, and effect durability.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects overwritten policy authority."""


def _construct_unique_mapping(loader: yaml.SafeLoader, node: MappingNode, deep: bool = False) -> dict[object, object]:
    loader.flatten_mapping(node)
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError("while constructing a mapping", node.start_mark, "found an unhashable key", key_node.start_mark) from exc
        if duplicate:
            raise ConstructorError("while constructing a mapping", node.start_mark, f"found duplicate key {key!r}", key_node.start_mark)
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


class PolicyProfile(str, Enum):
    OBSERVE = "observe"
    BETA = "beta"
    FULL = "full"


class TargetChannel(str, Enum):
    BETA = "beta"
    RELEASE = "release"


class ActionClass(str, Enum):
    INSPECT = "read-only-inspection"
    WORKER_CONTROL = "worker-control-mutation"
    DEPLOYMENT = "deployment-mutation"
    RELEASE_PROMOTION = "release-channel-promotion"


class OperationClass(str, Enum):
    FORWARD = "forward"
    RECONCILIATION = "reconciliation"


class DecisionOutcome(str, Enum):
    ALLOWED = "ALLOWED"
    DENIED = "DENIED"
    UNAVAILABLE = "UNAVAILABLE"


class ReasonCode(str, Enum):
    ALLOWED_INSPECTION = "allowed_inspection"
    ALLOWED_BETA_MUTATION = "allowed_beta_mutation"
    ALLOWED_FULL_MUTATION = "allowed_full_mutation"
    ALLOWED_RELEASE_PROMOTION = "allowed_release_promotion"
    POLICY_UNREADABLE = "policy_unreadable"
    POLICY_MALFORMED = "policy_malformed"
    TARGET_UNKNOWN = "target_unknown"
    REPOSITORY_REQUIRED = "repository_required"
    REPOSITORY_MISMATCH = "repository_mismatch"
    CHANNEL_MISMATCH = "channel_mismatch"
    ACTION_UNKNOWN = "action_unknown"
    OPERATION_UNKNOWN = "operation_unknown"
    RECONCILIATION_NOT_AUTHORIZED = "reconciliation_not_authorized"
    PROFILE_DENIES_MUTATION = "profile_denies_mutation"
    CHANNEL_DENIES_MUTATION = "channel_denies_mutation"
    PROMOTION_REQUIRES_RELEASE = "promotion_requires_release"


@dataclass(frozen=True)
class PolicyTarget:
    target_id: str
    channel: TargetChannel
    repositories: tuple[str, ...]


@dataclass(frozen=True)
class ManagementSelfUpdateConfig:
    """Separate operator opt-in; never a beta or release worker target."""

    instance_id: str
    repository: str
    enabled: bool = False


@dataclass(frozen=True)
class OperatingPolicy:
    profile: PolicyProfile
    targets: tuple[PolicyTarget, ...]
    revision: str
    management_self_update: ManagementSelfUpdateConfig | None = None


@dataclass(frozen=True)
class PolicyLoadResult:
    policy: OperatingPolicy | None
    outcome: DecisionOutcome
    reason_code: ReasonCode
    revision: str


@dataclass(frozen=True)
class PolicyIntent:
    target_id: str
    repository: str | None
    action: ActionClass | str
    claimed_channel: TargetChannel | str | None = None
    operation: OperationClass | str = OperationClass.FORWARD


@dataclass(frozen=True)
class PolicyDecision:
    outcome: DecisionOutcome
    reason_code: ReasonCode
    policy_revision: str
    profile: PolicyProfile | None
    target_id: str
    channel: TargetChannel | None
    repository: str | None
    operation: OperationClass | None


_REPOSITORY_PATTERN = re.compile(r"^[^/\s]+/[^/\s]+$")
_INSTANCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _content_revision(data: object, prefix: str = "sha256") -> str:
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"{prefix}:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _invalid_revision(raw: bytes) -> str:
    return f"invalid-sha256:{hashlib.sha256(raw).hexdigest()}"


def load_operating_policy(path: Path) -> PolicyLoadResult:
    """Load one fresh local snapshot, representing every failure as unavailable."""
    try:
        raw = path.read_bytes()
    except OSError:
        return PolicyLoadResult(None, DecisionOutcome.UNAVAILABLE, ReasonCode.POLICY_UNREADABLE, "unavailable")

    try:
        document = yaml.load(raw, Loader=_UniqueKeyLoader)
        policy = _parse_policy(document)
    except (yaml.YAMLError, ValueError, TypeError):
        return PolicyLoadResult(None, DecisionOutcome.UNAVAILABLE, ReasonCode.POLICY_MALFORMED, _invalid_revision(raw))

    normalized: dict[str, object] = {
        "profile": policy.profile.value,
        "targets": {target.target_id: {"channel": target.channel.value, "repositories": list(target.repositories)} for target in sorted(policy.targets, key=lambda item: item.target_id)},
    }
    if policy.management_self_update is not None:
        manager = policy.management_self_update
        normalized["management_self_update"] = {"instance_id": manager.instance_id, "repository": manager.repository, "enabled": manager.enabled}
    snapshot = OperatingPolicy(policy.profile, policy.targets, _content_revision(normalized), policy.management_self_update)
    return PolicyLoadResult(snapshot, DecisionOutcome.ALLOWED, ReasonCode.ALLOWED_INSPECTION, snapshot.revision)


def _parse_policy(document: object) -> OperatingPolicy:
    if not isinstance(document, dict) or set(document) - {"profile", "targets", "management_self_update"}:
        raise ValueError("policy must be a mapping with only profile, targets and management_self_update")
    profile_value = document.get("profile", PolicyProfile.OBSERVE.value)
    if not isinstance(profile_value, str):
        raise ValueError("profile must be a string")
    try:
        profile = PolicyProfile(profile_value)
    except ValueError as exc:
        raise ValueError("unknown profile") from exc

    target_values = document.get("targets")
    if not isinstance(target_values, dict) or not target_values:
        raise ValueError("targets must be a non-empty mapping")
    targets: list[PolicyTarget] = []
    for target_id, value in target_values.items():
        if not isinstance(target_id, str) or not target_id.strip() or not isinstance(value, dict):
            raise ValueError("invalid target")
        if set(value) != {"channel", "repositories"}:
            raise ValueError("target must contain exactly channel and repositories")
        try:
            channel = TargetChannel(value["channel"])
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid target channel") from exc
        repositories = value["repositories"]
        if not isinstance(repositories, list) or not repositories or any(not isinstance(item, str) or not _REPOSITORY_PATTERN.fullmatch(item) for item in repositories):
            raise ValueError("repositories must be non-empty owner/name strings")
        if len(set(repositories)) != len(repositories):
            raise ValueError("repositories must be unique")
        targets.append(PolicyTarget(target_id, channel, tuple(sorted(repositories))))
    management = None
    if "management_self_update" in document:
        value = document["management_self_update"]
        if not isinstance(value, dict) or set(value) - {"instance_id", "repository", "enabled"} or not {"instance_id", "repository"} <= set(value):
            raise ValueError("invalid management self-update configuration")
        instance_id, repository, enabled = value["instance_id"], value["repository"], value.get("enabled", False)
        if not isinstance(instance_id, str) or not _INSTANCE_PATTERN.fullmatch(instance_id):
            raise ValueError("invalid management instance ID")
        if not isinstance(repository, str) or not _REPOSITORY_PATTERN.fullmatch(repository) or type(enabled) is not bool:
            raise ValueError("invalid management self-update repository or opt-in")
        management = ManagementSelfUpdateConfig(instance_id, repository, enabled)
    return OperatingPolicy(profile, tuple(sorted(targets, key=lambda item: item.target_id)), "", management)


def evaluate_operating_policy(path: Path, intent: PolicyIntent) -> PolicyDecision:
    """Reload and evaluate the policy, so an old allowance is never reused."""
    loaded = load_operating_policy(path)
    if loaded.policy is None:
        return PolicyDecision(loaded.outcome, loaded.reason_code, loaded.revision, None, intent.target_id, None, intent.repository, _operation(intent.operation))
    return evaluate_snapshot(loaded.policy, intent)


def _operation(value: OperationClass | str) -> OperationClass | None:
    try:
        return OperationClass(value)
    except ValueError:
        return None


def _decision(policy: OperatingPolicy, intent: PolicyIntent, target: PolicyTarget | None, outcome: DecisionOutcome, reason: ReasonCode, operation: OperationClass | None) -> PolicyDecision:
    return PolicyDecision(outcome, reason, policy.revision, policy.profile, intent.target_id, target.channel if target else None, intent.repository, operation)


def evaluate_snapshot(policy: OperatingPolicy, intent: PolicyIntent) -> PolicyDecision:
    """Purely evaluate an intent against an immutable, validated snapshot."""
    targets = {item.target_id: item for item in policy.targets}
    target = targets.get(intent.target_id)
    operation = _operation(intent.operation)
    if operation is None:
        return _decision(policy, intent, target, DecisionOutcome.DENIED, ReasonCode.OPERATION_UNKNOWN, None)
    try:
        action = ActionClass(intent.action)
    except ValueError:
        return _decision(policy, intent, target, DecisionOutcome.DENIED, ReasonCode.ACTION_UNKNOWN, operation)
    if target is None:
        return _decision(policy, intent, None, DecisionOutcome.DENIED, ReasonCode.TARGET_UNKNOWN, operation)
    if intent.claimed_channel is not None and intent.claimed_channel != target.channel:
        return _decision(policy, intent, target, DecisionOutcome.DENIED, ReasonCode.CHANNEL_MISMATCH, operation)
    if intent.repository is not None and intent.repository not in target.repositories:
        return _decision(policy, intent, target, DecisionOutcome.DENIED, ReasonCode.REPOSITORY_MISMATCH, operation)
    if action is ActionClass.INSPECT:
        return _decision(policy, intent, target, DecisionOutcome.ALLOWED, ReasonCode.ALLOWED_INSPECTION, operation)
    if intent.repository is None:
        return _decision(policy, intent, target, DecisionOutcome.DENIED, ReasonCode.REPOSITORY_REQUIRED, operation)
    if operation is OperationClass.RECONCILIATION:
        return _decision(policy, intent, target, DecisionOutcome.DENIED, ReasonCode.RECONCILIATION_NOT_AUTHORIZED, operation)
    if policy.profile is PolicyProfile.OBSERVE:
        return _decision(policy, intent, target, DecisionOutcome.DENIED, ReasonCode.PROFILE_DENIES_MUTATION, operation)
    if action is ActionClass.RELEASE_PROMOTION:
        if target.channel is not TargetChannel.RELEASE:
            return _decision(policy, intent, target, DecisionOutcome.DENIED, ReasonCode.PROMOTION_REQUIRES_RELEASE, operation)
        if policy.profile is PolicyProfile.FULL:
            return _decision(policy, intent, target, DecisionOutcome.ALLOWED, ReasonCode.ALLOWED_RELEASE_PROMOTION, operation)
        return _decision(policy, intent, target, DecisionOutcome.DENIED, ReasonCode.PROFILE_DENIES_MUTATION, operation)
    if policy.profile is PolicyProfile.BETA and target.channel is TargetChannel.RELEASE:
        return _decision(policy, intent, target, DecisionOutcome.DENIED, ReasonCode.CHANNEL_DENIES_MUTATION, operation)
    reason = ReasonCode.ALLOWED_BETA_MUTATION if policy.profile is PolicyProfile.BETA else ReasonCode.ALLOWED_FULL_MUTATION
    return _decision(policy, intent, target, DecisionOutcome.ALLOWED, reason, operation)
