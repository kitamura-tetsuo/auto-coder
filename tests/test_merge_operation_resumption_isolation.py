"""Production resume boundary with shared SQLite, fake time and no GitHub traffic."""

import asyncio
from dataclasses import dataclass
from unittest.mock import Mock

import httpx
import pytest

from src.auto_coder.automation_engine import _MergeOperationResumeHandler
from src.auto_coder.execution_trace import EventKind, Outcome, TraceCollector
from src.auto_coder.merge_operation_scheduler import RESUME_RETRY_INTERVAL_SECONDS, MergeOperationScheduler, get_merge_operation_scheduler
from src.auto_coder.merge_operation_state import BlockReason, ConfirmationSource, EffectName, EffectReceipt, EffectState, MergeOperationIdentity, MergeOperationPersistenceError, MergeOperationStore, OperationStatus
from src.auto_coder.util.github_request_outcome import GitHubApiOutcome, GitHubRequestError


@dataclass
class Clock:
    now: float = 100.0

    def __call__(self):
        return self.now


def create(store, repository="acme/widgets", head="head", number=5476, *, needs_approval=False):
    return store.get_or_create(
        MergeOperationIdentity("https://api.github.com", repository, number),
        expected_head_sha=head,
        merge_method="squash",
        approval_credential_role="role",
        reviewer_identity="reviewer" if needs_approval else "",
        needs_approval=needs_approval,
        now=100.0,
    )


def create_with_prior_effects(store):
    """Retain a confirmed approval and a real merge-throttle observation."""
    operation = create(store, needs_approval=True)
    approval = store.reserve_attempt(operation.identity, EffectName.APPROVAL, now=100.0)
    assert approval.granted
    receipt = EffectReceipt(ConfirmationSource.OWN_RESPONSE, review_id="review-123", reviewer_identity="reviewer", target_head_sha="head", recorded_at=100.0)
    assert store.record_confirmed_complete(operation.identity, EffectName.APPROVAL, approval.attempt_id, approval.generation, receipt, now=100.0)
    merge = store.reserve_attempt(operation.identity, EffectName.MERGE, now=100.0)
    assert merge.granted
    return store.defer_local(operation.identity, EffectName.MERGE, merge.attempt_id, merge.generation, is_real_throttle=True, throttle_attempt_id="prior-throttle", detail="prior throttle observation", now=100.0)


def bind(store, repository, clock, engine):
    scheduler = MergeOperationScheduler(store, clock=clock)
    scheduler.register_resume_handler(_MergeOperationResumeHandler(engine, repository), repository=repository)
    return scheduler


async def dispatch(scheduler):
    await scheduler._dispatch_due()
    if scheduler._tasks:
        await asyncio.gather(*list(scheduler._tasks))


@pytest.mark.asyncio
async def test_shared_store_collision_routes_only_owner_and_preserves_trace_identity(tmp_path, monkeypatch):
    store = MergeOperationStore(tmp_path / "shared.db")
    clock = Clock()
    own = create(store, "Acme/Widgets")
    foreign = create(store, "acme/other")
    monkeypatch.setattr(TraceCollector, "_instance", None)
    collector = TraceCollector()
    engines = []
    schedulers = []
    for repository in ("acme/widgets", "acme/other"):
        engine = Mock()
        engine.github.get_pull_request_metadata_strict.return_value = {"number": 5476, "head": {"sha": "head"}}
        engine.github.get_pr_details.side_effect = lambda raw: raw
        engine._process_single_candidate.return_value = Mock(target_outcome=None, capacity_deferred=False, refill_retry_required=False, error=None, success=True)
        engines.append(engine)
        schedulers.append(bind(store, repository, clock, engine))

    await dispatch(schedulers[0])
    assert store.get(foreign.identity) == foreign
    engines[0].github.get_pull_request_metadata_strict.assert_called_once_with("Acme/Widgets", 5476)
    assert engines[0]._process_single_candidate.call_args.args[0] == "Acme/Widgets"
    assert engines[0]._process_single_candidate.call_args.kwargs == {"origin": "merge-operation-resumption"}
    engines[1].github.get_pull_request_metadata_strict.assert_not_called()
    assert not collector.get_snapshot(repository="acme/other").events
    await dispatch(schedulers[1])
    engines[1].github.get_pull_request_metadata_strict.assert_called_once_with("acme/other", 5476)
    for operation in (own, foreign):
        events = collector.get_snapshot(repository=operation.identity.repository).events
        assert len([event for event in events if event.kind == EventKind.EXECUTION_STARTED.value]) == 1
        assert all(event.repository == operation.identity.repository for event in events)
    assert [entry["repository"] for entry in schedulers[0].snapshot()] == ["Acme/Widgets"]


def test_direct_foreign_handler_refuses_before_reads_or_trace(tmp_path, monkeypatch):
    store = MergeOperationStore(tmp_path / "shared.db")
    foreign = create(store, "acme/other")
    engine = Mock()
    monkeypatch.setattr(TraceCollector, "_instance", None)
    collector = TraceCollector()
    with pytest.raises(ValueError, match="repository"):
        _MergeOperationResumeHandler(engine, "acme/widgets")(foreign)
    assert engine.mock_calls == []
    assert not collector.get_snapshot().events
    assert store.get(foreign.identity) == foreign


@pytest.mark.asyncio
async def test_recovery_and_deadline_selection_do_not_consume_foreign_rows(tmp_path):
    store = MergeOperationStore(tmp_path / "shared.db")
    clock = Clock()
    own = create(store)
    foreign = create(store, "acme/other")
    for operation in (own, foreign):
        store.reserve_attempt(operation.identity, EffectName.MERGE, now=clock())
    foreign_running = store.get(foreign.identity)
    scheduler = bind(store, "acme/widgets", clock, Mock())
    await scheduler._recover_interrupted()
    assert store.get(foreign.identity) == foreign_running
    assert store.get(own.identity).effect(EffectName.MERGE).state is EffectState.DELIVERY_UNKNOWN
    own_recovered = store.get(own.identity)
    store.defer_resumption(own_recovered, not_before=150.0, now=clock())
    foreign_waiting = create(store, "acme/other", number=88)
    store.defer_resumption(foreign_waiting, not_before=101.0, now=clock())
    assert scheduler._next_delay() == 50.0
    assert store.due(101.0, repository="acme/widgets") == []
    other = bind(store, "acme/other", clock, Mock())
    await other._recover_interrupted()
    assert store.get(foreign.identity).effect(EffectName.MERGE).state is EffectState.DELIVERY_UNKNOWN


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["404", "escaping", "typed"])
async def test_production_failure_is_durably_paced_across_due_checks_and_restart(tmp_path, monkeypatch, failure):
    path = tmp_path / "shared.db"
    store = MergeOperationStore(path)
    operation = create_with_prior_effects(store)
    clock = Clock(now=operation.not_before)
    engine = Mock()
    request = httpx.Request("GET", "https://api.github.com/repos/acme/widgets/pulls/5476")
    error = httpx.HTTPStatusError("Not Found", request=request, response=httpx.Response(404, request=request)) if failure == "404" else RuntimeError("refresh failed")
    if failure == "typed":
        error = GitHubRequestError(Mock(status=503, classification=GitHubApiOutcome.REMOTE_ERROR))
    engine.github.get_pull_request_metadata_strict.side_effect = error
    monkeypatch.setattr(TraceCollector, "_instance", None)
    collector = TraceCollector()
    scheduler = bind(store, "acme/widgets", clock, engine)
    await dispatch(scheduler)
    saved = store.get(operation.identity)
    assert saved.not_before == clock() + RESUME_RETRY_INTERVAL_SECONDS
    assert saved.effects == operation.effects
    assert saved.status is OperationStatus.WAITING
    engine._process_single_candidate.assert_not_called()
    finished = [event for event in collector.get_snapshot().events if event.kind == EventKind.EXECUTION_FINISHED.value]
    assert len(finished) == 1
    assert finished[0].outcome == (Outcome.DEFERRED.value if failure == "typed" else Outcome.FAILED.value)
    restarted = bind(MergeOperationStore(path), "acme/widgets", clock, engine)
    await restarted._recover_interrupted()
    for candidate in (scheduler, restarted):
        for _ in range(5):
            await dispatch(candidate)
    assert engine.github.get_pull_request_metadata_strict.call_count == 1
    clock.now = saved.not_before
    engine.github.get_pull_request_metadata_strict.side_effect = None
    engine.github.get_pull_request_metadata_strict.return_value = {"number": 5476, "head": {"sha": "head"}}
    engine.github.get_pr_details.side_effect = lambda raw: raw

    def complete(repository, candidate, *, origin):
        assert repository == operation.identity.repository
        assert candidate.data["number"] == 5476
        assert origin == "merge-operation-resumption"
        reservation = store.reserve_attempt(operation.identity, EffectName.MERGE, now=clock())
        assert reservation.granted
        receipt = EffectReceipt(ConfirmationSource.OWN_RESPONSE, merge_commit_sha="merged", target_head_sha="head")
        store.record_confirmed_complete(operation.identity, EffectName.MERGE, reservation.attempt_id, reservation.generation, receipt, now=clock())
        return Mock(target_outcome=None, capacity_deferred=False, refill_retry_required=False, error=None, success=True)

    engine._process_single_candidate.side_effect = complete
    await dispatch(restarted)
    assert store.get(operation.identity).status is OperationStatus.MERGE_CONFIRMED
    clock.now += 1000
    await dispatch(restarted)
    assert engine.github.get_pull_request_metadata_strict.call_count == 2


@pytest.mark.asyncio
async def test_run_loop_consumes_failure_self_wake_without_retrying_before_deadline(tmp_path, monkeypatch):
    store = MergeOperationStore(tmp_path / "shared.db")
    operation = create_with_prior_effects(store)
    clock = Clock(now=operation.not_before)
    engine = Mock()
    engine.github.get_pull_request_metadata_strict.side_effect = RuntimeError("refresh failed")
    scheduler = bind(store, "acme/widgets", clock, engine)
    rounds = asyncio.Queue()
    real_dispatch = scheduler._dispatch_due

    async def observe_dispatch():
        # Run the real dispatch and callback to completion before observing a
        # round. This barrier leaves the callback's real wake event intact;
        # it does not inject a wake, advance time, or replace due selection.
        await real_dispatch()
        if scheduler._tasks:
            await asyncio.gather(*list(scheduler._tasks))
        rounds.put_nowait((engine.github.get_pull_request_metadata_strict.call_count, store.get(operation.identity), scheduler._wake_event.is_set()))

    monkeypatch.setattr(scheduler, "_dispatch_due", observe_dispatch)
    shutdown = asyncio.Event()
    running = asyncio.create_task(scheduler.run(shutdown))

    async def next_round():
        # The timeout is only a deadlock bound, never the pacing oracle.
        return await asyncio.wait_for(rounds.get(), timeout=5)

    try:
        attempts, saved, callback_wake_pending = await next_round()
        assert attempts == 1
        assert callback_wake_pending is True
        deadline = clock() + RESUME_RETRY_INTERVAL_SECONDS
        assert saved.not_before == deadline
        assert saved.effect(EffectName.APPROVAL).receipt.review_id == "review-123"
        assert saved.effect(EffectName.MERGE).throttle_attempts == 1
        assert saved.effect(EffectName.MERGE).throttled_attempt_ids == ("prior-throttle",)
        assert saved.effect(EffectName.MERGE).last_error == "prior throttle observation"
        assert saved.effects == operation.effects

        # No test wake occurs here: only _run_claimed's completion wake can
        # drive this second pass while fake time remains at the first attempt.
        assert await next_round() == (1, saved, False)
        for now in (clock(), deadline - 0.001):
            clock.now = now
            scheduler.wake()
            assert await next_round() == (1, saved, False)
        clock.now = deadline
        scheduler.wake()
        attempts, retried, callback_wake_pending = await next_round()
        assert attempts == 2
        assert callback_wake_pending is True
        assert retried.not_before == deadline + RESUME_RETRY_INTERVAL_SECONDS
        assert retried.effects == operation.effects
        assert await next_round() == (2, retried, False)
        engine._process_single_candidate.assert_not_called()
    finally:
        shutdown.set()
        scheduler.wake()
        await asyncio.wait_for(running, timeout=5)


@pytest.mark.asyncio
async def test_persistence_failure_keeps_local_floor_and_reports_failure(tmp_path, monkeypatch):
    store = MergeOperationStore(tmp_path / "shared.db")
    operation = create(store)
    clock = Clock()
    scheduler = MergeOperationScheduler(store, clock=clock)
    resume = Mock(side_effect=RuntimeError("escaped"))
    scheduler.register_resume_handler(resume, repository="acme/widgets")
    monkeypatch.setattr(store, "defer_resumption", Mock(side_effect=MergeOperationPersistenceError("unavailable")))
    await dispatch(scheduler)
    for _ in range(5):
        await dispatch(scheduler)
    assert resume.call_count == 1
    assert store.get(operation.identity) == operation
    clock.now += RESUME_RETRY_INTERVAL_SECONDS
    await dispatch(scheduler)
    assert resume.call_count == 2


@pytest.mark.parametrize("transition", ["future", "short_future", "new_generation", "blocked", "superseded", "running"])
def test_pacing_preserves_authoritative_transitions(tmp_path, transition):
    store = MergeOperationStore(tmp_path / "shared.db")
    original = create(store)
    if transition in {"future", "short_future"}:
        reservation = store.reserve_attempt(original.identity, EffectName.MERGE, now=100.0)
        store.defer_local(original.identity, EffectName.MERGE, reservation.attempt_id, reservation.generation, is_real_throttle=True, throttle_attempt_id="throttle", retry_after_seconds=200.0 if transition == "future" else 1.0, now=100.0)
    elif transition == "new_generation":
        create(store, head="new-head")
    elif transition == "superseded":
        store.supersede(original.identity, now=100.0)
    else:
        reservation = store.reserve_attempt(original.identity, EffectName.MERGE, now=100.0)
        if transition == "blocked":
            store.operationally_block(original.identity, EffectName.MERGE, reservation.attempt_id, reservation.generation, BlockReason.AUTHENTICATION, now=100.0)
    expected = store.get(original.identity)
    store.defer_resumption(original, not_before=130.0, now=100.0)
    assert store.get(original.identity) == expected


@pytest.mark.asyncio
async def test_binding_is_required_immutable_and_not_process_global():
    first = get_merge_operation_scheduler()
    second = get_merge_operation_scheduler()
    assert first is not second
    with pytest.raises(ValueError, match="bound"):
        await first.run(asyncio.Event())
    first.register_resume_handler(Mock(), repository="acme/widgets")
    with pytest.raises(ValueError, match="bound"):
        first.register_resume_handler(Mock(), repository="acme/other")
    second.register_resume_handler(Mock(), repository="acme/other")
