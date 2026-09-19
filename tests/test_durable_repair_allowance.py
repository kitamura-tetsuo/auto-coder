"""Tests for the durable repair allowance state machine (GitHub Issue #2140).

Covers:
- AS-001: Three failures, no fourth repair (REQ-001..REQ-008).
- AS-002: One generation is not three commits (REQ-002, REQ-004..REQ-006).
- AS-003: Old evidence becomes visible late (REQ-003..REQ-005, REQ-010).
- AS-004: Ambiguous delivery and restart (REQ-003, REQ-005, REQ-010, REQ-011).
- AS-005: Mixed outcomes and recurrence (REQ-006..REQ-008).
- AS-006: Last allowance and explicit grant contention (REQ-009, REQ-010, REQ-012, REQ-013).
- Duplicate observations, failed persistence, and reconciliation-required
  history are additionally covered as standalone REQ-013 regressions.

Per Issue #2140's own scoping, this module alone does not yet intercept
production providers: tests supply pre-normalized observations directly to
the public transition boundary, exactly as the issue's Implementation Notes
anticipate ("A pure model test may supply normalized observations; the
provider stage must prove those observations arise from real production
inputs").
"""

from __future__ import annotations

import multiprocessing
import sqlite3
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from auto_coder.durable_repair_allowance import (
    BlockerSettlement,
    CompletionAvailability,
    CorrectiveGenerationBundle,
    DeliveryOutcome,
    GenerationLifecycleState,
    InvalidOperatorGrantError,
    RepairAllowanceIdempotencyConflictError,
    RepairAllowanceLedger,
    RepairAllowancePersistenceError,
    RepairAllowanceStatus,
    RepairAllowanceUnavailableError,
    StaleRepairAllowanceEpochError,
    ValidationAvailability,
    ValidationObservation,
)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "repair_allowance.db"


@pytest.fixture()
def store(db_path: Path) -> RepairAllowanceLedger:
    return RepairAllowanceLedger(db_path=db_path)


API_ORIGIN = "https://api.github.com"
REPO = "kitamura-tetsuo/auto-coder"
PR_NUMBER = 2140


def _run_full_generation(store: RepairAllowanceLedger, epoch: int, blocker_id: str, op_suffix: str, *, completion_seq: int, code_changed: bool = True, still_unmet: bool = True) -> tuple[int, str]:
    """Admit, confirm-deliver, complete, and validate one generation covering a single blocker."""
    bundle = CorrectiveGenerationBundle(bundle_reference=f"bundle-{op_suffix}", covered_blocker_ids=(blocker_id,), owning_identity="task-1", observed_baseline="sha-0")
    admission = store.admit_generation(API_ORIGIN, REPO, PR_NUMBER, f"op-admit-{op_suffix}", epoch, bundle, open_blocker_ids=(blocker_id,))
    assert admission.admitted, admission.denial_reason
    generation_id = admission.generation_id
    assert generation_id is not None
    epoch = admission.snapshot.epoch
    snapshot = store.record_delivery_outcome(API_ORIGIN, REPO, PR_NUMBER, f"op-deliver-{op_suffix}", epoch, generation_id, DeliveryOutcome.CONFIRMED, f"deliv-{op_suffix}")
    epoch = snapshot.epoch
    snapshot = store.record_completion_observation(API_ORIGIN, REPO, PR_NUMBER, f"op-complete-{op_suffix}", epoch, generation_id, CompletionAvailability.KNOWN, completion_seq=completion_seq, code_changed=code_changed)
    epoch = snapshot.epoch
    snapshot = store.record_validation_results(
        API_ORIGIN,
        REPO,
        PR_NUMBER,
        f"op-validate-{op_suffix}",
        epoch,
        generation_id,
        [ValidationObservation(blocker_id=blocker_id, still_unmet=still_unmet, availability=ValidationAvailability.KNOWN, validation_seq=completion_seq + 1)],
    )
    return snapshot.epoch, generation_id


def test_as001_three_failures_no_fourth_repair(store: RepairAllowanceLedger) -> None:
    """AS-001: three settled, still-unmet generations exhaust a blocker; a fourth is denied.

    Cosmetic detail changes (a differently worded bundle_reference) and a
    newly found independent blocker do not restore admission for the PR.
    """
    snapshot = store.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)
    blocker_id = "blk_as001"
    epoch = snapshot.epoch

    for i in range(3):
        epoch, generation_id = _run_full_generation(store, epoch, blocker_id, f"as001-{i}", completion_seq=10 * (i + 1))
        allowance = store.get_snapshot(API_ORIGIN, REPO, PR_NUMBER).get_blocker_allowance(blocker_id)
        assert allowance is not None
        assert allowance.total_failed_count == i + 1
        generation = store.get_snapshot(API_ORIGIN, REPO, PR_NUMBER).get_generation(generation_id)
        assert generation is not None
        assert generation.lifecycle_state == GenerationLifecycleState.SETTLED

    final_snapshot = store.get_snapshot(API_ORIGIN, REPO, PR_NUMBER)
    allowance = final_snapshot.get_blocker_allowance(blocker_id)
    assert allowance is not None
    assert allowance.status == RepairAllowanceStatus.EXHAUSTED
    assert allowance.failed_count == 3
    assert allowance.remaining == 0

    fourth_bundle = CorrectiveGenerationBundle(bundle_reference="cosmetic reword", covered_blocker_ids=(blocker_id,), owning_identity="task-1", observed_baseline="sha-0")
    fourth = store.admit_generation(API_ORIGIN, REPO, PR_NUMBER, "op-admit-fourth", epoch, fourth_bundle, open_blocker_ids=(blocker_id,))
    assert not fourth.admitted
    assert fourth.denial_reason is not None and "exhausted" in fourth.denial_reason.lower()

    other_blocker = "blk_as001_independent"
    other_bundle = CorrectiveGenerationBundle(bundle_reference="independent-blocker", covered_blocker_ids=(other_blocker,), owning_identity="task-1", observed_baseline="sha-0")
    other = store.admit_generation(API_ORIGIN, REPO, PR_NUMBER, "op-admit-other", epoch, other_bundle, open_blocker_ids=(blocker_id, other_blocker))
    assert not other.admitted, "a newly found independent blocker must not restore admission while another open blocker is exhausted"


def test_as002_one_generation_is_not_three_commits(store: RepairAllowanceLedger) -> None:
    """AS-002: one confirmed generation charges at most one failure, even with two validators.

    A no-change completion can still be charged once, given genuinely later
    independent validation; an arbitrary head change with no recorded
    completion evidence cannot be charged.
    """
    snapshot = store.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)
    blocker_id = "blk_as002"
    epoch = snapshot.epoch

    bundle = CorrectiveGenerationBundle(bundle_reference="bundle-as002", covered_blocker_ids=(blocker_id,), owning_identity="task-1", observed_baseline="sha-0")
    admission = store.admit_generation(API_ORIGIN, REPO, PR_NUMBER, "op-admit", epoch, bundle, open_blocker_ids=(blocker_id,))
    assert admission.admitted
    generation_id = admission.generation_id
    assert generation_id is not None
    epoch = admission.snapshot.epoch

    snapshot = store.record_delivery_outcome(API_ORIGIN, REPO, PR_NUMBER, "op-deliver", epoch, generation_id, DeliveryOutcome.CONFIRMED, "deliv-1")
    epoch = snapshot.epoch
    snapshot = store.record_completion_observation(API_ORIGIN, REPO, PR_NUMBER, "op-complete", epoch, generation_id, CompletionAvailability.KNOWN, completion_seq=5, code_changed=True)
    epoch = snapshot.epoch

    snapshot = store.record_validation_results(API_ORIGIN, REPO, PR_NUMBER, "op-validate-a", epoch, generation_id, [ValidationObservation(blocker_id=blocker_id, still_unmet=True, availability=ValidationAvailability.KNOWN, validation_seq=6, evidence="validator-A")])
    epoch = snapshot.epoch
    snapshot = store.record_validation_results(API_ORIGIN, REPO, PR_NUMBER, "op-validate-b", epoch, generation_id, [ValidationObservation(blocker_id=blocker_id, still_unmet=True, availability=ValidationAvailability.KNOWN, validation_seq=7, evidence="validator-B")])
    epoch = snapshot.epoch

    allowance = snapshot.get_blocker_allowance(blocker_id)
    assert allowance is not None
    assert allowance.total_failed_count == 1, "two validators reporting the same unresolved blocker must charge exactly one failure"

    # A no-change completion can still be charged once after a genuinely later validation.
    epoch, no_change_generation = _run_full_generation(store, epoch, blocker_id, "as002-no-change", completion_seq=20, code_changed=False)
    allowance = store.get_snapshot(API_ORIGIN, REPO, PR_NUMBER).get_blocker_allowance(blocker_id)
    assert allowance is not None
    assert allowance.total_failed_count == 2

    # An arbitrary head change alone (no completion recorded) cannot be charged.
    third_bundle = CorrectiveGenerationBundle(bundle_reference="bundle-as002-head-only", covered_blocker_ids=(blocker_id,), owning_identity="task-1", observed_baseline="sha-1")
    third_admission = store.admit_generation(API_ORIGIN, REPO, PR_NUMBER, "op-admit-third", epoch, third_bundle, open_blocker_ids=(blocker_id,))
    assert third_admission.admitted
    third_generation_id = third_admission.generation_id
    assert third_generation_id is not None
    epoch = third_admission.snapshot.epoch
    snapshot = store.record_delivery_outcome(API_ORIGIN, REPO, PR_NUMBER, "op-deliver-third", epoch, third_generation_id, DeliveryOutcome.CONFIRMED, "deliv-third")
    epoch = snapshot.epoch
    snapshot = store.record_validation_results(API_ORIGIN, REPO, PR_NUMBER, "op-validate-head-only", epoch, third_generation_id, [ValidationObservation(blocker_id=blocker_id, still_unmet=True, availability=ValidationAvailability.KNOWN, validation_seq=999)])
    allowance = snapshot.get_blocker_allowance(blocker_id)
    assert allowance is not None
    assert allowance.total_failed_count == 2, "a validation without recorded completion evidence must not be charged"
    third_generation = snapshot.get_generation(third_generation_id)
    assert third_generation is not None
    assert third_generation.lifecycle_state == GenerationLifecycleState.CONFIRMED_DELIVERED


def test_as003_old_evidence_becomes_visible_late(store: RepairAllowanceLedger) -> None:
    """AS-003: unavailable, baseline, and premature evidence cannot advance or settle a generation."""
    snapshot = store.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)
    blocker_id = "blk_as003"
    epoch = snapshot.epoch

    bundle = CorrectiveGenerationBundle(bundle_reference="bundle-as003", covered_blocker_ids=(blocker_id,), owning_identity="task-1", observed_baseline="sha-0")
    admission = store.admit_generation(API_ORIGIN, REPO, PR_NUMBER, "op-admit", epoch, bundle, open_blocker_ids=(blocker_id,))
    assert admission.admitted
    generation_id = admission.generation_id
    assert generation_id is not None
    epoch = admission.snapshot.epoch

    snapshot = store.record_delivery_outcome(API_ORIGIN, REPO, PR_NUMBER, "op-deliver", epoch, generation_id, DeliveryOutcome.CONFIRMED, "deliv-1")
    epoch = snapshot.epoch

    snapshot = store.record_completion_observation(API_ORIGIN, REPO, PR_NUMBER, "op-complete-unavail", epoch, generation_id, CompletionAvailability.UNAVAILABLE)
    epoch = snapshot.epoch
    generation = snapshot.get_generation(generation_id)
    assert generation is not None
    assert generation.lifecycle_state == GenerationLifecycleState.PENDING_COMPLETION
    assert generation.completion_seq is None

    snapshot = store.record_completion_observation(API_ORIGIN, REPO, PR_NUMBER, "op-complete-baseline", epoch, generation_id, CompletionAvailability.KNOWN, completion_seq=1, code_changed=True, causally_after_admission=False)
    epoch = snapshot.epoch
    generation = snapshot.get_generation(generation_id)
    assert generation is not None
    assert generation.lifecycle_state == GenerationLifecycleState.PENDING_COMPLETION, "baseline evidence must not advance the generation"
    assert generation.completion_seq is None

    snapshot = store.record_validation_results(API_ORIGIN, REPO, PR_NUMBER, "op-validate-early", epoch, generation_id, [ValidationObservation(blocker_id=blocker_id, still_unmet=True, availability=ValidationAvailability.KNOWN, validation_seq=3)])
    epoch = snapshot.epoch
    allowance = snapshot.get_blocker_allowance(blocker_id)
    assert allowance is not None
    assert allowance.total_failed_count == 0, "validation captured before any completion evidence cannot settle or charge"

    snapshot = store.record_completion_observation(API_ORIGIN, REPO, PR_NUMBER, "op-complete-real", epoch, generation_id, CompletionAvailability.KNOWN, completion_seq=10, code_changed=True, causally_after_admission=True)
    epoch = snapshot.epoch
    generation = snapshot.get_generation(generation_id)
    assert generation is not None
    assert generation.lifecycle_state == GenerationLifecycleState.PENDING_REVALIDATION
    assert generation.completion_seq == 10

    allowance = snapshot.get_blocker_allowance(blocker_id)
    assert allowance is not None
    assert allowance.total_failed_count == 0, "the earlier premature validation must not retroactively combine with the newer completion"

    snapshot = store.record_validation_results(API_ORIGIN, REPO, PR_NUMBER, "op-validate-late", epoch, generation_id, [ValidationObservation(blocker_id=blocker_id, still_unmet=True, availability=ValidationAvailability.KNOWN, validation_seq=11)])
    allowance = snapshot.get_blocker_allowance(blocker_id)
    assert allowance is not None
    assert allowance.total_failed_count == 1, "a later causally bound validation may settle the generation"


def test_as004_ambiguous_delivery_and_restart(store: RepairAllowanceLedger, db_path: Path) -> None:
    """AS-004: definite non-delivery retries the same generation; indeterminate delivery survives a restart."""
    snapshot = store.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)
    blocker_id = "blk_as004"
    epoch = snapshot.epoch

    bundle = CorrectiveGenerationBundle(bundle_reference="bundle-as004", covered_blocker_ids=(blocker_id,), owning_identity="task-1", observed_baseline="sha-0")
    admission = store.admit_generation(API_ORIGIN, REPO, PR_NUMBER, "op-admit", epoch, bundle, open_blocker_ids=(blocker_id,))
    assert admission.admitted
    generation_id = admission.generation_id
    assert generation_id is not None
    epoch = admission.snapshot.epoch

    # A proven, definite non-delivery (e.g. quota refusal) retries the same logical generation.
    snapshot = store.record_delivery_outcome(API_ORIGIN, REPO, PR_NUMBER, "op-deliver-refused", epoch, generation_id, DeliveryOutcome.DEFINITE_NON_DELIVERY, "deliv-refused")
    epoch = snapshot.epoch
    generation = snapshot.get_generation(generation_id)
    assert generation is not None
    assert generation.lifecycle_state == GenerationLifecycleState.RESERVED
    allowance = snapshot.get_blocker_allowance(blocker_id)
    assert allowance is not None
    assert allowance.total_failed_count == 0, "a proven non-delivery consumes no failure"

    # Retrying delivery loses the send response: outcome is indeterminate.
    snapshot = store.record_delivery_outcome(API_ORIGIN, REPO, PR_NUMBER, "op-deliver-lost", epoch, generation_id, DeliveryOutcome.INDETERMINATE, "deliv-lost")
    epoch = snapshot.epoch
    generation = snapshot.get_generation(generation_id)
    assert generation is not None
    assert generation.lifecycle_state == GenerationLifecycleState.INDETERMINATE
    assert len(generation.delivery_attempts) == 2

    restarted = RepairAllowanceLedger(db_path=db_path)
    restarted_snapshot = restarted.get_snapshot(API_ORIGIN, REPO, PR_NUMBER)
    restarted_generation = restarted_snapshot.get_generation(generation_id)
    assert restarted_generation is not None
    assert restarted_generation.lifecycle_state == GenerationLifecycleState.INDETERMINATE, "state must survive a process restart"

    with pytest.raises(InvalidOperatorGrantError):
        restarted.operator_grant(API_ORIGIN, REPO, PR_NUMBER, "op-grant-blocked", restarted_snapshot.epoch, target_blocker_ids=(blocker_id,))

    second_bundle = CorrectiveGenerationBundle(bundle_reference="bundle-as004-second", covered_blocker_ids=(blocker_id,), owning_identity="task-1", observed_baseline="sha-0")
    second_admission = restarted.admit_generation(API_ORIGIN, REPO, PR_NUMBER, "op-admit-second", restarted_snapshot.epoch, second_bundle, open_blocker_ids=(blocker_id,))
    assert not second_admission.admitted, "no speculative second generation may be admitted while the first remains indeterminate"
    allowance = second_admission.snapshot.get_blocker_allowance(blocker_id)
    assert allowance is not None
    assert allowance.total_failed_count == 0

    db_path.write_bytes(b"NOT A VALID SQLITE DATABASE FILE - CORRUPTED")
    corrupt_store = RepairAllowanceLedger(db_path=db_path)
    with pytest.raises(RepairAllowanceUnavailableError):
        corrupt_store.get_snapshot(API_ORIGIN, REPO, PR_NUMBER, require_retained_state=True)


def test_as005_mixed_outcomes_and_recurrence(store: RepairAllowanceLedger) -> None:
    """AS-005: independent per-blocker settlement; recurrence and wording changes preserve history."""
    snapshot = store.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)
    blocker_a, blocker_b = "blk_as005_a", "blk_as005_b"
    epoch = snapshot.epoch

    bundle = CorrectiveGenerationBundle(bundle_reference="bundle-as005", covered_blocker_ids=(blocker_a, blocker_b), owning_identity="task-1", observed_baseline="sha-0")
    admission = store.admit_generation(API_ORIGIN, REPO, PR_NUMBER, "op-admit", epoch, bundle, open_blocker_ids=(blocker_a, blocker_b))
    assert admission.admitted
    generation_id = admission.generation_id
    assert generation_id is not None
    epoch = admission.snapshot.epoch

    snapshot = store.record_delivery_outcome(API_ORIGIN, REPO, PR_NUMBER, "op-deliver", epoch, generation_id, DeliveryOutcome.CONFIRMED, "deliv-1")
    epoch = snapshot.epoch
    snapshot = store.record_completion_observation(API_ORIGIN, REPO, PR_NUMBER, "op-complete", epoch, generation_id, CompletionAvailability.KNOWN, completion_seq=5, code_changed=True)
    epoch = snapshot.epoch
    snapshot = store.record_validation_results(
        API_ORIGIN,
        REPO,
        PR_NUMBER,
        "op-validate",
        epoch,
        generation_id,
        [
            ValidationObservation(blocker_id=blocker_a, still_unmet=False, availability=ValidationAvailability.KNOWN, validation_seq=6),
            ValidationObservation(blocker_id=blocker_b, still_unmet=True, availability=ValidationAvailability.KNOWN, validation_seq=6),
        ],
    )
    epoch = snapshot.epoch

    allowance_a = snapshot.get_blocker_allowance(blocker_a)
    allowance_b = snapshot.get_blocker_allowance(blocker_b)
    assert allowance_a is not None and allowance_a.total_failed_count == 0
    assert allowance_b is not None and allowance_b.total_failed_count == 1
    generation = snapshot.get_generation(generation_id)
    assert generation is not None
    assert generation.lifecycle_state == GenerationLifecycleState.SETTLED
    settlement_a = generation.get_settlement(blocker_a)
    settlement_b = generation.get_settlement(blocker_b)
    assert settlement_a is not None and settlement_a.settlement == BlockerSettlement.CORRECTED
    assert settlement_b is not None and settlement_b.settlement == BlockerSettlement.STILL_OPEN

    # A recurs later: its allowance history is preserved, not reset.
    epoch, _recurrence_generation_id = _run_full_generation(store, epoch, blocker_a, "as005-recurrence", completion_seq=20)
    allowance_a_after = store.get_snapshot(API_ORIGIN, REPO, PR_NUMBER).get_blocker_allowance(blocker_a)
    assert allowance_a_after is not None
    assert allowance_a_after.total_failed_count == 1

    # A mere wording change for B's bundle does not reset or bypass its preserved allowance.
    reworded_bundle = CorrectiveGenerationBundle(bundle_reference="reworded-description-only", covered_blocker_ids=(blocker_b,), scope_revision="reworded-v2", owning_identity="task-1", observed_baseline="sha-1")
    reworded_admission = store.admit_generation(API_ORIGIN, REPO, PR_NUMBER, "op-admit-reworded", epoch, reworded_bundle, open_blocker_ids=(blocker_b,))
    assert reworded_admission.admitted
    allowance_b_after = reworded_admission.snapshot.get_blocker_allowance(blocker_b)
    assert allowance_b_after is not None
    assert allowance_b_after.total_failed_count == 1, "a wording change alone must not reset B's preserved allowance"


def _worker_admit(db_path_str: str, operation_id: str, expected_epoch: int, blocker_id: str, entry_barrier, release_barrier, out_queue, is_first: bool) -> None:  # type: ignore[no-untyped-def]
    from auto_coder.durable_repair_allowance import CorrectiveGenerationBundle as _Bundle
    from auto_coder.durable_repair_allowance import RepairAllowanceLedger as _Ledger
    from auto_coder.durable_repair_allowance import StaleRepairAllowanceEpochError as _StaleError

    worker_store = _Ledger(db_path=Path(db_path_str))
    entry_barrier.wait()
    if not is_first:
        release_barrier.wait()
    bundle = _Bundle(bundle_reference=f"bundle-{operation_id}", covered_blocker_ids=(blocker_id,), owning_identity="task-contend", observed_baseline="sha-0")
    try:
        result = worker_store.admit_generation(API_ORIGIN, REPO, PR_NUMBER, operation_id, expected_epoch, bundle, open_blocker_ids=(blocker_id,))
        if is_first:
            release_barrier.wait()
        out_queue.put(("ADMITTED" if result.admitted else "DENIED", result.generation_id))
    except _StaleError:
        out_queue.put(("STALE", None))
    except Exception as exc:  # pragma: no cover - defensive, surfaced via assertion below
        out_queue.put(("ERROR", str(exc)))


def test_as006_last_allowance_and_grant_contention(store: RepairAllowanceLedger, db_path: Path) -> None:
    """AS-006: contested admission yields exactly one generation; operator grants are idempotent and gated."""
    snapshot = store.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)
    blocker_id = "blk_as006"
    epoch = snapshot.epoch

    entry_barrier = multiprocessing.Barrier(2)
    release_barrier = multiprocessing.Barrier(2)
    out_queue: multiprocessing.Queue = multiprocessing.Queue()
    p1 = multiprocessing.Process(target=_worker_admit, args=(str(db_path), "op-contend-1", epoch, blocker_id, entry_barrier, release_barrier, out_queue, True))
    p2 = multiprocessing.Process(target=_worker_admit, args=(str(db_path), "op-contend-2", epoch, blocker_id, entry_barrier, release_barrier, out_queue, False))
    p1.start()
    p2.start()
    p1.join(timeout=10)
    p2.join(timeout=10)

    results = [out_queue.get(timeout=5), out_queue.get(timeout=5)]
    statuses = sorted(r[0] for r in results)
    assert statuses == ["ADMITTED", "STALE"], f"exactly one contended admission must win: {results}"

    snapshot = store.get_snapshot(API_ORIGIN, REPO, PR_NUMBER)
    outstanding = [g for g in snapshot.generations if g.is_outstanding()]
    assert len(outstanding) == 1, "only one logical generation is admitted under contention"
    generation_id = outstanding[0].generation_id
    epoch = snapshot.epoch

    # Drive the blocker to exhaustion using its default limit of three.
    for index, suffix in enumerate(("first", "second", "third")):
        if index > 0:
            bundle = CorrectiveGenerationBundle(bundle_reference=f"bundle-as006-{suffix}", covered_blocker_ids=(blocker_id,), owning_identity="task-1", observed_baseline="sha-0")
            admission = store.admit_generation(API_ORIGIN, REPO, PR_NUMBER, f"op-admit-{suffix}", epoch, bundle, open_blocker_ids=(blocker_id,))
            assert admission.admitted
            generation_id = admission.generation_id
            assert generation_id is not None
            epoch = admission.snapshot.epoch
        snapshot = store.record_delivery_outcome(API_ORIGIN, REPO, PR_NUMBER, f"op-deliver-{suffix}", epoch, generation_id, DeliveryOutcome.CONFIRMED, f"deliv-{suffix}")
        epoch = snapshot.epoch
        snapshot = store.record_completion_observation(API_ORIGIN, REPO, PR_NUMBER, f"op-complete-{suffix}", epoch, generation_id, CompletionAvailability.KNOWN, completion_seq=100 + index, code_changed=True)
        epoch = snapshot.epoch
        snapshot = store.record_validation_results(API_ORIGIN, REPO, PR_NUMBER, f"op-validate-{suffix}", epoch, generation_id, [ValidationObservation(blocker_id=blocker_id, still_unmet=True, availability=ValidationAvailability.KNOWN, validation_seq=100 + index)])
        epoch = snapshot.epoch

    allowance = snapshot.get_blocker_allowance(blocker_id)
    assert allowance is not None
    assert allowance.status == RepairAllowanceStatus.EXHAUSTED
    assert allowance.total_failed_count == 3

    first_grant = store.operator_grant(API_ORIGIN, REPO, PR_NUMBER, "op-grant-1", epoch, target_blocker_ids=(blocker_id,), new_limit=3)
    assert first_grant.granted
    assert first_grant.snapshot is not None
    epoch = first_grant.snapshot.epoch
    allowance_after_grant = first_grant.snapshot.get_blocker_allowance(blocker_id)
    assert allowance_after_grant is not None
    assert allowance_after_grant.status == RepairAllowanceStatus.ALLOWABLE
    assert allowance_after_grant.remaining == 3
    assert allowance_after_grant.total_failed_count == 3, "prior failure history is preserved across an explicit grant"

    replayed_grant = store.operator_grant(API_ORIGIN, REPO, PR_NUMBER, "op-grant-1", 999, target_blocker_ids=(blocker_id,), new_limit=3)
    assert replayed_grant.granted
    assert replayed_grant.granted_blocker_ids == first_grant.granted_blocker_ids

    with pytest.raises(RepairAllowanceIdempotencyConflictError):
        store.operator_grant(API_ORIGIN, REPO, PR_NUMBER, "op-grant-1", epoch, target_blocker_ids=(blocker_id,), new_limit=5)

    with pytest.raises(StaleRepairAllowanceEpochError):
        store.operator_grant(API_ORIGIN, REPO, PR_NUMBER, "op-grant-stale", 1, target_blocker_ids=(blocker_id,), new_limit=3)

    indeterminate_bundle = CorrectiveGenerationBundle(bundle_reference="bundle-as006-indeterminate", covered_blocker_ids=(blocker_id,), owning_identity="task-1", observed_baseline="sha-0")
    indeterminate_admission = store.admit_generation(API_ORIGIN, REPO, PR_NUMBER, "op-admit-indeterminate", epoch, indeterminate_bundle, open_blocker_ids=(blocker_id,))
    assert indeterminate_admission.admitted
    indeterminate_generation_id = indeterminate_admission.generation_id
    assert indeterminate_generation_id is not None
    epoch = indeterminate_admission.snapshot.epoch
    snapshot = store.record_delivery_outcome(API_ORIGIN, REPO, PR_NUMBER, "op-deliver-indeterminate", epoch, indeterminate_generation_id, DeliveryOutcome.INDETERMINATE, "deliv-indeterminate")
    epoch = snapshot.epoch

    with pytest.raises(InvalidOperatorGrantError):
        store.operator_grant(API_ORIGIN, REPO, PR_NUMBER, "op-grant-blocked", epoch, target_blocker_ids=(blocker_id,), new_limit=3)


def test_duplicate_and_newly_visible_terminal_records_are_not_double_charged(store: RepairAllowanceLedger) -> None:
    snapshot = store.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)
    blocker_id = "blk_dup"
    epoch = snapshot.epoch

    bundle = CorrectiveGenerationBundle(bundle_reference="bundle-dup", covered_blocker_ids=(blocker_id,), owning_identity="task-1", observed_baseline="sha-0")
    admission = store.admit_generation(API_ORIGIN, REPO, PR_NUMBER, "op-admit", epoch, bundle, open_blocker_ids=(blocker_id,))
    assert admission.admitted
    generation_id = admission.generation_id
    assert generation_id is not None
    epoch = admission.snapshot.epoch
    snapshot = store.record_delivery_outcome(API_ORIGIN, REPO, PR_NUMBER, "op-deliver", epoch, generation_id, DeliveryOutcome.CONFIRMED, "deliv-1")
    epoch = snapshot.epoch
    snapshot = store.record_completion_observation(API_ORIGIN, REPO, PR_NUMBER, "op-complete", epoch, generation_id, CompletionAvailability.KNOWN, completion_seq=5, code_changed=True)
    epoch = snapshot.epoch

    validations = [ValidationObservation(blocker_id=blocker_id, still_unmet=True, availability=ValidationAvailability.KNOWN, validation_seq=6)]
    snapshot = store.record_validation_results(API_ORIGIN, REPO, PR_NUMBER, "op-validate-dup", epoch, generation_id, validations)
    epoch = snapshot.epoch
    allowance = snapshot.get_blocker_allowance(blocker_id)
    assert allowance is not None and allowance.total_failed_count == 1

    # Replaying the identical operation ID with a wrong expected_epoch is still an idempotent no-op.
    replayed = store.record_validation_results(API_ORIGIN, REPO, PR_NUMBER, "op-validate-dup", 999, generation_id, validations)
    allowance = replayed.get_blocker_allowance(blocker_id)
    assert allowance is not None and allowance.total_failed_count == 1

    # A freshly-observed duplicate terminal record for the same settled generation+blocker,
    # submitted under a brand new operation ID, must still not consume a second attempt.
    newly_visible = store.record_validation_results(API_ORIGIN, REPO, PR_NUMBER, "op-validate-newly-visible", epoch, generation_id, validations)
    allowance = newly_visible.get_blocker_allowance(blocker_id)
    assert allowance is not None and allowance.total_failed_count == 1


def test_failed_persistence_does_not_appear_as_progress(store: RepairAllowanceLedger, db_path: Path) -> None:
    store.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)
    store._simulate_failure_before_commit = True
    bundle = CorrectiveGenerationBundle(bundle_reference="bundle-fail", covered_blocker_ids=("blk_fail",), owning_identity="task-1", observed_baseline="sha-0")
    with pytest.raises(RepairAllowancePersistenceError):
        store.admit_generation(API_ORIGIN, REPO, PR_NUMBER, "op-fail", 1, bundle, open_blocker_ids=("blk_fail",))

    reconstructed = RepairAllowanceLedger(db_path=db_path)
    snapshot = reconstructed.get_snapshot(API_ORIGIN, REPO, PR_NUMBER)
    assert len(snapshot.generations) == 0
    assert snapshot.epoch == 1

    with pytest.raises(RepairAllowanceUnavailableError):
        reconstructed.get_snapshot(API_ORIGIN, REPO, 999999, require_retained_state=True)


def test_orphaned_blocker_reference_is_reconciliation_required(store: RepairAllowanceLedger, db_path: Path) -> None:
    """REQ-011: ambiguous/pre-upgrade history is represented as RECONCILIATION_REQUIRED, never a fresh zero allowance."""
    snapshot = store.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)
    blocker_id = "blk_orphan"
    epoch = snapshot.epoch

    bundle = CorrectiveGenerationBundle(bundle_reference="bundle-orphan", covered_blocker_ids=(blocker_id,), owning_identity="task-1", observed_baseline="sha-0")
    admission = store.admit_generation(API_ORIGIN, REPO, PR_NUMBER, "op-admit", epoch, bundle, open_blocker_ids=(blocker_id,))
    assert admission.admitted

    conn = sqlite3.connect(str(db_path))
    conn.execute("DELETE FROM blocker_allowances WHERE blocker_id = ?", (blocker_id,))
    conn.commit()
    conn.close()

    reconciled_store = RepairAllowanceLedger(db_path=db_path)
    snapshot = reconciled_store.get_snapshot(API_ORIGIN, REPO, PR_NUMBER)
    allowance = snapshot.get_blocker_allowance(blocker_id)
    assert allowance is not None
    assert allowance.status == RepairAllowanceStatus.RECONCILIATION_REQUIRED
    assert allowance.historical_unknown is True


def test_admission_requires_a_positive_default_limit(store: RepairAllowanceLedger) -> None:
    store.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)
    bundle = CorrectiveGenerationBundle(bundle_reference="bundle-invalid-limit", covered_blocker_ids=("blk_invalid",), owning_identity="task-1", observed_baseline="sha-0")
    with pytest.raises(ValueError):
        store.admit_generation(API_ORIGIN, REPO, PR_NUMBER, "op-invalid-limit", 1, bundle, open_blocker_ids=("blk_invalid",), default_limit_for_new_blockers=0)


def test_immutable_snapshots_prevent_mutation() -> None:
    bundle = CorrectiveGenerationBundle(bundle_reference="bundle-immutable", covered_blocker_ids=("blk_immutable",))
    with pytest.raises(FrozenInstanceError):
        bundle.bundle_reference = "changed"  # type: ignore[misc]
