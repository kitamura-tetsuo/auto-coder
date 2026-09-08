"""Daemon-lifecycle resumption for durable merge operations (Issue #1939).

``MergeOperationStore`` (Issue #1937) already owns exclusive-execution
rights, backoff deadlines, and correlated receipts for one PR's
approval/merge effects; ``merge_operation_adapter`` (Issue #1938) already
owns turning a real GitHub exchange into one of those typed outcomes. What
was still missing is *waking normal PR processing again* once a deferred
effect's deadline has passed, without a busy-poll loop and without
depending on a webhook (REQ-004).

This module is deliberately a thin sibling of
``github_pending_work.PendingWorkScheduler`` rather than a new stage
registered on it: that scheduler's obligations are indexed and deferred
through ``PendingWorkStore``'s own ``GitHubRequestError``-shaped schema,
while a merge operation's per-effect deadlines, throttle counters, and
receipts are already fully and correctly owned by ``MergeOperationStore``.
Duplicating that bookkeeping into a second store just to fit one scheduler
class would itself become the kind of second source of truth REQ-004 warns
against, so this scheduler reads deadlines directly from
``MergeOperationStore.due()``/``next_due_at()`` instead, while reusing the
exact same wake/sleep-until-due run loop contract.

Resuming a due operation never re-implements CI/review/thread/adversarial
revalidation here: it hands the operation's identity to a caller-registered
``resume`` callback, which (in production) is
``AutomationEngine._process_single_candidate`` -- the same entrypoint used
for both a freshly discovered PR and one resumed by
``pr_processor.PR_PROCESSING_STAGE``. Normal PR processing already
re-establishes current CI/review/thread/mergeability conditions before
calling back into ``pr_processor._merge_pr``, which is what actually
advances the durable operation's still-unfinished effect through the
adapter (REQ-005). This module's only job is timing.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Callable

from .logger_config import get_logger
from .merge_operation_state import MergeOperation, MergeOperationPersistenceError, MergeOperationStore

logger = get_logger(__name__)

DEFAULT_POLL_INTERVAL_SECONDS = 1.0

# The callback resumes normal processing for one PR; it does not return a
# result to this scheduler because the durable operation and its effects are
# the source of truth for what happened, not this loop's own bookkeeping.
ResumeCallback = Callable[[MergeOperation], None]


class MergeOperationScheduler:
    """Daemon service that resumes durable merge operations once due."""

    def __init__(
        self,
        store: MergeOperationStore,
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._poll_interval = poll_interval
        self._clock = clock
        self._resume: ResumeCallback | None = None
        self._wake_event: asyncio.Event | None = None
        self._in_flight: set[str] = set()
        self._in_flight_lock = threading.Lock()
        self._tasks: set[asyncio.Task[None]] = set()

    def register_resume_handler(self, resume: ResumeCallback) -> None:
        """Register the callback that resumes normal PR processing for one operation."""
        self._resume = resume

    def wake(self) -> None:
        """Interrupt an idle wait, e.g. right after a new deadline was recorded."""
        event = self._wake_event
        if event is not None and not event.is_set():
            event.set()

    def snapshot(self) -> list[dict[str, object]]:
        """Observable view of every retained merge operation for status reporting."""
        try:
            operations = self._store.all_operations()
        except MergeOperationPersistenceError as exc:
            logger.error("Merge-operation status snapshot unavailable: {}", exc)
            return []
        result: list[dict[str, object]] = []
        for operation in operations:
            result.append(
                {
                    "repository": operation.identity.repository,
                    "pr_number": operation.identity.pr_number,
                    "expected_head_sha": operation.expected_head_sha,
                    "status": operation.status.value,
                    "resume_reason": operation.resume_reason,
                    "not_before": operation.not_before,
                    "effects": {name.value: effect.state.value for name, effect in operation.effects.items()},
                }
            )
        return result

    async def run(self, shutdown_event: asyncio.Event) -> None:
        """Recover interrupted effects, then resume due operations until shutdown.

        Cancellation leaves any in-flight resume's operation exactly as
        ``MergeOperationStore`` last recorded it -- this loop owns none of
        that state itself, so a genuine crash and a cooperative shutdown are
        recovered identically on the next start via
        ``MergeOperationStore.recover_after_restart()``.
        """
        self._wake_event = asyncio.Event()
        try:
            await self._recover_interrupted()
            while not shutdown_event.is_set():
                delay = self._next_delay()
                wake_wait = asyncio.ensure_future(self._wake_event.wait())
                shutdown_wait = asyncio.ensure_future(shutdown_event.wait())
                try:
                    await asyncio.wait({wake_wait, shutdown_wait}, timeout=delay, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in (wake_wait, shutdown_wait):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(wake_wait, shutdown_wait, return_exceptions=True)
                self._wake_event.clear()
                if shutdown_event.is_set():
                    return
                await self._dispatch_due()
        finally:
            pending_tasks = list(self._tasks)
            for pending_task in pending_tasks:
                pending_task.cancel()
            if pending_tasks:
                await asyncio.gather(*pending_tasks, return_exceptions=True)

    def _next_delay(self) -> float:
        try:
            upcoming = self._store.next_due_at()
        except MergeOperationPersistenceError:
            return self._poll_interval
        now = self._clock()
        if upcoming is None or upcoming <= now:
            # A due-now operation that cannot make progress (no resume
            # handler registered, or one that just re-deferred it) must not
            # spin the loop at zero delay; wait at least one poll interval
            # unless an explicit wake cuts it short.
            return self._poll_interval
        return min(upcoming - now, max(self._poll_interval, 3600.0))

    async def _recover_interrupted(self) -> None:
        try:
            recovered = await asyncio.to_thread(self._store.recover_after_restart)
        except MergeOperationPersistenceError as exc:
            logger.error("Could not recover interrupted merge operations: {}", exc)
            return
        for operation in recovered:
            logger.warning(
                "Merge operation {} left an effect interrupted before a terminal outcome was recorded; it now waits for reconciliation",
                operation.identity.key(),
            )
        self.wake()

    async def _dispatch_due(self) -> None:
        resume = self._resume
        if resume is None:
            return
        try:
            due = await asyncio.to_thread(self._store.due, self._clock())
        except MergeOperationPersistenceError as exc:
            logger.error("Could not read due merge operations: {}", exc)
            return
        for operation in due:
            key = operation.identity.key()
            with self._in_flight_lock:
                if key in self._in_flight:
                    continue
                self._in_flight.add(key)
            self._spawn(operation, resume)

    def _spawn(self, operation: MergeOperation, resume: ResumeCallback) -> None:
        task = asyncio.ensure_future(self._run_claimed(operation, resume))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_claimed(self, operation: MergeOperation, resume: ResumeCallback) -> None:
        key = operation.identity.key()
        try:
            try:
                await asyncio.to_thread(resume, operation)
            except Exception as exc:
                logger.opt(exception=True).error("Merge-operation resume handler raised for {}: {}", key, exc)
        finally:
            with self._in_flight_lock:
                self._in_flight.discard(key)
        self.wake()


_LOCK = threading.Lock()
_DEFAULT_SCHEDULER: MergeOperationScheduler | None = None


def get_merge_operation_scheduler() -> MergeOperationScheduler:
    """Return the process-wide merge-operation resumption service."""
    global _DEFAULT_SCHEDULER
    with _LOCK:
        if _DEFAULT_SCHEDULER is None:
            from .merge_operation_state import get_merge_operation_store

            _DEFAULT_SCHEDULER = MergeOperationScheduler(get_merge_operation_store())
        return _DEFAULT_SCHEDULER
