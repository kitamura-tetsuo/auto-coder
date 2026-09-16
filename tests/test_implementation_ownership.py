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

from auto_coder.automation_config import AutomationConfig, CandidateProcessingResult
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
