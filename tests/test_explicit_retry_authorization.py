from __future__ import annotations

import json
import threading

import pytest

from auto_coder.implementation_ownership import acquire_explicit_retry, invalidate_explicit_retry
from auto_coder.implementation_slots import (
    ImplementationOwner,
    ImplementationSlotRepository,
    ImplementationSlotUnavailable,
)
from auto_coder.issue_stage_routing import IssueStageRoutingStore, RetryRequestConflict

REPOSITORY = "owner/repo"
GENERATION = "generation-exact"


def stores(tmp_path):
    return (
        IssueStageRoutingStore(tmp_path / "routing.sqlite3"),
        ImplementationSlotRepository(REPOSITORY, 2, tmp_path / "slots.json"),
    )


def test_acceptance_is_durable_idempotent_and_does_not_acquire(tmp_path):
    routing, slots = stores(tmp_path)
    accepted = routing.accept_retry_request("request-1", REPOSITORY, 7, GENERATION)

    assert accepted.status == "pending"
    assert accepted.attempt_id not in {accepted.request_id, accepted.generation}
    assert slots.active_owners() == ()
    assert not routing.is_implementation_owned(REPOSITORY, 7, GENERATION)

    restarted = IssueStageRoutingStore(tmp_path / "routing.sqlite3")
    assert restarted.accept_retry_request("request-1", REPOSITORY, 7, GENERATION) == accepted
    with pytest.raises(RetryRequestConflict):
        restarted.accept_retry_request("request-1", REPOSITORY, 8, GENERATION)
    assert restarted.retry_request("request-1") == accepted


def test_retry_acquires_once_despite_tombstone_and_survives_reconstruction(tmp_path):
    routing, slots = stores(tmp_path)
    owner = ImplementationOwner("issue", 7)
    original = slots.start_execution(owner, generation=GENERATION)
    assert original is not None
    routing.record_implementation_owned(REPOSITORY, 7, GENERATION)
    slots.finish_execution(owner, original)

    accepted = routing.accept_retry_request("request-1", REPOSITORY, 7, GENERATION)
    acquired = acquire_explicit_retry(routing, slots, REPOSITORY, 7, GENERATION, "request-1")
    assert acquired.status == "owned"
    assert acquired.attempt_id == accepted.attempt_id
    assert acquired.ownership_reference is not None
    assert acquired.ownership_reference != original
    assert slots.active_execution_ids(owner) == (acquired.ownership_reference,)

    restarted_routing = IssueStageRoutingStore(tmp_path / "routing.sqlite3")
    restarted_slots = ImplementationSlotRepository(REPOSITORY, 2, tmp_path / "slots.json")
    replay = acquire_explicit_retry(restarted_routing, restarted_slots, REPOSITORY, 7, GENERATION, "request-1")
    assert replay == acquired
    assert restarted_slots.active_execution_ids(owner) == (acquired.ownership_reference,)


def test_contention_defers_without_consuming_and_same_consumer_can_retry(tmp_path):
    routing, slots = stores(tmp_path)
    owner = ImplementationOwner("issue", 7)
    live = slots.start_execution(owner, generation=GENERATION)
    assert live is not None
    routing.accept_retry_request("request-1", REPOSITORY, 7, GENERATION)

    deferred = acquire_explicit_retry(routing, slots, REPOSITORY, 7, GENERATION, "request-1")
    assert deferred.status == "pending"
    assert deferred.refusal == "local execution contention or capacity unavailable"
    slots.finish_execution(owner, live)

    acquired = acquire_explicit_retry(routing, slots, REPOSITORY, 7, GENERATION, "request-1")
    assert acquired.status == "owned"
    assert slots.active_execution_ids(owner) == (acquired.ownership_reference,)


def test_reconstruction_repairs_crash_after_slot_capture(tmp_path, monkeypatch):
    routing, slots = stores(tmp_path)
    routing.accept_retry_request("request-1", REPOSITORY, 7, GENERATION)

    def interrupted_projection(*_args, **_kwargs):
        raise RuntimeError("injected projection interruption")

    monkeypatch.setattr(routing, "mark_retry_owned", interrupted_projection)
    with pytest.raises(RuntimeError, match="projection interruption"):
        acquire_explicit_retry(routing, slots, REPOSITORY, 7, GENERATION, "request-1")
    captured = slots.active_execution_ids(ImplementationOwner("issue", 7))
    assert len(captured) == 1

    restarted_routing = IssueStageRoutingStore(tmp_path / "routing.sqlite3")
    restarted_slots = ImplementationSlotRepository(REPOSITORY, 2, tmp_path / "slots.json")
    recovered = acquire_explicit_retry(
        restarted_routing,
        restarted_slots,
        REPOSITORY,
        7,
        GENERATION,
        "request-1",
    )
    assert recovered.status == "owned"
    assert recovered.ownership_reference == captured[0]
    assert restarted_slots.active_execution_ids(ImplementationOwner("issue", 7)) == captured


def test_two_consumers_share_one_attempt_and_one_execution(tmp_path):
    routing, slots = stores(tmp_path)
    accepted = routing.accept_retry_request("request-1", REPOSITORY, 7, GENERATION)
    barrier = threading.Barrier(3)
    results = []

    def consume():
        local_routing = IssueStageRoutingStore(tmp_path / "routing.sqlite3")
        local_slots = ImplementationSlotRepository(REPOSITORY, 2, tmp_path / "slots.json")
        barrier.wait()
        results.append(acquire_explicit_retry(local_routing, local_slots, REPOSITORY, 7, GENERATION, "request-1"))

    threads = [threading.Thread(target=consume) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert len(results) == 2
    assert {result.attempt_id for result in results} == {accepted.attempt_id}
    assert {result.ownership_reference for result in results} == {results[0].ownership_reference}
    assert len(slots.active_execution_ids(ImplementationOwner("issue", 7))) == 1


def test_invalidation_is_monotonic_and_preserves_owned_history(tmp_path):
    routing, slots = stores(tmp_path)
    routing.accept_retry_request("pending", REPOSITORY, 7, GENERATION)
    invalid = invalidate_explicit_retry(routing, slots, "pending", "generation-2")
    assert invalid.status == "invalidated"
    assert "generation-2" in (invalid.refusal or "")
    assert invalidate_explicit_retry(routing, slots, "pending", GENERATION).status == "invalidated"
    assert acquire_explicit_retry(routing, slots, REPOSITORY, 7, GENERATION, "pending").status == "invalidated"

    routing.accept_retry_request("owned", REPOSITORY, 8, GENERATION)
    owned = acquire_explicit_retry(routing, slots, REPOSITORY, 8, GENERATION, "owned")
    retained = invalidate_explicit_retry(routing, slots, "owned", "generation-2")
    assert retained.status == "owned"
    assert retained.ownership_reference == owned.ownership_reference


def test_retained_remote_membership_does_not_block_distinct_local_attempt(tmp_path):
    routing, slots = stores(tmp_path)
    owner = ImplementationOwner("issue", 7)
    routing.accept_retry_request("first", REPOSITORY, 7, GENERATION)
    first = acquire_explicit_retry(routing, slots, REPOSITORY, 7, GENERATION, "first")
    assert slots.record_provider_session(owner, "provider-session")
    slots.finish_execution(owner, first.ownership_reference or "")

    second_accepted = routing.accept_retry_request("second", REPOSITORY, 7, GENERATION)
    second = acquire_explicit_retry(routing, slots, REPOSITORY, 7, GENERATION, "second")

    assert second.status == "owned"
    assert second.attempt_id == second_accepted.attempt_id
    assert second.attempt_id != first.attempt_id
    assert second.ownership_reference != first.ownership_reference
    assert slots.has_provider_sessions(owner)


def test_malformed_ownership_fails_closed_without_rebinding(tmp_path):
    routing, slots = stores(tmp_path)
    owner = ImplementationOwner("issue", 7)
    assert slots.reserve(owner, implementation_pr=99)
    raw = json.loads(slots.storage_path.read_text())
    raw[owner.key]["implementation_generation"] = 42
    slots.storage_path.write_text(json.dumps(raw))
    routing.accept_retry_request("request-1", REPOSITORY, 7, GENERATION)

    with pytest.raises(ImplementationSlotUnavailable, match="generation"):
        acquire_explicit_retry(routing, slots, REPOSITORY, 7, GENERATION, "request-1")
    assert routing.retry_request("request-1").status == "pending"
    assert json.loads(slots.storage_path.read_text())[owner.key]["implementation_generation"] == 42


def test_enumeration_is_repository_and_issue_scoped(tmp_path):
    routing, _slots = stores(tmp_path)
    first = routing.accept_retry_request("one", REPOSITORY, 7, GENERATION)
    routing.accept_retry_request("two", REPOSITORY, 8, GENERATION)
    routing.accept_retry_request("three", "other/repo", 7, GENERATION)

    assert routing.retry_requests(REPOSITORY, 7) == (first,)
    assert {record.request_id for record in routing.retry_requests(REPOSITORY)} == {"one", "two"}
