"""Tests for the runtime reclamation scheduling layer (Issue #2148).

These tests exercise the scheduling/wiring layer that sits on top of the
already-tested retirement predicate (#2146, `test_implementation_retirement.py`)
and evidence collector (#2147, `test_implementation_retirement_observer.py`):
obligation persistence across a simulated restart, 60-second-cadence
rescheduling, coalescing duplicate wakes, capacity refill firing a real
admission after a release within the same run, startup recovery finding an
owner whose PR closed while offline and is absent from open-PR enumeration,
and that an in-flight newer incarnation's obligation survives an older
completion.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

from auto_coder.implementation_reclamation_scheduler import (
    RECLAMATION_RECHECK_SECONDS,
    ReclamationObligationStore,
    recover_obligations_at_startup,
    run_due_reclamation_checks,
    schedule_reevaluation,
)
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository

REPO = "kitamura-tetsuo/auto-coder"
ISSUE_100 = ImplementationOwner("issue", 100)
ISSUE_200 = ImplementationOwner("issue", 200)


def _setup_slots(tmp_path: Path, limit: int = 3) -> ImplementationSlotRepository:
    return ImplementationSlotRepository(REPO, limit, tmp_path / "slots.json")


def _make_github_client(
    pr_responses: Optional[Dict[int, Any]] = None,
    connected_prs: Optional[Dict[int, List[int]]] = None,
    open_prs: Optional[List[Dict[str, Any]]] = None,
) -> MagicMock:
    """Minimal github_client double, matching the shape production code expects."""
    client = MagicMock()

    def _get_pull_request_metadata_strict(repo: str, number: int) -> Dict[str, Any]:
        if pr_responses is None or number not in pr_responses:
            raise RuntimeError(f"GitHub did not return PR metadata for PR #{number}")
        return pr_responses[number]

    client.get_pull_request_metadata_strict.side_effect = _get_pull_request_metadata_strict
    client.get_pull_request.side_effect = lambda repo, number: (pr_responses or {}).get(number)

    def _get_connected_prs(repo: str, issue: int, strict: bool = False) -> List[int]:
        return (connected_prs or {}).get(issue, [])

    client.get_connected_prs.side_effect = _get_connected_prs

    def _get_open_pull_requests_strict(repo: str) -> List[Dict[str, Any]]:
        return open_prs or []

    client.get_open_pull_requests_strict.side_effect = _get_open_pull_requests_strict
    client.get_open_pull_requests.side_effect = _get_open_pull_requests_strict
    client.get_issue.side_effect = lambda repo, number: {"number": number, "state": "open"}
    return client


def _establish_owner_with_closed_pr(slots: ImplementationSlotRepository, owner: ImplementationOwner, pr_number: int, generation: str) -> str:
    """Build a real terminal-eligible reservation through production admission writers."""
    execution_id = slots.start_execution(owner, generation=generation)
    assert execution_id is not None
    assert slots.record_implementation_pr(owner, pr_number) is True
    slots.finish_execution(owner, execution_id)
    incarnation = slots.owner_incarnation(owner)
    assert incarnation is not None
    return incarnation


# ---------------------------------------------------------------------------
# Obligation scheduling and coalescing
# ---------------------------------------------------------------------------


def test_schedule_reevaluation_is_noop_for_non_issue_or_inactive_owner(tmp_path):
    slots = _setup_slots(tmp_path)
    store = ReclamationObligationStore.for_slots(slots)
    assert schedule_reevaluation(ImplementationOwner("pr", 5), slots, store) is False
    assert schedule_reevaluation(ISSUE_100, slots, store) is False  # not active
    assert store.all() == ()


def test_schedule_reevaluation_coalesces_duplicate_wakes_to_earliest_due(tmp_path):
    slots = _setup_slots(tmp_path)
    store = ReclamationObligationStore.for_slots(slots)
    _establish_owner_with_closed_pr(slots, ISSUE_100, 201, "gen-100")

    now = 1_000_000.0
    assert schedule_reevaluation(ISSUE_100, slots, store, reason="first", due_at=now + 50) is True
    # A second wake for the SAME incarnation with a LATER due time must not
    # push the check back, and must not create a second entry.
    assert schedule_reevaluation(ISSUE_100, slots, store, reason="second", due_at=now + 100) is True
    obligations = store.all()
    assert len(obligations) == 1
    assert obligations[0].next_due_at == now + 50

    # A wake with an EARLIER due time still wins (coalesce to the earliest).
    assert schedule_reevaluation(ISSUE_100, slots, store, reason="third", due_at=now + 10) is True
    obligations = store.all()
    assert len(obligations) == 1
    assert obligations[0].next_due_at == now + 10


# ---------------------------------------------------------------------------
# Persistence across a simulated restart
# ---------------------------------------------------------------------------


def test_obligation_persists_across_simulated_restart(tmp_path):
    slots = _setup_slots(tmp_path)
    store = ReclamationObligationStore.for_slots(slots)
    incarnation = _establish_owner_with_closed_pr(slots, ISSUE_100, 201, "gen-100")
    schedule_reevaluation(ISSUE_100, slots, store, reason="pr-closed", due_at=1_000_000.0)

    # Simulate a restart: fresh repository/store instances over the same files.
    restarted_slots = ImplementationSlotRepository(REPO, slots.max_implementations, slots.storage_path)
    restarted_store = ReclamationObligationStore.for_slots(restarted_slots)
    obligations = restarted_store.all()
    assert len(obligations) == 1
    assert obligations[0].owner == ISSUE_100
    assert obligations[0].incarnation == incarnation
    assert obligations[0].next_due_at == 1_000_000.0


# ---------------------------------------------------------------------------
# Due-check consumer: release, 60s cadence reschedule, capacity-freed callback
# ---------------------------------------------------------------------------


def test_run_due_reclamation_checks_releases_and_clears_obligation_and_fires_callback(tmp_path):
    slots = _setup_slots(tmp_path)
    store = ReclamationObligationStore.for_slots(slots)
    _establish_owner_with_closed_pr(slots, ISSUE_100, 201, "gen-100")
    schedule_reevaluation(ISSUE_100, slots, store, reason="pr-closed", due_at=0.0)

    github_client = _make_github_client(
        pr_responses={201: {"number": 201, "state": "closed", "merged": False}},
        connected_prs={100: [201]},
        open_prs=[],
    )
    freed_calls = []
    released = run_due_reclamation_checks(
        slots,
        store,
        github_client=github_client,
        on_capacity_freed=lambda: freed_calls.append(True),
        now=1.0,
    )

    assert released == 1
    assert freed_calls == [True]
    assert store.all() == ()
    assert ISSUE_100 not in slots.active_owners()


def test_run_due_reclamation_checks_reschedules_retained_owner_60s_out(tmp_path):
    slots = _setup_slots(tmp_path)
    store = ReclamationObligationStore.for_slots(slots)
    incarnation = _establish_owner_with_closed_pr(slots, ISSUE_100, 201, "gen-100")
    schedule_reevaluation(ISSUE_100, slots, store, reason="pr-closed", due_at=0.0)

    # PR is still open -> RETAINED_ACTIVE.
    github_client = _make_github_client(
        pr_responses={201: {"number": 201, "state": "open"}},
        connected_prs={100: [201]},
        open_prs=[{"number": 201, "state": "open", "head": {"ref": "issue-100-work"}, "body": ""}],
    )
    now = 1_000.0
    released = run_due_reclamation_checks(slots, store, github_client=github_client, now=now)

    assert released == 0
    obligations = store.all()
    assert len(obligations) == 1
    assert obligations[0].incarnation == incarnation
    assert obligations[0].next_due_at == now + RECLAMATION_RECHECK_SECONDS
    assert ISSUE_100 in slots.active_owners()


def test_run_due_reclamation_checks_skips_not_yet_due_obligations(tmp_path):
    slots = _setup_slots(tmp_path)
    store = ReclamationObligationStore.for_slots(slots)
    _establish_owner_with_closed_pr(slots, ISSUE_100, 201, "gen-100")
    schedule_reevaluation(ISSUE_100, slots, store, reason="pr-closed", due_at=5_000.0)

    github_client = _make_github_client()
    released = run_due_reclamation_checks(slots, store, github_client=github_client, now=1_000.0)

    assert released == 0
    # The obligation is untouched (still exactly the originally scheduled due time).
    obligations = store.all()
    assert len(obligations) == 1
    assert obligations[0].next_due_at == 5_000.0
    github_client.get_pull_request_metadata_strict.assert_not_called()


# ---------------------------------------------------------------------------
# Startup recovery: PR closed while offline, absent from open-PR enumeration
# ---------------------------------------------------------------------------


def test_recover_obligations_at_startup_seeds_owner_absent_from_open_pr_enumeration(tmp_path):
    slots = _setup_slots(tmp_path)
    store = ReclamationObligationStore.for_slots(slots)
    incarnation = _establish_owner_with_closed_pr(slots, ISSUE_100, 201, "gen-100")
    # No prior obligation was ever persisted for this owner (e.g. the process
    # that closed the PR crashed before it could schedule one).
    assert store.all() == ()

    seeded = recover_obligations_at_startup(slots, store, now=42.0)
    assert seeded == 1
    obligations = store.all()
    assert len(obligations) == 1
    assert obligations[0].owner == ISSUE_100
    assert obligations[0].incarnation == incarnation
    assert obligations[0].next_due_at == 42.0

    # The open-PR enumeration omits PR #201 entirely (it closed while offline),
    # yet the due check still reaches and releases it via durable slot
    # membership + a fresh strict PR read.
    github_client = _make_github_client(
        pr_responses={201: {"number": 201, "state": "closed", "merged": False}},
        connected_prs={100: [201]},
        open_prs=[],  # absent from open-PR enumeration
    )
    released = run_due_reclamation_checks(slots, store, github_client=github_client, now=100.0)
    assert released == 1
    assert ISSUE_100 not in slots.active_owners()


def test_recover_obligations_at_startup_does_not_delay_an_earlier_pending_obligation(tmp_path):
    slots = _setup_slots(tmp_path)
    store = ReclamationObligationStore.for_slots(slots)
    _establish_owner_with_closed_pr(slots, ISSUE_100, 201, "gen-100")
    schedule_reevaluation(ISSUE_100, slots, store, reason="pr-closed", due_at=10.0)

    seeded = recover_obligations_at_startup(slots, store, now=500.0)
    assert seeded == 1
    obligations = store.all()
    assert len(obligations) == 1
    # Startup recovery must not push an already-earlier-due obligation later.
    assert obligations[0].next_due_at == 10.0


# ---------------------------------------------------------------------------
# Newer incarnation survives an older incarnation's completion (AS-005)
# ---------------------------------------------------------------------------


def test_older_incarnation_completion_does_not_consume_newer_obligation(tmp_path):
    slots = _setup_slots(tmp_path)
    store = ReclamationObligationStore.for_slots(slots)
    old_incarnation = _establish_owner_with_closed_pr(slots, ISSUE_100, 201, "gen-100")
    schedule_reevaluation(ISSUE_100, slots, store, reason="pr-closed", due_at=0.0)

    # Simulate a race: retire the old incarnation directly (out-of-band from
    # the due-check consumer, representing a concurrent path), then start a
    # fresh reservation for the same logical owner under a NEW incarnation
    # with its own obligation.
    from auto_coder.implementation_retirement import (
        ImplementationPRObservation,
        ImplementationRetirementObservation,
        PRTerminalState,
        RetirementStatus,
        retire_implementation_slot,
    )

    old_revision = slots.owner_activity_revision(ISSUE_100)
    observation = ImplementationRetirementObservation(
        repository=REPO,
        owner=ISSUE_100,
        reservation_incarnation=old_incarnation,
        activity_revision=old_revision,
        implementation_prs=(ImplementationPRObservation(number=201, state=PRTerminalState.CLOSED, merged=False),),
    )
    result = retire_implementation_slot(slots, observation)
    assert result.status is RetirementStatus.RELEASED

    new_incarnation = _establish_owner_with_closed_pr(slots, ISSUE_100, 202, "gen-100b")
    assert new_incarnation != old_incarnation
    schedule_reevaluation(ISSUE_100, slots, store, reason="pr-closed", due_at=999_999.0)

    # The old completion's cleanup must not be able to erase the newer
    # obligation that now exists for this owner key.
    store.clear(ISSUE_100, old_incarnation)

    obligations = store.all()
    assert len(obligations) == 1
    assert obligations[0].incarnation == new_incarnation
    assert obligations[0].next_due_at == 999_999.0


def test_stale_incarnation_obligation_is_discarded_without_touching_current_owner(tmp_path):
    """A due obligation whose incarnation no longer matches the live store is
    dropped without retiring or otherwise touching the current reservation."""
    slots = _setup_slots(tmp_path)
    store = ReclamationObligationStore.for_slots(slots)
    old_incarnation = _establish_owner_with_closed_pr(slots, ISSUE_100, 201, "gen-100")
    schedule_reevaluation(ISSUE_100, slots, store, reason="pr-closed", due_at=0.0)

    # Owner incarnation changes underneath the obligation (retire + recreate)
    # without going through the obligation store at all, mimicking a
    # concurrent path that does not know about scheduling.
    from auto_coder.implementation_retirement import (
        ImplementationPRObservation,
        ImplementationRetirementObservation,
        PRTerminalState,
        RetirementStatus,
        retire_implementation_slot,
    )

    old_revision = slots.owner_activity_revision(ISSUE_100)
    observation = ImplementationRetirementObservation(
        repository=REPO,
        owner=ISSUE_100,
        reservation_incarnation=old_incarnation,
        activity_revision=old_revision,
        implementation_prs=(ImplementationPRObservation(number=201, state=PRTerminalState.CLOSED, merged=False),),
    )
    assert retire_implementation_slot(slots, observation).status is RetirementStatus.RELEASED
    new_incarnation = _establish_owner_with_closed_pr(slots, ISSUE_100, 202, "gen-100b")

    github_client = _make_github_client()
    released = run_due_reclamation_checks(slots, store, github_client=github_client, now=1.0)

    assert released == 0
    # Stale obligation gone; current (new-incarnation) owner still active and untouched.
    assert store.all() == ()
    assert slots.owner_incarnation(ISSUE_100) == new_incarnation
    github_client.get_pull_request_metadata_strict.assert_not_called()


# ---------------------------------------------------------------------------
# Capacity refill firing a real admission after a release within the same run
# ---------------------------------------------------------------------------


def test_capacity_refill_admits_new_issue_after_release_within_same_run(tmp_path):
    """Once a release commits, the same run's normal admission path (here:
    ImplementationSlotRepository.reserve_new, standing in for the daemon's
    capacity-refill admission call) can admit an independent Issue without
    waiting for a new unrelated webhook."""
    slots = _setup_slots(tmp_path, limit=1)
    store = ReclamationObligationStore.for_slots(slots)
    _establish_owner_with_closed_pr(slots, ISSUE_100, 201, "gen-100")
    schedule_reevaluation(ISSUE_100, slots, store, reason="pr-closed", due_at=0.0)

    # Capacity is full: an independent Issue cannot be admitted yet.
    assert slots.reserve_new(ISSUE_200) is False

    github_client = _make_github_client(
        pr_responses={201: {"number": 201, "state": "closed", "merged": False}},
        connected_prs={100: [201]},
        open_prs=[],
    )
    admitted_after_release = []

    def _on_capacity_freed() -> None:
        # Exercise the real admission entry point in the same call stack as
        # the release, exactly as automation_engine.py's capacity-refill
        # wiring does via its on_capacity_freed callback.
        admitted_after_release.append(slots.reserve_new(ISSUE_200))

    released = run_due_reclamation_checks(
        slots,
        store,
        github_client=github_client,
        on_capacity_freed=_on_capacity_freed,
        now=1.0,
    )

    assert released == 1
    assert admitted_after_release == [True]
    assert ISSUE_200 in slots.active_owners()
