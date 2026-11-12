"""Strict offline safety contracts, not fabricated end-to-end producer PASS."""

import asyncio
import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import mcp
import mcp.client.streamable_http
import pytest
from scripts import dot_mcp_acceptance as accept
from scripts import management_mcp_rollout_smoke as smoke

from auto_coder.management.mcp_runtime_tools import ManagementRuntime


def fixture():
    return accept.Fixture(
        schema=1,
        fixture_nonce="a" * 32,
        firebase_project_id="fixture-test-123",
        repository="kitamura-tetsuo/auto-coder-test",
        fixture_id="pr2835-test-abc",
        compose_project="dot-pr2835-test-abc",
        subnet="10.249.99.0/24",
        endpoint="http://127.0.0.1:8081/mcp",
        endpoint_namespace="management-container-only",
        target_ids={"beta": "dot-pr2835-test-abc-beta", "release": "dot-pr2835-test-abc-release"},
        mutation_policy="observe until separately configured and granted",
        docker_authority_in_worker_or_management=False,
        source_fixture_sha="b" * 40,
        source_fixture_available=True,
    )


def checkpoint(**kwargs):
    return replace(
        smoke.DotCheckpoint(
            schema=1,
            stage="beta",
            task_id="dot-pr2835",
            repository="kitamura-tetsuo/auto-coder",
            issue_number="2608",
            stage_revision="operator-v1",
            source_sha="b" * 40,
            ci_run_id="43120903270",
            ci_run_attempt="1",
            tested_source_ref="refs/tags/auto-coder-tested-" + "b" * 40,
            tested_source_oid="c" * 40,
            beta_target="dot-pr2835-test-abc-beta",
            idempotency_key="stable-original-reevaluation",
        ),
        **kwargs,
    )


def registry(fixture):
    return {
        "workers": [
            {
                "target_id": fixture.target_ids[role],
                "supervisor_instance_id": fixture.target_ids[role],
                "channel": role,
                "repository": accept.BETA_WORK_REPOSITORY if role == "beta" else fixture.repository,
                "bridge_address": "10.249.99." + ("11" if role == "beta" else "12"),
                "bridge_port": 8843,
                "bridge_secret_file": f"/fixture/secrets/{role}-bridge",
            }
            for role in ("beta", "release")
        ]
    }


def test_no_manifest_default_does_no_network_and_returns_not_run(tmp_path):
    result = subprocess.run(
        [sys.executable, str(Path(accept.__file__))],
        capture_output=True,
        text=True,
        env={**os.environ, "XDG_STATE_HOME": str(tmp_path), "AUTO_CODER_MCP_ACCESS_TOKEN": "unusable-token"},
        timeout=20,
    )
    assert result.returncode == 3
    output = json.loads(result.stdout.splitlines()[-1])
    assert output["verdict"] == "NOT_RUN"
    assert output["effect_dispatched"] is False
    assert output["positive_deployment_proven"] is False
    assert (tmp_path / "auto-coder" / "dot-mcp-acceptance.log").is_file()


def test_fixture_manifest_is_strict_and_refuses_nonfixture_targets(tmp_path):
    raw = asdict(fixture())
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert accept.Fixture.load(path) == fixture()
    for changed in (
        {**raw, "repository": "kitamura-tetsuo/auto-coder"},
        {**raw, "target_ids": {"beta": "beta-worker", "release": "release-worker"}},
        {**raw, "endpoint": "http://127.0.0.1:8081/mcp?port=9999"},
        {**raw, "docker_authority_in_worker_or_management": True},
        {**raw, "fixture_nonce": "not-a-nonce"},
        {**raw, "firebase_project_id": "auto-coder-production"},
        {**raw, "source_fixture_sha": "current HEAD"},
        {**raw, "nonsecret_but_unknown": "unexpected"},
    ):
        path.write_text(json.dumps(changed), encoding="utf-8")
        with pytest.raises(accept.Blocked):
            accept.Fixture.load(path)


def test_bound_fixture_requires_nonce_registry_and_no_docker_socket(tmp_path):
    expected = fixture()
    environ = {"DOT_DISPOSABLE_FIXTURE_NONCE": expected.fixture_nonce, "AUTO_CODER_WORKER_REGISTRY": json.dumps(registry(expected))}
    absent_socket = tmp_path / "not-a-docker-socket"
    accept.bound_local_fixture(expected, environ=environ, docker_socket=absent_socket)
    with pytest.raises(accept.Blocked, match="exact disposable"):
        accept.bound_local_fixture(expected, environ={**environ, "DOT_DISPOSABLE_FIXTURE_NONCE": "different"}, docker_socket=absent_socket)
    with pytest.raises(accept.Blocked, match="Docker"):
        accept.bound_local_fixture(expected, environ={**environ, "DOCKER_HOST": "tcp://host:2375"}, docker_socket=absent_socket)
    changed = registry(expected)
    changed["workers"][0]["repository"] = "kitamura-tetsuo/auto-coder"
    with pytest.raises(accept.Blocked, match="registry"):
        accept.bound_local_fixture(expected, environ={**environ, "AUTO_CODER_WORKER_REGISTRY": json.dumps(changed)}, docker_socket=absent_socket)


@pytest.mark.parametrize("stage", ["observe", "beta", "full"])
def test_observe_never_confers_mutation_authority(stage):
    original = checkpoint(stage=stage)
    with pytest.raises(accept.Blocked):
        accept.original_stage(original, "reevaluate", stage, "operator-v1", mutation=False)
    if stage == "observe":
        with pytest.raises(accept.Blocked, match="Observe stage"):
            accept.original_stage(original, "reevaluate", stage, "operator-v1", mutation=True)


@pytest.mark.parametrize("selected_revision", ["", "another-revision"])
def test_mutation_requires_current_original_operator_stage_revision(selected_revision):
    with pytest.raises(accept.Blocked):
        accept.original_stage(checkpoint(), "reevaluate", "beta", selected_revision, mutation=True)


@pytest.mark.parametrize("state", ["SENT_UNKNOWN", "NEEDS_ATTENTION"])
def test_unresolved_original_effect_never_allows_new_mutation(state):
    original = checkpoint(beta_operation_id="original-deploy", pending_operation_id="original-deploy", pending_effect_state=state)
    with pytest.raises(accept.Blocked, match="original unknown effect"):
        accept.original_stage(original, "reevaluate", "beta", "operator-v1", mutation=True)


def test_ticket_is_persisted_before_send_and_refuses_replay_or_other_action_same_key(tmp_path):
    path = tmp_path / "dot-fixture-reevaluate-original-send.json"
    original = checkpoint()
    args = {"target_id": fixture().target_ids["beta"], "idempotency_key": original.idempotency_key, "number": 14}
    accept.persist_original_ticket(path, fixture(), original, "auto_coder_reevaluate_target", args)
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["effect_state"] == "ADMISSION_UNKNOWN"
    assert record["operation_id"] == ""
    assert record["args"] == args
    assert "access_token" not in record
    assert path.stat().st_mode & 0o077 == 0
    with pytest.raises(accept.Blocked, match="previous admission"):
        accept.persist_original_ticket(path, fixture(), original, "auto_coder_reevaluate_target", args)
    with pytest.raises(accept.Blocked, match="cannot be reused"):
        accept.persist_original_ticket(tmp_path / "dot-fixture-beta-source-original-send.json", fixture(), original, "auto_coder_deploy_beta_source", args)
    accept.update_original_ticket(path, fixture(), "op-original-123", "ADMITTED")
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["operation_id"] == "op-original-123" and saved["effect_state"] == "ADMITTED"
    accept.update_original_ticket(path, fixture(), "op-original-123", "SUCCEEDED")
    with pytest.raises(accept.Blocked, match="regress"):
        accept.update_original_ticket(path, fixture(), "op-original-123", "ADMITTED")
    with pytest.raises(accept.Blocked, match="changed"):
        accept.update_original_ticket(path, fixture(), "op-other", "SUCCEEDED")


def test_unknown_effect_requires_only_saved_original_operation_id():
    original = checkpoint(beta_operation_id="op-original-123", pending_operation_id="op-original-123", pending_effect_state="SENT_UNKNOWN")
    assert smoke.read_plan(original, original.beta_target, "op-original-123")[1][0] == "auto_coder_operation_inspect"
    assert all(name != "auto_coder_operation_get" for name in accept.READS)


def test_container_running_and_heartbeat_not_available_never_pass():
    snapshot = {
        "status": "OK",
        "alive": True,
        "process_state": "RUNNING",
        "installed_binding_status": "UNAVAILABLE",
        "installed_runtime": None,
        "worker_observation": {"lifecycle": {"state": "running", "startup_reconciled": True, "startup_reconciliation_error": None, "daemon_serving": True}, "invocations": {"state": "running"}, "heartbeat": {"available": False, "sequence": None}},
    }
    with pytest.raises(accept.Blocked, match="unstaged"):
        accept.positive_heartbeat(snapshot)
    snapshot["installed_binding_status"] = "VERIFIED"
    snapshot["installed_runtime"] = {"source_sha": "b" * 40}
    with pytest.raises(accept.Blocked, match="heartbeat"):
        accept.positive_heartbeat(snapshot, source_sha="b" * 40)
    with pytest.raises(accept.Failed, match="contradicts"):
        accept.positive_heartbeat(snapshot, source_sha="a" * 40)


def _real_worker_sample(sequence, *, process_run_id="process-1", generation="deployment-1", supervisor="supervisor-1", source_sha="b" * 40):
    return {
        "status": "OK",
        "alive": True,
        "process_state": "RUNNING",
        "supervisor_generation": supervisor,
        "selected_runtime_id": "runtime-1",
        "worker": {
            "process_run_id": process_run_id,
            "deployment_generation": generation,
            "runtime_id": "runtime-1",
        },
        "installed_binding_status": "VERIFIED",
        "installed_runtime": {"source_sha": source_sha},
        "worker_observation": {
            "lifecycle": {"state": "running", "startup_reconciled": True, "startup_reconciliation_error": None, "daemon_serving": True},
            "invocations": {"state": "running"},
            "heartbeat": {"available": True, "sequence": sequence, "stage": "producer:maintenance-sleep"},
        },
    }


def test_heartbeats_advance_for_one_unchanged_original_worker():
    accept.require_advancing_worker_heartbeat(_real_worker_sample(12), _real_worker_sample(13), source_sha="b" * 40)


@pytest.mark.parametrize(
    "changed",
    [
        {"process_run_id": "process-2"},
        {"generation": "deployment-2"},
        {"supervisor": "supervisor-2"},
        {"source_sha": "a" * 40},
    ],
)
def test_restart_or_binding_switch_cannot_masquerade_as_worker_heartbeat_progress(changed):
    first = _real_worker_sample(12)
    second = _real_worker_sample(13, **changed)
    with pytest.raises((accept.Blocked, accept.Failed)):
        accept.require_advancing_worker_heartbeat(first, second, source_sha="b" * 40)


def test_repeated_or_unbound_heartbeat_cannot_prove_progress():
    with pytest.raises(accept.Blocked, match="advance"):
        accept.require_advancing_worker_heartbeat(_real_worker_sample(12), _real_worker_sample(12))
    unbound = _real_worker_sample(13, process_run_id="")
    with pytest.raises(accept.Blocked, match="identity"):
        accept.require_advancing_worker_heartbeat(_real_worker_sample(12), unbound)


def test_heartbeat_waits_for_slow_independent_producer_progress(monkeypatch):
    # Real idle workers commonly advance on a 60-second maintenance cycle.
    remaining = {"beta": [12, 12, 12, 13], "release": [21, 21, 22]}
    calls = []
    sentinel = object()

    async def read_target(live, target, channel, repository):
        assert live is sentinel
        assert target == fixture().target_ids[channel]
        assert repository == (fixture().repository if channel == "beta" else "fixture/" + fixture().fixture_id + "-release")
        values = remaining[channel]
        sequence = values.pop(0) if len(values) > 1 else values[0]
        calls.append((channel, sequence))
        return _real_worker_sample(sequence)

    monkeypatch.setattr(accept, "observed_target", read_target)
    asyncio.run(
        accept.await_advancing_worker_heartbeats(
            sentinel,
            [
                (fixture().target_ids["beta"], "beta", fixture().repository, _real_worker_sample(12), "b" * 40),
                (fixture().target_ids["release"], "release", "fixture/" + fixture().fixture_id + "-release", _real_worker_sample(21), ""),
            ],
            timeout_seconds=10,
            poll_seconds=0,
        )
    )
    assert ("beta", 13) in calls
    assert ("release", 22) in calls


def test_heartbeat_wait_is_bounded_and_stale_evidence_remains_blocked(monkeypatch):
    async def stale_target(live, target, channel, repository):
        return _real_worker_sample(12)

    monkeypatch.setattr(accept, "observed_target", stale_target)
    with pytest.raises(accept.Blocked, match="bounded observation window"):
        asyncio.run(
            accept.await_advancing_worker_heartbeats(
                object(),
                [(fixture().target_ids["beta"], "beta", fixture().repository, _real_worker_sample(12), "b" * 40)],
                timeout_seconds=0,
                poll_seconds=0,
            )
        )


def test_heartbeat_wait_rejects_restart_without_waiting(monkeypatch):
    async def restarted_target(live, target, channel, repository):
        return _real_worker_sample(13, process_run_id="process-restarted")

    monkeypatch.setattr(accept, "observed_target", restarted_target)
    with pytest.raises(accept.Blocked, match="identity"):
        asyncio.run(
            accept.await_advancing_worker_heartbeats(
                object(),
                [(fixture().target_ids["beta"], "beta", fixture().repository, _real_worker_sample(12), "b" * 40)],
                timeout_seconds=10,
                poll_seconds=0,
            )
        )


def test_heartbeat_wait_rechecks_earlier_worker_until_both_advance(monkeypatch):
    reads = {"beta": 0, "release": 0}

    async def read_target(live, target, channel, repository):
        reads[channel] += 1
        if channel == "beta":
            return _real_worker_sample(13, process_run_id="process-1" if reads[channel] == 1 else "process-restarted")
        return _real_worker_sample(21 if reads[channel] == 1 else 22)

    monkeypatch.setattr(accept, "observed_target", read_target)
    with pytest.raises(accept.Blocked, match="identity"):
        asyncio.run(
            accept.await_advancing_worker_heartbeats(
                object(),
                [
                    (fixture().target_ids["beta"], "beta", fixture().repository, _real_worker_sample(12), "b" * 40),
                    (fixture().target_ids["release"], "release", "fixture/" + fixture().fixture_id + "-release", _real_worker_sample(21), ""),
                ],
                timeout_seconds=10,
                poll_seconds=0,
            )
        )
    assert reads["beta"] == 2


def test_disposable_git_source_not_present_cannot_trigger_proof_network():
    with pytest.raises(accept.Blocked, match="fixture"):
        accept.verify_real_source(checkpoint(), replace(fixture(), source_fixture_available=False, source_fixture_sha=""))


def test_authenticated_identity_is_bound_to_fixture_resource_client_and_current_scopes():
    identity = {
        "project_id": fixture().firebase_project_id,
        "uid": "verified-owner",
        "client_id": "dot-disposable-operator",
        "resource": fixture().endpoint,
        "scopes": ["auto-coder.read", "auto-coder.operate", "auto-coder.deploy"],
    }
    accept.verify_current_operator(identity, fixture(), "reevaluate", mutation=True)
    accept.verify_current_operator(identity, fixture(), "beta-source", mutation=True)
    for change in (
        {"resource": "https://production.example/mcp"},
        {"client_id": "production-desktop"},
        {"scopes": ["auto-coder.read"]},
        {"uid": ""},
        {"project_id": "fixture-production-other"},
    ):
        with pytest.raises(accept.Blocked, match="OAuth"):
            accept.verify_current_operator({**identity, **change}, fixture(), "reevaluate", mutation=True)


def test_current_fixture_policy_requires_exactly_two_distinct_complete_target_bindings():
    selected = fixture()
    beta = {"target_id": selected.target_ids["beta"], "channel": "beta", "repository": accept.BETA_WORK_REPOSITORY}
    release = {"target_id": selected.target_ids["release"], "channel": "release", "repository": selected.repository}
    accept.verify_current_targets({"status": "OK", "targets": [beta, release]}, selected)
    accept.verify_current_targets({"status": "OK", "targets": [release, beta]}, selected)
    for invalid in (
        {"status": "UNAVAILABLE", "targets": [beta, release]},
        {"status": "OK", "targets": [beta]},
        {"status": "OK", "targets": [beta, release, beta]},
        {"status": "OK", "targets": [beta, release, "invalid"]},
        {"status": "OK", "targets": [beta, beta]},
        {"status": "OK", "targets": [beta, {**release, "repository": "production/worker"}]},
        {"status": "OK", "targets": [beta, {**release, "target_id": 123}]},
        {"status": "OK", "targets": [beta, {"target_id": selected.target_ids["release"], "channel": "release"}]},
        {"status": "OK", "targets": [beta, "invalid"]},
        {"status": "OK", "targets": None},
    ):
        with pytest.raises(accept.Blocked, match="current policy"):
            accept.verify_current_targets(invalid, selected)


def _confirmed_reevaluation_projection():
    """Match the real ManagementRuntime._project_operation response shape."""
    return {
        "status": "accepted_for_reevaluation",
        "operation_id": "original-req-001",
        "target_id": fixture().target_ids["beta"],
        "action": "reevaluate_target",
        "phase": "succeeded",
        "effect_state": "confirmed",
        "receipt": {
            "source": "worker-uds-invalidation-receipt",
            "result": json.dumps(
                {
                    "outcome": "accepted_for_reevaluation",
                    "generation": 3,
                    "disposition": "queued",
                    "accepting_process_run_id": "original-process-123",
                },
                sort_keys=True,
            ),
            "observed_at": 1794445200.0,
        },
    }


def test_real_reevaluation_journal_projection_is_recognized_without_issue_completion():
    assert accept.verified_reevaluation_receipt(
        _confirmed_reevaluation_projection(),
        operation_id="original-req-001",
        target_id=fixture().target_ids["beta"],
        process_run_id="original-process-123",
    )


def test_reevaluation_journal_only_accepts_original_confirmed_receipt():
    original = _confirmed_reevaluation_projection()
    for change in (
        {"operation_id": "other-operation"},
        {"target_id": fixture().target_ids["release"]},
        {"action": "deploy_beta_source"},
        {"status": "SUCCEEDED"},
        {"phase": "running"},
        {"effect_state": "sent_unknown"},
        {"receipt": None},
        {"receipt": {"source": "operator", "result": original["receipt"]["result"]}},
        {"receipt": {"source": original["receipt"]["source"], "result": "accepted_for_reevaluation"}},
        {"receipt": {"source": original["receipt"]["source"], "result": json.dumps({"outcome": "accepted_for_reevaluation", "generation": 1, "disposition": "queued", "accepting_process_run_id": "other-process"})}},
    ):
        with pytest.raises((accept.Blocked, accept.Failed)):
            accept.verified_reevaluation_receipt({**original, **change}, operation_id="original-req-001", target_id=fixture().target_ids["beta"], process_run_id="original-process-123")


def test_unsettled_reevaluation_does_not_fabricate_acceptance():
    pending = {**_confirmed_reevaluation_projection(), "status": "NEEDS_ATTENTION", "phase": "needs_attention", "effect_state": "sent_unknown", "receipt": None}
    assert not accept.verified_reevaluation_receipt(pending, operation_id="original-req-001", target_id=fixture().target_ids["beta"], process_run_id="original-process-123")


@pytest.mark.parametrize(
    "original_state,original_id,raises",
    [
        ("", "", ConnectionError),
        ("ADMISSION_UNKNOWN", "", ConnectionError),
        ("ADMITTED", "original-op-123", ConnectionError),
        ("SUCCEEDED", "original-op-123", RuntimeError),
        ("ADMITTED", "original-op-123", Exception),
        ("ADMITTED", "original-op-123", accept.Failed),
    ],
)
def test_cli_transport_exception_preserves_original_send_uncertainty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    original_state: str,
    original_id: str,
    raises: type[Exception],
) -> None:
    """Distinguish a pre-send refusal from a lost response after a durable send ticket."""
    test_fixture = fixture()
    original = checkpoint()
    ticket = tmp_path / "original-send.json"
    calls = 0

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "dot_mcp_acceptance.py",
            "--fixture-manifest",
            str(tmp_path / "fixture.json"),
            "--checkpoint",
            str(tmp_path / "checkpoint.json"),
            "--scenario",
            "reevaluate",
            "--enable-fixture-mutation",
            "--operator-stage",
            "beta",
            "--operator-stage-revision",
            "operator-v1",
        ],
    )
    monkeypatch.setattr(accept.Fixture, "load", staticmethod(lambda _path: test_fixture))
    monkeypatch.setattr(accept, "bound_local_fixture", lambda *_args: None)
    monkeypatch.setattr(accept.smoke, "load_checkpoint", lambda _path: original)
    monkeypatch.setattr(accept, "original_stage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(accept, "ticket_path", lambda _scenario: ticket)

    async def lost_response(*_args: object, **_kwargs: object) -> accept.Result:
        nonlocal calls
        calls += 1
        if original_state:
            arguments = {"target_id": test_fixture.target_ids["beta"], "idempotency_key": original.idempotency_key}
            accept.persist_original_ticket(ticket, test_fixture, original, "auto_coder_reevaluate_target", arguments)
            if original_id:
                accept.update_original_ticket(ticket, test_fixture, original_id, original_state)
        raise raises("Original producer response lost")

    monkeypatch.setattr(accept, "run_authenticated", lost_response)
    assert accept.main() == (1 if raises is accept.Failed else 2)
    result = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert result["verdict"] == ("FAIL" if raises is accept.Failed else "BLOCKED")
    assert result["effect_dispatched"] is (None if original_state else False)
    assert result["operation_id"] == original_id
    assert result["original_ticket_state"] == original_state
    assert calls == 1
    if original_state:
        assert json.loads(ticket.read_text())["effect_state"] == original_state


def test_corrupt_or_replaced_original_send_ticket_never_reports_no_dispatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "original-send.json"
    path.write_text("{unreadable", encoding="utf-8")
    monkeypatch.setattr(accept, "ticket_path", lambda _scenario: path)
    outcome = accept.exception_result("reevaluate", "Transport failed", mutation=True)
    assert outcome.effect_dispatched is None
    assert outcome.operation_id == ""
    assert outcome.original_ticket_state == "UNKNOWN"


def test_mcp_task_group_wrapper_preserves_single_trusted_blocker() -> None:
    blocked = accept.Blocked("Signed Supervisor cannot establish a live original worker")
    failed = accept.Failed("Installed source binding contradicts the exact tested Git commit")
    assert accept._single_nested_acceptance_error(ExceptionGroup("streamable HTTP", [ExceptionGroup("session", [blocked])])) is blocked
    assert accept._single_nested_acceptance_error(ExceptionGroup("streamable HTTP", [failed])) is failed
    assert accept._single_nested_acceptance_error(ExceptionGroup("streamable HTTP", [ValueError("unexpected transport failure")])) is None
    assert accept._single_nested_acceptance_error(ExceptionGroup("streamable HTTP", [blocked, RuntimeError("other failure")])) is None


@pytest.mark.parametrize("scenario", ["promote-source", "release-source"])
def test_full_actions_require_current_full_stage_without_fabricated_prior_promotion(scenario):
    original = checkpoint(release_target=fixture().target_ids["release"])
    with pytest.raises(accept.Blocked, match="full operator stage"):
        accept.original_stage(original, scenario, "beta", "operator-v1", mutation=True)
    full = replace(original, stage="full")
    # The first promotion has no promotion operation/catalog yet. Its real
    # prerequisite is beta acceptance, checked at the producer boundary.
    accept.original_stage(full, scenario, "full", "operator-v1", mutation=True)
    with pytest.raises(accept.Blocked, match="revision"):
        accept.original_stage(full, scenario, "full", "other-revision", mutation=True)


def test_unknown_ticket_blocks_a_new_key_on_the_same_target(tmp_path):
    first = tmp_path / "dot-fixture-reevaluate-original-send.json"
    original = checkpoint()
    args = {"target_id": fixture().target_ids["beta"], "idempotency_key": original.idempotency_key}
    accept.persist_original_ticket(first, fixture(), original, accept.MUTATIONS["reevaluate"], args)
    next_checkpoint = replace(original, idempotency_key="different-key")
    later = tmp_path / "dot-fixture-beta-source-original-send.json"
    with pytest.raises(accept.Blocked, match="changing the idempotency key"):
        accept.persist_original_ticket(later, fixture(), next_checkpoint, accept.MUTATIONS["beta-source"], {**args, "idempotency_key": "different-key"})
    assert not later.exists()
    accept.record_refused_ticket(first, fixture(), reason="profile_denies_mutation", journal_sha256="d" * 64)
    accept.persist_original_ticket(later, fixture(), next_checkpoint, accept.MUTATIONS["beta-source"], {**args, "idempotency_key": "different-key"})
    accept.update_original_ticket(later, fixture(), "admitted-original", "ADMITTED")
    with pytest.raises(accept.Blocked, match="admitted"):
        accept.record_refused_ticket(later, fixture(), reason="profile_denies_mutation", journal_sha256="d" * 64)


def _install_offline_mcp_session(monkeypatch, handler):
    """Controlled SDK boundary for unit regressions, never live E2E evidence."""
    calls = []
    tools = {"auto_coder_whoami", "auto_coder_targets", "auto_coder_deployment_status", "auto_coder_operation_inspect", "auto_coder_operation_get", "auto_coder_beta_acceptance_get", *accept.MUTATIONS.values()}

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def initialize(self):
            return None

        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in tools])

        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            if name == "auto_coder_whoami":
                value = {"project_id": fixture().firebase_project_id, "uid": "fixture-owner", "client_id": "dot-disposable-operator", "resource": fixture().endpoint, "scopes": ["auto-coder.read", "auto-coder.operate", "auto-coder.deploy"]}
            elif name == "auto_coder_targets":
                value = {"status": "OK", "targets": [{"target_id": target, "channel": role, "repository": accept.BETA_WORK_REPOSITORY if role == "beta" else accept.TEST_REPOSITORY} for role, target in fixture().target_ids.items()]}
            else:
                value = handler(name, arguments)
            return SimpleNamespace(isError=False, structuredContent=value)

    @asynccontextmanager
    async def transport(*_args, **_kwargs):
        yield None, None, None

    monkeypatch.setenv("AUTO_CODER_MCP_ACCESS_TOKEN", "offline-regression-placeholder")
    monkeypatch.setattr(mcp, "ClientSession", lambda *_args: Session())
    monkeypatch.setattr(mcp.client.streamable_http, "streamablehttp_client", transport)
    return calls


def test_original_release_journal_is_inspected_before_unavailable_beta(monkeypatch):
    original = checkpoint(stage="observe", release_target=fixture().target_ids["release"], release_operation_id="release-original", pending_operation_id="release-original", pending_effect_state="SENT_UNKNOWN")

    def handler(name, arguments):
        assert name == "auto_coder_operation_inspect"
        assert arguments == {"target_id": fixture().target_ids["release"], "operation_id": "release-original"}
        return {"operation_id": "release-original", "status": "NEEDS_ATTENTION"}

    calls = _install_offline_mcp_session(monkeypatch, handler)
    report = asyncio.run(accept.run_authenticated(fixture(), original, "inspect-original", mutate=False, item_number=0))
    assert report.verdict == "BLOCKED"
    assert report.operation_id == "release-original"
    assert [name for name, _ in calls] == ["auto_coder_whoami", "auto_coder_operation_inspect"]
    assert report.effect_dispatched is False


def test_explicit_original_resumption_never_repeats_admission(monkeypatch, tmp_path):
    original = checkpoint(idempotency_key="original-deploy-key")
    ticket = tmp_path / "dot-fixture-beta-source-original-send.json"
    monkeypatch.setattr(accept, "ticket_path", lambda _scenario: ticket)
    arguments = {"target_id": fixture().target_ids["beta"], "idempotency_key": original.idempotency_key, "selected_git_sha": original.source_sha}
    accept.persist_original_ticket(ticket, fixture(), original, accept.MUTATIONS["beta-source"], arguments)
    accept.update_original_ticket(ticket, fixture(), "deployment-original", "ADMITTED")
    sequence = 10

    def handler(name, args):
        nonlocal sequence
        assert name in {"auto_coder_operation_inspect", "auto_coder_deployment_status"}
        if name == "auto_coder_operation_inspect":
            return {"operation_id": "deployment-original", "target_id": fixture().target_ids["beta"], "action": "deploy_beta_source", "source_sha": original.source_sha, "status": "SUCCEEDED", "phase": "succeeded", "effect_state": "confirmed", "new_worker": _real_worker_sample(10)["worker"]}
        sequence += 1
        return {**_real_worker_sample(sequence), "target_id": fixture().target_ids["beta"], "instance_id": fixture().target_ids["beta"], "channel": "beta", "repository": accept.BETA_WORK_REPOSITORY}

    calls = _install_offline_mcp_session(monkeypatch, handler)
    report = asyncio.run(accept.run_authenticated(fixture(), original, "beta-source", mutate=True, item_number=0, resume_original=True))
    assert report.verdict == "PASS" and report.positive_deployment_proven is True
    assert report.operation_id == "deployment-original"
    assert all(name not in accept.MUTATIONS.values() and name != "auto_coder_operation_get" for name, _ in calls)
    assert calls[2] == ("auto_coder_operation_inspect", {"target_id": fixture().target_ids["beta"], "operation_id": "deployment-original"})
    persisted = json.loads(ticket.read_text())
    assert persisted["idempotency_key"] == original.idempotency_key
    assert persisted["effect_state"] == "SUCCEEDED"
    assert persisted["terminal_evidence"]["source_sha"] == original.source_sha


def test_unknown_admission_cannot_resume_or_change_intent(monkeypatch, tmp_path):
    original = checkpoint()
    ticket = tmp_path / "dot-fixture-beta-source-original-send.json"
    monkeypatch.setattr(accept, "ticket_path", lambda _scenario: ticket)
    arguments = {"target_id": fixture().target_ids["beta"], "idempotency_key": original.idempotency_key, "selected_git_sha": original.source_sha}
    accept.persist_original_ticket(ticket, fixture(), original, accept.MUTATIONS["beta-source"], arguments)
    with pytest.raises(accept.Blocked, match="lacks its operation ID"):
        accept.load_original_ticket(fixture(), original, "beta-source", resume=True)
    accept.update_original_ticket(ticket, fixture(), "original-source", "ADMITTED")
    for changed in (replace(original, idempotency_key="another-key"), replace(original, stage_revision="another-revision"), replace(original, source_sha="d" * 40), replace(original, pending_operation_id="another-operation")):
        with pytest.raises(accept.Blocked):
            accept.load_original_ticket(fixture(), changed, "beta-source", resume=True)
    assert accept.load_original_ticket(fixture(), original, "beta-source", resume=True)["operation_id"] == "original-source"


def _producer_record(action, operation_id, target_id, receipt_source, result):
    from auto_coder.management.operations import AcceptedAuthority, ActionParameter, EffectAttempt, EffectReceipt, EffectState, ManagedTarget, OperationIntent, OperationPhase, OperationRecord

    intent = OperationIntent(ManagedTarget(target_id, "beta" if action == "run_beta_acceptance" else "release", accept.BETA_WORK_REPOSITORY if action == "run_beta_acceptance" else accept.TEST_REPOSITORY), action, (ActionParameter("target_id", target_id),), "original-key", expected_artifact="b" * 40)
    authority = AcceptedAuthority("http://127.0.0.1:8081", fixture().endpoint, fixture().firebase_project_id, "fixture-owner", "dot-disposable-operator", "original-grant", ("auto-coder.read", "auto-coder.deploy"), 1, 2)
    receipt = EffectReceipt(operation_id, receipt_source, 3, json.dumps(result, sort_keys=True, separators=(",", ":")))
    return OperationRecord(operation_id, intent, intent.fingerprint(), authority, OperationPhase.SUCCEEDED, 3, effect=EffectAttempt(state=EffectState.CONFIRMED, correlation_identity=operation_id, receipt=receipt))


def test_beta_acceptance_consumes_actual_producer_projection_and_rejects_unpublished_local_pass():
    original = "beta-acceptance-original"
    record = _producer_record("run_beta_acceptance", original, fixture().target_ids["beta"], "github-source-acceptance-readback", {"status": "PASS_PUBLISHED", "tag_ref": "refs/tags/auto-coder-beta-acceptance-" + original, "tag_object_oid": "c" * 40, "direct_commit_sha": "b" * 40})
    runtime = object.__new__(ManagementRuntime)
    runtime.beta_acceptance = None
    projected = runtime._project_beta_acceptance(record)
    result = accept.verified_acceptance_receipt(projected, operation_id=original, target_id=fixture().target_ids["beta"], source_sha="b" * 40)
    assert result["acceptance_oid"] == "c" * 40
    for change in ({"status": "PASS"}, {"effect_state": "sent_unknown"}, {"receipt": None}, {"operation_id": "different-original"}, {"target_id": fixture().target_ids["release"]}):
        with pytest.raises((accept.Blocked, accept.Failed)):
            accept.verified_acceptance_receipt({**projected, **change}, operation_id=original, target_id=fixture().target_ids["beta"], source_sha="b" * 40)


def _producer_promotion_projection():
    from auto_coder.management.source_release_dispatch import SourceReleaseDispatch

    original = "promotion-original"
    run = {"id": 42, "run_attempt": 1, "workflow_id": 100}
    record = _producer_record("promote_accepted_source_release", original, fixture().target_ids["release"], "github-source-release-readback", {"outcome": "SOURCE_PROMOTION_CONFIRMED", "run_id": 42, "run_attempt": 1, "catalog_oid": "d" * 40, "source_sha": "b" * 40, "worker_deployed": False})
    runtime = SimpleNamespace(_project_operation=ManagementRuntime._project_operation, settings=None, journal=SimpleNamespace(source_release_run=lambda _original: run))
    return SourceReleaseDispatch(runtime).project(record)


def test_promotion_producer_receipt_never_proves_worker_deployment():
    projected = _producer_promotion_projection()
    result = accept.verified_promotion_receipt(projected, operation_id="promotion-original", target_id=fixture().target_ids["release"], source_sha="b" * 40)
    assert result["release_worker_deployed"] is False
    assert result["promotion_run_id"] == 42
    for change in ({"source_promotion": "COMPLETED_BUT_SUPERSEDED"}, {"worker_deployed": True}, {"workflow_run": {"id": 43, "run_attempt": 1}}, {"effect_state": "sent_unknown"}):
        with pytest.raises((accept.Blocked, accept.Failed)):
            accept.verified_promotion_receipt({**projected, **change}, operation_id="promotion-original", target_id=fixture().target_ids["release"], source_sha="b" * 40)


def test_effectful_original_query_is_denied_by_default_and_bound_to_one_operation():
    calls = []

    class Session:
        async def call_tool(self, name, args):
            calls.append((name, args))
            return SimpleNamespace(isError=False, structuredContent={"status": "NEEDS_ATTENTION"})

    live = accept.Live(Session(), {"auto_coder_operation_get"})
    args = {"target_id": fixture().target_ids["release"], "operation_id": "promotion-original"}
    with pytest.raises((accept.Blocked, ValueError)):
        asyncio.run(live.call("auto_coder_operation_get", args))
    with pytest.raises(accept.Blocked):
        asyncio.run(live.call("auto_coder_operation_get", args, mutation=True))
    live.authorize_original_reconciliation(args["target_id"], args["operation_id"])
    with pytest.raises(accept.Blocked):
        asyncio.run(live.call("auto_coder_operation_get", {**args, "operation_id": "another-operation"}, mutation=True))
    asyncio.run(live.call("auto_coder_operation_get", args, mutation=True))
    assert calls == [("auto_coder_operation_get", args)]


@pytest.mark.parametrize("status", ["CONFLICT", "BUSY", "DENIED", "INVALID_REQUEST", "UNAVAILABLE"])
def test_admission_refusal_cannot_adopt_an_occupying_operation(monkeypatch, tmp_path, status):
    original = checkpoint()
    ticket = tmp_path / "dot-fixture-reevaluate-original-send.json"
    monkeypatch.setattr(accept, "ticket_path", lambda _scenario: ticket)

    async def prepare(*_args):
        return {"target_id": fixture().target_ids["beta"], "idempotency_key": original.idempotency_key, "target_type": "issue", "number": 14, "expected_process_run_id": "original-worker", "expected_deployment_generation": "original-generation"}

    monkeypatch.setattr(accept, "prepare_arguments", prepare)

    def handler(name, _arguments):
        assert name == "auto_coder_reevaluate_target"
        # The production admission handlers use this actual response helper
        # for both conflict and occupied-lane replies, sometimes with an ID.
        return ManagementRuntime._response(status, "journal_" + status.lower(), operation_id="occupying-operation")

    calls = _install_offline_mcp_session(monkeypatch, handler)
    with pytest.raises(accept.Blocked, match="Admission was refused"):
        asyncio.run(accept.run_authenticated(fixture(), original, "reevaluate", mutate=True, item_number=14))
    record = json.loads(ticket.read_text())
    assert record["effect_state"] == "ADMISSION_UNKNOWN"
    assert record["operation_id"] == ""
    assert [name for name, _ in calls] == ["auto_coder_whoami", "auto_coder_targets", "auto_coder_reevaluate_target"]


@pytest.mark.parametrize("observed_source", ["a" * 40, "b" * 40])
def test_release_activation_needs_current_installed_source_after_promotion(monkeypatch, tmp_path, observed_source):
    from auto_coder.management.worker_lifecycle import WorkerLifecycle

    original = checkpoint(stage="full", release_target=fixture().target_ids["release"], idempotency_key="original-release-key")
    ticket = tmp_path / "dot-fixture-release-source-original-send.json"
    monkeypatch.setattr(accept, "ticket_path", lambda _scenario: ticket)
    arguments = {
        "target_id": fixture().target_ids["release"],
        "idempotency_key": original.idempotency_key,
        "selected_git_sha": original.source_sha,
        "promotion_operation_id": "promotion-original",
        "catalog_ref": "refs/tags/auto-coder-source-release-promotion-original",
        "catalog_oid": "d" * 40,
        "acceptance_ref": "refs/tags/auto-coder-beta-acceptance-beta-original",
        "acceptance_oid": "e" * 40,
    }
    accept.persist_original_ticket(ticket, fixture(), original, accept.MUTATIONS["release-source"], arguments)
    accept.update_original_ticket(ticket, fixture(), "release-original", "ADMITTED")
    record = _producer_record("deploy_release_source", "release-original", fixture().target_ids["release"], "supervisor-and-worker-lifecycle-journal", "source_deployed")
    runtime = SimpleNamespace(_params=ManagementRuntime._params)
    lifecycle = WorkerLifecycle(runtime)
    identity = {key: arguments[key] for key in ("promotion_operation_id", "catalog_ref", "catalog_oid", "acceptance_ref", "acceptance_oid")}
    projected = lifecycle.project_snapshot(record, {"stage": "SUCCEEDED", "source_sha": original.source_sha, "new_worker": _real_worker_sample(1)["worker"], "release_identity": {**identity, "source_sha": original.source_sha}})
    sequence = 10

    async def promoted(*_args, **_kwargs):
        return {**identity, "release_worker_deployed": False}

    monkeypatch.setattr(accept, "promotion_evidence", promoted)

    def handler(name, args):
        nonlocal sequence
        if name == "auto_coder_operation_inspect":
            return projected
        assert name == "auto_coder_deployment_status"
        sequence += 1
        target = fixture().target_ids["release"]
        return {**_real_worker_sample(sequence, source_sha=observed_source), "target_id": target, "instance_id": target, "channel": "release", "repository": accept.TEST_REPOSITORY}

    calls = _install_offline_mcp_session(monkeypatch, handler)
    if observed_source != original.source_sha:
        with pytest.raises(accept.Failed, match="Installed source binding"):
            asyncio.run(accept.run_authenticated(fixture(), original, "release-source", mutate=True, item_number=0, resume_original=True))
        assert json.loads(ticket.read_text())["effect_state"] == "ADMITTED"
    else:
        report = asyncio.run(accept.run_authenticated(fixture(), original, "release-source", mutate=True, item_number=0, resume_original=True))
        assert report.verdict == "PASS"
        assert report.positive_deployment_proven is True
        assert report.details["release_worker_deployed"] is True
        assert json.loads(ticket.read_text())["effect_state"] == "SUCCEEDED"
    assert all(name not in accept.MUTATIONS.values() for name, _ in calls)


def test_lifecycle_success_without_confirmed_effect_remains_blocked():
    original = checkpoint()
    value = {"operation_id": "original-beta", "target_id": fixture().target_ids["beta"], "action": "deploy_beta_source", "source_sha": original.source_sha, "status": "SUCCEEDED", "phase": "succeeded"}
    for state in (None, "not_sent", "sent_unknown"):
        with pytest.raises(accept.Blocked, match="confirmed terminal"):
            accept.verified_deployment({**value, "effect_state": state}, fixture(), original, "beta-source", "original-beta")
    accept.verified_deployment({**value, "effect_state": "confirmed"}, fixture(), original, "beta-source", "original-beta")


def test_pending_effect_cannot_be_ignored_by_worker_only_observe():
    original = checkpoint(beta_operation_id="original-deploy", pending_operation_id="original-deploy", pending_effect_state="SENT_UNKNOWN")
    with pytest.raises(accept.Blocked, match="Inspect the original unresolved"):
        accept.original_stage(original, "observe", "", "", mutation=False)
    accept.original_stage(original, "inspect-original", "", "", mutation=False)


@pytest.mark.parametrize(
    "section,changed",
    [
        ("lifecycle", {"state": "draining"}),
        ("lifecycle", {"daemon_serving": False}),
        ("lifecycle", {"startup_reconciliation_error": "reconciliation_failed"}),
        ("lifecycle", {"startup_failure": {"event_id": "original-failure"}}),
        ("invocations", {"state": "closing"}),
        ("heartbeat", {"stage": "bootstrap-ready"}),
        ("heartbeat", {"sequence": 0}),
    ],
)
def test_advancing_counter_cannot_mask_nonserving_or_nonproducer_worker(section, changed):
    before, after = _real_worker_sample(10), _real_worker_sample(11)
    after["worker_observation"][section].update(changed)
    with pytest.raises(accept.Blocked):
        accept.require_advancing_worker_heartbeat(before, after, source_sha="b" * 40)


def test_original_promotion_run_and_catalog_cannot_be_replaced_by_readback():
    evidence = accept.verified_promotion_receipt(_producer_promotion_projection(), operation_id="promotion-original", target_id=fixture().target_ids["release"], source_sha="b" * 40)
    original = checkpoint(stage="full", release_target=fixture().target_ids["release"], promotion_operation_id="promotion-original", promotion_run_id="42", promotion_catalog_ref=str(evidence["catalog_ref"]), promotion_catalog_oid=str(evidence["catalog_oid"]))
    accept.confirm_saved_promotion(original, evidence)
    for changed in (replace(original, promotion_run_id="43"), replace(original, promotion_operation_id="other-promotion"), replace(original, promotion_catalog_oid="e" * 40), replace(original, promotion_catalog_ref="")):
        with pytest.raises(accept.Failed):
            accept.confirm_saved_promotion(changed, evidence)


def _retained_actual_exhaustion(tmp_path):
    """Produce and retain a real terminal journal outcome, without live claims."""
    from scripts import dot_mcp_negative_acceptance as negative

    from tests.test_dot_mcp_negative_acceptance import ProjectedOriginal, confirmed_prior_runtime_recovery

    record, snapshot = confirmed_prior_runtime_recovery(fixture(), tmp_path)
    original = checkpoint(beta_operation_id=record.operation_id, pending_operation_id=record.operation_id, pending_effect_state="NEEDS_ATTENTION")
    producer = ProjectedOriginal(record, snapshot)
    result = asyncio.run(negative.original_scenario(producer, fixture(), original, "repeated-failure", root=tmp_path))
    assert result.negative_boundary_verified is True and result.verdict == "BLOCKED"
    producer.calls.clear()
    return record, producer, tmp_path / f"dot-recovery-{result.failure_signature}.json"


def test_terminal_exhaustion_without_send_ticket_blocks_new_checkpoint_and_key(monkeypatch, tmp_path):
    """The real producer releases FAILED lanes; the retained client budget does not."""
    from auto_coder.management.operation_store import ManagementOperationStore
    from auto_coder.management.operations import AdmissionStatus

    record, producer, ledger = _retained_actual_exhaustion(tmp_path)
    original_bytes = ledger.read_bytes()
    journal = ManagementOperationStore(tmp_path / "journal")
    assert journal.list_operations(outstanding_only=True, target=record.intent.target).records == ()
    fresh = checkpoint(idempotency_key="new-checkpoint-and-key", stage_revision="operator-v2")
    ticket = tmp_path / "dot-fixture-beta-source-original-send.json"
    monkeypatch.setattr(accept, "ticket_path", lambda _scenario: ticket)

    def handler(name, arguments):
        assert name == "auto_coder_operation_inspect"
        assert arguments == {"target_id": fixture().target_ids["beta"], "operation_id": record.operation_id}
        return producer.producer.project_snapshot(record, producer.snapshot)

    calls = _install_offline_mcp_session(monkeypatch, handler)
    with pytest.raises(accept.Blocked, match="exhausted the recovery budget"):
        asyncio.run(accept.run_authenticated(fixture(), fresh, "beta-source", mutate=True, item_number=0))
    assert [name for name, _ in calls] == ["auto_coder_whoami", "auto_coder_targets", "auto_coder_operation_inspect"]
    assert not ticket.exists()
    assert ledger.read_bytes() == original_bytes
    # This counterfactual admission occurs only in the isolated offline journal.
    # It demonstrates why the server's lane check alone cannot enforce REQ-004.
    counterfactual = journal.admit(replace(record.intent, idempotency_key=fresh.idempotency_key), record.authority)
    assert counterfactual.status is AdmissionStatus.CREATED
    assert counterfactual.record.operation_id != record.operation_id


@pytest.mark.parametrize("changed", ["action", "target", "source"])
def test_retained_exhaustion_does_not_fence_an_unrelated_identity(tmp_path, changed):
    _, producer, ledger = _retained_actual_exhaustion(tmp_path)
    before = ledger.read_bytes()
    selected = checkpoint(source_sha="f" * 40) if changed == "source" else checkpoint()
    action = accept.MUTATIONS["beta-acceptance" if changed == "action" else "beta-source"]
    target = fixture().target_ids["release" if changed == "target" else "beta"]
    asyncio.run(accept.check_retained_recovery(producer, fixture(), selected, action, target, root=tmp_path))
    assert producer.calls == [] and ledger.read_bytes() == before


@pytest.mark.parametrize(
    ("changed", "message"),
    [
        ({"status": "UNAVAILABLE"}, "recovery outcome changed"),
        ({"operation_id": "another-operation"}, "cannot be independently verified"),
        ({"source_sha": "f" * 40}, "original source"),
        ({"recovery_attempts": 0}, "recovery outcome changed"),
        ({"effect_state": "sent_unknown"}, "no confirmed original producer outcome"),
    ],
)
def test_retained_exhaustion_requires_current_exact_original_evidence(tmp_path, changed, message):
    record, producer, ledger = _retained_actual_exhaustion(tmp_path)
    before = ledger.read_bytes()
    calls = []

    async def read(name, arguments, *, mutation=False):
        assert name == "auto_coder_operation_inspect" and mutation is False
        calls.append(arguments)
        return {**producer.producer.project_snapshot(record, producer.snapshot), **changed}

    with pytest.raises(accept.Blocked, match=message):
        asyncio.run(accept.check_retained_recovery(SimpleNamespace(call=read), fixture(), checkpoint(), accept.MUTATIONS["beta-source"], fixture().target_ids["beta"], root=tmp_path))
    assert calls == [{"target_id": fixture().target_ids["beta"], "operation_id": record.operation_id}]
    assert ledger.read_bytes() == before


def test_changed_retained_failure_signature_cannot_authorize_new_admission(tmp_path):
    _, producer, ledger = _retained_actual_exhaustion(tmp_path)
    value = json.loads(ledger.read_text())
    value["signature"] = "0" * 64
    ledger.write_text(json.dumps(value))
    with pytest.raises(accept.Blocked, match="changed or is incomplete"):
        asyncio.run(accept.check_retained_recovery(producer, fixture(), checkpoint(), accept.MUTATIONS["beta-source"], fixture().target_ids["beta"], root=tmp_path))
    assert producer.calls == []


def test_retained_exhaustion_keeps_exact_original_inspection_and_resumption(tmp_path, monkeypatch):
    record, producer, ledger = _retained_actual_exhaustion(tmp_path)
    before = ledger.read_bytes()
    saved = checkpoint(idempotency_key=record.intent.idempotency_key, beta_operation_id=record.operation_id, pending_operation_id=record.operation_id, pending_effect_state="NEEDS_ATTENTION", recovery_attempts=1)
    ticket = tmp_path / "dot-fixture-beta-source-original-send.json"
    monkeypatch.setattr(accept, "ticket_path", lambda _scenario: ticket)

    def handler(name, arguments):
        assert name == "auto_coder_operation_inspect"
        assert arguments == {"target_id": fixture().target_ids["beta"], "operation_id": record.operation_id}
        return producer.producer.project_snapshot(record, producer.snapshot)

    calls = _install_offline_mcp_session(monkeypatch, handler)
    inspected = asyncio.run(accept.run_authenticated(fixture(), saved, "inspect-original", mutate=False, item_number=0))
    assert inspected.verdict == "BLOCKED" and inspected.operation_id == record.operation_id
    assert [name for name, _ in calls] == ["auto_coder_whoami", "auto_coder_operation_inspect"]
    arguments = {"target_id": fixture().target_ids["beta"], "idempotency_key": saved.idempotency_key, "selected_git_sha": saved.source_sha}
    accept.persist_original_ticket(ticket, fixture(), saved, accept.MUTATIONS["beta-source"], arguments)
    accept.update_original_ticket(ticket, fixture(), record.operation_id, "ADMITTED")
    calls.clear()
    with pytest.raises(accept.Blocked, match="Original operation failed"):
        asyncio.run(accept.run_authenticated(fixture(), saved, "beta-source", mutate=True, item_number=0, resume_original=True))
    assert [name for name, _ in calls] == ["auto_coder_whoami", "auto_coder_targets", "auto_coder_operation_inspect"]
    assert ledger.read_bytes() == before
    assert json.loads(ticket.read_text())["operation_id"] == record.operation_id


def test_concurrent_distinct_action_tickets_cannot_both_admit_one_target(monkeypatch, tmp_path):
    """Two independent fixture clients must not bypass the same-target original fence."""
    import concurrent.futures
    import threading

    selected_fixture = fixture()
    beta = selected_fixture.target_ids["beta"]
    first_seen = threading.Event()
    second_seen = threading.Event()
    real_open = accept.os.open

    def simultaneous_original_ticket_open(path, flags, mode=0o777, **kwargs):
        if str(path).endswith("-original-send.json") and flags & os.O_CREAT:
            if not first_seen.is_set():
                first_seen.set()
                second_seen.wait(timeout=0.6)
            else:
                second_seen.set()
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(accept.os, "open", simultaneous_original_ticket_open)

    def reserve(scenario, key):
        path = tmp_path / f"dot-fixture-{scenario}-original-send.json"
        source = checkpoint(idempotency_key=key)
        try:
            accept.persist_original_ticket(
                path,
                selected_fixture,
                source,
                accept.MUTATIONS[scenario],
                {"target_id": beta, "idempotency_key": key},
            )
        except accept.Blocked:
            return "BLOCKED"
        return "RESERVED"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(reserve, "reevaluate", "first-original-key")
        b = pool.submit(reserve, "beta-source", "second-original-key")
        outcomes = [a.result(timeout=5), b.result(timeout=5)]

    assert sorted(outcomes) == ["BLOCKED", "RESERVED"]
    assert len(list(tmp_path.glob("dot-fixture-*-original-send.json"))) == 1


@pytest.mark.parametrize("invalid_lock", ["symlink", "group-writable"])
def test_original_admission_refuses_replaced_or_unprotected_lock(tmp_path, invalid_lock):
    """An unsafe original-admission guard cannot authorize a new effect."""
    lock = tmp_path / ".dot-fixture-original-send.lock"
    if invalid_lock == "symlink":
        target = tmp_path / "replacement"
        target.write_text("")
        lock.symlink_to(target)
    else:
        lock.write_text("")
        lock.chmod(0o660)

    path = tmp_path / "dot-fixture-reevaluate-original-send.json"
    with pytest.raises(accept.Blocked, match="admission lock"):
        accept.persist_original_ticket(
            path,
            fixture(),
            checkpoint(),
            accept.MUTATIONS["reevaluate"],
            {"target_id": fixture().target_ids["beta"], "idempotency_key": "original-key"},
        )
    assert not path.exists()


def test_distinct_target_original_intents_remain_independently_admissible(tmp_path):
    """The fixture lock must not classify two physical targets as one effect."""
    selected = fixture()
    for role, scenario in (("beta", "reevaluate"), ("release", "release-source")):
        path = tmp_path / f"dot-fixture-{scenario}-original-send.json"
        accept.persist_original_ticket(
            path,
            selected,
            checkpoint(idempotency_key=f"original-key-{role}"),
            accept.MUTATIONS[scenario],
            {"target_id": selected.target_ids[role], "idempotency_key": f"original-key-{role}"},
        )
        assert path.is_file()
