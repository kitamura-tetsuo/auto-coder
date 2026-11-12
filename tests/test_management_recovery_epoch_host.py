"""Authorization gates and acceptance oracles for the real recovery VM driver.

These are component tests of the driver, never substitutes for its native VM
run. The separate workflow runs the driver against genuine Docker/systemd,
original stores, qualified source publication, READY and peer custody.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from scripts import test_management_recovery_epoch_host as fixture

from auto_coder.management.original_attempts import canonical
from tests.test_management_archived_cutover_container import Generation


def _inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    source = tmp_path / "source"
    source.mkdir()
    token = tmp_path / "token"
    token.write_text("component-token-placeholder")
    for name in ("AUTOCODER_FRESH_TEST_BASE_IMAGE", "AUTOCODER_RECOVERY_CADDY_IMAGE"):
        monkeypatch.setenv(name, "sha256:" + "a" * 64)
    return {"authorization_id": "test-authorization", "source_repository": source, "source_token_file": token, "source_sha": "b" * 40, "uv": Path(sys.executable).resolve(), "uv_version": "0.13.6", "output": tmp_path / "result.json"}


def test_invalid_source_stops_before_host_authorization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inputs = _inputs(tmp_path, monkeypatch)
    inputs["source_sha"] = "main"
    monkeypatch.setattr(fixture, "require_disposable_host", lambda _: pytest.fail("host authorization must not be reached"))
    with pytest.raises(ValueError, match="exact qualified source"):
        fixture.run_fixture(**inputs)
    assert not inputs["output"].exists()


def test_unmatched_marker_stops_before_fixture_provisioning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inputs = _inputs(tmp_path, monkeypatch)
    created = []

    def refusal(_):
        raise PermissionError("the actual original marker does not select this host")

    monkeypatch.setattr(fixture, "require_disposable_host", refusal)
    monkeypatch.setattr(fixture, "_private_directory", lambda path: created.append(path))
    monkeypatch.setattr(fixture.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("unauthorized driver attempted a host effect"))
    with pytest.raises(PermissionError, match="original marker"):
        fixture.run_fixture(**inputs)
    assert created == [] and not inputs["output"].exists()


def test_unique_fixture_leaf_fits_the_actual_original_socket_paths() -> None:
    first, second = fixture._fixture_directory(), fixture._fixture_directory()
    assert first.parent == second.parent == fixture.FIXTURE_ROOT and first != second
    assert len(first.name) == 16 and int(first.name, 16) >= 0
    for name in ("A", "B", "C", "D"):
        assert len(os.fsencode(first / "deployment" / name / "guard" / "fresh-guard.sock")) <= 107
    assert len(os.fsencode(fixture.PEER_SOCKET)) <= 107


def test_oversized_socket_path_stops_before_any_original_provisioning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inputs = _inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(fixture, "require_disposable_host", lambda _: {})
    monkeypatch.setattr(fixture, "FIXTURE_ROOT", tmp_path / ("long-original-root-" * 8))
    monkeypatch.setattr(fixture, "_private_directory", lambda _: pytest.fail("oversized socket path reached original provisioning"))
    monkeypatch.setattr(fixture.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("oversized socket path reached a native effect"))
    with pytest.raises(ValueError, match="Unix socket pathname exceeds"):
        fixture.run_fixture(**inputs)
    assert not inputs["output"].exists()


def test_mutable_image_tag_stops_before_host_authorization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inputs = _inputs(tmp_path, monkeypatch)
    monkeypatch.setenv("AUTOCODER_FRESH_TEST_BASE_IMAGE", "auto-coder:latest")
    monkeypatch.setattr(fixture, "require_disposable_host", lambda _: pytest.fail("host authorization must not be reached"))
    with pytest.raises(ValueError, match="exact already imported"):
        fixture.run_fixture(**inputs)


def test_linked_credential_stops_before_host_authorization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inputs = _inputs(tmp_path, monkeypatch)
    linked = tmp_path / "linked-token"
    linked.symlink_to(inputs["source_token_file"])
    inputs["source_token_file"] = linked
    monkeypatch.setattr(fixture, "require_disposable_host", lambda _: pytest.fail("host authorization must not be reached"))
    with pytest.raises(ValueError, match="canonical absolute"):
        fixture.run_fixture(**inputs)


def test_command_failure_exports_bounded_diagnostics_without_inputs_or_credentials() -> None:
    error = subprocess.CalledProcessError(
        1,
        ["docker", "compose", "--private-command-value"],
        output="Network selected Created\nError response from daemon: AppArmor profile could not be loaded\nAuthorization: Bearer secret-value\n",
        stderr="stderr boundary\n" + "safe line\n" * 100,
    )
    result = fixture._command_failure(error)
    assert result["type"] == "CalledProcessError" and result["returncode"] == 1
    assert len(result["diagnostics"]) <= 60
    assert "private-command-value" not in json.dumps(result) and "secret-value" not in json.dumps(result)
    short = fixture._command_failure(subprocess.CalledProcessError(1, ["docker", "compose"], output=b"Error response from daemon: AppArmor profile could not be loaded\n"))
    assert "AppArmor profile could not be loaded" in "".join(short["diagnostics"])
    assert fixture._command_failure(ValueError("unrelated error")) is None


def test_no_child_or_admission_oracle_rejects_any_native_store(tmp_path: Path) -> None:
    state, journal = tmp_path / "state", tmp_path / "journal"
    state.mkdir()
    journal.mkdir()
    (state / ".supervisor-owner.lock").write_bytes(b"")
    (state / "logs").mkdir()
    generation = Generation("target", tmp_path, "a" * 64, [])
    fixture._no_target_child(generation, running=False)
    (state / "management-child.sqlite3").write_bytes(b"not allowed")
    with pytest.raises(AssertionError, match="native consumer state"):
        fixture._no_target_child(generation, running=False)
    (state / "management-child.sqlite3").unlink()
    (journal / "management-operations.sqlite3").write_bytes(b"not allowed")
    with pytest.raises(AssertionError, match="admitted an operation"):
        fixture._no_target_child(generation, running=False)


@pytest.mark.parametrize("inventory", ["a" * 64, "truncated-id", "\n".join(["b" * 64, "b" * 64])])
def test_removed_target_oracle_rejects_presence_or_incomplete_inventory(monkeypatch: pytest.MonkeyPatch, inventory: str) -> None:
    monkeypatch.setattr(fixture.Chain, "docker", lambda *_args: inventory)
    with pytest.raises(AssertionError):
        fixture._require_target_absent("a" * 64)


def test_removed_target_oracle_compares_exact_full_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def docker(*arguments):
        calls.append(arguments)
        return "a" * 63 + "b"

    monkeypatch.setattr(fixture.Chain, "docker", docker)
    fixture._require_target_absent("a" * 64)
    assert calls == [("ps", "--all", "--quiet", "--no-trunc")]


def test_original_fingerprint_binds_inodes_and_bytes(tmp_path: Path) -> None:
    path = tmp_path / "native-original"
    path.write_bytes(b"original")
    before = fixture._snapshot((tmp_path,))
    assert fixture._snapshot((tmp_path,)) == before
    other = tmp_path / "replacement"
    other.write_bytes(path.read_bytes())
    os.replace(other, path)
    assert fixture._snapshot((tmp_path,)) != before


def _workers(root: Path) -> None:
    for channel in ("beta", "release", "agent-table"):
        parent = root / channel / "supervisor"
        parent.mkdir(parents=True)
        (parent / "state.json").write_bytes(
            canonical({"schema_version": 1, "operations": {}, "selected_runtime_id": "bootstrap", "worker": {"pid": 2, "start_token": "1000", "process_run_id": channel + "-run", "deployment_generation": channel + "-deployment", "runtime_id": "bootstrap", "binding_observed_at": 1.0}})
        )
    for name in ("compose.yml", "beta-secret", "release-secret", "agent-table-secret"):
        (root / name).write_bytes(b"original-component-input")


def test_worker_claim_oracle_preserves_effects_and_excludes_only_observation_refresh(tmp_path: Path) -> None:
    _workers(tmp_path)
    baseline = fixture._worker_claims(tmp_path)
    path = tmp_path / "beta" / "supervisor" / "state.json"
    state = json.loads(path.read_bytes())
    state["worker"]["binding_observed_at"] = 2.0
    replacement = path.with_name("replacement")
    replacement.write_bytes(canonical(state))
    os.replace(replacement, path)
    assert fixture._worker_claims(tmp_path) == baseline
    state["operations"]["new-operation"] = {"status": "running"}
    path.write_bytes(canonical(state))
    assert fixture._worker_claims(tmp_path) != baseline


def test_worker_claim_oracle_requires_real_nonempty_ownership(tmp_path: Path) -> None:
    _workers(tmp_path)
    path = tmp_path / "beta" / "supervisor" / "state.json"
    state = json.loads(path.read_bytes())
    state["worker"]["process_run_id"] = None
    path.write_bytes(canonical(state))
    with pytest.raises(AssertionError, match="ownership evidence"):
        fixture._worker_claims(tmp_path)


def _durable(root: Path) -> tuple[str, str]:
    # Deliberately synthetic pure oracle input; the VM driver obtains these
    # records only from the actual production issuer and genuine peer logs.
    inventory = canonical({"component": "inventory"})
    (root / "inventory.json").write_bytes(inventory)
    inventory_sha = hashlib.sha256(inventory).hexdigest()
    evidence = canonical({"lineage": {"unavailable": [], "genesis": {"component": "origin"}}, "inspection": {"unresolved_targets": 1, "historical_keys": 3, "launch_authority": False}, "target": {"receipt": {"source_sha": "a" * 40}}}).decode()
    binding = {"evidence_json": evidence, "evidence_sha256": hashlib.sha256(evidence.encode()).hexdigest()}
    previous = ""
    for sequence, phase in enumerate(("reserved", "intent", "consumed", "receipt")):
        record = canonical({"schema_version": 1, "phase": phase, "sequence": sequence, "previous": previous, "inventory_sha256": inventory_sha, "binding": binding})
        (root / (phase + ".json")).write_bytes(record)
        head = canonical({"schema_version": 1, "phase": phase, "record_sha256": hashlib.sha256(record).hexdigest(), "previous": previous, "inventory_sha256": inventory_sha})
        (root / (phase + "-head.json")).write_bytes(head)
        previous = hashlib.sha256(head).hexdigest()
    return "One consumed recovery epoch grant send completed", "Recovery custody acknowledged"


def test_durable_oracle_requires_all_heads_and_actual_consumer_acknowledgment(tmp_path: Path) -> None:
    issuer, consumer = _durable(tmp_path)
    result = fixture.inspect_durable_result(tmp_path, issuer_log=issuer, consumer_log=consumer)
    assert result["unresolved_targets"] == 1 and result["historical_keys"] == 3
    with pytest.raises(AssertionError):
        fixture.inspect_durable_result(tmp_path, issuer_log=issuer, consumer_log="consumer waiting")
    (tmp_path / "receipt-head.json").unlink()
    with pytest.raises(FileNotFoundError):
        fixture.inspect_durable_result(tmp_path, issuer_log=issuer, consumer_log=consumer)


def test_durable_oracle_rejects_a_changed_original_record(tmp_path: Path) -> None:
    issuer, consumer = _durable(tmp_path)
    path = tmp_path / "consumed.json"
    value = json.loads(path.read_bytes())
    value["binding"]["evidence_sha256"] = "b" * 64
    path.write_bytes(canonical(value))
    with pytest.raises(AssertionError):
        fixture.inspect_durable_result(tmp_path, issuer_log=issuer, consumer_log=consumer)


def _result() -> dict[str, object]:
    return {
        "schema": fixture.RESULT_SCHEMA,
        "status": "passed",
        "cases": sorted(fixture.required_cases("grant")),
        "no_child_started": True,
        "preserved_originals": True,
        "preserved_workers": True,
        "original_outcomes": {"C": "launched", "D": "never-created"},
        "durable": {"record_sha256": {name: "a" * 64 for name in ("reserved", "intent", "consumed", "receipt")}},
        "peer_observations": {
            "schema": "actual-recovery-native-peer-faults-v1",
            "cases": [
                {
                    "case": case,
                    "issuer_returncode": 2,
                    "epoch_unreserved": True,
                    "issuer_endpoint_removed_by_original": True,
                    "consumer_grant_accepted": False,
                    "no_child_or_admission": True,
                    "issuer_syscall": {"bytes_observed": 10},
                    "receiver_alive_until_issuer_refusal": True,
                    "issuer_refused_before_receiver_termination": True,
                }
                for case in sorted(fixture.REQUIRED_PEER_CASES)
            ],
        },
        "factory_override_observations": {
            "schema": "actual-recovery-factory-overrides-v1",
            "cases": [{"case": case, "returncode": 2, "refusal": "component refusal", "alternate_inputs_sha256": "d" * 64, "epoch_sha256": "e" * 64} for case in sorted(fixture.FACTORY_OBSERVATION_CASES)],
        },
        "task_observations": {
            "schema": "actual-recovery-task-observations-v1",
            "cases": [
                {"case": "escaped-old-network-nonleader-refused"},
                {"case": "escaped-nonleader-current-containment-refused", "status": "unavailable"},
                {
                    "case": "measured-exited-nonleader-current-containment-reverified",
                    "status": "observed-current-recovery-containment",
                    "after_exit": {"pidfd_readable": True, "nonleader_absent": True, "leader": {"state": "Z", "network_namespace": None}},
                    "after_verification": {"pidfd_readable": True, "nonleader_absent": True, "leader": {"state": "Z", "network_namespace": None}},
                },
            ],
        },
    }


@pytest.mark.parametrize("case", sorted(fixture.required_cases("grant")))
def test_native_result_cannot_omit_any_required_case(case: str) -> None:
    result = _result()
    fixture.validate_result(result)
    result["cases"].remove(case)
    with pytest.raises(AssertionError):
        fixture.validate_result(result)


@pytest.mark.parametrize("field", ["no_child_started", "preserved_originals", "preserved_workers"])
def test_native_result_cannot_claim_success_without_preservation(field: str) -> None:
    result = _result()
    result[field] = False
    with pytest.raises(AssertionError):
        fixture.validate_result(result)


def _lost_reply_durable() -> dict[str, object]:
    lifetimes = {"target_pid": 321, "target_start_token": "12345", "issuer_pid": 654, "issuer_start_token": "54321"}
    grant = {"schema": fixture.GRANT_SCHEMA, "target_id": "a" * 64, "host_pid": lifetimes["issuer_pid"], "host_start_token": lifetimes["issuer_start_token"]}
    encoded = canonical(grant) + b"\n"
    return {
        "phase": "receipt",
        "consumer_grant_acknowledged": False,
        "grant_send_completed": True,
        "receiver_paused_before_send": True,
        "issuer_died_before_receiver_resumed": True,
        "native_observation": {**lifetimes, "issuer_death_verified_before_receiver_resumed": True},
        "queued_grant": {
            **lifetimes,
            "schema": "actual-recovery-queued-grant-v1",
            "grant_schema": fixture.GRANT_SCHEMA,
            "target_id": grant["target_id"],
            "grant": grant,
            "message_sha256": hashlib.sha256(encoded).hexdigest(),
            "bytes_observed": len(encoded),
            "socket_inode": 789,
            "socket_flags_before": 2048,
            "socket_flags_after": 2048,
            "peer_credentials": {"pid": lifetimes["issuer_pid"], "uid": 0, "gid": 0},
            "peek_only": True,
            "socket_flags_unchanged": True,
            "bytes_still_queued": True,
            "receiver_stopped": True,
            "issuer_alive": True,
        },
    }


@pytest.mark.parametrize("scenario,phase", [("kill-after-intent", "intent"), ("kill-after-consume", "consumed"), ("lost-reply", "receipt")])
def test_interruption_result_requires_the_actual_boundary_and_no_grant(scenario: str, phase: str) -> None:
    result = _result()
    durable = _lost_reply_durable() if scenario == "lost-reply" else {"phase": phase, "consumer_grant_acknowledged": False, "grant_send_completed": False}
    result.update(scenario=scenario, cases=sorted(fixture.required_cases(scenario)), durable=durable)
    fixture.validate_result(result)
    result["durable"]["consumer_grant_acknowledged"] = True
    with pytest.raises(AssertionError):
        fixture.validate_result(result)
    result["durable"] = {"phase": "reserved", "consumer_grant_acknowledged": False}
    with pytest.raises(AssertionError):
        fixture.validate_result(result)


def test_unknown_native_scenario_is_not_a_success_alias() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        fixture.required_cases("unreviewed-boundary")


def test_canonical_acceptance_contains_all_distinct_publication_and_late_faults() -> None:
    assert len(set(fixture.ACCEPTANCE_SCENARIOS)) == len(fixture.ACCEPTANCE_SCENARIOS)
    assert set(fixture.ACCEPTANCE_SCENARIOS) == {"grant", "lost-reply", "retarget-after-reservation", *fixture.BOUNDARY_SCENARIOS, *fixture.LATE_MUTATION_SCENARIOS}
    assert set(fixture.SCENARIOS) - set(fixture.ACCEPTANCE_SCENARIOS) == {"kill-after-intent", "kill-after-consume"}
    assert fixture._selected_boundary("kill-after-intent") == fixture._selected_boundary("after-intent-head") == ("intent-head.json", "after")
    assert fixture._selected_boundary("kill-after-consume") == fixture._selected_boundary("after-consumed-head") == ("consumed-head.json", "after")
    assert fixture._selected_boundary("lost-reply") == ("receipt-head.json", "after")


@pytest.mark.parametrize("field", ["grant_send_completed", "receiver_paused_before_send", "issuer_died_before_receiver_resumed"])
def test_lost_reply_requires_actual_send_before_issuer_death(field: str) -> None:
    result = _result()
    result.update(scenario="lost-reply", cases=sorted(fixture.required_cases("lost-reply")), durable=_lost_reply_durable())
    fixture.validate_result(result)
    result["durable"][field] = False
    with pytest.raises(AssertionError):
        fixture.validate_result(result)


@pytest.mark.parametrize("field", ["peek_only", "socket_flags_unchanged", "bytes_still_queued", "receiver_stopped", "issuer_alive"])
def test_lost_reply_requires_nonconsuming_current_kernel_observation(field: str) -> None:
    result = _result()
    result.update(scenario="lost-reply", cases=sorted(fixture.required_cases("lost-reply")), durable=_lost_reply_durable())
    fixture.validate_result(result)
    result["durable"]["queued_grant"][field] = False
    with pytest.raises(AssertionError):
        fixture.validate_result(result)


@pytest.mark.parametrize("mutation", ["missing", "foreign-peer", "foreign-lifetime", "changed-flags", "empty-queue", "issuer-still-alive", "changed-packet", "foreign-grant"])
def test_lost_reply_log_cannot_replace_exact_queued_bytes_and_lifetimes(mutation: str) -> None:
    result = _result()
    result.update(scenario="lost-reply", cases=sorted(fixture.required_cases("lost-reply")), durable=_lost_reply_durable())
    if mutation == "missing":
        result["durable"].pop("queued_grant")
    elif mutation == "foreign-peer":
        result["durable"]["queued_grant"]["peer_credentials"]["pid"] += 1
    elif mutation == "foreign-lifetime":
        result["durable"]["queued_grant"]["target_start_token"] = "99999"
    elif mutation == "changed-flags":
        result["durable"]["queued_grant"]["socket_flags_after"] += 1
    elif mutation == "empty-queue":
        result["durable"]["queued_grant"]["bytes_observed"] = 0
    elif mutation in {"changed-packet", "foreign-grant"}:
        queued = result["durable"]["queued_grant"]
        queued["grant"]["target_id"] = "b" * 64
        if mutation == "foreign-grant":
            encoded = canonical(queued["grant"]) + b"\n"
            queued.update(message_sha256=hashlib.sha256(encoded).hexdigest(), bytes_observed=len(encoded))
    else:
        result["durable"]["native_observation"]["issuer_death_verified_before_receiver_resumed"] = False
    with pytest.raises((AssertionError, KeyError)):
        fixture.validate_result(result)


@pytest.mark.parametrize("case", sorted(fixture.REQUIRED_PEER_CASES))
def test_grant_result_requires_the_actual_peer_evidence_inventory(case: str) -> None:
    result = _result()
    fixture.validate_result(result)
    result["peer_observations"]["cases"] = [item for item in result["peer_observations"]["cases"] if item["case"] != case]
    with pytest.raises(AssertionError):
        fixture.validate_result(result)


@pytest.mark.parametrize("field", ["receiver_alive_until_issuer_refusal", "issuer_refused_before_receiver_termination"])
@pytest.mark.parametrize("case", ["queued-rights-without-receipt-refused", "received-rights-without-receipt-refused"])
def test_missing_peer_receipt_cannot_be_masked_by_an_already_dead_receiver(case: str, field: str) -> None:
    result = _result()
    fixture.validate_result(result)
    selected = next(item for item in result["peer_observations"]["cases"] if item["case"] == case)
    selected[field] = False
    with pytest.raises(AssertionError):
        fixture.validate_result(result)


@pytest.mark.parametrize("name", ["factory_override_observations", "task_observations"])
def test_native_factory_and_task_measurements_are_required(name: str) -> None:
    result = _result()
    fixture.validate_result(result)
    result.pop(name)
    with pytest.raises(AssertionError):
        fixture.validate_result(result)


def test_exited_task_success_requires_the_same_observed_zombie() -> None:
    result = _result()
    fixture.validate_result(result)
    selected = next(item for item in result["task_observations"]["cases"] if item["case"] == "measured-exited-nonleader-current-containment-reverified")
    selected["after_verification"]["leader"]["state"] = "R"
    with pytest.raises(AssertionError):
        fixture.validate_result(result)


@pytest.mark.parametrize("scenario", fixture.LATE_MUTATION_SCENARIOS)
def test_late_mutation_result_names_its_deliberate_preservation_boundary(scenario: str) -> None:
    result = _result()
    result.update(
        scenario=scenario,
        cases=sorted(fixture.required_cases(scenario)),
        preservation_baseline="after-deliberate-fixture-mutation",
        preserved_originals_sha256="c" * 64,
        durable={"phase": "intent", "consumer_grant_acknowledged": False, "grant_send_completed": False, "mutation": {"scenario": scenario, "boundary": "after-intent-head", "preservation_baseline": "after-deliberate-fixture-mutation"}, "originals_after_injection_sha256": "c" * 64},
    )
    if scenario == "late-target-loss":
        result["durable"]["mutation"]["target_fault"] = {"pidfd_death_observed": True, "exact_docker_id_absent": True, "docker_rm_returncode": 0, "other_docker_ids_preserved": True}
    elif scenario == "late-original-attempt":
        result["durable"]["mutation"].update(
            original_attempt={"instance_id": "D", "outcome": "never-created", "terminal_kind": "never-created", "independent_lineage_verified": True, "retained_intent_lineage_matched": True, "original_file": {"path": "/component/history/00000007.json", "device": 1, "inode": 2, "sha256": "a" * 64}},
            file_fault={"path": "/component/history/00000007.json", "before": [1, 2, "a" * 64]},
        )
    fixture.validate_result(result)
    result["preservation_baseline"] = "before-deliberate-fixture-mutation"
    with pytest.raises(AssertionError):
        fixture.validate_result(result)
    result["preservation_baseline"] = "after-deliberate-fixture-mutation"
    result["durable"]["originals_after_injection_sha256"] = "d" * 64
    with pytest.raises(AssertionError):
        fixture.validate_result(result)


@pytest.mark.parametrize("field", ["pidfd_death_observed", "exact_docker_id_absent", "other_docker_ids_preserved"])
def test_late_target_loss_requires_death_absence_and_preserved_other_objects(field: str) -> None:
    scenario = "late-target-loss"
    result = _result()
    result.update(
        scenario=scenario,
        cases=sorted(fixture.required_cases(scenario)),
        preservation_baseline="after-deliberate-fixture-mutation",
        preserved_originals_sha256="c" * 64,
        durable={
            "phase": "intent",
            "consumer_grant_acknowledged": False,
            "grant_send_completed": False,
            "mutation": {"scenario": scenario, "boundary": "after-intent-head", "preservation_baseline": "after-deliberate-fixture-mutation", "target_fault": {"pidfd_death_observed": True, "exact_docker_id_absent": True, "docker_rm_returncode": 0, "other_docker_ids_preserved": True}},
            "originals_after_injection_sha256": "c" * 64,
        },
    )
    fixture.validate_result(result)
    result["durable"]["mutation"]["target_fault"][field] = False
    with pytest.raises(AssertionError):
        fixture.validate_result(result)


@pytest.mark.parametrize("scenario", fixture.BOUNDARY_SCENARIOS)
def test_publication_result_requires_exact_kernel_observation(scenario: str) -> None:
    publication, when = fixture.parse_boundary(scenario)
    result = _result()
    result.update(scenario=scenario, cases=sorted(fixture.required_cases(scenario)), durable={"publication": publication, "when": when, "consumer_grant_acknowledged": False, "file_fsync_observed": when == "after", "directory_fsync_observed": when == "after"})
    fixture.validate_result(result)
    result["durable"]["directory_fsync_observed"] = when != "after"
    with pytest.raises(AssertionError):
        fixture.validate_result(result)


def test_retarget_result_cannot_hide_unchanged_inventory() -> None:
    result = _result()
    result.update(scenario="retarget-after-reservation", cases=sorted(fixture.required_cases("retarget-after-reservation")), durable={"phase": "reserved", "consumer_grant_acknowledged": False, "inventory_sha256": "a" * 64, "retargeted_inventory_sha256": "a" * 64})
    with pytest.raises(AssertionError):
        fixture.validate_result(result)
    result["durable"]["retargeted_inventory_sha256"] = "b" * 64
    fixture.validate_result(result)


def test_semantic_preservation_excludes_only_deliberate_rename_metadata(tmp_path: Path) -> None:
    original = tmp_path / "original"
    original.mkdir()
    database = original / "database"
    database.write_bytes(b"retained bytes")
    baseline = fixture._snapshot((original,), semantic=True)
    held = tmp_path / "held-original"
    database.rename(held)
    assert fixture._snapshot((original,), semantic=True) != baseline
    held.rename(database)
    assert fixture._snapshot((original,), semantic=True) == baseline
    database.write_bytes(b"changed bytes")
    assert fixture._snapshot((original,), semantic=True) != baseline
