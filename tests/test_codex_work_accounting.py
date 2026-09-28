import multiprocessing
from dataclasses import replace
from pathlib import Path

import pytest

from auto_coder.codex_work_accounting import (
    CodexWorkAccounting,
    CodexWorkOperation,
    CodexWorkPhase,
    ReconstructionReceipt,
    RetirementValidation,
    WorkAccountingStatus,
)
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository, ImplementationSlotUnavailable

OWNER = ImplementationOwner("issue", 2333)


def _register_in_process(storage_path: str, incarnation: str, barrier, output) -> None:
    slots = ImplementationSlotRepository("owner/repo", 2, storage_path=Path(storage_path))
    accounting = CodexWorkAccounting(slots)
    barrier.wait()
    try:
        registration = accounting.register(
            OWNER,
            incarnation,
            logical_operation_id="concurrent-operation",
            kind="repair",
            source_request_id="concurrent-request",
        )
        output.put(("registered", registration.send_authorized, registration.snapshot.revision))
    except ImplementationSlotUnavailable:
        output.put(("unavailable", False, None))


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
        evidence_source_request_id="attempt-0",
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
            evidence_source_request_id="attempt-0/correction-1",
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
        evidence_source_request_id="attempt-0/correction-1",
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
            evidence_source_request_id="request",
            task_id="task",
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
    with pytest.raises(ValueError, match="complete reconstruction"):
        accounting.initialize_from_receipt(
            OWNER,
            incarnation,
            ReconstructionReceipt("receipt", ("cloud", "retry"), ("cloud", "retry")),
        )
    assert accounting.snapshot(OWNER, incarnation).accounting_status is WorkAccountingStatus.UNINITIALIZED

    initialized = accounting.initialize_from_receipt(
        OWNER,
        incarnation,
        ReconstructionReceipt(
            "receipt",
            ("cloud", "retry"),
            ("retry", "cloud"),
            (("cloud", ()), ("retry", ())),
        ),
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


def test_accepted_responsibility_survives_ambiguous_delivery_and_cannot_become_non_delivery(tmp_path: Path) -> None:
    slots, incarnation = _reservation(tmp_path)
    accounting = CodexWorkAccounting(slots)
    accounting.initialize_fresh(OWNER, incarnation)
    accounting.register(OWNER, incarnation, logical_operation_id="op", kind="submission", source_request_id="request")
    accounting.transition(OWNER, incarnation, "op", CodexWorkPhase.ACCEPTED, evidence_id="accepted")
    accounting.transition(OWNER, incarnation, "op", CodexWorkPhase.DELIVERY_UNKNOWN, evidence_id="tracking-lost")

    with pytest.raises(ValueError, match="Settlement requires"):
        accounting.transition(
            OWNER,
            incarnation,
            "op",
            CodexWorkPhase.SETTLED,
            evidence_id="not-delivered",
            evidence_source_request_id="request",
            definite_non_delivery=True,
        )

    operation = accounting.snapshot(OWNER, incarnation).operations[0]
    assert operation.accepted is True
    assert operation.phase is CodexWorkPhase.DELIVERY_UNKNOWN
    assert operation.settled is False


@pytest.mark.parametrize(
    "malformed_update",
    [
        {"settlement_evidence_id": None},
        {"execution_complete": "false"},
    ],
)
def test_malformed_settlement_record_is_unavailable(tmp_path: Path, malformed_update: dict[str, object]) -> None:
    slots, incarnation = _reservation(tmp_path)
    accounting = CodexWorkAccounting(slots)
    accounting.initialize_fresh(OWNER, incarnation)
    accounting.register(OWNER, incarnation, logical_operation_id="op", kind="submission", source_request_id="request")
    accounting.transition(
        OWNER,
        incarnation,
        "op",
        CodexWorkPhase.SETTLED,
        evidence_id="terminal",
        evidence_source_request_id="request",
        execution_complete=True,
        publication_complete=True,
        tracking_complete=True,
    )
    with slots._state_lock():
        owners = slots._read()
        operation = owners[OWNER.key]["codex_work_accounting"]["operations"]["op"]
        operation.update(malformed_update)
        slots._write(owners)

    snapshot = accounting.snapshot(OWNER, incarnation)
    assert snapshot.accounting_status is WorkAccountingStatus.UNAVAILABLE
    assert snapshot.releasable is False


def test_reconstruction_receipt_preserves_unsettled_correlated_operation(tmp_path: Path) -> None:
    slots, incarnation = _reservation(tmp_path)
    operation = CodexWorkOperation(
        logical_operation_id="publication",
        kind="publication",
        source_request_id="request",
        causal_baseline="head-a",
        task_id="task-a",
        phase=CodexWorkPhase.ACCEPTED,
        execution_complete=False,
        publication_complete=False,
        tracking_complete=False,
        settlement_evidence_id=None,
        accepted=True,
    )
    receipt = ReconstructionReceipt(
        "receipt",
        ("cloud", "retry"),
        ("retry", "cloud"),
        (("cloud", ("publication",)), ("retry", ())),
        (operation,),
    )

    snapshot = CodexWorkAccounting(slots).initialize_from_receipt(OWNER, incarnation, receipt)
    assert snapshot.accounting_status is WorkAccountingStatus.COMPLETE
    assert snapshot.operations == (operation,)
    assert snapshot.releasable is False


def test_retirement_guard_rejects_foreign_repository_snapshot(tmp_path: Path) -> None:
    slots, incarnation = _reservation(tmp_path)
    accounting = CodexWorkAccounting(slots)
    snapshot = accounting.initialize_fresh(OWNER, incarnation)

    with accounting.retirement_guard(replace(snapshot, repository="another/repository")) as guard:
        assert guard.status is RetirementValidation.STALE


def test_settlement_requires_task_identity_when_no_baseline_exists(tmp_path: Path) -> None:
    slots, incarnation = _reservation(tmp_path)
    accounting = CodexWorkAccounting(slots)
    accounting.initialize_fresh(OWNER, incarnation)
    accounting.register(OWNER, incarnation, logical_operation_id="op", kind="submission", source_request_id="request-a", task_id="task-a")

    with pytest.raises(ValueError, match="identify the operation's task"):
        accounting.transition(
            OWNER,
            incarnation,
            "op",
            CodexWorkPhase.SETTLED,
            evidence_id="terminal-task-b",
            evidence_source_request_id="request-a",
            execution_complete=True,
            publication_complete=True,
            tracking_complete=True,
        )

    snapshot = accounting.snapshot(OWNER, incarnation)
    assert snapshot.operations[0].phase is CodexWorkPhase.RESERVED
    assert snapshot.releasable is False


def test_cross_process_registration_first_invalidates_retirement_snapshot(tmp_path: Path) -> None:
    slots, incarnation = _reservation(tmp_path)
    accounting = CodexWorkAccounting(slots)
    old_snapshot = accounting.initialize_fresh(OWNER, incarnation)
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    output = context.Queue()
    process = context.Process(target=_register_in_process, args=(str(slots.storage_path), incarnation, barrier, output))
    process.start()

    # The snapshot is already captured before both participants cross the
    # barrier, so registration has a deterministic registration-first order.
    barrier.wait(timeout=10)
    process.join(timeout=10)
    assert process.exitcode == 0
    assert output.get(timeout=2) == ("registered", False, old_snapshot.revision + 1)

    with accounting.retirement_guard(old_snapshot) as guard:
        assert guard.status is RetirementValidation.STALE
    current = accounting.snapshot(OWNER, incarnation)
    with accounting.retirement_guard(current) as guard:
        assert guard.status is RetirementValidation.NON_RELEASABLE


def test_cross_process_retirement_first_fences_later_registration(tmp_path: Path) -> None:
    slots, incarnation = _reservation(tmp_path)
    accounting = CodexWorkAccounting(slots)
    snapshot = accounting.initialize_fresh(OWNER, incarnation)
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    output = context.Queue()

    with accounting.retirement_guard(snapshot) as guard:
        assert guard.status is RetirementValidation.AUTHORIZED
        process = context.Process(target=_register_in_process, args=(str(slots.storage_path), incarnation, barrier, output))
        process.start()
        # The guard holds the cross-process owner/store boundary before the
        # registering process is released to call register().
        barrier.wait(timeout=10)
        with slots._state_lock():
            owners = slots._read()
            del owners[OWNER.key]
            slots._write(owners)

    process.join(timeout=10)
    assert process.exitcode == 0
    assert output.get(timeout=2) == ("unavailable", False, None)
    assert slots.owner_incarnation(OWNER) is None
