import shutil
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from auto_coder.management.operation_store import ManagementOperationStore
from auto_coder.management.operations import (
    AcceptedAuthority,
    ActionParameter,
    AdmissionStatus,
    EffectReceipt,
    EffectState,
    ManagedTarget,
    MutationStatus,
    OperationIntent,
    OperationPhase,
    QueryStatus,
    ReadStatus,
)


def authority(later: float = 1, scopes: tuple[str, ...] = ("manage",)) -> AcceptedAuthority:
    return AcceptedAuthority("issuer", "resource", "project", "uid", "client", "grant", scopes, later, later)


def intent(key: str = "key-1", digest: str = "one", channel: str = "stable") -> OperationIntent:
    return OperationIntent(ManagedTarget("instance", channel, "owner/repo"), "restart", (ActionParameter("digest", digest),), key, "incarnation")


def test_replay_conflict_and_original_authority_survive_restart(tmp_path: Path) -> None:
    first = ManagementOperationStore(tmp_path).admit(intent(), authority())
    assert first.status is AdmissionStatus.CREATED and first.record
    replay = ManagementOperationStore(tmp_path).admit(intent(), authority(99, ("manage", "admin")))
    assert replay.status is AdmissionStatus.REPLAY
    assert replay.record == first.record
    assert ManagementOperationStore(tmp_path).admit(intent(digest="two"), authority()).status is AdmissionStatus.CONFLICT
    assert len(ManagementOperationStore(tmp_path).list_operations().records) == 1


def test_lane_is_durable_and_different_targets_are_independent(tmp_path: Path) -> None:
    store = ManagementOperationStore(tmp_path)
    first = store.admit(intent(), authority()).record
    assert first
    assert ManagementOperationStore(tmp_path).admit(intent("other"), authority()).status is AdmissionStatus.BUSY
    assert ManagementOperationStore(tmp_path).admit(intent("other", channel="canary"), authority()).status is AdmissionStatus.CREATED
    receipt = EffectReceipt("worker/1", "worker-journal", 3, "accepted_for_reevaluation")
    assert store.transition(first.operation_id, 0, OperationPhase.SUCCEEDED, evidence=receipt).status is MutationStatus.UPDATED
    assert store.admit(intent("other"), authority()).status is AdmissionStatus.CREATED


def test_terminal_evidence_is_nonempty_correlated_and_persisted(tmp_path: Path) -> None:
    store = ManagementOperationStore(tmp_path)
    record = store.admit(intent(), authority()).record
    assert record
    unknown = store.begin_effect(record.operation_id, 0, owner_attempt_id="owner", attempt_id="attempt", correlation_identity="container/1")
    assert unknown.record
    invalid = (
        EffectReceipt("", "", 0, ""),
        EffectReceipt("wrong", "docker", 2, "running"),
        EffectReceipt("container/1", "x" * 513, 2, "running"),
    )
    for evidence in invalid:
        result = store.transition(record.operation_id, 1, OperationPhase.SUCCEEDED, evidence=evidence)
        assert result.status is MutationStatus.INVALID
        assert store.admit(intent("competitor"), authority()).status is AdmissionStatus.BUSY
    valid = EffectReceipt("container/1", "docker-observation", 3, "running")
    completed = store.transition(record.operation_id, 1, OperationPhase.SUCCEEDED, evidence=valid)
    assert completed.status is MutationStatus.UPDATED
    reopened = ManagementOperationStore(tmp_path).get(record.operation_id)
    assert reopened.status is ReadStatus.FOUND
    assert reopened.record and reopened.record.effect.receipt == valid


def test_sent_unknown_history_retains_identity_and_cannot_be_replaced(tmp_path: Path) -> None:
    store = ManagementOperationStore(tmp_path)
    record = store.admit(intent(), authority()).record
    assert record
    unknown = store.begin_effect(record.operation_id, 0, owner_attempt_id="owner-a", attempt_id="attempt-a", correlation_identity="container/a")
    assert unknown.record and unknown.record.effect.state is EffectState.SENT_UNKNOWN
    replacement = store.begin_effect(record.operation_id, 1, owner_attempt_id="owner-b", attempt_id="attempt-b", correlation_identity="container/b")
    assert replacement.status is MutationStatus.INVALID
    rejected_receipt = EffectReceipt("container/a", "docker", 2, "rejected")
    rejected = store.record_effect(record.operation_id, 1, attempt_id="attempt-a", state=EffectState.REJECTED, receipt=rejected_receipt)
    assert rejected.status is MutationStatus.UPDATED
    second = store.begin_effect(record.operation_id, 2, owner_attempt_id="owner-b", attempt_id="attempt-b", correlation_identity="container/b")
    assert second.status is MutationStatus.UPDATED
    entries = ManagementOperationStore(tmp_path).history(record.operation_id).entries
    sent = entries[1]
    assert (sent.effect_state, sent.effect_owner_generation, sent.effect_attempt_id, sent.correlation_identity) == (EffectState.SENT_UNKNOWN, 1, "attempt-a", "container/a")
    assert (entries[-1].effect_attempt_id, entries[-1].correlation_identity) == ("attempt-b", "container/b")
    wrong = EffectReceipt("container/a", "docker", 4, "running")
    assert store.record_effect(record.operation_id, 3, attempt_id="attempt-b", state=EffectState.CONFIRMED, receipt=wrong).status is MutationStatus.STALE
    assert ManagementOperationStore(tmp_path).get(record.operation_id).record == second.record


class BarrierStore(ManagementOperationStore):
    barrier = threading.Barrier(2)

    def _before_admission_transaction(self) -> None:
        self.barrier.wait(timeout=5)


class LostAcknowledgementStore(ManagementOperationStore):
    def _after_admission_commit(self) -> None:
        raise sqlite3.OperationalError("controlled acknowledgement loss")


def test_independent_stores_contend_after_deterministic_barrier(tmp_path: Path) -> None:
    stores = (BarrierStore(tmp_path), BarrierStore(tmp_path))
    # Initialize the schema before both independent connections reach the barrier.
    assert all(store.get("missing").status is ReadStatus.NOT_FOUND for store in stores)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda pair: pair[0].admit(intent(pair[1]), authority()), zip(stores, ("a", "b"))))
    assert {result.status for result in results} == {AdmissionStatus.CREATED, AdmissionStatus.BUSY}
    winner = next(result for result in results if result.status is AdmissionStatus.CREATED)
    assert winner.record
    replay = ManagementOperationStore(tmp_path).admit(winner.record.intent, authority())
    assert replay.status is AdmissionStatus.REPLAY and replay.record == winner.record


def test_commit_response_loss_uses_exact_key_readback(tmp_path: Path) -> None:
    result = LostAcknowledgementStore(tmp_path).admit(intent(), authority())
    assert result.status is AdmissionStatus.REPLAY
    assert result.detail == "confirmed after uncertain commit"
    assert result.record
    reopened = ManagementOperationStore(tmp_path).find_by_key(intent(), authority())
    assert reopened.status is ReadStatus.FOUND and reopened.record == result.record
    assert len(ManagementOperationStore(tmp_path).list_operations().records) == 1


def test_corrupt_storage_is_typed_unavailable_and_does_not_release_lane(tmp_path: Path) -> None:
    good_root, corrupt_root = tmp_path / "good", tmp_path / "corrupt"
    good = ManagementOperationStore(good_root)
    record = good.admit(intent(), authority()).record
    assert record
    corrupt_root.mkdir()
    shutil.copy2(good_root / "management-operations.sqlite3", corrupt_root / "management-operations.sqlite3")
    (corrupt_root / "management-operations.sqlite3").write_bytes(b"not a sqlite database")
    broken = ManagementOperationStore(corrupt_root)
    assert broken.get(record.operation_id).status is ReadStatus.UNAVAILABLE
    assert broken.find_by_key(intent(), authority()).status is ReadStatus.UNAVAILABLE
    assert broken.list_operations().status is QueryStatus.UNAVAILABLE
    assert broken.history(record.operation_id).status is QueryStatus.UNAVAILABLE
    receipt = EffectReceipt("container/1", "docker", 2, "running")
    assert broken.record_effect(record.operation_id, 0, attempt_id="attempt", state=EffectState.CONFIRMED, receipt=receipt).status is MutationStatus.UNAVAILABLE
    assert broken.resolve_needs_attention(record.operation_id, 0, OperationPhase.FAILED, evidence=receipt).status is MutationStatus.UNAVAILABLE
    assert broken.admit(intent("competitor"), authority()).status is AdmissionStatus.UNAVAILABLE
    assert good.get(record.operation_id).status is ReadStatus.FOUND
    assert good.admit(intent("competitor"), authority()).status is AdmissionStatus.BUSY


def test_schema_and_credentials_fail_closed(tmp_path: Path) -> None:
    store = ManagementOperationStore(tmp_path)
    assert store.admit(intent(), authority()).status is AdmissionStatus.CREATED
    unsafe = replace(intent("unsafe", channel="other"), parameters=(ActionParameter("bearer_token", "canary-secret"),))
    assert store.admit(unsafe, authority()).status is AdmissionStatus.UNAVAILABLE
    assert b"canary-secret" not in (tmp_path / "management-operations.sqlite3").read_bytes()
    connection = sqlite3.connect(tmp_path / "management-operations.sqlite3")
    connection.execute("UPDATE metadata SET value='999' WHERE key='schema_version'")
    connection.commit()
    connection.close()
    assert ManagementOperationStore(tmp_path).admit(intent("new"), authority()).status is AdmissionStatus.UNAVAILABLE
