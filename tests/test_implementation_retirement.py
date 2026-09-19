"""Acceptance tests for safe retirement of terminal PR-backed implementation slots (Issue #2146).

Covers AS-001 through AS-006 and REQ-001 through REQ-009.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from auto_coder.implementation_ownership import (
    OwnershipStartDecision,
    evaluate_implementation_start,
)
from auto_coder.implementation_retirement import (
    ContinuingObligations,
    ExecutionTerminalState,
    ImplementationPRObservation,
    ImplementationRetirementObservation,
    LocalExecutionObservation,
    ProviderSessionObservation,
    PRTerminalState,
    RetirementResult,
    RetirementStatus,
    SessionTerminalState,
    evaluate_retirement_predicate,
    retire_implementation_slot,
)
from auto_coder.implementation_slots import (
    ImplementationOwner,
    ImplementationSlotRepository,
    ImplementationSlotSnapshot,
    ImplementationSlotUnavailable,
)
from auto_coder.issue_stage_routing import IssueStageRoutingStore

REPO = "kitamura-tetsuo/auto-coder"
ISSUE_100 = ImplementationOwner("issue", 100)


def _setup_stores(tmp_path: Path, limit: int = 1) -> tuple[ImplementationSlotRepository, IssueStageRoutingStore]:
    slots = ImplementationSlotRepository(REPO, limit, tmp_path / "slots.json")
    routing = IssueStageRoutingStore(tmp_path / "routing.sqlite3")
    return slots, routing


# ---------------------------------------------------------------------------
# AS-001: Open Issue with terminal published work actually releases
# Covers REQ-001 through REQ-004, REQ-006, REQ-009.
# ---------------------------------------------------------------------------


def test_as001_open_issue_with_terminal_closed_pr_releases(tmp_path):
    """AS-001: open Issue with closed/unmerged PR and ended Jules session releases capacity."""
    slots, routing = _setup_stores(tmp_path, limit=1)

    # 1. Create reservation through real execution/admission writer
    execution_id = slots.start_execution(ISSUE_100, generation="gen-100")
    assert execution_id is not None
    # 2. Register implementation PR and Jules session through real membership writers
    assert slots.record_implementation_pr(ISSUE_100, 201) is True
    assert slots.record_provider_session(ISSUE_100, "session-jules-1") is True
    # 3. Finish the local execution
    slots.finish_execution(ISSUE_100, execution_id)
    # Captured generation remains preserved
    assert slots.implementation_generation(ISSUE_100) == "gen-100"

    # Capture store incarnation and revision
    incarnation = slots.owner_incarnation(ISSUE_100)
    assert incarnation is not None
    revision = slots.owner_activity_revision(ISSUE_100)
    assert revision is not None

    # Supply complete normalized evidence: PR closed/unmerged, session ended, no continuation
    observation = ImplementationRetirementObservation(
        repository=REPO,
        owner=ISSUE_100,
        reservation_incarnation=incarnation,
        activity_revision=revision,
        implementation_prs=(ImplementationPRObservation(number=201, state=PRTerminalState.CLOSED, merged=False),),
        provider_sessions=(
            ProviderSessionObservation(
                session_id="session-jules-1",
                provider="jules",
                state=SessionTerminalState.ENDED,
                latest_activity_ended=True,
            ),
        ),
        local_executions=(LocalExecutionObservation(execution_id=execution_id, state=ExecutionTerminalState.ENDED),),
        continuing_obligations=ContinuingObligations(),
    )

    # Initial occupancy: 1/1 used, 0 available
    assert slots.available_normal_slots() == 0
    assert slots.active_owners() == (ISSUE_100,)

    # 4. Invoke the real retirement transaction
    result = slots.retire_owner(observation, routing)
    assert result.status is RetirementStatus.RELEASED
    assert result.is_released

    # 5. A newly constructed repository must see no active reservation for that incarnation,
    # lower actual usage, available capacity when usage is below the limit,
    # retained historical associations, and preserved acquired-start evidence.
    new_slots = ImplementationSlotRepository(REPO, 1, tmp_path / "slots.json")
    assert new_slots.active_owners() == ()
    assert new_slots.available_normal_slots() == 1
    snapshot = new_slots.snapshot()
    assert isinstance(snapshot, ImplementationSlotSnapshot)
    assert snapshot.normal_usage == 0
    assert snapshot.normal_available == 1
    assert snapshot.owners == ()

    # Historical associations preserved
    assert new_slots.has_retired_associations(ISSUE_100)
    assert new_slots.has_retired_pr(201)
    assert new_slots.has_retired_session("session-jules-1")
    assert new_slots.has_retired_generation("gen-100")
    records = new_slots.retired_records()
    assert len(records) == 1
    assert records[0].incarnation == incarnation
    assert records[0].implementation_prs == (201,)
    assert records[0].provider_sessions == ("session-jules-1",)
    assert records[0].generation == "gen-100"

    # Preserved acquired-start evidence in routing store
    assert routing.is_implementation_owned(REPO, 100, "gen-100")

    # Repeating retirement is idempotent
    repeat_result = slots.retire_owner(observation, routing)
    assert repeat_result.status is RetirementStatus.RELEASED


def test_as001_variant_merged_pr_releases(tmp_path):
    """AS-001 variant: merged PR evidence also releases."""
    slots, routing = _setup_stores(tmp_path, limit=1)
    execution_id = slots.start_execution(ISSUE_100, generation="gen-merged")
    assert slots.record_implementation_pr(ISSUE_100, 202) is True
    slots.finish_execution(ISSUE_100, execution_id)

    incarnation = slots.owner_incarnation(ISSUE_100)
    revision = slots.owner_activity_revision(ISSUE_100)
    assert incarnation is not None and revision is not None

    observation = ImplementationRetirementObservation(
        repository=REPO,
        owner=ISSUE_100,
        reservation_incarnation=incarnation,
        activity_revision=revision,
        implementation_prs=(ImplementationPRObservation(number=202, state=PRTerminalState.MERGED, merged=True),),
        local_executions=(LocalExecutionObservation(execution_id=execution_id, state=ExecutionTerminalState.ENDED),),
    )

    result = slots.retire_owner(observation, routing)
    assert result.status is RetirementStatus.RELEASED
    assert slots.available_normal_slots() == 1
    assert slots.has_retired_pr(202)
    assert routing.is_implementation_owned(REPO, 100, "gen-merged")


def test_as001_variant_local_only_work_releases(tmp_path):
    """AS-001 variant: local-only PR-backed work (no Jules session) releases."""
    slots, routing = _setup_stores(tmp_path, limit=1)
    execution_id = slots.start_execution(ISSUE_100, generation="gen-local")
    assert slots.record_implementation_pr(ISSUE_100, 203) is True
    slots.finish_execution(ISSUE_100, execution_id)

    incarnation = slots.owner_incarnation(ISSUE_100)
    revision = slots.owner_activity_revision(ISSUE_100)
    assert incarnation is not None and revision is not None

    observation = ImplementationRetirementObservation(
        repository=REPO,
        owner=ISSUE_100,
        reservation_incarnation=incarnation,
        activity_revision=revision,
        implementation_prs=(ImplementationPRObservation(number=203, state=PRTerminalState.CLOSED, merged=False),),
        provider_sessions=(),
        local_executions=(LocalExecutionObservation(execution_id=execution_id, state=ExecutionTerminalState.ENDED),),
    )

    result = slots.retire_owner(observation, routing)
    assert result.status is RetirementStatus.RELEASED
    assert slots.available_normal_slots() == 1
    assert slots.has_retired_pr(203)
    assert routing.is_implementation_owned(REPO, 100, "gen-local")


# ---------------------------------------------------------------------------
# AS-002: Each independent blocker defeats apparent terminality
# Covers REQ-001 through REQ-003.
# ---------------------------------------------------------------------------


def test_as002_each_independent_blocker_defeats_terminality(tmp_path):
    """AS-002: independently test each blocker; assert ACTIVE/UNKNOWN, unchanged occupancy, member-specific reason."""
    slots, routing = _setup_stores(tmp_path, limit=2)

    # Base setup: active reservation for ISSUE_100 with PR 201 and session-1
    execution_id = slots.start_execution(ISSUE_100, generation="gen-100")
    assert slots.record_implementation_pr(ISSUE_100, 201) is True
    assert slots.record_provider_session(ISSUE_100, "session-1") is True
    slots.finish_execution(ISSUE_100, execution_id)

    incarnation = slots.owner_incarnation(ISSUE_100)
    revision = slots.owner_activity_revision(ISSUE_100)
    assert incarnation is not None and revision is not None

    # Helper to assert blocker outcomes
    def assert_blocked(obs, expected_status, expected_member_substr):
        occupancy_before = slots.available_normal_slots()
        owners_before = slots.active_owners()
        res = slots.retire_owner(obs, routing)
        assert res.status is expected_status
        assert any(expected_member_substr in m for m in res.responsible_members) or (res.diagnostic and expected_member_substr in res.diagnostic)
        # Unchanged occupancy
        assert slots.available_normal_slots() == occupancy_before
        assert slots.active_owners() == owners_before

    # 1. Another open PR
    assert slots.record_implementation_pr(ISSUE_100, 202) is True
    rev_with_pr2 = slots.owner_activity_revision(ISSUE_100)
    obs_open_pr = ImplementationRetirementObservation(
        repository=REPO,
        owner=ISSUE_100,
        reservation_incarnation=incarnation,
        activity_revision=rev_with_pr2,
        implementation_prs=(
            ImplementationPRObservation(number=201, state=PRTerminalState.CLOSED),
            ImplementationPRObservation(number=202, state=PRTerminalState.OPEN),
        ),
        provider_sessions=(ProviderSessionObservation(session_id="session-1", provider="jules", state=SessionTerminalState.ENDED),),
        local_executions=(LocalExecutionObservation(execution_id=execution_id, state=ExecutionTerminalState.ENDED),),
    )
    assert_blocked(obs_open_pr, RetirementStatus.RETAINED_ACTIVE, "pr:202")

    # 2. Second nonterminal session
    assert slots.record_provider_session(ISSUE_100, "session-2") is True
    rev_with_s2 = slots.owner_activity_revision(ISSUE_100)
    obs_active_session = ImplementationRetirementObservation(
        repository=REPO,
        owner=ISSUE_100,
        reservation_incarnation=incarnation,
        activity_revision=rev_with_s2,
        implementation_prs=(
            ImplementationPRObservation(number=201, state=PRTerminalState.CLOSED),
            ImplementationPRObservation(number=202, state=PRTerminalState.CLOSED),
        ),
        provider_sessions=(
            ProviderSessionObservation(session_id="session-1", provider="jules", state=SessionTerminalState.ENDED),
            ProviderSessionObservation(session_id="session-2", provider="jules", state=SessionTerminalState.ACTIVE),
        ),
        local_executions=(LocalExecutionObservation(execution_id=execution_id, state=ExecutionTerminalState.ENDED),),
    )
    assert_blocked(obs_active_session, RetirementStatus.RETAINED_ACTIVE, "session:session-2")

    # 3. Live local execution
    live_exec_id = slots.start_execution(ISSUE_100, bypass_active_execution=True)
    assert live_exec_id is not None
    rev_with_live = slots.owner_activity_revision(ISSUE_100)
    obs_live_exec = ImplementationRetirementObservation(
        repository=REPO,
        owner=ISSUE_100,
        reservation_incarnation=incarnation,
        activity_revision=rev_with_live,
        implementation_prs=(
            ImplementationPRObservation(number=201, state=PRTerminalState.CLOSED),
            ImplementationPRObservation(number=202, state=PRTerminalState.CLOSED),
        ),
        provider_sessions=(
            ProviderSessionObservation(session_id="session-1", provider="jules", state=SessionTerminalState.ENDED),
            ProviderSessionObservation(session_id="session-2", provider="jules", state=SessionTerminalState.ENDED),
        ),
        local_executions=(
            LocalExecutionObservation(execution_id=execution_id, state=ExecutionTerminalState.ENDED),
            LocalExecutionObservation(execution_id=live_exec_id, state=ExecutionTerminalState.LIVE),
        ),
    )
    assert_blocked(obs_live_exec, RetirementStatus.RETAINED_ACTIVE, f"execution:{live_exec_id}")
    slots.finish_execution(ISSUE_100, live_exec_id)

    # 4. Execution with unreadable liveness
    rev_after_finish = slots.owner_activity_revision(ISSUE_100)
    obs_unknown_exec = ImplementationRetirementObservation(
        repository=REPO,
        owner=ISSUE_100,
        reservation_incarnation=incarnation,
        activity_revision=rev_after_finish,
        implementation_prs=(
            ImplementationPRObservation(number=201, state=PRTerminalState.CLOSED),
            ImplementationPRObservation(number=202, state=PRTerminalState.CLOSED),
        ),
        provider_sessions=(
            ProviderSessionObservation(session_id="session-1", provider="jules", state=SessionTerminalState.ENDED),
            ProviderSessionObservation(session_id="session-2", provider="jules", state=SessionTerminalState.ENDED),
        ),
        local_executions=(LocalExecutionObservation(execution_id=execution_id, state=ExecutionTerminalState.UNKNOWN),),
    )
    assert_blocked(obs_unknown_exec, RetirementStatus.RETAINED_UNKNOWN, f"execution:{execution_id}")

    # 5. Accepted replacement-publication obligation
    obs_replacement = ImplementationRetirementObservation(
        repository=REPO,
        owner=ISSUE_100,
        reservation_incarnation=incarnation,
        activity_revision=rev_after_finish,
        implementation_prs=(
            ImplementationPRObservation(number=201, state=PRTerminalState.CLOSED),
            ImplementationPRObservation(number=202, state=PRTerminalState.CLOSED),
        ),
        provider_sessions=(
            ProviderSessionObservation(session_id="session-1", provider="jules", state=SessionTerminalState.ENDED),
            ProviderSessionObservation(session_id="session-2", provider="jules", state=SessionTerminalState.ENDED),
        ),
        local_executions=(LocalExecutionObservation(execution_id=execution_id, state=ExecutionTerminalState.ENDED),),
        continuing_obligations=ContinuingObligations(has_replacement_publication=True),
    )
    assert_blocked(obs_replacement, RetirementStatus.RETAINED_ACTIVE, "obligation:replacement_publication")

    # 6. Indeterminate submission
    obs_submission = ImplementationRetirementObservation(
        repository=REPO,
        owner=ISSUE_100,
        reservation_incarnation=incarnation,
        activity_revision=rev_after_finish,
        implementation_prs=(
            ImplementationPRObservation(number=201, state=PRTerminalState.CLOSED),
            ImplementationPRObservation(number=202, state=PRTerminalState.CLOSED),
        ),
        provider_sessions=(
            ProviderSessionObservation(session_id="session-1", provider="jules", state=SessionTerminalState.ENDED),
            ProviderSessionObservation(session_id="session-2", provider="jules", state=SessionTerminalState.ENDED),
        ),
        local_executions=(LocalExecutionObservation(execution_id=execution_id, state=ExecutionTerminalState.ENDED),),
        continuing_obligations=ContinuingObligations(has_unresolved_submission=True),
    )
    assert_blocked(obs_submission, RetirementStatus.RETAINED_UNKNOWN, "obligation:unresolved_submission")

    # 7. Incomplete membership (store has PR 201 & 202, observation only provides 201)
    obs_incomplete_pr = ImplementationRetirementObservation(
        repository=REPO,
        owner=ISSUE_100,
        reservation_incarnation=incarnation,
        activity_revision=rev_after_finish,
        implementation_prs=(ImplementationPRObservation(number=201, state=PRTerminalState.CLOSED),),
        provider_sessions=(
            ProviderSessionObservation(session_id="session-1", provider="jules", state=SessionTerminalState.ENDED),
            ProviderSessionObservation(session_id="session-2", provider="jules", state=SessionTerminalState.ENDED),
        ),
        local_executions=(LocalExecutionObservation(execution_id=execution_id, state=ExecutionTerminalState.ENDED),),
    )
    assert_blocked(obs_incomplete_pr, RetirementStatus.RETAINED_UNKNOWN, "pr:202")

    # 8. Bare reservation with no PR
    owner_bare = ImplementationOwner("issue", 101)
    assert slots.reserve_new(owner_bare) is True
    inc_bare = slots.owner_incarnation(owner_bare)
    rev_bare = slots.owner_activity_revision(owner_bare)
    obs_bare = ImplementationRetirementObservation(
        repository=REPO,
        owner=owner_bare,
        reservation_incarnation=inc_bare,
        activity_revision=rev_bare,
        implementation_prs=(),
    )
    res_bare = slots.retire_owner(obs_bare, routing)
    assert res_bare.status is RetirementStatus.RETAINED_UNKNOWN
    assert "no implementation PR" in res_bare.diagnostic

    # 9. Unsupported provider attribution
    obs_unsupported_provider = ImplementationRetirementObservation(
        repository=REPO,
        owner=ISSUE_100,
        reservation_incarnation=incarnation,
        activity_revision=rev_after_finish,
        implementation_prs=(
            ImplementationPRObservation(number=201, state=PRTerminalState.CLOSED),
            ImplementationPRObservation(number=202, state=PRTerminalState.CLOSED),
        ),
        provider_sessions=(
            ProviderSessionObservation(session_id="session-1", provider="unsupported_ai", state=SessionTerminalState.ENDED),
            ProviderSessionObservation(session_id="session-2", provider="jules", state=SessionTerminalState.ENDED),
        ),
        local_executions=(LocalExecutionObservation(execution_id=execution_id, state=ExecutionTerminalState.ENDED),),
    )
    assert_blocked(obs_unsupported_provider, RetirementStatus.RETAINED_UNKNOWN, "session:session-1")


# ---------------------------------------------------------------------------
# AS-003: Newer activity survives an old terminal observation
# Covers REQ-005, REQ-007, REQ-008.
# ---------------------------------------------------------------------------


def test_as003_newer_activity_survives_old_observation_variants(tmp_path):
    """AS-003: 5 semantically distinct variants of contested ordering resulting in STALE_OBSERVATION."""
    # Variant 1: new PR membership admitted while paused
    slots1 = ImplementationSlotRepository(REPO, 2, tmp_path / "slots.json")
    slots2 = ImplementationSlotRepository(REPO, 2, tmp_path / "slots.json")

    exec_id = slots1.start_execution(ISSUE_100, generation="gen-100")
    slots1.record_implementation_pr(ISSUE_100, 201)
    slots1.finish_execution(ISSUE_100, exec_id)

    # Capture terminal observation in instance 1
    incarnation = slots1.owner_incarnation(ISSUE_100)
    revision = slots1.owner_activity_revision(ISSUE_100)
    obs = ImplementationRetirementObservation(
        repository=REPO,
        owner=ISSUE_100,
        reservation_incarnation=incarnation,
        activity_revision=revision,
        implementation_prs=(ImplementationPRObservation(number=201, state=PRTerminalState.CLOSED),),
        local_executions=(LocalExecutionObservation(execution_id=exec_id, state=ExecutionTerminalState.ENDED),),
    )

    barrier_fired = threading.Event()

    def pause_barrier_v1():
        # Instance 2 commits new PR membership
        assert slots2.record_implementation_pr(ISSUE_100, 202) is True
        barrier_fired.set()

    res_v1 = slots1.retire_owner(obs, pre_lock_barrier=pause_barrier_v1)
    assert barrier_fired.is_set()
    assert res_v1.status is RetirementStatus.STALE_OBSERVATION
    # Proves new PR membership was preserved in store
    assert 202 in slots2._read()[ISSUE_100.key]["implementation_prs"]

    # The original participant can perform a fresh evaluation successfully when newer work ends
    fresh_rev = slots1.owner_activity_revision(ISSUE_100)
    fresh_obs = ImplementationRetirementObservation(
        repository=REPO,
        owner=ISSUE_100,
        reservation_incarnation=incarnation,
        activity_revision=fresh_rev,
        implementation_prs=(
            ImplementationPRObservation(number=201, state=PRTerminalState.CLOSED),
            ImplementationPRObservation(number=202, state=PRTerminalState.CLOSED),
        ),
        local_executions=(LocalExecutionObservation(execution_id=exec_id, state=ExecutionTerminalState.ENDED),),
    )
    fresh_res = slots1.retire_owner(fresh_obs)
    assert fresh_res.status is RetirementStatus.RELEASED


def test_as003_variant_2_added_live_execution(tmp_path):
    """AS-003 variant 2: an added execution that remains live."""
    slots1 = ImplementationSlotRepository(REPO, 2, tmp_path / "slots.json")
    slots2 = ImplementationSlotRepository(REPO, 2, tmp_path / "slots.json")

    exec_id = slots1.start_execution(ISSUE_100, generation="gen-100")
    slots1.record_implementation_pr(ISSUE_100, 201)
    slots1.finish_execution(ISSUE_100, exec_id)

    obs = ImplementationRetirementObservation(
        repository=REPO,
        owner=ISSUE_100,
        reservation_incarnation=slots1.owner_incarnation(ISSUE_100),
        activity_revision=slots1.owner_activity_revision(ISSUE_100),
        implementation_prs=(ImplementationPRObservation(number=201, state=PRTerminalState.CLOSED),),
        local_executions=(LocalExecutionObservation(execution_id=exec_id, state=ExecutionTerminalState.ENDED),),
    )

    def pause_barrier_v2():
        live_id = slots2.start_execution(ISSUE_100, bypass_active_execution=True)
        assert live_id is not None

    res_v2 = slots1.retire_owner(obs, pre_lock_barrier=pause_barrier_v2)
    assert res_v2.status is RetirementStatus.STALE_OBSERVATION
    assert len(slots2.active_execution_ids(ISSUE_100)) == 1


def test_as003_variant_3_execution_starts_and_finishes_while_paused(tmp_path):
    """AS-003 variant 3: execution starts and finishes while release is paused."""
    slots1 = ImplementationSlotRepository(REPO, 2, tmp_path / "slots.json")
    slots2 = ImplementationSlotRepository(REPO, 2, tmp_path / "slots.json")

    exec_id = slots1.start_execution(ISSUE_100, generation="gen-100")
    slots1.record_implementation_pr(ISSUE_100, 201)
    slots1.finish_execution(ISSUE_100, exec_id)

    obs = ImplementationRetirementObservation(
        repository=REPO,
        owner=ISSUE_100,
        reservation_incarnation=slots1.owner_incarnation(ISSUE_100),
        activity_revision=slots1.owner_activity_revision(ISSUE_100),
        implementation_prs=(ImplementationPRObservation(number=201, state=PRTerminalState.CLOSED),),
        local_executions=(LocalExecutionObservation(execution_id=exec_id, state=ExecutionTerminalState.ENDED),),
    )

    def pause_barrier_v3():
        new_exec = slots2.start_execution(ISSUE_100, bypass_active_execution=True)
        assert new_exec is not None
        slots2.finish_execution(ISSUE_100, new_exec)

    res_v3 = slots1.retire_owner(obs, pre_lock_barrier=pause_barrier_v3)
    # Even though execution list is now empty again, activity_revision incremented
    assert res_v3.status is RetirementStatus.STALE_OBSERVATION


def test_as003_variant_4_same_session_id_followup(tmp_path):
    """AS-003 variant 4: same-session-ID follow-up activity committed while paused."""
    slots1 = ImplementationSlotRepository(REPO, 2, tmp_path / "slots.json")
    slots2 = ImplementationSlotRepository(REPO, 2, tmp_path / "slots.json")

    exec_id = slots1.start_execution(ISSUE_100, generation="gen-100")
    slots1.record_implementation_pr(ISSUE_100, 201)
    slots1.record_provider_session(ISSUE_100, "session-jules-1")
    slots1.finish_execution(ISSUE_100, exec_id)

    obs = ImplementationRetirementObservation(
        repository=REPO,
        owner=ISSUE_100,
        reservation_incarnation=slots1.owner_incarnation(ISSUE_100),
        activity_revision=slots1.owner_activity_revision(ISSUE_100),
        implementation_prs=(ImplementationPRObservation(number=201, state=PRTerminalState.CLOSED),),
        provider_sessions=(ProviderSessionObservation(session_id="session-jules-1", provider="jules", state=SessionTerminalState.ENDED),),
        local_executions=(LocalExecutionObservation(execution_id=exec_id, state=ExecutionTerminalState.ENDED),),
    )

    def pause_barrier_v4():
        # Follow-up registration of another PR or activity on the same session
        assert slots2.record_implementation_pr(ISSUE_100, 209) is True

    res_v4 = slots1.retire_owner(obs, pre_lock_barrier=pause_barrier_v4)
    assert res_v4.status is RetirementStatus.STALE_OBSERVATION


def test_as003_variant_5_remove_and_recreate_owner(tmp_path):
    """AS-003 variant 5: removal and recreation of owner with superficially identical members."""
    slots1 = ImplementationSlotRepository(REPO, 2, tmp_path / "slots.json")
    slots2 = ImplementationSlotRepository(REPO, 2, tmp_path / "slots.json")

    exec_id = slots1.start_execution(ISSUE_100, generation="gen-100")
    slots1.record_implementation_pr(ISSUE_100, 201)
    slots1.finish_execution(ISSUE_100, exec_id)

    old_incarnation = slots1.owner_incarnation(ISSUE_100)
    old_revision = slots1.owner_activity_revision(ISSUE_100)
    obs = ImplementationRetirementObservation(
        repository=REPO,
        owner=ISSUE_100,
        reservation_incarnation=old_incarnation,
        activity_revision=old_revision,
        implementation_prs=(ImplementationPRObservation(number=201, state=PRTerminalState.CLOSED),),
        local_executions=(LocalExecutionObservation(execution_id=exec_id, state=ExecutionTerminalState.ENDED),),
    )

    def pause_barrier_v5():
        slots2.release(ISSUE_100)
        assert slots2.reserve(ISSUE_100, implementation_pr=201) is True

    res_v5 = slots1.retire_owner(obs, pre_lock_barrier=pause_barrier_v5)
    # New incarnation does not match old observation
    assert res_v5.status is RetirementStatus.STALE_OBSERVATION
    new_incarnation = slots2.owner_incarnation(ISSUE_100)
    assert new_incarnation != old_incarnation


# ---------------------------------------------------------------------------
# AS-004: Crash cannot free capacity by deleting start evidence
# Covers REQ-004 through REQ-007.
# ---------------------------------------------------------------------------


def test_as004_crash_cannot_free_capacity_by_deleting_start_evidence(tmp_path):
    """AS-004: failure before history commit, between start preservation and active removal, and after commit."""
    slots, routing = _setup_stores(tmp_path, limit=1)

    # Start through real generation-binding writer
    exec_id = slots.start_execution(ISSUE_100, generation="gen-crash-test")
    slots.record_implementation_pr(ISSUE_100, 201)
    slots.finish_execution(ISSUE_100, exec_id)

    incarnation = slots.owner_incarnation(ISSUE_100)
    revision = slots.owner_activity_revision(ISSUE_100)
    obs = ImplementationRetirementObservation(
        repository=REPO,
        owner=ISSUE_100,
        reservation_incarnation=incarnation,
        activity_revision=revision,
        implementation_prs=(ImplementationPRObservation(number=201, state=PRTerminalState.CLOSED),),
        local_executions=(LocalExecutionObservation(execution_id=exec_id, state=ExecutionTerminalState.ENDED),),
    )

    # 1. Failure injection before history commit (e.g. _write_retired raises)
    original_write_retired = slots._write_retired

    def fail_write_retired(_records):
        raise OSError("Simulated disk error before history commit")

    slots._write_retired = fail_write_retired
    with pytest.raises((ImplementationSlotUnavailable, OSError), match="Simulated disk error"):
        slots.retire_owner(obs, routing)
    slots._write_retired = original_write_retired

    # Reconstruct from disk: active reservation still retained!
    reconstructed_1 = ImplementationSlotRepository(REPO, 1, tmp_path / "slots.json")
    assert reconstructed_1.active_owners() == (ISSUE_100,)
    assert reconstructed_1.available_normal_slots() == 0
    # Generation remains bound on the owner record
    assert reconstructed_1.implementation_generation(ISSUE_100) == "gen-crash-test"

    # 2. Failure injection between acquired-start preservation and active removal
    original_write = slots._write

    def fail_active_write(_owners):
        raise OSError("Simulated disk error on active store removal")

    slots._write = fail_active_write
    with pytest.raises((ImplementationSlotUnavailable, OSError), match="Simulated disk error"):
        slots.retire_owner(obs, routing)
    slots._write = original_write

    # Reconstruct from disk: active reservation still retained!
    reconstructed_2 = ImplementationSlotRepository(REPO, 1, tmp_path / "slots.json")
    assert reconstructed_2.active_owners() == (ISSUE_100,)
    assert reconstructed_2.available_normal_slots() == 0
    # Acquired-start fact is already safely in routing store
    assert routing.is_implementation_owned(REPO, 100, "gen-crash-test")

    # 3. Successful retirement commit
    res = slots.retire_owner(obs, routing)
    assert res.status is RetirementStatus.RELEASED

    # Reconstruct from disk: fully committed retirement
    reconstructed_3 = ImplementationSlotRepository(REPO, 1, tmp_path / "slots.json")
    assert reconstructed_3.active_owners() == ()
    assert reconstructed_3.available_normal_slots() == 1
    assert reconstructed_3.has_retired_associations(ISSUE_100)

    # The captured generation MUST NEVER become a fresh duplicate start
    gate = evaluate_implementation_start(routing, reconstructed_3, REPO, ISSUE_100, "gen-crash-test")
    assert gate.decision is OwnershipStartDecision.ALREADY_OWNED


def test_as004_malformed_and_unwriteable_store_fails_closed(tmp_path):
    """AS-004: malformed and unwriteable state fails closed without empty-store fallback."""
    slots, routing = _setup_stores(tmp_path, limit=1)
    slots.reserve(ISSUE_100, implementation_pr=201)

    obs = ImplementationRetirementObservation(
        repository=REPO,
        owner=ISSUE_100,
        reservation_incarnation=slots.owner_incarnation(ISSUE_100),
        activity_revision=slots.owner_activity_revision(ISSUE_100),
        implementation_prs=(ImplementationPRObservation(number=201, state=PRTerminalState.CLOSED),),
    )

    # Write corrupt data to slots.json
    slots.storage_path.write_text("NOT_VALID_JSON{", encoding="utf-8")
    with pytest.raises(ImplementationSlotUnavailable):
        slots.retire_owner(obs, routing)


# ---------------------------------------------------------------------------
# AS-005: Legacy recovery and honest counts
# Covers REQ-001, REQ-004, REQ-006, REQ-007.
# ---------------------------------------------------------------------------


def test_as005_legacy_recovery_and_honest_counts(tmp_path):
    """AS-005: load legacy owner image (no generation/incarnation), establish boundary, retire without guessing generation."""
    slots, routing = _setup_stores(tmp_path, limit=3)

    # Legacy image with 2 owners, no incarnation or generation
    legacy_state = {
        "issue:100": {
            "kind": "issue",
            "number": 100,
            "implementation_prs": [101],
            "provider_sessions": ["sess-1"],
            "executions": [],
        },
        "issue:200": {
            "kind": "issue",
            "number": 200,
            "implementation_prs": [201],
            "provider_sessions": ["sess-2"],
            "executions": [],
        },
    }
    slots.storage_path.write_text(json.dumps(legacy_state), encoding="utf-8")

    # Establish and commit safe local incarnation boundary for issue:100
    inc_100 = slots.establish_incarnation(ISSUE_100)
    assert inc_100 is not None
    rev_100 = slots.owner_activity_revision(ISSUE_100)
    assert rev_100 is not None

    # Prove terminality for issue:100
    obs_100 = ImplementationRetirementObservation(
        repository=REPO,
        owner=ISSUE_100,
        reservation_incarnation=inc_100,
        activity_revision=rev_100,
        implementation_prs=(ImplementationPRObservation(number=101, state=PRTerminalState.CLOSED),),
        provider_sessions=(ProviderSessionObservation(session_id="sess-1", provider="jules", state=SessionTerminalState.ENDED),),
    )
    res_100 = slots.retire_owner(obs_100, routing)
    assert res_100.status is RetirementStatus.RELEASED

    # Retired record must have generation: None ("without inventing a semantic generation")
    retired = slots.retired_associations(ISSUE_100)
    assert len(retired) == 1
    assert retired[0].generation is None

    # Preserve issue:200 with unavailable session evidence
    inc_200 = slots.establish_incarnation(ImplementationOwner("issue", 200))
    obs_200 = ImplementationRetirementObservation(
        repository=REPO,
        owner=ImplementationOwner("issue", 200),
        reservation_incarnation=inc_200,
        activity_revision=slots.owner_activity_revision(ImplementationOwner("issue", 200)),
        implementation_prs=(ImplementationPRObservation(number=201, state=PRTerminalState.CLOSED),),
        provider_sessions=(ProviderSessionObservation(session_id="sess-2", provider="jules", state=SessionTerminalState.UNKNOWN),),
    )
    res_200 = slots.retire_owner(obs_200, routing)
    assert res_200.status is RetirementStatus.RETAINED_UNKNOWN
    assert ImplementationOwner("issue", 200) in slots.active_owners()


def test_as005_honest_counts_progression(tmp_path):
    """AS-005 part 2: 5 normal owners, limit 3. Retire 2 -> 3/3 used, 0 avail. Retire 1 -> 2/3 used, 1 avail."""
    slots, routing = _setup_stores(tmp_path, limit=3)

    # Populate 5 normal owners (limit 3)
    owners = [ImplementationOwner("issue", i) for i in range(1, 6)]
    state = {}
    for o in owners:
        state[o.key] = {
            "kind": o.kind,
            "number": o.number,
            "incarnation": f"inc-{o.number}",
            "activity_revision": 1,
            "implementation_prs": [o.number + 100],
            "provider_sessions": [],
            "executions": [],
            "emergency": False,
        }
    slots.storage_path.write_text(json.dumps(state), encoding="utf-8")

    # Initial: 5/3 used, 0 available
    snapshot = slots.snapshot()
    assert (snapshot.normal_usage, snapshot.normal_available) == (5, 0)

    # Retire 2 terminal owners: issue:1 and issue:2
    for o in (owners[0], owners[1]):
        obs = ImplementationRetirementObservation(
            repository=REPO,
            owner=o,
            reservation_incarnation=f"inc-{o.number}",
            activity_revision=1,
            implementation_prs=(ImplementationPRObservation(number=o.number + 100, state=PRTerminalState.CLOSED),),
        )
        assert slots.retire_owner(obs, routing).status is RetirementStatus.RELEASED

    # Observe 3/3 used and zero available, NOT two available!
    snap_after_2 = slots.snapshot()
    assert (snap_after_2.normal_usage, snap_after_2.normal_available) == (3, 0)

    # Retire 1 more: issue:3
    obs_3 = ImplementationRetirementObservation(
        repository=REPO,
        owner=owners[2],
        reservation_incarnation=f"inc-{owners[2].number}",
        activity_revision=1,
        implementation_prs=(ImplementationPRObservation(number=owners[2].number + 100, state=PRTerminalState.CLOSED),),
    )
    assert slots.retire_owner(obs_3, routing).status is RetirementStatus.RELEASED

    # Observe 2/3 used and one available!
    snap_after_3 = slots.snapshot()
    assert (snap_after_3.normal_usage, snap_after_3.normal_available) == (2, 1)

    # Retired history and emergency records must not distort those counts
    assert len(slots.retired_records()) == 3


# ---------------------------------------------------------------------------
# AS-006: Retired history is neither new authority nor a permanent PR blacklist
# Covers REQ-004, REQ-006, REQ-008, REQ-009.
# ---------------------------------------------------------------------------


def test_as006_retired_history_neither_authority_nor_blacklist(tmp_path):
    """AS-006: old PR evidence does not resurrect owner; reopened PR admitted via normal admission."""
    slots, routing = _setup_stores(tmp_path, limit=1)

    # Retire ISSUE_100
    exec_id = slots.start_execution(ISSUE_100, generation="gen-100")
    slots.record_implementation_pr(ISSUE_100, 201)
    slots.record_provider_session(ISSUE_100, "session-1")
    slots.finish_execution(ISSUE_100, exec_id)

    obs = ImplementationRetirementObservation(
        repository=REPO,
        owner=ISSUE_100,
        reservation_incarnation=slots.owner_incarnation(ISSUE_100),
        activity_revision=slots.owner_activity_revision(ISSUE_100),
        implementation_prs=(ImplementationPRObservation(number=201, state=PRTerminalState.CLOSED),),
        provider_sessions=(ProviderSessionObservation(session_id="session-1", provider="jules", state=SessionTerminalState.ENDED),),
        local_executions=(LocalExecutionObservation(execution_id=exec_id, state=ExecutionTerminalState.ENDED),),
    )
    assert slots.retire_owner(obs, routing).status is RetirementStatus.RELEASED

    # Present only old PR/session evidence: owner resolution does not attribute to retired owner
    mock_gh = MagicMock()
    resolved = slots.resolve_owner("pr", {"number": 201, "body": "Session ID: session-1"}, mock_gh)
    # Resolves to standalone PR or unlinked, not the retired Issue owner
    assert resolved != ISSUE_100

    # Repeat old generation wake: no reservation or fresh implementation appears
    gate = evaluate_implementation_start(routing, slots, REPO, ISSUE_100, "gen-100")
    assert gate.decision is OwnershipStartDecision.ALREADY_OWNED
    assert not gate.may_start

    # Separately supply a current reopened PR through ordinary admission:
    # 1. When capacity is available (1 available): ordinary admission succeeds
    assert slots.available_normal_slots() == 1
    assert slots.reserve(ISSUE_100, implementation_pr=201) is True
    assert slots.active_owners() == (ISSUE_100,)
    # New incarnation was created
    assert slots.owner_incarnation(ISSUE_100) != obs.reservation_incarnation

    # 2. When capacity is full: ordinary admission fails
    other_owner = ImplementationOwner("issue", 200)
    assert slots.available_normal_slots() == 0
    assert slots.reserve_new(other_owner) is False
