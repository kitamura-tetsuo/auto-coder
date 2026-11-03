"""Issue #2722: scheduler wakes must preserve ordinary admission authority."""

import asyncio
import inspect
import threading
import time
from unittest.mock import MagicMock

import pytest

from auto_coder.automation_config import ExplicitTargetOutcome
from auto_coder.automation_engine import ISSUE_PROCESSING_STAGE, _issue_content_revision, _IssueProcessingStageHandler
from auto_coder.github_pending_work import PendingWorkScheduler, StageOutcome, WorkIdentity
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.issue_dispatch import DispatchOutcome, DispatchResult, IssueAttemptIdentity
from auto_coder.ordinary_issue_adapter import IssueDispatchExecution
from auto_coder.specification_validation_lifecycle import SpecificationValidationLifecycle
from tests.test_pending_work_resumption import _admission_deferral, _admitted_issue_engine


async def reached(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.002)


def existing_owner(monkeypatch, tmp_path):
    issue = {"id": 70, "number": 7, "title": "Retained", "body": "## Requirements\nREQ-001: Preserve behavior.", "labels": [], "state": "open", "user": {"id": 1}}
    engine, store = _admitted_issue_engine(monkeypatch, tmp_path, issue)
    engine.config.jules_mode = False
    engine._get_authoritative_parent_number = MagicMock(return_value=None)
    engine._preflight_explicit_issue_relationships = MagicMock(return_value=dict(issue))
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    owner = ImplementationOwner("issue", 7)
    execution = slots.start_execution(owner)
    assert execution is not None
    assert slots.record_provider_session(owner, "existing-provider")
    slots.finish_execution(owner, execution)
    engine.implementation_slots = slots
    identity = WorkIdentity("owner/repo", "issue:7", ISSUE_PROCESSING_STAGE, _issue_content_revision(issue))
    store.defer(identity, _admission_deferral(), ("confirmed-already", "refresh", "implementation"))
    store.complete_effect(identity, "confirmed-already")
    assert store.manual_retry(identity, now=time.time() - 1) is not None
    return engine, store, identity, slots, owner


@pytest.mark.parametrize("timing", ["before-expiry", "after-expiry", "external-idle", "external-thread", "external-inflight", "unrelated-completion"])
@pytest.mark.parametrize("unrelated_deadline", [None, 1, 3600, "due-missing-handler"])
@pytest.mark.asyncio
async def test_real_protected_owner_deferred_preserves_armed_wait(tmp_path, monkeypatch, timing, unrelated_deadline):
    engine, store, identity, slots, owner = existing_owner(monkeypatch, tmp_path)
    now = [time.time()]
    if unrelated_deadline is not None:
        unrelated = WorkIdentity("owner/repo", "issue:8", "future-other-stage", "future-other-revision")
        store.defer(unrelated, _admission_deferral(), ("other",))
        deadline = now[0] - 1 if unrelated_deadline == "due-missing-handler" else now[0] + unrelated_deadline
        assert store.manual_retry(unrelated, now=deadline) is not None
    entered = []
    outcomes = []
    reader_times = []
    barrier = threading.Event()
    reader = engine.github.get_issue_dispatch_snapshot_strict.return_value

    def strict(repo, number):
        assert (repo, number) == ("owner/repo", 7)
        reader_times.append(now[0])
        return dict(reader)

    engine.github.get_issue_dispatch_snapshot_strict.side_effect = strict
    normal = engine._process_single_candidate_unified
    dispositions = []

    def observe(*args, **kwargs):
        assert not kwargs.get("force") and not kwargs.get("manual_retry")
        result = normal(*args, **kwargs)
        dispositions.append(result)
        return result

    engine._process_single_candidate_unified = observe

    class ObservedHandler(_IssueProcessingStageHandler):
        def dispatch(self, obligation):
            if not entered:
                now[0] += 7  # Entry is observably later than regular wait arm.
            entered.append(now[0])
            result = super().dispatch(obligation)
            outcomes.append(result)
            if len(entered) == 1:
                assert barrier.wait(5)
            return result

    arms = []

    class ObservedScheduler(PendingWorkScheduler):
        def _next_delay(self):
            delay = super()._next_delay()
            arms.append((now[0], delay))
            return delay

    scheduler = ObservedScheduler(store, repository="owner/repo", poll_interval=30, clock=lambda: now[0])
    engine.pending_work_scheduler = scheduler
    scheduler.register_handler(ISSUE_PROCESSING_STAGE, ObservedHandler(engine, "owner/repo"))

    class FutureOtherHandler:
        def dispatch(self, obligation):
            return StageOutcome(completed_effects=("other",))

        recover = dispatch

    if unrelated_deadline != "due-missing-handler":
        scheduler.register_handler("future-other-stage", FutureOtherHandler())
    shutdown = asyncio.Event()
    task = asyncio.create_task(scheduler.run(shutdown))
    try:
        await reached(lambda: scheduler._wake_event is not None)
        scheduler.wake()
        await reached(lambda: len(outcomes) == 1)
        expiry = next(arm + 30 for arm, delay in reversed(arms) if delay > 0)
        assert expiry == pytest.approx(entered[0] + 23, rel=0, abs=1e-6)
        assert scheduler._regular_wait_deadline == pytest.approx(expiry, rel=0, abs=1e-6)
        assert outcomes == [StageOutcome()]
        assert dispositions[-1].target_outcome is ExplicitTargetOutcome.DEFERRED
        assert "ownership already exists" in dispositions[-1].actions[0]
        during = store.get(identity)
        assert during.status == "running"
        assert during.unfinished_effects == ("refresh", "implementation")
        retained_attempts = during.throttle_attempts

        if timing == "after-expiry":
            now[0] = expiry + 1
            scheduler._wake_event.set()  # Timer observation, not an independent wake.
            await asyncio.sleep(0.03)
            assert len(entered) == 1
        elif timing == "external-inflight":
            scheduler.wake()
            await asyncio.sleep(0.03)
            assert len(entered) == 1
        if timing != "after-expiry":
            now[0] += 5  # Settlement is also later than handler entry.
        barrier.set()
        await reached(lambda: not scheduler._in_flight)

        if timing in {"after-expiry", "external-inflight"}:
            await reached(lambda: len(outcomes) >= 2)
        else:
            await asyncio.sleep(0.03)
            assert len(entered) == 1
            retained = store.get(identity)
            assert retained.status == "waiting"
            assert retained.identity == identity
            assert retained.unfinished_effects == ("refresh", "implementation")
            assert retained.throttle_attempts == retained_attempts
            assert scheduler._next_delay() == pytest.approx(expiry - now[0])
            if timing == "unrelated-completion":
                unrelated = WorkIdentity("owner/repo", "issue:8", "unrelated-stage", "other-revision")

                class OtherHandler:
                    def dispatch(self, obligation):
                        return StageOutcome(completed_effects=("other",))

                    recover = dispatch

                scheduler.register_handler("unrelated-stage", OtherHandler())
                store.defer(unrelated, _admission_deferral(), ("other",))
                assert store.manual_retry(unrelated, now=now[0] - 1) is not None
                scheduler._notify()  # Internal settlement, never an independent wake.
                await reached(lambda: store.get(unrelated) is None)
                await reached(lambda: not scheduler._in_flight)
                await asyncio.sleep(0.03)
                assert len(entered) == 1
            if timing in {"external-idle", "external-thread"}:
                if timing == "external-thread":
                    await asyncio.to_thread(scheduler.wake)
                else:
                    scheduler.wake()
            else:
                now[0] = expiry
                scheduler._wake_event.set()
            await reached(lambda: len(outcomes) >= 2)
        # Entry precedes the real strict reader; observe returned outcomes
        # before inspecting reader timestamps, even under a thread switch.
        assert entered[1] == (expiry + 1 if timing == "after-expiry" else expiry if timing in {"before-expiry", "unrelated-completion"} else entered[0] + 5)
        assert reader_times[0] == entered[0]
        assert reader_times[-1] == entered[1]
        assert slots.active_owners() == (owner,)
        assert slots.snapshot().owners[0].provider_sessions == ("existing-provider",)
        assert all(result.target_outcome is ExplicitTargetOutcome.DEFERRED for result in dispositions)
        assert all(result == StageOutcome() for result in outcomes)
        await reached(lambda: not scheduler._in_flight)
        # A second and third unchanged deferral must each retain the newly
        # armed regular wait; checking only the first retry misses a spin.
        await asyncio.sleep(0.03)
        assert len(entered) == 2
        second_expiry = next(arm + 30 for arm, delay in reversed(arms) if delay > 0)
        now[0] = second_expiry
        scheduler._wake_event.set()
        await reached(lambda: len(entered) == 3 and not scheduler._in_flight)
        await asyncio.sleep(0.03)
        assert len(entered) == 3
    finally:
        barrier.set()
        shutdown.set()
        await asyncio.wait_for(task, 3)

    retained = store.get(identity)
    assert retained is not None
    assert retained.unfinished_effects == ("refresh", "implementation")
    # Process-local cadence state must not replace or erase durable recovery.
    reopened = type(store)(tmp_path / "pending.db")
    assert reopened.get(identity) == retained


@pytest.mark.parametrize("independent_wake", [False, True])
@pytest.mark.parametrize("allowed", [False, True, "after-deferred"])
@pytest.mark.asyncio
async def test_wake_preserves_real_common_allow_or_author_refusal(tmp_path, monkeypatch, independent_wake, allowed):
    engine, store, identity, slots, owner = existing_owner(monkeypatch, tmp_path)
    retained_owner = allowed == "after-deferred"
    if not retained_owner:
        assert slots.finish_provider_session(owner, "existing-provider")
        assert slots.active_owners() == ()
    engine._is_issue_author_allowed = MagicMock(return_value=bool(allowed))
    engine._get_specification_validator = MagicMock(return_value=SpecificationValidationLifecycle("owner/repo", "test", tmp_path / "validation.json"))
    engine.github.get_item_type_strict = MagicMock(return_value="issue")
    engine.github.get_parent_issue_details_strict = MagicMock(return_value=None)
    engine.github.get_open_sub_issues_strict = MagicMock(return_value=[])
    issue = engine.github.get_issue_dispatch_snapshot_strict.return_value
    issue["labels"] = [{"name": "implementation-ready"}]
    issue["updated_at"] = "2026-01-01T00:00:00Z"
    engine.github.get_issue_strict = MagicMock(return_value=dict(issue))
    engine.github.get_issue = MagicMock(return_value=dict(issue))
    engine._reconcile_validation_snapshot.return_value = dict(issue)
    effects = []

    def backend(repo, snapshot, config, github, candidates, **kwargs):
        assert (repo, snapshot["number"]) == ("owner/repo", 7)
        assert kwargs.get("retry_authority") is None
        assert slots.current_execution_id(owner)
        effects.append("ordinary-implementation")
        return IssueDispatchExecution(DispatchResult(IssueAttemptIdentity("owner", "repo", 7, "test-attempt"), DispatchOutcome.LOCAL_COMPLETED), ["Implemented through the admitted backend boundary"])

    monkeypatch.setattr("auto_coder.issue_processor._dispatch_issue_candidates", backend)
    monkeypatch.setattr("auto_coder.issue_processor._ordinary_issue_candidates", lambda repo: ["test-backend"])
    dispositions = []
    common = engine._process_single_candidate_unified
    signature = inspect.signature(common)

    def observe(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        assert not bound.arguments["force"] and not bound.arguments["retry"]
        result = common(*args, **kwargs)
        if not bound.arguments["generation_serialized"]:
            dispositions.append(result)
        return result

    engine._process_single_candidate_unified = observe
    scheduler = PendingWorkScheduler(store, repository="owner/repo", poll_interval=0.03)
    engine.pending_work_scheduler = scheduler
    settle = threading.Event()

    class TransitionHandler(_IssueProcessingStageHandler):
        def dispatch(self, obligation):
            result = super().dispatch(obligation)
            if retained_owner and len(dispositions) == 1:
                assert settle.wait(5)
            return result

    scheduler.register_handler(ISSUE_PROCESSING_STAGE, TransitionHandler(engine, "owner/repo"))
    shutdown = asyncio.Event()
    task = asyncio.create_task(scheduler.run(shutdown))
    try:
        await reached(lambda: scheduler._wake_event is not None)
        if independent_wake:
            scheduler.wake()
        if retained_owner:
            await reached(lambda: len(dispositions) >= 1)
            assert dispositions[0].target_outcome is ExplicitTargetOutcome.DEFERRED
            assert effects == []
            assert store.get(identity).unfinished_effects == ("refresh", "implementation")
            assert slots.finish_provider_session(owner, "existing-provider")
            settle.set()
            # No webhook, restart, force or new external wake: the ordinary
            # timer must reconsider ownership through the same common path.
        await reached(lambda: store.get(identity) is None)
    finally:
        settle.set()
        shutdown.set()
        await asyncio.wait_for(task, 3)
    assert len(dispositions) == (2 if retained_owner else 1)
    result = dispositions[-1]
    assert result.target_outcome is (ExplicitTargetOutcome.SUCCESS if allowed else ExplicitTargetOutcome.SKIPPED), result
    assert result.success is bool(allowed)
    assert effects == (["ordinary-implementation"] if allowed else [])
    assert engine.github.get_issue_dispatch_snapshot_strict.called


@pytest.mark.parametrize("scan_elapsed", [7, 31])
@pytest.mark.parametrize("delay_before_spawn", [False, True])
@pytest.mark.asyncio
async def test_scan_delay_preserves_the_wait_expiry_of_the_dispatched_issue(tmp_path, monkeypatch, scan_elapsed, delay_before_spawn):
    engine, store, identity, slots, owner = existing_owner(monkeypatch, tmp_path)
    now = [time.time()]
    outcomes = []
    dispatched_expiries = []

    class ObservedHandler(_IssueProcessingStageHandler):
        def dispatch(self, obligation):
            result = super().dispatch(obligation)
            outcomes.append(result)
            return result

    class DelayedScanScheduler(PendingWorkScheduler):
        async def _dispatch_due(self):
            first = not dispatched_expiries
            if first:
                dispatched_expiries.append(self._regular_wait_deadline)
                if delay_before_spawn:
                    now[0] = dispatched_expiries[0] - 30 + scan_elapsed
            await super()._dispatch_due()
            if first and not delay_before_spawn:
                # The actual due scan has scheduled the production handler,
                # but its coroutine has not read or claimed the durable row.
                # Time passes before run arms its following controller wait.
                now[0] = dispatched_expiries[0] - 30 + scan_elapsed

    scheduler = DelayedScanScheduler(store, repository="owner/repo", poll_interval=30, clock=lambda: now[0])
    scheduler.register_handler(ISSUE_PROCESSING_STAGE, ObservedHandler(engine, "owner/repo"))
    shutdown = asyncio.Event()
    task = asyncio.create_task(scheduler.run(shutdown))
    try:
        await reached(lambda: scheduler._wake_event is not None)
        scheduler.wake()
        if scan_elapsed < 30:
            await reached(lambda: len(outcomes) == 1 and not scheduler._in_flight)
            assert scheduler._unchanged_waits[identity.key()][0] == dispatched_expiries[0]
            await asyncio.sleep(0.03)
            assert len(outcomes) == 1
            now[0] = dispatched_expiries[0]
            scheduler._wake_event.set()  # Existing timer expiry, no new wake authority.
        await reached(lambda: len(outcomes) >= 2)
        assert now[0] == dispatched_expiries[0] + max(0, scan_elapsed - 30)
        assert outcomes[:2] == [StageOutcome(), StageOutcome()]
        assert slots.active_owners() == (owner,)
        assert slots.snapshot().owners[0].provider_sessions == ("existing-provider",)
        await reached(lambda: not scheduler._in_flight)
        retained = store.get(identity)
        assert retained is not None and retained.status == "waiting"
        assert retained.unfinished_effects == ("refresh", "implementation")
        await asyncio.sleep(0.03)
        assert len(outcomes) == 2
    finally:
        shutdown.set()
        await asyncio.wait_for(task, 3)
