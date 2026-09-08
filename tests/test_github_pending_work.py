from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

from auto_coder.github_pending_work import (
    MAX_THROTTLED_RETRIES,
    ObligationStatus,
    PendingReason,
    PendingWorkScheduler,
    PendingWorkStore,
    StageOutcome,
    WorkIdentity,
)
from auto_coder.util.github_request_outcome import (
    DeliveryCertainty,
    GitHubApiOutcome,
    GitHubRequestContext,
    GitHubRequestError,
    GitHubRequestOutcome,
    GitHubRequestRefused,
    GitHubResponseMetadata,
    RequestProvenance,
)


def _error(classification, *, delivery=DeliveryCertainty.HTTP_RESPONSE_RECEIVED, retry_after=5):
    outcome = GitHubRequestOutcome(
        GitHubRequestContext("operation", "attempt", "test", "https://api.github.com", "GET", "read", "/repos/{owner}/{repo}"),
        None if classification is GitHubApiOutcome.REFUSED else 403,
        classification,
        RequestProvenance.NETWORK,
        delivery,
        GitHubResponseMetadata(retry_after_seconds=retry_after),
        1,
    )
    return GitHubRequestRefused(outcome) if classification is GitHubApiOutcome.REFUSED else GitHubRequestError(outcome)


def test_obligation_survives_restart_and_effects_complete_independently(tmp_path):
    path = tmp_path / "pending.db"
    identity = WorkIdentity("acme/widgets", "issue:12", "validation", "submission-a")
    store = PendingWorkStore(path)
    saved = store.defer(identity, _error(GitHubApiOutcome.SECONDARY_THROTTLED), ("diagnostic", "readiness-withdrawal"), now=100)

    restarted = PendingWorkStore(path)
    assert restarted.due(now=104) == []
    assert restarted.due(now=105) == [saved]
    assert restarted.complete_effect(identity, "diagnostic") is True
    assert restarted.due(now=105)[0].unfinished_effects == ("readiness-withdrawal",)
    assert restarted.complete_effect(identity, "readiness-withdrawal") is True
    assert restarted.due(now=105) == []


def test_admission_deferral_does_not_spend_throttle_attempt(tmp_path):
    store = PendingWorkStore(tmp_path / "pending.db")
    identity = WorkIdentity("acme/widgets", "pr:4", "ci", "head")
    for _ in range(8):
        result = store.defer(identity, _error(GitHubApiOutcome.REFUSED, retry_after=0), ("ci",), governor_deadline=200, now=100)
    assert result.reason is PendingReason.ADMISSION_DEFERRED
    assert result.throttle_attempts == 0
    assert result.not_before == 200


def test_retry_bound_is_semantic_and_survives_restart(tmp_path):
    path = tmp_path / "pending.db"
    identity = WorkIdentity("acme/widgets", "startup", "enumeration", "config-v1")
    for attempt in range(MAX_THROTTLED_RETRIES + 1):
        result = PendingWorkStore(path).defer(
            identity,
            _error(GitHubApiOutcome.PRIMARY_THROTTLED),
            ("complete-authoritative-scan",),
            now=100 + attempt,
        )
    assert result.throttle_attempts == 4
    assert result.reason is PendingReason.RETRIES_EXHAUSTED
    assert result.automatically_retryable is False
    assert PendingWorkStore(path).due(now=1000) == []


def test_auth_and_indeterminate_delivery_are_operational_blocks(tmp_path):
    store = PendingWorkStore(tmp_path / "pending.db")
    auth = store.defer(WorkIdentity("a/b", "issue:1", "read"), _error(GitHubApiOutcome.AUTHENTICATION_FAILURE), ("read",), now=1)
    ambiguous = store.defer(
        WorkIdentity("a/b", "issue:2", "publish"),
        _error(GitHubApiOutcome.TRANSPORT_FAILURE, delivery=DeliveryCertainty.INDETERMINATE),
        ("reconcile-publication",),
        now=1,
    )
    assert auth.reason is PendingReason.AUTHENTICATION
    assert ambiguous.reason is PendingReason.INDETERMINATE
    assert store.due(now=10_000) == []


def test_legacy_schema_without_status_column_defaults_to_waiting(tmp_path):
    """A preflight-schema database predates the status column; rows must stay resumable."""
    path = tmp_path / "pending.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE github_pending_work (
            work_key TEXT PRIMARY KEY, repository TEXT NOT NULL, entity TEXT NOT NULL,
            stage TEXT NOT NULL, revision TEXT NOT NULL, reason TEXT NOT NULL,
            not_before REAL NOT NULL, unfinished_effects TEXT NOT NULL,
            throttle_attempts INTEGER NOT NULL, last_error TEXT NOT NULL,
            updated_at REAL NOT NULL)"""
        )
        identity = WorkIdentity("a/b", "issue:1", "validation", "rev-1")
        connection.execute(
            "INSERT INTO github_pending_work VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (identity.key(), "a/b", "issue:1", "validation", "rev-1", "throttled", 50.0, "[]", 1, "boom", 10.0),
        )

    store = PendingWorkStore(path)
    obligations = store.all_pending()
    assert len(obligations) == 1
    assert obligations[0].status == ObligationStatus.WAITING.value
    assert store.due(now=100) != []
    assert store.due(now=100)[0].identity == identity


class _RecordingHandler:
    """Controlled stage handler for scheduler tests."""

    def __init__(self):
        self.dispatched: list[WorkIdentity] = []
        self.recovered: list[WorkIdentity] = []
        self.dispatch_result: StageOutcome = StageOutcome(completed_effects=("effect",))
        self.recover_result: StageOutcome = StageOutcome(completed_effects=("effect",))
        self.dispatch_event: asyncio.Event | None = None

    def dispatch(self, obligation):
        self.dispatched.append(obligation.identity)
        return self.dispatch_result

    def recover(self, obligation):
        self.recovered.append(obligation.identity)
        return self.recover_result


async def _run_until(condition, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met before timeout")


@pytest.mark.asyncio
async def test_scheduler_dispatches_due_obligation_to_registered_handler(tmp_path):
    """AS-001: retained work reaches its handler once the local deadline is eligible."""
    store = PendingWorkStore(tmp_path / "pending.db")
    identity = WorkIdentity("acme/widgets", "issue:1", "validation", "rev-a")
    now = time.time()
    store.defer(identity, _error(GitHubApiOutcome.SECONDARY_THROTTLED, retry_after=0.05), ("effect",), now=now)

    scheduler = PendingWorkScheduler(store, poll_interval=0.05)
    handler = _RecordingHandler()
    scheduler.register_handler("validation", handler)

    shutdown = asyncio.Event()
    task = asyncio.create_task(scheduler.run(shutdown))
    try:
        await _run_until(lambda: handler.dispatched == [identity])
        await _run_until(lambda: store.get(identity) is None)
    finally:
        shutdown.set()
        await task


@pytest.mark.asyncio
async def test_scheduler_leaves_unknown_stage_visibly_blocked(tmp_path):
    """REQ-001: an obligation for an unregistered stage is never silently discarded."""
    store = PendingWorkStore(tmp_path / "pending.db")
    identity = WorkIdentity("acme/widgets", "issue:2", "unknown-stage", "rev-a")
    store.defer(identity, _error(GitHubApiOutcome.SECONDARY_THROTTLED, retry_after=0), ("effect",), now=time.time())

    scheduler = PendingWorkScheduler(store, poll_interval=0.05)
    shutdown = asyncio.Event()
    task = asyncio.create_task(scheduler.run(shutdown))
    try:
        await asyncio.sleep(0.2)
        snapshot = scheduler.snapshot()
        assert len(snapshot) == 1
        assert snapshot[0]["blocked"] == "no registered stage handler"
        assert store.get(identity) is not None
    finally:
        shutdown.set()
        await task


@pytest.mark.asyncio
async def test_scheduler_recovers_interrupted_work_on_restart_instead_of_dispatch(tmp_path):
    """AS-002: work left 'running' by a stopped controller enters recovery, not a fresh dispatch."""
    path = tmp_path / "pending.db"
    store = PendingWorkStore(path)
    identity = WorkIdentity("acme/widgets", "issue:3", "validation", "rev-a")
    store.defer(identity, _error(GitHubApiOutcome.SECONDARY_THROTTLED, retry_after=0), ("effect",), now=time.time())
    store.mark_running(identity)  # simulate a controller that crashed mid-dispatch

    scheduler = PendingWorkScheduler(store, poll_interval=0.05)
    handler = _RecordingHandler()
    scheduler.register_handler("validation", handler)
    shutdown = asyncio.Event()
    task = asyncio.create_task(scheduler.run(shutdown))
    try:
        await _run_until(lambda: handler.recovered == [identity])
        assert handler.dispatched == []
    finally:
        shutdown.set()
        await task


@pytest.mark.asyncio
async def test_scheduler_shutdown_leaves_in_flight_obligation_running_for_recovery(tmp_path):
    """REQ-007/REQ-008: shutdown is not a completion event; an in-flight claim survives it."""
    store = PendingWorkStore(tmp_path / "pending.db")
    identity = WorkIdentity("acme/widgets", "issue:4", "validation", "rev-a")
    store.defer(identity, _error(GitHubApiOutcome.SECONDARY_THROTTLED, retry_after=0), ("effect",), now=time.time())

    scheduler = PendingWorkScheduler(store, poll_interval=0.05)

    started = asyncio.Event()
    release = asyncio.Event()

    class _SlowHandler:
        def dispatch(self, obligation):
            started.set()
            # Block the worker thread until the test allows it to proceed;
            # the scheduler must be cancellable while this runs.
            import asyncio as _asyncio
            import concurrent.futures

            future = concurrent.futures.Future()

            def wait_and_set():
                import time as _time

                while not release.is_set():
                    _time.sleep(0.01)
                future.set_result(None)

            import threading as _threading

            _threading.Thread(target=wait_and_set, daemon=True).start()
            future.result(timeout=5)
            return StageOutcome(completed_effects=("effect",))

        def recover(self, obligation):
            raise AssertionError("recover should not run while shutdown is racing dispatch")

    scheduler.register_handler("validation", _SlowHandler())
    shutdown = asyncio.Event()
    task = asyncio.create_task(scheduler.run(shutdown))
    try:
        await _run_until(lambda: started.is_set())
        assert store.get(identity).status == ObligationStatus.RUNNING.value
        shutdown.set()
        await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    finally:
        release.set()

    # The obligation must remain retained and 'running', not deleted or
    # silently marked complete, so a fresh scheduler recovers it.
    remaining = store.get(identity)
    assert remaining is not None
    assert remaining.status == ObligationStatus.RUNNING.value
    assert remaining.unfinished_effects == ("effect",)


def test_manual_retry_resets_count_and_deadline_without_touching_effects(tmp_path):
    """REQ-007: an explicit manual retry clears only the selected retry block/count."""
    store = PendingWorkStore(tmp_path / "pending.db")
    identity = WorkIdentity("acme/widgets", "issue:5", "publish", "rev-a")
    for attempt in range(MAX_THROTTLED_RETRIES + 1):
        result = store.defer(identity, _error(GitHubApiOutcome.PRIMARY_THROTTLED), ("publish-effect",), now=100 + attempt)
    assert result.reason is PendingReason.RETRIES_EXHAUSTED
    assert store.due(now=10_000) == []

    retried = store.manual_retry(identity, now=10_000)
    assert retried is not None
    assert retried.throttle_attempts == 0
    assert retried.unfinished_effects == ("publish-effect",)
    assert store.due(now=10_000) == [retried]

    assert store.manual_retry(WorkIdentity("a/b", "issue:404", "stage")) is None


@pytest.mark.asyncio
async def test_scheduler_manual_retry_wakes_idle_loop(tmp_path):
    """A manual retry must offer the obligation to its handler without waiting a full poll cycle."""
    store = PendingWorkStore(tmp_path / "pending.db")
    identity = WorkIdentity("acme/widgets", "issue:6", "validation", "rev-a")
    for attempt in range(MAX_THROTTLED_RETRIES + 1):
        store.defer(identity, _error(GitHubApiOutcome.PRIMARY_THROTTLED), ("effect",), now=100 + attempt)
    assert store.due(now=10_000) == []

    scheduler = PendingWorkScheduler(store, poll_interval=30.0)
    handler = _RecordingHandler()
    scheduler.register_handler("validation", handler)
    shutdown = asyncio.Event()
    task = asyncio.create_task(scheduler.run(shutdown))
    try:
        await asyncio.sleep(0.05)
        assert scheduler.manual_retry(identity) is not None
        await _run_until(lambda: handler.dispatched == [identity], timeout=2.0)
    finally:
        shutdown.set()
        await task


@pytest.mark.asyncio
async def test_scheduler_stage_error_redefers_remaining_effects(tmp_path):
    """A stage handler reporting an error re-defers only its still-unfinished effects."""
    store = PendingWorkStore(tmp_path / "pending.db")
    identity = WorkIdentity("acme/widgets", "issue:7", "validation", "rev-a")
    store.defer(identity, _error(GitHubApiOutcome.SECONDARY_THROTTLED, retry_after=0), ("first", "second"), now=time.time())

    class _PartialHandler:
        def __init__(self):
            self.calls = 0

        def dispatch(self, obligation):
            self.calls += 1
            return StageOutcome(completed_effects=("first",), error=_error(GitHubApiOutcome.SECONDARY_THROTTLED, retry_after=100))

        def recover(self, obligation):
            raise AssertionError("not expected")

    scheduler = PendingWorkScheduler(store, poll_interval=0.05)
    handler = _PartialHandler()
    scheduler.register_handler("validation", handler)
    shutdown = asyncio.Event()
    task = asyncio.create_task(scheduler.run(shutdown))
    try:
        await _run_until(lambda: handler.calls >= 1)
        await asyncio.sleep(0.1)
        remaining = store.get(identity)
        assert remaining is not None
        assert remaining.unfinished_effects == ("second",)
        assert remaining.status == ObligationStatus.WAITING.value
        assert remaining.not_before > time.time() + 50
    finally:
        shutdown.set()
        await task
