"""Durable local smoke evidence shares its authenticated operation's journal."""

from __future__ import annotations

import copy
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from auto_coder.management.operation_store import ManagementOperationStore
from auto_coder.management.operations import AcceptedAuthority, ActionParameter, AdmissionStatus, ManagedTarget, OperationIntent, OperationPhase, ReadStatus


def _authority() -> AcceptedAuthority:
    return AcceptedAuthority("issuer", "resource", "project", "operator", "client", "original-grant", ("read", "accept"), 10, 11)


def _intent(key: str = "smoke-key") -> OperationIntent:
    return OperationIntent(ManagedTarget("beta-instance", "beta", "owner/repo"), "accept_beta_runtime", (ActionParameter("source_sha", "a" * 40),), key)


def _snapshot() -> dict[str, object]:
    return {
        "schema_version": 1,
        "binding": {"source_sha": "a" * 40, "intent_fingerprint": "original", "authority": {"trusted_grant_id": "original-grant", "accepted_at": 11}},
        "profile": {"profile_id": "beta-runtime-smoke-v1", "revision": "profile-content-id", "interval": 1},
        "window": {"started_at": 100, "deadline": 110},
        "capture": {"selection_oid": "b" * 40, "runtime_id": "installed-a", "process_run_id": "worker-a"},
        "samples": [{"sequence": 1, "observed_at": 100}],
        "result": None,
    }


def _admitted(tmp_path: Path) -> tuple[ManagementOperationStore, str]:
    store = ManagementOperationStore(tmp_path)
    result = store.admit(_intent(), _authority())
    assert result.status is AdmissionStatus.CREATED and result.record is not None
    return store, result.record.operation_id


def test_smoke_reconstruction_keeps_operation_authority_and_lane(tmp_path: Path) -> None:
    store, operation_id = _admitted(tmp_path)
    original = store.get(operation_id).record
    history = store.history(operation_id)
    snapshot = _snapshot()
    assert store.smoke_save(operation_id, -1, snapshot)
    reopened = ManagementOperationStore(tmp_path)
    assert reopened.smoke_snapshot(operation_id) == (0, snapshot)
    assert reopened.get(operation_id).record == original
    assert reopened.history(operation_id) == history
    assert len(reopened.list_operations().records) == 1
    assert reopened.admit(_intent("competing-key"), _authority()).status is AdmissionStatus.BUSY
    assert reopened.admit(_intent(), _authority()).record == original
    assert not reopened.smoke_save("not-an-operation", -1, snapshot)
    assert reopened.smoke_snapshot("not-an-operation") is None

    completed = copy.deepcopy(snapshot)
    completed["samples"] = [*snapshot["samples"], {"sequence": 2, "observed_at": 102}]
    completed["result"] = {"local_result": "PASS", "evidence_id": "exact-local-content-id"}
    assert reopened.smoke_save(operation_id, 0, completed)
    assert reopened.smoke_snapshot(operation_id) == (1, completed)
    # Local PASS neither releases the acceptance lane nor publishes acceptance.
    assert reopened.get(operation_id).record == original
    assert reopened.admit(_intent("competing-key"), _authority()).status is AdmissionStatus.BUSY
    assert original is not None and original.phase is OperationPhase.ACCEPTED


@pytest.mark.parametrize("field", ["binding", "profile", "window", "capture", "samples"])
def test_original_binding_window_capture_and_samples_cannot_be_replaced(tmp_path: Path, field: str) -> None:
    store, operation_id = _admitted(tmp_path)
    original = _snapshot()
    assert store.smoke_save(operation_id, -1, original)
    replacement = copy.deepcopy(original)
    replacement[field] = [] if field == "samples" else {"replacement": True}
    assert not store.smoke_save(operation_id, 0, replacement)
    assert ManagementOperationStore(tmp_path).smoke_snapshot(operation_id) == (0, original)


def test_capture_is_set_once_and_samples_only_extend_original_prefix(tmp_path: Path) -> None:
    store, operation_id = _admitted(tmp_path)
    initial = _snapshot()
    initial.update(capture=None, samples=[])
    assert store.smoke_save(operation_id, -1, initial)
    captured = _snapshot()
    assert store.smoke_save(operation_id, 0, captured)
    changed_first_sample = copy.deepcopy(captured)
    changed_first_sample["samples"] = [{"sequence": 2, "observed_at": 100}, {"sequence": 3, "observed_at": 102}]
    assert not store.smoke_save(operation_id, 1, changed_first_sample)
    assert not store.smoke_save(operation_id, 1, initial)
    appended = copy.deepcopy(captured)
    appended["samples"] = [*captured["samples"], {"sequence": 2, "observed_at": 102}]
    assert not store.smoke_save(operation_id, 0, appended)
    assert store.smoke_save(operation_id, 1, appended)
    assert store.smoke_snapshot(operation_id) == (2, appended)


def test_terminal_snapshot_is_immutable_and_identical_replay_does_not_bump_revision(tmp_path: Path) -> None:
    store, operation_id = _admitted(tmp_path)
    original = _snapshot()
    original["result"] = {"local_result": "BLOCKED", "evidence_id": "immutable-result", "completed_at": 110}
    assert store.smoke_save(operation_id, -1, original)
    assert store.smoke_save(operation_id, -1, copy.deepcopy(original))
    assert store.smoke_save(operation_id, 0, copy.deepcopy(original))
    assert store.smoke_snapshot(operation_id) == (0, original)
    for field, replacement in (
        ("result", {"local_result": "PASS", "evidence_id": "replacement"}),
        ("result", None),
        ("reason", "new-reason"),
        ("samples", [*original["samples"], {"sequence": 99, "observed_at": 101}]),
    ):
        changed = copy.deepcopy(original)
        changed[field] = replacement
        assert not store.smoke_save(operation_id, 0, changed)
    assert ManagementOperationStore(tmp_path).smoke_snapshot(operation_id) == (0, original)


def test_independent_connections_cas_compete_for_same_sample_revision(tmp_path: Path) -> None:
    store, operation_id = _admitted(tmp_path)
    original = _snapshot()
    assert store.smoke_save(operation_id, -1, original)
    barrier = threading.Barrier(2)

    class ContendingStore(ManagementOperationStore):
        def _before_smoke_transaction(self) -> None:
            barrier.wait(timeout=10)

    stores = (ContendingStore(tmp_path), ContendingStore(tmp_path))
    proposals = []
    for sequence in (2, 3):
        proposal = copy.deepcopy(original)
        proposal["samples"] = [*original["samples"], {"sequence": sequence, "observed_at": 102}]
        proposals.append(proposal)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(candidate.smoke_save, operation_id, 0, proposal) for candidate, proposal in zip(stores, proposals)]
        results = [future.result(timeout=15) for future in futures]
    assert sorted(results) == [False, True]
    winning = proposals[results.index(True)]
    assert ManagementOperationStore(tmp_path).smoke_snapshot(operation_id) == (1, winning)
    assert len(store.list_operations().records) == 1


def test_failed_sqlite_write_preserves_original_snapshot_and_lane(tmp_path: Path) -> None:
    store, operation_id = _admitted(tmp_path)
    original = _snapshot()
    assert store.smoke_save(operation_id, -1, original)
    with sqlite3.connect(tmp_path / "management-operations.sqlite3") as connection:
        connection.execute("CREATE TRIGGER reject_smoke_update BEFORE UPDATE ON beta_runtime_smoke BEGIN SELECT RAISE(ABORT, 'controlled durable write failure'); END")
    replacement = copy.deepcopy(original)
    replacement["result"] = {"local_result": "PASS", "evidence_id": "not-persisted"}
    with pytest.raises(sqlite3.DatabaseError, match="controlled durable write failure"):
        store.smoke_save(operation_id, 0, replacement)
    reopened = ManagementOperationStore(tmp_path)
    assert reopened.smoke_snapshot(operation_id) == (0, original)
    assert reopened.admit(_intent("competitor"), _authority()).status is AdmissionStatus.BUSY


def test_lost_commit_acknowledgement_requires_exact_durable_readback(tmp_path: Path) -> None:
    store, operation_id = _admitted(tmp_path)
    completed = _snapshot()
    completed["result"] = {"local_result": "PASS", "evidence_id": "committed-before-response-loss"}

    class LostAcknowledgementStore(ManagementOperationStore):
        def _after_smoke_commit(self) -> None:
            raise sqlite3.OperationalError("controlled commit acknowledgement loss")

    uncertain = LostAcknowledgementStore(tmp_path)
    with pytest.raises(sqlite3.OperationalError, match="acknowledgement loss"):
        uncertain.smoke_save(operation_id, -1, completed)
    assert ManagementOperationStore(tmp_path).smoke_snapshot(operation_id) == (0, completed)
    assert uncertain.smoke_save(operation_id, -1, completed)
    assert store.smoke_snapshot(operation_id) == (0, completed)
    assert len(store.list_operations().records) == 1


@pytest.mark.parametrize(
    "bad",
    [float("nan"), float("inf"), float("-inf"), "x" * (1024 * 1024)],
    ids=["nan", "inf", "neg-inf", "oversized-string"],
)
def test_nonfinite_or_unbounded_evidence_cannot_be_written(tmp_path: Path, bad: object) -> None:
    store, operation_id = _admitted(tmp_path)
    invalid = _snapshot()
    invalid["invalid_evidence"] = bad
    with pytest.raises(ValueError):
        store.smoke_save(operation_id, -1, invalid)
    assert store.smoke_snapshot(operation_id) is None
    assert store.get(operation_id).status is ReadStatus.FOUND


@pytest.mark.parametrize("corrupt", ['{"schema_version":999}', '{"schema_version":1,"value":NaN}', "[1,2]", "not-json"])
def test_corrupt_snapshot_cannot_be_read_as_empty_or_overwritten(tmp_path: Path, corrupt: str) -> None:
    store, operation_id = _admitted(tmp_path)
    original = _snapshot()
    assert store.smoke_save(operation_id, -1, original)
    with sqlite3.connect(tmp_path / "management-operations.sqlite3") as connection:
        connection.execute("UPDATE beta_runtime_smoke SET snapshot_json=? WHERE operation_id=?", (corrupt, operation_id))
    with pytest.raises(ValueError):
        store.smoke_snapshot(operation_id)
    with pytest.raises(ValueError):
        store.smoke_save(operation_id, 0, original)
    with sqlite3.connect(tmp_path / "management-operations.sqlite3") as connection:
        assert connection.execute("SELECT snapshot_json FROM beta_runtime_smoke WHERE operation_id=?", (operation_id,)).fetchone()[0] == corrupt


def test_nonfinite_stored_number_is_rejected_even_in_valid_shape(tmp_path: Path) -> None:
    store, operation_id = _admitted(tmp_path)
    original = _snapshot()
    assert store.smoke_save(operation_id, -1, original)
    original["clock"] = float("nan")
    with sqlite3.connect(tmp_path / "management-operations.sqlite3") as connection:
        connection.execute("UPDATE beta_runtime_smoke SET snapshot_json=? WHERE operation_id=?", (json.dumps(original), operation_id))
    with pytest.raises(ValueError):
        store.smoke_snapshot(operation_id)
