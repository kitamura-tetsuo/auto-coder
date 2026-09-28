from pathlib import Path

import pytest

from auto_coder.codex_work_accounting import (
    CodexWorkAccounting,
    CodexWorkPhase,
    ReconstructionReceipt,
    RetirementValidation,
    WorkAccountingStatus,
)
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository, ImplementationSlotUnavailable

OWNER = ImplementationOwner("issue", 2333)


def _repository(path: Path) -> ImplementationSlotRepository:
    return ImplementationSlotRepository("owner/repo", 2, storage_path=path / "slots.json")


def _reservation(path: Path) -> tuple[ImplementationSlotRepository, str]:
    slots = _repository(path)
    assert slots.reserve(OWNER)
    incarnation = slots.owner_incarnation(OWNER)
    assert incarnation is not None
    return slots, incarnation


def test_same_task_distinct_operation_invalidates_snapshot_and_replay_is_idempotent(tmp_path: Path) -> None:
    slots, incarnation = _reservation(tmp_path)
    accounting = CodexWorkAccounting(slots)
    accounting.initialize_fresh(OWNER, incarnation)
    first = accounting.register(
        OWNER,
        incarnation,
        logical_operation_id="initial",
        kind="submission",
        source_request_id="attempt-0",
        causal_baseline="head-a",
        task_id="task-one",
    )
    settled = accounting.transition(
        OWNER,
        incarnation,
        "initial",
        CodexWorkPhase.SETTLED,
        evidence_id="terminal-task-one-head-a",
        evidence_causal_baseline="head-a",
        task_id="task-one",
        execution_complete=True,
        publication_complete=True,
        tracking_complete=True,
    )
    assert first.created is True
    assert first.send_authorized is False
    assert settled.releasable is True

    repair = accounting.register(
        OWNER,
        incarnation,
        logical_operation_id="repair-1",
        kind="repair",
        source_request_id="attempt-0/correction-1",
        causal_baseline="head-b",
        task_id="task-one",
    )
    replay = accounting.register(
        OWNER,
        incarnation,
        logical_operation_id="repair-1",
        kind="repair",
        source_request_id="attempt-0/correction-1",
        causal_baseline="head-b",
        task_id="task-one",
    )

    assert repair.snapshot.revision == settled.revision + 1
    assert replay.created is False
    assert replay.snapshot.revision == repair.snapshot.revision
    assert [operation.logical_operation_id for operation in replay.snapshot.operations] == ["initial", "repair-1"]
    with accounting.retirement_guard(settled) as guard:
        assert guard.status is RetirementValidation.STALE
    with accounting.retirement_guard(repair.snapshot) as guard:
        assert guard.status is RetirementValidation.NON_RELEASABLE

    with pytest.raises(ValueError, match="causal baseline"):
        accounting.transition(
            OWNER,
            incarnation,
            "repair-1",
            CodexWorkPhase.SETTLED,
            evidence_id="terminal-task-one-head-a",
            evidence_causal_baseline="head-a",
            task_id="task-one",
            execution_complete=True,
            publication_complete=True,
            tracking_complete=True,
        )
    still_pending = accounting.snapshot(OWNER, incarnation)
    assert still_pending.operations[1].phase is CodexWorkPhase.RESERVED
    assert still_pending.operations[1].settlement_evidence_id is None
    assert still_pending.releasable is False

    repaired = accounting.transition(
        OWNER,
        incarnation,
        "repair-1",
        CodexWorkPhase.SETTLED,
        evidence_id="terminal-task-one-head-b",
        evidence_causal_baseline="head-b",
        task_id="task-one",
        execution_complete=True,
        publication_complete=True,
        tracking_complete=True,
    )
    assert repaired.operations[1].phase is CodexWorkPhase.SETTLED
    assert repaired.operations[1].settlement_evidence_id == "terminal-task-one-head-b"
    assert repaired.releasable is True


def test_ambiguous_delivery_and_accepted_handoff_remain_unsettled_after_reconstruction(tmp_path: Path) -> None:
    slots, incarnation = _reservation(tmp_path)
    accounting = CodexWorkAccounting(slots)
    accounting.initialize_fresh(OWNER, incarnation)
    accounting.register(OWNER, incarnation, logical_operation_id="op", kind="submission", source_request_id="request", task_id="task")
    accounting.transition(OWNER, incarnation, "op", CodexWorkPhase.DELIVERY_UNKNOWN, evidence_id="timeout", task_id="task")

    reconstructed = CodexWorkAccounting(_repository(tmp_path))
    snapshot = reconstructed.snapshot(OWNER, incarnation)
    assert snapshot.operations[0].phase is CodexWorkPhase.DELIVERY_UNKNOWN
    assert snapshot.releasable is False
    with pytest.raises(ValueError, match="Settlement requires"):
        reconstructed.transition(
            OWNER,
            incarnation,
            "op",
            CodexWorkPhase.SETTLED,
            evidence_id="cancelled-followup",
            definite_non_delivery=True,
        )


def test_missing_inventory_is_unknown_and_receipt_controls_legacy_initialization(tmp_path: Path) -> None:
    slots, incarnation = _reservation(tmp_path)
    slots.record_implementation_pr(OWNER, 88)
    accounting = CodexWorkAccounting(slots)
    snapshot = accounting.snapshot(OWNER, incarnation)
    assert snapshot.accounting_status is WorkAccountingStatus.UNINITIALIZED
    assert snapshot.releasable is False
    with pytest.raises(ImplementationSlotUnavailable, match="Fresh accounting"):
        accounting.initialize_fresh(OWNER, incarnation)
    with pytest.raises(ValueError, match="complete reconstruction"):
        accounting.initialize_from_receipt(OWNER, incarnation, ReconstructionReceipt("receipt", ("cloud", "retry"), ("cloud",)))

    initialized = accounting.initialize_from_receipt(
        OWNER,
        incarnation,
        ReconstructionReceipt("receipt", ("cloud", "retry"), ("retry", "cloud")),
    )
    assert initialized.accounting_status is WorkAccountingStatus.COMPLETE
    assert initialized.reconstruction_receipt_id == "receipt"
    assert initialized.releasable is True


def test_retired_incarnation_cannot_be_recreated_or_changed_by_late_receipt(tmp_path: Path) -> None:
    slots, old_incarnation = _reservation(tmp_path)
    accounting = CodexWorkAccounting(slots)
    settled = accounting.initialize_fresh(OWNER, old_incarnation)
    with accounting.retirement_guard(settled) as guard:
        assert guard.status is RetirementValidation.AUTHORIZED
        with slots._state_lock():
            owners = slots._read()
            del owners[OWNER.key]
            slots._write(owners)

    assert slots.reserve(OWNER)
    new_incarnation = slots.owner_incarnation(OWNER)
    assert new_incarnation is not None and new_incarnation != old_incarnation
    with pytest.raises(ImplementationSlotUnavailable, match="stale"):
        accounting.register(
            OWNER,
            old_incarnation,
            logical_operation_id="late",
            kind="tracking",
            source_request_id="old-request",
        )
    assert slots.owner_incarnation(OWNER) == new_incarnation
