import sqlite3
from dataclasses import replace
from pathlib import Path

from auto_coder.management.operation_store import ManagementOperationStore
from auto_coder.management.operations import AcceptedAuthority, ActionParameter, AdmissionStatus, EffectReceipt, EffectState, ManagedTarget, MutationStatus, OperationIntent, OperationPhase


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


def test_sent_unknown_restart_stale_generation_and_history(tmp_path: Path) -> None:
    store = ManagementOperationStore(tmp_path)
    record = store.admit(intent(), authority()).record
    assert record
    unknown = store.begin_effect(record.operation_id, 0, owner_attempt_id="owner-a", attempt_id="attempt-a", correlation_identity="container/1")
    assert unknown.record and unknown.record.effect.state is EffectState.SENT_UNKNOWN
    reopened = ManagementOperationStore(tmp_path)
    assert reopened.get(record.operation_id) == unknown.record
    newer = reopened.transition(record.operation_id, 1, OperationPhase.RECONCILING, owner_attempt_id="owner-b")
    receipt = EffectReceipt("container/1", "docker-observation", 4, "running")
    stale = reopened.record_effect(record.operation_id, 1, attempt_id="attempt-a", state=EffectState.CONFIRMED, receipt=receipt)
    assert stale.status is MutationStatus.STALE
    assert reopened.get(record.operation_id) == newer.record
    assert reopened.admit(intent("competitor"), authority()).status is AdmissionStatus.BUSY
    page = reopened.history(record.operation_id, limit=1)
    assert len(page.entries) == 1 and page.next_cursor


def test_terminal_requires_evidence_and_schema_fails_closed(tmp_path: Path) -> None:
    store = ManagementOperationStore(tmp_path)
    record = store.admit(intent(), authority()).record
    assert record
    assert store.transition(record.operation_id, 0, OperationPhase.FAILED).status is MutationStatus.INVALID
    unsafe = replace(intent("unsafe", channel="other"), parameters=(ActionParameter("bearer_token", "canary-secret"),))
    assert store.admit(unsafe, authority()).status is AdmissionStatus.UNAVAILABLE
    assert b"canary-secret" not in (tmp_path / "management-operations.sqlite3").read_bytes()
    connection = sqlite3.connect(tmp_path / "management-operations.sqlite3")
    connection.execute("UPDATE metadata SET value='999' WHERE key='schema_version'")
    connection.commit()
    connection.close()
    assert ManagementOperationStore(tmp_path).admit(intent("new"), authority()).status is AdmissionStatus.UNAVAILABLE
