"""Production-boundary regressions for Issue #2061: bind Implementation
generations to durable production ownership acquisition.

Part 1 exercises :mod:`auto_coder.implementation_ownership` directly against
real ``ImplementationSlotRepository``/``IssueStageRoutingStore`` instances
(both file/DB backed) to pin down every REQ-001 through REQ-008 decision
branch cheaply and precisely.

Part 2 drives the real production admission boundary
(``AutomationEngine._process_single_candidate_unified`` and
``issue_processor.handle_stale_jules_issue_sessions``) so the REQ-010
regressions demonstrate the handoff crossing its actual supported origins
rather than directly constructing an owned routing record.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from auto_coder.automation_config import AutomationConfig, Candidate, CandidateProcessingResult
from auto_coder.automation_engine import AutomationEngine
from auto_coder.implementation_ownership import (
    OwnershipStartDecision,
    begin_implementation_ownership,
    confirm_implementation_ownership,
    evaluate_implementation_start,
)
from auto_coder.implementation_slots import (
    ImplementationOwner,
    ImplementationSlotRepository,
    ImplementationSlotUnavailable,
)
from auto_coder.issue_processor import handle_stale_jules_issue_sessions
from auto_coder.issue_stage_routing import IssueStageRoutingStore
from auto_coder.specification_analyzer import SpecificationAnalysisResult
from auto_coder.specification_validation_lifecycle import SpecificationValidationLifecycle
from tests.test_issue_stage_routing import REPO, _routing_engine

ISSUE = ImplementationOwner("issue", 1)


def _stores(tmp_path):
    slots = ImplementationSlotRepository(REPO, 1, tmp_path / "slots.json")
    routing = IssueStageRoutingStore(tmp_path / "routing.sqlite3")
    return slots, routing


# ---------------------------------------------------------------------------
# Part 1: adapter decision unit coverage (REQ-001 through REQ-008)
# ---------------------------------------------------------------------------


def test_fresh_owner_starts_new_and_confirm_tombstones_generation(tmp_path):
    """REQ-002, REQ-003: a fresh owner may start, and acquisition is the durable execution."""
    slots, routing = _stores(tmp_path)
    gate = evaluate_implementation_start(routing, slots, REPO, ISSUE, "g1")
    assert gate.decision is OwnershipStartDecision.START_NEW and gate.may_start
    begin_implementation_ownership(routing, REPO, ISSUE, gate)
    assert not routing.is_implementation_owned(REPO, 1, "g1")

    execution_id = slots.start_execution(ISSUE, generation="g1")
    assert execution_id is not None
    # Before the tombstone write, the execution alone already qualifies
    # (REQ-003): a hypothetical crash here would still be recoverable.
    assert slots.implementation_generation(ISSUE) == "g1"
    assert not routing.is_implementation_owned(REPO, 1, "g1")

    confirm_implementation_ownership(routing, REPO, ISSUE, "g1")
    assert routing.is_implementation_owned(REPO, 1, "g1")


def test_bare_idle_reservation_does_not_own_generation(tmp_path):
    """REQ-002: a bare capacity reservation with no execution/session/PR is not an owned start."""
    slots, routing = _stores(tmp_path)
    assert slots.reserve_new(ISSUE)
    assert not slots.has_qualifying_implementation_activity(ISSUE)
    gate = evaluate_implementation_start(routing, slots, REPO, ISSUE, "g1")
    assert gate.decision is OwnershipStartDecision.START_NEW


def test_finishing_execution_does_not_erase_binding_before_tombstone(tmp_path):
    """REQ-004: reclaiming/finishing a local execution must not lose the captured generation."""
    slots, routing = _stores(tmp_path)
    execution_id = slots.start_execution(ISSUE, generation="g1")
    assert execution_id is not None
    slots.finish_execution(ISSUE, execution_id)
    assert not slots.has_qualifying_implementation_activity(ISSUE)
    assert slots.implementation_generation(ISSUE) == "g1"

    # Recovery (e.g. after a restart) must recompute CONTINUE from the
    # captured binding, not treat the now-idle owner as a fresh attempt.
    gate = evaluate_implementation_start(routing, slots, REPO, ISSUE, "g1")
    assert gate.decision is OwnershipStartDecision.CONTINUE
    confirm_implementation_ownership(routing, REPO, ISSUE, "g1")
    assert routing.is_implementation_owned(REPO, 1, "g1")


def test_idle_bound_and_tombstoned_owner_refuses_duplicate_start(tmp_path):
    """REQ-006: once G is both durably tombstoned and fully idle (no retained
    execution/session/PR), a duplicate wake must not start G again.

    This is the case ``test_finishing_execution_does_not_erase_binding_before_tombstone``
    does not cover: there, the tombstone is not yet durable, so CONTINUE
    correctly re-establishes it. Once the tombstone *is* durable and nothing
    is left retained, the owner is idle rather than mid-attempt, and the
    same ``existing_generation == generation`` match must resolve to
    ALREADY_OWNED instead of CONTINUE.
    """
    slots, routing = _stores(tmp_path)
    execution_id = slots.start_execution(ISSUE, generation="g1")
    assert execution_id is not None
    confirm_implementation_ownership(routing, REPO, ISSUE, "g1")
    slots.finish_execution(ISSUE, execution_id)
    assert not slots.has_qualifying_implementation_activity(ISSUE)
    assert slots.implementation_generation(ISSUE) == "g1"
    assert routing.is_implementation_owned(REPO, 1, "g1")

    gate = evaluate_implementation_start(routing, slots, REPO, ISSUE, "g1")
    assert gate.decision is OwnershipStartDecision.ALREADY_OWNED
    assert not gate.may_start

    # Retained evidence for the same generation (e.g. a provider session
    # still in flight) makes it a genuine continuation once again.
    assert slots.record_provider_session(ISSUE, "still-running")
    resumed_gate = evaluate_implementation_start(routing, slots, REPO, ISSUE, "g1")
    assert resumed_gate.decision is OwnershipStartDecision.CONTINUE


def test_busy_with_different_generation_defers_without_owning_new_one(tmp_path):
    """REQ-006: retained evidence under an older generation defers a distinct new one."""
    slots, routing = _stores(tmp_path)
    execution_id = slots.start_execution(ISSUE, generation="g1")
    assert execution_id is not None
    assert slots.record_provider_session(ISSUE, "session-1")
    slots.finish_execution(ISSUE, execution_id)
    assert slots.has_qualifying_implementation_activity(ISSUE)  # provider session retained

    gate = evaluate_implementation_start(routing, slots, REPO, ISSUE, "g2")
    assert gate.decision is OwnershipStartDecision.BUSY_OTHER_GENERATION
    assert not gate.may_start
    begin_implementation_ownership(routing, REPO, ISSUE, gate)
    assert not routing.is_implementation_owned(REPO, 1, "g1")
    assert not routing.is_implementation_owned(REPO, 1, "g2")


def test_supersession_tombstones_old_generation_before_rebinding(tmp_path):
    """REQ-001, REQ-004, REQ-006, AS-003: G's fact survives being superseded by G2."""
    slots, routing = _stores(tmp_path)
    first_execution = slots.start_execution(ISSUE, generation="g1")
    assert first_execution is not None
    confirm_implementation_ownership(routing, REPO, ISSUE, "g1")
    slots.finish_execution(ISSUE, first_execution)
    assert not slots.has_qualifying_implementation_activity(ISSUE)

    gate = evaluate_implementation_start(routing, slots, REPO, ISSUE, "g2")
    assert gate.decision is OwnershipStartDecision.SUPERSEDE
    assert gate.superseded_generation == "g1"
    begin_implementation_ownership(routing, REPO, ISSUE, gate)
    assert routing.is_implementation_owned(REPO, 1, "g1")

    second_execution = slots.start_execution(ISSUE, generation="g2")
    assert second_execution is not None
    assert slots.implementation_generation(ISSUE) == "g2"
    confirm_implementation_ownership(routing, REPO, ISSUE, "g2")
    assert routing.is_implementation_owned(REPO, 1, "g2")

    # Exact reversion back to G1 must not be treated as a fresh start: the
    # local record now says G2, so routing's own recovered tombstone for G1
    # is the only thing standing between reversion and a duplicate start.
    reverted_gate = evaluate_implementation_start(routing, slots, REPO, ISSUE, "g1")
    assert reverted_gate.decision is OwnershipStartDecision.ALREADY_OWNED
    assert not reverted_gate.may_start


def test_missing_generation_binding_fails_closed_for_that_owner_only(tmp_path):
    """REQ-008: retained legacy evidence with no captured generation blocks all new starts."""
    slots, routing = _stores(tmp_path)
    # A record predating this feature (or otherwise corrupted): qualifying
    # evidence with no ``implementation_generation`` field at all.
    assert slots.reserve(ISSUE, implementation_pr=42)
    assert slots.has_qualifying_implementation_activity(ISSUE)
    assert slots.implementation_generation(ISSUE) is None

    gate = evaluate_implementation_start(routing, slots, REPO, ISSUE, "g1")
    assert gate.decision is OwnershipStartDecision.AMBIGUOUS_BINDING
    assert not gate.may_start

    # An unrelated owner is unaffected (target-scoped fail-closed).
    other = ImplementationOwner("issue", 2)
    other_gate = evaluate_implementation_start(routing, slots, REPO, other, "g1")
    assert other_gate.decision is OwnershipStartDecision.START_NEW


def test_unavailable_evidence_raises_instead_of_deciding(tmp_path):
    """AS-004: unreadable durable state must defer, never be treated as owned or not."""
    slots, routing = _stores(tmp_path)
    slots.storage_path.parent.mkdir(parents=True, exist_ok=True)
    slots.storage_path.write_text("not json", encoding="utf-8")
    with pytest.raises(ImplementationSlotUnavailable):
        evaluate_implementation_start(routing, slots, REPO, ISSUE, "g1")
    assert not routing.is_implementation_owned(REPO, 1, "g1")


def test_already_owned_after_full_release_stays_suppressed(tmp_path):
    """REQ-006: full release (PR merged, owner retired) does not clear the tombstone."""
    slots, routing = _stores(tmp_path)
    execution_id = slots.start_execution(ISSUE, generation="g1")
    assert execution_id is not None
    confirm_implementation_ownership(routing, REPO, ISSUE, "g1")
    slots.finish_execution(ISSUE, execution_id)
    assert slots.release_unbound_idle_owner(ISSUE)
    assert slots.implementation_generation(ISSUE) is None

    gate = evaluate_implementation_start(routing, slots, REPO, ISSUE, "g1")
    assert gate.decision is OwnershipStartDecision.ALREADY_OWNED
    assert not gate.may_start


# ---------------------------------------------------------------------------
# Part 2: production-boundary regressions
# ---------------------------------------------------------------------------


def _standalone_snapshot(number: int, created_at: str) -> dict:
    body = f"## Objective\n\nShip #{number}.\n\n## Requirements\n\nREQ-001: Ship #{number}."
    return {
        "id": number * 100 + 1,
        "number": number,
        "title": f"Standalone {number}",
        "body": body,
        "state": "open",
        "created_at": created_at,
        "labels": [{"name": "implementation-ready"}],
        "user": {"id": 1},
    }


def _ready_engine(tmp_path, monkeypatch, snapshots, config):
    """A ``_routing_engine`` with the real admission path restored (REQ-010)."""
    engine, github = _routing_engine(tmp_path, monkeypatch, snapshots, [], config)
    engine._process_single_candidate = AutomationEngine._process_single_candidate.__get__(engine)
    for number in snapshots:
        identity_body = snapshots[number]["body"]
        engine._specification_validators.setdefault(
            REPO,
            SpecificationValidationLifecycle(REPO, "policy", tmp_path / "ready.json", lambda *_args: SpecificationAnalysisResult("READY")),
        )
    engine.implementation_slots = ImplementationSlotRepository(REPO, 1, tmp_path / "slots.json")
    return engine, github


@pytest.mark.asyncio
async def test_duplicate_wake_never_dispatches_twice_for_the_same_generation(tmp_path, monkeypatch):
    """AS-005/REQ-006 at the real admission boundary: a second wake for the same
    unchanged Issue must not start implementation again once acquired."""
    created_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    snapshots = {1: _standalone_snapshot(1, created_at)}
    config = AutomationConfig(repo_name=REPO)
    config.issue_specification_validation = True
    config.issue_decomposition_validation = False
    engine, github = _ready_engine(tmp_path, monkeypatch, snapshots, config)
    reserved = MagicMock(return_value=CandidateProcessingResult(type="issue", number=1, success=True, actions=["dispatched"]))
    engine._process_single_candidate_reserved = reserved
    github.get_issue_comments_strict.return_value = []

    worker = asyncio.create_task(engine._worker_loop(REPO, 0, "issue"))
    await engine.invalidate_entity(REPO, "issue", 1)
    await asyncio.wait_for(engine.queue.join(), timeout=5)
    assert reserved.call_count == 1
    owner = ImplementationOwner("issue", 1)
    generation = engine.implementation_slots.implementation_generation(owner)
    assert generation is not None
    assert engine.issue_stage_routing.is_implementation_owned(REPO, 1, generation)

    # Duplicate wake for the exact same (unchanged) Issue: routing must
    # suppress a second production start rather than dispatching again.
    await engine.invalidate_entity(REPO, "issue", 1)
    await asyncio.wait_for(engine.queue.join(), timeout=5)
    assert reserved.call_count == 1

    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)


def test_duplicate_start_after_completed_execution_stays_suppressed_at_production_boundary(tmp_path, monkeypatch):
    """REQ-006 at the real admission boundary: once G's execution has
    finished normally (idle, bound, tombstoned -- not yet released), a
    duplicate admission for the exact same unchanged generation must not
    start a second execution.

    ``test_duplicate_wake_never_dispatches_twice_for_the_same_generation``
    above drives this through ordinary (non-retry) admission, where an
    earlier, unrelated "implementation ownership already exists" gate
    (matching ``validation_identity``) already refuses the second wake
    before ``evaluate_implementation_start`` is ever consulted -- so it does
    not, by itself, prove this Issue's own adapter decision is correct. This
    test isolates that decision by using the manual-retry origin (as the
    REQ-008 test above does) to bypass that earlier gate and reach the
    adapter directly with the idle-but-bound-and-tombstoned state.
    """
    created_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    snapshot = _standalone_snapshot(1, created_at)
    config = AutomationConfig(repo_name=REPO)
    config.issue_specification_validation = True
    config.issue_decomposition_validation = False
    engine, github = _ready_engine(tmp_path, monkeypatch, {1: snapshot}, config)
    engine._process_single_candidate_reserved = MagicMock(return_value=CandidateProcessingResult(type="issue", number=1, success=True, actions=["dispatched"]))
    github.get_issue_comments_strict.return_value = []
    owner = ImplementationOwner("issue", 1)

    # Admit G to owned (execution + tombstone), then finish the execution
    # without releasing the owner: retained binding, no qualifying activity,
    # tombstone durable.
    generation = engine._compute_implementation_generation(REPO, snapshot, None)
    execution_id = engine.implementation_slots.start_execution(owner, generation=generation)
    assert execution_id is not None
    engine.issue_stage_routing.record_implementation_owned(REPO, 1, generation)
    engine.implementation_slots.finish_execution(owner, execution_id)
    assert engine.implementation_slots.active_execution_ids(owner) == ()
    assert not engine.implementation_slots.has_qualifying_implementation_activity(owner)

    candidate = Candidate(type="issue", data=dict(snapshot), priority=0)
    result = engine._process_single_candidate_unified(REPO, candidate, engine.config, explicit_only=True, force=True, retry=True, origin="explicit-single-target")

    engine._process_single_candidate_reserved.assert_not_called()
    assert result.actions == ["Skipped - Implementation generation already has a durable production start"]
    assert engine.implementation_slots.active_execution_ids(owner) == ()
    assert engine.implementation_slots.implementation_generation(owner) == generation
    assert engine.issue_stage_routing.is_implementation_owned(REPO, 1, generation)


def test_malformed_generation_binding_fails_closed_at_production_boundary_target_scoped(tmp_path, monkeypatch):
    """REQ-008/AS-007 at the real admission boundary: retained implementation
    evidence with a malformed (non-string) generation binding blocks a new
    start for that owner only -- while an unrelated owner is unaffected.

    A pre-existing owner record for the *same* Issue is already refused by
    an earlier, unrelated "implementation ownership already exists" gate in
    ``_process_single_candidate_unified_impl`` unless the caller is an
    explicit manual retry (``explicit_only``+``force``+``retry``), so this
    drives admission the same way ``test_manual_retry_*`` in
    ``test_specification_validation_lifecycle.py`` does -- the real supported
    "retry/resumption" origin from REQ-009 -- to actually reach the
    generation-ownership adapter this Issue adds.
    """
    created_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    snapshot1 = _standalone_snapshot(1, created_at)
    snapshot2 = _standalone_snapshot(2, created_at)
    config = AutomationConfig(repo_name=REPO)
    config.issue_specification_validation = True
    config.issue_decomposition_validation = False
    engine, github = _ready_engine(tmp_path, monkeypatch, {1: snapshot1, 2: snapshot2}, config)
    # Capacity for two independent owners, so #2's admission below is never
    # blocked by #1's occupied slot -- this test is about generation-binding
    # fail-closure, not normal capacity contention.
    engine.implementation_slots = ImplementationSlotRepository(REPO, 2, tmp_path / "slots.json")
    engine._process_single_candidate_reserved = MagicMock(return_value=CandidateProcessingResult(type="issue", number=0, success=True, actions=["dispatched"]))
    github.get_issue_comments_strict.return_value = []

    owner1 = ImplementationOwner("issue", 1)
    # A record with retained implementation-mutating evidence (an
    # implementation PR) but a malformed, non-string generation binding --
    # the shape REQ-008 requires production admission to refuse rather than
    # guess.
    assert engine.implementation_slots.reserve(owner1, implementation_pr=999)
    slots_path = engine.implementation_slots.storage_path
    raw_state = json.loads(slots_path.read_text())
    raw_state[owner1.key]["implementation_generation"] = 12345
    slots_path.write_text(json.dumps(raw_state))

    candidate1 = Candidate(type="issue", data=dict(snapshot1), priority=0)
    result1 = engine._process_single_candidate_unified(REPO, candidate1, engine.config, explicit_only=True, force=True, retry=True, origin="explicit-single-target")

    engine._process_single_candidate_reserved.assert_not_called()
    assert result1.error is not None
    assert not result1.success
    assert engine.implementation_slots.active_execution_ids(owner1) == ()
    # The malformed value is untouched -- never coerced or silently rebound
    # to whatever generation admission just computed as "current".
    unchanged_state = json.loads(slots_path.read_text())
    assert unchanged_state[owner1.key]["implementation_generation"] == 12345

    # An unrelated owner (#2, no pre-existing record) is unaffected: it
    # starts and is durably owned normally through ordinary admission.
    candidate2 = Candidate(type="issue", data=dict(snapshot2), priority=0)
    result2 = engine._process_single_candidate_unified(REPO, candidate2, engine.config, origin="worker")
    assert result2.success
    owner2 = ImplementationOwner("issue", 2)
    generation2 = engine.implementation_slots.implementation_generation(owner2)
    assert generation2 is not None
    assert engine.issue_stage_routing.is_implementation_owned(REPO, 2, generation2)


def test_ambiguous_provider_outcome_resolves_three_ways_at_production_boundary(tmp_path, monkeypatch):
    """REQ-003 through REQ-005/AS-004 at the real admission boundary.

    On the current production Issue-implementation path, ownership is
    already acquired at the local-execution boundary before any provider
    call is attempted (REQ-003), so an "ambiguous transport" outcome is
    exercised here as three distinct authoritative states that a later,
    re-entrant admission for the same generation discovers in the durable
    ``ImplementationSlotRepository`` evidence -- the same evidence a real
    reconciliation pass reads to decide the identical three outcomes. As in
    the REQ-008 test above, reaching this owner's already-retained evidence
    through the real admission boundary requires the manual-retry origin.
    """
    created_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    snapshot = _standalone_snapshot(1, created_at)
    config = AutomationConfig(repo_name=REPO)
    config.issue_specification_validation = True
    config.issue_decomposition_validation = False
    owner = ImplementationOwner("issue", 1)

    # (a) Confirmed acquisition: a retained provider session for G survives
    # even though the local execution that first acquired it is gone; a
    # retry is recognized as a continuation of G, not a fresh routing start.
    engine_a, github_a = _ready_engine(tmp_path / "a", monkeypatch, {1: dict(snapshot)}, config)
    reserved_a = MagicMock(return_value=CandidateProcessingResult(type="issue", number=1, success=True, actions=["dispatched"]))
    engine_a._process_single_candidate_reserved = reserved_a
    github_a.get_issue_comments_strict.return_value = []

    # Cross the real production acquisition boundary first (ordinary
    # admission) rather than directly constructing the owned routing record.
    initial_candidate = Candidate(type="issue", data=dict(snapshot), priority=0)
    initial_result = engine_a._process_single_candidate_unified(REPO, initial_candidate, engine_a.config, origin="worker")
    assert initial_result.success
    generation = engine_a.implementation_slots.implementation_generation(owner)
    assert generation is not None
    assert engine_a.issue_stage_routing.is_implementation_owned(REPO, 1, generation)

    # Simulate the provider now retaining this attempt after the local
    # execution that originally acquired it has finished.
    assert engine_a.implementation_slots.record_provider_session(owner, "ambiguous-session")
    for execution_id in engine_a.implementation_slots.active_execution_ids(owner):
        engine_a.implementation_slots.finish_execution(owner, execution_id)
    assert engine_a.implementation_slots.active_execution_ids(owner) == ()

    candidate_a = Candidate(type="issue", data=dict(snapshot), priority=0)
    result_a = engine_a._process_single_candidate_unified(REPO, candidate_a, engine_a.config, explicit_only=True, force=True, retry=True, origin="explicit-single-target")
    assert result_a.success
    assert reserved_a.call_count == 2  # the initial dispatch, plus this legitimate continuation
    # G's binding and tombstone are unchanged -- recognized as the same
    # already-owned attempt, never rebound or duplicated as a new G2.
    assert engine_a.implementation_slots.implementation_generation(owner) == generation
    assert engine_a.issue_stage_routing.is_implementation_owned(REPO, 1, generation)

    # (b) Confirmed non-acquisition: nothing was ever actually retained for
    # this owner (a bare idle reservation only, since released) -- G is
    # fully retryable through ordinary (non-retry) admission.
    engine_b, github_b = _ready_engine(tmp_path / "b", monkeypatch, {1: dict(snapshot)}, config)
    reserved_b = MagicMock(return_value=CandidateProcessingResult(type="issue", number=1, success=True, actions=["dispatched"]))
    engine_b._process_single_candidate_reserved = reserved_b
    github_b.get_issue_comments_strict.return_value = []
    assert engine_b.implementation_slots.reserve_new(owner)
    assert engine_b.implementation_slots.release_unbound_idle_owner(owner)

    candidate_b = Candidate(type="issue", data=dict(snapshot), priority=0)
    result_b = engine_b._process_single_candidate_unified(REPO, candidate_b, engine_b.config, origin="worker")
    assert result_b.success
    assert reserved_b.call_count == 1
    generation_b = engine_b.implementation_slots.implementation_generation(owner)
    assert generation_b is not None
    assert engine_b.issue_stage_routing.is_implementation_owned(REPO, 1, generation_b)

    # (c) Evidence unavailable: an unreadable generation binding for this
    # owner defers both a new start and a tombstone rather than treating
    # absent/unreadable evidence as either oracle. (A whole-file JSON
    # corruption is deliberately not used here: it would also break the
    # unrelated, pre-existing ``has_provider_sessions`` read this same
    # admission path performs earlier, which is outside this Issue's scope.)
    engine_c, github_c = _ready_engine(tmp_path / "c", monkeypatch, {1: dict(snapshot)}, config)
    reserved_c = MagicMock(return_value=CandidateProcessingResult(type="issue", number=1, success=True, actions=["dispatched"]))
    engine_c._process_single_candidate_reserved = reserved_c
    github_c.get_issue_comments_strict.return_value = []
    assert engine_c.implementation_slots.reserve(owner, implementation_pr=999)
    slots_path_c = engine_c.implementation_slots.storage_path
    raw_state_c = json.loads(slots_path_c.read_text())
    raw_state_c[owner.key]["implementation_generation"] = 12345
    slots_path_c.write_text(json.dumps(raw_state_c))

    candidate_c = Candidate(type="issue", data=dict(snapshot), priority=0)
    result_c = engine_c._process_single_candidate_unified(REPO, candidate_c, engine_c.config, explicit_only=True, force=True, retry=True, origin="explicit-single-target")
    assert not result_c.success
    assert result_c.error is not None
    assert reserved_c.call_count == 0
    assert not engine_c.issue_stage_routing.is_implementation_owned(REPO, 1, generation)


@pytest.mark.asyncio
async def test_capacity_deferred_generation_remains_retryable_until_acquired(tmp_path, monkeypatch):
    """AS-001 at the real admission boundary: deferral before any execution is
    durable leaves the generation fully retryable; it becomes owned once
    capacity actually admits it."""
    created_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    snapshots = {1: _standalone_snapshot(1, created_at)}
    config = AutomationConfig(repo_name=REPO)
    config.issue_specification_validation = True
    config.issue_decomposition_validation = False
    engine, github = _ready_engine(tmp_path, monkeypatch, snapshots, config)
    engine._process_single_candidate_reserved = MagicMock(return_value=CandidateProcessingResult(type="issue", number=1, success=True, actions=["dispatched"]))
    github.get_issue_comments_strict.return_value = []

    # Occupy the single normal slot with an unrelated owner first.
    other_owner = ImplementationOwner("issue", 99)
    assert engine.implementation_slots.reserve_new(other_owner)

    worker = asyncio.create_task(engine._worker_loop(REPO, 0, "issue"))
    await engine.invalidate_entity(REPO, "issue", 1)
    await asyncio.wait_for(engine.queue.join(), timeout=5)

    owner = ImplementationOwner("issue", 1)
    assert engine.implementation_slots.implementation_generation(owner) is None
    expected_generation = None
    pending = engine.issue_stage_routing.pending(REPO, "implementation")
    assert len(pending) == 1
    expected_generation = pending[0].generation
    assert not engine.issue_stage_routing.is_implementation_owned(REPO, 1, expected_generation)

    # Free the slot and let capacity refill (simulated here as another
    # invalidation, matching how the real capacity-refill loop re-enters
    # this exact admission boundary) admit the still-retryable generation.
    assert engine.implementation_slots.release_unbound_idle_owner(other_owner)
    await engine.invalidate_entity(REPO, "issue", 1)
    await asyncio.wait_for(engine.queue.join(), timeout=5)
    assert engine.implementation_slots.implementation_generation(owner) == expected_generation
    assert engine.issue_stage_routing.is_implementation_owned(REPO, 1, expected_generation)

    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.asyncio
async def test_family_supersession_and_exact_reversion_do_not_rebind_old_ownership(tmp_path, monkeypatch):
    """AS-003 at the real admission boundary: editing then exactly reverting a
    child's family must not resurrect its already-owned generation."""
    created_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    parent_body = "## Objective\n\nCoordinate delivery."
    child_body = "Parent-Issue: #10\n\n## Objective\n\nShip child.\n\n## Requirements\n\nREQ-001: Ship child."
    snapshots = {
        10: {"id": 100, "number": 10, "title": "P", "body": parent_body, "state": "open", "created_at": created_at, "labels": [{"name": "implementation-ready"}]},
        11: {"id": 110, "number": 11, "title": "A", "body": child_body, "state": "open", "created_at": created_at, "labels": [], "parent_issue_number": 10},
    }
    config = AutomationConfig(repo_name=REPO)
    config.issue_specification_validation = True
    config.issue_decomposition_validation = False
    engine, github = _routing_engine(tmp_path, monkeypatch, snapshots, [11], config)
    engine._process_single_candidate = AutomationEngine._process_single_candidate.__get__(engine)
    engine._specification_validators[REPO] = SpecificationValidationLifecycle(REPO, "policy", tmp_path / "ready.json", lambda *_args: SpecificationAnalysisResult("READY"))
    engine.implementation_slots = ImplementationSlotRepository(REPO, 1, tmp_path / "slots.json")
    engine._process_single_candidate_reserved = MagicMock(return_value=CandidateProcessingResult(type="issue", number=11, success=True, actions=["dispatched"]))
    github.get_issue_comments_strict.return_value = []

    worker = asyncio.create_task(engine._worker_loop(REPO, 0, "issue"))
    await engine.invalidate_entity(REPO, "issue", 11)
    await asyncio.wait_for(engine.queue.join(), timeout=5)
    owner = ImplementationOwner("issue", 11)
    original_generation = engine.implementation_slots.implementation_generation(owner)
    assert original_generation is not None
    assert engine.issue_stage_routing.is_implementation_owned(REPO, 11, original_generation)

    # Release the local execution/PR evidence (as a real completed/abandoned
    # attempt would) while keeping the routing tombstone, then edit the
    # child so its Implementation generation changes.
    assert engine.implementation_slots.release_unbound_idle_owner(owner)
    snapshots[11] = {**snapshots[11], "title": "A - edited"}
    await engine.invalidate_entity(REPO, "issue", 11)
    await asyncio.wait_for(engine.queue.join(), timeout=5)
    superseded_generation = engine.implementation_slots.implementation_generation(owner)
    assert superseded_generation is not None and superseded_generation != original_generation
    assert engine.issue_stage_routing.is_implementation_owned(REPO, 11, original_generation)
    assert engine.issue_stage_routing.is_implementation_owned(REPO, 11, superseded_generation)

    # Exact reversion: restore the original title and re-release evidence.
    assert engine.implementation_slots.release_unbound_idle_owner(owner)
    snapshots[11] = {**snapshots[11], "title": "A"}
    await engine.invalidate_entity(REPO, "issue", 11)
    await asyncio.wait_for(engine.queue.join(), timeout=5)
    # The reverted generation is still suppressed by its recovered tombstone;
    # the owner record must not have been rebound to the old generation.
    assert engine.implementation_slots.implementation_generation(owner) != original_generation

    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.asyncio
async def test_stale_jules_recovery_continues_same_generation_without_new_routing_start(tmp_path, monkeypatch):
    """AS-005/REQ-007/REQ-009: stale-provider recovery is a continuation of the
    already-acquired attempt, never a second routing start for the same
    generation."""
    created_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    snapshots = {1: _standalone_snapshot(1, created_at)}
    config = AutomationConfig(repo_name=REPO)
    config.issue_specification_validation = True
    config.issue_decomposition_validation = False
    engine, github = _ready_engine(tmp_path, monkeypatch, snapshots, config)
    engine._process_single_candidate_reserved = MagicMock(return_value=CandidateProcessingResult(type="issue", number=1, success=True, actions=["dispatched"]))
    github.get_issue_comments_strict.return_value = []

    worker = asyncio.create_task(engine._worker_loop(REPO, 0, "issue"))
    await engine.invalidate_entity(REPO, "issue", 1)
    await asyncio.wait_for(engine.queue.join(), timeout=5)
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)

    owner = ImplementationOwner("issue", 1)
    generation = engine.implementation_slots.implementation_generation(owner)
    assert generation is not None
    assert engine.issue_stage_routing.is_implementation_owned(REPO, 1, generation)
    assert engine.implementation_slots.record_provider_session(owner, "jules-session-1")
    for execution_id in engine.implementation_slots.active_execution_ids(owner):
        engine.implementation_slots.finish_execution(owner, execution_id)
    # This owner record now has no execution left, only a retained provider
    # session, mirroring what a real Jules dispatch leaves behind.

    stale_session = {
        "name": f"sessions/jules-session-1",
        "createTime": (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat(),
    }
    jules_client = MagicMock()
    jules_client.list_sessions.return_value = [stale_session]
    cloud_manager = MagicMock()
    cloud_manager.get_issue_by_session.return_value = 1
    monkeypatch.setattr("auto_coder.issue_processor.JulesClient", lambda: jules_client)
    monkeypatch.setattr("auto_coder.issue_processor.CloudManager", lambda _repo: cloud_manager)
    monkeypatch.setattr("auto_coder.issue_processor.resolve_authoritative_item_type", lambda *_a, **_k: "issue")
    monkeypatch.setattr("auto_coder.issue_processor.is_session_stopped", lambda _sid: False)
    monkeypatch.setattr("auto_coder.issue_processor.get_session_pull_request", lambda _session: None)
    monkeypatch.setattr("auto_coder.issue_processor._stop_jules_session_for_issue", lambda *_a, **_k: True)
    monkeypatch.setattr("auto_coder.issue_processor._take_issue_actions", lambda *_a, **_k: ["fallback dispatched"])
    github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, _number: dict(snapshots[1])
    github.get_issue.side_effect = lambda _repo, _number: dict(snapshots[1])
    github.get_issue_details.side_effect = lambda issue: dict(issue)
    github.has_linked_pr.return_value = False

    result = handle_stale_jules_issue_sessions(
        REPO,
        config,
        github,
        implementation_slots=engine.implementation_slots,
        authorize_dispatch=lambda _repo, _number, current: dict(current),
        routing=engine.issue_stage_routing,
    )
    assert result.issue_numbers == [1]
    # Still the same generation, still owned exactly once: no new routing
    # arrival/tombstone was fabricated for a "new" attempt.
    assert engine.implementation_slots.implementation_generation(owner) == generation
    assert engine.issue_stage_routing.is_implementation_owned(REPO, 1, generation)
    assert engine.issue_stage_routing.pending(REPO, "implementation") == ()
