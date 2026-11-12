"""Strict offline safety contracts, not fabricated end-to-end producer PASS."""

import json
import os
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from scripts import dot_mcp_acceptance as accept
from scripts import management_mcp_rollout_smoke as smoke


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
                "repository": fixture.repository if role == "beta" else "fixture/" + fixture.fixture_id + "-release",
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
        "worker_observation": {"lifecycle": {"startup_reconciled": True}, "heartbeat": {"available": False, "sequence": None}},
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
            "lifecycle": {"startup_reconciled": True},
            "heartbeat": {"available": True, "sequence": sequence},
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
