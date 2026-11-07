"""Target-identity and durable bridge-binding regression tests."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path

import pytest

from auto_coder.management.identity import FirebaseOperatorIdentity, IdentityConfig
from auto_coder.management.mcp_runtime_tools import ManagementRuntime, ResolvedWorker
from auto_coder.management.oauth_config import ManagementSettings, OAuthClient, OAuthSettings
from auto_coder.management.oauth_grants import OAuthGrantService
from auto_coder.management.operations import AcceptedAuthority, ActionParameter, AdmissionStatus, ManagedTarget, OperationIntent
from auto_coder.management.worker_network_client import WorkerEndpointConfig, WorkerNetworkClient, configured_worker_bridge_environment, configured_worker_registry
from auto_coder.operating_policy import ActionClass


def _configs() -> dict[str, WorkerEndpointConfig]:
    return {
        target_id: WorkerEndpointConfig(target_id, channel, repository, ip, 8843, secret, target_id)
        for target_id, channel, repository, ip, secret in (
            ("release-worker", "release", "fixture/release", "10.40.0.11", b"r" * 48),
            ("beta-worker", "beta", "fixture/outliner", "10.40.0.12", b"b" * 48),
            ("beta-agent-table-worker", "beta", "fixture/agent-table", "10.40.0.13", b"a" * 48),
        )
    }


def _runtime(tmp_path: Path, configs: dict[str, WorkerEndpointConfig]) -> ManagementRuntime:
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        "profile: full\ntargets:\n  release-worker:\n    channel: release\n    repositories: [fixture/release]\n  beta-worker:\n    channel: beta\n    repositories: [fixture/outliner]\n  beta-agent-table-worker:\n    channel: beta\n    repositories: [fixture/agent-table]\n",
        encoding="utf-8",
    )
    oauth = OAuthSettings(tmp_path / "oauth.sqlite3", "https://management.example", "fixture-project", (OAuthClient("client", ("https://management.example/callback",)),))
    settings = ManagementSettings(tmp_path, oauth, operating_policy_path=policy, operation_state_root=tmp_path / "journal")
    grants = OAuthGrantService(oauth, FirebaseOperatorIdentity(IdentityConfig("fixture-project", tmp_path / "allowed_uids.json")))
    return ManagementRuntime(settings, grants, {target_id: WorkerNetworkClient(config) for target_id, config in configs.items()})


def _authority() -> AcceptedAuthority:
    return AcceptedAuthority("https://management.example", "https://management.example/mcp", "fixture-project", "operator", "client", "original-grant", ("auto-coder:read", "auto-coder:deploy"), 100.0, 101.0)


def test_registry_rejects_duplicate_json_and_duplicate_machine_key_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secrets = []
    for index in range(3):
        path = tmp_path / f"secret-{index}"
        path.write_bytes(base64.b64encode(os.urandom(48)))
        secrets.append(path)
    entries = [
        {"target_id": target_id, "supervisor_instance_id": target_id, "channel": channel, "repository": repository, "bridge_address": ip, "bridge_port": 8843, "bridge_secret_file": str(secrets[index])}
        for index, (target_id, channel, repository, ip) in enumerate((("release-worker", "release", "fixture/release", "10.40.0.11"), ("beta-worker", "beta", "fixture/outliner", "10.40.0.12"), ("beta-agent-table-worker", "beta", "fixture/agent-table", "10.40.0.13")))
    ]
    monkeypatch.setenv("AUTO_CODER_WORKER_REGISTRY", json.dumps({"workers": entries}))
    assert set(configured_worker_registry()) == {entry["target_id"] for entry in entries}
    assert configured_worker_bridge_environment()["AUTO_CODER_WORKER_REGISTRY"] == os.environ["AUTO_CODER_WORKER_REGISTRY"]

    monkeypatch.setenv("AUTO_CODER_WORKER_REGISTRY", '{"workers":[],"workers":[]}')
    with pytest.raises(ValueError, match="duplicate"):
        configured_worker_registry()
    monkeypatch.setenv("AUTO_CODER_WORKER_REGISTRY", json.dumps({"workers": [entries[0], entries[0]]}))
    with pytest.raises(ValueError, match="distinct"):
        configured_worker_registry()
    monkeypatch.setenv("AUTO_CODER_WORKER_REGISTRY", json.dumps({"workers": [*entries[:2], {**entries[2], "repository": entries[1]["repository"]}]}))
    with pytest.raises(ValueError, match="distinct"):
        configured_worker_registry()
    secrets[2].write_bytes(secrets[1].read_bytes())
    monkeypatch.setenv("AUTO_CODER_WORKER_REGISTRY", json.dumps({"workers": entries}))
    with pytest.raises(ValueError, match="machine credentials"):
        configured_worker_registry()


def test_exact_policy_target_lookup_and_duplicate_repository_claims(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, _configs())
    outliner = runtime._resolve("beta-worker", ActionClass.INSPECT)
    agent = runtime._resolve("beta-agent-table-worker", ActionClass.INSPECT)
    release = runtime._resolve("release-worker", ActionClass.INSPECT)
    assert all(isinstance(target, ResolvedWorker) for target in (outliner, agent, release))
    assert isinstance(outliner, ResolvedWorker) and isinstance(agent, ResolvedWorker)
    assert outliner.client is not agent.client
    assert outliner.client.config.channel == agent.client.config.channel == "beta"
    assert runtime._resolve("missing-target", ActionClass.INSPECT)["reason"] == "target_unknown"  # type: ignore[index]
    assert set(runtime._worker_locks) == {"release-worker", "beta-worker", "beta-agent-table-worker"}

    policy = runtime.settings.operating_policy_path
    assert policy is not None
    original = policy.read_text(encoding="utf-8")
    policy.write_text(original.replace("repositories: [fixture/agent-table]", "repositories: [fixture/outliner]"), encoding="utf-8")
    assert runtime._resolve("beta-agent-table-worker", ActionClass.INSPECT)["reason"] == "target_configuration_mismatch"  # type: ignore[index]
    assert isinstance(runtime._resolve("beta-worker", ActionClass.INSPECT), ResolvedWorker)
    policy.write_text(original.replace("repositories: [fixture/agent-table]", "repositories: [fixture/unknown]"), encoding="utf-8")
    assert runtime._resolve("beta-agent-table-worker", ActionClass.INSPECT)["reason"] == "target_configuration_mismatch"  # type: ignore[index]
    assert isinstance(runtime._resolve("beta-worker", ActionClass.INSPECT), ResolvedWorker)


def test_original_bridge_binding_survives_restart_and_prevents_redirection(tmp_path: Path) -> None:
    configs = _configs()
    runtime = _runtime(tmp_path, configs)
    authority = _authority()
    agent = configs["beta-agent-table-worker"]
    outliner = configs["beta-worker"]

    def intent(config: WorkerEndpointConfig, *, with_binding: bool) -> OperationIntent:
        params = (ActionParameter("target_id", config.target_id),)
        if with_binding:
            params += (ActionParameter("worker_binding", config.binding_digest),)
        return OperationIntent(ManagedTarget(config.instance_id, config.channel, config.repository), "start_worker", params, "same-key")

    admitted_agent = runtime.journal.admit(intent(agent, with_binding=True), authority)
    admitted_outliner = runtime.journal.admit(intent(outliner, with_binding=True), authority)
    assert admitted_agent.status is admitted_outliner.status is AdmissionStatus.CREATED
    assert admitted_agent.record is not None and admitted_outliner.record is not None
    assert admitted_agent.record.operation_id != admitted_outliner.record.operation_id
    assert runtime._record_client(admitted_agent.record)[0] == "beta-agent-table-worker"  # type: ignore[index]
    assert runtime._record_client(admitted_outliner.record)[0] == "beta-worker"  # type: ignore[index]

    changed = dict(configs)
    changed["beta-agent-table-worker"] = WorkerEndpointConfig(agent.instance_id, agent.channel, agent.repository, "10.40.0.19", 8843, b"z" * 48, agent.target_id)
    replacement = _runtime(tmp_path, changed)
    assert replacement._record_client(admitted_agent.record) is None
    assert replacement._record_client(admitted_outliner.record) is not None
    assert replacement._same_original_intent(admitted_agent.record, intent(changed["beta-agent-table-worker"], with_binding=True)) is False
    assert replacement.journal.find_by_key(intent(changed["beta-agent-table-worker"], with_binding=True), authority).record == admitted_agent.record


def test_pre_registry_operations_pin_migration_time_bridge_across_restarts(tmp_path: Path) -> None:
    configs = _configs()
    runtime = _runtime(tmp_path, configs)
    config = configs["beta-worker"]
    legacy = OperationIntent(ManagedTarget(config.instance_id, config.channel, config.repository), "start_worker", (ActionParameter("target_id", "beta-worker"),), "old-key")
    admitted = runtime.journal.admit(legacy, _authority())
    assert admitted.record is not None
    assert runtime._record_client(admitted.record) is not None
    new_intent = OperationIntent(legacy.target, legacy.action, (*legacy.parameters, ActionParameter("worker_binding", config.binding_digest)), legacy.idempotency_key)
    assert runtime._same_original_intent(admitted.record, new_intent)

    next_config = WorkerEndpointConfig(config.instance_id, config.channel, config.repository, "10.40.0.27", config.port, config.secret, config.target_id)
    changed = dict(configs, **{"beta-worker": next_config})
    restarted = _runtime(tmp_path, changed)
    assert restarted._record_client(admitted.record) is None
    assert restarted._same_original_intent(admitted.record, OperationIntent(legacy.target, legacy.action, (*legacy.parameters, ActionParameter("worker_binding", next_config.binding_digest)), legacy.idempotency_key))


def test_monitor_submits_each_independent_target_even_when_another_is_unavailable(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, _configs())
    authority = _authority()
    admitted = []
    for target_id, config in _configs().items():
        intent = OperationIntent(ManagedTarget(config.instance_id, config.channel, config.repository), "start_worker", (ActionParameter("target_id", target_id), ActionParameter("worker_binding", config.binding_digest)), target_id)
        result = runtime.journal.admit(intent, authority)
        assert result.record is not None
        admitted.append(result.record.operation_id)
    queued: list[str] = []

    async def submit(operation_id: str) -> None:
        queued.append(operation_id)

    runtime.dispatcher.submit = submit  # type: ignore[method-assign]
    asyncio.run(runtime._poll_lifecycle_once())
    assert set(queued) == set(admitted)
