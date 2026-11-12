"""Authorization gates and acceptance oracles for the real recovery VM driver.

These are component tests of the driver, never substitutes for its native VM
run. The separate workflow runs the driver against genuine Docker/systemd,
original stores, qualified source publication, READY and peer custody.
"""

from __future__ import annotations

import hashlib
import json
import os
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
        "cases": sorted(fixture.REQUIRED_CASES),
        "no_child_started": True,
        "preserved_originals": True,
        "preserved_workers": True,
        "original_outcomes": {"C": "launched", "D": "never-created"},
        "durable": {"record_sha256": {name: "a" * 64 for name in ("reserved", "intent", "consumed", "receipt")}},
    }


@pytest.mark.parametrize("case", sorted(fixture.REQUIRED_CASES))
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


@pytest.mark.parametrize("scenario,phase", [("kill-after-intent", "intent"), ("kill-after-consume", "consumed"), ("lost-reply", "receipt")])
def test_interruption_result_requires_the_actual_boundary_and_no_grant(scenario: str, phase: str) -> None:
    result = _result()
    result.update(scenario=scenario, cases=sorted(fixture.required_cases(scenario)), durable={"phase": phase, "consumer_grant_acknowledged": False})
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
