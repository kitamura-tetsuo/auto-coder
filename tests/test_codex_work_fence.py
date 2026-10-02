from pathlib import Path

import pytest

from auto_coder.codex_work_accounting import CodexWorkAccounting, CodexWorkPhase
from auto_coder.codex_work_fence import CodexWorkFence, CodexWorkIdentity, stable_codex_operation_id
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository, ImplementationSlotUnavailable


def test_fence_registers_before_transport_and_refuses_replay_or_retired_incarnation(tmp_path: Path) -> None:
    slots = ImplementationSlotRepository("owner/repo", 1, storage_path=tmp_path / "slots.json")
    owner = ImplementationOwner("issue", 2334)
    assert slots.reserve(owner)
    incarnation = slots.establish_incarnation(owner)
    accounting = CodexWorkAccounting(slots)
    accounting.initialize_fresh(owner, incarnation)
    fence = CodexWorkFence(accounting)
    identity = CodexWorkIdentity(
        repository="owner/repo",
        issue_number=2334,
        incarnation=incarnation,
        operation_id=stable_codex_operation_id("repair", "bundle:7"),
        operation_kind="repair",
        source_request_id="bundle:7",
        task_id="task-1",
        causal_baseline="turn-4",
    )
    observed = []

    result = fence.execute(identity, lambda: observed.append(accounting.snapshot(owner, incarnation)) or "sent")

    assert result == "sent"
    assert observed[0].operations[0].phase is CodexWorkPhase.RESERVED
    assert observed[0].operations[0].task_id == "task-1"
    with pytest.raises(ImplementationSlotUnavailable, match="duplicate send"):
        fence.execute(identity, lambda: pytest.fail("replay reached transport"))

    with slots._state_lock():
        records = slots._read()
        del records[owner.key]
        slots._write(records)
    with pytest.raises(ImplementationSlotUnavailable, match="stale"):
        fence.execute(
            CodexWorkIdentity("owner/repo", 2334, incarnation, "later", "repair", "bundle:8", "task-1"),
            lambda: pytest.fail("retired work reached transport"),
        )


def test_fence_preserves_accepted_and_ambiguous_outcomes(tmp_path: Path) -> None:
    slots = ImplementationSlotRepository("owner/repo", 1, storage_path=tmp_path / "slots.json")
    owner = ImplementationOwner("issue", 2334)
    assert slots.reserve(owner)
    incarnation = slots.establish_incarnation(owner)
    accounting = CodexWorkAccounting(slots)
    accounting.initialize_fresh(owner, incarnation)
    fence = CodexWorkFence(accounting)
    accepted = CodexWorkIdentity("owner/repo", 2334, incarnation, "accepted", "submission", "attempt-0")
    unknown = CodexWorkIdentity("owner/repo", 2334, incarnation, "unknown", "follow-up", "feedback-1", "task-1", "turn-4")
    fence.execute(accepted, lambda: None)
    fence.record_delivery(accepted, accepted=True, indeterminate=False, evidence_id="receipt", task_id="task-1")
    fence.execute(unknown, lambda: None)
    fence.record_delivery(unknown, accepted=False, indeterminate=True, evidence_id="timeout")

    operations = {operation.logical_operation_id: operation for operation in accounting.snapshot(owner, incarnation).operations}
    assert operations["accepted"].phase is CodexWorkPhase.ACCEPTED
    assert operations["accepted"].accepted is True
    assert operations["unknown"].phase is CodexWorkPhase.DELIVERY_UNKNOWN
    assert operations["unknown"].settled is False


@pytest.mark.timeout(5)
def test_followup_fence_reenters_engine_owner_lock_with_fresh_repository(tmp_path: Path) -> None:
    engine_slots = ImplementationSlotRepository("owner/repo", 1, storage_path=tmp_path / "slots.json")
    client_slots = ImplementationSlotRepository("owner/repo", 1, storage_path=tmp_path / "slots.json")
    owner = ImplementationOwner("issue", 2405)
    assert engine_slots.reserve(owner)
    incarnation = engine_slots.establish_incarnation(owner)
    accounting = CodexWorkAccounting(client_slots)
    accounting.initialize_fresh(owner, incarnation)
    fence = CodexWorkFence(accounting)
    identity = CodexWorkIdentity("owner/repo", 2405, incarnation, "repair:2409", "follow-up", "conflict:2409", "task-1", "turn-4")
    sends = []

    with engine_slots.serialize(owner):
        assert fence.execute(identity, lambda: sends.append("sent") or True) is True
        with pytest.raises(ImplementationSlotUnavailable, match="duplicate send"):
            fence.execute(identity, lambda: pytest.fail("duplicate transport"))

    assert sends == ["sent"]
    operation = accounting.snapshot(owner, incarnation).operations[0]
    assert operation.logical_operation_id == "repair:2409"
    assert operation.phase is CodexWorkPhase.RESERVED
